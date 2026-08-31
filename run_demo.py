# -*- coding: utf-8 -*-
"""开发机演示：初始化日志 + 模拟手柄 + 启动 usbip 服务（M1）。"""
import sys
import time

sys.path.insert(0, r"D:\QwenpawWorkspace\project\ds5hub")
from ds5hub import logger
from ds5hub.pad_manager import default_simulated_pads
from ds5hub.usbip_server import PadUsbipServer


def main():
    logger.init(level="DEBUG")
    logger.info("DS5Hub 开发模式启动")

    mgr = default_simulated_pads(count=2)
    servers = []
    for slot in mgr.list():
        dev = mgr.get_device(slot.info.pad_id)
        srv = PadUsbipServer(slot, dev, host="0.0.0.0", port=slot.port)
        if srv.start():
            servers.append(srv)

    logger.info(f"共 {len(servers)} 个 usbip 服务运行中 (端口 {[s.port for s in servers]})")
    try:
        while True:
            time.sleep(2)
    except KeyboardInterrupt:
        logger.info("退出")
        for s in servers:
            s.stop()


if __name__ == "__main__":
    main()