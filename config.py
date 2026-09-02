# -*- coding: utf-8 -*-
"""统一配置入口：密钥只从环境变量读，工作目录在这里判白名单。

为什么单独拆出来：以前 bridge.py 和 web_server.py 各自读一次
bridge_config.json，密钥明文躺在里面。两边都改走这里之后，
密钥只有一个来源，配置文件里只留非敏感字段。

密钥查找顺序（前面找到就不再看后面）：
  1. 进程环境变量          FEISHU_APP_SECRET
  2. 同目录 .env 文件      FEISHU_APP_SECRET=xxx
  3. bridge_config.json    app_secret 字段 —— 仅兼容旧配置，会告警

第 3 条是迁移过渡用的。迁完就该从 json 里删掉，删了之后
secret_source() 会返回 "env" 或 "dotenv"，不再返回 "json(不安全)"。
"""

import io
import os
import json

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "bridge_config.json")
ENV_PATH = os.path.join(HERE, ".env")
HOME = os.path.expanduser("~")

# 环境变量名 -> 配置字段名
SECRET_KEYS = {
    "FEISHU_APP_ID": "app_id",
    "FEISHU_APP_SECRET": "app_secret",
}

# 默认允许干活的目录。只有这些及其子目录能当工作目录。
# 想加目录改 bridge_config.json 的 allowed_dirs，不要改这里。
# 刻意只留一个通用目录：源代码要开源，默认值里不能出现任何个人路径
# （2026-08-25 review 发现之前硬编码了私人目录）。自己的目录写本地配置里。
DEFAULT_ALLOWED_DIRS = [
    os.path.join(HOME, ".claude", "claude-files"),
]

# 黑名单目录名：路径里出现这些片段就拒绝，哪怕它落在白名单里面。
# 凭据、浏览器配置、系统目录都在这些底下。
DENY_PARTS = [
    ".ssh", ".aws", ".gnupg", ".azure", ".kube", ".docker",
    "appdata", "programdata", "windows", "system32",
    ".git\\config", "credentials", "keychain",
]

# 敏感文件名：读到这些不给进（目录白名单之外的第二道）
DENY_FILE_HINTS = [".pem", ".key", ".pfx", "id_rsa", ".env", "password"]

def load_env(path=ENV_PATH):
    """把 .env 读进 os.environ。

    已存在的环境变量不覆盖 —— 想临时换个值，命令行 set 一下就能压过文件。
    切等号只切第一个：base64 密钥自己带 =，切多了值就断了。
    """
    if not os.path.exists(path):
        return False
    try:
        for raw in io.open(path, encoding="utf-8-sig"):
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            if k and k not in os.environ:
                os.environ[k] = v
        return True
    except Exception:
        return False


# 导入即生效。bridge.py / web_server.py 只要 import config 就拿得到密钥。
_DOTENV_LOADED = load_env()


def redact(s, keep=4):
    """脱敏。日志、自检输出、报错信息里的密钥都得过这一道。"""
    s = str(s or "")
    if not s:
        return "（空）"
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * min(len(s) - keep, 12)


def secret_source():
    """密钥是从哪儿来的。自检要显示这个，明文残留在 json 里必须看得见。"""
    if os.environ.get("FEISHU_APP_SECRET"):
        return "dotenv" if _DOTENV_LOADED else "env"
    try:
        raw = json.loads(io.open(CONFIG_PATH, encoding="utf-8-sig").read())
        if raw.get("app_secret"):
            return "json(不安全)"
    except Exception:
        pass
    return "缺失"

# 档位白名单。auto 是 claude 自己的 --permission-mode auto，
# 少了它 bridge_config.json 里写 auto 会被悄悄降成 read。
PERM_VALUES = ("read", "write", "full", "chat", "auto")


