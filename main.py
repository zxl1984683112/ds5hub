# -*- coding: utf-8 -*-
"""
DS5Hub 入口：托盘 + Web 管理 + 核心服务。

命令行：
  python main.py            # 正常启动（含托盘）
  python main.py --tray     # 开机自启模式：托盘 + 服务，不主动开浏览器
  python main.py --web-only # 仅 Web（调试）
  python main.py --detect   # 仅组件检测后退出
  python main.py --pack     # 生成安装脚本后退出
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ds5hub import logger  # noqa: E402
from ds5hub.app import DS5HubApp  # noqa: E402
from ds5hub.config import Config  # noqa: E402
from ds5hub.web_api import create_app  # noqa: E402


def run_web(app: DS5HubApp) -> None:
    import uvicorn
    config = uvicorn.Config(
        create_app(app),
        host=app.cfg.get("web_host", "127.0.0.1"),
        port=app.cfg.get("web_port", 8080),
        log_level="warning",
    )
    server = uvicorn.Server(config)
    server.run()


def run_tray(app: DS5HubApp) -> None:
    """系统托盘（M2 增强：状态指示 + HidHide 快捷操作）。"""
    try:
        import pystray
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        logger.warn("pystray/Pillow 未安装，跳过托盘（仅 Web 模式）")
        return

    def make_icon():
        img = Image.new("RGB", (64, 64), "#0f1420")
        d = ImageDraw.Draw(img)
        d.ellipse([10, 10, 54, 54], fill="#3b82f6")
        d.text((20, 24), "D", fill="white")
        return img

    def on_open():
        webbrowser.open(f"http://{app.cfg.get('web_host','127.0.0.1')}:{app.cfg.get('web_port',8080)}")

    def on_detect():
        components = app.check_components()
        msg = "\n".join(f"• {k}: {v.get('status','?')}" for k, v in components.items())
        logger.info(f"组件检测:\n{msg}")

    menu = (
        pystray.MenuItem("打开管理面板", on_open),
        pystray.MenuItem("组件检测", on_detect),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", lambda: (app.stop(), icon.stop())),
    )
    icon = pystray.Icon("DS5Hub", make_icon(), "DS5Hub", menu)
    icon.run()


def cmd_detect(app: DS5HubApp) -> None:
    """仅组件检测并输出结果。"""
    print("=" * 50)
    print("DS5Hub 组件检测")
    print("=" * 50)
    components = app.check_components()
    emoji_map = {"installed": "✅", "missing": "❌", "update_required": "⚠️",
                 "partial": "🟡"}
    for key, comp in components.items():
        if isinstance(comp, dict):
            status = comp.get("status", "unknown")
            emoji = emoji_map.get(status, "?")
            print(f"\n{emoji} {comp.get('name', key)}")
            print(f"   状态: {status}")
            print(f"   说明: {comp.get('message', '')}")
            url = comp.get("install_url", "")
            if url:
                print(f"   下载: {url}")
            if not comp.get("required", True):
                print(f"   (可选)")
    print("\n" + "=" * 50)


def cmd_pack(app: DS5HubApp) -> None:
    """生成本地安装引导脚本。"""
    from ds5hub.installer import InstallationHelper, ComponentDetector
    detector = ComponentDetector()
    results = detector.check_all()
    
    output_dir = os.path.expanduser("~\\Desktop")
    script_path = os.path.join(output_dir, "DS5Hub_安装引导.bat")
    
    helper = InstallationHelper()
    helper.write_installer_script(script_path)
    print(f"安装引导脚本已生成: {script_path}")


def main() -> None:
    # 无控制台/重定向环境下强制 UTF-8 输出，避免 emoji 触发 GBK 编码错误
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(description="DS5Hub - DualSense USB/IP Hub")
    parser.add_argument("--tray", action="store_true", help="最小化托盘启动（开机自启）")
    parser.add_argument("--web-only", action="store_true", help="仅 Web 服务")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--detect", action="store_true",
                        help="仅组件检测并退出")
    parser.add_argument("--pack", action="store_true",
                        help="生成安装引导脚本并退出")
    args = parser.parse_args()

    cfg = Config(args.config)
    logger.init(
        log_dir=cfg.get("log.dir", None),
        level=cfg.get("log.level", "INFO"),
        max_bytes=cfg.get("log.max_bytes", 10 * 1024 * 1024),
        backup_count=cfg.get("log.backup_count", 5),
        ring_size=cfg.get("log.ring_size", 2000),
    )
    
    logger.info(f"DS5Hub 启动 (v{__import__('ds5hub').__version__})")
    logger.info(f"配置: {cfg.path}")
    logger.info("模式: 真实手柄 (hidapi)")

    app = DS5HubApp(cfg)

    # ---- 特殊命令 ----
    if args.detect:
        cmd_detect(app)
        return
    
    if args.pack:
        cmd_pack(app)
        return
    
    app.start()

    if args.web_only:
        run_web(app)
        return

    # Web 在后台线程，托盘在主线程
    t = threading.Thread(target=run_web, args=(app,), daemon=True)
    t.start()
    logger.info(f"Web 管理: http://{cfg.get('web_host','127.0.0.1')}:{cfg.get('web_port',8080)}")

    if not args.tray:
        try:
            webbrowser.open(f"http://{cfg.get('web_host','127.0.0.1')}:{cfg.get('web_port',8080)}")
        except Exception:  # noqa: BLE001
            pass
    run_tray(app)


if __name__ == "__main__":
    main()
