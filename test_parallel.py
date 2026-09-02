# -*- coding: utf-8 -*-
"""并发压力测试 —— 验证多条线同时跑活会不会串。

跟 test_bridge.py 的区别：那个是一条一条按顺序验逻辑，
这个是真的开线程同时打，看状态文件会不会被互相覆盖、
两条线的会话号会不会张冠李戴。

跑法：python test_parallel.py
"""
import io
import json
import os
import sys
import threading
import time

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


# 假的发消息：记下每条消息发给了谁，带线程安全
SENT = []
SENT_LOCK = threading.Lock()


class FakeClient(object):
    pass


def fake_send(client, chat_id, text):
    with SENT_LOCK:
        SENT.append(text)
    return True


B.send_msg = fake_send
B.STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "_test_parallel_state.json")
if os.path.exists(B.STATE_PATH):
    os.remove(B.STATE_PATH)

CFG = {
    "cwd": os.path.join(os.path.expanduser("~"), ".claude", "claude-files"),
    "permission": "full",
    "confirm_dangerous": True,
    "timeout_seconds": 600,
    "reply_max_chars": 3000,
}

print()
print("=" * 58)
print("并发压力测试")
print("=" * 58)

print()
print("组 A  20 个线程同时改状态，看会不会互相覆盖")
B.save_state({})
# 先建 20 条线
for i in range(20):
    B.handle_line_cmd(FakeClient(), "chat1", "new", "线%02d" % i, CFG)

st = B.load_state()
check("★ 20 条线都建出来了", len(st["lines"]) == 20,
      "只有 %d 条：%r" % (len(st["lines"]), sorted(st["lines"])))

# 20 个线程同时给自己那条线写 session_id
errs = []


def writer(i):
    try:
        for _ in range(15):
            B.touch_line("线%02d" % i, session_id="sid-%02d" % i,
                         last="第 %d 号在忙" % i)
            time.sleep(0.001)
    except Exception as e:
        errs.append("%d: %s" % (i, e))


ths = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
t0 = time.time()
for t in ths:
    t.start()
for t in ths:
    t.join()
took = time.time() - t0

check("并发写没抛异常", not errs, "errs=%r" % errs[:3])

st = B.load_state()
check("★ 并发写完还是 20 条线（没被覆盖丢掉）", len(st["lines"]) == 20,
      "只剩 %d 条：%r" % (len(st["lines"]), sorted(st["lines"])))

wrong = [n for n in st["lines"]
         if st["lines"][n].get("session_id") != "sid-" + n[-2:]]
check("★ 每条线的会话号都是自己的，没张冠李戴", not wrong,
      "错的：%r" % [(n, st["lines"][n].get("session_id")) for n in wrong[:5]])

check("状态文件还是合法 JSON",
      isinstance(json.loads(io.open(B.STATE_PATH, encoding="utf-8-sig").read()), dict))
print("      （20 线程 × 15 次写，耗时 %.2f 秒）" % took)

print()
print("组 B  真的并行跑两个引擎，看结果会不会串")
B.save_state({})
B.handle_line_cmd(FakeClient(), "chat1", "new", "算数A", CFG)
B.handle_line_cmd(FakeClient(), "chat1", "new", "算数B codex", CFG)

SENT[:] = []
results = {}


def worker(line, prompt):
    try:
        B.do_task(FakeClient(), "chat1", prompt, dict(CFG, permission="read"),
                  None, line)
        results[line] = "跑完了"
    except Exception as e:
        results[line] = "炸了：%s" % e


# A 线问 111+222，B 线（codex）问 333+444，两个数字不重叠，串了就看得出来
t1 = threading.Thread(target=worker, args=("算数A", "只回答一个数字：111 加 222 等于几"))
t2 = threading.Thread(target=worker, args=("算数B", "只回答一个数字：333 加 444 等于几"))
t0 = time.time()
t1.start()
t2.start()
t1.join()
t2.join()
took = time.time() - t0
print("      （两个引擎并行，总耗时 %.1f 秒）" % took)

