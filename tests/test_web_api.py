# -*- coding: utf-8 -*-
"""Web API 冒烟测试（M1）"""
import os
import sys

sys.path.insert(0, r"D:\QwenpawWorkspace\project\ds5hub")
from fastapi.testclient import TestClient

# 先初始化日志和 app
from ds5hub import logger
logger.init(level="DEBUG")
from ds5hub.app import DS5HubApp
from ds5hub.web_api import create_app

app = DS5HubApp()
app.start()
client = TestClient(create_app(app))

# 1. 状态（真实模式：无手柄时 pads 为空列表）
r = client.get("/api/status")
assert r.status_code == 200, r.text
s = r.json()
print("status ok:", s["status"], "mode:", s["mode"], "pads:", len(s["pads"]))
assert s["mode"] == "real", "模拟配置应已移除, mode 必须为 real"
assert isinstance(s["pads"], list)

# 2. 手柄操作：有手柄走成功路径，无手柄验证错误路径
if s["pads"]:
    pad = s["pads"][0]
    r = client.post(f"/api/pads/{pad['pad_id']}/action", json={"action": "connect"})
    print("connect:", r.json())
    assert r.json()["ok"] is True
    r = client.post(f"/api/pads/{pad['pad_id']}/action", json={"action": "disconnect"})
    print("disconnect:", r.json())
else:
    r = client.post("/api/pads/no-such-pad/action", json={"action": "connect"})
    print("no pads (real mode), unknown pad connect:", r.json())
    assert r.json().get("ok") is False

# 4. 日志
r = client.get("/api/logs?limit=20")
assert r.status_code == 200
print("logs:", len(r.json()["logs"]), "条")

# 5. 配置
r = client.post("/api/config", json={"key": "usbip_base_port", "value": 4000})
assert r.json()["ok"] is True
print("config saved")

# 6. 自启状态
r = client.get("/api/autostart/status")
print("autostart:", r.json())

# 7. 首页
r = client.get("/")
assert r.status_code == 200 and "DS5Hub" in r.text
print("index page ok")

app.stop()
print("\n=== Web API 冒烟测试全部通过 ===")