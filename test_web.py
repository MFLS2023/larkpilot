# -*- coding: utf-8 -*-
"""Web 服务器和新功能的集成测试。

测试：
  - 扫描器输出格式
  - Web API 各端点（需要服务器跑着）
  - 序号指令（用假的 run_engine 拦截，不真调 claude）
  - 飞书和 Web 同时发同一个会话（锁防冲突）

跑法：
  确认 web_server.py 跑着后：python test_web.py
"""

import io
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_scanner as S
import session_lock as L
import bridge as B

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PASS, FAIL = [], []
HERE = os.path.dirname(os.path.abspath(__file__))


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("   [OK] %s" % name)
    else:
        FAIL.append((name, detail))
        print("   [NG] %s   → %s" % (name, detail))


# ── 独立的锁和状态文件，测试不动真的 ──────────
L.LOCKS_PATH = os.path.join(HERE, "_tw_locks.json")
L.GATE_PATH = os.path.join(HERE, "_tw_locks.gate")
B.STATE_PATH = os.path.join(HERE, "_tw_state.json")
for p in (L.LOCKS_PATH, L.GATE_PATH, B.STATE_PATH):
    if os.path.exists(p):
        os.remove(p)

# 假的 send_msg，记下发出去的文字
SENT = []
SENT_LOCK = threading.Lock()
_real_send = B.send_msg


def fake_send(client, chat_id, text):
    with SENT_LOCK:
        SENT.append(text)
    return True


B.send_msg = fake_send

# 假的 run_engine，记参数、不调 claude
ENGINE_CALLS = []


def fake_engine(engine, prompt, cfg, session_id=None):
    ENGINE_CALLS.append({"engine": engine, "prompt": prompt, "sid": session_id})
    return ("假回复：%s 用 %s 说了「%s」" % (session_id or "new", engine, prompt[:40]),
            session_id or "fake-sid-%d" % int(time.time()), False)


_real_engine = B.run_engine
B.run_engine = fake_engine

CFG = {
    "app_id": "test", "app_secret": "test",
    "cwd": os.path.join(os.path.expanduser("~"), ".claude", "claude-files"),
    "permission": "full", "confirm_dangerous": False,
    "timeout_seconds": 300, "allowed_user_ids": [], "allowed_chat_ids": [],
    "reply_max_chars": 3000,
}


class FakeClient:
    pass


# ────────────────────────────────────────────────
print()
print("=" * 58)
print("Web 服务器 & 新功能集成测试")
print("=" * 58)

print()
print("组 1  扫描器输出格式检查")
ss = S.scan_all("recent")
check("★ 扫描器能跑（返回列表）", isinstance(ss, list),
      "返回了 %r" % type(ss))
if ss:
    s0 = ss[0]
    for f in ("session_id", "engine", "project", "level", "status",
              "last_time", "last_msg", "title"):
        check("字段 %s 存在" % f, f in s0, "没有这个字段，有的是 %s" % list(s0))
    check("★ level 值合法", s0["level"] in ("active", "recent", "stale"),
          "level=%r" % s0["level"])
    check("★ engine 值合法", s0["engine"] in ("claude", "codex", "pi", "zcode", "antigravity"),
          "engine=%r" % s0["engine"])
    check("★ title 不空", bool(s0["title"]))
    check("★ recent 里没有 stale 会话",
          all(s["level"] in ("active", "recent") for s in ss),
          "有 stale：%d 个" % sum(1 for s in ss if s["level"] == "stale"))

all_ss = S.scan_all()
check("★ scan_all() 不加参数返回所有", len(all_ss) > len(ss),
      "all=%d recent=%d" % (len(all_ss), len(ss)))

print()
print("组 2  详情页历史读取")
if ss:
    sid0 = ss[0]["session_id"]
    h = S.get_session(sid0)
    check("★ 能读到详情", h is not None, "sid=%s" % sid0[:8])
    if h:
        check("★ turns 是列表", isinstance(h.get("turns"), list))
        check("★ 每条 turn 有 role 和 text",
              all("role" in t and "text" in t for t in h["turns"]),
              "有问题的：%r" % [t for t in h["turns"] if "role" not in t][:1])
        check("★ role 只有 user/assistant",
              all(t["role"] in ("user", "assistant") for t in h["turns"]),
              "有不合法的：%r" % [t["role"] for t in h["turns"]
                               if t["role"] not in ("user", "assistant")][:3])

