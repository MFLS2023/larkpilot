# -*- coding: utf-8 -*-
"""LarkPilot 服务管理器 —— start / stop / restart / status / doctor。

为什么要有这个：之前改一次代码就要人肉开 PowerShell 杀进程、拉启动脚本、
curl 验证，步骤全在脑子里。现在：

    python manager.py start            # 起桥 + 网页
    python manager.py stop             # 全停
    python manager.py restart          # 重启（先停后起）
    python manager.py restart web      # 只重启一个
    python manager.py status           # 谁活着、端口通不通、锁表心跳
    python manager.py doctor           # 深度体检：配置/白名单/凭据/测试入口

设计决定：
- 启动用 pythonw 直接脱离终端跑，日志追加到各自 .log —— 和开机 vbs 的效果
  一致，两条路可以混用不冲突
- 找进程靠 PowerShell 查命令行（python 进程很多，只有命令行能区分谁是谁）
- stop 前先看锁表：有任务在跑会拒绝并提示，防止把干活的掐死
"""

import io
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = {
    "bridge": {"script": "bridge.py", "log": "bridge.log"},
    "web": {"script": "web_server.py", "log": "web_server.log"},
}


def find_pids(script):
    """找出正在跑某个脚本的 python 进程 PID 列表。

    用 PowerShell 查 Win32_Process 的 CommandLine —— 任务管理器同款数据源。
    正则里 feishu-bridge 前缀保证不误伤别的 python 程序；
    目录和脚本名之间的分隔符是反斜杠，用 [\\\\/] 字符类匹配
    （写成 \\. 是错的 —— 那匹配的是点号，实测一个都抓不到）。
    """
    cmd = ("Get-CimInstance Win32_Process | Where-Object {"
           "$_.Name -match 'python' -and $_.CommandLine -match "
           "'feishu-bridge[\\\\/]%s'} | ForEach-Object { \"$($_.ProcessId)\" }" % script)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                             capture_output=True, text=True, timeout=30)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def locks_snapshot():
    try:
        d = json.loads(io.open(os.path.join(HERE, "session_locks.json"),
                               encoding="utf-8-sig").read())
        return {k: v.get("who") for k, v in d.items()}
    except Exception:
        return {}


def heartbeat_age():
    """桥心跳距今多少秒；读不到返回 None。"""
    try:
        hb = json.loads(io.open(os.path.join(HERE, "bridge_heartbeat.json"),
                                encoding="utf-8-sig").read())
        return int(time.time() - float(hb.get("ts") or 0)), bool(hb.get("net_ok"))
    except Exception:
        return None, None


def web_port():
    """网页端口从配置读，不写死 —— 用户改了端口体检才不会误报。"""
    try:
        d = json.loads(io.open(os.path.join(HERE, "web_config.json"),
                               encoding="utf-8-sig").read())
        return int(d.get("port") or 8000)
    except Exception:
        return 8000


def port_ok(port):
    import socket
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=2)
        s.close()
        return True
    except Exception:
        return False


def cmd_start(target):
    for name in SERVICES:
        if target not in ("all", name):
            continue
        pids = find_pids(SERVICES[name]["script"])
        if pids:
            print("  [%s] 已在跑（PID %s），跳过" % (name, ",".join(map(str, pids))))
            continue
        logp = os.path.join(HERE, SERVICES[name]["log"])
        DETACHED = 0x00000008  # DETACHED_PROCESS：关掉终端也不死
        with io.open(logp, "a", encoding="utf-8") as lf:
            subprocess.Popen(
                [sys.executable, os.path.join(HERE, SERVICES[name]["script"])],
                cwd=HERE, stdout=lf, stderr=subprocess.STDOUT,
                creationflags=DETACHED)
        print("  [%s] 已启动，日志 -> %s" % (name, SERVICES[name]["log"]))
    time.sleep(3)


def cmd_stop(target):
    # 锁表非空 = 有引擎子进程在干活（网页起的任务也记在锁表里）。
    # 不管停哪个都先拦一道 —— 掐死 web 进程同样会让正在跑的任务失去收件人。
    busy = locks_snapshot()
    if busy and "--force" not in sys.argv:
        print("!! 锁表非空，有任务在跑：%s\n   确认要掐就加 --force 重试"
              "（python manager.py stop %s --force）" % (busy, target))
        return 1
    for name in SERVICES:
        if target not in ("all", name):
            continue
        pids = find_pids(SERVICES[name]["script"])
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True)
        print("  [%s] %s" % (name, "已停止 %d 个进程" % len(pids) if pids else "本来没在跑"))
    return 0


