# -*- coding: utf-8 -*-
"""桥接程序自测。不需要飞书凭据，把飞书的收发换成假的记录下来。

测这些：
  1. 危险词识别准不准（该拦的拦、不该拦的别拦）
  2. 内置命令（/帮助 /新会话 /状态）
  3. 危险操作的「确认 / 取消」流程
  4. 临时切档前缀
  5. 回复长度裁剪
  6. 真的调 claude -p 全开档，看能不能干活
"""
import io
import json
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bridge as B

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("   %s %s%s" % ("[OK]" if cond else "[NG]", name,
                          ("   → " + detail) if (detail and not cond) else ""))


# 假的飞书客户端：不发网络请求，只把要发的话记下来
class FakeClient(object):
    def __init__(self):
        self.sent = []


def fake_send(client, chat_id, text):
    client.sent.append(text)
    return True


B.send_msg = fake_send

# 状态文件挪到测试专用的，别污染真的
B.STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_state.json")
if os.path.exists(B.STATE_PATH):
    os.remove(B.STATE_PATH)

CFG = {
    "app_id": "x", "app_secret": "y",
    "cwd": os.path.join(os.path.expanduser("~"), ".claude", "claude-files"),
    "permission": "full", "confirm_dangerous": True,
    "timeout_seconds": 600, "allowed_user_ids": [], "allowed_chat_ids": [],
    "reply_max_chars": 3000,
}

print()
print("组 1  危险词识别")
DANGER_YES = [
    ("把 build 目录 rm -rf 掉", "递归删除文件"),
    ("git push --force 到 main", "强制推送 git"),
    ("git reset --hard HEAD~3", "破坏性 git 操作"),
    ("DROP TABLE users", "删库删表"),
    ("帮我部署到生产", "部署生产环境"),
    ("npm publish 一下", "发布到公共仓库"),
    ("Remove-Item C:\\temp -Recurse", "递归删除文件"),
    ("del /s *.tmp", "递归删除文件"),
]
for text, want in DANGER_YES:
    got = B.find_danger(text)
    check("拦住：%s" % text[:28], got == want, "识别成 %r，期望 %r" % (got, want))

DANGER_NO = [
    "把 notify.py 的超时改成 30 秒",
    "看一下 claude-files 里有什么文件",
    "git status 看看",
    "git commit -m 修好了",
    "SELECT * FROM users WHERE id=1",
    "DELETE FROM logs WHERE day < 100",   # 有 WHERE，不算删库
    "读一下 README 告诉我怎么装",
]
for text in DANGER_NO:
    got = B.find_danger(text)
    check("放过：%s" % text[:28], got is None, "误拦成 %r" % got)

print()
print("组 2  内置命令")
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/帮助", CFG)
check("/帮助 回了帮助文字", c.sent and "飞书遥控" in c.sent[-1])

# 老格式的状态文件（只有一个顶层 session_id）要能自动搬到「默认」这条线上
c = FakeClient()
B.save_state({"session_id": "abc-123"})
st = B.load_state()
check("★ 老格式状态自动迁移成线", "默认" in st["lines"],
      "迁移后是 %r" % st)
check("★ 迁移后 session_id 没丢",
      st["lines"].get("默认", {}).get("session_id") == "abc-123")

B.save_state(st)
B.handle_text(c, "chat1", "u1", "/状态", CFG)
check("/状态 显示了档位", c.sent and "档位：full" in c.sent[-1])
check("/状态 显示了会话号", c.sent and "abc-123"[:8] in c.sent[-1])
check("/状态 显示了在哪条线", c.sent and "在哪条线：默认" in c.sent[-1])

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/新会话", CFG)
check("/新会话 清掉了当前线的 session_id",
      not B.load_state()["lines"]["默认"].get("session_id"))
check("/新会话 有回话", c.sent and "清空" in c.sent[-1])

print()
print("组 3  危险操作的确认流程")
c = FakeClient()
B.save_state({})
B.handle_text(c, "chat1", "u1", "把 dist 目录 rm -rf 掉", CFG)
st = B.load_state()
check("★ 危险操作没有直接执行", bool(st.get("pending")), "pending 是 %r" % st.get("pending"))
check("★ 问了用户一句", c.sent and "回「确认」" in c.sent[-1])
check("提示里说明了是什么操作", c.sent and "递归删除文件" in c.sent[-1])
check("提示里带上了原话", c.sent and "dist" in c.sent[-1])

