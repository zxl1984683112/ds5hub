# -*- coding: utf-8 -*-
"""
一键卸载编排器测试（开发机零驱动安全：dry_run / 源码运行保护 / 假 stop 回调）。

覆盖：
- dry_run 全流程状态机 -> DONE（无任何真实系统变更）
- stop_callback 被调用、组件模拟卸载结果
- 注册表枚举 find_component_uninstall 无驱动环境不崩溃
- 源码运行自删保护（_self_delete 跳过）
- API：GET/POST /api/uninstall（dry_run）
- app.status() 包含 uninstaller 视图
"""
import sys
sys.path.insert(0, '.')

from fastapi.testclient import TestClient
from ds5hub.app import DS5HubApp
from ds5hub.config import Config
from ds5hub.web_api import create_app
from ds5hub.logger import init
from ds5hub.uninstall_orchestrator import (
    UninstallOrchestrator, find_component_uninstall)


def run_test():
    cfg = Config()
    cfg.set("orchestrator.dry_run", True)   # 开发机：全流程模拟
    init(level="INFO", ring_size=500)
    app = DS5HubApp(config=cfg)
    api = create_app(app)
    client = TestClient(api)

    results = {"pass": 0, "fail": 0}

    def check(name, condition, detail=""):
        if condition:
            results["pass"] += 1
            print(f"  ✓ {name}")
        else:
            results["fail"] += 1
            print(f"  ✗ {name}  {detail}")

    print("\n== 1. 卸载编排器 dry_run 全流程 ==")
    stopped = []
    o = UninstallOrchestrator(cfg, stop_callback=lambda: stopped.append(1),
                              dry_run=True)
    check("初始状态 IDLE", o.status()["state"] == "idle")
    r = o.start(remove_components=True)
    check("start() 返回 ok", r["ok"] is True)
    import time
    for _ in range(150):
        st = o.status()
        if st["state"] in ("done", "failed", "needs_reboot"):
            break
        time.sleep(0.1)
    st = o.status()
    check("dry_run 全流程 -> DONE", st["state"] == "done", st)
    check("stop_callback 恰好调用一次", stopped == [1], stopped)
    check("组件模拟卸载结果", st["removed_components"] ==
          {"hidhide": True, "usbipd": True}, st["removed_components"])
    check("进度 100", st["progress"] == 100, st["progress"])

    print("\n== 2. 重复卸载保护（运行中并发拒绝）==")
    import threading
    gate = threading.Event()

    def blocking_stop():
        gate.wait(5)          # 模拟服务停止耗时，让卸载线程保持运行
    o_gate = UninstallOrchestrator(cfg, stop_callback=blocking_stop, dry_run=True)
    o_gate.start()
    time.sleep(0.5)           # 等线程进入 stop_callback 阻塞
    r_gate = o_gate.start()
    check("运行中再 start 被拒绝", r_gate["ok"] is False, r_gate)
    gate.set()                # 放行，让线程跑完
    for _ in range(100):
        if o_gate.status()["state"] in ("done", "failed"):
            break
        time.sleep(0.1)

    print("\n== 3. 注册表枚举（无驱动环境）==")
    entries = find_component_uninstall()
    check("find_component_uninstall 不崩溃且返回 dict", isinstance(entries, dict),
          entries)
    # 开发机未装 HidHide/usbipd 时应为空；若本机装有其它软件同名组件也不影响
    print(f"    （本机找到 {len(entries)} 个匹配组件条目）")

    print("\n== 4. 源码运行自删保护 ==")
    o2 = UninstallOrchestrator(cfg, stop_callback=lambda: None, dry_run=False)
    try:
        o2._self_delete()
        check("非 frozen 环境跳过自删", True)
    except Exception as e:  # noqa: BLE001
        check("非 frozen 环境跳过自删", False, e)

    print("\n== 5. Web API ==")
    r1 = client.get("/api/uninstall")
    check("GET /api/uninstall 200", r1.status_code == 200, r1.text[:200])
    env = r1.json()
    check("uninstall 字段齐全", all(k in env for k in
          ("state", "progress", "message", "dry_run", "running", "log")),
          env.keys())

    # 用假 stop 回调替换真实 app.stop，避免影响后续断言
    app.uninstaller.stop_callback = lambda: None
    r2 = client.post("/api/uninstall",
                     json={"remove_components": False})
    check("POST /api/uninstall 200", r2.status_code == 200, r2.text[:200])
    check("POST 返回 ok", r2.json().get("ok") is True, r2.json())
    for _ in range(200):
        st = client.get("/api/uninstall").json()
        if st["state"] in ("done", "failed", "needs_reboot"):
            break
        time.sleep(0.1)
    st = client.get("/api/uninstall").json()
    check("app 内卸载器 dry_run -> DONE", st["state"] == "done", st)
    check("未勾选组件时保留组件", st["removed_components"] == {},
          st["removed_components"])

    print("\n== 6. status 视图集成 ==")
    s = client.get("/api/status").json()
    check("status 含 uninstaller 视图",
          "uninstaller" in s and s["uninstaller"]["state"] == "done", s.keys())

    print(f"\n结果: {results['pass']} 通过, {results['fail']} 失败")
    return results["fail"] == 0


if __name__ == "__main__":
    ok = run_test()
    sys.exit(0 if ok else 1)
