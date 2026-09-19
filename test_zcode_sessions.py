"""Zcode 适配器回归：仅使用临时数据库，不调用 AI、不修改真实会话。"""
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zcode_sessions as Z
import session_scanner as S


class ZcodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sessions.sqlite")
        with sqlite3.connect(self.path) as db:
            db.executescript('''
                CREATE TABLE session(id TEXT PRIMARY KEY,directory TEXT,title TEXT,time_updated INTEGER,time_archived INTEGER);
                CREATE TABLE message(id TEXT PRIMARY KEY,session_id TEXT,data TEXT,time_created INTEGER,sequence INTEGER);
                CREATE TABLE part(id TEXT PRIMARY KEY,session_id TEXT,message_id TEXT,data TEXT,time_created INTEGER,sequence INTEGER);
            ''')
            db.execute("INSERT INTO session VALUES(?,?,?,?,?)", ("sess_demo", self.tmp.name, "示例", 1700000000000, None))
            db.execute("INSERT INTO session VALUES(?,?,?,?,?)", ("sess_archived", self.tmp.name, "归档", 1700000000000, 1))
            for index, role in enumerate(("user", "assistant")):
                mid = "m%d" % index
                db.execute("INSERT INTO message VALUES(?,?,?,?,?)", (mid, "sess_demo", json.dumps({"role": role}), 1700000000000 + index, index))
                for order, (kind, text) in enumerate((("text", role), ("reasoning", "不应展示"), ("tool", "不应展示"))):
                    db.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", (mid + str(order), "sess_demo", mid, json.dumps({"type": kind, "text": text}), 1700000000000, order))
        db.close()

    def test_scan_metadata_and_archive(self):
        rows = Z.scan(self.path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "sess_demo")
        self.assertEqual(rows[0]["last_time"], 1700000000)
        self.assertFalse(rows[0]["can_run"])

    def test_history_text_order(self):
        data = Z.history("sess_demo", path=self.path)
        self.assertEqual([t["text"] for t in data["turns"]], ["user", "assistant"])

    def test_database_unchanged(self):
        before = hashlib.sha256(Path(self.path).read_bytes()).hexdigest()
        Z.scan(self.path)
        Z.history("sess_demo", path=self.path)
        self.assertEqual(hashlib.sha256(Path(self.path).read_bytes()).hexdigest(), before)
        with Z._connect(self.path) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM session")

    def test_missing_and_unknown(self):
        missing = os.path.join(self.tmp.name, "missing.sqlite")
        self.assertEqual(Z.scan(missing), [])
        self.assertFalse(os.path.exists(missing))
        self.assertIsNone(Z.history("sess_missing", path=self.path))
        self.assertIsNone(Z.history("sess_' OR 1=1 --", path=self.path))

    def test_scanner_integration(self):
        with patch.object(Z, "DB_PATH", self.path), patch.object(S, "scan_claude_sessions", return_value=[]), patch.object(S, "scan_codex_sessions", return_value=[]), patch.object(S, "scan_extra_sessions", return_value=[]), patch.object(S, "_line_names", return_value={}), patch.object(S, "_hidden_sids", return_value=set()):
            rows = S.scan_all("all")
            self.assertEqual(rows[0]["title"], "示例")
            self.assertEqual(S.get_session("sess_demo")["engine"], "zcode")
            self.assertEqual(S.find_session_files("sess_demo"), [])

    def test_invalid_database_fails_closed(self):
        path = os.path.join(self.tmp.name, "invalid.sqlite")
        Path(path).write_bytes(b"not a sqlite database")
        self.assertEqual(Z.scan(path), [])
        self.assertIsNone(Z.history("sess_demo", path=path))

    def test_uncommitted_rows_invisible_and_wal_visible(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("UPDATE session SET title='未提交' WHERE id='sess_demo'")
            self.assertEqual(Z.scan(self.path)[0]["custom_title"], "示例")
            db.commit()
            self.assertEqual(Z.scan(self.path)[0]["custom_title"], "未提交")
        finally:
            db.close()

    def test_synthetic_and_broken_parts_ignored(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", ("synthetic", "sess_demo", "m1", json.dumps({"type": "text", "text": "不应展示", "synthetic": True}), 1700000000000, 5))
            db.execute("INSERT INTO part VALUES(?,?,?,?,?,?)", ("broken", "sess_demo", "m1", "{", 1700000000000, 6))
            db.commit()
        finally:
            db.close()
        self.assertEqual([t["text"] for t in Z.history("sess_demo", path=self.path)["turns"]], ["user", "assistant"])


if __name__ == "__main__":
    unittest.main()
