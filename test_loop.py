# -*- coding: utf-8 -*-
"""闭环测试 —— 伪造飞书事件喂给真实的处理函数，回复真的发到群里。

为什么要这样测：
  机器人靠长连接接收事件，测试脚本没法「代替你在手机上打字」。
  所以这里造一个跟飞书推过来一模一样的事件对象，喂给 make_handler 造出的
  那个 on_message —— 走的是完全相同的代码路径（去重、跳过自己、丢后台线程），
  回复也真的调飞书 API 发出去。你打开飞书就能看到结果。

跑法：python test_loop.py [群id] [--keep]
     不给群id就自己建一个（机器人当群主，所以它一定在群里）。
     --keep 测完不删群；默认全过就删、有失败就留着给你查。

★ 判定口径 —— 以前的版本在这里假阳性：
  只有飞书返回 code == 0 才算「发出去了」。以前只要调用了发送函数就记一笔，
  机器人不在群里（230002）、缺 im:message.group_msg 权限（230027）时，
  一条消息都没真的发出去，测试却全绿。
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge as B

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print("   [OK] %s" % name)
    else:
        FAIL.append((name, detail))
        print("   [NG] %s   → %s" % (name, detail))


ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
KEEP = "--keep" in sys.argv

# ---- 不许两份同时跑 ----
# 踩过：上一次跑卡在 wait_final 的 300 秒超时里没退出，我以为杀掉了，
# 结果它和新起的那份同时往 _test_loop_state.json 和日志里写，
# 输出互相覆盖，报出来的失败项根本不是这次的。
LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "_test_loop.lock")


def _other_alive():
    try:
        old = int(io.open(LOCK_PATH, encoding="utf-8").read().strip())
    except Exception:
        return 0
    if old == os.getpid():
        return 0
    try:
        import psutil
        if psutil.pid_exists(old):
            p = psutil.Process(old)
            if "python" in (p.name() or "").lower():
                return old
    except ImportError:
        return old      # 判不了就当它还活着，宁可拦住也别再撞一次
    except Exception:
        pass
    return 0


_busy = _other_alive()
if _busy:
    print("已经有一份 test_loop 在跑（PID %s），先等它跑完或者杀掉它。" % _busy)
    print("确认它已经死了就删掉 %s" % LOCK_PATH)
    sys.exit(2)
io.open(LOCK_PATH, "w", encoding="utf-8").write(u"%d" % os.getpid())
import atexit
atexit.register(lambda: os.path.exists(LOCK_PATH) and os.remove(LOCK_PATH))

CFG = B.load_config()

# 伪造的发件人。桥现在是默认拒绝的，不把它放进白名单的话所有事件都会被静默丢掉，
# 后面每一条断言都会失败 —— 而且失败原因看起来像「功能坏了」，其实是没授权。
# 只改这个进程里的 cfg 字典，不动 bridge_auth.json，跑完不留痕迹。
FAKE_UID = "ou_faketest"
CFG["allowed_user_ids"] = list(CFG.get("allowed_user_ids") or []) + [FAKE_UID]

# ---- 飞书 OpenAPI（建群、删群用，跟 test_e2e.py 同一套写法）----
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机有代理，飞书得绕开
BASE = "https://open.feishu.cn/open-apis"


def api(path, body=None, token=None, method=None):
    """打飞书 OpenAPI。返回 (code, msg, data)。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 method=method or ("POST" if data else "GET"))
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=30) as r:
            j = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode("utf-8"))
        except Exception:
            return (-1, "HTTP %s" % e.code, {})
    except Exception as e:
        return (-1, str(e), {})
    return (j.get("code"), j.get("msg"), j.get("data") or {})


def get_token():
    req = urllib.request.Request(
        BASE + "/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": CFG["app_id"],
                         "app_secret": CFG["app_secret"]}).encode("utf-8"),
        method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with OPENER.open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("tenant_access_token")
    except Exception:
        return None


