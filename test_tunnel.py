# -*- coding: utf-8 -*-
"""公网隧道与网络检测单元测试。"""

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import tunnel


class TestTunnel(unittest.TestCase):

    def test_find_bin(self):
        bin_path = tunnel.find_cloudflared_bin()
        self.assertIsNotNone(bin_path, "cloudflared.exe 应该已被找到")
        self.assertTrue(os.path.isfile(bin_path), "找到的路径必须是真实存在的文件")

    def test_lan_ips_filtering(self):
        ips = tunnel.get_lan_ips()
        self.assertIsInstance(ips, list)
        self.assertNotIn("127.0.0.1", ips)
        self.assertNotIn("28.0.0.1", ips, "虚拟网卡 28.0.0.1 应该被成功过滤")

    def test_network_info_structure(self):
        info = tunnel.get_network_info(port=58080)
        self.assertIn("summary_text", info)
        self.assertIn("all_ips", info)
        self.assertIn("port", info)
        self.assertEqual(info["port"], 58080)
        self.assertIn("LarkPilot", info["summary_text"])

    def test_state_persistence(self):
        original_state = tunnel.read_tunnel_state()
        try:
            test_state = {
                "status": "online",
                "url": "https://test-example.trycloudflare.com",
                "port": 58080,
                "pid": 999999,
            }
            tunnel.save_tunnel_state(test_state)
            read_back = tunnel.read_tunnel_state()
            self.assertEqual(read_back.get("url"), test_state["url"])
            self.assertEqual(read_back.get("status"), "online")
        finally:
            tunnel.save_tunnel_state(original_state)


if __name__ == "__main__":
    unittest.main()