check("不存在的 sid 返回 None", S.get_session("不存在的uuid") is None)

print()
print("组 3  飞书序号指令（用假引擎，不真调 claude）")
B.save_state({})
ENGINE_CALLS[:] = []
SENT[:] = []

# ★ 必须钉住扫描结果，否则测不出东西来：
#   会话文件一直在被写（包括跑测试的这个会话自己），排序随时变。
#   测试拿一份列表、bridge 内部再扫一次，两份的「1 号」可能已经不是同一个会话，
#   断言就会失败——但那是测试的竞态，不是 bridge 的 bug。
#   所以这里 monkey-patch 成固定快照，让两边看到同一份。
_SNAPSHOT = S.scan_all("recent")[:12]
_real_scan_cached = S.scan_all_cached
S.scan_all_cached = lambda level=None: list(_SNAPSHOT)

if _SNAPSHOT:
    target = _SNAPSHOT[0]
    tid = target["session_id"]

    idx = 1
    c = FakeClient()
    B.handle_text(c, "chat1", "u1", "%d 接着干" % idx, CFG)
    # 等后台线程跑完
    t0 = time.time()
    while time.time() - t0 < 15 and len(ENGINE_CALLS) < 1:
        time.sleep(0.3)

    check("★ 序号指令调用了 run_engine", len(ENGINE_CALLS) >= 1,
          "ENGINE_CALLS=%r" % ENGINE_CALLS)
    if ENGINE_CALLS:
        call = ENGINE_CALLS[-1]
        check("★ 传的 session_id 是目标会话的",
              call["sid"] == target["session_id"],
              "传了 %r，期望 %r" % (call["sid"], target["session_id"]))
        check("★ 消息内容对",
              call["prompt"] == "接着干",
              "传的是 %r" % call["prompt"])
        check("★ 回复带了标题前缀",
              any(target["title"] in s for s in SENT if "假回复" in s),
              "发出去的：%r" % [s[:60] for s in SENT])

    # 超出范围
    SENT[:] = []
    B.handle_text(c, "chat1", "u1", "9999 随便说什么", CFG)
    time.sleep(1)
    check("★ 超出范围告知", any("超出范围" in s for s in SENT),
          "发出去的：%r" % SENT[:2])

print()
print("组 4  会话锁在序号指令里生效（飞书和「Web」同时抢同一个会话）")
if _SNAPSHOT:
    ENGINE_CALLS[:] = []
    SENT[:] = []
    # 用快照里的 1 号，跟 bridge 内部看到的是同一个（上面已 patch scan_all_cached）
    sid1 = _SNAPSHOT[0]["session_id"]

    # 先用锁占着
    tok = L.acquire(sid1, who="Web 测试")
    check("★ 预先占锁成功", bool(tok))

    # 再发序号指令
    c2 = FakeClient()
    B.handle_text(c2, "chat1", "u1", "1 再做一件事", CFG)
    time.sleep(1)

    check("★ 锁住时序号指令被挡住，没调引擎",
          len(ENGINE_CALLS) == 0, "居然调了：%r" % ENGINE_CALLS)
    check("★ 告知被占用",
          any("占" in s for s in SENT), "发出去的：%r" % SENT[:2])

    L.release(sid1, tok)
    check("★ 放锁后能正常发", bool(L.acquire(sid1, who="验证放锁")))
    L.release(sid1)

# 快照用完了，恢复真实扫描（后面的组要用真数据）
S.scan_all_cached = _real_scan_cached

print()
print("组 5  Web API（需要 web_server.py 在跑）")
def _web_port():
    """从 web_config.json 读端口，不写死 —— 端口改过一次了（8000→58080），
    写死会导致测试连不上但看起来像服务器挂了。"""
    try:
        return int(json.loads(
            io.open(os.path.join(HERE, "web_config.json"),
                    encoding="utf-8-sig").read()).get("port") or 58080)
    except Exception:
        return 58080


BASE = "http://127.0.0.1:%d" % _web_port()


