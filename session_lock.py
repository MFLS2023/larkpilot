# -*- coding: utf-8 -*-
"""会话锁 —— 防止飞书和网页同时操作同一个会话。

为什么要这个：两条通道各自起 `claude -p -r <同一个session_id>` 进程，
两个进程同时往一个会话文件里写，谁后写完谁覆盖谁，上下文就乱了。
乱了之后没法回滚，因为 JSONL 是追加式的、错的那几行就在里面。

## 怎么用

    import session_lock as L

    tok = L.acquire(sid, who="飞书")
    if not tok:
        info = L.who_holds(sid)
        # 告诉用户「这个会话正被 %s 占着，%s 开始的」
        return
    try:
        ...干活...
    finally:
        L.release(sid, tok)

或者用上下文管理器（拿不到锁就抛 Busy）：

    try:
        with L.hold(sid, who="网页"):
            ...干活...
    except L.Busy as e:
        ...

## 为什么锁写在文件里

飞书桥和网页是**两个独立的 Python 进程**，内存里的锁互相看不见。
文件是它们唯一的共同介质。同机同盘，不涉及网络文件系统的一致性问题。

## 抢锁怎么做到不打架

Windows 上没有 fcntl，所以用「创建独占文件」当互斥量：
os.open(..., O_CREAT|O_EXCL) 这个组合是原子的 —— 两个进程同时调，
只有一个能成，另一个抛 FileExistsError。这是跨平台都成立的做法。

拿到这把「大门锁」之后再改 session_locks.json，改完立刻放掉大门锁。
所以大门锁只在几毫秒内被持有，不影响并发。
"""

import io
import json
import os
import random
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
LOCKS_PATH = os.path.join(HERE, "session_locks.json")
GATE_PATH = os.path.join(HERE, ".locks.gate")     # 改 json 时用的互斥量

# 锁多久算过期。跟任务超时（bridge_config.json 的 timeout_seconds，默认 1800）
# 对齐再加点余量 —— 万一进程崩了没释放，过期后自动可抢，不会永久卡死。
DEFAULT_TTL = 2400        # 40 分钟

# 抢大门锁最多等多久。改个 json 本来是几毫秒的事，但机器忙的时候
# 线程可能几百毫秒都轮不到 CPU —— 原来定 3 秒，实测 8 个进程压着 CPU 时
# 20 个线程锁 20 个不同会话会有 1 个抢不到（test_lock 组 6）。
# 放宽到 10 秒不会造成永久卡死：大门锁残留超过 30 秒会被自动清掉（见 _gate_acquire）。
GATE_TIMEOUT = 10.0


class Busy(Exception):
    """会话正被别人占着。异常带上占用者信息，好告诉用户是谁在用。"""

    def __init__(self, sid, holder):
        self.sid = sid
        self.holder = holder or {}
        who = self.holder.get("who") or "另一个程序"
        super(Busy, self).__init__("会话 %s 正被「%s」占着" % (sid[:8], who))


def _gate_acquire(timeout=GATE_TIMEOUT):
    """抢大门锁。拿到返回 True。

    O_CREAT|O_EXCL 是原子操作：文件已存在就抛 FileExistsError，
    所以同时调的两个进程只有一个能建出来。

    PermissionError 也算「已被占」。这是 Windows 特有的坑：os.unlink 之后
    文件会进入「删除挂起」，这期间别人 os.open 拿到的是拒绝访问（Errno 13），
    不是 FileExistsError。以前它掉进最后那个 except 里直接 return False ——
    才 17 毫秒就放弃，根本没等。表现是机器一忙就随机丢任务，
    而且报的原因是「会话被占着」（假话，其实没人占）。
    实测 24 线程各抢 60 轮能触发 3 次。
    """
    deadline = time.time() + timeout
    tries = 0
    while True:
        try:
            fd = os.open(GATE_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except (FileExistsError, PermissionError):
            # 大门锁本身也可能因为进程崩了而残留。它只该被持有几毫秒，
            # 超过 30 秒必然是残留，直接清掉 —— 不清就永久卡死。
            try:
                if time.time() - os.path.getmtime(GATE_PATH) > 30:
                    os.unlink(GATE_PATH)
                    continue
            except Exception:
                pass
            if time.time() > deadline:
                return False
            # 必须带随机抖动。原来是死等 0.02 秒 —— 20 个线程同时撞上之后
            # 全睡一样长，于是同时醒、又同时撞，永远保持同步，倒霉的那个
            # 会一直倒霉直到超时。加了抖动就会自然错开。
            # 也逐步退让：撞得越久睡得越长，别忙着轮询把 CPU 占死。
            tries += 1
            back = min(0.02 * tries, 0.15)
            time.sleep(back * (0.5 + random.random()))
        except Exception:
            return False


def _gate_release():
    try:
        os.unlink(GATE_PATH)
    except Exception:
        pass


def _read():
    """读锁表。读坏了当成空表 —— 宁可少锁一次，也不能整个挂掉。"""
    try:
        d = json.loads(io.open(LOCKS_PATH, encoding="utf-8-sig").read())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write(d):
    """写锁表。先写临时文件再改名，避免写一半断电留下坏 json。"""
    tmp = LOCKS_PATH + ".tmp"
    try:
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False, indent=2))
        os.replace(tmp, LOCKS_PATH)     # 同盘 replace 是原子的
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


def _alive(rec, now=None):
    """这条锁记录还有效吗（没过期）。"""
    if not isinstance(rec, dict):
        return False
    now = now or time.time()
    try:
        return float(rec.get("expires_at") or 0) > now
    except Exception:
        return False


