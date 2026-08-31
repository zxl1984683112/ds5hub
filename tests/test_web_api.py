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

app = DS5HubApp(simulated=True)
app.start()
client = TestClient(create_app(app))

# 1. 状态
r = client.get("/api/status")
assert r.status_code == 200, r.text
s = r.json()
print("status ok:", s["status"], "mode:", s["mode"], "pads:", len(s["pads"]))
assert len(s["pads"]) >= 1

# 2. 手柄连接
pad = s["pads"][0]
r = client.post(f"/api/pads/{pad['pad_id']}/action", json={"action": "connect"})
print("connect:", r.json())
assert r.json()["ok"] is True

# 3. 断开
r = client.post(f"/api/pads/{pad['pad_id']}/action", json={"action": "disconnect"})
print("disconnect:", r.json())

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