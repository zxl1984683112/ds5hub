# -*- coding: utf-8 -*-
"""
一键环境部署编排器 + 自动 attach 测试（开发机零驱动安全：全部 dry_run / 无客户端）。

覆盖：
- 编排器 dry_run 全流程状态机 -> DONE（msiexec/验证/白名单全部模拟）
- redist 目录探测与占位 msi 生成
- find_usbip_cli / service_running 无客户端环境不崩溃
- API：GET /api/environment、POST /api/environment/deploy（dry_run）
- API：POST /api/pads/{id}/attach（无 usbip 客户端 -> 明确报错）
- app.status() 包含 orchestrator / attach 视图
"""
import sys
sys.path.insert(0, '.')

from fastapi.testclient import TestClient
from ds5hub.app import DS5HubApp
from ds5hub.config import Config
from ds5hub.web_api import create_app
from ds5hub.logger import init
from ds5hub.install_orchestrator import InstallOrchestrator, find_usbip_cli, service_running


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

    print("\n== 1. 编排器 dry_run 状态机 ==")
    orch = InstallOrchestrator(cfg, dry_run=True)
    check("初始状态 IDLE", orch.status()["state"] == "idle")
    r = orch.start()
    check("start() 返回 ok", r["ok"] is True)
    for _ in range(150):
        st = orch.status()
        if st["state"] in ("done", "failed", "needs_reboot"):
            break
        import time; time.sleep(0.1)
    st = orch.status()
    check("dry_run 全流程 -> DONE", st["state"] == "done", st)
    check("installed 两项为真", st["installed"]["hidhide"] and st["installed"]["usbipd"], st["installed"])
    check("进度 100", st["progress"] == 100, st["progress"])
    check("verify 含 dry_run 标记", st["verify"].get("dry_run") is True, st["verify"])

    print("\n== 2. redist 目录与占位 msi ==")
    import os
    d = orch._redist_dir()
    check("redist 目录可写", os.path.isdir(d), str(d))
    fake = d / "_dryrun_hidhide.msi"
    check("占位 msi 已生成", fake.exists(), str(fake))

    print("\n== 3. 无客户端环境探测 ==")
    us = find_usbip_cli()
    check("find_usbip_cli 不崩溃（可为空串）", isinstance(us, str))
    svc = service_running("NefariusHidHide")
    check("service_running 返回 bool", isinstance(svc, bool))

    print("\n== 4. Web API ==")
    r1 = client.get("/api/environment")
    check("GET /api/environment 200", r1.status_code == 200, r1.text[:200])
    env = r1.json()
    check("environment 字段齐全", all(k in env for k in
          ("state", "progress", "message", "dry_run", "running", "log")), env.keys())

    r2 = client.post("/api/environment/deploy")
    check("POST deploy 200", r2.status_code == 200, r2.text[:200])
    deploy = r2.json()
    check("deploy 返回 ok", deploy.get("ok") is True, deploy)

    # 等待 app 内 orchestrator 跑完（后台线程）
    import time
    for _ in range(200):
        st = client.get("/api/environment").json()
        if st["state"] in ("done", "failed", "needs_reboot"):
            break
        time.sleep(0.1)
    st = client.get("/api/environment").json()
    check("app 内编排器 dry_run -> DONE", st["state"] == "done", st)

    print("\n== 5. attach API（无 usbip 客户端）==")
    pads = app.pads.list()
    if pads:
        pid = pads[0].info.pad_id
        r = client.post(f"/api/pads/{pid}/attach")
        j = r.json()
        check("attach 返回明确错误（客户端不可用）",
              j.get("ok") is False and "usbip" in j.get("error", ""), j)
    else:
        r = client.post("/api/pads/nonexistent/attach")
        check("无手柄时 attach 返回 pad not found",
              r.json().get("ok") is False, r.text[:200])

    print("\n== 6. status 视图集成 ==")
    s = client.get("/api/status").json()
    check("status 含 orchestrator 视图",
          "orchestrator" in s and s["orchestrator"]["state"] == "done", s.keys())
    check("status 含 attach 视图", "attach" in s and "results" in s["attach"], s.keys())

    print(f"\n结果: {results['pass']} 通过, {results['fail']} 失败")
    return results["fail"] == 0


if __name__ == "__main__":
    ok = run_test()
    sys.exit(0 if ok else 1)
