# -*- coding: utf-8 -*-
"""调试 /api/logs/level 端点。"""
import asyncio
import sys
sys.path.insert(0, '.')

from fastapi.testclient import TestClient
from ds5hub.app import DS5HubApp
from ds5hub.config import Config
from ds5hub.web_api import create_app
from ds5hub.logger import init

cfg = Config()
init(level="INFO", ring_size=500)
app = DS5HubApp(config=cfg, simulated=True)
api = create_app(app)
client = TestClient(api)

print("Testing POST /api/logs/level...")
r = client.post("/api/logs/level", json={"level": "DEBUG"})
print(f"Status: {r.status_code}")
print(f"Headers: {dict(r.headers)}")
print(f"Body: {r.text}")
try:
    print(f"JSON: {r.json()}")
except Exception as e:
    print(f"JSON parse error: {e}")
