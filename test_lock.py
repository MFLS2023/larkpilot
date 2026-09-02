# -*- coding: utf-8 -*-
"""会话锁测试。重点验真并发 —— 顺序调用测不出竞态。

跑法：python test_lock.py
"""

import io
import json
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import session_lock as L

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


# 测试用独立的锁文件，别动真的
HERE = os.path.dirname(os.path.abspath(__file__))
L.LOCKS_PATH = os.path.join(HERE, "_test_locks.json")
L.GATE_PATH = os.path.join(HERE, "_test_locks.gate")
for p in (L.LOCKS_PATH, L.GATE_PATH):
    if os.path.exists(p):
        os.remove(p)

print()
print("=" * 58)
print("会话锁测试")
print("=" * 58)

print()
print("组 1  基本的拿锁放锁")
tok = L.acquire("sid-A", who="飞书")
check("★ 拿到锁了", bool(tok), "返回 %r" % tok)
check("★ 同一个会话第二次拿不到", L.acquire("sid-A", who="网页") is None)
check("★ 别的会话不受影响", bool(L.acquire("sid-B", who="网页")))

h = L.who_holds("sid-A")
check("★ 查得出是谁占着", h and h.get("who") == "飞书", "查到 %r" % h)
check("★ 查询结果里没有 token（那是凭据）", h and "token" not in h,
      "泄漏了：%r" % h)
check("占用时长算得出", h and isinstance(h.get("held_for"), int))

check("★ 放锁成功", L.release("sid-A", tok))
check("★ 放完能再拿", bool(L.acquire("sid-A", who="别人")))

print()
print("组 2  token 不对不许放（防止误放别人的锁）")
L.release("sid-A")
L.release("sid-B")
tok2 = L.acquire("sid-C", who="甲")
check("★ 拿错 token 放不掉", not L.release("sid-C", "假token"))
check("★ 锁还在", L.who_holds("sid-C") is not None)
check("★ 正确 token 能放掉", L.release("sid-C", tok2))

print()
print("组 3  过期自动可抢（进程崩了不会永久卡死）")
tok3 = L.acquire("sid-D", who="崩掉的进程", ttl=1)
check("拿到短命锁", bool(tok3))
check("现在占着", L.who_holds("sid-D") is not None)
time.sleep(1.3)
check("★ 过期后查出来是没人占", L.who_holds("sid-D") is None)
check("★ 过期后别人能抢到", bool(L.acquire("sid-D", who="新来的")))
L.release("sid-D")

print()
print("组 4  续期（长任务用）")
tok4 = L.acquire("sid-E", who="长任务", ttl=2)
time.sleep(1)
check("★ 续期成功", L.renew("sid-E", tok4, ttl=10))
time.sleep(1.5)
check("★ 续期后还占着（不然这时候已经过期了）",
      L.who_holds("sid-E") is not None)
check("★ 拿错 token 续不了", not L.renew("sid-E", "假token"))
L.release("sid-E", tok4)

print()
print("组 5  ★ 真并发：50 个线程抢同一把锁，只能有一个赢")
winners = []
lock_g = threading.Lock()


def racer(i):
    t = L.acquire("sid-RACE", who="线程%d" % i)
    if t:
        with lock_g:
            winners.append((i, t))


ths = [threading.Thread(target=racer, args=(i,)) for i in range(50)]
t0 = time.time()
for t in ths:
    t.start()
for t in ths:
    t.join()
took = time.time() - t0

check("★ 50 个线程只有 1 个拿到锁", len(winners) == 1,
      "拿到的有 %d 个：%r" % (len(winners), [w[0] for w in winners]))
print("      （50 线程抢锁耗时 %.2f 秒）" % took)
if winners:
    L.release("sid-RACE", winners[0][1])
check("★ 抢完锁文件还是合法 JSON",
      isinstance(json.loads(io.open(L.LOCKS_PATH, encoding="utf-8-sig").read()), dict))

print()
print("组 6  ★ 真并发：20 个线程锁 20 个不同会话，全都该成功")
ok2 = []


def racer2(i):
    t = L.acquire("sid-M%02d" % i, who="线程%d" % i)
    if t:
        with lock_g:
            ok2.append(i)


ths = [threading.Thread(target=racer2, args=(i,)) for i in range(20)]
for t in ths:
    t.start()
for t in ths:
    t.join()
check("★ 20 个不同会话都锁上了（互不干扰）", len(ok2) == 20,
      "只成功 %d 个" % len(ok2))
check("★ locked_set 数得对", len(L.locked_set()) == 20,
      "locked_set 有 %d 个" % len(L.locked_set()))
for i in range(20):
    L.release("sid-M%02d" % i)
check("★ 全放掉后 locked_set 空了", len(L.locked_set()) == 0,
      "还剩 %r" % L.locked_set())

print()
print("组 7  ★ 跨进程：另起一个 python 进程抢同一把锁")
tok7 = L.acquire("sid-X", who="本进程")
check("本进程先拿到", bool(tok7))

