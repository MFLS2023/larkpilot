# -*- coding: utf-8 -*-
"""AI 会话监控 Web 服务器。

一句话：手机浏览器打开 http://电脑IP:8000，看到所有正在跑的 Claude/Codex 会话，
能查对话历史、能发消息让它继续干活。飞书那条通道同时保留，两边共用同一套锁。

跑法：
    python web_server.py

默认监听 0.0.0.0:8000（局域网内可以用 http://192.168.x.x:8000 访问）。
第一次打开会提示设置密码。

端口和密码在同目录的 web_config.json 里改。
"""

import io
import json
import os
import sys
import threading
import time
import uuid

from flask import (Flask, abort, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

# 同目录的模块
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import session_lock as L
import session_scanner as S
import config as CFG        # 目录白名单 / 工作目录校验

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

WEB_CONFIG_PATH = os.path.join(HERE, "web_config.json")
WEB_CONFIG_DEFAULTS = {
    "port": 8000,
    "host": "0.0.0.0",
    "password_hash": "",          # 空 = 第一次打开提示设置
    "session_secret": "",         # 空 = 启动时随机生成（重启后已登录的 Cookie 失效）
    "cookie_days": 30,
    "task_timeout": 1800,         # 发消息的子进程最长跑多久（秒）
    # 出厂默认权限档位。2026-08-24 反转为 read（只读）：开源后陌生人第一次跑
    # 不该自带删文件执行命令的能力；自己机器上在 web_config.json 显式写
    # "default_perm": "full" 即可保持全开体验。
    "default_perm": "read",
    # 网页派的任务完成后往飞书绑定群推一条。不想被打扰就改成 false。
    "notify_feishu": True,
    "debug": False,
}


def load_web_config():
    cfg = dict(WEB_CONFIG_DEFAULTS)
    try:
        with io.open(WEB_CONFIG_PATH, encoding="utf-8-sig") as stream:
            d = json.load(stream)
        if isinstance(d, dict):
            cfg.update(d)
    except Exception:
        pass
    return cfg


def save_web_config(cfg):
    try:
        with io.open(WEB_CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(cfg, ensure_ascii=False, indent=2))
        return True
    except Exception:
        return False


WEB_CFG = load_web_config()

# Flask 用 secret_key 给 Cookie 签名。每次启动随机生成的话，重启后 Cookie 失效，
# 你得重新输密码。生产建议手动设一个固定的写进 web_config.json。
if not WEB_CFG.get("session_secret"):
    WEB_CFG["session_secret"] = uuid.uuid4().hex

app = Flask(__name__, template_folder=os.path.join(HERE, "templates"),
            static_folder=os.path.join(HERE, "static"))
app.secret_key = WEB_CFG["session_secret"]
# 静态文件让浏览器缓存 1 小时：手机走 Tailscale 时每个资源都要付往返延迟，
# 不缓存的话每次刷新都重新拉全部 CSS/JS（约 73KB），慢链路上就是好几秒。
# 代价是改样式后最长 1 小时看不到——急看就强制刷新。
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

# ---------------------------------------------------------------------------
# 任务队列：发消息是异步的，不然手机上等几分钟会超时
# ---------------------------------------------------------------------------

TASKS = {}           # task_id → {status, result, error, started}
TASKS_LOCK = threading.Lock()


def _make_task():
    tid = uuid.uuid4().hex
    with TASKS_LOCK:
        TASKS[tid] = {"status": "pending", "result": None, "error": None,
                      "partial": "",   # 流式增量文本（claude 引擎才有）
                      "started": time.time()}
        # 清掉超过 1 小时的旧任务，不然内存会一直涨
        old = [k for k, v in TASKS.items()
               if time.time() - v["started"] > 3600]
        for k in old:
            TASKS.pop(k, None)
    return tid


def _task_done(tid, result=None, error=None):
    with TASKS_LOCK:
        if tid in TASKS:
            TASKS[tid]["status"] = "done"
            TASKS[tid]["result"] = result
            TASKS[tid]["error"] = error


# ---------------------------------------------------------------------------
# 任务完成后推送到飞书：网页派的活干完了，人在外面也能被叫回来。
# 推送是锦上添花 —— 任何失败都静默吞掉（最多影响通知），绝不能反过来
# 干扰任务本身或拖慢响应。
# ---------------------------------------------------------------------------

_FEISHU_CLIENT = None


def _feishu_client():
    """懒加载飞书客户端，进程内复用。失败返回 None，下次再试。"""
    global _FEISHU_CLIENT
    if _FEISHU_CLIENT is not None:
        return _FEISHU_CLIENT
    try:
        import lark_oapi as lark
        import bridge as B
        cfg = B.load_config()
        if not cfg.get("app_id") or not cfg.get("app_secret"):
            return None
        _FEISHU_CLIENT = (lark.Client.builder()
                          .app_id(cfg["app_id"])
                          .app_secret(cfg["app_secret"])
                          .log_level(lark.LogLevel.ERROR)
                          .build())
        return _FEISHU_CLIENT
    except Exception:
        return None


def _bound_chat_ids():
    """绑定群列表（bridge_auth.json 的 bound_chats），最多取 3 个防刷屏。"""
    try:
        d = json.loads(io.open(os.path.join(HERE, "bridge_auth.json"),
                               encoding="utf-8-sig").read())
        return list((d.get("bound_chats") or {}).keys())[:3]
    except Exception:
        return []


def feishu_notify_task(engine, seconds, ok, preview):
    """发完成通知。preview 是回复文本开头一小段。"""
    client = _feishu_client()
    chats = _bound_chat_ids()
    if client is None or not chats:
        return
    tag = "✅ 网页任务完成" if ok else "⚠️ 网页任务出错"
    body = "%s · %s · %d 秒\n%s" % (
        tag, S.ENGINE_CN.get(engine, engine), int(seconds),
        (preview or "").strip()[:160])
    try:
        import bridge as B
        for chat_id in chats:
            try:
                B.send_msg(client, chat_id, body)
            except Exception:
                pass
    except Exception:
        pass


# 非 full 档位下，引擎回复里出现这些说法 ≈ 有动作被权限拦了。
# 刻意保守（只认高置信短语），误报的代价只是多一条提示横幅。
_PERM_BLOCK_MARKERS = (
    "permission denied", "requires approval", "not permitted",
    "权限不足", "需要批准", "被权限拦截",
)


def perm_blocked_hint(text):
    """当前档位下引擎是不是碰壁了 —— 给前端挂「切全开重试」提示用。"""
    t = str(text or "").lower()
    return any(m in t for m in _PERM_BLOCK_MARKERS)


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def _logged_in():
    return session.get("auth") == "ok"


def _require_login(f):
    """装饰器：没登录就跳到登录页（网页）或返回 401（API）。"""
    from functools import wraps

    @wraps(f)
    def wrapper(*args, **kwargs):
        if _logged_in():
            return f(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登录"}), 401
        return redirect(url_for("login"))
    return wrapper


# ---------------------------------------------------------------------------
# 路由：认证
# ---------------------------------------------------------------------------

_INDEX_BATCH = 40       # 搜索索引每轮处理的常规文件数


# 登录失败节流：同一来源连错 5 次 → 封 15 分钟。内存态，重启即清。
# 这是开源前最后一个安全硬伤 —— 之前密码是唯一防线却没有防爆破，
# 公网暴露的场景下等于不设防。
_LOGIN_FAILS = {}       # ip -> {"n": 连错次数, "until": 封禁截止时间戳}
_LOGIN_MAX_FAILS = 5
_LOGIN_LOCK_SECONDS = 900


def _login_blocked_left(ip):
    """被封中还剩多少秒；没被封返回 0。"""
    rec = _LOGIN_FAILS.get(ip)
    if not rec:
        return 0
    left = int(rec.get("until", 0) - time.time())
    return max(left, 0)


def _login_record_fail(ip):
    rec = _LOGIN_FAILS.setdefault(ip, {"n": 0, "until": 0})
    rec["n"] += 1
    if rec["n"] >= _LOGIN_MAX_FAILS:
        rec["until"] = time.time() + _LOGIN_LOCK_SECONDS
        rec["n"] = 0


@app.before_request
def _t0():
    request._t0 = time.time()


@app.after_request
def _log_slow(resp):
    """慢请求日志：>800ms 或 5xx 的记一行，排查「手机上列表刷不出来」这类问题。"""
    try:
        dt = (time.time() - getattr(request, "_t0", time.time())) * 1000
        if resp.status_code >= 500 or dt > 800 or "/api/" in request.path:
            line = "[%s] %sms %s %s -> %s" % (
                time.strftime("%H:%M:%S"), int(dt), request.method,
                request.path, resp.status_code)
            with io.open(os.path.join(HERE, "web_timing.log"), "a",
                         encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
    return resp


@app.route("/login", methods=["GET", "POST"])
def login():
    cfg = load_web_config()
    error = ""
    ip = request.remote_addr or "?"

    if request.method == "POST":
        blocked = _login_blocked_left(ip)
        if blocked > 0:
            return render_template(
                "login.html",
                error="失败次数太多，%d 分 %d 秒后再试"
                      % (blocked // 60, blocked % 60)), 429

    # 还没设密码：先引导设置
    if not cfg.get("password_hash"):
        if request.method == "POST":
            pw = request.form.get("password", "").strip()
            pw2 = request.form.get("password2", "").strip()
            if not pw:
                error = "密码不能为空"
            elif pw != pw2:
                error = "两次输入的密码不一样"
            else:
                cfg["password_hash"] = generate_password_hash(pw)
                save_web_config(cfg)
                session["auth"] = "ok"
                session.permanent = True
                from datetime import timedelta
                app.permanent_session_lifetime = timedelta(
                    days=int(cfg.get("cookie_days") or 30))
                return redirect(url_for("index"))
        return render_template("setup.html", error=error)

    if request.method == "POST":
        pw = request.form.get("password", "")
        if check_password_hash(cfg["password_hash"], pw):
            _LOGIN_FAILS.pop(ip, None)      # 成功就清零，别冤枉自己
            session["auth"] = "ok"
            session.permanent = True
            from datetime import timedelta
            app.permanent_session_lifetime = timedelta(
                days=int(cfg.get("cookie_days") or 30))
            return redirect(url_for("index"))
        _login_record_fail(ip)
        left_n = _LOGIN_MAX_FAILS - _LOGIN_FAILS.get(ip, {}).get("n", 0)
        error = ("密码不对" if left_n > 0
                 else "失败次数太多，账号已临时锁定 15 分钟")

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# 路由：页面
# ---------------------------------------------------------------------------

@app.route("/")
@_require_login
def index():
    return render_template("list.html")


@app.route("/session/<sid>")
@_require_login
def detail(sid):
    return render_template("detail.html", sid=sid)


# ---------------------------------------------------------------------------
# 路由：API
# ---------------------------------------------------------------------------

@app.route("/api/sessions")
@_require_login
def api_sessions():
    """会话列表。

    参数：
        level   recent（默认）/ active / all —— 活跃度档位
        kind    claude-desktop / claude-code / codex / pi —— 平台细分筛选，
                空 = 不筛。Claude 引擎下按客户端拆成两档
        days    整数，只看最近 N 天；>0 时数据源自动换成全量
                （recent 档只有 12 小时窗口，不换源筛日期等于没筛）
    """
    level = request.args.get("level", "recent")
    if level not in ("active", "recent", "all"):
        level = "recent"
    kind_f = request.args.get("kind", "")
    # days 支持小数：12 小时 = 0.5 天
    days = request.args.get("days", type=float) or 0

    # 用带缓存的扫描：网页 3 秒一轮询 + 切筛选，不能每次都全盘重读
    # 700+ 个文件（那要几秒，手机上切个筛选项就得干等）。
    # kind/days 的过滤是纯内存操作，命中缓存时毫秒级返回。
    sessions = S.scan_all_cached("all" if days > 0 else level)

    # kind = 平台细分的筛选键。Claude 引擎下再按客户端拆开：
    # 桌面客户端和 Claude Code 是你眼里两个不同的东西，筛的时候要分开
    def _kind_of(s):
        if s["engine"] != "claude":
            return s["engine"]
        return "claude-desktop" if s.get("client") == "Claude Desktop" else "claude-code"

    for s in sessions:
        s["kind"] = _kind_of(s)

    if kind_f:
        sessions = [s for s in sessions if s["kind"] == kind_f]
    if days > 0:
        cutoff = time.time() - days * 86400
        sessions = [s for s in sessions if s["last_time"] >= cutoff]
    locked = L.locked_set()
    result = []
    for s in sessions:
        result.append({
            "session_id": s["session_id"],
            "engine": s["engine"],
            "client": s.get("client") or s["engine"],      # 新：哪个软件
            "can_run": s.get("can_run", True) and s["engine"] in _supported_engines(),
            "title": s["title"],           # 线名 or 第一句话 or 项目名
            "project": s["project"],
            "first_msg": s.get("first_msg") or "",
            "cwd": s["cwd"],
            "status": s["status"],
            "level": s["level"],
            "last_time": s["last_time"],
            "last_time_ago": S.time_ago(s["last_time"]),
            "last_msg": s["last_msg"],
            "locked": s["session_id"] in locked,
            "lock_info": L.who_holds(s["session_id"]) if s["session_id"] in locked else None,
        })
    # 解析器自检：最近的会话文件里有整份读不出消息的 = 上游可能又改格式了，
    # 前端拿去在列表顶部挂报警横幅。查坏了也不挡列表，降级成没有横幅。
    try:
        health_bad = S.parser_health()
    except Exception:
        health_bad = []

    # 已登记的全部平台细分（kind），给前端动态渲染筛选条 ——
    # 以后在 EXTRA_SOURCES 挂新工具，筛选条自动多一项，不用改模板
    kinds = ["claude-desktop", "claude-code", "codex"]
    kinds += [x["engine"] for x in S.EXTRA_SOURCES]
    kinds = list(dict.fromkeys(kinds + ["zcode"]))

    # 飞书桥心跳：桥进程每 20 秒写一次 bridge_heartbeat.json。
    # 读不到 / 超过 60 秒没更新 = 桥没在跑或卡死了 —— 之前手机发消息没反应
    # 分不清原因，这个灯就是补那个黑箱的。
    hb_age, hb_net = None, None
    try:
        with io.open(os.path.join(HERE, "bridge_heartbeat.json"),
                     encoding="utf-8-sig") as stream:
            hb = json.load(stream)
        hb_age = int(time.time() - float(hb.get("ts") or 0))
        hb_net = bool(hb.get("net_ok"))
    except Exception:
        pass

    return jsonify({"sessions": result, "count": len(result), "level": level,
                    "health_bad": health_bad, "kinds": kinds,
                    "bridge": {"age_sec": hb_age, "net_ok": hb_net}})


@app.route("/api/sessions/<sid>")
@_require_login
def api_session_detail(sid):
    """某个会话的对话历史。"""
    max_turns = int(request.args.get("turns", 30))
    h = S.get_session(sid, max_turns)
    if not h:
        return jsonify({"error": "找不到这个会话：%s" % sid}), 404

    locked = sid in L.locked_set()
    try:
        usage = S.get_usage(sid)
    except Exception:
        usage = {}
    # 标题跟列表页同源（摘要 > 首句 > 项目名），详情页顶栏不再显示
    # 光秃秃的项目目录名 —— 用户反馈的「和 Claude Code 标题不匹配」就是这
    title = None
    for s in S.scan_all_cached("all"):
        if s["session_id"] == sid:
            title = s.get("title")
            break
    return jsonify({
        "session_id": sid,
        "engine": h["engine"],
        "project": h["project"],
        "title": title,
        "cwd": h["cwd"],
        "turns": h["turns"],
        "locked": locked,
        "usage": usage,
        "lock_info": L.who_holds(sid) if locked else None,
        "can_run": h.get("can_run", True) and h["engine"] in _supported_engines(),
        "can_trash": h["engine"] != "zcode",
        # 告诉前端本机配置的默认档位，按钮初始高亮跟着它走
        "default_perm": (load_web_config().get("default_perm") or "read"),
    })


@app.route("/api/workdirs")
@_require_login
def api_workdirs():
    """新建对话可选的工作目录。

    来源是 bridge_config.json 的 allowed_dirs 白名单（只列真实存在的），
    默认目录（cfg.cwd）排第一。安全边界不变：真正落盘前 safe_cwd 还会再校验一次，
    这里给的只是「菜单」，不是授权。
    """
    cfg = CFG.load_config()
    out, seen = [], set()

    def add(p):
        if not p or p in seen or not os.path.isdir(p):
            return
        seen.add(p)
        name = os.path.basename(p.rstrip("\\/")) or p
        out.append({"path": p, "label": name})

    add(cfg.get("cwd"))
    for d in (cfg.get("allowed_dirs") or []):
        add(d)
    return jsonify({"workdirs": out})


IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
DOC_EXTS = (".pdf", ".txt", ".log", ".md", ".csv", ".json")
UPLOAD_DIR = os.path.join(HERE, "uploads")


@app.route("/api/upload", methods=["POST"])
@_require_login
def api_upload():
    """手机上传附件给 AI：截图或文档（PDF/文本类）。

    存到 uploads/ 目录（时间戳重命名防路径注入），把落盘路径还给前端；
    前端把它拼进消息文本，引擎用自己读文件的工具打开。
    安全边界：只收白名单扩展名、限 20MB、文件名不采用用户的。
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "没收到文件"}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in IMAGE_EXTS + DOC_EXTS:
        return jsonify({"error": "支持的类型：图片 "
                        + "/".join(IMAGE_EXTS)
                        + "，文档 " + "/".join(DOC_EXTS)}), 400
    blob = f.read()
    if len(blob) > 20 * 1024 * 1024:
        return jsonify({"error": "文件超过 20MB"}), 400
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = "%d_%s%s" % (int(time.time() * 1000), uuid.uuid4().hex[:6], ext)
    path = os.path.join(UPLOAD_DIR, name)
    with io.open(path, "wb") as out:
        out.write(blob)
    # 顺手清掉 7 天前的旧图 —— 不清理的话这个目录只进不出，迟早几个 GB
    threading.Thread(target=_cleanup_uploads, daemon=True).start()
    return jsonify({"path": path})


def _cleanup_uploads():
    """删 uploads/ 里超过 7 天的文件。任何失败都静默 —— 清理不是关键路径。"""
    try:
        cutoff = time.time() - 7 * 86400
        for n in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, n)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 隐藏 / 回收站。设计原则：隐藏零风险（只记名单不动文件）；彻底删除 =
# 移入 trash/ 保留 30 天可找回，到期自动清空。
# ---------------------------------------------------------------------------

HIDDEN_STORE = os.path.join(HERE, "hidden_sessions.json")
TRASH_DIR = os.path.join(HERE, "trash")
TRASH_KEEP_DAYS = 30


def _hs_read():
    try:
        d = json.loads(io.open(HIDDEN_STORE, encoding="utf-8-sig").read())
    except Exception:
        d = {}
    d.setdefault("hidden", {})
    d.setdefault("trashed", {})
    return d


def _hs_write(d):
    tmp = HIDDEN_STORE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, HIDDEN_STORE)


def _session_meta(sid):
    for s in S.scan_all_cached("all"):
        if s["session_id"] == sid:
            return s
    return None


def _purge_trash():
    """清掉回收站里超过保留期的文件。任何失败都静默。"""
    try:
        cutoff = time.time() - TRASH_KEEP_DAYS * 86400
        for n in os.listdir(TRASH_DIR):
            p = os.path.join(TRASH_DIR, n)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


@app.route("/api/hidden")
@_require_login
def api_hidden():
    """隐藏中 + 回收站中的会话清单，给「已隐藏」视图用。"""
    d = _hs_read()
    return jsonify({"hidden": d["hidden"], "trashed": d["trashed"]})


@app.route("/api/hide", methods=["POST"])
@_require_login
def api_hide():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    if not sid:
        return jsonify({"error": "sid 不能为空"}), 400
    meta = _session_meta(sid) or {}
    d = _hs_read()
    d["hidden"][sid] = {"when": time.time(),
                        "title": meta.get("title") or meta.get("first_msg")
                        or sid[:8],
                        "engine": meta.get("engine") or ""}
    _hs_write(d)
    S._CACHE.clear()               # 隐藏立即从列表消失，不等 4 秒缓存过期
    return jsonify({"ok": True})


@app.route("/api/unhide", methods=["POST"])
@_require_login
def api_unhide():
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    d = _hs_read()
    if not d["hidden"].pop(sid, None):
        return jsonify({"error": "它不在隐藏列表里"}), 404
    _hs_write(d)
    S._CACHE.clear()               # 恢复显示立即生效
    return jsonify({"ok": True})


@app.route("/api/trash", methods=["POST"])
@_require_login
def api_trash():
    """彻底删除 = 把该会话的所有文件移进 trash/（30 天内可 untrash 找回）。"""
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    if not sid:
        return jsonify({"error": "sid 不能为空"}), 400
    files = S.find_session_files(sid)
    if not files:
        return jsonify({"error": "在磁盘上没找到这个会话的文件"}), 404
    os.makedirs(TRASH_DIR, exist_ok=True)
    moved = []
    stamp = int(time.time() * 1000)
    for i, orig in enumerate(files):
        new = os.path.join(TRASH_DIR, "%d_%d_%s" % (stamp, i, os.path.basename(orig)))
        os.replace(orig, new)
        moved.append({"orig": orig, "new": new})
    d = _hs_read()
    d["hidden"].pop(sid, None)
    meta = _session_meta(sid) or {}
    d["trashed"][sid] = {"when": time.time(), "files": moved,
                         "title": (meta.get("title") or meta.get("first_msg")
                                   or sid[:8]),
                         "engine": meta.get("engine") or ""}
    _hs_write(d)
    S._CACHE.clear()               # 移入回收站立即从列表消失
    threading.Thread(target=_purge_trash, daemon=True).start()
    return jsonify({"ok": True, "moved": len(moved)})


@app.route("/api/untrash", methods=["POST"])
@_require_login
def api_untrash():
    """从回收站找回来：按记录的原路径移回去。原路径目录没了就建出来。"""
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    d = _hs_read()
    rec = d["trashed"].pop(sid, None)
    if not rec:
        return jsonify({"error": "它不在回收站里"}), 404
    restored = 0
    for pair in rec.get("files") or []:
        orig, cur = pair.get("orig"), pair.get("new")
        if not orig or not cur or not os.path.isfile(cur):
            continue
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        try:
            os.replace(cur, orig)
            restored += 1
        except Exception:
            pass
    _hs_write(d)
    S._CACHE.clear()               # 找回后立即出现在列表
    if restored == 0:
        return jsonify({"error": "回收站里的文件已不在（可能过了 30 天被清了）"}), 410
    return jsonify({"ok": True, "restored": restored})


@app.route("/api/search")
@_require_login
def api_search():
    """跨所有会话搜内容。索引由后台线程周期性增量更新，
    刚装好或刚改完格式后头几分钟可能搜不全，属预期行为。"""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"results": []})
    try:
        import session_search
        results = session_search.search(q, limit=20)
    except Exception:
        results = []
    return jsonify({"results": results})


def _supported_engines():
    """桥当前能从手机续接的引擎名单，以 bridge.supported_engines() 为准。

    名单 = 内置 claude/codex + clients.json 里 can_run 且有 runner 的来源。
    """
    try:
        import bridge as B
        return tuple(B.supported_engines())
    except Exception:
        return ("claude", "codex")


@app.route("/api/send", methods=["POST"])
@_require_login
def api_send():
    """给会话发消息。立刻返回 task_id，前端拿它轮询进度。

    请求体（JSON）：
        sid     string   目标会话的 session_id；new=true 时留空
        msg     string   要说的话
        perm    string   "full"|"write"|"read"|"chat"，默认 full
        new     bool     true = 开全新对话（不带任何历史）
        engine  string   new=true 时必填："claude" 或 "codex"

    新对话为什么用固定锁键：新会话还没有 session_id，没有可争抢的
    历史文件，但两个同引擎的新建同时跑会让「哪个是新会话」变糊涂，
    所以用 new-claude / new-codex 把同类新建串起来。
    """
    body = request.get_json(silent=True) or {}
    sid = str(body.get("sid") or "").strip()
    msg = str(body.get("msg") or "").strip()
    # 前端没带档位时用配置里的出厂默认（read），不再硬编码全开
    perm = str(body.get("perm") or load_web_config().get("default_perm")
               or "read").lower()
    engine = str(body.get("engine") or "").strip().lower()
    is_new = str(body.get("new") or "").lower() in ("1", "true", "yes")
    # 只读来源直接拒绝，不依赖可能隐藏、过期或读取失败的会话列表。
    if sid.startswith("sess_") or engine == "zcode":
        return jsonify({"error": "Zcode 当前仅支持查看历史，不能发送任务。"}), 403
    want_cwd = None         # 新建对话才用：想落哪个目录，is_new 分支里覆盖

    if is_new:
        if engine not in _supported_engines():
            return jsonify({"error": "新建对话引擎只支持：%s"
                            % " / ".join(_supported_engines())}), 400
        lock_key = "new-" + engine
        # 新对话可选工作目录：这里只记住用户想用哪个，
        # 落盘前 _run 里还会过一次 safe_cwd 白名单校验
        want_cwd = str(body.get("cwd") or "").strip() or None
    else:
        if not sid:
            return jsonify({"error": "sid 不能为空"}), 400
        lock_key = sid
        # 不在支持名单里的来源只能看历史 —— 名单以 bridge.SUPPORTED_ENGINES 为准
        src_engine = _engine_of(sid)
        if src_engine not in _supported_engines():
            return jsonify({
                "error": "这个会话来自 %s，暂时只能看历史，还不能从手机发消息续接"
                         % S.ENGINE_CN.get(src_engine, src_engine)}), 400
    if not msg:
        return jsonify({"error": "消息不能为空"}), 400
    if perm not in ("full", "write", "read", "chat"):
        perm = "read"       # 瞎写的档位按最保守处理

    # 先查有没有在跑（新建时查的是同类新建这个槽位）
    if lock_key in L.locked_set():
        info = L.who_holds(lock_key)
        who = (info or {}).get("who") or "另一个程序"
        return jsonify({"error": "这个会话正被「%s」占着，等它跑完再发" % who,
                        "locked": True, "lock_info": info}), 409

    tid = _make_task()

    def _run():
        # 这里 import bridge 里的函数，不重复实现 claude/codex 调用逻辑
        try:
            import bridge as B
            cfg = B.load_config()
            cfg["permission"] = perm
            # 新对话指定了工作目录：必须再过一次 safe_cwd 白名单，
            # 不合格会自动退回默认目录 —— 前端给的菜单不是授权
            if is_new and want_cwd:
                safe = CFG.safe_cwd(cfg, want_cwd)
                if safe:
                    cfg["cwd"] = safe
            # 跟 bridge.py 一样：挤不进锁表要重试，那不是「被占用」。
            tok, why = None, None
            for _try in range(3):
                tok, why = L.acquire_ex(lock_key, who="网页",
                                        ttl=int(cfg.get("timeout_seconds") or 1800))
                if tok or why != L.GATE:
                    break
                time.sleep(0.5)
            if not tok:
                if why == L.BUSY:
                    h = L.who_holds(lock_key) or {}
                    _task_done(tid, error="会话正被「%s」占着" % (h.get("who") or "另一个程序"))
                elif why == L.WRITE:
                    _task_done(tid, error="锁表写不进去（磁盘满或没权限）")
                else:
                    _task_done(tid, error="锁表一直挤不进去，请重试")
                return
            try:
                with TASKS_LOCK:
                    if tid in TASKS:
                        TASKS[tid]["status"] = "running"
                t0 = time.time()
                use_engine = engine if is_new else _engine_of(sid)

                def _progress(txt, _tid=tid):
                    """claude 流式回调：把累计文本塞进任务，轮询接口带给前端。"""
                    with TASKS_LOCK:
                        if _tid in TASKS:
                            TASKS[_tid]["partial"] = txt

                text, new_sid, bad = B.run_engine(
                    use_engine, msg, cfg, None if is_new else sid,
                    on_progress=_progress)
                # 如果会话在 bridge_state 里有对应的线，更新那条线的 session_id
                if not is_new:
                    _maybe_update_line(sid, new_sid)
                _task_done(tid, result={
                    "text": text, "session_id": new_sid, "bad": bad,
                    # 非 full 档位下碰了权限墙 → 前端挂「切全开重试」提示
                    "perm_hint": (perm != "full" and perm_blocked_hint(text))})
                # 完成推送到飞书（web_config.json 的 notify_feishu 可关）
                if str(load_web_config().get("notify_feishu", True)).lower() \
                        not in ("0", "false", "no"):
                    threading.Thread(
                        target=feishu_notify_task,
                        args=(use_engine, time.time() - t0,
                              not bad, (text or "")[:200]),
                        daemon=True).start()
            finally:
                L.release(lock_key, tok)
        except Exception as e:
            _task_done(tid, error=str(e))

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"task_id": tid, "status": "pending"})


@app.route("/api/task/<tid>")
@_require_login
def api_task(tid):
    """查异步任务的状态。"""
    with TASKS_LOCK:
        t = TASKS.get(tid)
    if not t:
        return jsonify({"error": "任务不存在（可能已过期清理）"}), 404
    return jsonify({"task_id": tid, **t})


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------



def _engine_of(sid):
    """从 bridge_state 或 scanner 里推断这个会话用哪个引擎。"""
    try:
        st = json.loads(io.open(S.BRIDGE_STATE, encoding="utf-8-sig").read())
        for ln in (st.get("lines") or {}).values():
            if ln.get("session_id") == sid:
                return ln.get("engine") or "claude"
    except Exception:
        pass
    # 找不到就扫一遍。必须用带缓存的版本 —— 这里在「发消息」的关键路径上，
    # 飞书没记录的会话（比如网页建的、pi 的）每次发送都会走到这，
    # 用全量裸扫一次就是 2.5 秒的额外等待
    for s in S.scan_all_cached("all"):
        if s["session_id"] == sid:
            return s["engine"]
    return "claude"


def _maybe_update_line(old_sid, new_sid):
    """如果飞书桥有对应的线，更新 session_id（续接后 sid 可能变）。"""
    if not new_sid or new_sid == old_sid:
        return
    try:
        path = S.BRIDGE_STATE
        st = json.loads(io.open(path, encoding="utf-8-sig").read())
        changed = False
        for ln in (st.get("lines") or {}).values():
            if ln.get("session_id") == old_sid:
                ln["session_id"] = new_sid
                changed = True
        if changed:
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(st, ensure_ascii=False, indent=2))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------

def main():
    cfg = load_web_config()
    host = cfg.get("host") or "0.0.0.0"
    port = int(cfg.get("port") or 8000)
    debug = bool(cfg.get("debug"))

    # ── 回收站定期清理：启动清一次 + 每 12 小时一次 ──
    _purge_trash()

    def _trash_loop():
        while True:
            time.sleep(12 * 3600)
            _purge_trash()
    threading.Thread(target=_trash_loop, daemon=True).start()

    # ── 扫描缓存预热：服务一起来就后台扫好，手机第一次打开不等冷扫 ──
    def _warm():
        try:
            import session_scanner as S
            S.scan_all_cached("recent")
            S.scan_all_cached("all")
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()

    # ── 全文搜索索引：后台线程每 60 秒啃一小批文件 ──
    # 头两轮放宽到 300 个/轮，尽快把存量历史吃进索引；之后 40 个/轮
    # 只消化新增量。全部在 daemon 线程里，崩了不影响主业务。
    def _index_loop():
        time.sleep(20)                       # 让主业务先起来
        rounds = 0
        while True:
            try:
                import session_search
                info = session_search.update_index(
                    max_files=300 if rounds < 2 else _INDEX_BATCH)
                rounds += 1
            except Exception:
                pass
            time.sleep(60)
    threading.Thread(target=_index_loop, daemon=True).start()

    # 找本机局域网 IP，打印出来告诉用户手机上该访问哪个地址
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    print("=" * 56)
    print("AI 会话监控 Web 服务器")
    print("=" * 56)
    if not cfg.get("password_hash"):
        print("第一次启动，打开页面后会引导你设密码。")
    print()
    print("  本机：    http://127.0.0.1:%d" % port)
    print("  局域网：  http://%s:%d" % (local_ip, port))
    print()
    print("手机和电脑在同一个 Wi-Fi 下，用局域网地址。")
    print("Ctrl+C 停止。")
    print("=" * 56)

    if not os.path.isdir(os.path.join(HERE, "templates")):
        print("[警告] templates/ 目录不存在，页面会显示空白。")

    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == "__main__":
    main()
