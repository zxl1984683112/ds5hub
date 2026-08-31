# -*- coding: utf-8 -*-
"""
多手柄管理：枚举/注册手柄槽位，状态机管理，为每个手柄生成 usbip 设备描述。

PadSlot 状态机:
  DISCONNECTED -> CONNECTING -> READY -> EXPOSED(SERVICE_RUNNING)
  READY/EXPOSED -> DISCONNECTED (掉线) -> CONNECTING ... (自动重连)

仅管理真实 hidapi 设备（HidPad），不再提供模拟桩。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from .usbip_protocol import UsbDeviceInfo


class PadState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    READY = "ready"
    EXPOSED = "exposed"       # 正在被 usbip 服务/客户端连接
    ERROR = "error"


@dataclass
class PadInfo:
    pad_id: str
    name: str
    vid: int
    pid: int
    connection: str            # "bluetooth" / "usb" / "simulated"
    path: str = ""             # 设备实例路径/接口路径
    serial: str = ""


@dataclass
class PadSlot:
    """一个物理手柄槽位。"""
    info: PadInfo
    state: PadState = PadState.DISCONNECTED
    busid: str = ""
    port: int = 0
    last_error: str = ""
    last_seen: float = 0.0
    client_count: int = 0

    @property
    def devinfo(self) -> UsbDeviceInfo:
        if not self.busid:
            self.busid = f"1-{self.info.pad_id[-4:].lstrip('0') or '1'}"
        return UsbDeviceInfo(
            path=f"/sys/devices/platform/ds5hub/usb1/{self.busid}",
            busid=self.busid,
            id_vendor=self.info.vid,
            id_product=self.info.pid,
            interfaces=[(3, 0, 0, 0)],   # HID 接口
        )

    def to_dict(self) -> dict:
        return {
            "pad_id": self.info.pad_id,
            "name": self.info.name,
            "vid": f"0x{self.info.vid:04X}",
            "pid": f"0x{self.info.pid:04X}",
            "connection": self.info.connection,
            "path": self.info.path,
            "serial": self.info.serial,
            "state": self.state.value,
            "busid": self.busid,
            "port": self.port,
            "last_error": self.last_error,
            "last_seen": self.last_seen,
            "client_count": self.client_count,
        }


class AbstractPadDevice:
    """手柄设备抽象（真实 hidapi 设备实现此接口）。"""
    def open(self) -> bool: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
    def read_report(self, timeout_ms: int = 100) -> bytes | None: raise NotImplementedError
    def write_report(self, data: bytes) -> bool: raise NotImplementedError
    def is_open(self) -> bool: return False
    def describe(self) -> PadInfo: raise NotImplementedError


class PadManager:
    def __init__(self, factory=None):
        """
        factory: callable(pad_id) -> AbstractPadDevice，用于创建真实设备。
                 留空时仅注册槽位、不创建设备（get_device 返回 None）。
        """
        self._factory = factory
        self._slots: dict[str, PadSlot] = {}
        self._devices: dict[str, AbstractPadDevice] = {}
        self._lock = threading.RLock()
        self._running = False
        self._supervisor: threading.Thread | None = None
        self._on_state_change = None

    # ---- 注册/移除 ----
    def register(self, info: PadInfo, auto_port: int = 0) -> PadSlot:
        with self._lock:
            if info.pad_id in self._slots:
                return self._slots[info.pad_id]
            slot = PadSlot(info=info, port=auto_port)
            self._slots[info.pad_id] = slot
            if self._factory:
                dev = self._factory(info.pad_id)
                if dev is not None:
                    self._devices[info.pad_id] = dev
            self._log(f"注册手柄: {info.name} ({info.pad_id})")
            return slot

    def unregister(self, pad_id: str) -> None:
        with self._lock:
            self._slots.pop(pad_id, None)
            dev = self._devices.pop(pad_id, None)
            if dev:
                dev.close()
            self._log(f"移除手柄: {pad_id}")

    def list(self) -> list[PadSlot]:
        with self._lock:
            return list(self._slots.values())

    def get(self, pad_id: str) -> PadSlot | None:
        with self._lock:
            return self._slots.get(pad_id)

    # ---- 连接管理 ----
    def connect(self, pad_id: str) -> bool:
        """打开手柄设备（由 supervisor / Web 触发）。"""
        with self._lock:
            slot = self._slots.get(pad_id)
            dev = self._devices.get(pad_id)
            if not slot or not dev:
                return False
        if slot.state in (PadState.READY, PadState.EXPOSED):
            return True
        slot.state = PadState.CONNECTING
        ok = dev.open()
        if ok:
            slot.state = PadState.READY
            slot.last_seen = time.time()
            slot.last_error = ""
            self._log(f"手柄已连接: {slot.info.name}")
        else:
            slot.state = PadState.ERROR
            slot.last_error = "open 失败"
            self._log(f"手柄连接失败: {slot.info.name}")
        return ok

    def disconnect(self, pad_id: str) -> None:
        with self._lock:
            slot = self._slots.get(pad_id)
            dev = self._devices.get(pad_id)
            if not slot:
                return
        dev.close() if dev else None
        slot.state = PadState.DISCONNECTED

    # ---- 数据读写（供 URB 服务使用）----
    def get_device(self, pad_id: str) -> AbstractPadDevice | None:
        with self._lock:
            return self._devices.get(pad_id)

    # ---- 自动重连监督 ----
    def start(self):
        self._running = True
        self._supervisor = threading.Thread(target=self._supervise_loop, daemon=True)
        self._supervisor.start()
        self._log("手柄监督线程已启动")

    def stop(self):
        self._running = False
        for pad_id in list(self._slots):
            self.disconnect(pad_id)

    def _supervise_loop(self):
        cfg = {"enabled": True, "initial_delay": 3.0, "max_delay": 30.0, "max_retries": 0}
        while self._running:
            try:
                for slot in self.list():
                    if slot.state == PadState.DISCONNECTED:
                        # 尝试重连
                        if self.connect(slot.info.pad_id):
                            delay = cfg["initial_delay"]
                        else:
                            delay = min(delay * 2, cfg["max_delay"]) if "delay" in locals() else cfg["initial_delay"]
                            slot.last_error = slot.last_error or "重连失败"
                time.sleep(2)
            except Exception as e:  # noqa: BLE001
                time.sleep(2)

    def set_state_change_cb(self, cb):
        self._on_state_change = cb

    def _log(self, msg: str):
        from . import logger
        logger.info(msg)