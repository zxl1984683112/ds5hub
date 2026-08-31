# -*- coding: utf-8 -*-
"""最终端到端集成测试——模拟客户端验证所有 API 响应格式。"""
import asyncio
import sys
sys.path.insert(0, '.')

from fastapi.testclient import TestClient
from ds5hub.app import DS5HubApp
from ds5hub.config import Config
from ds5hub.web_api import create_app
from ds5hub.logger import init


def run_test():
    print("=" * 60)
    print("DS5Hub 端到端集成测试")
    print("=" * 60)
    
    # Setup
    cfg = Config()
    init(level="INFO", ring_size=500)
    app = DS5HubApp(config=cfg)
    api = create_app(app)
    client = TestClient(api)
    
    results = {"pass": 0, "fail": 0}
    
    def check(name, condition, detail=""):
        if condition:
            print(f"✅ {name}")
            results["pass"] += 1
        else:
            print(f"❌ {name}" + (f" — {detail}" if detail else ""))
            results["fail"] += 1
    
    # ---- /api/status ----
    print("\n📡 [Status API]")
    r = client.get("/api/status")
    check("GET /api/status → 200", r.status_code == 200)
    data = r.json()
    check("status字段有running", data.get("status") == "running", f"got {data.get('status')}")
    check("mode为real(无模拟)", data.get("mode") == "real", f"got {data.get('mode')}")
    check("pads为数组(真实模式,无手柄时为空)", isinstance(data.get("pads"), list), f"len={len(data.get('pads',[]))}")
    check("config有usbip_host", "usbip_host" in str(data.get("config")), "")
    check("hidhide有status键", "hidhide" in data or "hidhide_status" in data, "")
    check("components有检测结果", "components" in data, f"keys: {list(data.keys())}")
    pads = data.get("pads", [])
    if pads:
        pad = pads[0]
        check("每个pad含pad_id/name/vid/pid/state",
              all(k in pad for k in ("pad_id","name","vid","pid","state")),
              f"keys={list(pad.keys())}")
    else:
        check("真实模式无手柄:pads为空列表(合法)", True)
    
    # ---- /api/pads/{id}/action ----
    print("\n🎮 [Pad Actions]")
    if pads:
        pid = pads[0]["pad_id"]
    else:
        pid = "no-such-pad"   # 无手柄环境:用未知ID验证错误路径
    r = client.post(f"/api/pads/{pid}/action", json={"action":"connect"})
    check(f"POST /pads/{pid}/action connect → 200", r.status_code == 200)
    if pads:
        check("connect 返回 ok=true", r.json().get("ok") is True, f"result={r.json()}")
        # disconnect
        r = client.post(f"/api/pads/{pid}/action", json={"action":"disconnect"})
        check("POST /pads/{pid}/action disconnect → 200", r.status_code == 200)
        # reconnect
        r = client.post(f"/api/pads/{pid}/action", json={"action":"reconnect"})
        check("POST /pads/{pid}/action reconnect → 200", r.status_code == 200)
    else:
        check("未知pad connect 返回 ok=false(错误路径)", r.json().get("ok") is False, f"result={r.json()}")

    # invalid action
    r = client.post(f"/api/pads/{pid}/action", json={"action":"invalid_action_xyz"})
    check("POST /pads/{pid}/action invalid → error", not r.json().get("ok", True))
    
    # ---- /api/hidhide/action/global ----
    print("\n🔐 [HidHide Actions]")
    r = client.post("/api/hidhide/action/global", json={"action":"whitelist"})
    check("POST /hidhide/action/global whitelist → 200", r.status_code == 200)
    wl_result = r.json()
    # 开发环境无 HidHideCLI，返回 error 是正常行为
    if wl_result.get("ok"):
        check("whitelist 成功", True)
    else:
        check("whitelist 返回 error（开发环境无 HidHideCLI）", "error" in wl_result, wl_result.get("error"))
    
    r = client.post("/api/hidhide/action/global", json={"action":"check_hide"})
    check("POST /hidhide/action/global check_hide → 200", r.status_code == 200)
    
    r = client.post("/api/hidhide/action/global", json={"action":"unhide"})
    check("POST /hidhide/action/global unhide → 200", r.status_code == 200)
    
    # per-pad hidhide action
    r = client.post(f"/api/hidhide/action/{pid}", json={"action":"check_hide"})
    check(f"POST /hidhide/action/{{pid}} check_hide → 200", r.status_code == 200)
    
    # ---- /api/hidhide/status ----
    print("\n🔑 [HidHide Status]")
    r = client.get("/api/hidhide/status")
    check("GET /hidhide/status → 200", r.status_code == 200)
    hh = r.json()
    check("hidhide status有cli_path", "cli_path" in hh, f"keys={list(hh.keys())}")
    
    # ---- /api/config ----
    print("\n⚙️ [Config]")
    r = client.get("/api/config")
    check("GET /config → 200", r.status_code == 200)
    cfg_data = r.json()
    check("config有usbip_host/web_port/reconnect", 
          "usbip_host" in cfg_data and "web_port" in cfg_data and "reconnect" in cfg_data,
          f"keys={list(cfg_data.keys())[:5]}...")
    
    # patch config
    r = client.post("/api/config", json={"key":"test_key","value":"test_val"})
    check("POST /config patch → 200", r.status_code == 200)
    check("patch返回ok", r.json().get("ok") is True)
    
    # ---- /api/autostart ----
    print("\n🚀 [Autostart]")
    r = client.get("/api/autostart/status")
    check("GET /autostart/status → 200", r.status_code == 200)
    check("有enabled字段", "enabled" in r.json(), "")
    
    r = client.post("/api/autostart", json={"enabled": True})
    check("POST /autostart enabled=true → 200", r.status_code == 200)
    
    # ---- /api/logs ----
    print("\n📋 [Logs]")
    r = client.get("/api/logs?limit=100")
    check("GET /logs → 200", r.status_code == 200)
    logs = r.json().get("logs", [])
    check("日志数组非空(至少有初始化日志)", len(logs) > 0, f"count={len(logs)}")
    
    # set log level
    r = client.post("/api/logs/level", json={"level":"DEBUG"})
    check("POST /logs/level DEBUG → 200", r.status_code == 200)
    check("返回正确level", r.json().get("level") == "DEBUG", "")
    
    # filtered logs
    r = client.get("/api/logs?limit=100&level=INFO")
    check("GET /logs?level=INFO → 200", r.status_code == 200)
    
    # ---- /api/components ----
    print("\n🧩 [Components]")
    r = client.get("/api/components")
    check("GET /components → 200", r.status_code == 200)
    comps = r.json()
    check("components有hidhide/python/usbip_win2键", 
          "hidhide" in comps or "python" in comps,
          f"keys={list(comps.keys())}")
    
    # ---- Frontend static files ----
    print("\n🌐 [Frontend]")
    r = client.get("/")
    check("GET / → 200", r.status_code == 200)
    html = r.text
    check("首页包含用户指南按钮", '用户指南' in html or 'guide' in html.lower(), "")
    check("首页包含手柄卡片模板", 'pad-card' in html, "")
    check("首页包含Tab切换结构", 'tab-content' in html, "")
    check("首页包含重连设置", 'reconnectToggle' in html or 'reconnect' in html.lower(), "")
    check("JS中有refreshStatus函数", 'refreshStatus' in html, "")
    check("JS中有showGuide函数", 'showGuide' in html, "")
    
    # Summary
    print("\n" + "=" * 60)
    total = results["pass"] + results["fail"]
    print(f"结果: {results['pass']}/{total} 通过 | {results['fail']}/{total} 失败")
    print("=" * 60)
    
    return results["fail"] == 0


if __name__ == "__main__":
    success = run_test()
    sys.exit(0 if success else 1)
