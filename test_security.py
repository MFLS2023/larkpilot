# -*- coding: utf-8 -*-
"""安全边界测试：目录白名单 + 身份白名单 + 绑定码 + 临时提权 + 审计日志。

这两个文件（config.py / auth.py）是这轮改动的核心，之前一行测试都没有。
安全代码没测试等于没有 —— 它平时不出声，出声就是已经被绕过了。

重点验绕过手法，不是验正常路径：
  目录：前缀冒充（claude-files-evil）、`..` 穿越、软链接、黑名单片段
  身份：白名单空时默认拒绝、陌生人、码格式、爆破废码、码用一次就废
  日志：绑定码和 token 不能落盘

跑法：python test_security.py
不碰真实的 bridge_auth.json 和 security.log，全在临时目录里跑。
"""

import io
import os
import re
import sys
import json
import time
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C
import auth as A

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


def group(title):
    print("\n%s" % title)


# ---- 把 auth 的落盘位置挪到临时目录，别动真文件 ----
# 不能用系统默认临时目录：Windows 的 TEMP 在 AppData\Local\Temp 底下，
# 整条路径命中 appdata 黑名单，所有目录测试会全灭。挪到家目录下。
TMP = tempfile.mkdtemp(prefix="_fbsec_", dir=os.path.expanduser("~"))
A.AUTH_PATH = os.path.join(TMP, "auth.json")
A.SEC_LOG = os.path.join(TMP, "security.log")

# ---- 造一批真实存在的目录，check_dir 最后一道要判 isdir ----
ROOT = os.path.join(TMP, "allowed")
SUB = os.path.join(ROOT, "sub", "deep")
EVIL = os.path.join(TMP, "allowed-evil")        # 前缀冒充：字符串以 ROOT 开头
OUTSIDE = os.path.join(TMP, "outside")
for d in (ROOT, SUB, EVIL, OUTSIDE):
    os.makedirs(d, exist_ok=True)

CFG = {"allowed_dirs": [ROOT], "cwd": ROOT}


group("组 1  目录白名单：正常路径")

ok, info = C.check_dir(ROOT, CFG)
check("★ 白名单根目录本身可以用", ok, info)

ok, info = C.check_dir(SUB, CFG)
check("★ 白名单下的子目录可以用", ok, info)

ok, info = C.check_dir(os.path.join(ROOT, "sub", "..", "sub", "deep"), CFG)
check("★ 绕一圈还在里面的路径归一化后仍可用", ok, info)

ok, info = C.check_dir(ROOT.upper(), CFG)
check("★ 大小写不同也认（Windows 路径不分大小写）", ok, info)

ok, info = C.check_dir(ROOT + os.sep, CFG)
check("★ 尾部多个分隔符不影响判定", ok, info)


group("组 2  目录白名单：这些必须被拒")

ok, info = C.check_dir(EVIL, CFG)
check("★ 前缀冒充被拒（allowed-evil 不算 allowed 的子目录）",
      not ok, "居然放行了：%s" % info)

ok, info = C.check_dir(OUTSIDE, CFG)
check("★ 白名单外的目录被拒", not ok, "居然放行了：%s" % info)

ok, info = C.check_dir(os.path.join(ROOT, "..", "outside"), CFG)
check("★ `..` 穿越到外面被拒", not ok, "居然放行了：%s" % info)

ok, info = C.check_dir(os.path.join(ROOT, "..", ".."), CFG)
check("★ 一路 `..` 到上层被拒", not ok, "居然放行了：%s" % info)

ok, info = C.check_dir(os.path.join(ROOT, "notexist"), CFG)
check("★ 不存在的目录被拒（不能凭空当工作目录）", not ok, info)

for bad in ("", None):
    ok, info = C.check_dir(bad, CFG)
    check("★ 空目录参数被拒（%r）" % bad, not ok, info)


group("组 3  黑名单片段：哪怕在白名单里面也不许进")

HOME = os.path.expanduser("~")
for p, why in (
    (os.path.join(HOME, ".ssh"), "SSH 私钥"),
    (os.path.join(HOME, ".aws"), "云凭据"),
    (os.path.join(HOME, "AppData", "Roaming"), "浏览器和应用配置"),
    (os.path.join(HOME, ".gnupg"), "GPG 私钥"),
    (r"C:\Windows\System32", "系统目录"),
    (r"C:\ProgramData", "系统目录"),
):
    ok, info = C.check_dir(p, CFG)
    check("★ 拒绝 %s（%s）" % (os.path.basename(p.rstrip("\\/")) or p, why),
          not ok, "居然放行了：%s" % info)

