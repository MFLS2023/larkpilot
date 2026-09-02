# -*- coding: utf-8 -*-
"""验证飞书应用配置到哪一步了。

不连长连接，只用普通 HTTP 接口问飞书几个问题：
  1. app_id / app_secret 对不对（能不能换到 token）
  2. 机器人能力开了没
  3. 权限批量开通了没（试着调一下要权限的接口）
  4. 机器人在哪些群里
"""
import io
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.loads(io.open(os.path.join(HERE, "bridge_config.json"), encoding="utf-8-sig").read())
APP_ID = cfg["app_id"]
APP_SECRET = cfg["app_secret"]

# 本机配了代理，飞书接口走代理可能不通，这里显式绕开
proxy_handler = urllib.request.ProxyHandler({})
opener = urllib.request.build_opener(proxy_handler)


def post(url, body, token=None):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with opener.open(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"code": -1, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


def get(url, token):
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", "Bearer " + token)
    try:
        with opener.open(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"code": -1, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return {"code": -1, "msg": str(e)}


print("=" * 58)
print("飞书应用配置检查")
print("=" * 58)
print("App ID:", APP_ID)
print()

# ---- 第1关：换 token。这一步过了说明 id/secret 都对 ----
print("[1] 用 app_id + app_secret 换 tenant_access_token")
r = post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
         {"app_id": APP_ID, "app_secret": APP_SECRET})
if r.get("code") == 0:
    token = r["tenant_access_token"]
    print("    [OK] 钥匙有效，拿到 token（有效期 %s 秒）" % r.get("expire"))
else:
    print("    [失败] code=%s  msg=%s" % (r.get("code"), r.get("msg")))
    print()
    print("    code 10003 = app_id 写错了")
    print("    code 10014 = app_secret 写错了")
    print("    code -1    = 网络不通（本机有代理，可能被拦）")
    sys.exit(1)

print()

# ---- 第2关：应用自己的信息，看名字和状态 ----
print("[2] 查应用自身信息")
r = get("https://open.feishu.cn/open-apis/application/v6/applications/%s?lang=zh_cn" % APP_ID, token)
if r.get("code") == 0:
    app = (r.get("data") or {}).get("app") or {}
    print("    应用名  :", app.get("app_name"))
    print("    状态    :", {0: "停用", 1: "启用", 2: "未启用/未发布", 3: "未启用"}.get(app.get("status"), app.get("status")))
    print("    应用类型:", "自建应用" if app.get("app_scene_type") == 0 else app.get("app_scene_type"))
else:
    print("    [取不到] code=%s msg=%s" % (r.get("code"), r.get("msg")))
    print("    （这个接口自己也要权限，取不到不代表配置有问题）")

print()

# ---- 第3关：机器人在哪些群里。这个接口要 im:chat 权限 ----
print("[3] 查机器人加入了哪些群（顺带验证 im:chat 权限）")
r = get("https://open.feishu.cn/open-apis/im/v1/chats?page_size=20", token)
if r.get("code") == 0:
    items = (r.get("data") or {}).get("items") or []
    print("    [OK] im:chat 权限正常")
    if items:
        print("    机器人在 %d 个群里：" % len(items))
        for it in items:
            print("       - %s   chat_id=%s" % (it.get("name") or "(无名)", it.get("chat_id")))
    else:
        print("    机器人还没进任何群。等下把它拉进群，或者直接跟它私聊。")
elif r.get("code") == 99991672 or "permission" in str(r.get("msg", "")).lower():
    print("    [权限没开] code=%s msg=%s" % (r.get("code"), r.get("msg")))
    print("    → 去权限管理页面开通 im:chat，然后点「批量开通」")
else:
    print("    [失败] code=%s msg=%s" % (r.get("code"), r.get("msg")))

print()
print("=" * 58)
print("下一步：在开发者后台把订阅方式选成「使用长连接接收事件」")
print("        选之前必须先把 bridge.py 跑起来")
print("=" * 58)
