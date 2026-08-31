# -*- coding: utf-8 -*-
"""
真实 DualSense 手柄 hidapi 适配：枚举、打开/关闭、读写报告。

无 hidapi 库时枚举结果为空（保持真实模式，无模拟回退）。

支持 USB 有线与蓝牙无线双模接入（通过 hidapi 统一接口）。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .pad_manager import AbstractPadDevice, PadInfo

try:
    import hidapi
    _HIDAPI_AVAILABLE = True
except ImportError:
    _HIDAPI_AVAILABLE = False


@dataclass
class HidDeviceEntry:
    """枚举到的 HID 设备信息。"""
    vendor_id: int
    product_id: int
    serial_number: str
    path: str               # hidapi device path
    interface_number: int
    manufacturer: str = ""
    product: str = ""
    release_version: int = 0


_DUALSENSE_VID = 0x054C
_DUALSENSE_USB_PID = 0x0CE6
_DUALSENSE_BT_PID = 0x09CC
_DUALSENSE_NAME_KEYWORDS = ("dualsense", "wireless controller", "playstation")


def enumerate_ds5_pads() -> List[HidDeviceEntry]:
    """枚举所有 DualSense 手柄（USB + Bluetooth）。"""
    results: List[HidDeviceEntry] = []
    if not _HIDAPI_AVAILABLE:
        return results
    try:
        hidapi.init()
    except Exception:
        return results
    try:
        for dev in hidapi.enumerate(_DUALSENSE_VID):
            pid = dev.get("product_id", 0)
            if pid not in (_DUALSENSE_USB_PID, _DUALSENSE_BT_PID):
                continue
            name = (dev.get("product_string", "") + " " +
                    dev.get("manufacturer_string", "")).lower()
            # 二次确认关键词匹配
            if any(kw in name for kw in _DUALSENSE_NAME_KEYWORDS):
                results.append(HidDeviceEntry(
                    vendor_id=_DUALSENSE_VID,
                    product_id=pid,
                    serial_number=dev.get("serial_number", ""),
                    path=dev.get("path", ""),
                    interface_number=dev.get("interface_number", 0),
                    manufacturer=dev.get("manufacturer_string", ""),
                    product=dev.get("product_string", ""),
                    release_version=dev.get("release_number", 0),
                ))
    finally:
        try:
            hidapi.exit()
        except Exception:
            pass
    return results


class HidPad(AbstractPadDevice):
    """基于 hidapi 的真实 DualSense 手柄设备。"""

    # DualSense 输出报告 ID 和长度
    OUT_REPORT_ID = 0x01
    OUT_REPORT_LEN = 582   # Bluetooth 模式下带 report id
    IN_REPORT_LEN = 64     # 输入报告最大长度

    def __init__(self, info: PadInfo):
        self._info = info
        self._handle = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_report: bytes = b""
        self._report_available = threading.Event()

    @property
    def info(self) -> PadInfo:
        return self._info

    def open(self) -> bool:
        if not _HIDAPI_AVAILABLE:
            return False
        try:
            hidapi.init()
            self._handle = hidapi.open_path(self._info.path)
            if self._handle is None:
                return False
            # 尝试设置独占模式（非阻塞）
            hidapi.set_nonblocking(self._handle, 1)
            self._start_read_thread()
            return True
        except Exception as e:
            return False

    def close(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        with self._lock:
            if self._handle is not None:
                try:
                    hidapi.close(self._handle)
                except Exception:
                    pass
                self._handle = None

    def is_open(self) -> bool:
        return self._handle is not None

    def read_report(self, timeout_ms: int = 100) -> bytes | None:
        if not self.is_open():
            return None
        signaled = self._report_available.wait(timeout=timeout_ms / 1000.0)
        if not signaled:
            return None
        with self._lock:
            report = self._last_report
            self._report_available.clear()
            return report

    def write_report(self, data: bytes) -> bool:
        if not self.is_open():
            return False
        try:
            with self._lock:
                # hidapi.write 要求第一个字节为 report id（0 表示无 report id）
                payload = bytearray(data)
                if len(payload) < self.OUT_REPORT_LEN:
                    pad_len = self.OUT_REPORT_LEN - len(payload)
                    payload.extend(b"\x00" * pad_len)
                hidapi.write(self._handle, bytes(payload))
            return True
        except Exception:
            return False

    def describe(self) -> PadInfo:
        return self._info

    def _start_read_thread(self) -> None:
        """后台线程持续读取输入报告。"""
        def _read_loop():
            while not self._stop_event.is_set():
                try:
                    with self._lock:
                        if self._handle is None:
                            break
                        chunk = hidapi.read(self._handle, self.IN_REPORT_LEN)
                    if chunk:
                        with self._lock:
                            self._last_report = chunk
                        self._report_available.set()
                    else:
                        self._stop_event.wait(0.01)
                except Exception:
                    break
        self._thread = threading.Thread(target=_read_loop, daemon=True)
        self._thread.start()


class HidPadManager:
    """
    真实手柄管理器：扫描并注册所有 DualSense 手柄，
    在目标机上替代模拟桩。

    初始化后调用 scan() 发现手柄，返回已注册的 slot 列表。
    """

    def __init__(self):
        self._mgr = _create_pad_manager()

    def scan(self) -> List[PadInfo]:
        """扫描并注册所有 DualSense 手柄。"""
        pads = enumerate_ds5_pads()
        for entry in pads:
            conn_mode = "bluetooth" if entry.product_id == _DUALSENSE_BT_PID else "usb"
            info = PadInfo(
                pad_id=f"hps_{entry.serial_number[:8] or 'none'}",
                name=entry.product or f"DualSense {conn_mode}",
                vid=entry.vendor_id,
                pid=entry.product_id,
                connection=conn_mode,
                path=entry.path,
                serial=entry.serial_number,
            )
            self._mgr.register(info)
        return list(self._mgr.list())

    def register(self, info: PadInfo) -> Any:
        """手动注册一个已知路径的手柄。"""
        self._mgr.register(info)

    def list(self) -> List[Any]:
        return self._mgr.list()

    def get(self, pad_id: str) -> Any:
        return self._mgr.get(pad_id)

    def connect(self, pad_id: str) -> bool:
        return self._mgr.connect(pad_id)

    def disconnect(self, pad_id: str) -> None:
        self._mgr.disconnect(pad_id)

    def get_device(self, pad_id: str) -> Optional[HidPad]:
        return self._mgr.get_device(pad_id)

    def start(self):
        self._mgr.start()

    def stop(self):
        self._mgr.stop()


def _create_pad_manager() -> object:
    """创建 PadManager：有 hidapi 时用 HidPad 工厂；无 hidapi 时空管理器。"""
    from .pad_manager import PadManager

    if _HIDAPI_AVAILABLE:
        def factory(pad_id: str):
            # 查找已注册槽位信息，构造真实 HidPad；查不到则无设备
            for s in _registered_slots.values():
                if s["pad_id"] == pad_id:
                    return HidPad(s["info"])
            return None
        mgr = PadManager(factory=factory)
    else:
        mgr = PadManager()

    # 保存注册回调以便 factory 查找
    global _registered_slots
    _registered_slots = {}
    orig_register = mgr.register
    def patched_register(info, auto_port=0):
        _registered_slots[info.pad_id] = {"info": info, "auto_port": auto_port}
        return orig_register(info, auto_port)
    mgr.register = patched_register  # type: ignore

    return mgr


# 全局注册槽位映射
_registered_slots: dict = {}
