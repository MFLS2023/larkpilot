# -*- coding: utf-8 -*-
"""扫描本机的 Claude Code / Codex 会话，判断活跃度。

飞书桥和网页共用这一个模块。

## 关于「这个窗口还开着吗」

判断不了。试过四种办法全部失败：
  读进程命令行参数        62 个 node/claude 进程，命令行里没有 session_id
  查 Temp/claude 目录时间  反映的是「上次跑后台任务」，不是窗口状态
  handle64 -u ".claude"   空
  handle64 -u "<sid>"     空（handle64 本身好的，搜 "log" 有一堆输出）

根因：Claude 写 JSONL 是追加一行、立刻关闭，不长期持有句柄，
所以操作系统层面看不出哪个进程属于哪个会话。

替代方案是按文件最后修改时间分三档，见 LEVELS。
反正发消息是新起进程续接（claude -p -r <sid>），跟窗口开没开无关，
分档只影响列表好不好看。
"""

import glob
import io
import json
import os
import re
import time
from datetime import datetime

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")
HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_STATE = os.path.join(HERE, "bridge_state.json")

# 三档活跃度的分界线（秒）
ACTIVE_WITHIN = 300        # 🟢 5 分钟内有写入
RECENT_WITHIN = 43200      # 🟡 12 小时内有写入（2026-08-24 从 2 小时放宽，你要的）
# 更早的都算 ⚪

# 判「没了下文」的门槛。比 ACTIVE_WITHIN 宽得多：跑长活（大工具调用、
# 等权限确认）十几分钟不写记录很正常，之前用 5 分钟就喊「断了」，
# 2026-08-23 实测一天误报 252 个 —— 桌面上明明显示还在跑。
# 宁可晚点报断，也别把还在干的吓成断的。
STALLED_AFTER = 900

LEVELS = {
    "active": "🟢 活跃",
    "recent": "🟡 可能开着",
    "stale": "⚪ 已停",
}

# 从文件尾部读多少行够判断状态。
# Claude 的一轮对话可能有几十条 tool_use 记录，200 行能盖住最后好几轮。
TAIL_LINES = 200


def _epoch(ts):
    """ISO 时间串转秒。认不出来返回 None。"""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _tail(path, n=TAIL_LINES):
    """读文件最后 n 行。

    大文件不能整个读进内存 —— 实测有的会话文件 8.9 MB。
    从尾部往前按块读，凑够 n 行就停。
    """
    try:
        size = os.path.getsize(path)
    except Exception:
        return []
    if size == 0:
        return []
    block = 64 * 1024
    try:
        with io.open(path, "rb") as f:
            if size <= block * 4:
                # 小文件直接读完，省得来回 seek
                data = f.read()
            else:
                chunks, pos, found = [], size, 0
                while pos > 0 and found < n + 1:
                    step = min(block, pos)
                    pos -= step
                    f.seek(pos)
                    c = f.read(step)
                    chunks.insert(0, c)
                    found += c.count(b"\n")
                data = b"".join(chunks)
    except Exception:
        return []
    text = data.decode("utf-8", "replace")
    return text.splitlines()[-n:]