def watchers():
    """把已绑定的人拉进测试群，这样你在飞书里能亲眼看到过程。拿不到就算了。"""
    ids = []
    try:
        import auth as AUTH
        ids = list((AUTH._load().get("bound_users") or {}).keys())
    except Exception:
        pass
    ids += [u for u in (CFG.get("allowed_user_ids") or []) if u != FAKE_UID]
    return [u for u in dict.fromkeys(ids) if u.startswith("ou_")][:5]


TOKEN = None
MADE_GROUP = False
if ARGS:
    CHAT_ID = ARGS[0]
else:
    # 自己建群：机器人是群主，「机器人不在群里」这种情况从根上排除。
    # 以前写死一个群 id，那个群被删掉或者机器人被踢出去之后，测试照样全绿。
    TOKEN = get_token()
    if not TOKEN:
        print("拿不到 tenant_access_token，建不了群。检查 .env 里的 app_id/app_secret。")
        sys.exit(1)
    body = {"name": "桥接闭环自测 %s" % time.strftime("%m-%d %H:%M"),
            "description": "test_loop.py 建的，测完自动删",
            "chat_mode": "group", "chat_type": "private"}
    # user_id_list 默认按 user_id 解析，传 open_id 必须显式声明 user_id_type
    watch = watchers()
    code, msg, data = api(
        "/im/v1/chats?set_bot_manager=true&user_id_type=open_id",
        dict(body, user_id_list=watch) if watch else body, TOKEN)
    if code != 0 and watch:
        # 拉人失败不该让整个测试跑不起来 —— 群本身比观众重要
        print("      带观众建群失败（code=%s %s），改成只有机器人的群" % (code, msg))
        code, msg, data = api("/im/v1/chats?set_bot_manager=true", body, TOKEN)
    CHAT_ID = data.get("chat_id")
    if not CHAT_ID:
        print("建群失败：code=%s msg=%s" % (code, msg))
        print("常见原因：im:chat 权限没开，或者应用没发布版本。")
        sys.exit(1)
    MADE_GROUP = True

# 群白名单：bridge_auth.json 里已经绑过群的话，ok_chats 非空，
# 这个新建的群不在里面就会被拒。显式加进去。
CFG["allowed_chat_ids"] = list(CFG.get("allowed_chat_ids") or []) + [CHAT_ID]

# ★ 必须换掉 load_config 本身。
#   on_message 每收一条事件都重新 `cfg = load_config()` 读磁盘（bridge.py:1177），
#   传给 make_handler 的那个 cfg_holder 在鉴权这条路上没被用上。
#   所以只改内存里的 CFG 字典是没用的 —— 磁盘上 allowed_user_ids 是空的，
#   伪造发件人会被默认拒绝规则静默丢掉，看起来像「功能全坏了」。
B.load_config = lambda: CFG

# 用真的 lark client，回复真的发到群里
import lark_oapi as lark
CLIENT = lark.Client.builder() \
    .app_id(CFG["app_id"]).app_secret(CFG["app_secret"]).build()

# 同时记一份，好在这里断言
SENT = []        # 只放飞书真的收下了的（code == 0）
REJECTED = []    # 飞书拒了的，(code, msg, 内容前80字)
_real_send = B.send_msg


def spy_send(client, chat_id, text):
    """★ 只有飞书收下了才记进 SENT。

    以前是先 append 再发，发失败也算一条 —— 于是「机器人不在群里」这种
    根本没送达的情况，所有断言依然全过。现在拒了就不记，
    后面那些 `out and "xxx" in out[-1]` 会自然失败。
    """
    res = _real_send(client, chat_id, text)
    if getattr(res, "ok", bool(res)) and getattr(res, "code", 0) == 0:
        SENT.append(text)
    else:
        REJECTED.append((getattr(res, "code", "?"), getattr(res, "msg", "?"),
                         (text or "")[:80].replace("\n", " ")))
    return res


B.send_msg = spy_send

# 状态单独放，不污染真在跑的桥
B.STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_test_loop_state.json")
if os.path.exists(B.STATE_PATH):
    os.remove(B.STATE_PATH)


class Obj(object):
    """伪造飞书事件对象。lark 推过来的是对象不是字典，所以得用属性访问。"""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


_seq = [0]


