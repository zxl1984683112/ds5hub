# -*- coding: utf-8 -*-
"""
DS5Hub 核心服务协调器：把配置、日志、手柄管理、usbip 服务、Web API 组装起来。

M2 增强：
- HidHide 自动化检测与设备隐藏
- 自动重连策略（指数退避）
- 真实 hidapi 手柄枚举（无手柄时保持真实模式，手柄列表为空）
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
    AbstractPadDevice, PadManager, PadInfo,
    PadState, PadSlot
)
from .reconnect import AutoReconnector, ReconnectPolicy
from .usbip_server import PadUsbipServer


class DS5HubApp:
    def __init__(self, config: Config | None = None):
        self.cfg = config or Config()

        # ---- 手柄管理器（仅真实 hidapi 枚举）----
        self._real_hid_mgr = HidPadManager()
        pads_found = self._real_hid_mgr.scan()
        if not pads_found:
            logger.warn("未发现真实 DualSense 手柄（真实模式，无模拟回退）")
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

        # ---- 一键环境部署编排器 ----
        from .install_orchestrator import InstallOrchestrator
        self.orchestrator = InstallOrchestrator(
            self.cfg, dry_run=bool(self.cfg.get("orchestrator.dry_run", False)))

        # ---- 一键卸载编排器 ----
        from .uninstall_orchestrator import UninstallOrchestrator
        self.uninstaller = UninstallOrchestrator(
            self.cfg, stop_callback=self.stop,
            dry_run=bool(self.cfg.get("orchestrator.dry_run", False)))

        # ---- 自动本机 attach ----
        self._running = False
        self._attach_attempted: set = set()      # 已尝试 attach 的 pad_id
        self._attach_results: Dict[str, dict] = {}
        self._attach_thread: Optional[threading.Thread] = None
    
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

        # 自动本机 attach 线程
        self._running = True
        self._attach_thread = threading.Thread(
            target=self._auto_attach_loop, daemon=True, name="ds5hub-auto-attach")
        self._attach_thread.start()

        # 尝试隐藏真实手柄（目标机）
        if self.hidhide_cli:
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
                "mode": "real",
                "pads": pads,
                "config": self.cfg.all(),
                "log_level": logger.get().get_level(),
                "reconnector": {
                    "enabled": self.reconnector.policy.enabled,
                    "status": reconnect_status,
                },
                "hidhide": self.hidhide_status,
                "components": comps,
                "orchestrator": self.orchestrator.status(),
                "uninstaller": self.uninstaller.status(),
                "attach": self.attach_status(),
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

    # ---- 自动本机 attach（usbip 回环）----
    def _auto_attach_loop(self) -> None:
        logger.info("[attach] 自动 attach 线程启动")
        interval = float(self.cfg.get("orchestrator.attach_interval", 3.0))
        while self._running:
            try:
                self._auto_attach_once()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[attach] 扫描异常: {e}")
            time.sleep(max(1.0, interval))
        logger.info("[attach] 自动 attach 线程退出")

    def _auto_attach_once(self) -> None:
        """扫描 EXPOSED 且服务在线的手柄，逐个 attach（每 pad 只自动尝试一次）。"""
        if not self.cfg.get("orchestrator.auto_attach", True):
            return
        candidates = []
        with self._lock:
            for slot in self.pads.list():
                if slot.pad_id in self._attach_attempted:
                    continue
                if slot.pad_id in self.servers and slot.state.value in ("exposed", "ready"):
                    candidates.append(slot)
        if not candidates:
            return
        from .install_orchestrator import find_usbip_cli
        if not find_usbip_cli():
            return  # usbip 客户端未就绪（环境未部署），静默等待
        for slot in candidates:
            self._attach_attempted.add(slot.info.pad_id)
            result = self.attach_pad(slot.info.pad_id)
            self._attach_results[slot.info.pad_id] = result
            logger.info(
                f"[attach] {slot.info.name}: "
                f"{'成功' if result.get('ok') else '失败 - ' + result.get('error', '')}")

    def attach_pad(self, pad_id: str) -> dict:
        """对本机 attach 一个手柄（usbip 回环，虚拟设备直插系统）。"""
        slot = self.pads.get(pad_id)
        if not slot:
            return {"ok": False, "error": "pad not found"}
        if pad_id not in self.servers:
            return {"ok": False, "error": "手柄 usbip 服务未运行"}
        from .install_orchestrator import find_usbip_cli, run_elevated
        usbip = find_usbip_cli()
        if not usbip:
            return {"ok": False, "error": "usbip 客户端不可用（请先完成环境部署）"}
        host = "127.0.0.1"
        port = int(slot.port or self.cfg.get("usbip_base_port", 3240))
        args = ["attach", "--remote", host, "--busid", slot.busid]
        if port != 3240:  # 非标准端口才需要显式指定
            args += ["--tcpport", str(port)]
        code, out = run_elevated(usbip, args, timeout=60)
        ok = code == 0
        result = {"ok": ok, "busid": slot.busid, "port": port, "output": out}
        self._attach_results[pad_id] = result
        if ok:
            logger.info(f"[attach] 已 attach {slot.info.name} (busid {slot.busid})")
        else:
            logger.warn(f"[attach] attach 失败 {slot.info.name}: {out}")
        return result

    def attach_status(self) -> dict:
        return {"auto": bool(self.cfg.get("orchestrator.auto_attach", True)),
                "attempted": sorted(self._attach_attempted),
                "results": dict(self._attach_results)}
    
    # ---- 关闭 ----
    def stop(self) -> None:
        logger.info("DS5Hub 停止")
        self._running = False
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