def _head(path, n=5):
    """读文件开头 n 行。Codex 的 session_meta 在第一行，只能从头读。"""
    out = []
    try:
        with io.open(path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= n:
                    break
                out.append(line)
    except Exception:
        pass
    return out


def _cwd_from_head(path):
    """从 Codex 文件开头的 session_meta 里取工作目录。"""
    for line in _head(path):
        if '"session_meta"' not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "session_meta":
            return str((rec.get("payload") or {}).get("cwd") or "")
    return ""


def _level_of(last_time):
    """按最后活动时间分档。"""
    age = time.time() - last_time
    if age <= ACTIVE_WITHIN:
        return "active"
    if age <= RECENT_WITHIN:
        return "recent"
    return "stale"


def _text_from_content(content):
    """从 Claude 的 content 里抠出纯文本。

    content 可能是字符串，也可能是 [{"type":"text","text":...}, {"type":"tool_use",...}]
    这种块列表。只要 text 块。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "text":
            t = blk.get("text")
            if t:
                return str(t)
    # 全是 tool_use 之类的块，没有文字
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "tool_use":
            return "（在用工具：%s）" % (blk.get("name") or "?")
    return ""


def _text_from_codex_content(content):
    """从 Codex response_item/message 的 content 里抠出纯文本。

    content 可能是字符串，也可能是块列表：
        用户消息  [{"type":"input_text","text":...}, ...]
        助手消息  [{"type":"output_text","text":...}, ...]
    实测于 2026-08-23 的新格式 rollout 文件。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for blk in content:
        # 不死抠块的 type 名（input_text/output_text 是今天的叫法，
        # 上游改个名这里就瞎了）—— 只要是带非空 text 字符串的块就收。
        if isinstance(blk, dict) and isinstance(blk.get("text"), str) and blk["text"]:
            parts.append(blk["text"])
    return "\n".join(parts)


def _codex_injected(t):
    """Codex 会往 user 消息里塞系统注入，不是用户真说的话。

    实测两种：<environment_context>（工作目录/shell/日期）和
    AGENTS.md 全文（开头是「# AGENTS.md instructions」）。都跳过，
    不然详情页每轮前面挂两大段配置，真正的对话反而被淹没。
    """
    s = str(t or "").lstrip()
    return s.startswith("<") or s.startswith("# AGENTS.md")


def _clean(s, n=100):
    """压成一行，截断。列表里显示用。"""
    s = " ".join(str(s or "").split())
    return s[:n]


# 匹配路径：带盘符的（C:\Users\...）、Unix 长路径（/home/xxx/...）、
# 以及不带盘符但含反斜杠的相对路径（WeChat Files\wxid_xxx\...）
_PATH_RE = re.compile(
    r"[A-Za-z]:\\[^\s\"']+"                      # C:\Users\...
    r"|/(?:home|Users|mnt|var|opt)/[^\s\"']+"    # /home/xxx/...
    r"|[^\s\"'\\/]+(?:\\[^\s\"'\\/]+){2,}"       # a\b\c 至少三段的相对路径
)


def _shorten_paths(s):
    """把标题里的完整路径压成文件名。

    「读一下 C:\\Users\\<你>\\.claude\\hooks\\notify_config.json」
    → 「读一下 notify_config.json」

    为什么要做：第一句话经常是「读一下 <一长串路径>」，整条路径挤满标题栏，
    真正有信息的部分（文件名）反而被截断掉了。
    """
    def repl(m):
        p = m.group(0).rstrip("\\/")
        # 取最后一段（文件名或目录名）
        tail = re.split(r"[\\/]", p)[-1]
        return tail or p
    return _PATH_RE.sub(repl, str(s or ""))


def _line_names():
    """读飞书桥的状态文件，拿 session_id → 线名 的映射。

    这样网页列表里能显示「改网站」而不是一串 UUID —— 飞书起的线有名字。
    """
    out = {}
    try:
        st = json.loads(io.open(BRIDGE_STATE, encoding="utf-8-sig").read())
    except Exception:
        return out
    for name, ln in (st.get("lines") or {}).items():
        sid = ln.get("session_id")
        if sid:
            out[sid] = name
    return out


ENTRYPOINT_CN = {
    "claude-desktop-3p": "Claude Desktop",
    "cli":               "Claude Code",
    "sdk-cli":           "SDK/自动化",
    "vscode":            "VSCode 插件",
    "jetbrains":         "JetBrains 插件",
    "intellij":          "JetBrains 插件",
    "ide":               "IDE 插件",
}


def _client_name(entrypoint):
    """把 entrypoint 值翻译成人话。认不出来就原样保留。"""
    ep = str(entrypoint or "").strip().lower()
    for k, v in ENTRYPOINT_CN.items():
        if k in ep:
            return v
    return entrypoint or "Claude"


def scan_claude_sessions():
    """扫 Claude Code / Claude 客户端的会话。

    两个客户端写的是同一个地方（~/.claude/projects/<项目>/<session_id>.jsonl），
    所以这一个函数全包了，不用分开处理。
    """
    sessions = []
    try:
        files = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl"))
    except Exception:
        return sessions

    for fpath in files:
        sid = os.path.basename(fpath)[:-6]      # 去掉 .jsonl
        try:
            mtime = os.path.getmtime(fpath)
        except Exception:
            continue

        last_msg, status, cwd, entrypoint, first_user_msg, custom_title = "", "idle", "", "", "", ""
        found_role = False   # 已经拿到状态和最后一条消息了
        for line in reversed(_tail(fpath)):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            if not cwd:
                cwd = str(rec.get("cwd") or "")
            if not entrypoint:
                entrypoint = str(rec.get("entrypoint") or "")
            # custom-title 可能在 assistant 消息之前（倒序扫时是「之后」），
            # 所以不能在找到 role 之后就 break，要继续扫直到也拿到 custom_title
            if not custom_title and rec.get("type") == "custom-title":
                t = str(rec.get("customTitle") or "").strip()
                if t:
                    custom_title = t

            if not found_role:
                msg = rec.get("message")
                if isinstance(msg, dict):
                    role = msg.get("role")
                    if role == "assistant":
                        found_role = True
                        status = "done"
                        last_msg = _clean(_text_from_content(msg.get("content"))) or "（没有文字输出）"
                    elif role == "user":
                        found_role = True
                        status = "running"
                        last_msg = "你说：" + (_clean(_text_from_content(msg.get("content")), 80) or "…")

            # 两样都拿到了才停
            if found_role and custom_title:
                break

        # entrypoint 在文件头部，尾部可能读不到，单独扫头部几行
        if not entrypoint:
            for line in _head(fpath, 10):
                try:
                    rec = json.loads(line)
                    if rec.get("entrypoint"):
                        entrypoint = str(rec["entrypoint"])
                        if not cwd and rec.get("cwd"):
                            cwd = str(rec["cwd"])
                        break
                except Exception:
                    pass

        # 尝试读第一句用户消息当标题摘要
        # Claude Desktop 对话通常没有项目名，用第一句话能快速认出是哪个对话
        if not first_user_msg:
            for line in _head(fpath, 30):
                try:
                    rec = json.loads(line)
                    msg = rec.get("message")
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        t = _text_from_content(msg.get("content"))
                        if not t:
                            continue
                        # 跳过系统注入的内容：XML 块、技能路径、以 < 开头等
                        if t.startswith("<") or t.startswith("Base directory"):
                            continue
                        if len(t.strip()) < 4:   # 太短的也跳过（单字或空）
                            continue
                        # 先把长路径压成文件名，不然「读一下 C:\Users\...\x.json」
                        # 整条路径挤满标题，真正有用的文件名反而被截掉
                        first_user_msg = _clean(_shorten_paths(t), 60)
                        break
                except Exception:
                    pass

        if status == "running" and (time.time() - mtime) > STALLED_AFTER:
            status = "stalled"

        project = os.path.basename(cwd.rstrip("\\/")) or os.path.basename(os.path.dirname(fpath))
        client = _client_name(entrypoint)

        sessions.append({
            "session_id": sid,
            "engine": "claude",
            "client": client,
            "entrypoint": entrypoint,
            "custom_title": custom_title,  # Claude Desktop 右键重命名设的标题
            "first_msg": first_user_msg,
            "project": project,
            "cwd": cwd,
            "last_time": mtime,
            "last_msg": last_msg,
            "status": status,
            "level": _level_of(mtime),
        })
    return sessions


# ── 第三方工具的会话源 ─────────────────────────────────────────
# 来源清单从 clients.json 读（没有就退回内置的 Pi 一条）。
# 字段说明：
#   name       列表徽章显示名
#   engine     引擎号（全局唯一，续接分发和筛选都靠它）
#   dir        历史目录
#   glob       相对 dir 的匹配式，缺省 **/*.jsonl（递归）
#   extractor  内容抽取器风格："envelope"（pi 式，顶层 type=='message'）
#              或 "claude"（Claude Code 式，顶层 type=='user'/'assistant'）
#   can_run    True = bridge 里有对应的 runner 函数，能从手机续接；
#              缺省 False = 只读来源，网页端拦下发送
# 加新工具 = clients.json 加一段 + （若格式是全新家族）加一个抽取函数 +
#           （若要续接）bridge 里照 run_pi 模板写个 runner 进 RUNNERS。
CLIENTS_PATH = os.path.join(HERE, "clients.json")

_DEFAULT_EXTRA_SOURCES = [
    {"name": "Pi", "engine": "pi",
     "dir": os.path.join(HOME, ".pi", "agent", "sessions"),
     "glob": os.path.join("**", "*.jsonl"),
     "extractor": "envelope",
     "can_run": True},
]


def load_extra_sources():
    """读 clients.json。文件缺失/写坏都不致命 —— 退回内置默认并继续跑。"""
    try:
        d = json.loads(io.open(CLIENTS_PATH, encoding="utf-8-sig").read())
        srcs = d.get("sources")
        if not isinstance(srcs, list) or not srcs:
            return [dict(x) for x in _DEFAULT_EXTRA_SOURCES]
        out = []
        for s in srcs:
            if not isinstance(s, dict):
                continue
            if not s.get("name") or not s.get("engine") or not s.get("dir"):
                continue                     # 三要素不全的直接跳过
            # 配置示例允许 ~，glob 不会替我们展开用户目录。
            s["dir"] = os.path.expanduser(str(s["dir"]))
            s.setdefault("glob", os.path.join("**", "*.jsonl"))
            s.setdefault("extractor", "envelope")
            s.setdefault("can_run", False)
            out.append(s)
        return out or [dict(x) for x in _DEFAULT_EXTRA_SOURCES]
    except Exception:
        return [dict(x) for x in _DEFAULT_EXTRA_SOURCES]


EXTRA_SOURCES = load_extra_sources()


def _extra_msg_of(rec, style):
    """从一条记录里取 (role, text)。不是消息或该过滤的返回 None。

    两种家族：
      envelope（pi 等）: 顶层 type=='message'，真身在 rec['message'] 里
      claude           : 顶层 type=='user'/'assistant'，真身也在 message 字段
    """
    m = rec.get("message")
    if style == "claude":
        if rec.get("type") in ("user", "assistant") and isinstance(m, dict):
            role = m.get("role") or rec.get("type")
            if role in ("user", "assistant"):
                return (role, _text_from_content(m.get("content")).strip())
        return None
    # envelope 默认
    if rec.get("type") != "message" or not isinstance(m, dict):
        return None
    role = m.get("role")
    if role not in ("user", "assistant"):
        return None
    return (role, _text_from_content(m.get("content")).strip())


def _sid_from_name(name):
    """从文件名末尾抠 UUID 当 session_id（pi 的命名习惯：时间戳_UUID.jsonl）。"""
    m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                  r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$", str(name))
    return m.group(1) if m else None


def scan_extra_sessions():
    """扫第三方工具的会话目录（clients.json 里登记的来源）。

    这些工具自带历史存储，不经过 Claude/Codex 的目录，所以单独挂进来。
    能不能从手机续接由 can_run + bridge 的 runner 注册表决定。

    抽取按 extractor 风格分派（envelope / claude），细节见 _extra_msg_of；
    助手的 content 空占位记录要跳过继续往前找（pi 实测有这种行）。
    """
    sessions = []
    for src in EXTRA_SOURCES:
        style = src.get("extractor") or "envelope"
        try:
            files = glob.glob(os.path.join(src["dir"], src.get("glob")
                                           or os.path.join("**", "*.jsonl")),
                              recursive=True)
        except Exception:
            continue

        for fpath in files:
            sid = _sid_from_name(os.path.basename(fpath))
            if not sid:
                continue
            try:
                mtime = os.path.getmtime(fpath)
            except Exception:
                continue

            last_msg, status, cwd = "", "idle", ""
            for line in reversed(_tail(fpath)):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") == "session" and not cwd:
                    cwd = str(rec.get("cwd") or "")
                    continue
                got = _extra_msg_of(rec, style)
                if not got:
                    continue
                role, text = got
                if role == "assistant":
                    if text:
                        status, last_msg = "done", _clean(text)
                        break
                    continue        # content 为空的占位记录，不是真回复
                if role == "user" and text and not _codex_injected(text):
                    status = "running"
                    last_msg = "你说：" + (_clean(text, 80) or "…")
                    break

            # 头部有 cwd / 元信息，尾部扫不到时从头补读
            if not cwd:
                for line in _head(fpath, 3):
                    try:
                        rec = json.loads(line)
                        if rec.get("cwd"):
                            cwd = str(rec["cwd"])
                            break
                    except Exception:
                        pass

            first_user_msg = ""
            for line in _head(fpath, 30):
                if '"text"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                got = _extra_msg_of(rec, style)
                if got and got[0] == "user" and got[1]:
                    t = got[1]
                    if len(t) >= 4 and not _codex_injected(t):
                        first_user_msg = _clean(_shorten_paths(t), 60)
                        break

            if status == "running" and (time.time() - mtime) > STALLED_AFTER:
                status = "stalled"

            sessions.append({
                "session_id": sid,
                "engine": src["engine"],
                "client": src["name"],
                "entrypoint": src["engine"],
                "custom_title": "",
                "first_msg": first_user_msg,
                "project": os.path.basename(cwd.rstrip("\\/")) or src["name"],
                "cwd": cwd,
                "last_time": mtime,
                "last_msg": last_msg,
                "status": status,
                "level": _level_of(mtime),
            })
    return sessions


def scan_codex_sessions():
    """扫 Codex 的会话。

    Codex 的 JSONL 结构跟 Claude 完全不同，字段名是实测出来的（不是猜的）：
        {"type":"session_meta","payload":{"session_id":...,"cwd":...}}
        {"type":"event_msg","payload":{"type":"user_message","message":"..."}}
        {"type":"event_msg","payload":{"type":"agent_message","message":"..."}}
        {"type":"event_msg","payload":{"type":"task_complete","last_agent_message":"..."}}
    文件名 rollout-<ISO时间>-<session_id>.jsonl，按 年/月/日 三层目录放。
    """
    sessions = []
    try:
        files = glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*.jsonl"))
    except Exception:
        return sessions

    # 同一个 session_id 可能有多个 rollout 文件（续接过），取最新那个
    best = {}
    for fpath in files:
        name = os.path.basename(fpath)[len("rollout-"):-len(".jsonl")]
        # 文件名形如 2026-08-09T00-04-04-019fe21d-ff9c-7cc0-951c-cccb8e3ca754
        # session_id 是 UUID（5 段）。切供应商后从旧会话续接会分叉出双 ID：
        #   rollout-2026-08-23T13-37-03-<旧id>_<新id>.jsonl
        # 这时从后往前数 5 段会把两个 id 各取一半拼成错的，resume 会失败。
        # 所以先用正则锚定结尾的完整 UUID（那才是续接后的新 id），数段只兜底。
        m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                      r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", name)
        sid = m.group(0) if m else "-".join(name.split("-")[-5:])
        try:
            m = os.path.getmtime(fpath)
        except Exception:
            continue
        if sid not in best or m > best[sid][0]:
            best[sid] = (m, fpath)

    for sid, (mtime, fpath) in best.items():
        last_msg, status, cwd = "", "idle", ""
        # 倒序找「最新的状态记录」：第一条能定性的记录说了算，找到就停。
        # 为什么不分家族各收一套再比优先级：实测（2026-08-23）有的会话文件尾
        # 是 task_complete（干完了），但最后一条 user 消息在它前面几行 ——
        # 按家族优先会把「已完成」误判成「还在跑」，静默 5 分钟后又显示成
        # 「断了」。谁最新听谁的才符合直觉。
        # 两种格式都认；message 的外层类型名不死抠（今天是 response_item）。
        for line in reversed(_tail(fpath)):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue

            p = rec.get("payload")
            if not isinstance(p, dict):
                continue
            t = rec.get("type")

            if t == "session_meta" and not cwd:
                cwd = str(p.get("cwd") or "")
                continue

            pt = p.get("type")
            if t == "event_msg":
                if pt == "task_complete":
                    status = "done"
                    last_msg = _clean(p.get("last_agent_message")) or "（没有文字输出）"
                    break
                if pt == "agent_message":
                    status = "done"
                    last_msg = _clean(p.get("message")) or "（没有文字输出）"
                    break
                if pt == "user_message":
                    status = "running"
                    last_msg = "你说：" + (_clean(p.get("message"), 80) or "…")
                    break
                if pt == "turn_aborted":
                    # 用户主动停的（Esc / 停止按钮），不是出事，别算「断了」
                    status = "aborted"
                    last_msg = "（这轮被手动中止了）"
                    break
                if pt == "error":
                    status = "error"
                    last_msg = "出错：" + _clean(p.get("message") or p.get("error"), 80)
                    break
            elif pt == "message":
                role = p.get("role")
                text = _text_from_codex_content(p.get("content")).strip()
                if role == "assistant" and text:
                    status = "done"
                    last_msg = _clean(text) or "（没有文字输出）"
                    break
                if role == "user" and text and not _codex_injected(text):
                    status = "running"
                    last_msg = "你说：" + (_clean(text, 80) or "…")
                    break

        # session_meta 在文件**开头**（第一行），倒着读永远碰不到。
        # 所以单独从头部读几行找它 —— 之前用 _tail 找是错的，那是读尾部。
        if not cwd:
            cwd = _cwd_from_head(fpath)

        # 读第一句用户消息。Codex 会话的项目名都是 cwd 目录名，
        # 同一个目录下开好几个会话就会全部重名（实测 8 个都叫 claude-files），
        # 列表里没法区分。第一句话能区分。
        # 新格式没有 event_msg/user_message，改认 input_text 块。
        first_user_msg = ""
        for line in _head(fpath, 60):
            if '"user_message"' not in line and '"input_text"' not in line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            p = rec.get("payload") or {}
            t = ""
            if p.get("type") == "user_message":
                t = str(p.get("message") or "").strip()
            elif (rec.get("type") == "response_item"
                    and p.get("type") == "message"
                    and p.get("role") == "user"):
                t = _text_from_codex_content(p.get("content")).strip()
            if t and len(t) >= 4 and not _codex_injected(t):
                first_user_msg = _clean(_shorten_paths(t), 60)
                break

        if status == "running" and (time.time() - mtime) > STALLED_AFTER:
            status = "stalled"

        sessions.append({
            "session_id": sid,
            "engine": "codex",
            "client": "Codex",
            "entrypoint": "codex",
            "custom_title": "",
            "first_msg": first_user_msg,
            "project": os.path.basename(cwd.rstrip("\\/")) or "Codex",
            "cwd": cwd,
            "last_time": mtime,
            "last_msg": last_msg,
            "status": status,
            "level": _level_of(mtime),
        })
    return sessions


def scan_all(level=None):
    """扫所有会话，按最后活动时间倒序。

    level 过滤：
        "active"  只要 🟢
        "recent"  🟢 + 🟡（列表页默认，511 个历史会话不能全铺出来）
        None/"all" 全都要
    """
    import zcode_sessions
    zcode = zcode_sessions.scan()
    for session in zcode:
        session["level"] = _level_of(session["last_time"])
    import antigravity_sessions
    agy = antigravity_sessions.scan()
    for session in agy:
        session["level"] = _level_of(session["last_time"])
    all_s = scan_claude_sessions() + scan_codex_sessions() + scan_extra_sessions() + zcode + agy

    # ── 先算出每个项目名被几个会话占用，重名的不能只显示项目名 ──
    # 实测过：同一个目录下开 8 个 Codex 会话，全都叫 claude-files，
    # 你在飞书看到 8 个一样的条目，不知道该发几号。
    proj_count = {}
    for s in all_s:
        p = s.get("project") or ""
        proj_count[p] = proj_count.get(p, 0) + 1

    # 第一句话也可能重复（跑测试时同一句话开了好几个会话）
    first_count = {}
    for s in all_s:
        f = (s.get("first_msg") or "")[:40]
        if f:
            first_count[f] = first_count.get(f, 0) + 1

    names = _line_names()
    for s in all_s:
        n = names.get(s["session_id"])
        if n:
            s["line_name"] = n
            s["title"] = n
            continue

        s["line_name"] = None
        custom = s.get("custom_title") or ""
        proj = s.get("project") or ""
        first = (s.get("first_msg") or "")[:40]
        # 「能认出来」的判据是唯一，不是非空 —— 被好几个会话共用的名字等于没有名字
        # 排除「项目名=主目录名」这种没有信息量的情况（动态取，不硬编码用户名）
        proj_unique = (proj and proj not in (".claude", os.path.basename(HOME))
                       and proj_count.get(proj, 0) == 1)
        first_unique = first and first_count.get(first, 0) == 1

        # 优先级：① 你改的标题 ② 飞书线名（上面处理了）
        #          ③ 唯一的第一句话 ④ 唯一的项目名 ⑤ 名字+会话号
        #
        # ★ 第一句话排在项目名前面。实测过反过来的后果：cwd 恰好是
        #   Downloads / skills 这种唯一目录名时，标题就变成「skills」，
        #   完全看不出这个会话在干什么，而第一句话「知识库 进入这个项目…」
        #   一眼就懂。目录名只在没有第一句话时才有用。
        #
        # 另外：Claude Code（cli）根本没有 customTitle 字段（实测 20 个会话
        # 全是 0 个），它 /resume 列表里的标题就是第一句话的截断 ——
        # 所以这一档同时也是「跟 Claude Code 自己显示的标题保持一致」。
        if custom:
            s["title"] = custom
        elif first_unique:
            s["title"] = first
        elif proj_unique:
            s["title"] = proj
        else:
            # 重名了，必须带上会话号才能区分。
            # ★ 用尾部 6 位不用头部 —— Codex 的 id 是时间戳前缀（019fe44d…），
            #   同一天生成的头部完全一样，取头部等于没区分（实测 8 个都是 019fe4）
            base = first[:26] or proj or "会话"
            s["title"] = "%s · %s" % (base, s["session_id"][-6:])

        # 确保 client 字段存在（Codex 那边已设，Claude 那边新加的）
        if "client" not in s:
            s["client"] = "Claude"

    if level == "active":
        all_s = [s for s in all_s if s["level"] == "active"]
    elif level == "recent":
        all_s = [s for s in all_s if s["level"] in ("active", "recent")]

    all_s.sort(key=lambda x: x["last_time"], reverse=True)

    # 用户隐藏的会话不进列表（文件原样保留，可随时恢复）
    try:
        hidden = _hidden_sids()
        if hidden:
            all_s = [s for s in all_s if s["session_id"] not in hidden]
    except Exception:
        pass
    return all_s


def get_session(sid, max_turns=30):
    """读某个会话的对话历史，给详情页用。

    返回 {"session_id","engine","project","turns":[{"role","text","time"}...]}
    找不到返回 None。max_turns 是「最多几轮」，一轮 = 你说一句 + 它回一句。
    """
    if isinstance(sid, str) and sid.startswith("sess_"):
        import zcode_sessions
        return zcode_sessions.history(sid, max_turns)

    # Antigravity 的会话号和 Claude 一样是 uuid 格式，先查它的摘要库：
    # 命中就走 antigravity，没命中才落到下面的 Claude/Codex 查找
    if isinstance(sid, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", sid or ""):
        import antigravity_sessions
        hit = antigravity_sessions.history(sid, max_turns)
        if hit:
            return hit

    # 先在 Claude 那边找
    hits = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "%s.jsonl" % sid))
    if hits:
        return _history_claude(hits[0], sid, max_turns)

    # 再在 Codex 那边找（文件名带时间前缀，用通配）
    hits = glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*%s.jsonl" % sid))
    if hits:
        hits.sort(key=os.path.getmtime, reverse=True)
        return _history_codex(hits[0], sid, max_turns)

    # 最后在第三方工具目录里找（clients.json 登记的来源，按各自风格解析）
    for src in EXTRA_SOURCES:
        hits = glob.glob(os.path.join(src["dir"], "**", "*%s.jsonl" % sid),
                         recursive=True)
        if not hits:
            continue
        hits.sort(key=os.path.getmtime, reverse=True)
        return _history_extra(hits[0], sid, max_turns,
                              src.get("extractor") or "envelope",
                              src.get("engine") or src["name"],
                              src.get("name") or src["engine"])
    return None