# 关键：黑名单要能压过白名单，不能因为写进 allowed_dirs 就放行
sneaky = os.path.join(TMP, "allowed", "appdata_like")
os.makedirs(sneaky, exist_ok=True)
ok, info = C.check_dir(sneaky, {"allowed_dirs": [ROOT, sneaky], "cwd": ROOT})
check("★ 黑名单压过白名单（手填进 allowed_dirs 也没用）",
      not ok, "居然放行了：%s" % info)

# 黑名单是「路径里出现这个词就拒」，不是「只看最后一段」。
# 副作用：工作目录不能放在任何带这些词的父目录下面，比如系统临时目录。
# 这是保守方向，宁可误拒不可误放 —— 写成测试是为了以后有人踩到时知道是故意的。
tempish = os.path.join(TMP, "AppData", "work")
os.makedirs(tempish, exist_ok=True)
ok, info = C.check_dir(tempish, {"allowed_dirs": [TMP], "cwd": TMP})
check("★ 父目录带黑名单词，子目录也进不去（故意如此，非 bug）",
      not ok, "居然放行了：%s" % info)


group("组 4  safe_cwd：调用方永远拿到合法目录")

check("★ want 合法就用 want", C.safe_cwd(CFG, want=SUB) == C._norm(SUB),
      "拿到 %r" % C.safe_cwd(CFG, want=SUB))

check("★ want 不合法时退回配置里的 cwd",
      C.safe_cwd(CFG, want=OUTSIDE) == C._norm(ROOT),
      "拿到 %r" % C.safe_cwd(CFG, want=OUTSIDE))

check("★ want 是黑名单目录时也退回，不是照用",
      C.safe_cwd(CFG, want=os.path.join(HOME, ".ssh")) == C._norm(ROOT),
      "拿到 %r" % C.safe_cwd(CFG, want=os.path.join(HOME, ".ssh")))

bad_cfg = {"allowed_dirs": [ROOT], "cwd": OUTSIDE}
check("★ 配置里的 cwd 越界时退回白名单第一个可用目录",
      C.safe_cwd(bad_cfg) == C._norm(ROOT), "拿到 %r" % C.safe_cwd(bad_cfg))

none_cfg = {"allowed_dirs": [os.path.join(TMP, "nope1"), os.path.join(TMP, "nope2")],
            "cwd": os.path.join(TMP, "nope1")}
check("★ 白名单目录全不存在时返回空（引擎据此报错，不偷偷用当前目录）",
      not C.safe_cwd(none_cfg), "拿到 %r" % C.safe_cwd(none_cfg))


group("组 5  .env 解析：密钥从这里来，解错了就连不上")

ENVF = os.path.join(TMP, "t.env")
io.open(ENVF, "w", encoding="utf-8").write(u"\n".join([
    u"# 注释行，要跳过",
    u"",
    u"没有等号的行也要跳过",
    u"PLAIN=abc123",
    u'QUOTED="有引号的值"',
    u"SINGLE='单引号'",
    u"B64=YWJjZGVmZ2g=",          # base64 自带 =，只能切第一个等号
    u"SPACED  =  两边有空格  ",
    u"ALREADY_SET=文件里的值",
]))
os.environ.pop("PLAIN", None)
os.environ.pop("B64", None)
os.environ["ALREADY_SET"] = "环境里的值"

check("★ .env 读成功返回 True", C.load_env(ENVF) is True)
check("★ 普通键值读对了", os.environ.get("PLAIN") == "abc123",
      "%r" % os.environ.get("PLAIN"))
check("★ 双引号被剥掉", os.environ.get("QUOTED") == "有引号的值",
      "%r" % os.environ.get("QUOTED"))
check("★ 单引号被剥掉", os.environ.get("SINGLE") == "单引号",
      "%r" % os.environ.get("SINGLE"))
check("★ base64 值里的 = 没被切断（只切第一个等号）",
      os.environ.get("B64") == "YWJjZGVmZ2g=", "%r" % os.environ.get("B64"))