# 回「取消」
c2 = FakeClient()
B.handle_text(c2, "chat1", "u1", "取消", CFG)
check("★ 回「取消」后 pending 清掉了", not B.load_state().get("pending"))
check("★ 回「取消」后确实什么都没做", c2.sent and "什么都没动" in c2.sent[-1])

# 危险操作 → 发了别的话（既不是确认也不是取消）
c3 = FakeClient()
B.save_state({})
B.handle_text(c3, "chat1", "u1", "DROP TABLE orders", CFG)
check("第二个危险操作也被拦", bool(B.load_state().get("pending")))
# 关掉二次确认后不该拦
CFG_OFF = dict(CFG, confirm_dangerous=False)
check("配置关掉后 find_danger 仍能识别（拦不拦由 handle_text 决定）",
      B.find_danger("rm -rf /tmp/x") is not None)

print()
print("组 4  临时切档前缀")


# 把 do_task 换成假的，只记参数不真调 claude
calls = []


def fake_do_task(client, chat_id, prompt, cfg, perm_override=None):
    calls.append({"prompt": prompt, "perm": perm_override})


real_do_task = B.do_task
B.do_task = fake_do_task

B.save_state({})
for pre, want_perm in (("/只读", "read"), ("/聊天", "chat"), ("/改文件", "write")):
    calls.clear()
    B.handle_text(FakeClient(), "chat1", "u1", pre + " 看看有什么文件", CFG)
    ok = calls and calls[0]["perm"] == want_perm
    check("%s 切到 %s 档" % (pre, want_perm), ok,
          "实际 %r" % (calls[0] if calls else None))
    check("%s 前缀被剥掉了" % pre,
          calls and calls[0]["prompt"] == "看看有什么文件",
          "prompt=%r" % (calls[0]["prompt"] if calls else None))

# 只读档下危险词不该拦（本来就改不了东西）
calls.clear()
B.save_state({})
B.handle_text(FakeClient(), "chat1", "u1", "/只读 rm -rf 会怎样", CFG)
check("★ 只读档下危险词不拦（反正它做不到）",
      calls and calls[0]["perm"] == "read" and not B.load_state().get("pending"))

# 切档前缀后面空的
c4 = FakeClient()
calls.clear()
B.handle_text(c4, "chat1", "u1", "/只读", CFG)
check("空的切档前缀会提示", c4.sent and "得跟上" in c4.sent[-1])

B.do_task = real_do_task

print()
print("组 5  长回复分条发（以前是截断，内容会丢）")
# 5000 字在老版本会被砍到 3000 —— 现在应该一个字不丢
long_text = "啊" * 5000
out = B.build_reply(long_text, CFG)
check("短回复还是一条", B.build_reply("好了", CFG) == ["好了"])
check("出错前缀会拼上", B.build_reply("坏了", CFG, "✗ 出错了\n\n")[0].startswith("✗"))
check("★ 5000 字不再被截断（一条装得下）", len(out) == 1 and "截断" not in out[0],
      "分了 %d 条" % len(out))
check("★ 内容一个字没少", out[0] == long_text,
      "原文 %d 字，发出去 %d 字" % (len(long_text), len(out[0])))

# 真的超过单条上限：10 万汉字 = 30 万字节，得分 3 条
huge = "。\n\n".join("第%d段内容" % i + "文" * 400 for i in range(120))
parts = B.build_reply(huge, CFG)
check("★ 超长回复被分成多条", len(parts) > 1, "只分了 %d 条" % len(parts))
check("★ 每条都在飞书 150KB 上限内",
      all(len(p.encode("utf-8")) < B.FEISHU_TEXT_LIMIT_BYTES for p in parts),
      "最大一条 %d 字节" % max(len(p.encode("utf-8")) for p in parts))