check("两条线都跑完了没抛异常",
      results.get("算数A") == "跑完了" and results.get("算数B") == "跑完了",
      "results=%r" % results)

# 从发出去的消息里挑出各自的最终回复（带线名前缀，且不是「收到，在做了」那句）
def final_of(line):
    tag = "［%s］" % line
    hits = [s for s in SENT if s.startswith(tag) and "收到，在做了" not in s]
    return hits[-1] if hits else ""


rа = final_of("算数A")
rb = final_of("算数B")
check("★ A 线的答案对（111+222=333）", "333" in rа, "A 回的是：%s" % rа[:200])
check("★ B 线的答案对（333+444=777）", "777" in rb, "B 回的是：%s" % rb[:200])
check("★ 两条线的回复都标了自己的线名",
      rа.startswith("［算数A］") and rb.startswith("［算数B］"),
      "A=%r B=%r" % (rа[:20], rb[:20]))

st = B.load_state()
sa = st["lines"]["算数A"].get("session_id")
sb = st["lines"]["算数B"].get("session_id")
check("★ 两条线各自存下了会话号", bool(sa) and bool(sb), "A=%r B=%r" % (sa, sb))
check("★ 两个会话号不一样（没互相覆盖）", sa != sb, "都是 %r" % sa)
check("★ 跑完 busy 都摘掉了",
      not st["lines"]["算数A"].get("busy") and not st["lines"]["算数B"].get("busy"),
      "A busy=%r B busy=%r" % (st["lines"]["算数A"].get("busy"),
                               st["lines"]["算数B"].get("busy")))

print()
print("组 C  并行之后各自续接，验证上下文没混")
SENT[:] = []
t1 = threading.Thread(target=worker, args=("算数A", "把刚才那个结果加 1，只回答数字"))
t2 = threading.Thread(target=worker, args=("算数B", "把刚才那个结果加 1，只回答数字"))
t1.start()
t2.start()
t1.join()
t2.join()

rа2 = final_of("算数A")
rb2 = final_of("算数B")
check("★ A 线接着自己的上下文（333+1=334）", "334" in rа2,
      "A 回的是：%s" % rа2[:200])
check("★ B 线接着自己的上下文（777+1=778）", "778" in rb2,
      "B 回的是：%s" % rb2[:200])

st = B.load_state()
check("★ 续接后会话号没变（还是各自那条）",
      st["lines"]["算数A"].get("session_id") == sa
      and st["lines"]["算数B"].get("session_id") == sb,
      "A %r→%r  B %r→%r" % (sa, st["lines"]["算数A"].get("session_id"),
                            sb, st["lines"]["算数B"].get("session_id")))

print()
print("组 D  引擎跑挂了，busy 得摘掉（不然那条线永远显示正在跑）")
B.save_state({})
B.handle_line_cmd(FakeClient(), "chat1", "new", "会炸的", CFG)

real_run = B.run_engine


def boom(engine, prompt, cfg, session_id=None):
    raise RuntimeError("假装引擎炸了")


B.run_engine = boom
try:
    B.do_task(FakeClient(), "chat1", "随便", CFG, None, "会炸的")
except RuntimeError:
    pass
B.run_engine = real_run

st = B.load_state()
check("★ 引擎抛异常后 busy 也摘掉了",
      not st["lines"]["会炸的"].get("busy"),
      "busy=%r" % st["lines"]["会炸的"].get("busy"))

print()
print("组 E  状态文件被写坏，不能整个崩")
io.open(B.STATE_PATH, "w", encoding="utf-8").write("{这不是JSON")
st = B.load_state()
check("★ 坏状态文件读出空结构而不是抛异常", isinstance(st.get("lines"), dict),
      "读出来是 %r" % st)
B.handle_line_cmd(FakeClient(), "chat1", "new", "灾后重建", CFG)
check("★ 坏文件之后还能正常建线", "灾后重建" in B.load_state()["lines"])

if os.path.exists(B.STATE_PATH):
    os.remove(B.STATE_PATH)

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
