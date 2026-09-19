"""Antigravity 适配器回归：仅使用临时数据库，不调用 AI、不修改真实会话。"""
import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import antigravity_sessions as A
import session_scanner as S


def _enc_v(v):
    out = b""
    while True:
        b7 = v & 0x7F
        v >>= 7
        if v:
            out += bytes([b7 | 0x80])
        else:
            return out + bytes([b7])


def _enc(fn, wt, payload):
    tag = _enc_v((fn << 3) | wt)
    if wt == 2:
        return tag + _enc_v(len(payload)) + payload
    return tag + payload


def _step_payload(step_type, text, ts=1789740949):
    """按实测字段号构造 steps.step_payload：1=类型 5.1.1=秒 19.2=用户文 20.3=助手文。"""
    body = _enc(1, 0, _enc_v(step_type))
    body += _enc(5, 2, _enc(1, 2, _enc(1, 0, _enc_v(ts))))
    if step_type == A.STEP_USER:
        body += _enc(19, 2, _enc(2, 2, text.encode("utf-8")))
    else:
        body += _enc(20, 2, _enc(3, 2, text.encode("utf-8")))
    return body


UUID = "0fd1d05b-41f9-4e09-a145-18a088c79e38"
UUID_KILLED = "11111111-2222-3333-4444-555555555555"
TIME_STR = "2026-09-19 05:06:02.2320262+00:00"


class AntigravityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        conv_dir = os.path.join(self.tmp.name, "conversations")
        os.makedirs(conv_dir)
        self.summary = os.path.join(self.tmp.name, "conversation_summaries.db")
        with sqlite3.connect(self.summary) as db:
            db.execute("CREATE TABLE conversation_summaries("
                       "conversation_id TEXT PRIMARY KEY, title TEXT, preview TEXT,"
                       " last_modified_time TEXT, workspace_uris TEXT, project_id TEXT,"
                       " killed INTEGER)")
            db.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?,?)",
                       (UUID, "示例会话", "示例预览", TIME_STR, "", "outside-of-project", 0))
            db.execute("INSERT INTO conversation_summaries VALUES(?,?,?,?,?,?,?)",
                       (UUID_KILLED, "已杀会话", "", TIME_STR, "", "outside-of-project", 1))
        db.close()
        with sqlite3.connect(os.path.join(conv_dir, UUID + ".db")) as db:
            db.execute("CREATE TABLE steps(idx INTEGER PRIMARY KEY, step_type INTEGER,"
                       " step_payload BLOB)")
            db.execute("INSERT INTO steps VALUES(0, ?, ?)",
                       (A.STEP_USER, _step_payload(A.STEP_USER, "第一句", 1789740949)))
            db.execute("INSERT INTO steps VALUES(1, ?, ?)",
                       (132, b"\x0a\x0bcall_673737"))          # 工具调用：不该出现在对话里
            db.execute("INSERT INTO steps VALUES(2, ?, ?)",
                       (A.STEP_ASSISTANT, _step_payload(A.STEP_ASSISTANT, "第一答", 1789740960)))
        db.close()

    def _patch(self):
        return patch.multiple(A, SUMMARY_DB=self.summary, CONV_DIR=os.path.join(self.tmp.name, "conversations"))

    def test_scan_metadata_and_filter(self):
        with self._patch():
            rows = A.scan()
        self.assertEqual([r["session_id"] for r in rows], [UUID])
        self.assertEqual(rows[0]["custom_title"], "示例会话")
        self.assertFalse(rows[0]["can_run"])
        self.assertEqual(rows[0]["engine"], "antigravity")
        # 7 位小数的 UTC 时间要能解析成 epoch 秒
        self.assertAlmostEqual(rows[0]["last_time"], 1789794362.232026, places=3)

    def test_history_text_order(self):
        with self._patch():
            data = A.history(UUID)
        self.assertEqual([t["text"] for t in data["turns"]], ["第一句", "第一答"])
        self.assertEqual([t["role"] for t in data["turns"]], ["user", "assistant"])
        self.assertEqual(data["turns"][0]["time"], 1789740949)
        self.assertFalse(data["can_run"])

    def test_is_managed_and_guards(self):
        with self._patch():
            self.assertTrue(A.is_managed(UUID))
            self.assertFalse(A.is_managed("99999999-2222-3333-4444-555555555555"))
            self.assertFalse(A.is_managed("sess_demo"))
            self.assertIsNone(A.history("sess_' OR 1=1 --"))
            self.assertIsNone(A.history("99999999-2222-3333-4444-555555555555"))

    def test_database_unchanged(self):
        conv = os.path.join(self.tmp.name, "conversations", UUID + ".db")
        before = (hashlib.sha256(Path(self.summary).read_bytes()).hexdigest(),
                  hashlib.sha256(Path(conv).read_bytes()).hexdigest())
        with self._patch():
            A.scan()
            A.history(UUID)
            A.is_managed(UUID)
        after = (hashlib.sha256(Path(self.summary).read_bytes()).hexdigest(),
                 hashlib.sha256(Path(conv).read_bytes()).hexdigest())
        self.assertEqual(before, after)
        with A._connect(self.summary) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM conversation_summaries")

    def test_missing_and_invalid_database_fails_closed(self):
        with patch.multiple(A, SUMMARY_DB=os.path.join(self.tmp.name, "missing.db"),
                            CONV_DIR=os.path.join(self.tmp.name, "none")):
            self.assertEqual(A.scan(), [])
            self.assertIsNone(A.history(UUID))
            self.assertFalse(A.is_managed(UUID))
        bad = os.path.join(self.tmp.name, "invalid.sqlite")
        Path(bad).write_bytes(b"not a sqlite database")
        self.assertEqual(A.scan(bad), [])

    def test_scanner_integration(self):
        with self._patch(), \
             patch.object(S, "scan_claude_sessions", return_value=[]), \
             patch.object(S, "scan_codex_sessions", return_value=[]), \
             patch.object(S, "scan_extra_sessions", return_value=[]), \
             patch.object(S, "_line_names", return_value={}), \
             patch.object(S, "_hidden_sids", return_value=set()):
            rows = S.scan_all("all")
            self.assertTrue(any(r["engine"] == "antigravity" for r in rows))
            self.assertEqual(S.get_session(UUID)["engine"], "antigravity")
            self.assertEqual(S.find_session_files(UUID), [])


if __name__ == "__main__":
    unittest.main()