# 子进程用同一个锁文件试着抢
code = (
    "import sys,os;"
    "sys.path.insert(0,%r);"
    "import session_lock as L;"
    "L.LOCKS_PATH=%r;"
    "L.GATE_PATH=%r;"
    "t=L.acquire('sid-X',who='子进程');"
    "print('GOT' if t else 'BUSY')"
) % (HERE, L.LOCKS_PATH, L.GATE_PATH)
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
out = (r.stdout or "").strip()
check("★ 子进程抢不到（跨进程互斥真的生效）", out == "BUSY",
      "子进程说 %r，stderr=%r" % (out, (r.stderr or "")[:200]))

L.release("sid-X", tok7)
r2 = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
check("★ 放掉之后子进程能抢到", (r2.stdout or "").strip() == "GOT",
      "子进程说 %r" % (r2.stdout or "").strip())
L.release("sid-X")

print()
print("组 8  上下文管理器")
with L.hold("sid-W", who="网页"):
    check("★ with 块里锁着", L.who_holds("sid-W") is not None)
    try:
        with L.hold("sid-W", who="飞书"):
            check("★ 拿不到时抛 Busy", False, "居然没抛异常")
    except L.Busy as e:
        check("★ 拿不到时抛 Busy", True)
        check("★ Busy 里带着占用者是谁", "网页" in str(e), "异常说：%s" % e)
check("★ 出了 with 块自动放掉", L.who_holds("sid-W") is None)

# 块里抛异常也得放掉，不然一次报错就永久卡死
try:
    with L.hold("sid-V", who="要炸的"):
        raise RuntimeError("假装干活时炸了")
except RuntimeError:
    pass
check("★ 块里抛异常也会放掉锁", L.who_holds("sid-V") is None)

print()
print("组 9  坏文件和残留大门锁不能让整个系统卡死")
io.open(L.LOCKS_PATH, "w", encoding="utf-8").write("{这不是JSON")
check("★ 锁文件坏了还能拿锁（当成空表）", bool(L.acquire("sid-Z", who="灾后")))
L.release("sid-Z")

# 模拟大门锁残留（进程崩了没删）
io.open(L.GATE_PATH, "w").write("99999")
old = time.time() - 60
os.utime(L.GATE_PATH, (old, old))     # 改成 60 秒前，超过 30 秒阈值
check("★ 残留的大门锁会被自动清掉", bool(L.acquire("sid-Y", who="清完之后")),
      "被残留的大门锁卡住了")
L.release("sid-Y")

for p in (L.LOCKS_PATH, L.GATE_PATH, L.LOCKS_PATH + ".tmp"):
    if os.path.exists(p):
        os.remove(p)

print()
print("组 10 ★ Windows 删除挂起：PermissionError 必须重试，不许立刻放弃")
# 这组是为一个真实 bug 补的回归测试。
# 原来 _gate_acquire 只把 FileExistsError 当成「已被占」去重试，
# 别的异常一律 return False。而 Windows 上 os.unlink 之后文件会进入
# 「删除挂起」状态，这期间别人 os.open 拿到的是 PermissionError(Errno 13)，
# 于是 17 毫秒就放弃 —— 机器一忙就随机丢任务，还谎报「会话被占着」。
# 靠并发跑是偶发的（16 进程压力下 10 轮挂 2 轮），所以这里用假的 os.open
# 稳定复现：前 3 次抛 PermissionError，第 4 次才成功。
_real_open = os.open
_calls = {"n": 0}


def fake_open(path, *a, **kw):
    if path == L.GATE_PATH and (a and (a[0] & os.O_EXCL)):
        _calls["n"] += 1
        if _calls["n"] <= 3:
            raise PermissionError(13, "Permission denied", path)
    return _real_open(path, *a, **kw)


os.open = fake_open
try:
    t0 = time.time()
    got = L._gate_acquire(timeout=5.0)
    took = time.time() - t0
finally:
    os.open = _real_open

check("★ 撞上 PermissionError 之后仍然拿到了大门锁", got,
      "17 毫秒就放弃的老 bug 回来了（耗时 %.3fs）" % took)
check("★ 确实重试了 3 次（不是第一次就成功）", _calls["n"] == 4,
      "os.open 被调了 %d 次" % _calls["n"])
check("★ 没有等到超时（说明是重试成功，不是碰巧）", took < 5.0,
      "耗时 %.3fs" % took)
if got:
    L._gate_release()

# 顺带确认区分失败原因的新接口没把 busy 和 gate 搞混
tokA, whyA = L.acquire_ex("sid-EX", who="甲")
check("★ acquire_ex 成功时不带原因", bool(tokA) and whyA is None)
tokB, whyB = L.acquire_ex("sid-EX", who="乙")
check("★ 真被占着报 busy（不是 gate）", (not tokB) and whyB == L.BUSY,
      "原因是 %r" % whyB)
L.release("sid-EX", tokA)
check("★ 空 sid 报 badsid", L.acquire_ex("")[1] == L.BADSID)

for p in (L.LOCKS_PATH, L.GATE_PATH, L.LOCKS_PATH + ".tmp"):
    if os.path.exists(p):
        os.remove(p)

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
