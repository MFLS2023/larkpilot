# -*- coding: utf-8 -*-
"""飞书 ←→ Claude Code 双向桥。

你在飞书群里发一句话，这个程序把它交给 claude -p 干活，干完把结果发回群里。
在外面用手机就能指挥电脑干活。

怎么连上的：走飞书官方的「长连接模式」——
这个程序主动往飞书服务器拨一条 WebSocket，飞书顺着这条线把你的消息推回来。
不需要公网 IP，不需要在路由器上开端口，别人从外面摸不到你的机器。

跑起来：
    python bridge.py            正常运行
    python bridge.py --check    只检查配置和环境，不连飞书

配置在同目录 bridge_config.json。
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime

# ---- 飞书永远直连，不走代理。必须在 import lark_oapi 之前设 ----
# 起因：bridge_stdout.log 里 2026-08-09 21:19 那次，代理（127.0.0.1:7897）
# 关掉之后长连接断开，之后每次重连还是往那个死代理打，全部 ProxyError。
# 表现是进程活着但一条消息都收不到 —— 比直接崩更难发现。
# 飞书是国内服务，本来就没有走代理的理由；这里只放行飞书域名，
# 别的站（比如 api.anthropic.com）该走代理还走代理。
_NO_PROXY_HOSTS = "open.feishu.cn,.feishu.cn,open.larksuite.com,.larksuite.com"
for _k in ("NO_PROXY", "no_proxy"):
    _old = os.environ.get(_k) or ""
    _need = [h for h in _NO_PROXY_HOSTS.split(",") if h not in _old]
    if _need:
        os.environ[_k] = ",".join([x for x in ([_old] if _old else []) + _need])

# 窗口扫描器
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import session_scanner
import session_lock as SL
import config as CFG      # 目录白名单、密钥脱敏
import auth as AUTH       # 身份白名单、绑定码、安全日志

# Windows 控制台默认 GBK，中文会炸，这里强制 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "bridge_config.json")
ENV_PATH = os.path.join(HERE, ".env")
STATE_PATH = os.path.join(HERE, "bridge_state.json")
LOG_PATH = os.path.join(HERE, "bridge.log")
HOME = os.path.expanduser("~")
SETTINGS_PATH = os.path.join(HOME, ".claude", "settings.json")

# 权限档位。飞书那头能干什么由这里写死，不靠 Claude 自觉。
# `auto` 交给 Claude Code 的自动权限分类器判断；`full` 才是完全跳过权限检查。
# 但命令行的 --tools 白名单能压住它，给 Read 就真的只能读。
PERMISSIONS = {
    # 只读：能看代码、能查，动不了任何文件
    "read": ["Read", "Grep", "Glob", "WebSearch", "WebFetch"],
    # 干活：能改文件，但不能跑命令
    "write": ["Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch"],
    # 全开：命令行工具全给，MCP 也全给（full 档单独处理，不走这张表）
    "full": None,
    # 自动：工具和 MCP 全给，由 Claude Code auto mode 决定是否需要确认
    "auto": None,
    # 沙箱：一个工具都不给，纯聊天
    "chat": [],
}

# 危险操作关键词。命中就先问一句，不直接执行。
# 这些是"删了就回不来"或"影响别人"的操作，在手机上看不见细节，值得多问一句。
DANGER_PATTERNS = [
    (r"rm\s+-rf|rmdir\s+/s|del\s+/[sq]|Remove-Item.*-Recurse", "递归删除文件"),
    (r"\bformat\b|diskpart|mkfs", "格式化磁盘"),
    (r"git\s+push.*(-f|--force)", "强制推送 git"),
    (r"git\s+reset\s+--hard|git\s+clean\s+-[fdx]+|git\s+branch\s+-D", "破坏性 git 操作"),
    (r"DROP\s+(TABLE|DATABASE)|TRUNCATE\s+TABLE|DELETE\s+FROM(?!.*WHERE)", "删库删表"),
    (r"shutdown|restart-computer|reg\s+delete", "关机重启或改注册表"),
    (r"部署到?生产|deploy.*prod|publish.*release", "部署生产环境"),
    (r"npm\s+publish|pypi.*upload|twine\s+upload", "发布到公共仓库"),
]

# 触发词。在飞书里回这些可以临时切档或做别的操作。
CMD_HELP = ("/帮助", "/help", "帮助")
CMD_NEW = ("/新会话", "/new", "新会话")
CMD_STATUS = ("/状态", "/status", "状态")
CMD_LIST = ("/列表", "/list", "列表")
CMD_YES = ("确认", "/确认", "yes", "y", "确定")
CMD_NO = ("取消", "/取消", "no", "n", "算了")


def log(msg):
    """同时写屏幕和日志文件。程序在后台跑的时候只有日志文件看得见。"""
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def load_config():
    """读配置。每次读盘不缓存，改了配置下一条消息就生效。

    实现在 config.py，这里只是转一下并把读盘错误写进日志。
    以前这个函数自己解析 .env、自己拼默认值，跟 config.py 那份是两套 ——
    结果 allowed_dirs 的默认值只有 config.py 有，桥这边全靠
    bridge_config.json 里恰好写着才没出事。
    """
    return CFG.load_config(on_error=lambda e: log("配置读不了（%s），用默认值" % e))


def load_state():
    """读状态文件。

    结构长这样：
      {
        "current": "改网站",              当前在哪条线上
        "lines": {
          "改网站": {
             "engine": "claude",          这条线用 claude 还是 codex
             "session_id": "abc-123",     续接用的会话号
             "last": "把首页导航改一下",   最后说的一句（/列表 时显示）
             "when": 1754640000,          最后一次活动时间
             "busy": false                正在跑活没跑完
          },
          "查数据": {...}
        },
        "pending": {...}                  等你回「确认」的危险操作
      }

    多条线各存自己的 session_id，所以两个任务不会串。
    """
    try:
        st = json.loads(io.open(STATE_PATH, encoding="utf-8-sig").read())
    except Exception:
        st = {}
    if not isinstance(st.get("lines"), dict):
        st["lines"] = {}
    # 兼容老格式：以前只有一个顶层 session_id，搬到「默认」这条线上
    old_sid = st.pop("session_id", None)
    if old_sid and not st["lines"]:
        st["lines"]["默认"] = {"engine": "claude", "session_id": old_sid,
                              "last": "", "when": int(time.time()), "busy": False}
        st["current"] = "默认"
    return st


def save_state(st):
    try:
        with io.open(STATE_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(st, ensure_ascii=False, indent=2))
    except Exception as e:
        log("状态存不下（%s）" % e)


# 改状态时要上锁。多条消息可能同时进来，两个线程一起读改写会互相覆盖。
STATE_LOCK = threading.RLock()


def cur_line(st):
    """取当前这条线的名字。没有就叫「默认」。"""
    name = st.get("current")
    if name and name in st["lines"]:
        return name
    if st["lines"]:
        # 当前指向的线没了，挑最近活动的那条
        name = max(st["lines"], key=lambda k: st["lines"][k].get("when") or 0)
        st["current"] = name
        return name
    st["lines"]["默认"] = {"engine": "claude", "session_id": None,
                          "last": "", "when": int(time.time()), "busy": False}
    st["current"] = "默认"
    return "默认"


def touch_line(name, **kw):
    """更新某条线的字段，顺手记时间。带锁，防止并发覆盖。"""
    with STATE_LOCK:
        st = load_state()
        ln = st["lines"].setdefault(name, {"engine": "claude", "session_id": None,
                                           "last": "", "when": 0, "busy": False})
        ln.update(kw)
        ln["when"] = int(time.time())
        save_state(st)
        return st


def claude_env():
    """给 claude -p 子进程准备环境变量。

    坑在这：API key 存在 settings.json 的 env 段里，claude 客户端自己会读，
    但 -p 起的子进程读不到，不注入就报 "Not logged in · Please run /login"。
    所以这里把 settings.json 的 env 全捞出来塞进子进程。
    ${...} 形式的是占位符不是真值，跳过。
    """
    env = dict(os.environ)
    try:
        s = json.loads(io.open(SETTINGS_PATH, encoding="utf-8-sig").read())
        for k, v in (s.get("env") or {}).items():
            if isinstance(v, str) and not v.startswith("${"):
                env[k] = v
    except Exception as e:
        log("读 settings.json 失败（%s），子进程可能没登录凭据" % e)
    return env


def claude_bin():
    """找 claude 的可执行入口。

    Windows 上 npm 装出来三个文件：无后缀的（bash 脚本）、.cmd、.ps1。
    Python 的 subprocess 走 CreateProcess，认不了 bash 脚本，必须用 .cmd，
    否则报 WinError 2 系统找不到指定的文件。
    """
    for p in (os.path.join(HOME, "AppData", "Roaming", "npm", "claude.cmd"),
              os.path.join(HOME, "AppData", "Roaming", "npm", "claude.exe")):
        if os.path.exists(p):
            return p
    return "claude.cmd"  # 兜底：靠 PATH 找


def find_danger(text):
    """扫一遍有没有危险操作。返回命中的说明，没有返回 None。"""
    for pat, desc in DANGER_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return desc
    return None


def run_claude(prompt, cfg, session_id=None, on_progress=None):
    """把提示词交给 claude -p，返回 (回复文本, 新的session_id, 出错了没)。

    on_progress 传了就走流式：--output-format stream-json 增量吐事件，
    每凑出一段助手文本就回调一次 on_progress(目前累计文本)，网页轮询
    拿去显示「跑到哪了」。不传则走原来的一次性 JSON 路径（测试依赖它）。

    提示词必须走 stdin，不能当命令行参数——因为 --tools 是可变参数
    （--tools <tools...>），写在它后面的提示词会被当成工具名一起吞掉，
    报 "Input must be provided either through stdin or as a prompt argument"。
    """
    perm = str(cfg.get("permission") or "read").lower()
    streaming = callable(on_progress)
    cmd = [claude_bin(), "-p",
           "--output-format", "stream-json" if streaming else "json"]
    if streaming:
        # stream-json 在 -p 模式下官方要求搭配 --verbose 才给全事件
        cmd.append("--verbose")

    if perm in ("full", "auto"):
        # 两档都开放工具和 MCP；区别由 Claude Code 的权限模式决定。
        mode = "bypassPermissions" if perm == "full" else "auto"
        cmd += ["--permission-mode", mode]
    else:
        tools = PERMISSIONS.get(perm)
        if tools is None:
            tools = PERMISSIONS["read"]
        # --tools "" 关不掉 MCP 工具（实测：只剩 context7 和 deepwiki 还在），
        # 要连 MCP 一起关必须配上 --strict-mcp-config + 一个空的 mcp 配置文件
        empty_mcp = os.path.join(HERE, "_empty_mcp.json")
        if not os.path.exists(empty_mcp):
            io.open(empty_mcp, "w", encoding="utf-8").write('{"mcpServers": {}}\n')
        cmd += ["--tools", ",".join(tools) if tools else "",
                "--mcp-config", empty_mcp, "--strict-mcp-config"]

    if session_id:
        cmd += ["-r", session_id]

    # 工作目录必须过白名单。以前是「目录不存在就退回 os.getcwd()」——
    # 那等于配置里写什么目录都进得去，包括 .ssh 和 AppData。
    workdir = CFG.safe_cwd(cfg)
    if not workdir:
        return ("工作目录不在白名单里，兜底目录也没有。"
                "检查 bridge_config.json 的 allowed_dirs。", session_id, True)

    log("调 claude（档位=%s，续接=%s，流式=%s）"
        % (perm, (session_id or "无")[:8], streaming))
    t0 = time.time()

    # ── 流式路径：Popen 逐行读事件 ──────────────────────────────
    if streaming:
        timeout_s = int(cfg.get("timeout_seconds") or 1800)
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,   # 不读 stderr；留着会塞满管道把进程卡死
                env=claude_env(), cwd=workdir)
        except Exception as e:
            return ("起不来 claude：%s" % e, session_id, True)

        killed = {"t": False}

        def _watchdog():
            time.sleep(timeout_s)
            killed["t"] = True
            try:
                proc.kill()
            except Exception:
                pass
        watchdog = threading.Timer(timeout_s, _watchdog)
        watchdog.start()

        acc, new_sid, is_err, final_text = [], session_id, False, None
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
        except Exception as e:
            watchdog.cancel()
            return ("写不进 claude 的 stdin：%s" % e, session_id, True)

        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            et = ev.get("type")
            if et == "assistant":
                for blk in ((ev.get("message") or {}).get("content") or []):
                    if (isinstance(blk, dict) and blk.get("type") == "text"
                            and blk.get("text")):
                        acc.append(blk["text"])
                        try:
                            on_progress("".join(acc))
                        except Exception:
                            pass     # 回调炸了不能拖死任务
            elif et == "system" and ev.get("subtype") == "init":
                new_sid = ev.get("session_id") or new_sid
            elif et == "result":
                final_text = ev.get("result")
                new_sid = ev.get("session_id") or new_sid
                is_err = bool(ev.get("is_error"))
        watchdog.cancel()
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        rc = proc.returncode

        # 失败必须报成失败 —— 流式路径以前会把「进程根本没起来」伪装成
        # 「（空回复）且没出错」，网页上看到一条空回复还以为它答完了
        if killed["t"]:
            partial = "".join(acc)[:300]
            extra = "，已生成的部分开头：%s…" % partial if partial else ""
            return ("超时了（%d 秒还没干完）%s。任务可能太大，拆小一点再试。"
                    % (timeout_s, extra), session_id, True)
        if final_text is None and not acc:
            if rc not in (0, None):
                return ("claude 进程异常退出（code=%s），多半是启动失败。"
                        "看 bridge.log 最后几行找原因。" % rc, session_id, True)
            return ("claude 什么都没回（流式下零事件）。", session_id, True)
        text = final_text or "".join(acc)
        if rc not in (0, None):
            # 有部分产出但进程中途挂了：内容给你，但标成出错
            text += "\n\n[进程中途退出 code=%s，以上可能不完整]" % rc
            log("claude 中途退出 code=%s（流式）" % rc)
            return (text, new_sid, True)
        log("claude 回来了（%d 秒，出错=%s，流式）"
            % (int(time.time() - t0), is_err))
        return (text, new_sid, is_err)

    # ── 原一次性路径（飞书和测试都走这里）──────────────────────
    try:
        r = subprocess.run(cmd, input=prompt.encode("utf-8"),
                           capture_output=True, env=claude_env(),
                           cwd=workdir, timeout=int(cfg.get("timeout_seconds") or 1800))
    except subprocess.TimeoutExpired:
        return ("超时了（%d 秒还没干完）。任务可能太大，拆小一点再试。"
                % int(cfg.get("timeout_seconds") or 1800), session_id, True)
    except Exception as e:
        return ("起不来 claude：%s" % e, session_id, True)

    took = int(time.time() - t0)
    out = r.stdout.decode("utf-8", "replace").strip()
    err = r.stderr.decode("utf-8", "replace").strip()

    if not out:
        return ("claude 什么都没回。stderr：%s" % (err[:500] or "（空）"), session_id, True)

    try:
        j = json.loads(out)
    except Exception:
        return ("claude 回的不是 JSON：%s" % out[:500], session_id, True)

    text = j.get("result") or "（空回复）"
    new_sid = j.get("session_id") or session_id
    bad = bool(j.get("is_error"))
    log("claude 回来了（%d 秒，出错=%s）" % (took, bad))
    return (text, new_sid, bad)


def pi_bin():
    """pi 也是 npm 装的，同样得用 .cmd（原因见 claude_bin 的注释）。"""
    for p in (os.path.join(HOME, "AppData", "Roaming", "npm", "pi.cmd"),
              os.path.join(HOME, "AppData", "Roaming", "npm", "pi.exe")):
        if os.path.exists(p):
            return p
    return "pi.cmd"


def _pi_session_file(session_id):
    """把 session id 定位成会话文件的完整路径。找不到返回 None。

    实测（2026-08-25）三种续接方式里只有「直接给文件路径」可靠：
      --session <id>      跨项目目录会交互式问 fork，headless 卡死
      --fork <id>         -p 下不发起模型请求（usage 全 0），返回空回复
      --session <path>    ✅ 正常执行且后续上下文连续
    """
    import glob as _g
    hits = _g.glob(os.path.join(HOME, ".pi", "agent", "sessions",
                                "**", "*%s.jsonl" % session_id),
                   recursive=True)
    return max(hits, key=os.path.getmtime) if hits else None


def run_pi(prompt, cfg, session_id=None, on_progress=None):
    """把提示词交给 pi -p，返回 (回复文本, session_id, 出错了没)。

    pi 的 headless 接口（2026-08-25 实测）：-p 非交互、--mode json 输出
    JSONL 事件流、--session <id> 按 UUID（可以是部分）精确续接。
    权限映射：chat→禁所有工具；read→只留 read 工具；write/full 全开
    （pi 没有 claude 那种 bypass/auto 模式，默认即全工具）。
    """
    perm = str(cfg.get("permission") or "read").lower()
    cmd = [pi_bin(), "-p", "--mode", "json"]
    if perm == "chat":
        cmd.append("--no-tools")
    elif perm == "read":
        cmd += ["--tools", "read"]      # write/bash/edit 都不给

    workdir = CFG.safe_cwd(cfg)
    if not workdir:
        return ("工作目录不在白名单里，兜底目录也没有。"
                "检查 bridge_config.json 的 allowed_dirs。", session_id, True)

    if session_id:
        sess_file = _pi_session_file(session_id)
        if not sess_file:
            return ("找不到这个 pi 会话的存储文件（%s）。" % session_id[:8],
                    session_id, True)
        # 必须给文件路径 —— 给 id 会触发跨项目确认或空回复（见 _pi_session_file）
        cmd += ["--session", sess_file]

    log("调 pi（档位=%s，续接=%s）" % (perm, (session_id or "无")[:8]))
    t0 = time.time()
    try:
        r = subprocess.run(cmd, input=prompt.encode("utf-8"),
                           capture_output=True,
                           cwd=workdir,
                           timeout=int(cfg.get("timeout_seconds") or 1800))
    except subprocess.TimeoutExpired:
        return ("超时了（%d 秒还没干完）。任务可能太大，拆小一点再试。"
                % int(cfg.get("timeout_seconds") or 1800), session_id, True)
    except Exception as e:
        return ("起不来 pi：%s" % e, session_id, True)

    took = int(time.time() - t0)
    out = r.stdout.decode("utf-8", "replace").strip()
    if not out:
        err = r.stderr.decode("utf-8", "replace").strip()
        return ("pi 什么都没回。stderr：%s" % (err[:400] or "（空）"),
                session_id, True)

    # 事件流解析：session 行给 id；assistant 的 message_end 是最终文本；
    # text_delta 天然是增量 —— 有回调就顺手喂给它（和 claude 流式同款体验）
    new_sid, final_text, acc = session_id, None, []
    stop_reason, prov, model = None, None, None
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        et = ev.get("type")
        if et == "session":
            new_sid = ev.get("id") or new_sid
        elif et == "message_end":
            m = ev.get("message") or {}
            if m.get("role") != "assistant":
                continue
            # 记下最后一条助手消息的元信息 —— 供应商报错时要拿来说人话
            stop_reason = m.get("stopReason")
            prov, model = m.get("provider"), m.get("model")
            parts = [b.get("text") for b in (m.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            if any(parts):
                final_text = "".join(p for p in parts if p)
        elif et == "message_update" and callable(on_progress):
            ae = ev.get("assistantMessageEvent") or {}
            if ae.get("type") == "text_delta" and ae.get("delta"):
                acc.append(ae["delta"])
                try:
                    on_progress("".join(acc))
                except Exception:
                    pass

    if final_text is None and acc:
        final_text = "".join(acc)
    if final_text is None:
        if stop_reason == "error":
            # 实测（2026-08-25）：中转供应商故障时 pi 拿到 stop=error 的空消息，
            # usage 全 0 —— 不是格式问题，别冤枉解析器
            return ("pi 的模型供应商返回错误（%s / %s）。多半是中转站抽风，"
                    "换个供应商或稍后再试。" % (prov or "?", model or "?"),
                    session_id, True)
        return ("pi 回了但没找到可用的回复文本（stop=%s）。"
                "开头：%s" % (stop_reason or "?", out[:300]), session_id, True)
    log("pi 回来了（%d 秒）" % took)
    return (final_text, new_sid, False)


RUNNERS = {}                  # 引擎号 -> runner；supported_engines() 按它放行
RUNNERS["pi"] = run_pi        # 注册进引擎表（RUNNERS/supported_engines 见后文）


def codex_bin():
    """codex 也是 npm 装的，同样得用 .cmd（原因见 claude_bin 的注释）。"""
    for p in (os.path.join(HOME, "AppData", "Roaming", "npm", "codex.cmd"),
              os.path.join(HOME, "AppData", "Roaming", "npm", "codex.exe")):
        if os.path.exists(p):
            return p
    return "codex.cmd"


# codex 的沙箱档位。跟 claude 的 --tools 白名单不是一回事：
# codex 管的是「能不能写磁盘、能不能联网」，claude 管的是「能用哪几个工具」。
CODEX_SANDBOX = {
    "read":  "read-only",
    "write": "workspace-write",
    "full":  "danger-full-access",
    "auto":  "danger-full-access",
    "chat":  "read-only",
}


def run_codex(prompt, cfg, session_id=None):
    """把提示词交给 codex exec，返回 (回复文本, 新的session_id, 出错了没)。

    实测出来的三件事（codex-cli 0.146.0）：
      1. 输出加 --json 是 JSONL，一行一个事件，不是一整块 JSON；
         最终答案在 {"type":"item.completed","item":{"type":"agent_message","text":...}}
      2. 会话号在第一行 {"type":"thread.started","thread_id":"..."}
      3. `codex exec resume` 不认 -s / -C，只能用 -c sandbox_mode=... 覆盖，
         工作目录靠 subprocess 的 cwd 传
    """
    perm = str(cfg.get("permission") or "read").lower()
    sandbox = CODEX_SANDBOX.get(perm) or "read-only"
    exe = codex_bin()

    if session_id:
        cmd = [exe, "exec", "resume", session_id, "--json", "--skip-git-repo-check",
               "-c", 'sandbox_mode="%s"' % sandbox]
    else:
        cmd = [exe, "exec", "--json", "--skip-git-repo-check", "-s", sandbox]

    # 工作目录必须过白名单。以前是「目录不存在就退回 os.getcwd()」——
    # 那等于配置里写什么目录都进得去，包括 .ssh 和 AppData。
    workdir = CFG.safe_cwd(cfg)
    if not workdir:
        return ("工作目录不在白名单里，兜底目录也没有。"
                "检查 bridge_config.json 的 allowed_dirs。", session_id, True)

    log("调 codex（沙箱=%s，续接=%s）" % (sandbox, (session_id or "无")[:8]))
    t0 = time.time()
    try:
        r = subprocess.run(cmd, input=prompt.encode("utf-8"),
                           capture_output=True, env=claude_env(),
                           cwd=workdir, timeout=int(cfg.get("timeout_seconds") or 1800))
    except subprocess.TimeoutExpired:
        return ("超时了（%d 秒还没干完）。任务可能太大，拆小一点再试。"
                % int(cfg.get("timeout_seconds") or 1800), session_id, True)
    except Exception as e:
        return ("起不来 codex：%s" % e, session_id, True)

    took = int(time.time() - t0)
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace").strip()
    text, new_sid, errors = parse_codex_jsonl(out)

    if not new_sid:
        new_sid = session_id
    if not text:
        # 没拿到答案才把中途的 error 事件当成失败原因，
        # 否则那些 error 多半只是「模型元数据没找到」这类噪音
        why = "；".join(errors[:3]) or err[:500] or "（stdout 和 stderr 都是空的）"
        return ("codex 没给出答案。%s" % why, new_sid, True)

    log("codex 回来了（%d 秒）" % took)
    return (text, new_sid, False)


def parse_codex_jsonl(out):
    """从 codex --json 的 JSONL 里挑出最终回复、会话号、报错。

    单独拆成函数，测试里可以不真的起 codex 就验证解析对不对。
    """
    text_parts, new_sid, errors = [], None, []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue          # "Reading prompt from stdin..." 这类杂音
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "thread.started":
            new_sid = ev.get("thread_id") or new_sid
        elif ev.get("type") == "item.completed":
            it = ev.get("item") or {}
            if it.get("type") == "agent_message":
                if it.get("text"):
                    text_parts.append(it["text"])
            elif it.get("type") == "error":
                if it.get("message"):
                    errors.append(it["message"])
        elif ev.get("type") == "error":
            if ev.get("message"):
                errors.append(ev["message"])
    return ("\n\n".join(text_parts).strip(), new_sid, errors)


# ── 引擎注册表 ────────────────────────────────────────────────
# 能从手机续接的引擎 = 内置（claude/codex）+ clients.json 里
# can_run=true 且在 RUNNERS 注册了 runner 函数的来源。
# 加新 CLI 工具：照 run_pi 模板写个 runner，然后 RUNNERS["名字"] = runner。


def supported_engines():
    """当前真正能续接的引擎名单（网页放行判断以此为准）。"""
    engs = ["claude", "codex"]
    try:
        sources = session_scanner.EXTRA_SOURCES
    except Exception:
        sources = []
    for src in sources:
        e = str(src.get("engine") or "")
        if src.get("can_run") and e in RUNNERS and e not in engs:
            engs.append(e)
    return tuple(engs)


def run_engine(engine, prompt, cfg, session_id=None, on_progress=None):
    """按引擎名分发。飞书那头只管说话，用哪个引擎由这条线自己记着。

    on_progress 只有支持事件流的 runner 认（claude/pi）；不支持的就忽略，
    任务照跑只是没有增量预览。
    """
    eng = str(engine).lower()
    if eng == "codex":
        return run_codex(prompt, cfg, session_id)
    runner = RUNNERS.get(eng)
    if runner:
        return runner(prompt, cfg, session_id, on_progress=on_progress)
    return run_claude(prompt, cfg, session_id, on_progress=on_progress)


HELP_TEXT = """飞书遥控 Claude / Codex — 能回什么

