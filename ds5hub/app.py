# -*- coding: utf-8 -*-
"""
DS5Hub 核心服务协调器：把配置、日志、手柄管理、usbip 服务、Web API 组装起来。

M2 增强：
- HidHide 自动化检测与设备隐藏
- 自动重连策略（指数退避）
- 真实 hidapi 手柄枚举（开发机无库时回退模拟桩）
- 手柄/客户端两层故障恢复
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import logger
from .config import Config
from .hid_pad import HidPadManager, enumerate_ds5_pads, HidPad
from .pad_manager import (
    AbstractPadDevice, PadManager, PadInfo, default_simulated_pads,
    PadState, PadSlot, SimulatedPad
)
from .reconnect import AutoReconnector, ReconnectPolicy
from .usbip_server import PadUsbipServer


class DS5HubApp:
    def __init__(self, config: Config | None = None, simulated: bool = False):
        self.cfg = config or Config()
        self.simulated = simulated
        
        # ---- 手柄管理器 ----
        if simulated:
            self.pads = default_simulated_pads(count=self.cfg.get("demo.pad_count", 2))
            self._real_hid_mgr = None
        else:
            # 目标机：尝试 hidapi 枚举，失败回退模拟桩
            self._real_hid_mgr = HidPadManager()
            pads_found = self._real_hid_mgr.scan()
            if not pads_found:
                logger.warn("未发现真实 DualSense 手柄，回退到模拟模式")
                self.simulated = True
                self.pads = default_simulated_pads(1)
                self._real_hid_mgr = None
            else:
                self.pads = self._real_hid_mgr
        
        # ---- usbip 服务端口映射 ----
        self.servers: Dict[str, PadUsbipServer] = {}
        self._lock = threading.RLock()
        
        # ---- 自动重连 ----
        reconnect_cfg = self.cfg.get("reconnect", {})
        self.reconnector = AutoReconnector(ReconnectPolicy(
            enabled=reconnect_cfg.get("enabled", True),
            initial_delay=reconnect_cfg.get("initial_delay", 3.0),
            max_delay=reconnect_cfg.get("max_delay", 30.0),
            max_retries=reconnect_cfg.get("max_retries", 0),  # 0=无限
            retry_backoff=reconnect_cfg.get("retry_backoff", 2.0),
        ))
        
        # ---- HidHide 管理器 ----
        self._hidhide = self._init_hidhide()
    
    def _init_hidhide(self):
        """初始化 HidHide 管理器（目标机可用时）。"""
        try:
            from .hidhid_manager import detect_overall_status, get_cli_path
            cli = get_cli_path()
            status = detect_overall_status(cli)
            if status.status.value == "ok":
                logger.info("HidHide 驱动就绪")
                return {"cli": cli, "status": status}
        except Exception as e:
            logger.debug(f"HidHide 初始化跳过: {e}")
        return {"cli": "", "status": None}
    
    @property
    def hidhide_cli(self) -> str:
        return self._hidhide.get("cli", "")
    
    @property
    def hidhide_status(self) -> Optional[Dict[str, Any]]:
        s = self._hidhide.get("status")
        if s is None:
            return None
        return {"status": s.status.value, "message": s.message}
    
    # ---- 启动 ----
    def start(self) -> None:
        logger.info("DS5Hub 核心启动")
        self.pads.start()
        
        # 连接所有已发现的手柄
        for slot in self.pads.list():
            if slot.state.value == "disconnected":
                ok = self.pads.connect(slot.info.pad_id)
                if ok:
                    logger.info(f"手柄自动连接: {slot.info.name}")
                else:
                    logger.warn(f"手柄自动连接失败: {slot.info.name}")
        
        # 注册重连回调
        for slot in self.pads.list():
            self.reconnector.on_connect(
                slot.info.pad_id,
                lambda pid: self.pads.connect(pid)
            )
        
        self._start_servers()
        self.reconnector.start()
        
        # 尝试隐藏真实手柄（目标机）
        if not self.simulated and self.hidhide_cli:
            for slot in self.pads.list():
                vid_hex = f"{slot.info.vid:04x}"
                pid_hex = f"{slot.info.pid:04x}"
                try:
                    from .hidhid_manager import hide_devices_by_vid_pid
                    result = hide_devices_by_vid_pid(
                        self.hidhide_cli, vid_hex, pid_hex)
                    logger.info(f"HidHide 隐藏: {slot.info.name} -> {result.message}")
                except Exception as e:
                    logger.warn(f"HidHide 隐藏失败 {slot.info.name}: {e}")
    
    def _start_servers(self) -> None:
        host = self.cfg.get("usbip_host", "0.0.0.0")
        base = self.cfg.get("usbip_base_port", 3240)
        for i, slot in enumerate(self.pads.list()):
            if slot.port == 0:
                slot.port = base + i
            dev = self.pads.get_device(slot.info.pad_id)
            if not dev:
                continue
            srv = PadUsbipServer(slot, dev, host=host, port=slot.port,
                                 backlog=self.cfg.get("usbip_backlog", 16))
            if srv.start():
                self.servers[slot.info.pad_id] = srv
        logger.info(f"usbip 服务已启动: {len(self.servers)} 个端口")
    
    # ---- Web API 视图 ----
    def status(self) -> dict:
        with self._lock:
            pads = [s.to_dict() for s in self.pads.list()]
            for p in pads:
                srv = self.servers.get(p["pad_id"])
                if srv:
                    p["client_count"] = srv.client_count
            
            # 汇总重连状态
            reconnect_status = {}
            for pad in pads:
                reconnect_status[pad["pad_id"]] = self.reconnector.status(pad["pad_id"])
            
            # 组件检测结果
            try:
                comps = self.check_components()
            except Exception:  # noqa: BLE001
                comps = {}
            
            return {
                "status": "running",
                "mode": "simulated" if self.simulated else "real",
                "pads": pads,
                "config": self.cfg.all(),
                "log_level": logger.get().get_level(),
                "reconnector": {
                    "enabled": self.reconnector.policy.enabled,
                    "status": reconnect_status,
                },
                "hidhide": self.hidhide_status,
                "components": comps,
            }
    
    def set_pad_state(self, pad_id: str, action: str) -> dict:
        """action: connect / disconnect / reconnect"""
        slot = self.pads.get(pad_id)
        if not slot:
            return {"ok": False, "error": "pad not found"}
        
        if action == "connect":
            ok = self.pads.connect(pad_id)
            if ok and pad_id not in self.servers:
                dev = self.pads.get_device(pad_id)
                if dev:
                    srv = PadUsbipServer(slot, dev,
                                         host=self.cfg.get("usbip_host", "0.0.0.0"),
                                         port=slot.port)
                    if srv.start():
                        self.servers[pad_id] = srv
                        logger.info(f"USB/IP 服务启动: {slot.info.name}")
            return {"ok": ok}
        
        if action == "disconnect":
            self.pads.disconnect(pad_id)
            return {"ok": True}
        
        if action == "reconnect":
            # 手动触发重连
            self.reconnector.retry(pad_id)
            return {"ok": True}
        
        return {"ok": False, "error": f"unknown action: {action}"}
    
    def add_hid_action(self, pad_id: str, action: str) -> dict:
        """HidHide 相关操作。"""
        if not self.hidhide_cli:
            return {"ok": False, "error": "HidHide CLI 不可用"}
        
        from .hidhid_manager import (
            register_app_as_whitelisted, unhide_all,
            detect_overall_status, hide_devices_by_vid_pid
        )
        
        if action == "whitelist":
            ok = register_app_as_whitelisted(self.hidhide_cli)
            return {"ok": ok, "action": "whitelist"}
        
        if action == "unhide":
            ok = unhide_all(self.hidhide_cli)
            return {"ok": ok, "action": "unhide_all"}
        
        if action == "check_hide":
            slot = self.pads.get(pad_id)
            if not slot:
                return {"ok": False, "error": "pad not found"}
            vid_hex = f"{slot.info.vid:04x}"
            pid_hex = f"{slot.info.pid:04x}"
            result = hide_devices_by_vid_pid(self.hidhide_cli, vid_hex, pid_hex)
            return {"ok": result.status.value == "ok", "message": result.message}
        
        return {"ok": False, "error": f"unknown hidhide action: {action}"}
    
    def check_components(self) -> Dict[str, Any]:
        """检测所有外部组件状态。"""
        try:
            from .installer import ComponentDetector
            detector = ComponentDetector()
            results = detector.check_all()
            output = {}
            for key, comp in results.items():
                output[key] = {
                    "name": comp.name,
                    "status": comp.status.value,
                    "message": comp.message,
                    "install_url": comp.install_url,
                    "required": comp.required,
                }
            return output
        except Exception as e:
            return {"error": str(e)}
    
    # ---- 关闭 ----
    def stop(self) -> None:
        logger.info("DS5Hub 停止")
        self.reconnector.stop()
        
        # 取消隐藏所有设备
        if self.hidhide_cli:
            try:
                from .hidhid_manager import unhide_all
                unhide_all(self.hidhide_cli)
            except Exception:
                pass
        
        for srv in self.servers.values():
            srv.stop()
        self.servers.clear()
        self.pads.stop()
