# -*- coding: utf-8 -*-
"""session_scanner 格式快照测试 —— 不依赖真实会话文件。

背景：2026-08-23 Codex 改了 rollout 记录格式（event_msg/user_message 消失，
改用 response_item/message），所有新会话详情页一夜变空白；切供应商续接又
产生「旧id_新id」双 UUID 文件名，sid 抠取拼错。

这份测试把三种格式各造一份最小样本放进临时目录，锁住解析行为：
以后谁改坏了扫描器，跑这一份立刻炸出来。

    python test_scanner_formats.py
"""

import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
import session_scanner as S

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("   [%s] %s%s" % ("OK" if cond else "NG", name,
                            "" if cond else "   → %s" % (detail,)))


def w(path, records):
    with io.open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def ts(hh, mm):
    return "2026-08-23T%02d:%02d:00.000Z" % (hh, mm)


def main():
    tmp = tempfile.mkdtemp(prefix="scan_fmt_")
    codex_root = os.path.join(tmp, "codex", "2026", "08", "23")
    os.makedirs(codex_root)
    claude_root = os.path.join(tmp, "claude", "proj")
    os.makedirs(claude_root)
    pi_root = os.path.join(tmp, "pi", "--C--Users-t--")
    os.makedirs(pi_root)

    real_codex, real_claude, real_pi = (S.CODEX_SESSIONS, S.CLAUDE_PROJECTS,
                                        [dict(x) for x in S.EXTRA_SOURCES])
    try:
        # ── 样本 1：老格式（event_msg）──────────────────────────
        old_sid = "019fe21d-ff9c-7cc0-951c-cccb8e3ca754"
        w(os.path.join(codex_root,
           "rollout-2026-08-09T00-04-04-%s.jsonl" % old_sid), [
            {"type": "session_meta", "timestamp": ts(0, 4),
             "payload": {"session_id": old_sid, "cwd": "C:\\tmp\\projA"}},
            {"type": "event_msg", "timestamp": ts(0, 5),
             "payload": {"type": "user_message", "message": "老格式提问"}},
            {"type": "event_msg", "timestamp": ts(0, 6),
             "payload": {"type": "agent_message", "message": "老格式回答"}},
            {"type": "event_msg", "timestamp": ts(0, 7),
             "payload": {"type": "task_complete", "last_agent_message": "完"}},
        ])

        # ── 样本 2：新格式（response_item/message + 注入）───────
        new_sid = "01a02cb6-70f2-7a72-b802-e18ad6b8edbf"
        w(os.path.join(codex_root,
           "rollout-2026-08-23T11-42-29-%s.jsonl" % new_sid), [
            {"type": "session_meta", "timestamp": ts(11, 42),
             "payload": {"session_id": new_sid, "cwd": "C:\\tmp\\projB"}},
            {"type": "response_item", "timestamp": ts(11, 43),
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text",
                  "text": "# AGENTS.md instructions\n<INSTRUCTIONS>全文"}]}},
            {"type": "response_item", "timestamp": ts(11, 44),
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text",
                  "text": "<environment_context>\n  <cwd>C:\\tmp</cwd>"}]}},
            {"type": "response_item", "timestamp": ts(11, 45),
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "新格式提问"}]}},
            {"type": "response_item", "timestamp": ts(11, 46),
             "payload": {"type": "message", "role": "assistant", "content": [
                 {"type": "output_text", "text": "新格式回答"}]}},
            {"type": "event_msg", "timestamp": ts(11, 47),
             "payload": {"type": "task_complete", "last_agent_message": "好"}},
        ])

        # ── 样本 3：切供应商分叉（旧id_新id 双 UUID 文件名）──────
        parent = "01a02bfc-32a2-79a1-9fb3-eb10782b9bbd"
        child = "01a02d1f-5556-7702-b337-e78909fa1234"
        w(os.path.join(codex_root,
           "rollout-2026-08-23T13-37-03-%s_%s.jsonl" % (parent, child)), [
            {"type": "session_meta", "timestamp": ts(13, 37),
             "payload": {"session_id": child, "cwd": "C:\\tmp\\projB"}},
            {"type": "response_item", "timestamp": ts(13, 38),
             "payload": {"type": "message", "role": "user", "content": [
                 {"type": "input_text", "text": "分叉后的第一句"}]}},
            {"type": "response_item", "timestamp": ts(13, 39),
             "payload": {"type": "message", "role": "assistant", "content": [
                 {"type": "output_text", "text": "分叉后的回复"}]}},
        ])

        # ── 样本 4：pi 格式（含 content 空占位）─────────────────
        pi_sid = "01a03323-b139-7725-9ce2-b3180b4b9ade"
        w(os.path.join(pi_root,
           "2026-08-24T09-39-32-281Z_%s.jsonl" % pi_sid), [
            {"type": "session", "version": 3, "id": pi_sid,
             "timestamp": ts(9, 39), "cwd": "C:\\Users\\t"},
            {"type": "message", "timestamp": ts(9, 40),
             "message": {"role": "user", "content": [
                 {"type": "text", "text": "pi 的提问"}]}},
            {"type": "message", "timestamp": ts(9, 41),
             "message": {"role": "assistant", "content": []}},
            {"type": "message", "timestamp": ts(9, 42),
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "pi 的回答"}]}},
        ])

        # ── 样本 5：健康自检用的「大而不可识别」文件 ─────────────
        w(os.path.join(codex_root,
           "rollout-2026-08-23T15-00-00-aaaa1111-2222-3333-4444-555566667777.jsonl"),
          [{"type": "brand_new_record", "timestamp": ts(15, i),
            "payload": {"type": "mystery", "blob": "x" * 4096}}
           for i in range(12)])

        # 挂载临时目录
        S.CODEX_SESSIONS = os.path.join(tmp, "codex")
        S.CLAUDE_PROJECTS = os.path.join(tmp, "claude")
        S.EXTRA_SOURCES[:] = [{"name": "Pi", "engine": "pi", "dir":
                               os.path.join(tmp, "pi")}]
        S._HEALTH_CACHE["bad"] = None
        S._CACHE.clear()
        time.sleep(0.01)

        ss = {x["session_id"]: x for x in S.scan_all("all")
              if x["engine"] in ("codex", "pi")}

        # 断言 1：老格式 sid 与对话轮次
        check("老格式 sid 正确", old_sid in ss, str(sorted(ss)[:2]))
        h = S.get_session(old_sid, 30)
        check("老格式读出 1 问 1 答",
              h and len(h["turns"]) == 2 and h["turns"][0]["text"] == "老格式提问",
              repr(h and h["turns"]))

        # 断言 2：新格式走 response_item、注入被过滤、task_complete 定 done
        check("新格式 sid 正确", new_sid in ss, str(sorted(ss)[:2]))
        check("新格式状态是已完成（最新记录说了算）",
              ss.get(new_sid, {}).get("status") == "done",
              str(ss.get(new_sid, {}).get("status")))
        h = S.get_session(new_sid, 30)
        check("新格式只留 1 问 1 答（注入全滤掉）",
              h and [t["text"] for t in h["turns"]] == ["新格式提问", "新格式回答"],
              repr(h and [t["text"] for t in h["turns"]]))

        # 断言 3：分叉文件名抠出结尾的新 UUID，且能命中详情
        check("分叉文件 sid = 结尾新 UUID（不是拼接错值）",
              child in ss and parent not in ss, str(sorted(ss)[:4]))
        h = S.get_session(child, 30)
        check("分叉文件读得出对话",
              h and len(h["turns"]) == 2 and h["turns"][0]["text"] == "分叉后的第一句",
              repr(h and h["turns"]))

        # 断言 4：pi 接入 + 空 content 占位不算一轮
        check("pi 会话进列表且 sid 干净", pi_sid in ss, str(sorted(ss)[:4]))
        h = S.get_session(pi_sid, 30)
        check("pi 读出 1 问 1 答（空占位跳过）",
              h and [t["text"] for t in h["turns"]] == ["pi 的提问", "pi 的回答"],
              repr(h and [t["text"] for t in h["turns"]]))

        # 断言 5：parser_health 对不可识别的大文件报警，正常文件不误报。
        # 自检是后台线程刷的：首查返回空，轮着等它算完（最多 ~10 秒）
        names = []
        for _ in range(50):
            names = [b["session_id"] for b in S.parser_health()]
            if names:
                break
            time.sleep(0.2)
        check("自检抓到不可识别文件",
              any("aaaa1111" in n for n in names), str(names))
        check("自检不误报正常会话",
              all(not n.startswith(("019fe21d", "01a02cb6", "01a02d1f", "01a03323"))
                  for n in names), str(names))
    finally:
        S.CODEX_SESSIONS, S.CLAUDE_PROJECTS = real_codex, real_claude
        S.EXTRA_SOURCES[:] = real_pi
        S._HEALTH_CACHE["bad"] = None
        S._CACHE.clear()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 50)
    print("通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    print("=" * 50)
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