check("★ 键和值两边的空格被去掉", os.environ.get("SPACED") == "两边有空格",
      "%r" % os.environ.get("SPACED"))
check("★ 已存在的环境变量不被文件覆盖（命令行能压过文件）",
      os.environ.get("ALREADY_SET") == "环境里的值",
      "%r" % os.environ.get("ALREADY_SET"))
check("★ 文件不存在返回 False 而不是抛异常",
      C.load_env(os.path.join(TMP, "没有这个文件.env")) is False)

for k in ("PLAIN", "QUOTED", "SINGLE", "B64", "SPACED", "ALREADY_SET"):
    os.environ.pop(k, None)


group("组 6  脱敏与密钥来源")

check("★ 长密钥只留前 4 位", C.redact("AgyKsecretsecret").startswith("Agy")
      and "secret" not in C.redact("AgyKsecretsecret"),
      C.redact("AgyKsecretsecret"))
check("★ 短串整条打掉", set(C.redact("abc")) == {"*"}, C.redact("abc"))
check("★ 空值不报错", C.redact("") == "（空）", C.redact(""))
check("★ 脱敏结果长度有上限（不泄漏原文长度）",
      len(C.redact("x" * 500)) < 60, len(C.redact("x" * 500)))


group("组 7  load_config：默认值和越界回退")

_real_cfg_path = C.CONFIG_PATH
CFGF = os.path.join(TMP, "cfg.json")
C.CONFIG_PATH = CFGF


def write_cfg(d):
    io.open(CFGF, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False))


write_cfg({"cwd": ROOT})
c = C.load_config()
check("★ json 里没写 allowed_dirs 时自动补默认值（不是空列表）",
      bool(c.get("allowed_dirs")) and c["allowed_dirs"] == list(C.DEFAULT_ALLOWED_DIRS),
      "%r" % c.get("allowed_dirs"))

write_cfg({"allowed_dirs": [], "cwd": ROOT})
c = C.load_config()
check("★ allowed_dirs 写成空列表也补默认值（空=谁都能进，不能允许）",
      bool(c.get("allowed_dirs")) and c["allowed_dirs"] == list(C.DEFAULT_ALLOWED_DIRS),
      "%r" % c.get("allowed_dirs"))

write_cfg({"permission": "随便瞎写"})
check("★ 档位写不认识的值退回 read（不是退回 full）",
      C.load_config()["permission"] == "read", C.load_config()["permission"])

for p in ("read", "write", "full", "chat", "auto"):
    write_cfg({"permission": p})
    check("★ 档位 %s 被原样保留" % p, C.load_config()["permission"] == p,
          C.load_config()["permission"])

io.open(CFGF, "w", encoding="utf-8").write(u"{ 这不是合法 json ")
seen = []
c = C.load_config(on_error=lambda e: seen.append(e))
check("★ json 坏了会调 on_error（不静默降级）", len(seen) == 1, "调了 %d 次" % len(seen))
check("★ json 坏了仍然拿到一份可用配置", bool(c.get("allowed_dirs")), "%r" % c)
check("★ json 坏了时档位是保守的 read", c["permission"] == "read", c["permission"])

os.environ["FEISHU_APP_SECRET"] = "环境里的密钥"
write_cfg({"app_secret": "json里的旧密钥"})
check("★ 环境变量压过 json 里的同名密钥",
      C.load_config()["app_secret"] == "环境里的密钥",
      C.load_config()["app_secret"])
check("★ 密钥来源认出是环境变量", C.secret_source() in ("env", "dotenv"),
      C.secret_source())
os.environ.pop("FEISHU_APP_SECRET", None)
check("★ 密钥只躺在 json 里时来源标记为不安全",
      "不安全" in C.secret_source(), C.secret_source())
write_cfg({})
check("★ 哪儿都没有密钥时来源是缺失", C.secret_source() == "缺失", C.secret_source())

C.CONFIG_PATH = _real_cfg_path


# ---- auth 的状态每次直接写盘，_load() 不缓存所以改了立刻生效 ----
def set_auth(users=None, chats=None, code=None):
    io.open(A.AUTH_PATH, "w", encoding="utf-8").write(json.dumps({
        "bound_users": {u: {"when": 1} for u in (users or [])},
        "bound_chats": {c: {"when": 1} for c in (chats or [])},
        "code": code,
    }, ensure_ascii=False))