def cmd_status():
    age, net = heartbeat_age()
    print("进程：")
    for name in SERVICES:
        pids = find_pids(SERVICES[name]["script"])
        print("  [%s] %s" % (name, "运行中 PID %s" % pids if pids else "未运行"))
    print("网页端口 %d：%s" % (web_port(),
                              "通" if port_ok(web_port()) else "不通"))
    if age is None:
        print("桥心跳：无记录")
    else:
        print("桥心跳：%d 秒前（到飞书网络%s）" % (age, "通" if net else "不通"))
    busy = locks_snapshot()
    print("锁表：%s" % ("空" if not busy else busy))


def cmd_doctor():
    """深度体检。每项都给「是什么、怎么修」，不看懂代码也能照着处理。"""
    print("== LarkPilot 体检 ==\n")

    ok = True

    def item(name, cond, fix=""):
        nonlocal ok
        print("  [%s] %s%s" % ("OK" if cond else "!!", name,
                               "" if cond else "\n      → %s" % fix))
        if not cond:
            ok = False

    # 配置文件
    for f in ("bridge_config.json", "web_config.json"):
        p = os.path.join(HERE, f)
        good = False
        why = "文件不存在"
        if os.path.isfile(p):
            try:
                json.loads(io.open(p, encoding="utf-8-sig").read())
                good = True
            except Exception as e:
                why = "JSON 解析失败：%s" % e
        item("%s 可解析" % f, good, why)

    # 飞书凭据存在（绝不打印值）。
    # 注意：app_id/app_secret 不在 bridge_config.json 里（那里只有 _密钥位置
    # 指引），config.load_config() 会把它们从 .env 合并进来 —— 必须查合并后的。
    try:
        import config as CFGM
        cfg = CFGM.load_config()
        item("飞书 app_id/app_secret 已填",
             bool(cfg.get("app_id")) and bool(cfg.get("app_secret")),
             "按 bridge_config.json 里 _密钥位置 的指引填入应用凭据")
        bad_dirs = [d for d in (cfg.get("allowed_dirs") or [])
                    if not os.path.isdir(d)]
        item("allowed_dirs 全部真实存在",
             not bad_dirs, "不存在的目录：%s（从配置删掉）" % bad_dirs)
    except Exception as e:
        item("配置加载", False, "config.load_config() 抛异常：%s" % e)

    # 工作目录白名单是安全边界，单独强调
    item("绑定关系文件存在（bridge_auth.json）",
         os.path.isfile(os.path.join(HERE, "bridge_auth.json")),
         "首次在飞书发消息并用绑定码绑定后会自动生成")

    age, net = heartbeat_age()
    item("桥心跳新鲜（≤60 秒）", age is not None and age <= 60,
         "桥没在跑或卡死：python manager.py restart bridge")
    if age is not None and age <= 60 and net is False:
        # 网络不通照常记为失败项 —— 不能因为它「不算服务的错」就把
        # 前面真实失败项的结论翻转成一切正常（实测踩过这个逻辑坑）
        item("到飞书网络连通", False,
             "桥活着但连不上飞书 —— 先查本机代理/TUN，服务本身可能没问题")

    port = web_port()
    item("网页端口 %d 响应" % port, port_ok(port),
         "python manager.py restart web")

    print("\n结论：%s" % ("一切正常" if ok else "有项要处理，见上面的 → 提示"))
    return 0 if ok else 1


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    target = sys.argv[2] if len(sys.argv) > 2 else "all"

    if action == "start":
        cmd_start(target if target in ("all", "bridge", "web") else "all")
    elif action == "stop":
        sys.exit(cmd_stop(target if target in ("all", "bridge", "web") else "all"))
    elif action == "restart":
        cmd_stop(target if target in ("all", "bridge", "web") else "all")
        time.sleep(1)
        cmd_start(target if target in ("all", "bridge", "web") else "all")
    elif action == "status":
        cmd_status()
    elif action == "doctor":
        sys.exit(cmd_doctor())
    else:
        print(__doc__)
        print("用法：python manager.py {start|stop|restart|status|doctor} [bridge|web|all]")


if __name__ == "__main__":
    main()
