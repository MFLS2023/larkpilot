# -*- coding: utf-8 -*-
"""LarkPilot 公网安全隧道与网络信息模块。

核心目标：解决手机在不同网络（5G流量、校园网隔离、离开家）无法连接电脑的问题。
原理：
1. 启动 Cloudflare Quick Tunnel，自动打通一条免配路由器、免公网IP的加密隧道。
2. 抓取生成的 https://xxxx.trycloudflare.com 专属网址，持久化存盘。
3. 自动识别校正本机真实局域网 IP、Tailscale IP，提供全场景访问指引。
"""

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "tunnel_state.json")
LOG_PATH = os.path.join(HERE, "tunnel.log")

CLOUDFLARED_CANDIDATES = [
    shutil.which("cloudflared"),
    os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "cloudflared", "cloudflared.exe"),
    os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "cloudflared", "cloudflared.exe"),
    os.path.join(HERE, "cloudflared.exe"),
]


def log(msg):
    line = "[%s] [Tunnel] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def find_cloudflared_bin():
    """寻找本机安装的 cloudflared.exe 路径。"""
    for p in CLOUDFLARED_CANDIDATES:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def read_tunnel_state():
    """读取当前隧道状态。"""
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with io.open(STATE_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return {}


def save_tunnel_state(state):
    """保存当前隧道状态。"""
    try:
        with io.open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log("保存隧道状态失败: %s" % e)


def get_lan_ips():
    """获取本机有效网络 IP 列表，剔除回环和虚拟网卡干扰。"""
    candidates = []
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            # 过滤常见的干扰虚拟网卡（例如 28.0.0.1）
            if ip == "28.0.0.1":
                continue
            candidates.append(ip)
    except Exception:
        pass
    return candidates


def get_network_info(port=58080):
    """获取所有连接通道的汇总信息，供飞书推送和前端展示。"""
    state = read_tunnel_state()
    tunnel_url = state.get("url") if state.get("status") == "online" else None

    all_ips = get_lan_ips()
    tailscale_ip = next((ip for ip in all_ips if ip.startswith("100.")), None)
    # 校园网/家庭局域网通常是 10.x, 192.168.x, 172.16-31.x
    lan_ip = next((ip for ip in all_ips if ip.startswith("192.168.") or ip.startswith("10.")), None)
    if not lan_ip and all_ips:
        # 取除 Tailscale 之外的第一个
        non_ts = [ip for ip in all_ips if not ip.startswith("100.")]
        lan_ip = non_ts[0] if non_ts else all_ips[0]

    lines = ["📱 LarkPilot 手机连接地址："]
    if tunnel_url:
        lines.append("🌐 公网加密隧道（异网/5G流量/免连同一Wi-Fi）：\n   %s" % tunnel_url)
    else:
        lines.append("🌐 公网加密隧道：正在启动中或未开启...")

    if lan_ip:
        lines.append("🏠 局域网直连（同Wi-Fi直连）：\n   http://%s:%d" % (lan_ip, port))

    if tailscale_ip:
        lines.append("🔒 Tailscale 私有网（手机开启Tailscale时）：\n   http://%s:%d" % (tailscale_ip, port))

    return {
        "tunnel_url": tunnel_url,
        "lan_ip": lan_ip,
        "tailscale_ip": tailscale_ip,
        "all_ips": all_ips,
        "port": port,
        "summary_text": "\n\n".join(lines),
    }


class TunnelRunner:
    """管理 Cloudflare Quick Tunnel 进程。"""

    def __init__(self, port=58080):
        self.port = port
        self.bin_path = find_cloudflared_bin()
        self.process = None
        self.running = False
        self.url = None
        self._loop_thread = None

    def start(self):
        if not self.bin_path:
            log("未找到 cloudflared.exe，无法启动公网隧道")
            save_tunnel_state({
                "status": "error",
                "error": "cloudflared not found",
                "url": None,
                "port": self.port,
            })
            return False

        self.running = True
        self._loop_thread = threading.Thread(target=self._retry_loop, daemon=True)
        self._loop_thread.start()
        return True

    def _spawn_process(self):
        log("启动 Cloudflare Tunnel，目标端口: %d..." % self.port)
        cmd = [
            self.bin_path,
            "tunnel",
            "--url", ("http://127.0.0.1:%d" % self.port),
            "--no-autoupdate",
        ]
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    def _retry_loop(self):
        # 严格过滤，排除 api.trycloudflare.com
        url_pattern = re.compile(r"https://(?!api\.)[a-zA-Z0-9-]+\.trycloudflare\.com")

        while self.running:
            try:
                self.process = self._spawn_process()
                log("Cloudflare 进程已拉起，PID: %d" % self.process.pid)
            except Exception as e:
                log("拉起 Cloudflare 进程失败: %s" % e)
                time.sleep(5)
                continue

            got_url = False
            while self.running and self.process and self.process.poll() is None:
                line = self.process.stderr.readline()
                if not line:
                    time.sleep(0.1)
                    continue

                if "error" in line.lower() or "fail" in line.lower():
                    log("cloudflared: %s" % line.strip())

                m = url_pattern.search(line)
                if m:
                    candidate = m.group(0)
                    if "api.trycloudflare.com" not in candidate:
                        self.url = candidate
                        got_url = True
                        log("🎉 成功获取公网隧道网址: %s" % self.url)
                        save_tunnel_state({
                            "status": "online",
                            "url": self.url,
                            "port": self.port,
                            "pid": self.process.pid,
                            "started_at": int(time.time()),
                        })
                        self._notify_feishu_url(self.url)

            if not self.running:
                break

            exit_code = self.process.poll() if self.process else None
            log("Cloudflare 隧道进程已退出 (code: %s)，5 秒后自动重试..." % exit_code)
            save_tunnel_state({
                "status": "reconnecting",
                "url": None,
                "port": self.port,
            })
            time.sleep(5)

    def _notify_feishu_url(self, url):
        """当公网网址可用时，主动向飞书群推送一次卡片。"""
        try:
            import auth as AUTH
            bound_chats = AUTH.load_auth().get("bound_chats", {})
            if not bound_chats:
                return

            import config as CFG
            cfg = CFG.load_config()
            app_id = cfg.get("app_id")
            app_secret = cfg.get("app_secret")
            if not app_id or not app_secret:
                return

            import lark_oapi as lark
            import bridge as B

            client = (lark.Client.builder()
                      .app_id(app_id)
                      .app_secret(app_secret)
                      .log_level(lark.LogLevel.ERROR)
                      .build())

            info = get_network_info(self.port)
            msg_text = "🟢 LarkPilot 公网隧道已建立！\n手机无论在什么网络，点击即可进入控制台：\n%s\n\n（在飞书发送 /网址 可随时再次获取）" % url

            for chat_id in bound_chats:
                try:
                    B.send_msg(client, chat_id, msg_text)
                    log("已向飞书群 %s 推送最新公网网址" % chat_id)
                except Exception as ex:
                    log("推送飞书消息失败: %s" % ex)
        except Exception as e:
            log("飞书推送异常: %s" % e)

    def stop(self):
        self.running = False
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=3)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        save_tunnel_state({
            "status": "stopped",
            "url": None,
            "port": self.port,
        })
        log("隧道已停止")


def run_standalone():
    """独立运行隧道守护。"""
    try:
        import web_server as W
        port = int(W.load_web_config().get("port") or 58080)
    except Exception:
        port = 58080

    runner = TunnelRunner(port=port)
    if not runner.start():
        sys.exit(1)

    try:
        while True:
            time.sleep(1)
            if not runner.running:
                log("检测到隧道异常退出，3秒后尝试重启...")
                time.sleep(3)
                runner.start()
    except KeyboardInterrupt:
        runner.stop()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "info":
        info = get_network_info()
        print(info["summary_text"])
    else:
        run_standalone()
