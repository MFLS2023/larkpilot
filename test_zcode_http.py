"""真实 HTTP 验证只读接口，不调用 AI，不读取真实会话或改写运行配置。"""
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from werkzeug.serving import make_server
import web_server as W


class ZcodeHTTPTests(unittest.TestCase):
    def test_readonly_send_rejected_without_lookup_or_task(self):
        app = W.app
        old_key = app.secret_key
        app.secret_key = "isolated-test-key-not-for-production"
        server = make_server("127.0.0.1", 0, app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        token = app.session_interface.get_signing_serializer(app).dumps({"auth": "ok"})
        base = "http://127.0.0.1:%d" % server.server_port
        try:
            with patch.object(W, "load_web_config", return_value=dict(W.WEB_CONFIG_DEFAULTS)), patch.object(W, "_engine_of", side_effect=AssertionError("只读请求不应依赖列表")), patch.object(W, "_make_task", side_effect=AssertionError("不应创建任务")) as make_task:
                for body in (
                    {"sid": "sess_hidden_fixture", "msg": "测试", "perm": "full"},
                    {"new": True, "engine": "zcode", "msg": "测试"},
                    {"sid": "sess_hidden_fixture", "new": True, "engine": "claude", "msg": "测试"},
                ):
                    request = urllib.request.Request(base + "/api/send", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Cookie": "session=" + token})
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        urllib.request.urlopen(request, timeout=5)
                    error = caught.exception
                    try:
                        self.assertEqual(error.code, 403)
                        self.assertIn("仅支持查看历史", json.load(error)["error"])
                    finally:
                        error.close()
                make_task.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            app.secret_key = old_key


if __name__ == "__main__":
    unittest.main()