def http(path, data=None, cookie=""):
    """简单 HTTP 工具。返回 (状态码, body字典 or None)。"""
    opener = urllib.request.build_opener()
    if cookie:
        opener.addheaders = [("Cookie", cookie)]
    try:
        if data:
            req = urllib.request.Request(
                BASE + path, data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(BASE + path)
        with opener.open(req, timeout=10) as r:
            raw = r.read()
            try:
                return r.getcode(), json.loads(raw)
            except Exception:
                return r.getcode(), {}      # HTML 页面不是 JSON，返回空字典就行
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:
            body = {}
        return e.code, body
    except Exception as e:
        return -1, {"error": str(e)}


# 先登录拿 Cookie
code, _ = http("/")
check("Web 服务器在跑", code in (200, 302, 401), "连不上（code=%d）" % code)

# 登录
import http.cookiejar as _cookiejar

# 登录
jar = _cookiejar.CookieJar()
opener_auth = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(jar))
try:
    opener_auth.open(
        urllib.request.Request(
            BASE + "/login",
            data=urllib.parse.urlencode({"password": "test1234"}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}),
        timeout=10)
    login_ok = True
    # 从 jar 里拿 session cookie 字符串
    cookie_val = "; ".join("%s=%s" % (c.name, c.value) for c in jar)
except Exception as e:
    login_ok = False
    cookie_val = ""

check("★ 登录成功", login_ok, "出错：%s" % (str(locals().get('e','')) if not login_ok else ""))

if cookie_val:
    # 列表 API
    code, body = http("/api/sessions?level=recent", cookie=cookie_val)
    check("★ /api/sessions 返回 200", code == 200, "code=%d body=%r" % (code, body))
    check("★ 返回的有 sessions 字段", "sessions" in body, "body=%r" % body)
    if "sessions" in body:
        check("★ sessions 不空（有最近的会话）", len(body["sessions"]) > 0,
              "sessions=%r" % body["sessions"][:1])
        if body["sessions"]:
            s = body["sessions"][0]
            for f in ("session_id", "engine", "title", "level", "status"):
                check("API 字段 %s 存在" % f, f in s)

    # 未登录状态应该 401
    code2, _ = http("/api/sessions")
    check("★ 没登录访问 API 返回 401", code2 == 401, "返回了 %d" % code2)

    # 不存在的会话返回 404（用合法 UUID 格式，不然 URL 含中文 urllib 直接连不上）
    code3, body3 = http("/api/sessions/00000000-0000-0000-0000-000000000000", cookie=cookie_val)
    check("★ 不存在的会话返回 404", code3 == 404, "返回了 %d" % code3)

    # 任务 API
    code4, body4 = http("/api/task/00000000000000000000000000000000", cookie=cookie_val)
    check("★ 不存在的任务返回 404", code4 == 404, "返回了 %d" % code4)

    # 发消息 API（不给 sid 应该 400）
    code5, body5 = http("/api/send", {"msg": "测试"}, cookie=cookie_val)
    check("★ 不给 sid 发消息返回 400", code5 == 400, "返回了 %d body=%r" % (code5, body5))

    # 被锁的会话 API 应该返回 409
    # 注：测试进程用的是独立的锁文件（_tw_locks.json），
    # web_server 是另一个进程用真实的 session_locks.json，
    # 两边文件不同，所以这里改为测「成功发消息返回 task_id」
    if body.get("sessions"):
        sid_test = body["sessions"][0]["session_id"]
        code6, body6 = http("/api/send", {"sid": sid_test, "msg": "测试task_id"}, cookie=cookie_val)
        check("★ 发消息 API 返回任务 id", code6 == 200 and "task_id" in body6,
              "code=%d body=%r" % (code6, body6))
else:
    print("   （登录没拿到 Cookie，跳过 API 测试）")

print()
print("组 6  飞书 /列表 显示所有窗口（不只是线）")
SENT[:] = []
c3 = FakeClient()
B.handle_text(c3, "chat1", "u1", "/列表", CFG)
time.sleep(1)
check("★ /列表 发了回复", bool(SENT), "没有发任何消息")
if SENT:
    reply = SENT[-1]
    check("★ 回复里有会话信息（有序号）",
          "1. 🟢" in reply or "1. 🟡" in reply or "1. ⚪" in reply,
          "开头是：%r" % reply[:60])
    check("★ 回复里有引擎名", "Claude" in reply or "Codex" in reply,
          "回复：%r" % reply[:200])

# 清理
for p in (L.LOCKS_PATH, L.GATE_PATH, B.STATE_PATH):
    if os.path.exists(p):
        os.remove(p)

B.run_engine = _real_engine
B.send_msg = _real_send

print()
print("=" * 58)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    print("失败的：")
    for n, d in FAIL:
        print("   - %s   → %s" % (n, d))
print("=" * 58)
sys.exit(1 if FAIL else 0)
