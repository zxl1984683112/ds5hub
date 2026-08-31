# -*- coding: utf-8 -*-
"""
开机自启：HKCU 下 Software/Microsoft/Windows/CurrentVersion/Run 写入 DS5Hub。
Windows 专用；其他平台返回未启用。
"""
from __future__ import annotations

import os
import subprocess
import sys

RUN_KEY = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DS5Hub"
_TRAY_ARG = "--tray"


def _current_exe() -> str:
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(sys.argv[0])


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["reg", "query", RUN_KEY, "/v", VALUE_NAME],
            capture_output=True, text=True, timeout=5,
            encoding=os.device_encoding() or "utf-8", errors="replace")
        return VALUE_NAME in out.stdout
    except Exception:  # noqa: BLE001
        return False


def set_enabled(enabled: bool, exe_path: str | None = None,
                extra_args: str = "") -> bool:
    """写入/删除自启项。exe_path 默认当前程序。"""
    if os.name != "nt":
        return False
    path = exe_path or _current_exe()
    try:
        if enabled:
            cmd = f'reg add "{RUN_KEY}" /v {VALUE_NAME} /t REG_SZ /d "\\"{path}\\" {_TRAY_ARG} {extra_args}" /f'
        else:
            cmd = f'reg delete "{RUN_KEY}" /v {VALUE_NAME} /f'
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8,
                           encoding=os.device_encoding() or "utf-8", errors="replace")
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    import json
    print(json.dumps({"enabled": is_enabled()}))