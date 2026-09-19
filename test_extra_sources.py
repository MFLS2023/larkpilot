"""第三方来源配置回归测试：全部使用内存样本，不读真实会话。"""
import io
import json
import os
import unittest
from unittest.mock import patch

import session_scanner as scanner


class ExtraSourceTests(unittest.TestCase):
    def load(self, data):
        with patch.object(scanner.io, "open", return_value=io.StringIO(json.dumps(data))):
            return scanner.load_extra_sources()

    def test_home_directory_expansion(self):
        sources = self.load({"sources": [{
            "name": "Example", "engine": "example", "dir": "~/sessions",
        }]})
        self.assertEqual(sources[0]["dir"], os.path.expanduser("~/sessions"))
        self.assertFalse(sources[0]["can_run"])

    def test_absolute_directory_preserved(self):
        directory = os.path.abspath("sample-sessions")
        sources = self.load({"sources": [{
            "name": "Example", "engine": "example", "dir": directory,
        }]})
        self.assertEqual(sources[0]["dir"], directory)

    def test_invalid_config_keeps_defaults(self):
        self.assertEqual(self.load({"sources": [None, {}]}), scanner._DEFAULT_EXTRA_SOURCES)


if __name__ == "__main__":
    unittest.main()