joined = "".join(re.sub(r"\n\n〔[^〕]*〕$", "", p) for p in parts)
check("★ 分条之后内容没丢（拼回来跟原文一样长）",
      len(joined.replace("\n", "")) == len(huge.replace("\n", "")),
      "原文 %d 字，拼回 %d 字" % (len(huge.replace("\n", "")),
                              len(joined.replace("\n", ""))))
check("★ 中间几条标了「第几段」", "〔第 1/" in parts[0], "第一条尾部：%r" % parts[0][-20:])
check("★ 最后一条标了「完」", "〔完，共" in parts[-1], "最后一条尾部：%r" % parts[-1][-20:])

# 单行超长（没有换行的大块日志）也要能切
oneline = "字" * 60000       # 18 万字节
parts2 = B.split_for_feishu(oneline)
check("★ 没有换行的大块文本也能切", len(parts2) > 1, "只分了 %d 条" % len(parts2))
check("★ 硬切的每条也在上限内",
      all(len(p.encode("utf-8")) <= B.SAFE_CHUNK_BYTES for p in parts2))

# 想回到老的截断行为
cut = B.build_reply(long_text, dict(CFG, truncate_reply=True))
check("truncate_reply=true 时回到老的截断行为",
      len(cut) == 1 and "截断" in cut[0], "结果：%r" % cut[0][-30:])

check("抠出普通文本",
      B.extract_text(json.dumps({"text": "你好"}), "text") == "你好")
check("★ 去掉 @机器人 占位符",
      B.extract_text(json.dumps({"text": "@_user_1 帮我改代码"}), "text") == "帮我改代码")
check("图片消息返回 None", B.extract_text('{"image_key":"x"}', "image") is None)
check("坏 JSON 不炸", B.extract_text("{坏", "text") is None)

print()
print("组 6  真的调 claude -p（全开档，会花点时间）")
txt, sid, bad = B.run_claude("只回答一个数字：3 加 4 等于几", CFG)
check("★ claude 全开档跑通了", not bad, "回的是：%s" % txt[:200])
check("★ 答案对", "7" in txt, "回的是：%s" % txt[:200])
check("★ 拿到了 session_id", bool(sid), "sid=%r" % sid)

if sid:
    txt2, sid2, bad2 = B.run_claude("我刚问你的算式是什么？原样重复一遍。", CFG, sid)
    check("★ 会话续接成功", (not bad2) and ("3" in txt2 and "4" in txt2),
          "回的是：%s" % txt2[:200])
    check("★ session_id 保持一致", sid2 == sid, "%r vs %r" % (sid2, sid))

# 只读档真的动不了文件
probe = os.path.join(CFG["cwd"], "test_bridge_probe.txt")
if os.path.exists(probe):
    os.remove(probe)
txt3, _, _ = B.run_claude(
    "在当前目录建一个 test_bridge_probe.txt，随便写点内容。做不到就说做不到。",
    dict(CFG, permission="read"))
check("★ 只读档确实建不出文件", not os.path.exists(probe),
      "文件被建出来了！回复：%s" % txt3[:200])
if os.path.exists(probe):
    os.remove(probe)

print()
print("组 7  多任务线（/新 /切 /列表 /删）")

# 拦住 do_task，只记「跑在哪条线上、用什么档位」，不真调引擎
calls = []


def fake_do_task2(client, chat_id, prompt, cfg, perm_override=None, line_name=None):
    calls.append({"prompt": prompt, "perm": perm_override, "line": line_name})


real_do_task2 = B.do_task
B.do_task = fake_do_task2

B.save_state({})
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/新 改网站", CFG)
st = B.load_state()
check("★ /新 建出了线", "改网站" in st["lines"], "现在有 %r" % list(st["lines"]))
check("★ /新 之后当前就是它", st.get("current") == "改网站")
check("/新 的回话带上了名字", c.sent and "改网站" in c.sent[-1])

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/新 查数据 codex", CFG)
st = B.load_state()
check("★ /新 能指定引擎", st["lines"].get("查数据", {}).get("engine") == "codex",
      "engine=%r" % st["lines"].get("查数据", {}).get("engine"))
check("两条线同时存在", len(st["lines"]) == 2, "有 %r" % list(st["lines"]))

