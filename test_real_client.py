# -*- coding: utf-8 -*-
"""用真实 usbip 客户端（usbip-win2 / cezanne 的 usbip.exe）验证 DS5Hub 服务端协议握手。

不装任何驱动，纯 TCP list 握手：起一个最小 PadUsbipServer（虚拟 DualSense），
再调用 usbip.exe list -r 127.0.0.1 观察是否能正确识别导出设备。

用法：
    python test_real_client.py [port] [usbip.exe 路径]
默认客户端用 find_usbip_cli() 自动定位（C:\\Program Files\\USBip\\usbip.exe 或 PATH）。
"""
from __future__ import annotations

import subprocess
import sys
import time

sys.path.insert(0, r"D:\QwenpawWorkspace\project\ds5hub")

from ds5hub import logger  # noqa: E402
from ds5hub.pad_manager import AbstractPadDevice, PadInfo, PadSlot  # noqa: E402
from ds5hub.usbip_server import PadUsbipServer  # noqa: E402

logger.init(level="INFO", ring_size=50)


class MockDevice(AbstractPadDevice):
    def __init__(self, info: PadInfo):
        self._info = info
        self._open = False

    def open(self) -> bool:
        self._open = True
        return True

    def close(self) -> None:
        self._open = False

    def read_report(self, timeout_ms: int = 100):
        time.sleep(0.05)
        return None

    def write_report(self, data: bytes) -> bool:
        return True

    def is_open(self) -> bool:
        return self._open

    def describe(self) -> PadInfo:
        return self._info


def _find_client_exe():
    from ds5hub.install_orchestrator import find_usbip_cli
    return find_usbip_cli()


def main(port: int = 3241, client_exe: str = None) -> int:
    info = PadInfo(pad_id="pad0001", name="DualSense Mock",
                   vid=0x054C, pid=0x0CE6, connection="bluetooth")
    device = MockDevice(info)
    slot = PadSlot(info=info, busid="1-1", port=port)
    srv = PadUsbipServer(slot, device, host="127.0.0.1", port=port)
    if not srv.start():
        print("服务端启动失败（端口被占用？）")
        return 1
    print(f"[server] 监听 127.0.0.1:{port} (busid 1-1)")

    exe = client_exe or _find_client_exe()
    if not exe:
        print("未找到 usbip 客户端；请先安装 usbip-win2，或显式传入 exe 路径")
        return 1
    try:
        r = subprocess.run(
            [exe, "--tcp-port", str(port), "list", "-r", "127.0.0.1"],
            capture_output=True, text=True, timeout=20,
            encoding="utf-8", errors="replace")
        print("\n=== usbip.exe list 输出 ===")
        print("--- stdout ---")
        print(r.stdout)
        print("--- stderr ---")
        print(r.stderr)
        print("--- returncode ---", r.returncode)
        ok = r.returncode == 0 and ("054c" in r.stdout.lower()
                                    or "0ce6" in r.stdout.lower()
                                    or "1-1" in r.stdout)
        print("\n握手判定:", "成功 ✅" if ok else "未识别到导出设备 ❌")
        return 0 if ok else 2
    finally:
        srv.stop()
        print("[server] 已停止")


if __name__ == "__main__":
    sys.exit(main())