def _history_extra(fpath, sid, max_turns, style="envelope",
                   engine="extra", label=None):
    """第三方来源的对话抽取。与扫描共用 _extra_msg_of，风格一致。"""
    turns, cwd = [], ""
    for line in _tail(fpath, max_turns * 40):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "session" or rec.get("cwd"):
            cwd = cwd or str(rec.get("cwd") or "")
        got = _extra_msg_of(rec, style)
        if not got:
            continue
        role, text = got
        if text and not _codex_injected(text):
            turns.append({"role": role, "text": text[:4000],
                          "time": _epoch(rec.get("timestamp")) or 0})
    return {
        "session_id": sid,
        "engine": engine,
        "project": os.path.basename(cwd.rstrip("\\/")) or (label or "Extra"),
        "cwd": cwd,
        "turns": turns[-(max_turns * 2):],
    }


def _history_claude(fpath, sid, max_turns):
    """从 Claude 的 JSONL 里抠出对话。"""
    turns, cwd = [], ""
    # 一轮最多几十条记录，读 max_turns * 40 行足够
    for line in _tail(fpath, max_turns * 40):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if not cwd:
            cwd = str(rec.get("cwd") or "")
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _text_from_content(msg.get("content"))
        if not text:
            continue
        turns.append({
            "role": role,
            "text": text[:4000],
            "time": _epoch(rec.get("timestamp")) or 0,
        })
    return {
        "session_id": sid,
        "engine": "claude",
        "project": os.path.basename(cwd.rstrip("\\/")) or os.path.basename(os.path.dirname(fpath)),
        "cwd": cwd,
        "turns": turns[-(max_turns * 2):],
    }