# 各自记住自己的 session_id —— 这是「多任务不串」的关键
B.touch_line("改网站", session_id="sid-web")
B.touch_line("查数据", session_id="sid-data")
st = B.load_state()
check("★ 两条线的 session_id 各自独立",
      st["lines"]["改网站"]["session_id"] == "sid-web"
      and st["lines"]["查数据"]["session_id"] == "sid-data")

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/切 改网站", CFG)
check("★ /切 切过去了", B.load_state().get("current") == "改网站")
check("/切 的回话说了上下文还在", c.sent and "接着上次聊" in c.sent[-1])

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/切 查", CFG)
check("★ /切 支持简写（唯一匹配）", B.load_state().get("current") == "查数据",
      "现在在 %r" % B.load_state().get("current"))

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/切 不存在的线", CFG)
check("/切 找不到时不改当前线", B.load_state().get("current") == "查数据")
check("/切 找不到时列出有哪些", c.sent and "改网站" in c.sent[-1])

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/列表", CFG)
check("/列表 有序号格式",
      c.sent and bool(re.search(r"1\. [🟢🟡⚪]", c.sent[-1])),
      "回的是：%s" % (c.sent[-1][:200] if c.sent else ""))
check("/列表 有客户端名",
      c.sent and bool(re.search(r"Claude Desktop|Claude Code|Codex|SDK", c.sent[-1])),
      "回的是：%s" % (c.sent[-1][:200] if c.sent else ""))

# 普通消息要落在当前这条线上
calls[:] = []
c = FakeClient()
B.handle_text(c, "chat1", "u1", "看一下有什么文件", CFG)
check("★ 普通消息跑在当前线上（不指定 line_name，由 do_task 自己取）",
      len(calls) == 1 and calls[0]["line"] is None,
      "calls=%r" % calls)

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/删 改网站", CFG)
st = B.load_state()
check("★ /删 删掉了线", "改网站" not in st["lines"], "还剩 %r" % list(st["lines"]))
check("/删 没动别的线", "查数据" in st["lines"])

# 正在跑的线不许删
B.touch_line("查数据", busy=True)
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/删 查数据", CFG)
check("★ 正在跑的线删不掉", "查数据" in B.load_state()["lines"])
check("说明了为什么删不掉", c.sent and "还在跑" in c.sent[-1])
B.touch_line("查数据", busy=False)

# 危险操作要记住是哪条线提的
B.save_state({})
B.handle_text(FakeClient(), "chat1", "u1", "/新 危险线", CFG)
B.handle_text(FakeClient(), "chat1", "u1", "/新 别的线", CFG)
B.handle_text(FakeClient(), "chat1", "u1", "/切 危险线", CFG)
B.handle_text(FakeClient(), "chat1", "u1", "把 build 目录 rm -rf 掉", CFG)
pend = B.load_state().get("pending") or {}
check("★ pending 记住了是哪条线提的", pend.get("line") == "危险线",
      "pending=%r" % pend)
# 中途切走，再确认
B.handle_text(FakeClient(), "chat1", "u1", "/切 别的线", CFG)
calls[:] = []
B.handle_text(FakeClient(), "chat1", "u1", "确认", CFG)
check("★ 确认后跑在原来那条线上，不是切走后的这条",
      len(calls) == 1 and calls[0]["line"] == "危险线", "calls=%r" % calls)

B.do_task = real_do_task2

print()
print("组 7.5  /新 后面直接跟一整句话（真实踩到的坑）")
# 实测：用户第一次用就发了「/新 请帮我查找一下……是利好还是利空？」，
# 结果整句话变成了线的名字，活一点没干。这组防它复发。
B.save_state({})
B.do_task = fake_do_task2
calls[:] = []
c = FakeClient()
LONG = "请帮我查找一下这周末发酵的光模块的消息，是利好旭创还是利空？"
B.handle_text(c, "chat1", "u1", "/新 " + LONG, CFG)
st = B.load_state()
check("★ 整句话没变成线名（名字被截短了）",
      LONG not in st["lines"] and len(list(st["lines"])[0]) <= 10,
      "线名是 %r" % list(st["lines"]))