def fake_event(text, msg_id=None, sender_type="user", msg_type="text", uid=None):
    """造一个跟 P2ImMessageReceiveV1 结构一致的事件。

    字段名是从 lark_oapi 装好的源码里抄的（event/dispatcher_handler.py 那条链），
    不是我猜的。
    """
    _seq[0] += 1
    return Obj(event=Obj(
        sender=Obj(sender_id=Obj(open_id=uid or FAKE_UID, user_id="fakeuser"),
                   sender_type=sender_type, tenant_key="t"),
        message=Obj(message_id=msg_id or ("om_fake_%d_%d" % (time.time(), _seq[0])),
                    chat_id=CHAT_ID, chat_type="group", message_type=msg_type,
                    content=json.dumps({"text": text}, ensure_ascii=False),
                    mentions=None, root_id=None, parent_id=None,
                    create_time=str(int(time.time() * 1000)))))


# make_handler 要的是字典不是列表 —— 键名照 bridge.main() 里的写法
HANDLER = B.make_handler({"client": CLIENT}, {"cfg": CFG})

print()
print("=" * 58)
print("闭环测试（伪造事件 → 真实处理 → 真发飞书）")
print("=" * 58)
print("群：%s%s" % (CHAT_ID, "（本次新建）" if MADE_GROUP else "（你指定的）"))

# ---- 预检：先确认这条群真的发得进去 ----
# 不先验这一步的话，后面 40 条断言会因为同一个原因全红，
# 而报错内容看起来像「功能坏了」，其实是机器人不在群里或者缺权限。
def bail(why, hints=()):
    """基础环节不通就立刻收工。

    ★ 不这么做的话，后面每个 wait_final 都要等满 300 秒 —— 一次跑成 20 分钟的僵尸，
      还会跟下一次运行同时写状态文件和日志，把结果搅成一团（真踩过）。
    """
    print()
    print("停：%s" % why)
    for h in hints:
        print("  " + h)
    if MADE_GROUP and not KEEP:
        api("/im/v1/chats/%s" % CHAT_ID, None, TOKEN or get_token(), method="DELETE")
        print("（刚建的测试群已删掉）")
    print("通过 %d 项，失败 %d 项（提前收工，后面没跑）" % (len(PASS), len(FAIL)))
    sys.exit(1)


pre = _real_send(CLIENT, CHAT_ID, "闭环自测开始 —— 这条能看到说明发送链路是通的")
check("★ 机器人真的能往这个群发消息", pre.ok and pre.code == 0,
      "code=%s msg=%s" % (pre.code, pre.msg))
if not (pre.ok and pre.code == 0):
    bail("消息发不进这个群，后面全测不了", [
        "230002  机器人不在这个群里 —— 把它拉进群，或者别传群 id 让脚本自己建",
        "230027  缺权限 —— 开发者后台加 im:message.group_msg 并重新发布版本",
        "99991663 / 99991664  app_id 或 app_secret 不对 —— 查 .env",
        "code=None  网络层没通（代理抽风 / 连接超时），跟权限无关，重跑一次看看",
    ])


def joined(msgs):
    """把多条拼起来再匹配。

    长回复会被 split_for_feishu 切成几条发，关键词可能落在任意一条里，
    所以断言不能只看 out[-1]。
    """
    return "\n".join(msgs or [])


def feed(text, wait=3, quiet=1.2, **kw):
    """喂一条消息进去，等后台线程干完。

    第一条到了之后再多等 quiet 秒 —— 长回复是分几条发的，
    第一条一到就返回会只抓到第 1 段（以前就是这样，断言时不时抽风）。
    """
    n0 = len(SENT)
    HANDLER(fake_event(text, **kw))
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(0.2)
        if len(SENT) > n0:
            break
    last = len(SENT)
    t1 = time.time()
    while time.time() - t1 < quiet:      # 等后续分片
        time.sleep(0.2)
        if len(SENT) > last:
            last = len(SENT)
            t1 = time.time()
    return SENT[n0:]


