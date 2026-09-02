# -*- coding: utf-8 -*-
"""把 Stop/StopFailure/Notification 事件通知到飞书企业自建应用，支持多窗口回复。

替代原有的 notify.py webhook 推送方案——webhook 只能单向发，收到通知后想回复
还得切回电脑；现在改成自建应用直接发消息，你在手机上回复，桥那边能收到并转给
对应的 Claude 窗口继续干活。

## 多窗口对应

每个 Claude Code 会话对应一个飞书私聊窗口（chat_id）。第一次收到通知时自动创建
私聊，标题格式「[Claude] 项目名 · 会话ID前8位」，以后该会话的通知都往这个窗口发。
你在那个窗口回复就是回给那个会话，不会串。

## 钩子配置

把原来 settings.json 里的 Stop/StopFailure/Notification 钩子的 command 改成：
    python notify_to_enterprise.py <事件名>

配置文件复用 notify_config.json，新增三个字段：
    enterprise_app_id       飞书企业应用的 App ID
    enterprise_app_secret   飞书企业应用的 App Secret
    user_open_id            你的 open_id（从飞书开发者后台或 API 拿）

原有的 webhook 字段不用删——可能别的地方还在用；这个脚本只看新增的三个字段。
"""

import json
import os
import sys
import time
import urllib.request

# Windows 控制台 UTF-8
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
BRIDGE_STATE = os.path.join(BASE, "bridge_state.json")  # 存 session_id → chat_id
CONFIG_PATH = os.path.join(os.path.dirname(BASE), "hooks", "notify_config.json")

# 导入原脚本的所有功能函数
sys.path.insert(0, os.path.join(os.path.dirname(BASE), "hooks"))
try:
    import notify
except ImportError:
    print("找不到 notify.py，请确认路径正确")
    sys.exit(1)


def load_bridge_state():
    """读飞书桥的状态文件，从里面拿 session_id → chat_id 的映射。

    桥状态现在的结构是 {"lines": {...}, "current": "..."}，不是平铺的。
    我们给它加一个顶层 chats 字段存 Claude session_id → 飞书 chat_id 的映射。
    """
    try:
        with open(BRIDGE_STATE, "r", encoding="utf-8-sig") as f:
            st = json.load(f)
    except Exception:
        st = {}
    # 确保结构完整
    if not isinstance(st.get("lines"), dict):
        st["lines"] = {}
    if not isinstance(st.get("chats"), dict):
        st["chats"] = {}
    return st


