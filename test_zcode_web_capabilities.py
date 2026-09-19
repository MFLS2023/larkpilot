"""合成会话回归：筛选分类和来源权限，不调用模型或读取真实会话。"""
import unittest
from unittest.mock import patch

import web_server as W


class ZcodeWebCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.key = W.app.secret_key
        W.app.secret_key = "isolated-capability-test-key"
        self.addCleanup(setattr, W.app, "secret_key", self.key)
        self.client = W.app.test_client()
        with self.client.session_transaction() as session:
            session["auth"] = "ok"
        self.row = dict(session_id="sess_fixture", engine="zcode", client="Zcode",
                        title="测试", project="fixture", first_msg="", cwd="fixture",
                        status="idle", level="recent", last_time=1, last_msg="",
                        can_run=False)
        for target, value in (
            ("S.scan_all_cached", [self.row]), ("S.parser_health", []),
            ("L.locked_set", set()), ("S.get_usage", {}),
            ("_supported_engines", ["claude", "codex", "pi", "zcode"]),
            ("load_web_config", {"default_perm": "read"}),
        ):
            mocker = patch("web_server." + target, return_value=value)
            mocker.start()
            self.addCleanup(mocker.stop)

    def test_zcode_filter_preserves_session_and_capability(self):
        response = self.client.get("/api/sessions?kind=zcode")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertIn("zcode", data["kinds"])
        self.assertEqual(data["count"], 1)
        self.assertFalse(data["sessions"][0]["can_run"])

    def test_zcode_not_in_claude_code_filter(self):
        data = self.client.get("/api/sessions?kind=claude-code").get_json()
        self.assertEqual(data["count"], 0)

    def test_source_readonly_overrides_engine_support(self):
        history = dict(self.row, turns=[])
        with patch.object(W.S, "get_session", return_value=history):
            response = self.client.get("/api/sessions/sess_fixture")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["can_run"])
        self.assertFalse(response.get_json()["can_trash"])


if __name__ == "__main__":
    unittest.main()