def drain(quiet=2.5, timeout=60):
    """等到「连续 quiet 秒没有新消息」，把迟到的回复清干净。

    ★ 组之间必须清一次。实测 /列表 要扫本机所有会话文件，回复 5 秒后才发出来，
      于是它落进了下一组的观察窗口 —— 「机器人自己发的消息被忽略了」那条
      就是这么误报的：SENT 里多出来的其实是上一组迟到的 /列表 回复。
    返回这段时间里清掉了几条。
    """
    n0 = len(SENT)
    last = len(SENT)
    t0 = t1 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(0.3)
        if len(SENT) > last:
            last = len(SENT)
            t1 = time.time()
        elif time.time() - t1 >= quiet:
            break
    return len(SENT) - n0


def wait_more(n0, timeout=300):
    """等后台线程把最终回复发出来。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if len(SENT) > n0:
            return True
        time.sleep(1)
    return False


def wait_final(line, n0, timeout=300):
    """等某条线的最终回复。

    ★ 不能靠「消息条数够了」来判断 —— 并行时几条线的消息交错发出，
    条数够了很可能是别人的回复先到，断言就会读到上一步的东西（我第一版就栽在这）。
    只认「开头是这条线的名字、且不是『收到，在做了』」的那条。
    """
    tag = "［%s］" % line
    t0 = time.time()
    while time.time() - t0 < timeout:
        hits = [s for s in SENT[n0:] if s.startswith(tag) and "收到，在做了" not in s]
        if hits:
            return hits[-1]
        time.sleep(1)
    return ""


print()
print("组 1  3 秒契约 —— 处理函数必须立刻返回")
t0 = time.time()
HANDLER(fake_event("只回答一个数字：1 加 1 等于几"))
elapsed = time.time() - t0
check("★ on_message 立刻就返回了（飞书要求 3 秒内）", elapsed < 3.0,
      "花了 %.2f 秒" % elapsed)
print("      （实测 %.3f 秒返回，活丢给后台线程了）" % elapsed)
# 必须等这个活彻底跑完 —— 它的回复要是迟到，会被后面的断言误读成自己的
print("      等这个活跑完……")
first = wait_final("默认", 0, 150)      # 实测 claude 十几秒就回，150 秒够宽
check("★ 后台线程真的把活干完了并回了飞书", "2" in first,
      "回的是：%r" % first[:200])
if "2" not in first:
    bail("第一个活就没跑通，后面全建立在这条链路上，没必要继续", [
        "回复是空的 → 事件被鉴权拦了（看上面有没有「拒绝」），或者 claude 没起来",
        "手动验一下：python bridge.py --check",
    ])
time.sleep(2)

print()
print("组 2  内置命令走完整链路（回复真的发到飞书了）")
out = feed("/帮助")
check("★ /帮助 真的发到飞书了", "飞书遥控" in joined(out),
      "发出去的是：%r" % out)

# /列表 要扫本机所有 Claude/Codex 会话文件，冷启动能跑好几秒，3 秒等不到
out = feed("/列表", wait=30)
check("★ /列表 有回话", bool(out), "发出去的是：%r" % out)

out = feed("/新 测试线A")
check("★ /新 建线并回话", "测试线A" in joined(out), "发出去的是：%r" % out)
check("状态文件里真有这条线", "测试线A" in B.load_state()["lines"])

out = feed("/新 测试线B codex")
check("★ /新 带引擎参数", "codex" in joined(out), "发出去的是：%r" % out)

# ★ /列表 有两套渲染（bridge.py:777）：电脑上有近期窗口就显示窗口列表，
#   没有才显示「线」列表。所以不能断言线名一定出现 —— 那取决于你电脑上
#   此刻开着几个 Claude/Codex 窗口，跟桥本身没关系。
#   这里只断言「回的是一份列表」，线列表本身单独直接验。
out = feed("/列表", wait=30)
txt = joined(out)
check("★ /列表 回的是一份列表（窗口列表或线列表都算）",
      ("当前会话（" in txt) or ("现在有 " in txt and "条线" in txt),
      "发出去的是：%r" % txt[:200])
lines_txt = B.list_lines(B.load_state())
check("★ 线列表本身两条线都在（直接验，不受窗口列表干扰）",
      "测试线A" in lines_txt and "测试线B" in lines_txt,
      "线列表是：%r" % lines_txt[:200])

print()
print("组 3  机器人不能回自己的消息（防死循环）")
_late = drain()   # 先把上一组迟到的回复清掉，不然会被算成这一组的
if _late:
    print("      （清掉了上一组迟到的 %d 条）" % _late)
n0 = len(SENT)
HANDLER(fake_event("/帮助", sender_type="app"))
time.sleep(2)
check("★ 机器人自己发的消息被忽略了", len(SENT) == n0,
      "居然回了：%r" % SENT[n0:])

print()
print("组 4  飞书重推同一条消息，不能干两遍")
same_id = "om_dedup_test_%d" % time.time()
out1 = feed("/状态", msg_id=same_id)
drain()                                        # 同上，清掉迟到的
n0 = len(SENT)
HANDLER(fake_event("/状态", msg_id=same_id))   # 同一个 message_id 再推一次
time.sleep(2)
check("★ 第一次处理了", bool(out1), "第一次发的：%r" % out1)
check("★ 重推的那条被去重了", len(SENT) == n0, "又回了：%r" % SENT[n0:])

print()
print("组 5  非文本消息不能炸")
n0 = len(SENT)
HANDLER(fake_event("x", msg_type="image"))
time.sleep(1.5)
check("★ 图片消息安静跳过，没崩", True)

print()
print("组 6  真跑一个活，看回复到不到飞书（claude）")
feed("/切 测试线A")
n0 = len(SENT)
HANDLER(fake_event("只回答一个数字：12 加 30 等于几"))
print("      等 claude 干活……")
wait_more(n0, 30)
tail = SENT[n0:]
check("★ 先回了「收到，在做了」", tail and "收到，在做了" in tail[0],
      "发出去的是：%r" % tail)
final = wait_final("测试线A", n0, 300)
check("★ claude 的答案发到飞书了（42）", "42" in final, "最后发的是：%r" % final[:300])
check("★ 回复带了线名前缀", final.startswith("［测试线A］"),
      "开头是：%r" % final[:30])

sid_a = B.load_state()["lines"]["测试线A"].get("session_id")
check("★ 这条线存下了会话号", bool(sid_a), "sid=%r" % sid_a)

print()
print("组 7  真跑一个活（codex）")
feed("/切 测试线B")
n0 = len(SENT)
HANDLER(fake_event("只回答一个数字：7 乘以 8 等于几"))
print("      等 codex 干活……")
final = wait_final("测试线B", n0, 300)
tail = SENT[n0:]
check("★ codex 的答案发到飞书了（56）", "56" in final, "最后发的是：%r" % final[:300])
check("★ 回复带了 codex 那条线的名字", final.startswith("［测试线B］"),
      "开头是：%r" % final[:30])
check("★ 提示里标明用的是 codex", tail and "codex" in tail[0],
      "开头那句：%r" % (tail[0] if tail else ""))

sid_b = B.load_state()["lines"]["测试线B"].get("session_id")
check("★ codex 线的会话号跟 claude 线不一样", sid_b and sid_b != sid_a,
      "A=%r B=%r" % (sid_a, sid_b))

print()
print("组 8  各自续接，验证上下文没串")
feed("/切 测试线A")
n0 = len(SENT)
HANDLER(fake_event("刚才那个结果减去 2，只回答数字"))
final = wait_final("测试线A", n0, 300)
check("★ A 线接上了自己的上下文（42-2=40）", "40" in final,
      "回的是：%r" % final[:300])

feed("/切 测试线B")
n0 = len(SENT)
HANDLER(fake_event("刚才那个结果减去 2，只回答数字"))
final = wait_final("测试线B", n0, 300)
check("★ B 线接上了自己的上下文（56-2=54）", "54" in final,
      "回的是：%r" % final[:300])

print()
print("组 9  危险操作走完整链路")
# 配置里 confirm_dangerous 现在是 false（你自己关的），但这个功能本身要测。
# 临时打开，测完还原 —— 只改内存里的 cfg，不动 bridge_config.json。
_old_confirm = CFG.get("confirm_dangerous")
CFG["confirm_dangerous"] = True
n0 = len(SENT)
out = feed("把 dist 目录 rm -rf 掉", wait=5)
check("★ 危险操作被拦住，问了一句", "回「确认」" in joined(out),
      "发出去的是：%r" % out)
check("★ 说清楚了是什么操作", "递归删除文件" in joined(out))
pend = B.load_state().get("pending")
check("★ 记下了待确认，也记了是哪条线", pend and pend.get("line") == "测试线B",
      "pending=%r" % pend)

out = feed("取消", wait=5)
check("★ 回「取消」真的作废了", "什么都没动" in joined(out),
      "发出去的是：%r" % out)
check("★ pending 清了", not B.load_state().get("pending"))
CFG["confirm_dangerous"] = _old_confirm

print()
print("组 10  两条线同时收到任务（真并行，走完整链路）")
n0 = len(SENT)
# 两条线连着打，中间不等 —— 模拟你在手机上快速切着发
HANDLER(fake_event("/切 测试线A"))
time.sleep(1)
HANDLER(fake_event("只回答一个数字：100 加 11 等于几"))
time.sleep(0.5)
HANDLER(fake_event("/切 测试线B"))
time.sleep(1)
HANDLER(fake_event("只回答一个数字：200 加 22 等于几"))
print("      两条线并行干活，等……")
fa = wait_final("测试线A", n0, 400)
fb = wait_final("测试线B", n0, 400)
check("★ A 线的结果回来了且答案对（111）", "111" in fa, "A 回的：%r" % fa[:200])
check("★ B 线的结果回来了且答案对（222）", "222" in fb, "B 回的：%r" % fb[:200])
check("★ 两条线的结果没串（各自答案不在对方回复里）",
      fa and fb and "222" not in fa and "111" not in fb,
      "A=%r B=%r" % (fa[:100], fb[:100]))
# 两条线同时干活时，各自的会话号也不能被对方覆盖
st = B.load_state()
check("★ 并行之后两条线的会话号还是各自的",
      st["lines"]["测试线A"].get("session_id") == sid_a
      and st["lines"]["测试线B"].get("session_id") == sid_b,
      "A %r→%r  B %r→%r" % (sid_a, st["lines"]["测试线A"].get("session_id"),
                            sid_b, st["lines"]["测试线B"].get("session_id")))

print()
print("组 11  默认拒绝：不在白名单的人一句话都收不到")
drain()
n0 = len(SENT)
HANDLER(fake_event("/帮助", uid="ou_stranger_not_bound"))
time.sleep(2.5)
check("★ 陌生人发 /帮助 完全没回应（不泄露任何系统信息）", len(SENT) == n0,
      "居然回了：%r" % SENT[n0:])

print()
print("组 12  发送结果判定：飞书拒过的消息一条都不能有")
# 这一组是整个测试的地基。它红了，上面所有绿的都不算数 ——
# 说明消息其实没送达，只是「调用了发送函数」。
check("★ 全程飞书没拒过任何一条消息", not REJECTED,
      "被拒 %d 条，头几条：%r" % (len(REJECTED), REJECTED[:3]))
print("      实际发出去 %d 条，被拒 %d 条" % (len(SENT), len(REJECTED)))

print()
print("=" * 58)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    print("失败的：")
    for n, d in FAIL:
        print("   - %s   → %s" % (n, d))
print()
print("这些消息都真的发进群 %s 了，打开飞书能看到。" % CHAT_ID)

# ---- 收尾：本次新建的群，全过就删，有失败就留着给你查 ----
if MADE_GROUP:
    if KEEP or FAIL:
        print("群留着了（%s）：%s" % ("你加了 --keep" if KEEP else "有失败项，方便你去看现场",
                                     CHAT_ID))
    else:
        TOKEN = TOKEN or get_token()
        code, msg, _ = api("/im/v1/chats/%s" % CHAT_ID, None, TOKEN, method="DELETE")
        print("测试群已删除" if code == 0 else "群没删掉（code=%s %s），自己去飞书里删" % (code, msg))
print("=" * 58)
sys.exit(1 if FAIL else 0)
