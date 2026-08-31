# -*- coding: utf-8 -*-
r"""
HidHide 管理器：检测 / 注册表白名单 / 一键安装引导。

关键路径：
- HKLM\SOFTWARE\Nefarius Software Solutions\eVasive Systems Supports Pvt Ltd\HidHide\Services\\ApplicationPathList
- HidHideCLI.exe (通常安装于 APPDATA)
- sc query NefariusHidHide  → 检查服务是否运行
"""
from __future__ import annotations

import os
import subprocess
import sys
from enum import Enum
from typing import List, NamedTuple, Optional


class HidHideStatus(Enum):
    NOT_INSTALLED = "not_installed"
    SERVICE_NOT_RUNNING = "service_not_running"
    DEVICE_NOT_HIDDEN = "device_not_hidden"   # 驱动正常但指定设备未隐藏
    OK = "ok"


class HidHideResult(NamedTuple):
    status: HidHideStatus
    message: str
    cli_path: str = ""
    action_needed: bool = False
    install_cmd: str = ""


def _get_hh_cli_path() -> str:
    """定位 HidHideCLI.exe。"""
    candidates: List[str] = []
    for base in [
        r"%APPDATA%\Programs",                          # 标准安装位置
        r"%PROGRAMFILES%\Nefarius Software Solutions",  # 部分旧安装
    ]:
        p = base.replace("%APPDATA%", os.environ.get("APPDATA", "")) \
                 .replace("%PROGRAMFILES%", os.getenv("PROGRAMFILES", ""))
        # 递归找 HidHideCLI.exe
        try:
            for root, dirs, files in os.walk(p):
                if "HidHideCLI.exe" in files:
                    return os.path.join(root, "HidHideCLI.exe")
        except FileNotFoundError:
            pass
    # PATH 中搜索
    env_paths = os.environ.get("PATH", "").split(os.pathsep)
    for d in env_paths:
        f = os.path.join(d, "HidHideCLI.exe")
        if os.path.isfile(f):
            return f
    return ""


# 全局缓存
_cached_cli: str = ""
_cached_status: HidHideStatus | None = None


def get_cli_path(force_refresh: bool = False) -> str:
    global _cached_cli
    if force_refresh or not _cached_cli:
        _cached_cli = _get_hh_cli_path()
    return _cached_cli


def get_service_running(cli: str = "") -> bool:
    """检查 Windows 服务 'NefariusHidHide' 是否正在运行。"""
    if not cli:
        cli = get_cli_path()
    try:
        out = subprocess.run(
            ["sc", "query", "NefariusHidHide"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace")
        return "RUNNING" in out.stdout.upper()
    except Exception:
        return False


def is_device_filtered(cli: str, vid_hex: str, pid_hex: str) -> Optional[bool]:
    """检查某个 vid/pid 是否已被 HidHide 过滤（返回 True/False/None 失败）。"""
    if not cli:
        return None
    try:
        # HID device GUID 格式: hid#{vid_XXXX&pid_YYYY}...
        cmd = [cli, "list-hidden-devices"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5, encoding="utf-8", errors="replace")
        devices = out.stdout.strip().splitlines()
        target = f"{vid_hex}&{pid_hex}"
        for dev in devices:
            if target in dev:
                return True
        return False
    except Exception:
        return None


def register_app_as_whitelisted(cli: str) -> bool:
    """将本程序 exe 添加到 HidHide 应用白名单（可操作其隐藏设备的能力）。"""
    if not cli:
        return False
    prog_exe = getattr(sys, "_MEIPASS", sys.executable)
    # HidHide 白名单通过注册表管理；用 HidHideCLI 的 whitelist subcommand
    # 注意: 不同版本的 CLI 命令可能不同，这里采用通用方案
    try:
        r = subprocess.run(
            [cli, "whitelist", "add", prog_exe],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        return r.returncode == 0
    except FileNotFoundError:
        return False
    except Exception:
        return False


def hide_devices_by_vid_pid(cli: str, vid_hex: str, pid_hex: str) -> HidHideResult:
    """尝试通过 HidHide 隐藏指定 VID/PID 的设备，使它们只对本程序可见。"""
    if not cli:
        return HidHideResult(
            HidHideStatus.NOT_INSTALLED,
            "HidHideCLI.exe 未找到",
            action_needed=True,
            install_cmd=_get_install_guide())

    # 检查设备是否已在被隐藏列表中
    hidden = is_device_filtered(cli, vid_hex, pid_hex)
    if hidden is True:
        return HidHideResult(HidHideStatus.OK,
                             f"设备 vid_{vid_hex} pid_{pid_hex} 已隐藏",
                             cli_path=cli)
    if hidden is False:
        return HidHideResult(HidHideStatus.DEVICE_NOT_HIDDEN,
                             f"设备 vid_{vid_hex} pid_{pid_hex} 未被隐藏",
                             cli_path=cli)

    # 尝试执行隐藏
    try:
        r = subprocess.run(
            [cli, "hide-device-by-id", f"vid_{vid_hex}", f"pid_{pid_hex}"],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        if r.returncode == 0:
            return HidHideResult(HidHideStatus.OK,
                                 f"已隐藏 hid#{vid_hex}_{pid_hex}",
                                 cli_path=cli)
        return HidHideResult(HidHideStatus.DEVICE_NOT_HIDDEN,
                             f"隐藏失败: {r.stderr.strip()[:200]}",
                             cli_path=cli)
    except FileNotFoundError:
        return HidHideResult(HidHideStatus.NOT_INSTALLED,
                             "HidHideCLI 文件丢失",
                             action_needed=True,
                             install_cmd=_get_install_guide())
    except Exception as e:
        return HidHideResult(HidHideStatus.DEVICE_NOT_HIDDEN,
                             f"隐藏异常: {e}",
                             cli_path=cli)


def unhide_all(cli: str) -> bool:
    """取消所有 HidHide 隐藏。"""
    if not cli:
        return False
    try:
        r = subprocess.run(
            [cli, "unhide-all"],
            capture_output=True, text=True, timeout=10, encoding="utf-8", errors="replace")
        return r.returncode == 0
    except Exception:
        return False


def detect_overall_status(cli: str = "") -> HidHideResult:
    """整体检测 HidHide 状态。"""
    if not cli:
        cli = get_cli_path()

    if not cli:
        return HidHideResult(
            HidHideStatus.NOT_INSTALLED,
            "未找到 HidHide 安装，需要下载并安装 HidHide。",
            action_needed=True,
            install_cmd=_get_install_guide())

    if not get_service_running(cli):
        return HidHideResult(
            HidHideStatus.SERVICE_NOT_RUNNING,
            "HidHide 服务未运行。请重启电脑或手动启动服务 'NefariusHidHide'。",
            cli_path=cli,
            action_needed=True)

    return HidHideResult(HidHideStatus.OK,
                         "HidHide 服务正常运行。",
                         cli_path=cli)


def _get_install_guide() -> str:
    return (
        "安装步骤:\n"
        "1. 下载 HidHide: https://github.com/nefarius/HidHide/releases\n"
        "2. 以管理员身份运行安装程序\n"
        "3. 安装完成后重启电脑\n"
        "4. 使用 DS5Hub 时将自动处理后续配置"
    )


if __name__ == "__main__":
    import json
    cli = get_cli_path()
    status = detect_overall_status(cli)
    print(json.dumps({
        "cli_path": cli,
        "status": status.status.value,
        "message": status.message,
        "action_needed": status.action_needed,
        "install_cmd": status.install_cmd,
    }, indent=2, ensure_ascii=False))