def load_config(on_error=None):
    """读配置并叠上环境变量里的密钥。每次读盘不缓存，改了下一条消息就生效。

    默认值取保守的一档（read + confirm_dangerous）—— 这只在
    bridge_config.json 读不出来时才用得上。正常情况下档位由配置文件决定，
    真正的安全边界是身份白名单（auth.py）和目录白名单（check_dir）。

    on_error: 配置文件读不出来时拿到异常。默认静默 ——
    但调用方应该传个记日志的进来，否则 json 写错一个逗号会全静默降级。
    """
    cfg = {
        "app_id": "", "app_secret": "",
        "cwd": DEFAULT_ALLOWED_DIRS[0],
        "permission": "read",
        "confirm_dangerous": True,
        "timeout_seconds": 1800,
        "allowed_user_ids": [], "allowed_chat_ids": [],
        "allowed_dirs": list(DEFAULT_ALLOWED_DIRS),
        "reply_max_chars": 3000,
        "truncate_reply": False,
    }
    try:
        cfg.update(json.loads(io.open(CONFIG_PATH, encoding="utf-8-sig").read()))
    except Exception as e:
        if on_error:
            try:
                on_error(e)
            except Exception:
                pass

    # 环境变量优先级最高，压过 json 里的同名字段
    for envk, cfgk in SECRET_KEYS.items():
        v = os.environ.get(envk)
        if v:
            cfg[cfgk] = v

    if not cfg.get("allowed_dirs"):
        cfg["allowed_dirs"] = list(DEFAULT_ALLOWED_DIRS)
    else:
        # 支持 ~/ 开头的写法：配置文件可移植，换机器不用改绝对路径
        cfg["allowed_dirs"] = [os.path.expanduser(str(d))
                               for d in cfg["allowed_dirs"]]
    cfg["cwd"] = os.path.expanduser(str(cfg.get("cwd") or ""))

    # 配置里写的档位不认就退回只读，不要退回 full
    if str(cfg.get("permission") or "").lower() not in PERM_VALUES:
        cfg["permission"] = "read"
    return cfg


def _norm(p):
    """归一化路径：解符号链接、转小写、去尾部分隔符。

    必须用 realpath 不能用 abspath —— abspath 不解符号链接，
    白名单目录里放一个指向 C:\\ 的链接就穿出去了。
    """
    try:
        r = os.path.realpath(p)
    except Exception:
        r = os.path.abspath(p)
    return os.path.normcase(r.rstrip("\\/")) or os.path.normcase(r)

def check_dir(path, cfg=None):
    """判一个目录能不能当工作目录。返回 (行不行, 真实路径或拒绝原因)。

    三道判：
      1. 归一化之后必须落在某个白名单根目录里（或就是那个根）
      2. 路径里不能出现黑名单片段（.ssh / AppData / system32 …）
      3. 必须真的是个目录

    `..`、符号链接、绝对路径三种绕法都被第 1 道挡住，因为比的是
    realpath 之后的结果，不是用户给的字符串。
    """
    cfg = cfg or load_config()
    if not path:
        return (False, "没给目录")

    real = _norm(path)

    for bad in DENY_PARTS:
        if bad in real:
            return (False, "命中黑名单片段「%s」" % bad)

    roots = [_norm(d) for d in (cfg.get("allowed_dirs") or DEFAULT_ALLOWED_DIRS)]
    inside = False
    for root in roots:
        if real == root or real.startswith(root + os.sep):
            inside = True
            break
    if not inside:
        return (False, "不在白名单目录里")

    if not os.path.isdir(real):
        return (False, "目录不存在")
    return (True, real)


def safe_cwd(cfg=None, want=None):
    """取一个一定安全的工作目录。

    想用 want 就先过 check_dir；不合格就退回第一个可用的白名单目录。
    调用方拿到的永远是合法路径，不用自己再判一次。
    """
    cfg = cfg or load_config()
    for cand in (want, cfg.get("cwd")):
        if cand:
            ok, r = check_dir(cand, cfg)
            if ok:
                return r
    for d in (cfg.get("allowed_dirs") or DEFAULT_ALLOWED_DIRS):
        ok, r = check_dir(d, cfg)
        if ok:
            return r
    return None

