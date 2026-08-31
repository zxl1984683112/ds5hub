# -*- coding: utf-8 -*-
"""FastAPI Web 管理接口（M2+ 增强）：
- 状态（含 hidhide/reconnect/components）
- 手柄操作（连接/断开/重连/隐藏）
- HidHide 控制（白名单/隐藏/取消隐藏 — 支持 per-pad + global）
- 组件检测
- 日志/配置/自启
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Depends, HTTPException, Header, Body, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import logger
from .app import DS5HubApp
from .autostart import is_enabled as autostart_is_enabled
from .autostart import set_enabled as autostart_set_enabled


def create_app(app: DS5HubApp) -> FastAPI:
    fastapi_app = FastAPI(title="DS5Hub", version="0.1.0")

    token = app.cfg.get("web_token", "")

    def check_token(authorization: str | None = Header(default=None)):
        if not token:
            return True
        if authorization and authorization == f"Bearer {token}":
            return True
        raise HTTPException(status_code=401, detail="未授权")

    # ---- 状态 ----
    @fastapi_app.get("/api/status", dependencies=[Depends(check_token)])
    def api_status():
        result = app.status()
        # 同时返回组件检测结果
        try:
            result["components"] = app.check_components()
        except Exception:  # noqa: BLE001
            pass
        return result

    # ---- 手柄操作 ----
    class PadAction(BaseModel):
        action: str

    @fastapi_app.post("/api/pads/{pad_id}/action", dependencies=[Depends(check_token)])
    def pad_action(pad_id: str, action: str = Body(..., embed=True)):
        """动作: connect / disconnect / reconnect"""
        return app.set_pad_state(pad_id, action)

    # ---- HidHide 操作 ----
    @fastapi_app.get("/api/hidhide/status", dependencies=[Depends(check_token)])
    def api_hidhide_status():
        status = app.hidhide_status
        cli = app.hidhide_cli
        return {"cli_path": cli, "status": status or {"message": "未检测到 HidHide"}}

    class HidHideAction(BaseModel):
        action: str  # whitelist / unhide / check_hide

    @fastapi_app.post("/api/hidhide/action/{pad_id}", dependencies=[Depends(check_token)])
    def api_hidhide_action(pad_id: str, action: str = Body(..., embed=True)):
        """
        执行 HidHide 操作。
        pad_id 可以是具体手柄 ID，也可以是 "global"：
          - "global" + "whitelist" → 将所有手柄加入白名单
          - "global" + "unhide"   → 取消所有设备的隐藏
          - "global" + "check_hide" → 检查所有手柄的隐藏状态
          - 具体 pad_id           → 只对该手柄操作
        """
        from .hidhid_manager import (
            register_app_as_whitelisted, hide_devices_by_vid_pid, unhide_all
        )

        cli = app.hidhide_cli
        if not cli:
            return {"ok": False, "error": "HidHide CLI 不可用"}

        if action == "whitelist":
            ok = register_app_as_whitelisted(cli)
            msg = "已添加到白名单" if ok else "添加失败（HidHideCLI 未响应或驱动未加载）"
            resp = {"ok": ok, "message": msg, "mode": "global"}
            if not ok:
                resp["error"] = msg
            return resp

        elif action == "unhide":
            # global: 取消所有；per-pad: 查找对应手柄 VID/PID
            if pad_id == "global":
                ok = unhide_all(cli)
                return {"ok": ok, "message": "已取消全部隐藏" if ok else "取消隐藏失败"}
            else:
                # 查该手柄的 info
                slot = app.pads.get(pad_id)
                if not slot:
                    return {"ok": False, "error": "handheld not found"}
                vid_hex = f"{slot.info.vid:04x}"
                pid_hex = f"{slot.info.pid:04x}"
                result = hide_devices_by_vid_pid(cli, vid_hex, pid_hex)
                return {"ok": result.status.value != "device_not_hidden",
                        "message": result.message, "mode": "per_pad"}

        elif action == "check_hide":
            if pad_id == "global":
                slots = list(app.pads.list())
                results = []
                for s in slots:
                    r = hide_devices_by_vid_pid(
                        cli, f"{s.info.vid:04x}", f"{s.info.pid:04x}")
                    results.append({"pad": s.info.name, "hidden": r.status.value == "ok"})
                return {"ok": True, "results": results, "mode": "global"}
            else:
                slot = app.pads.get(pad_id)
                if not slot:
                    return {"ok": False, "error": "handheld not found"}
                vid_hex = f"{slot.info.vid:04x}"
                pid_hex = f"{slot.info.pid:04x}"
                result = hide_devices_by_vid_pid(cli, vid_hex, pid_hex)
                return {"ok": result.status.value == "ok",
                        "message": result.message, "mode": "per_pad"}

        return {"ok": False, "error": f"unknown action: {action}"}

    # ---- 组件检测（独立端点，也可从 /api/status 获取） ----
    @fastapi_app.get("/api/components", dependencies=[Depends(check_token)])
    def api_components():
        return app.check_components()

    # ---- 一键环境部署 ----
    @fastapi_app.get("/api/environment", dependencies=[Depends(check_token)])
    def api_environment():
        return app.orchestrator.status()

    @fastapi_app.post("/api/environment/deploy", dependencies=[Depends(check_token)])
    def api_environment_deploy():
        return app.orchestrator.start()

    # ---- 手动本机 attach（自动 attach 失败后的重试入口） ----
    @fastapi_app.post("/api/pads/{pad_id}/attach", dependencies=[Depends(check_token)])
    def api_pad_attach(pad_id: str):
        return app.attach_pad(pad_id)

    # ---- 一键卸载 ----
    @fastapi_app.get("/api/uninstall", dependencies=[Depends(check_token)])
    def api_uninstall_status():
        return app.uninstaller.status()

    @fastapi_app.post("/api/uninstall", dependencies=[Depends(check_token)])
    async def api_uninstall(request: Request):
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        remove_components = bool(data.get("remove_components", False))
        remove_config = bool(data.get("remove_config", True))
        return app.uninstaller.start(
            remove_components=remove_components, remove_config=remove_config)

    # ---- 日志 ----
    @fastapi_app.get("/api/logs", dependencies=[Depends(check_token)])
    def api_logs(limit: int = 500, level: str = ""):
        logs = logger.get().recent(limit)
        if level:
            logs = [l for l in logs if l.get("level") == level]
        return {"logs": logs}

    # ---- 配置 ----
    @fastapi_app.get("/api/config", dependencies=[Depends(check_token)])
    def get_config():
        all_cfg = app.cfg.all()
        # 组装嵌套结构
        return {
            "usbip_host": all_cfg.get("usbip_host"),
            "usbip_base_port": all_cfg.get("usbip_base_port"),
            "usbip_backlog": all_cfg.get("usbip_backlog"),
            "web_host": all_cfg.get("web_host"),
            "web_port": all_cfg.get("web_port"),
            "web_token": all_cfg.get("web_token"),
            "reconnect": {
                "enabled": all_cfg.get("reconnect.enabled"),
                "initial_delay": all_cfg.get("reconnect.initial_delay"),
                "max_delay": all_cfg.get("reconnect.max_delay"),
                "max_retries": all_cfg.get("reconnect.max_retries"),
                "retry_backoff": all_cfg.get("reconnect.retry_backoff"),
            },
            "log_level": all_cfg.get("log.level"),
            "demo_pad_count": all_cfg.get("demo.pad_count"),
        }

    class ConfigPatch(BaseModel):
        key: str
        value: Any = None

    @fastapi_app.post("/api/config", dependencies=[Depends(check_token)])
    def patch_config(payload: dict = Body(...)):
        key = str(payload.get("key", ""))
        value = payload.get("value")
        if not key:
            raise HTTPException(status_code=400, detail="key 必填")
        app.cfg.set(key, value)
        app.cfg.save()
        return {"ok": True, "key": key, "value": value}

    # ---- 开机自启 ----
    @fastapi_app.get("/api/autostart/status", dependencies=[Depends(check_token)])
    def autostart_status():
        return {"enabled": autostart_is_enabled()}

    @fastapi_app.post("/api/autostart", dependencies=[Depends(check_token)])
    async def set_autostart(request: Request):
        """接受 JSON Body {enabled: true}"""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        enabled_val = data.get("enabled")
        autostart_set_enabled(bool(enabled_val))
        return {"ok": True, "enabled": enabled_val}

    # ---- 日志级别设置 ----
    @fastapi_app.post("/api/logs/level", dependencies=[Depends(check_token)])
    async def set_log_level(request: Request):
        """接受 JSON Body {level: "INFO"}"""
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = {}
        lvl = data.get("level", "INFO")
        logger.get().set_level(str(lvl))
        return {"ok": True, "level": logger.get().get_level()}

    # ---- 静态前端 ----
    static_dir = Path(__file__).parent / "web" / "static"
    fastapi_app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @fastapi_app.get("/")
    def index():
        return FileResponse(str(static_dir / "index.html"))

    @fastapi_app.get("/test")
    def controller_test():
        return FileResponse(str(static_dir / "controller_test.html"))

    return fastapi_app