def _history_codex(fpath, sid, max_turns):
    """从 Codex 的 JSONL 里抠出对话。

    两种格式都认（2026-08-23 实测）：
        老格式  event_msg/user_message、agent_message —— payload.message 直接是文本
        新格式  response_item/message，文本在 content 块的 input_text/output_text 里，
                且 user 消息混着 AGENTS.md / <environment_context> 注入，要过滤
    新格式文件里两种记录可能并存，优先 response_item 这套，避免同一轮读两次。
    """
    turns_resp, turns_ev, cwd = [], [], ""
    for line in _tail(fpath, max_turns * 40):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        p = rec.get("payload")
        if not isinstance(p, dict):
            continue
        if rec.get("type") == "session_meta":
            cwd = str(p.get("cwd") or "")
            continue
        ts = _epoch(rec.get("timestamp")) or 0

        # 同样宽容：认「带角色的 message」，不认外层类型名
        if p.get("type") == "message":
            role = p.get("role")
            text = _text_from_codex_content(p.get("content")).strip()
            if role in ("user", "assistant") and text and not _codex_injected(text):
                turns_resp.append(
                    {"role": role, "text": text[:4000], "time": ts})
        elif rec.get("type") == "event_msg":
            pt = p.get("type")
            if pt == "user_message":
                turns_ev.append(
                    {"role": "user", "text": str(p.get("message") or "")[:4000], "time": ts})
            elif pt == "agent_message":
                turns_ev.append(
                    {"role": "assistant", "text": str(p.get("message") or "")[:4000], "time": ts})

    turns = turns_resp or turns_ev
    return {
        "session_id": sid,
        "engine": "codex",
        "project": os.path.basename(cwd.rstrip("\\/")) or "Codex",
        "cwd": cwd,
        "turns": turns[-(max_turns * 2):],
    }


