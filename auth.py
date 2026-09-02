# -*- coding: utf-8 -*-
"""身份白名单 + 临时提权 + 安全日志。

三件事都是「谁可以做什么」，所以放一个文件里。

白名单是默认拒绝的：bound_users 为空时谁都进不来，包括你。
第一次用要先绑定 —— 在电脑上跑 `python auth.py --code` 生成一个
6 位一次性码，在飞书里把这个码发给机器人，它就把你的 open_id 记下来。
码只有在电脑前的人看得到，所以别人拿不到绑定资格。

提权是内存里的，不落盘：进程一重启所有临时权限都没了。
这是故意的 —— 高权限不该活过一次崩溃。
"""

import io
import os
import re
import json
import time
import secrets
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
AUTH_PATH = os.path.join(HERE, "bridge_auth.json")
SEC_LOG = os.path.join(HERE, "security.log")

_LOCK = threading.RLock()

# 绑定码有效期（秒）。短一点，够你从电脑走到手机就行。
CODE_TTL = 600

# 提权最长有效期（秒）。到点自动掉回基础档。
MAX_GRANT_TTL = 1800

# 绑定码最多允许错几次。超了废码，防慢速爆破。
MAX_CODE_FAILS = 5


def _load():
    try:
        d = json.loads(io.open(AUTH_PATH, encoding="utf-8-sig").read())
    except Exception:
        d = {}
    d.setdefault("bound_users", {})
    d.setdefault("bound_chats", {})
    d.setdefault("code", None)
    return d


def _save(d):
    with io.open(AUTH_PATH, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=2))

# 日志里要抹掉的东西：长数字串（绑定码、手机号）、长 token 串。
# 审计日志的定位是「谁在什么时候动了什么」，不是「他说了什么」——
# 记原文就迟早把密钥记进去：拒绝日志带用户原文，而打错的绑定码正是原文。
_SCRUB = [
    (re.compile(r"\d{6,}"), "<数字串>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{24,}\b"), "<长串>"),
]


# 绑定码长这样：正好 6 个半角数字。写成正则而不是 isdigit()，原因见 try_bind。
_CODE_RE = re.compile(r"^[0-9]{6}$")


def scrub(s):
    """抹掉一段文字里像密钥的部分。给 sec_log 用，也可以单独调。"""
    s = str(s or "")
    for pat, rep in _SCRUB:
        s = pat.sub(rep, s)
    return s