def acquire(sid, who="?", ttl=DEFAULT_TTL, note=""):
    """给会话上锁。成功返回一个 token（释放时要用），失败返回 None。

    token 的作用：防止 A 拿的锁被 B 释放掉。释放时对不上就不放。
    要区分失败原因（是真被占着，还是内部挤不进去）用 acquire_ex。
    """
    return acquire_ex(sid, who=who, ttl=ttl, note=note)[0]


# acquire_ex 的失败原因。给调用方拿去说人话用的。
BUSY = "busy"          # 会话真被别人占着
GATE = "gate"          # 大门锁挤不进去，跟这个会话有没有人用无关
BADSID = "badsid"      # 没给 sid
WRITE = "write"        # 锁表写不进去（磁盘满、权限）


def acquire_ex(sid, who="?", ttl=DEFAULT_TTL, note=""):
    """跟 acquire 一样，但返回 (token, 失败原因)。成功时原因是 None。

    为什么要分开：原来抢不到大门锁和会话被占着都返回 None，桥那边
    一律回「可能刚被抢走了」—— 机器忙的时候明明没人占也这么说，
    用户以为撞车了，实际是任务被静默丢掉。两种情况的处置也不同：
    真被占着该等人家做完，挤不进去该重试。
    """
    if not sid:
        return (None, BADSID)
    if not _gate_acquire():
        # 挤不进大门锁。宁可不锁也不冒险并发写坏锁表 —— 但要如实告诉调用方
        # 原因是这个，不是「被占着」。
        return (None, GATE)
    try:
        now = time.time()
        d = _read()
        rec = d.get(sid)
        if _alive(rec, now):
            return (None, BUSY)          # 已经被别人占着
        tok = uuid.uuid4().hex
        d[sid] = {
            "token": tok,
            "who": who,
            "note": note[:200],
            "pid": os.getpid(),
            "since": now,
            "expires_at": now + float(ttl),
        }
        # 顺手清掉过期的记录，不然这个文件会一直长
        for k in [k for k, v in d.items() if k != sid and not _alive(v, now)]:
            d.pop(k, None)
        if not _write(d):
            return (None, WRITE)
        return (tok, None)
    finally:
        _gate_release()


def release(sid, token=None):
    """放锁。token 对不上就不放（防止误放别人的锁）。

    返回 True 表示真放掉了。
    """
    if not sid:
        return False
    if not _gate_acquire():
        return False
    try:
        d = _read()
        rec = d.get(sid)
        if not isinstance(rec, dict):
            return False
        if token and rec.get("token") != token:
            return False                 # 不是你的锁
        d.pop(sid, None)
        _write(d)
        return True
    finally:
        _gate_release()


def renew(sid, token, ttl=DEFAULT_TTL):
    """续期。跑很久的任务用得上，免得干着活锁就过期了被别人抢走。"""
    if not _gate_acquire():
        return False
    try:
        d = _read()
        rec = d.get(sid)
        if not isinstance(rec, dict) or rec.get("token") != token:
            return False
        rec["expires_at"] = time.time() + float(ttl)
        _write(d)
        return True
    finally:
        _gate_release()


def who_holds(sid):
    """谁占着这个会话。没人占返回 None。

    返回的字典里 token 被去掉了 —— 那是凭据，不该给外面看。
    """
    rec = _read().get(sid)
    if not _alive(rec):
        return None
    out = dict(rec)
    out.pop("token", None)
    out["held_for"] = int(time.time() - float(rec.get("since") or 0))
    return out


def locked_set():
    """当前被占着的所有 session_id。列表页拿它标 ⏳。"""
    now = time.time()
    return {k for k, v in _read().items() if _alive(v, now)}


class hold(object):
    """上下文管理器版。拿不到锁抛 Busy。

        with hold(sid, who="网页"):
            ...
    """

    def __init__(self, sid, who="?", ttl=DEFAULT_TTL, note=""):
        self.sid, self.who, self.ttl, self.note = sid, who, ttl, note
        self.token = None

    def __enter__(self):
        self.token, why = acquire_ex(self.sid, self.who, self.ttl, self.note)
        if not self.token:
            # 挤不进大门锁时 who_holds 是 None，Busy 会说成「另一个程序占着」——
            # 那是假话（根本没人占）。这种情况给个说明白的占用者。
            holder = who_holds(self.sid)
            if not holder:
                holder = {"who": {GATE: "锁表太忙（没人占用，可重试）",
                                  WRITE: "锁表写不进去"}.get(why, "未知原因")}
            raise Busy(self.sid, holder)
        return self

    def __exit__(self, *a):
        release(self.sid, self.token)
        return False


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "ls":
        held = _read()
        now = time.time()
        alive = {k: v for k, v in held.items() if _alive(v, now)}
        if not alive:
            print("当前没有会话被占用")
        else:
            print("被占用的会话：")
            for k, v in alive.items():
                print("  %s  by %s  已 %d 秒  还剩 %d 秒过期"
                      % (k[:8], v.get("who"), now - float(v.get("since") or 0),
                         float(v.get("expires_at") or 0) - now))
    elif len(sys.argv) > 2 and sys.argv[1] == "unlock":
        print("强制解锁 %s：%s" % (sys.argv[2][:8],
                                "成功" if release(sys.argv[2]) else "本来就没锁"))
    else:
        print("用法：python session_lock.py ls          看谁被占着")
        print("      python session_lock.py unlock <sid>  强制解锁")