group("组 8  身份白名单：默认拒绝")

set_auth()
ok, why = A.check("ou_anyone", "oc_anychat", {})
check("★ 谁都没绑定时任何人都被拒（默认拒绝，不是默认放行）",
      not ok and why == "未绑定", "ok=%s why=%r" % (ok, why))

ok, why = A.check("ou_me", "oc_x", {})
check("★ 白名单空时连自己也进不来（这是对的，先绑定）", not ok, why)

set_auth(users=["ou_me"])
ok, why = A.check("ou_me", "oc_x", {})
check("★ 绑过的人可以用", ok, why)

ok, why = A.check("ou_someone_else", "oc_x", {})
check("★ 没绑过的陌生人被拒", not ok and why == "用户不在白名单",
      "ok=%s why=%r" % (ok, why))

set_auth()
ok, why = A.check("ou_static", "oc_x", {"allowed_user_ids": ["ou_static"]})
check("★ 配置里手填的白名单也算（不用绑定码也能进）", ok, why)

ok, why = A.check("ou_static", "oc_x", {"allowed_user_ids": ["ou_static"],
                                        "allowed_chat_ids": ["oc_only"]})
check("★ 群白名单生效时，人对群不对也被拒",
      not ok and why == "会话不在白名单", "ok=%s why=%r" % (ok, why))

ok, why = A.check("ou_static", "oc_only", {"allowed_user_ids": ["ou_static"],
                                           "allowed_chat_ids": ["oc_only"]})
check("★ 人和群都对才放行", ok, why)

set_auth(users=["ou_me"])
ok, why = A.check("ou_me", "oc_任意群", {})
check("★ 一个群都没绑时不判群（只认人，免得换群就用不了）", ok, why)

set_auth(users=["ou_me"], chats=["oc_bound"])
ok, why = A.check("ou_me", "oc_other", {})
check("★ 绑过群之后就开始判群", not ok and why == "会话不在白名单",
      "ok=%s why=%r" % (ok, why))


group("组 9  绑定码：一次性、限次、会过期")

NOT_CODES = ["你好", "12345", "1234567", "12a456", "", "  ", "/帮助",
             "０１２３４５",      # 全角数字：中文输入法很容易打出来
             "١٢٣٤٥٦",           # 阿拉伯文数字
             "１２3456",          # 半角全角混着
             "12 3456", "12.3456", "-123456", "123456 7"]

# ★ 必须在「有码待用」的状态下测，不能只测没码的状态。
# 没码时第一道判断就返回了，走不到 compare_digest —— 这个 bug 最早
# 就是因为测的时候没有待用的码，才漏过去的：isdigit() 认全角，
# compare_digest 遇到非 ASCII 抛 TypeError，桥那头表现为完全不回话。
for state, label in ((None, "没码待用"),
                     ({"value": "111111", "exp": int(time.time()) + 600}, "有码待用")):
    for t in NOT_CODES:
        set_auth(code=state)
        try:
            ok, msg = A.try_bind(t, "ou_x", "oc_x")
            crashed = None
        except Exception as e:
            ok, msg, crashed = None, None, "%s: %s" % (type(e).__name__, e)
        check("★ [%s] %r 不当成绑定码" % (label, t),
              crashed is None and ok is False and msg is None,
              crashed or ("ok=%s msg=%r" % (ok, msg)))

# 非码消息不能消耗猜错次数，否则输入法打错几次就把有效码废了
set_auth(code={"value": "111111", "exp": int(time.time()) + 600})
for t in NOT_CODES:
    A.try_bind(t, "ou_x", "oc_x")
d = json.loads(io.open(A.AUTH_PATH, encoding="utf-8").read())
check("★ 发一堆非码消息不消耗猜错次数（不然能被拖着废码）",
      not (d.get("code") or {}).get("fails"),
      "fails=%r" % (d.get("code") or {}).get("fails"))
check("★ 发一堆非码消息之后码还在", (d.get("code") or {}).get("value") == "111111",
      "%r" % (d.get("code") or {}))