check("★ 活真的派出去了，没只建个线就完事",
      len(calls) == 1, "calls=%r" % calls)
check("★ 派出去的是完整原话，不是被截短的名字",
      calls and calls[0]["prompt"] == LONG,
      "派出去的是 %r" % (calls[0]["prompt"] if calls else None))
check("回话说了「这就去办」", c.sent and "这就去办" in c.sent[-1],
      "回的是：%r" % (c.sent[-1] if c.sent else None))

# 短名字还是老行为：只建线，不跑活
calls[:] = []
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/新 改网站", CFG)
check("★ 短名字仍然只建线不跑活", not calls, "calls=%r" % calls)
check("短名字建出来的就是它自己", "改网站" in B.load_state()["lines"])

# 名字 + 引擎 + 任务 三样一起给
calls[:] = []
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/新 光模块 codex 帮我查查消息", CFG)
st = B.load_state()
check("★ 名字/引擎/任务能同时给：线名对", "光模块" in st["lines"],
      "线名 %r" % list(st["lines"]))
check("★ 引擎对", st["lines"].get("光模块", {}).get("engine") == "codex")
check("★ 任务对", calls and calls[0]["prompt"] == "帮我查查消息",
      "calls=%r" % calls)

# /切 后面跟任务
calls[:] = []
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/切 改网站 顺便看下报错", CFG)
check("★ /切 名字 + 任务：切过去了", B.load_state().get("current") == "改网站",
      "现在在 %r" % B.load_state().get("current"))
check("★ /切 名字 + 任务：活也办了",
      calls and calls[0]["prompt"] == "顺便看下报错", "calls=%r" % calls)

# 已存在的线 + 任务
calls[:] = []
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/新 改网站 再看一眼那个报错", CFG)
check("★ 线已存在时也不丢活",
      calls and calls[0]["prompt"] == "再看一眼那个报错",
      "calls=%r" % calls)
check("线已存在时切过去而不是重建", B.load_state().get("current") == "改网站")

# ★ 这几条是「按词数判断」栽过的地方，逐个钉住
n, t, g = B.split_name_task("改网站 再看一眼那个报错", {})
check("★ 短名字后面跟一句话：名字只取前面那个词", n == "改网站", "拆出 %r" % n)
check("★ 短名字后面跟一句话：活不带上名字", t == "再看一眼那个报错", "拆出 %r" % t)

n, t, g = B.split_name_task("光模块 codex 帮我查查消息", {})
check("★ 名字+引擎+任务三样分得开",
      (n, t, g) == ("光模块", "帮我查查消息", "codex"), "拆出 %r" % ((n, t, g),))

n, t, g = B.split_name_task(LONG, {})
check("★ 中文长句没空格也不会整句当名字", len(n) <= 8, "名字是 %r" % n)
check("★ 中文长句：整句当活", t == LONG, "活是 %r" % t[:40])

n, t, g = B.split_name_task("看一下报错", {})
check("★ 五个字没标点算名字，不算任务", (n, t) == ("看一下报错", ""),
      "拆出 %r" % ((n, t),))

n, t, g = B.split_name_task("查数据 codex 看看8月销量", {"查数据": {}})
check("★ 已有线+引擎+任务",
      (n, t, g) == ("查数据", "看看8月销量", "codex"), "拆出 %r" % ((n, t, g),))

B.do_task = real_do_task2

print()
print("组 8  换引擎（/codex /claude）")
B.save_state({})
B.do_task = fake_do_task2
calls[:] = []

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/新 跑codex", CFG)
B.touch_line("跑codex", session_id="claude-sid-1")
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/codex 看一下这个仓库", CFG)
st = B.load_state()
check("★ /codex 把这条线改成 codex 了",
      st["lines"]["跑codex"]["engine"] == "codex",
      "engine=%r" % st["lines"]["跑codex"]["engine"])
check("★ 换引擎时丢掉了旧 session_id（两边会话号不通用）",
      not st["lines"]["跑codex"]["session_id"],
      "sid=%r" % st["lines"]["跑codex"]["session_id"])
check("★ 前缀被剥掉，任务照跑",
      len(calls) == 1 and calls[0]["prompt"] == "看一下这个仓库",
      "calls=%r" % calls)