def save_bridge_state(st):
    try:
        with open(BRIDGE_STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_token(app_id, app_secret):
    """换 tenant_access_token。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=25) as r:
            j = json.load(r)
        if j.get("code") == 0:
            return j["tenant_access_token"]
    except Exception:
        pass
    return None


def create_chat_for_session(token, session_id, project, user_open_id):
    """为这个会话创建一个私聊窗口，标题标明项目和会话ID前缀。

    返回 chat_id 或 None。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    title = "[Claude] %s · %s" % (project, session_id[:8])
    body = json.dumps({
        "user_ids": [user_open_id],
        "name": title,
        "chat_mode": "p2p",
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/chats",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with opener.open(req, timeout=25) as r:
            j = json.load(r)
        if j.get("code") == 0:
            return (j.get("data") or {}).get("chat_id")
    except Exception:
        pass
    return None


def send_to_chat(token, chat_id, text):
    """往指定 chat_id 发消息。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with opener.open(req, timeout=25) as r:
            j = json.load(r)
        return j.get("code") == 0
    except Exception:
        return False


def main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""

    # 手动测试
    if event == "test":
        cfg = notify.load_config()
        app_id = cfg.get("enterprise_app_id")
        app_secret = cfg.get("enterprise_app_secret")
        user_open_id = cfg.get("user_open_id")

        if not all([app_id, app_secret, user_open_id]):
            print("notify_config.json 里缺少 enterprise_app_id / enterprise_app_secret / user_open_id")
            return 1

        token = get_token(app_id, app_secret)
        if not token:
            print("换 token 失败，检查 app_id / app_secret")
            return 1

        # 创建测试私聊
        chat_id = create_chat_for_session(token, "test-session", "测试项目", user_open_id)
        if not chat_id:
            print("创建私聊失败")
            return 1

        print("私聊创建成功，chat_id:", chat_id)
        ok = send_to_chat(token, chat_id, "[测试]\n看到这条说明企业通知链路配好了")
        print("发送：", "成功" if ok else "失败")
        return 0 if ok else 1

    # 读配置
    cfg = notify.load_config()
    app_id = cfg.get("enterprise_app_id")
    app_secret = cfg.get("enterprise_app_secret")
    user_open_id = cfg.get("user_open_id")

    if not all([app_id, app_secret, user_open_id]):
        # 配置不全，静默跳过（不报错）——可能用户只配了 webhook 方案
        return 0

    # Codex 的 Stop 必须回 JSON
    is_codex = event.startswith("codex-")
    base = event[6:] if is_codex else event

    def done():
        if is_codex and base == "stop":
            sys.stdout.write('{"continue": true}')
        return 0

    if not cfg.get("enabled", True):
        return done()

    payload = notify.read_stdin_json()
    sid = str(payload.get("session_id") or "default")

    # 只处理真正要推送的事件
    if base not in ("notification", "stop", "stopfail", "subagentstop",
                    "precompact", "postcompact", "sessionend", "permission"):
        return done()

    # 复用 notify.py 的逻辑构造通知文本
    project = notify.project_of(payload)
    who = notify.client_name(is_codex)
    limit = int(cfg.get("summary_chars", 300))
    want_tok = bool(cfg.get("show_tokens", True))
    state = notify.load_state()
    started = state.get(sid)

    def bill():
        if not want_tok or not started:
            return None
        try:
            since = float(started)
        except Exception:
            return None
        return notify.bill_for(sid, since, is_codex)

    elapsed = notify.elapsed_of(started) if started else None

    # 根据事件类型构造通知文本（简化版，只取最常用的几种）
    text = None
    tag = None
    body = ""

    if base == "notification":
        raw = payload.get("message")
        if notify.is_noise(raw):
            return done()
        tag = "[等你操作]"
        body = notify.clip(raw, limit)
    elif base == "stop":
        # 耗时门槛
        if elapsed is None or elapsed < float(cfg.get("min_seconds", 60)):
            # 从状态里清掉（即使不推送也要清，否则下次 sessionend 会误判）
            if sid in state:
                state.pop(sid)
                notify.save_state(state)
            return done()
        tag = "[已中断]" if notify.interrupt_reason(payload) else "[任务完成]"
        body = notify.clip(payload.get("last_assistant_message"), limit) or "（无输出）"
        # 清状态
        if sid in state:
            state.pop(sid)
            notify.save_state(state)
    elif base == "stopfail":
        tag = "[出错中断]"
        err = str(payload.get("error") or "unknown").strip().lower()
        hint = ("客户端会自己重试，一般不用管" if err in notify.RETRYABLE
                else "这类等不会好，要回来改配置")
        body = notify.error_cn(payload.get("error"), payload.get("error_details")) + "\n" + hint
    elif base == "subagentstop":
        tag = "[子任务完成]"
        msg = notify.clip(payload.get("last_assistant_message") or payload.get("message"), limit) or "子任务已完成"
        kind = str(payload.get("agent_type") or "").strip()
        body = ("子代理：%s\n%s" % (kind, msg)) if kind else msg
    elif base == "permission":
        tag = "[等你批准]"
        tool = str(payload.get("tool_name") or "某个操作")
        ti = payload.get("tool_input")
        detail = ""
        if isinstance(ti, dict):
            detail = notify.clip(ti.get("description") or ti.get("command"), limit)
        body = tool + (("\n" + detail) if detail else "")
    elif base == "precompact":
        trigger = str(payload.get("trigger") or "").strip().lower()
        if trigger == "manual":
            return done()
        tag = "[正在压缩上下文]"
        body = "上下文满了，正在自动压缩"
    elif base == "postcompact":
        trigger = str(payload.get("trigger") or "").strip().lower()
        if trigger == "manual":
            return done()
        tag = "[压缩完成]"
        body = "上下文已压缩，任务继续"
    elif base == "sessionend":
        # 只在任务还没收尾时才推
        if sid not in state:
            return done()
        state.pop(sid)
        notify.save_state(state)
        if elapsed is None or elapsed < float(cfg.get("min_seconds", 60)):
            return done()
        tag = "[会话中断]"
        why = str(payload.get("reason") or "").strip().lower()
        WHY_CN = {"clear": "被 /clear 清空", "resume": "切换到别的会话",
                  "logout": "退出登录", "prompt_input_exit": "你退出了",
                  "bypass_permissions_disabled": "权限模式被关掉",
                  "other": "异常结束"}
        body = "任务没跑完就结束了：" + WHY_CN.get(why, why or "原因不明")

    if not tag:
        return done()

    text = notify.compose(tag, who, project, elapsed, bill(), body)

    # 换 token
    token = get_token(app_id, app_secret)
    if not token:
        return done()

    # 从桥状态里找这个会话对应的 chat_id
    bridge_st = load_bridge_state()
    chat_id = bridge_st.get("chats", {}).get(sid)

    # 没有就创建
    if not chat_id:
        chat_id = create_chat_for_session(token, sid, project, user_open_id)
        if chat_id:
            bridge_st.setdefault("chats", {})[sid] = chat_id
            save_bridge_state(bridge_st)

    if not chat_id:
        return done()

    # 发送
    send_to_chat(token, chat_id, text)
    return done()


if __name__ == "__main__":
    try:
        code = main() or 0
    except Exception:
        code = 0

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass

    os._exit(code)