set_auth(code=None)
ok, msg = A.try_bind("123456", "ou_x", "oc_x")
check("★ 没生成码时报「没有可用的码」而不是放行",
      not ok and msg and "没有可用" in msg, "%r" % msg)

set_auth(code={"value": "111111", "exp": int(time.time()) + 600})
ok, msg = A.try_bind("222222", "ou_x", "oc_x")
check("★ 码不对不放行，并告知剩余次数",
      not ok and msg and "还能试" in msg, "%r" % msg)

d = json.loads(io.open(A.AUTH_PATH, encoding="utf-8").read())
check("★ 猜错一次被记下来了", (d.get("code") or {}).get("fails") == 1,
      "%r" % (d.get("code") or {}).get("fails"))

for i in range(A.MAX_CODE_FAILS - 2):
    A.try_bind("222222", "ou_x", "oc_x")
ok, msg = A.try_bind("222222", "ou_x", "oc_x")
check("★ 错满 %d 次后码直接作废（堵慢速爆破）" % A.MAX_CODE_FAILS,
      not ok and msg and "作废" in msg, "%r" % msg)
d = json.loads(io.open(A.AUTH_PATH, encoding="utf-8").read())
check("★ 作废后码从文件里清掉了", d.get("code") in (None, {}), "%r" % d.get("code"))

ok, msg = A.try_bind("111111", "ou_x", "oc_x")
check("★ 码作废后连正确的码也不能用了", not ok, "%r" % msg)

set_auth(code={"value": "333333", "exp": int(time.time()) - 1})
ok, msg = A.try_bind("333333", "ou_x", "oc_x")
check("★ 过期的码不放行", not ok and msg and "过期" in msg, "%r" % msg)

set_auth(code={"value": "444444", "exp": int(time.time()) + 600})
ok, msg = A.try_bind("444444", "ou_new", "oc_new")
check("★ 正确的码绑定成功", ok, "%r" % msg)
d = json.loads(io.open(A.AUTH_PATH, encoding="utf-8").read())
check("★ 绑定后用户进了 bound_users", "ou_new" in (d.get("bound_users") or {}),
      "%r" % list(d.get("bound_users") or {}))
check("★ 绑定后群也进了 bound_chats", "oc_new" in (d.get("bound_chats") or {}),
      "%r" % list(d.get("bound_chats") or {}))
check("★ 码用掉就废（不能第二个人拿同一个码再绑）",
      d.get("code") in (None, {}), "%r" % d.get("code"))

ok, msg = A.try_bind("444444", "ou_second_person", "oc_new")
check("★ 同一个码第二次用不了", not ok, "%r" % msg)
ok, why = A.check("ou_second_person", "oc_new", {})
check("★ 没绑成的人确实进不来", not ok, why)

code = A.new_code()
check("★ new_code 生成 6 位数字", len(code) == 6 and code.isdigit(), code)
ok, msg = A.try_bind(code, "ou_gen", "oc_gen")
check("★ new_code 生成的码能绑上", ok, "%r" % msg)

c1 = A.new_code()
c2 = A.new_code()
check("★ 生成新码作废旧码（同时只有一个有效）", c1 != c2)
ok, _ = A.try_bind(c1, "ou_old", "oc_x")
check("★ 旧码生成新码后失效", not ok)
ok, _ = A.try_bind(c2, "ou_new2", "oc_x")
check("★ 新码有效", ok)


group("组 10  临时提权：只往上取、会到期、不落盘")

A.revoke("k1")
check("★ 没提权时就是基础档", A.effective_perm("k1", "read") == "read",
      A.effective_perm("k1", "read"))
check("★ 没提权时剩余时间是 0", A.grant_left("k1") == 0, A.grant_left("k1"))

A.grant("k1", "full", 300)
check("★ 提权后生效", A.effective_perm("k1", "read") == "full",
      A.effective_perm("k1", "read"))
check("★ 剩余时间算得出来", 290 < A.grant_left("k1") <= 300, A.grant_left("k1"))

check("★ 提权只影响自己那个 key", A.effective_perm("k2", "read") == "read",
      A.effective_perm("k2", "read"))

A.grant("k3", "read", 300)
check("★ 提权档位比基础档低时按基础档走（残留记录压不下正常权限）",
      A.effective_perm("k3", "full") == "full", A.effective_perm("k3", "full"))