def get_usage(sid):
    """取某个会话最近一轮的 token 用量（尽力而为，读不到返回空字典）。

    实测记录结构：
        Codex   event_msg/token_count → payload.info.last_token_usage /
                total_token_usage，字段 input_tokens / output_tokens
        Claude  assistant 消息 message.usage，输入要算上缓存读/写三件套
    Claude 没有累计值（要全文件扫，不值当），只报最近一轮；
    Codex 两种都给。
    """
    def num(v):
        try:
            return int(v)
        except Exception:
            return None

    hits = glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*",
                                  "rollout-*%s.jsonl" % sid))
    if hits:
        hits.sort(key=os.path.getmtime, reverse=True)
        last_in = last_out = tot_in = tot_out = None
        for line in reversed(_tail(hits[0], 400)):
            if '"token_count"' not in line:
                continue
            try:
                info = ((json.loads(line).get("payload") or {}).get("info")
                        or {})
            except Exception:
                continue
            lt, tt = info.get("last_token_usage") or {}, \
                info.get("total_token_usage") or {}
            if last_in is None and lt:
                last_in, last_out = num(lt.get("input_tokens")), \
                    num(lt.get("output_tokens"))
            if tot_in is None and tt:
                tot_in, tot_out = num(tt.get("input_tokens")), \
                    num(tt.get("output_tokens"))
            if last_in is not None and tot_in is not None:
                break
        return {"engine": "codex", "last_in": last_in, "last_out": last_out,
                "total_in": tot_in, "total_out": tot_out}

    hits = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "%s.jsonl" % sid))
    if hits:
        last_in = last_out = None
        for line in reversed(_tail(hits[0], 400)):
            if '"usage"' not in line:
                continue
            try:
                rec = json.loads(line)
                u = (rec.get("message") or {}).get("usage") or {}
            except Exception:
                continue
            if not u:
                continue
            ins = [num(u.get(k)) for k in
                   ("input_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens")]
            last_in = sum(x for x in ins if x)
            last_out = num(u.get("output_tokens"))
            break
        if last_in is not None:
            return {"engine": "claude", "last_in": last_in,
                    "last_out": last_out,
                    "total_in": None, "total_out": None}
    return {}