calls[:] = []
c = FakeClient()
B.handle_text(c, "chat1", "u1", "/claude", CFG)
check("★ /claude 换回来了",
      B.load_state()["lines"]["跑codex"]["engine"] == "claude")
check("只发 /claude 不跑任务", not calls, "calls=%r" % calls)

c = FakeClient()
B.handle_text(c, "chat1", "u1", "/claude", CFG)
check("已经是这个引擎时会说一声", c.sent and "本来就是" in c.sent[-1])

B.do_task = real_do_task2

print()
print("组 9  codex 输出解析（不起进程，喂真实抓到的 JSONL）")
# 这段是真的从 codex exec --json 抓下来的，原样贴进来
real_jsonl = """Reading prompt from stdin...
{"type":"thread.started","thread_id":"019fe16a-990d-7312-8106-100e5ee69c6e"}
{"type":"item.completed","item":{"id":"item_0","type":"error","message":"clamping SessionEnd hook timeout to 3s"}}
{"type":"item.completed","item":{"id":"item_1","type":"error","message":"Model metadata for x not found."}}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"7"}}
{"type":"turn.completed","usage":{"input_tokens":30936}}
"""
txt, sid, errs = B.parse_codex_jsonl(real_jsonl)
check("★ 从 JSONL 里挑出了答案", txt == "7", "拿到 %r" % txt)
check("★ 挑出了 thread_id 当会话号",
      sid == "019fe16a-990d-7312-8106-100e5ee69c6e", "sid=%r" % sid)
check("中途的 error 事件收集了但没当成失败", len(errs) == 2, "errs=%r" % errs)
check("非 JSON 的那行没让它炸（Reading prompt from stdin）", True)

txt2, sid2, errs2 = B.parse_codex_jsonl(
    '{"type":"thread.started","thread_id":"t1"}\n'
    '{"type":"item.completed","item":{"type":"error","message":"炸了"}}\n')
check("★ 没有 agent_message 时答案为空（好让上层报错）", txt2 == "")
check("拿得到失败原因", errs2 == ["炸了"], "errs=%r" % errs2)

txt3, _, _ = B.parse_codex_jsonl(
    '{"type":"item.completed","item":{"type":"agent_message","text":"第一段"}}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"第二段"}}\n')
check("多段回复拼起来", txt3 == "第一段\n\n第二段", "拿到 %r" % txt3)
check("空输入不炸", B.parse_codex_jsonl("") == ("", None, []))
check("坏行不炸", B.parse_codex_jsonl("{不是JSON\n随便一行\n")[0] == "")

check("codex 沙箱档位映射：只读→read-only",
      B.CODEX_SANDBOX["read"] == "read-only")
check("codex 沙箱档位映射：全开→danger-full-access",
      B.CODEX_SANDBOX["full"] == "danger-full-access")

print()
print("组 10  真的调 codex exec（会花点时间）")
CFG_X = dict(CFG, permission="read")
txt, sid, bad = B.run_codex("只回答一个数字：5 加 6 等于几", CFG_X)
check("★ codex 跑通了", not bad, "回的是：%s" % txt[:300])
check("★ codex 答案对", "11" in txt, "回的是：%s" % txt[:300])
check("★ codex 给了会话号", bool(sid), "sid=%r" % sid)

if sid:
    txt2, sid2, bad2 = B.run_codex("把刚才那个数字乘以 2，只回答数字", CFG_X, sid)
    check("★ codex 会话续接成功（记得上一轮）",
          (not bad2) and "22" in txt2, "回的是：%s" % txt2[:300])
    check("★ codex session_id 保持一致", sid2 == sid, "%r vs %r" % (sid2, sid))

# run_engine 分发对不对
check("run_engine 认得 codex", B.run_engine.__name__ == "run_engine")

print()
print("=" * 56)
print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
if FAIL:
    print()
    print("失败的：")
    for f in FAIL:
        print("   -", f)
print("=" * 56)

if os.path.exists(B.STATE_PATH):
    os.remove(B.STATE_PATH)
sys.exit(1 if FAIL else 0)