check("★ 不认识的档位提权失败", A.grant("k4", "超级管理员", 300) is None)
check("★ 提权失败后档位没变", A.effective_perm("k4", "read") == "read")

exp = A.grant("k5", "full", 999999)
check("★ ttl 超上限被截断到 %d 秒" % A.MAX_GRANT_TTL,
      exp is not None and exp - time.time() <= A.MAX_GRANT_TTL + 1,
      "还剩 %s 秒" % A.grant_left("k5"))

A.grant("k6", "full", 1)
time.sleep(1.2)
check("★ 到期后自动退回基础档", A.effective_perm("k6", "read") == "read",
      A.effective_perm("k6", "read"))
check("★ 到期的提权从内存里清掉了", A.grant_left("k6") == 0, A.grant_left("k6"))

A.grant("k7", "full", 300)
check("★ revoke 能撤掉", A.revoke("k7") is True)
check("★ 撤掉之后回到基础档", A.effective_perm("k7", "read") == "read")
check("★ 重复 revoke 返回 False", A.revoke("k7") is False)

A.grant("k8", "full", 300)
disk = io.open(A.AUTH_PATH, encoding="utf-8").read()
check("★ 提权记录不落盘（进程重启就没了，高权限不该活过崩溃）",
      "k8" not in disk and "full" not in disk, "文件里出现了提权痕迹")

check("★ 基础档写错也不会变成高权限",
      A.effective_perm("nokey", "瞎写的档位") == "read",
      A.effective_perm("nokey", "瞎写的档位"))


group("组 11  审计日志：记得下谁动了什么，但不许记密钥")

check("★ 6 位绑定码被抹掉", "123456" not in A.scrub("我的码是 123456"),
      A.scrub("我的码是 123456"))
check("★ 长数字串被抹掉", "13800138000" not in A.scrub("手机号 13800138000"),
      A.scrub("手机号 13800138000"))
check("★ 长 token 串被抹掉",
      "AgyKabcdefghijklmnopqrstuvwxyz" not in A.scrub("secret=AgyKabcdefghijklmnopqrstuvwxyz"),
      A.scrub("secret=AgyKabcdefghijklmnopqrstuvwxyz"))
check("★ 正常中文原样保留（日志还得能看懂）",
      "把首页导航改一下" in A.scrub("把首页导航改一下"),
      A.scrub("把首页导航改一下"))
check("★ 短数字不受影响（第 3 页这种）", "3" in A.scrub("看第 3 页"),
      A.scrub("看第 3 页"))

line = A.sec_log("拒绝", uid="ou_" + "x" * 40, chat="oc_" + "y" * 40,
                 原因="用户不在白名单", 内容="我的绑定码是 998877")
check("★ 日志里 uid 只留前 12 位", ("ou_" + "x" * 40) not in line, line)
check("★ 日志里 chat 只留前 12 位", ("oc_" + "y" * 40) not in line, line)
check("★ 日志里认得出是谁（前 12 位还在）", "ou_xxxxxxxxx" in line, line)
check("★ 拒绝原因照记（这才是审计要看的）", "用户不在白名单" in line, line)
check("★ 用户原文里的绑定码没进日志", "998877" not in line, line)

A.sec_log("绑定失败", 原因="码不对", uid="ou_test", 剩余次数=3)
disk_log = io.open(A.SEC_LOG, encoding="utf-8").read()
check("★ 日志真写进文件了", "绑定失败" in disk_log)
check("★ 落盘的日志里也没有绑定码", "998877" not in disk_log)

code_now = A.new_code()
A.try_bind(code_now, "ou_logtest", "oc_logtest")
disk_log = io.open(A.SEC_LOG, encoding="utf-8").read()
check("★ 绑定成功的日志里不含那个码本身", code_now not in disk_log,
      "码 %s 出现在日志里" % code_now)

bad = A.new_code()
wrong = "%06d" % ((int(bad) + 1) % 1000000)
A.try_bind(wrong, "ou_logtest2", "oc_logtest2")
disk_log = io.open(A.SEC_LOG, encoding="utf-8").read()
check("★ 猜错的码也不进日志", wrong not in disk_log, "%s 出现在日志里" % wrong)


# ---- 收摊 ----
shutil.rmtree(TMP, ignore_errors=True)

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