# ── 隐藏 / 回收站 ─────────────────────────────────────────────
# 用户不想在列表里看到的会话记进 hidden_sessions.json（只藏不删，
# 原始文件一个字节都不动 —— 这是「桥不改写客户端数据」红线的延伸）。
# 彻底删除由 web_server 负责：把文件移进 trash/ 保留 30 天再清。
HIDDEN_PATH = os.path.join(HERE, "hidden_sessions.json")


def _hidden_sids():
    """被隐藏的 session_id 集合。文件坏了当空集 —— 宁可多显示不能全消失。"""
    try:
        d = json.loads(io.open(HIDDEN_PATH, encoding="utf-8-sig").read())
        return set((d.get("hidden") or {}).keys())
    except Exception:
        return set()


def find_session_files(sid):
    """定位某个会话在磁盘上的全部文件（续接分叉可能一份多文件）。"""
    # Zcode 多个会话共用一个数据库，绝不能把整库当作单会话移走。
    if isinstance(sid, str) and sid.startswith("sess_"):
        return []
    # Antigravity 每会话一个库，但 v1 定位只读，回收站入口已在前端禁用，
    # 这里再兜一层：uuid 先查摘要库，是它的会话就不给删除出口。
    if isinstance(sid, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", sid or ""):
        try:
            import antigravity_sessions
            if antigravity_sessions.is_managed(sid):
                return []
        except Exception:
            return []
    out = set(glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "%s.jsonl" % sid)))
    out |= set(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*",
                                      "rollout-*%s.jsonl" % sid)))
    for src in EXTRA_SOURCES:
        out |= set(glob.glob(os.path.join(src["dir"], "**", "*%s.jsonl" % sid),
                             recursive=True))
    return sorted(out)