直接发任务
  「把 notify.py 的超时改成 30 秒」
  正常聊天就行，会记得上下文，可以接着上一句说。

多任务（一条「线」＝一个独立会话，互不干扰）
  /新 改网站        开一条叫「改网站」的线，之后的话都在这条线上
  /新 查数据 codex  开一条线，指定用 codex 干活
  /切 改网站        切回那条线，上下文还在
  /列表             看有哪些线、各自在忙什么、当前在哪条
  /删 改网站        删掉那条线
  只发 /新 不给名字，就是把当前这条线清空重来。

看所有窗口（电脑上正在跑的所有 Claude/Codex 会话）
  /列表             列出所有窗口，包括电脑上其它客户端的
  <序号> <消息>     发给指定窗口，例如：
                    1 继续刚才的任务
                    3 停下来，换个思路

换引擎
  /codex 你的任务   这条线以后交给 codex
  /claude 你的任务  换回 claude

其它命令
  /状态   看当前档位、线、工作目录
  /帮助   这段话

临时切档（只影响这一条消息）
  /只读 你的任务    只能看不能改
  /聊天 你的问题    不给任何工具，纯问答

碰到危险操作
  会先问你一句，回「确认」才做，回「取消」就算了。
"""


# 飞书文本消息的硬上限是 150 KB（官方文档明写，超了报 code 230025）。
# 留出余量：JSON 转义会让实际请求体变长（换行符 \n 转义成 \\n 就多一个字节），
# 前缀「［线名］第 2/3 段」也要占位置。
FEISHU_TEXT_LIMIT_BYTES = 150 * 1024
SAFE_CHUNK_BYTES = 100 * 1024      # 单条实际发这么多，约 3 万汉字


def split_for_feishu(body, chunk_bytes=None):
    """把长回复切成几段，每段都在飞书单条上限内。返回段落列表。

    以前是直接截断，长回复后面全丢了 —— 那是我图省事，不是飞书的限制：
    飞书单条能装 150 KB（约 5 万汉字），我却砍在 3000 字。

    切的时候尽量在段落边界断开，其次在换行处，都不行才硬切。
    按字节算不按字数算，因为一个汉字占 3 字节，上限是字节数。
    """
    limit = int(chunk_bytes or SAFE_CHUNK_BYTES)
    if len(body.encode("utf-8")) <= limit:
        return [body]

    chunks, buf = [], ""

    def flush():
        if buf:
            chunks.append(buf)

    # 先按空行切成块，逐块往缓冲里塞，塞不下就先发出去
    for para in body.split("\n\n"):
        piece = (buf + "\n\n" + para) if buf else para
        if len(piece.encode("utf-8")) <= limit:
            buf = piece
            continue
        flush()
        buf = ""
        # 单个段落本身就超长 —— 按行切
        if len(para.encode("utf-8")) > limit:
            for line in para.split("\n"):
                piece2 = (buf + "\n" + line) if buf else line
                if len(piece2.encode("utf-8")) <= limit:
                    buf = piece2
                    continue
                flush()
                buf = ""
                # 单行还超长（比如一大块没换行的日志）—— 硬切
                if len(line.encode("utf-8")) > limit:
                    # ★ 按字符切不按字节切。按字节切会在汉字中间断开，
                    #   decode(errors="ignore") 就把那半个字丢了（实测 6 万字丢 1 个）
                    cur = ""
                    for ch in line:
                        if len((cur + ch).encode("utf-8")) > limit:
                            chunks.append(cur)
                            cur = ch
                        else:
                            cur += ch
                    if cur:
                        chunks.append(cur)
                else:
                    buf = line
        else:
            buf = para
    flush()
    return [c for c in chunks if c.strip()]


def build_reply(text, cfg, prefix=""):
    """准备要发的内容。返回段落列表 —— 长回复分几条发，不再截断。

    ★ 注意 reply_max_chars 只在 truncate_reply=true 时才管事。
      分条长度一律按飞书自己的上限来（约 3 万汉字一条），不拿这个值算 ——
      拿它算过一版，3000 字被换成 9000 字节当分片长度，
      结果 12 万字的回复切成了 43 条，纯属自己把自己限制死了。
    """
    body = (prefix + text) if prefix else text
    if cfg.get("truncate_reply"):
        # 老行为：直接砍掉。留着以防你哪天嫌消息太多条
        limit = int(cfg.get("reply_max_chars") or 3000)
        if len(body) > limit:
            body = body[:limit] + "\n\n……（太长截断了，完整内容在电脑上看）"
        return [body]

    parts = split_for_feishu(body)
    if len(parts) == 1:
        return parts
    # 多段就标上「第几段/共几段」，手机上才知道还有没有下文
    n = len(parts)
    return ["%s\n\n〔第 %d/%d 段〕" % (p, i + 1, n) if i < n - 1
            else "%s\n\n〔完，共 %d 段〕" % (p, n)
            for i, p in enumerate(parts)]


def send_reply(client, chat_id, parts):
    """把 build_reply 切好的几段依次发出去。返回成功发了几段。"""
    ok = 0
    for i, p in enumerate(parts):
        if i:
            time.sleep(0.4)   # 连发之间隔一下，避免触发飞书频控
        if send_msg(client, chat_id, p):
            ok += 1
    return ok


class SendResult(object):
    """发消息的结果。带飞书返回的真实 code，不只是成功失败。

    为什么不直接返回 bool：测试里「调了发送函数」被当成「飞书收到了」，
    机器人根本不在群里（code 230002）测试照样报通过。有了 code
    就能断言 code == 0，假阳性才消得掉。

    仍然可以 `if send_msg(...)` —— __bool__ 挂在 ok 上，老调用点不用改。
    """

    __slots__ = ("ok", "code", "msg", "message_id")

    def __init__(self, ok, code=None, msg="", message_id=""):
        self.ok = bool(ok)
        self.code = code
        self.msg = msg or ""
        self.message_id = message_id or ""

    def __bool__(self):
        return self.ok

    __nonzero__ = __bool__      # Python 2 写法，留着不碍事

    def __repr__(self):
        return "SendResult(ok=%s, code=%s, msg=%r)" % (self.ok, self.code, self.msg[:60])


# 发消息失败后各等几秒再试。只针对网络层异常，不针对飞书回的业务码。
# 次数是这么定的：实测偶发一次 30 秒连接超时，隔几秒重试就好了；
# 真断网的话试三次也没用，早点把失败报出来比干等着强。
SEND_BACKOFF = (1, 3)


def send_msg(client, chat_id, text):
    """往飞书群里发消息。返回 SendResult，只有飞书 code == 0 才算成功。

    常见失败码，排查时对着看：
      230002  机器人不在这个群里（最常见，把机器人拉进群就行）
      230027  缺权限，去开发者后台加对应的权限点
      230025  单条消息超长（正常走 split_for_feishu 不会碰到）
    """
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    req = (CreateMessageRequest.builder()
           .receive_id_type("chat_id")
           .request_body(CreateMessageRequestBody.builder()
                         .receive_id(chat_id)
                         .msg_type("text")
                         .content(json.dumps({"text": text}, ensure_ascii=False))
                         .build())
           .build())

    last = SendResult(False, code=None, msg="没试过")
    for i, wait in enumerate(SEND_BACKOFF + (0,)):
        try:
            resp = client.im.v1.message.create(req)
        except Exception as e:
            # 网络层挂了（代理抽风、连接超时）。这台机器实测会偶发 30 秒连接超时，
            # 不重试的话干完的活白干 —— 结果发不出去就等于没干。
            last = SendResult(False, code=None, msg=str(e))
            log("发消息抛异常（第 %d 次）：%s" % (i + 1, str(e)[:160]))
            if wait:
                time.sleep(wait)
                continue
            return last

        code = getattr(resp, "code", None)
        if not resp.success():
            # 飞书明确回了业务码，说明请求送到了、判定是确定的 —— 重试没意义。
            log("发消息失败 code=%s msg=%s" % (code, resp.msg))
            return SendResult(False, code=code, msg=resp.msg or "")

        mid = ""
        try:
            mid = resp.data.message_id or ""
        except Exception:
            pass
        if i:
            log("发消息第 %d 次成功" % (i + 1))
        return SendResult(True, code=code if code is not None else 0,
                          msg="", message_id=mid)
    return last


def extract_text(content_json, message_type):
    """从飞书事件里把用户发的文字抠出来。

    content 是一个 JSON 字符串，文本消息长这样：{"text":"你好"}
    群里 @机器人 的时候会带上 @_user_1 这种占位符，要去掉。
    """
    if message_type != "text":
        return None
    try:
        d = json.loads(content_json or "{}")
    except Exception:
        return None
    t = str(d.get("text") or "")
    t = re.sub(r"@_user_\d+", "", t)  # 去掉 @机器人 的占位符
    t = re.sub(r"@_all", "", t)
    return t.strip()


def fmt_when(ts):
    """把时间戳说成人话：刚刚 / 5 分钟前 / 2 小时前。"""
    if not ts:
        return "没记录"
    d = int(time.time()) - int(ts)
    if d < 60:
        return "刚刚"
    if d < 3600:
        return "%d 分钟前" % (d // 60)
    if d < 86400:
        return "%d 小时前" % (d // 3600)
    return "%d 天前" % (d // 86400)


def list_lines(st):
    """把所有线列出来。当前那条加个箭头。"""
    if not st["lines"]:
        return "还没有任何任务线。直接发任务，或者用 /新 名字 开一条。"
    cur = cur_line(st)
    rows = ["现在有 %d 条线（→ 是当前所在）：" % len(st["lines"]), ""]
    for name, ln in sorted(st["lines"].items(),
                           key=lambda kv: -(kv[1].get("when") or 0)):
        mark = "→" if name == cur else "  "
        rows.append("%s %s ［%s］%s" % (
            mark, name, ln.get("engine") or "claude",
            "  ⏳ 正在跑" if ln.get("busy") else ""))
        rows.append("     %s ｜ %s" % (
            fmt_when(ln.get("when")),
            (ln.get("last") or "（还没说过话）")[:40]))
    rows += ["", "切过去： /切 名字"]
    return "\n".join(rows)


def parse_line_cmd(text):
    """认出 /新 /切 /删 /列表 这几个命令。

    返回 (命令, 参数)。不是这几个命令就返回 (None, None)。
    中英文都收，因为手机上打中文更顺手但有人习惯敲英文。
    """
    t = text.strip()
    table = [
        (("/列表", "/list", "/线", "/lines"), "list"),
        (("/新", "/new", "/开", "/新会话"), "new"),
        (("/切", "/switch", "/用", "/go"), "switch"),
        (("/删", "/delete", "/del", "/关"), "delete"),
    ]
    for words, act in table:
        for w in words:
            if t == w or t.lower() == w:
                return (act, "")
            if t.startswith(w + " ") or t.startswith(w + "　"):  # 全角空格也认
                return (act, t[len(w):].strip())
    return (None, None)


def split_name_task(arg, existing=None):
    """把 /新 或 /切 后面的东西拆成 (线名, 要干的活, 引擎)。

    为什么要拆：手机上最自然的写法是「/新 帮我查一下光模块的消息」——
    人的意思是「开条新线，然后办这件事」，不是「把这一整句当线的名字」。
    实测踩过两次：
      第一次 整句话变成了线名，活一点没干
      第二次 「/新 改网站 再看一眼那个报错」也整句当了名字
             —— 因为我拿「像不像一句话」去猜，两个词又不长就猜错了

    所以现在不猜句式，按这个顺序判：
      1. 摘掉 claude / codex，那是指定引擎
      2. 第一个词正好是已有的线 → 切过去，剩下的是活
      3. 只剩一个词             → 就是线名，没有活
      4. 多个词                 → 取前一小段当名字，整句当活
    """
    arg = (arg or "").strip()
    if not arg:
        return ("", "", None)
    existing = existing or {}

    parts = arg.split()
    engine = None
    # 先把引擎摘出来。可能在末尾，也可能紧跟在名字后面
    for i, p in enumerate(parts):
        if p.lower() in ("codex", "claude") and i > 0:
            engine = p.lower()
            parts = parts[:i] + parts[i + 1:]
            arg = " ".join(parts)
            break

    if not parts:
        return ("", "", engine)

    PUNCT = "，。？！、；：,?!;:"

    # 第一个词就是已有的线 → 「切到那条线 + 办后面这件事」
    if parts[0] in existing and len(parts) > 1:
        return (parts[0], " ".join(parts[1:]), engine)

    def is_name_like(s):
        """像名字吗：够短、不带标点。词数不作为判据 ——
        中文常常整句不带空格（'请帮我查一下光模块'是一个'词'），
        按词数判会把整句话当成名字，这坑踩过。"""
        return len(s) <= 8 and not any(ch in s for ch in PUNCT)

    # 整串就像个名字（'改网站'、'fixbug'）→ 只建线，没有活
    if is_name_like(arg):
        return (arg, "", engine)

    # 第一个词像名字、后面还有别的 → 那是 '名字 + 一句话'
    if len(parts) > 1 and is_name_like(parts[0]):
        return (parts[0], " ".join(parts[1:]), engine)

    # 剩下的就是「一整句话」：取前一小段当名字，整句当活
    name = arg
    for ch in PUNCT:
        if ch in name:
            name = name.split(ch)[0]
            break
    name = name.strip()[:8] or arg[:8]
    return (name, arg, engine)


def handle_line_cmd(client, chat_id, act, arg, cfg):
    """处理任务线相关命令。返回 True 说明已经处理完了，不用再往下走。

    返回值有第二层含义：返回字符串时，表示「线的事办完了，但还有活要干」，
    调用方要拿这个字符串当任务继续跑。
    """
    with STATE_LOCK:
        st = load_state()

        if act == "list":
            # 先扫所有窗口
            all_sessions = session_scanner.scan_all_cached("recent")
            if all_sessions:
                send_msg(client, chat_id, session_scanner.format_list(all_sessions))
            else:
                send_msg(client, chat_id, list_lines(st))
            return True

        if act == "new":
            if not arg:
                # 不给名字就是把当前这条线清空重来，跟以前的 /新会话 一个意思
                name = cur_line(st)
                st["lines"][name].update({"session_id": None, "last": "",
                                          "when": int(time.time())})
                st.pop("pending", None)
                save_state(st)
                send_msg(client, chat_id,
                         "「%s」这条线清空了，之前聊的都忘了。\n"
                         "想开新的一条留着旧的，用：/新 名字" % name)
                return True
            name, task, engine = split_name_task(arg, st["lines"])
            engine = engine or "claude"
            if name in st["lines"]:
                st["current"] = name
                save_state(st)
                if task:
                    send_msg(client, chat_id,
                             "「%s」已经有了，切过去接着办（上下文还在）。" % name)
                    return task
                send_msg(client, chat_id,
                         "「%s」已经有了，切过去了（没清空，上下文还在）。\n"
                         "真要重开：/删 %s 再 /新 %s" % (name, name, name))
                return True
            st["lines"][name] = {"engine": engine, "session_id": None, "last": "",
                                 "when": int(time.time()), "busy": False}
            st["current"] = name
            # 注意：这里不清 pending。待确认的危险操作自己记着属于哪条线，
            # 你切走看一眼再回来说「确认」，做的还是当初那条线上那件事
            save_state(st)
            if task:
                # /新 后面直接跟了一整句话 —— 建线之后要把这句话当任务办掉，
                # 不能只建个线就完事（实测用户第一次用就是这么发的）
                send_msg(client, chat_id,
                         "开好了：「%s」［%s］，这就去办。" % (name, engine))
                return task
            send_msg(client, chat_id,
                     "开好了：「%s」［%s］\n之后的话都算这条线上的。" % (name, engine))
            return True

        if act == "switch":
            if not arg:
                send_msg(client, chat_id, list_lines(st))
                return True
            # /切 名字 后面还能跟一句话，切过去顺手把活办了
            task = ""
            name = match_line(st, arg)
            if not name:
                # 整串对不上，就把第一个词当名字试试（支持简写）
                head = arg.split()[0] if arg.split() else arg
                name = match_line(st, head)
                if name:
                    task = arg[len(head):].strip()
            if not name:
                send_msg(client, chat_id,
                         "没有叫「%s」的线。\n\n%s" % (arg, list_lines(st)))
                return True
            if task:
                st["current"] = name
                save_state(st)
                send_msg(client, chat_id, "切到「%s」，这就去办。" % name)
                return task
            st["current"] = name
            save_state(st)   # 同上，不清 pending
            ln = st["lines"][name]
            pend = st.get("pending") or {}
            send_msg(client, chat_id, "\n".join([
                "切到「%s」［%s］" % (name, ln.get("engine") or "claude"),
                "  上次活动：%s" % fmt_when(ln.get("when")),
                "  上次说的：%s" % ((ln.get("last") or "（还没说过话）")[:60]),
                "  上下文：%s" % ("接着上次聊" if ln.get("session_id") else "空的，从头开始"),
            ] + (["  ⏳ 这条线还在跑上一个活，等它回来再发新的"] if ln.get("busy") else [])
              + (["", "⚠ 还有一件事等你确认（在「%s」上）：%s"
                  % (pend.get("line") or "?", (pend.get("prompt") or "")[:50]),
                  "  回「确认」就做它，回「取消」作废。"] if pend else [])))
            return True

        if act == "delete":
            if not arg:
                send_msg(client, chat_id, "要删哪条？/删 名字")
                return True
            name = match_line(st, arg)
            if not name:
                send_msg(client, chat_id, "没有叫「%s」的线。" % arg)
                return True
            if st["lines"][name].get("busy"):
                send_msg(client, chat_id,
                         "「%s」还在跑活，等它回来再删。" % name)
                return True
            st["lines"].pop(name, None)
            if st.get("current") == name:
                st.pop("current", None)
            save_state(st)
            send_msg(client, chat_id,
                     "删了「%s」。现在在「%s」。" % (name, cur_line(load_state())))
            return True

    return False


def match_line(st, arg):
    """按名字找线。先精确匹配，找不到再看是不是某条线名字的开头。

    手机上打字容易少打字，「/切 改」能切到「改网站」，但只有唯一一条能对上时才认。
    """
    if arg in st["lines"]:
        return arg
    hits = [n for n in st["lines"] if n.lower().startswith(arg.lower())]
    if len(hits) == 1:
        return hits[0]
    hits = [n for n in st["lines"] if arg.lower() in n.lower()]
    if len(hits) == 1:
        return hits[0]
    return None


def handle_text(client, chat_id, user_id, text, cfg):
    """处理一条用户消息。这是整个桥的大脑。"""
    st = load_state()
    low = text.strip().lower()

    # ---- 序号指令：给列表里某个窗口发消息 ----
    # 格式：<序号> <消息>，例如 "1 继续干" 或 "3 换个思路"
    # /列表 默认显示 recent（🟢+🟡），序号从 1 开始
    m = re.match(r"^(\d+)\s+(.+)$", text.strip(), re.DOTALL)
    if m:
        idx = int(m.group(1))
        msg = m.group(2).strip()
        # 用跟 /列表 同样的过滤条件，限制最多 12 个与列表显示保持一致
        all_sessions = session_scanner.scan_all_cached("recent")[:12]
        if not all_sessions:
            all_sessions = session_scanner.scan_all_cached()[:12]   # 没有 recent 就取全部
        if idx < 1 or idx > len(all_sessions):
            send_msg(client, chat_id,
                     "序号 %d 超出范围了，当前显示 %d 个窗口。\n"
                     "发「/列表」看一眼。" % (idx, len(all_sessions)))
            return
        target = all_sessions[idx - 1]
        eng = target["engine"]
        sid = target["session_id"]
        title = target.get("title") or target.get("project") or sid[:8]

        # 先检查锁
        lock_info = SL.who_holds(sid)
        if lock_info:
            who = lock_info.get("who") or "?"
            send_msg(client, chat_id,
                     "窗口 %d「%s」正被「%s」占着，等它跑完再发。" % (idx, title, who))
            return

        send_msg(client, chat_id,
                 "给窗口 %d「%s」[%s] 发消息，在做了…" % (idx, title, eng))

        def _run_seq(sid=sid, eng=eng, msg=msg, idx=idx, title=title):
            # 挤不进锁表就重试几次 —— 那不是「被占着」，纯粹是机器忙的时候
            # 内部撞车，直接放弃会把任务静默丢掉（你在手机上连发几条正好触发）。
            tok, why = None, None
            for _try in range(3):
                tok, why = SL.acquire_ex(sid, who="飞书(序号%d)" % idx,
                                         ttl=int(cfg.get("timeout_seconds") or 1800),
                                         note=msg[:100])
                if tok or why != SL.GATE:
                    break
                log("窗口 %d 挤不进锁表，第 %d 次重试" % (idx, _try + 1))
                time.sleep(0.5)
            if not tok:
                if why == SL.BUSY:
                    h = SL.who_holds(sid) or {}
                    send_msg(client, chat_id, "窗口 %d 正被「%s」占着，等它做完再发。"
                             % (idx, h.get("who") or "另一个程序"))
                elif why == SL.WRITE:
                    send_msg(client, chat_id, "窗口 %d 锁表写不进去（磁盘满或没权限），任务没发出去。" % idx)
                else:
                    send_msg(client, chat_id, "窗口 %d 锁表一直挤不进去，任务没发出去，再试一次。" % idx)
                return
            try:
                run_cfg = dict(cfg)
                text_out, new_sid, bad = run_engine(eng, msg, run_cfg, sid)
                prefix = "✗ 出错了\n\n" if bad else ""
                parts = build_reply(text_out, cfg, "［%s］%s" % (title, prefix))
                if len(parts) > 1:
                    log("序号指令回复太长，分 %d 条发" % len(parts))
                send_reply(client, chat_id, parts)
            finally:
                SL.release(sid, tok)

        threading.Thread(target=_run_seq, daemon=True).start()
        return

    # ---- 内置命令 ----
    if low in [c.lower() for c in CMD_HELP]:
        send_msg(client, chat_id, HELP_TEXT)
        return

    act, arg = parse_line_cmd(text)
    if act:
        r = handle_line_cmd(client, chat_id, act, arg, cfg)
        if isinstance(r, str) and r:
            # 命令后面还跟着一句话（比如 /新 帮我查一下xxx），
            # 线的事办完了，把这句话当任务接着往下走
            text = r
            st = load_state()
        elif r:
            return

    if low in [c.lower() for c in CMD_STATUS]:
        name = cur_line(st)
        ln = st["lines"][name]
        sid = ln.get("session_id")
        send_msg(client, chat_id, "\n".join([
            "当前状态",
            "  在哪条线：%s（共 %d 条）" % (name, len(st["lines"])),
            "  引擎：%s" % (ln.get("engine") or "claude"),
            "  档位：%s" % cfg.get("permission"),
            "  上下文：%s" % (sid[:8] + "…" if sid else "空的，从头开始"),
            "  目录：%s" % cfg.get("cwd"),
            "  危险操作二次确认：%s" % ("开" if cfg.get("confirm_dangerous") else "关"),
        ] + (["  ⏳ 这条线正在跑活"] if ln.get("busy") else [])))
        return

    # ---- 危险操作的确认回复 ----
    pending = st.get("pending")
    if pending:
        if low in [c.lower() for c in CMD_YES]:
            st.pop("pending", None)
            save_state(st)
            send_msg(client, chat_id, "好，开始做了。")
            # 用当初提问时那条线，不是现在这条 —— 中间可能已经 /切 走了
            do_task(client, chat_id, pending["prompt"], cfg,
                    pending.get("perm"), pending.get("line"))
            return
        if low in [c.lower() for c in CMD_NO]:
            st.pop("pending", None)
            save_state(st)
            send_msg(client, chat_id, "取消了，什么都没动。")
            return
        # 既不是确认也不是取消，当成新任务，把待确认的丢掉
        st.pop("pending", None)
        save_state(st)

    # ---- 换引擎前缀。/codex 前缀会把这条线永久改成 codex ----
    engine_switch = None
    for pre, e in (("/codex", "codex"), ("/claude", "claude")):
        if text.strip().lower().startswith(pre):
            engine_switch = e
            text = text.strip()[len(pre):].strip()
            break
    if engine_switch:
        with STATE_LOCK:
            st = load_state()
            name = cur_line(st)
            old = st["lines"][name].get("engine") or "claude"
            if old != engine_switch:
                # 换引擎必须丢掉 session_id：claude 的会话号 codex 认不了，反之也一样
                st["lines"][name].update({"engine": engine_switch, "session_id": None})
                save_state(st)
                send_msg(client, chat_id,
                         "「%s」这条线换成 %s 了。\n"
                         "（两边的会话号不通用，上下文从头开始）" % (name, engine_switch))
            elif not text:
                send_msg(client, chat_id, "「%s」本来就是 %s。" % (name, engine_switch))
        if not text:
            return

    # ---- 临时切档前缀 ----
    perm_override = None
    for pre, p in (("/只读", "read"), ("/聊天", "chat"), ("/改文件", "write")):
        if text.strip().startswith(pre):
            perm_override = p
            text = text.strip()[len(pre):].strip()
            break
    if perm_override and not text:
        send_msg(client, chat_id, "切档前缀后面得跟上要问的内容。")
        return

    # ---- 危险操作拦一道 ----
    eff_perm = perm_override or str(cfg.get("permission") or "read")
    if cfg.get("confirm_dangerous") and eff_perm in ("full", "write"):
        hit = find_danger(text)
        if hit:
            with STATE_LOCK:
                st = load_state()
                st["pending"] = {"prompt": text, "perm": perm_override,
                                 "line": cur_line(st)}
                save_state(st)
            send_msg(client, chat_id, "\n".join([
                "⚠ 这条像是「%s」" % hit,
                "",
                "原话：%s" % text[:200],
                "",
                "这类操作做完不好回退，而且你在手机上看不见它到底动哪个文件。",
                "要做就回「确认」，不做回「取消」。",
            ]))
            return

    do_task(client, chat_id, text, cfg, perm_override)


def do_task(client, chat_id, prompt, cfg, perm_override=None, line_name=None):
    """真正跑任务并把结果发回飞书。

    line_name 指定跑在哪条线上。不给就用当前那条 —— 但一旦开始跑就把名字钉死，
    这样中途你 /切 到别的线，这个活的结果还是会标着它自己那条线的名字回来，不会串。
    """
    with STATE_LOCK:
        st = load_state()
        name = line_name or cur_line(st)
        ln = st["lines"].setdefault(name, {"engine": "claude", "session_id": None,
                                           "last": "", "when": 0, "busy": False})
        sid = ln.get("session_id")
        engine = ln.get("engine") or "claude"
        ln["busy"] = True
        ln["last"] = prompt[:100]
        ln["when"] = int(time.time())
        save_state(st)

    run_cfg = dict(cfg)
    if perm_override:
        run_cfg["permission"] = perm_override

    # 任务可能跑很久，先回一句让人知道收到了。多条线并行时要标清是哪条。
    send_msg(client, chat_id, "［%s］收到，在做了……（%s，档位 %s）"
             % (name, engine, run_cfg["permission"]))

    try:
        text, new_sid, bad = run_engine(engine, prompt, run_cfg, sid)
    finally:
        # 不管成没成都得把 busy 摘掉，不然这条线以后一直显示「正在跑」
        with STATE_LOCK:
            st2 = load_state()
            if name in st2["lines"]:
                st2["lines"][name]["busy"] = False
                save_state(st2)

    if new_sid and new_sid != sid:
        touch_line(name, session_id=new_sid)

    prefix = "✗ 出错了\n\n" if bad else ""
    # 回复也标上线名。手机上几条线的消息混在一个群里，不标就分不清哪条是哪条
    parts = build_reply(text, cfg, "［%s］%s" % (name, prefix))
    if len(parts) > 1:
        log("回复太长，分 %d 条发" % len(parts))
    send_reply(client, chat_id, parts)


UPLOAD_DIR = os.path.join(HERE, "uploads")   # 与 web_server 的附件目录共用


def _save_feishu_image(client, message_id, image_key):
    """下载飞书图片到 uploads/。返回 (路径, 错误信息)。"""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest
    req = (GetMessageResourceRequest.builder()
           .message_id(message_id)
           .file_key(image_key)
           .type("image")
           .build())
    resp = client.im.v1.message.resource.get(req)
    if not resp.success():
        hint = ""
        if resp.code in (99991672, 230027):
            hint = "\n（机器人缺 im:message.resource:readonly 权限，去飞书开发者后台开启后重试）"
        return None, "code=%s %s%s" % (resp.code, resp.msg, hint)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, "feishu_%d.jpg" % int(time.time() * 1000))
    try:
        with io.open(path, "wb") as f:
            f.write(resp.file.read())
    except Exception as e:
        return None, "写盘失败：%s" % e
    return path, None


def safe_handle_image(client, chat_id, uid, message_id, image_key, cfg):
    """收到飞书图片：下载存盘 → 让当前线的引擎看图回应。异常全兜底，
    每一步失败都回一条人话 —— 手机上发了图没反应是最伤信任的静默。"""
    try:
        if not image_key:
            send_msg(client, chat_id, "这张图片没取到 image_key，重发一次试试。")
            return
        path, err = _save_feishu_image(client, message_id, image_key)
        if not path:
            send_msg(client, chat_id,
                     "图片收下了但下载失败：%s\n"
                     "也可以先在网页端用 📎 发图，那边不受此限制。" % err)
            AUTH.sec_log("飞书图片下载失败", uid=uid, 原因=err or "")
            return
        log("收到飞书图片 -> %s" % os.path.basename(path))
        prompt = ("[用户在飞书发来一张图片，已保存到 %s]\n"
                  "请先读这张图片：如果图里是报错/代码/表格，直接分析并给出解决办法；"
                  "否则简要描述图片内容。" % path)
        do_task(client, chat_id, prompt, cfg)
    except Exception:
        log("处理飞书图片异常：%s" % traceback.format_exc()[-500:])
        try:
            send_msg(client, chat_id, "图片处理出错了，看 bridge.log 最后几行。")
        except Exception:
            pass


def make_handler(client_holder, cfg_holder):
    """造事件处理函数。

    飞书要求 3 秒内处理完，不然它认为推送失败会重推（15秒/5分钟/1小时/6小时，最多4次）。
    但 claude 跑任务动不动几分钟，所以这里必须立刻返回，
    把真正的活丢到后台线程去干。
    """
    seen = set()  # 处理过的 message_id，防止飞书重推导致同一条消息干两遍
    seen_lock = threading.Lock()

    def on_message(data):
        try:
            ev = data.event
            msg = ev.message
            sender = ev.sender

            mid = msg.message_id or ""
            with seen_lock:
                if mid in seen:
                    log("重复推送，跳过 %s" % mid[:16])
                    return
                seen.add(mid)
                if len(seen) > 500:      # 别无限长
                    seen.clear()
                    seen.add(mid)

            chat_id = msg.chat_id
            uid = ""
            if sender and sender.sender_id:
                uid = sender.sender_id.open_id or sender.sender_id.user_id or ""

            # 机器人自己发的消息不处理，不然会自己跟自己聊起来
            if sender and sender.sender_type == "app":
                return

            text = extract_text(msg.content, msg.message_type)

            cfg = load_config()
            cfg_holder["cfg"] = cfg

            cli = client_holder["client"]

            # ---- 图片消息：没有文字，单独一条通道（鉴权照走，绑定码跳过——图不可能是码）----
            if not text and str(msg.message_type) == "image":
                allowed, why = AUTH.check(uid, chat_id, cfg)
                if not allowed:
                    AUTH.sec_log("拒绝(图片)", uid=uid, chat=chat_id, 原因=why)
                    return
                image_key = ""
                try:
                    image_key = str(json.loads(msg.content or "{}").get("image_key") or "")
                except Exception:
                    pass
                th = threading.Thread(
                    target=safe_handle_image,
                    args=(cli, chat_id, uid, msg.message_id, image_key, cfg),
                    daemon=True)
                th.start()
                return
            elif not text:
                # 图片之外的富媒体（语音/文件等）暂不支持，静默忽略
                log("非文本消息（%s），忽略" % msg.message_type)
                return

            # ---- 绑定码。未授权的人唯一能做的事，所以判在鉴权之前 ----
            # 顺序反了就死锁：谁都没绑定 → 全被拒 → 永远没法完成第一次绑定
            bound, bind_msg = AUTH.try_bind(text, uid, chat_id)
            if bound:
                send_msg(cli, chat_id, bind_msg)
                return

            # ---- 鉴权：默认拒绝 ----
            # 白名单空的时候是「谁都不许用」，不是「不限制」。这跟以前相反。
            allowed, why = AUTH.check(uid, chat_id, cfg)
            if not allowed:
                AUTH.sec_log("拒绝", uid=uid, chat=chat_id, 原因=why,
                             内容=text[:40].replace("\n", " "))
                log("拒绝 %s…（%s）" % (uid[:12], why))
                # 码格式对但值不对时回一句，方便你自己打错时知道；
                # 其余情况完全静默 —— 陌生人发 /帮助 得不到任何系统信息
                if bind_msg:
                    send_msg(cli, chat_id, bind_msg)
                return

            # chat_id 记下来：想给这个会话主动发消息（或者排查问题）时要用它，
            # 私聊的 chat_id 不出现在 im/v1/chats 列表里，只能从事件里拿
            log("收到［%s］：%s" % (chat_id or "?", text[:80].replace("\n", " ")))

            # 丢后台干，立刻返回，满足飞书 3 秒要求
            th = threading.Thread(
                target=safe_handle,
                args=(client_holder["client"], chat_id, uid, text, cfg),
                daemon=True)
            th.start()
        except Exception:
            log("处理事件炸了：\n%s" % traceback.format_exc())

    return on_message


def safe_handle(client, chat_id, uid, text, cfg):
    """后台线程的外壳。任何异常都要接住，不然线程默默死掉，飞书那头没反应。"""
    try:
        handle_text(client, chat_id, uid, text, cfg)
    except Exception:
        tb = traceback.format_exc()
        log("干活炸了：\n%s" % tb)
        try:
            send_msg(client, chat_id, "桥接程序自己出错了：\n%s" % tb[-800:])
        except Exception:
            pass


def check_env():
    """开跑前自查。缺什么当场说清楚，别等连上飞书才发现。"""
    ok = True
    print("=" * 56)
    print("环境自查")
    print("=" * 56)

    try:
        import lark_oapi
        print("  [OK] lark-oapi 已装，版本", getattr(lark_oapi, "__version__", "未知"))
    except ImportError:
        print("  [缺] lark-oapi 没装，跑：python -m pip install lark-oapi")
        ok = False

    cb = claude_bin()
    if os.path.exists(cb):
        print("  [OK] claude 入口", cb)
    else:
        print("  [缺] 找不到 claude.cmd，Claude Code 装了吗")
        ok = False

    xb = codex_bin()
    if os.path.exists(xb):
        print("  [OK] codex 入口", xb)
    else:
        # codex 没装不算致命，只是飞书那头不能用 /codex
        print("  [注意] 找不到 codex.cmd，/codex 用不了（claude 还是能用）")

    if os.path.exists(SETTINGS_PATH):
        try:
            s = json.loads(io.open(SETTINGS_PATH, encoding="utf-8-sig").read())
            # 认多种凭据键名。以前只认 API_KEY，
            # 结果用 ANTHROPIC_AUTH_TOKEN 的机器一直被虚报「没找到凭据」
            CRED_HINTS = ("API_KEY", "AUTH_TOKEN", "ACCESS_TOKEN")
            envk = [k for k in (s.get("env") or {})
                    if any(h in k.upper() for h in CRED_HINTS)
                    and not str((s.get("env") or {})[k]).startswith("${")]
            if envk:
                print("  [OK] settings.json 里有凭据（键名 %s）" % ", ".join(envk))
            else:
                print("  [注意] settings.json 的 env 里没找到 API key，")
                print("         claude -p 可能报 Not logged in")
        except Exception as e:
            print("  [坏] settings.json 读不了：%s" % e)
            ok = False
    else:
        print("  [缺] 没有 settings.json")
        ok = False

    cfg = load_config()
    src = CFG.secret_source()
    if cfg.get("app_id") and cfg.get("app_secret"):
        # 只打脱敏值。日志和截图都可能被转发，明文不能出现在这里
        # secret 一位都不露（keep=0）。app_id 半公开，露前 8 位方便认是哪个应用
        print("  [OK] 飞书凭据已就位  app_id=%s  secret=%s"
              % (CFG.redact(cfg["app_id"], 8), CFG.redact(cfg["app_secret"], 0)))
        print("       来源：%s" % src)
        if src == "json(不安全)":
            print("  [警告] 密钥还明文躺在 bridge_config.json 里。")
            print("         搬到 .env（键名 FEISHU_APP_SECRET）并从 json 删掉。")
    else:
        print("  [缺] 读不到 app_id / app_secret（来源：%s）" % src)
        print("       在 .env 里写 FEISHU_APP_ID / FEISHU_APP_SECRET")
        ok = False

    # 目录白名单。工作目录必须落在里面，否则拒绝启动 ——
    # 启动就报比跑起来之后每条消息都失败要好排查
    print("  [OK] 目录白名单 %d 个：" % len(cfg.get("allowed_dirs") or []))
    for d in (cfg.get("allowed_dirs") or []):
        print("       %s %s" % ("[有]" if os.path.isdir(d) else "[没有]", d))
    wd_ok, wd_info = CFG.check_dir(cfg.get("cwd"), cfg)
    if wd_ok:
        print("  [OK] 工作目录 %s" % wd_info)
    else:
        print("  [缺] 工作目录不合格：%s（%s）" % (cfg.get("cwd"), wd_info))
        alt = CFG.safe_cwd(cfg)
        print("       会退回：%s" % (alt or "没有可用目录，任务全会失败"))
        if not alt:
            ok = False

    # 身份白名单。一个都没绑等于谁都用不了，必须让人看见
    print("  [OK] 身份白名单：")
    for line in AUTH.status_text().splitlines():
        print("       %s" % line)
    if not (AUTH._load().get("bound_users") or cfg.get("allowed_user_ids")):
        print("  [注意] 现在是默认拒绝状态，任何人发消息都会被忽略。")
        print("         跑 python auth.py --code 生成绑定码，在飞书里发给机器人。")

    perm = cfg.get("permission")
    print("  [OK] 档位 %s（%s）" % (perm, {
        "full": "完全跳过权限检查，工具和 MCP 都给",
        "auto": "Claude 自动判断权限；Codex 保持全权限",
        "write": "能改文件，不能跑命令",
        "read": "只能看",
        "chat": "纯聊天，不给工具",
    }.get(perm, "未知档位，会当只读处理")))
    print("  [OK] 危险操作二次确认：%s" % ("开" if cfg.get("confirm_dangerous") else "关"))

    print("=" * 56)
    print("结论：" + ("可以跑" if ok else "还差东西，先补上面标 [缺] 的"))
    return ok


def main():
    ap = argparse.ArgumentParser(description="飞书 ←→ Claude Code 双向桥")
    ap.add_argument("--check", action="store_true", help="只自查环境，不连飞书")
    args = ap.parse_args()

    if args.check:
        sys.exit(0 if check_env() else 1)

    if not check_env():
        print()
        print("环境没准备好，不连了。")
        sys.exit(1)

    import lark_oapi as lark

    cfg = load_config()
    client_holder = {}
    cfg_holder = {"cfg": cfg}

    # 发消息用的普通客户端
    client_holder["client"] = (lark.Client.builder()
                              .app_id(cfg["app_id"])
                              .app_secret(cfg["app_secret"])
                              .log_level(lark.LogLevel.INFO)
                              .build())

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(make_handler(client_holder, cfg_holder))
               .build())

    # 长连接客户端。auto_reconnect 默认开着，网断了自己重连。
    ws = lark.ws.Client(cfg["app_id"], cfg["app_secret"],
                        event_handler=handler,
                        log_level=lark.LogLevel.INFO)

    # ── 开机清一遍 busy 标记 ──
    # finally 只能防异常，防不了断电/强杀：任务死在半路时状态文件里的
    # 「正在跑」会永远挂着。进程重启 = 所有任务必死（引擎子进程不存活），
    # 所以开机把全部 busy 清零是安全的。
    try:
        with STATE_LOCK:
            st0 = load_state()
            changed = False
            for ln in (st0.get("lines") or {}).values():
                if ln.get("busy"):
                    ln["busy"] = False
                    changed = True
            if changed:
                save_state(st0)
                log("清掉了上次崩溃残留的 busy 标记")
    except Exception:
        pass

    # ── 心跳线程：每 20 秒写一次 bridge_heartbeat.json ──
    # 让网页和 manager 知道桥还活着、到飞书的网络通不通。
    # 之前「手机发消息没反应」分不清是电脑睡了、代理崩了还是桥挂了，
    # 心跳文件 + 网页顶部的状态灯就是补这个黑箱。
    def _heartbeat_loop():
        hb_path = os.path.join(HERE, "bridge_heartbeat.json")
        import socket
        while True:
            net_ok = False
            try:
                c = socket.create_connection(("open.feishu.cn", 443), timeout=4)
                c.close()
                net_ok = True
            except Exception:
                net_ok = False
            try:
                with io.open(hb_path, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"ts": time.time(), "net_ok": net_ok,
                                        "pid": os.getpid()}))
            except Exception:
                pass
            time.sleep(20)

    threading.Thread(target=_heartbeat_loop, daemon=True).start()

    print()
    log("开始连飞书长连接……连上会打印 connected to wss://…")
    log("连上之后，在飞书群里 @机器人 发一句话试试。回 /帮助 看能用什么。")
    print()
    ws.start()  # 阻塞在这里，直到进程结束


if __name__ == "__main__":
    main()
