# -*- coding: utf-8 -*-
"""端到端测试 —— 真的走飞书，验证「发消息进来 → 干活 → 回复出去」整条链。

跟前两个测试的区别：
  test_bridge.py    假的 client，验逻辑
  test_parallel.py  假的 client，验并发
  这个              真的飞书 API，真的建群、真的发消息、真的收回复

跑法：python test_e2e.py
前提：bridge.py 正在跑（长连接得连着，不然事件推不过来）
"""
import io
import json
import os
import sys
import time
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


CFG = B.load_config()
# 本机有代理，飞书的域名走代理会被拦，必须绕开
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = "https://open.feishu.cn/open-apis"


def api(path, body=None, token=None, method=None):
    """打飞书 OpenAPI。返回 (code, msg, data)。"""
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
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


print()
print("=" * 58)
print("端到端测试（真的走飞书）")
print("=" * 58)

print()
print("步骤 1  拿 token")
code, msg, _ = api("/auth/v3/tenant_access_token/internal",
                   {"app_id": CFG["app_id"], "app_secret": CFG["app_secret"]})
# 这个接口的 token 不在 data 里，单独取一次
req = urllib.request.Request(
    BASE + "/auth/v3/tenant_access_token/internal",
    data=json.dumps({"app_id": CFG["app_id"],
                     "app_secret": CFG["app_secret"]}).encode("utf-8"),
    method="POST")
req.add_header("Content-Type", "application/json; charset=utf-8")
with OPENER.open(req, timeout=30) as r:
    tk = json.loads(r.read().decode("utf-8"))
TOKEN = tk.get("tenant_access_token")
check("★ 拿到 tenant_access_token", bool(TOKEN), "返回 %r" % tk)
if not TOKEN:
    print("没 token 后面全做不了，停。")
    sys.exit(1)

print()
print("步骤 2  机器人自己建一个群（不用你手动拉）")
code, msg, data = api("/im/v1/chats?set_bot_manager=true",
                      {"name": "桥接自测群",
                       "description": "test_e2e.py 建的，测完可以删",
                       "chat_mode": "group",
                       "chat_type": "private"},
                      TOKEN)
CHAT_ID = data.get("chat_id")
check("★ 建群成功", code == 0 and bool(CHAT_ID),
      "code=%s msg=%s data=%r" % (code, msg, data))
if not CHAT_ID:
    print("建不出群，后面的收发测不了。")
    print("可能原因：im:chat 权限没开，或者应用没发布。")
    sys.exit(1)
print("      群 id：%s" % CHAT_ID)

print()
print("步骤 3  机器人往群里发一条（验证发送链路）")
code, msg, data = api("/im/v1/messages?receive_id_type=chat_id",
                      {"receive_id": CHAT_ID, "msg_type": "text",
                       "content": json.dumps({"text": "自测：发送链路 OK"},
                                             ensure_ascii=False)},
                      TOKEN)
check("★ 机器人能往群里发消息", code == 0, "code=%s msg=%s" % (code, msg))

print()
print("步骤 4  用 lark client 发（跟 bridge.py 里走的是同一条路）")
try:
    import lark_oapi as lark
    client = lark.Client.builder() \
        .app_id(CFG["app_id"]).app_secret(CFG["app_secret"]).build()
    sent = B.send_msg(client, CHAT_ID, "自测：bridge.send_msg 也 OK")
    # 只有飞书返回 code == 0 才算发出去。以前断言 sent is True，
    # 而 send_msg 里 except 之外一律 return True —— 机器人不在群里也算过。
    check("★ bridge.send_msg 发得出去", sent.ok and sent.code == 0,
          "code=%s msg=%s message_id=%s" % (sent.code, sent.msg, sent.message_id))
except Exception as e:
    check("★ bridge.send_msg 发得出去", False, "炸了：%s" % e)

print()
print("步骤 5  查群里的消息（验证能读回来）")
time.sleep(2)
code, msg, data = api(
    "/im/v1/messages?container_id_type=chat&container_id=%s&page_size=20" % CHAT_ID,
    None, TOKEN)
items = data.get("items") or []
texts = []
for it in items:
    try:
        texts.append(json.loads(it.get("body", {}).get("content") or "{}").get("text") or "")
    except Exception:
        pass
check("★ 读得到群里的消息", code == 0 and len(items) >= 2,
      "code=%s 条数=%d msg=%s" % (code, len(items), msg))
check("★ 刚发的两条都在", any("发送链路 OK" in t for t in texts)
      and any("send_msg 也 OK" in t for t in texts),
      "读到的：%r" % texts)

print()
print("步骤 6  机器人在不在群里（决定它能不能收到消息）")
code, msg, data = api("/im/v1/chats/%s/members?page_size=20" % CHAT_ID, None, TOKEN)
check("查群成员成功", code == 0, "code=%s msg=%s" % (code, msg))

code, msg, data = api("/im/v1/chats?page_size=20", None, TOKEN)
chats = [c.get("chat_id") for c in (data.get("items") or [])]
check("★ 机器人已经在这个群里了", CHAT_ID in chats,
      "机器人在的群：%r" % chats)

print()
print("=" * 58)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    print("失败的：")
    for n, d in FAIL:
        print("   - %s   → %s" % (n, d))
print()
print("群 id 记一下：%s" % CHAT_ID)
print("接下来要你做的：在飞书里打开「桥接自测群」，发一句 /帮助 试试。")
print("=" * 58)
sys.exit(1 if FAIL else 0)