def time_ago(ts):
    """时间戳说成人话。"""
    if not ts:
        return "没记录"
    d = int(time.time() - ts)
    if d < 60:
        return "%d 秒前" % max(d, 0)
    if d < 3600:
        return "%d 分钟前" % (d // 60)
    if d < 86400:
        return "%d 小时前" % (d // 3600)
    return "%d 天前" % (d // 86400)


STATUS_CN = {
    "running": "正在跑",
    "done": "已完成",
    "stalled": "没了下文",
    "aborted": "已手动中止",
    "error": "出错",
    "idle": "闲着",
}

ENGINE_CN = {"claude": "Claude", "codex": "Codex", "pi": "Pi"}


def format_list(sessions, limit=12, locked=None):
    """把会话列表排成飞书能发的文本。

    locked 是「正在被占用的 session_id 集合」，标 ⏳ 提示别同时发。
    """
    if not sessions:
        return ("最近 12 小时没有活动的会话。\n"
                "想看更早的，发「/列表 全部」。")

    locked = locked or set()
    dot = {"active": "🟢", "recent": "🟡", "stale": "⚪"}
    rows = ["当前会话（%d 个）：" % len(sessions), ""]
    for i, s in enumerate(sessions[:limit], 1):
        busy = " ⏳正被占用" if s["session_id"] in locked else ""
        # 显示客户端名，让你一眼知道是哪个软件的哪个窗口
        client = s.get("client") or ENGINE_CN.get(s["engine"], s["engine"])
        rows.append("%d. %s %s ［%s］%s" % (
            i, dot.get(s["level"], "⚪"), s["title"], client, busy))
        rows.append("   %s · %s · %s" % (
            STATUS_CN.get(s["status"], s["status"]),
            time_ago(s["last_time"]), s["session_id"][:8]))
        if s["last_msg"]:
            rows.append("   %s" % s["last_msg"][:60])
        rows.append("")

    if len(sessions) > limit:
        rows.append("（还有 %d 个没列出来）" % (len(sessions) - limit))
        rows.append("")
    rows.append("给某个发消息：<序号> <你要说的>")
    rows.append("例如：1 接着把测试补上")
    return "\n".join(rows)


_CACHE = {}            # level -> {"ts": 秒, "data": 结果}
_CACHE_TTL = 4.0       # 秒
_REVALIDATING = set()  # 正在后台重算的 level，防重复起线程
# 为什么按 level 分槽存：原来只存一份，「最近」和「全部」来回切会互相顶掉，
# 每次切档都得重新全盘扫一遍 —— 手机网页上切个筛选要等好几秒就是这来的。

def scan_all_cached(level=None):
    """带缓存 + stale-while-revalidate 的 scan_all。

    命中未过期：直接返回。
    过期但有旧数据：**立即返回旧数据**，同时后台线程重算下一轮替换 ——
      手机刷新页面不再吃 2.5 秒冷扫（用户反馈过「刷新出来慢」，这就是解）。
    冷启动无旧数据：只能同步扫这一次。
    """
    import threading
    key = level or "all"
    now = time.time()
    hit = _CACHE.get(key)
    if hit:
        if now - hit["ts"] < _CACHE_TTL:
            return hit["data"]
        if key not in _REVALIDATING:
            _REVALIDATING.add(key)

            def _recompute(k=key):
                try:
                    _CACHE[k] = {"ts": time.time(), "data": scan_all(k)}
                except Exception:
                    pass
                finally:
                    _REVALIDATING.discard(k)
            threading.Thread(target=_recompute, daemon=True).start()
        return hit["data"]

    result = scan_all(level)
    # 只留最近几个档位，防字典无限长
    if len(_CACHE) > 5:
        oldest = min(_CACHE, key=lambda k: _CACHE[k]["ts"])
        _CACHE.pop(oldest, None)
    _CACHE[key] = {"ts": now, "data": result}
    return result


# ── 解析器健康自检 ─────────────────────────────────────────────
# 背景：2026-08-23 Codex 改了 rollout 记录格式（event_msg/user_message 消失，
# 改用 response_item/message），所有新会话的详情页一夜变空白，是用户先发现的。
# 「上游改格式」没法全自动修 —— 没见过的格式写不出对应解析。能做的是：
#   1. 解析层写宽容（认「长得像消息的记录」，不死抠外层类型名）—— 上面的代码
#   2. 自检兜底：最近的会话文件里一条可识别的消息都没有 = 大概率又改格式了，
#      让网页列表顶部挂横幅报警，别等用户点开才发现空白。

def _known_codex_msg(p):
    """这条 payload 是不是认识的消息记录。两种格式各算数。"""
    if not isinstance(p, dict):
        return False
    if p.get("type") in ("user_message", "agent_message"):
        return True
    return p.get("type") == "message" and p.get("role") in ("user", "assistant")


def _known_claude_msg(rec):
    m = rec.get("message")
    return isinstance(m, dict) and m.get("role") in ("user", "assistant")


_HEALTH_CACHE = {"ts": 0.0, "bad": None}
_HEALTH_TTL = 600      # 10 分钟查一次，别每次刷列表都全文件扫
_HEALTH_BUSY = {"on": False}


def _health_scan(max_age, now):
    """真正的扫描体。只被后台线程调用 —— 全文件逐行读，最坏要几秒，
    放在请求路径里会把那次列表轮询卡死（实测教训）。"""
    def _check(paths, engine):
        out = []
        for fpath in paths:
            try:
                if now - os.path.getmtime(fpath) > max_age:
                    continue
                if os.path.getsize(fpath) < 20 * 1024:
                    continue
            except Exception:
                continue
            n_lines, known = 0, False
            try:
                with io.open(fpath, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        n_lines += 1
                        if n_lines > 20000:     # 防御超大文件把自检拖死
                            break
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        ok = (_known_claude_msg(rec) if engine == "claude"
                              else _known_codex_msg(rec.get("payload")))
                        if ok:
                            known = True
                            break
            except Exception:
                continue
            if known or n_lines <= 8:
                continue
            out.append({"engine": engine,
                        "session_id": os.path.basename(fpath)[:80],
                        "path": fpath})
        return out

    return (_check(glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")), "claude")
            + _check(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*",
                                            "rollout-*.jsonl")), "codex"))


def parser_health(max_age=86400):
    """检查最近 max_age 秒内动过的会话文件，找出整份读不出消息的。

    返回异常清单 [{"engine","session_id","path"}]，空列表 = 一切正常。
    判据刻意收窄到「几乎不可能是正常情况」：文件够大（>20KB）、行数够多
    （>8 行）却没有一条认识的消息记录 —— 开了没说话的死会话不会同时
    满足这两条，不会误报。

    缓存过期时**不在调用方线程里扫**：立刻返回上一次的结果（首查给空），
    起后台线程慢慢算完再换上来。扫描是秒级的，同步跑会卡住那一次网页轮询。
    """
    import threading
    now = time.time()
    cached = _HEALTH_CACHE["bad"]
    if cached is not None and now - _HEALTH_CACHE["ts"] < _HEALTH_TTL:
        return cached

    if not _HEALTH_BUSY["on"]:
        _HEALTH_BUSY["on"] = True

        def _run():
            try:
                bad = _health_scan(max_age, time.time())
                _HEALTH_CACHE.update({"ts": time.time(), "bad": bad})
            except Exception:
                pass
            finally:
                _HEALTH_BUSY["on"] = False
        threading.Thread(target=_run, daemon=True).start()

    if cached is None:
        # 首查还没有任何结果：先当健康返回，真有问题横幅最多晚 10 分钟
        _HEALTH_CACHE.update({"ts": now, "bad": []})
        return []
    return cached


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    lvl = sys.argv[1] if len(sys.argv) > 1 else "recent"
    t0 = time.time()
    ss = scan_all(None if lvl == "all" else lvl)
    took = time.time() - t0
    print(format_list(ss))
    print()
    print("（扫描耗时 %.2f 秒，档位过滤=%s）" % (took, lvl))