def sec_log(event, **kw):
    """写安全日志。拒绝、绑定、提权都记一笔，方便事后查谁动过什么。

    只记 open_id 前 12 位 —— 够你认出是谁，又不至于把完整 id 摊在日志里。
    除 uid/chat 外的字段都过 scrub()，免得绑定码、token 顺着原文进日志。
    """
    parts = ["[%s]" % time.strftime("%Y-%m-%d %H:%M:%S"), event]
    for k, v in kw.items():
        s = str(v)
        if k in ("uid", "chat"):
            # id 本身就是长串，scrub 会整条抹掉，所以单独走截断
            s = s[:12] + "…" if len(s) > 12 else s
        else:
            s = scrub(s)
        parts.append("%s=%s" % (k, s))
    line = " ".join(parts)
    try:
        with io.open(SEC_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    return line


def new_code():
    """生成一次性绑定码。旧码直接作废，同一时间只有一个码有效。"""
    code = "%06d" % secrets.randbelow(1000000)
    with _LOCK:
        d = _load()
        d["code"] = {"value": code, "exp": int(time.time()) + CODE_TTL}
        _save(d)
    sec_log("生成绑定码", exp_in="%d秒" % CODE_TTL)
    return code


def try_bind(text, uid, chat_id):
    """看这条消息是不是绑定码。是且对得上就绑定，返回 (成功没, 说什么)。

    不是码就返回 (False, None) —— 调用方据此判断要不要继续走正常流程。
    """
    t = (text or "").strip()
    # 必须是半角 0-9。不能用 isdigit()：它对全角「０１２３４５」和
    # 阿拉伯文数字也返回 True，而 compare_digest 遇到非 ASCII 直接抛
    # TypeError —— 中文输入法一不小心打出全角，机器人就一声不响了。
    if not _CODE_RE.match(t):
        return (False, None)

    with _LOCK:
        d = _load()
        c = d.get("code") or {}
        if not c.get("value"):
            sec_log("绑定失败", 原因="没有待用的码", uid=uid)
            return (False, "现在没有可用的绑定码。在电脑上跑 python auth.py --code 生成一个。")
        if int(time.time()) > int(c.get("exp") or 0):
            d["code"] = None
            _save(d)
            sec_log("绑定失败", 原因="码过期", uid=uid)
            return (False, "这个码过期了，重新生成一个。")
        if not secrets.compare_digest(t, str(c["value"])):
            # 错够 MAX_CODE_FAILS 次直接废码。
            # 猜错有回显，等于给了攻击者一个预言机；6 位数只有 100 万种，
            # 不限次数的话慢速爆破是可行的。废码把这条路堵掉。
            c["fails"] = int(c.get("fails") or 0) + 1
            left = MAX_CODE_FAILS - c["fails"]
            if left <= 0:
                d["code"] = None
                _save(d)
                sec_log("绑定码作废", 原因="错误次数超限", uid=uid)
                return (False, "错太多次，这个码作废了。重新生成一个。")
            d["code"] = c
            _save(d)
            sec_log("绑定失败", 原因="码不对", uid=uid, 剩余次数=left)
            return (False, "码不对，还能试 %d 次。" % left)

        d["bound_users"][uid] = {"when": int(time.time())}
        if chat_id:
            d["bound_chats"][chat_id] = {"when": int(time.time())}
        d["code"] = None          # 用掉就废，不能重复用
        _save(d)
    sec_log("绑定成功", uid=uid, chat=chat_id)
    return (True, "绑定好了。这个账号和这个会话以后可以用了。\n发 /帮助 看能干什么。")

def check(uid, chat_id, cfg=None):
    """判这个人这个会话能不能用。返回 (行不行, 拒绝原因)。

    允许的来源有两处，取并集：
      - bound_users：飞书里用绑定码绑上来的
      - 配置里的 allowed_user_ids：手填的静态白名单
    两处都空 = 谁都不许用。这是默认拒绝，不是配置漏了。
    """
    cfg = cfg or {}
    d = _load()
    ok_users = set(d.get("bound_users") or {}) | set(cfg.get("allowed_user_ids") or [])
    ok_chats = set(d.get("bound_chats") or {}) | set(cfg.get("allowed_chat_ids") or [])

    if not ok_users:
        return (False, "未绑定")

    if uid not in ok_users:
        return (False, "用户不在白名单")

    # 群白名单：绑过至少一个群才启用这道判断，否则只认用户
    if ok_chats and chat_id and chat_id not in ok_chats:
        return (False, "会话不在白名单")

    return (True, "")


# ---- 临时提权。只在内存里，进程一重启就没了 ----
_GRANTS = {}
_PERM_RANK = {"chat": 0, "read": 1, "write": 2, "full": 3}


def grant(key, perm, ttl=600):
    """给某个任务临时提权。ttl 最多 MAX_GRANT_TTL，超了截断。"""
    perm = str(perm).lower()
    if perm not in _PERM_RANK:
        return None
    ttl = max(1, min(int(ttl), MAX_GRANT_TTL))
    exp = time.time() + ttl
    with _LOCK:
        _GRANTS[str(key)] = {"perm": perm, "exp": exp}
    sec_log("提权", key=key, perm=perm, ttl="%d秒" % ttl)
    return exp


def effective_perm(key, base="read"):
    """算实际生效的档位：有没过期的提权就用它，否则用基础档。

    只往上取不往下取 —— 提权记录比基础档低时按基础档走，
    免得一条过期残留把正常权限压下去。
    """
    base = str(base or "read").lower()
    if base not in _PERM_RANK:
        base = "read"
    with _LOCK:
        g = _GRANTS.get(str(key))
        if not g:
            return base
        if time.time() > g["exp"]:
            _GRANTS.pop(str(key), None)
            sec_log("提权到期", key=key)
            return base
        return g["perm"] if _PERM_RANK[g["perm"]] > _PERM_RANK[base] else base


def revoke(key):
    with _LOCK:
        if _GRANTS.pop(str(key), None):
            sec_log("撤销提权", key=key)
            return True
    return False


def grant_left(key):
    """还剩多少秒。没有提权返回 0，给飞书显示用。"""
    with _LOCK:
        g = _GRANTS.get(str(key))
        return max(0, int(g["exp"] - time.time())) if g else 0

def status_text():
    """当前绑定情况，给命令行和飞书 /状态 用。"""
    d = _load()
    us = d.get("bound_users") or {}
    cs = d.get("bound_chats") or {}
    c = d.get("code") or {}
    left = int((c.get("exp") or 0) - time.time()) if c.get("value") else 0
    rows = [
        "已绑定用户：%d 个" % len(us),
        "已绑定会话：%d 个" % len(cs),
        "待用绑定码：%s" % ("有，还剩 %d 秒" % left if left > 0 else "无"),
    ]
    for uid in us:
        rows.append("  用户 %s…" % uid[:12])
    for cid in cs:
        rows.append("  会话 %s…" % cid[:12])
    if not us:
        rows += ["", "⚠ 一个用户都没绑定，现在谁都用不了（这是默认拒绝，不是坏了）。",
                 "  跑 python auth.py --code 生成绑定码，在飞书里发给机器人。"]
    return "\n".join(rows)


if __name__ == "__main__":
    import argparse
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="飞书桥身份白名单管理")
    ap.add_argument("--code", action="store_true", help="生成一次性绑定码")
    ap.add_argument("--status", action="store_true", help="看当前绑定情况")
    ap.add_argument("--unbind", metavar="OPEN_ID", help="解绑某个用户")
    a = ap.parse_args()

    if a.code:
        code = new_code()
        print("=" * 46)
        print("绑定码：%s" % code)
        print("=" * 46)
        print("在飞书里把这 6 位数字发给机器人，%d 秒内有效。" % CODE_TTL)
        print("用一次就废。别转发给别人 —— 拿到码就能控制这台机器。")
    elif a.unbind:
        with _LOCK:
            d = _load()
            if d["bound_users"].pop(a.unbind, None):
                _save(d)
                sec_log("解绑", uid=a.unbind)
                print("解绑了 %s…" % a.unbind[:12])
            else:
                print("没这个用户。")
    else:
        print(status_text())

