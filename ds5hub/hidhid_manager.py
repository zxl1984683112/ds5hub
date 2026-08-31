# -*- coding: utf-8 -*-
r"""
HidHide 管理器：检测 / 注册表白名单 / 一键安装引导。

关键路径：
- HKLM\SOFTWARE\Nefarius Software Solutions\eVasive Systems Supports Pvt Ltd\HidHide\Services\\ApplicationPathList
- HidHideCLI.exe (通常安装于 APPDATA)
- sc query HidHide  → 检查服务是否运行
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
    """检查 Windows 服务 'HidHide' 是否正在运行。"""
    if not cli:
        cli = get_cli_path()
    try:
        out = subprocess.run(
            ["sc", "query", "HidHide"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace")
        return "RUNNING" in out.stdout.upper()
    except Exception:
        return False


def _run_cli(cli: str, *args: str, timeout: int = 8) -> "tuple[int, str, str]":
    """运行 HidHideCLI，返回 (returncode, stdout, stderr)。

    HidHideCLI 在驱动未加载进设备栈（未重启）时会在退出前挂起，故统一带超时。
    """
    try:
        r = subprocess.run(
            [cli, *args], capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception:
        return -1, "", ""


def _find_instance_path(cli: str, vid_hex: str, pid_hex: str) -> str:
    """从 HidHide 设备枚举中按 vid/pid 匹配 device instance path。

    HidHideCLI --dev-gaming / --dev-all 输出形如:
        HID\\VID_054C&PID_0CE6\\7&...  DualSense Wireless Controller
    返回首列 instance path；找不到返回空串。
    """
    vid = f"vid_{vid_hex.lower()}"
    pid = f"pid_{pid_hex.lower()}"
    for args in (["--dev-gaming"], ["--dev-all"]):
        rc, out, _ = _run_cli(cli, *args)
        if rc != 0:
            continue
        for line in out.splitlines():
            low = line.lower()
            if vid in low and pid in low:
                tok = line.strip().split()
                if tok:
                    return tok[0]
    return ""


def is_device_filtered(cli: str, vid_hex: str, pid_hex: str) -> Optional[bool]:
    """检查某个 vid/pid 是否已被 HidHide 隐藏（--dev-list）。"""
    if not cli:
        return None
    rc, out, _ = _run_cli(cli, "--dev-list")
    if rc != 0:
        return None
    vid = f"vid_{vid_hex.lower()}"
    pid = f"pid_{pid_hex.lower()}"
    for line in out.splitlines():
        low = line.lower()
        if vid in low and pid in low:
            return True
    return False


def register_app_as_whitelisted(cli: str) -> bool:
    """将本程序 exe 注册到 HidHide 应用白名单（--app-reg）。"""
    if not cli:
        return False
    prog_exe = sys.executable  # 打包后即 ds5hub.exe；源码运行是 python.exe
    rc, _, _ = _run_cli(cli, "--app-reg", prog_exe)
    return rc == 0


def hide_devices_by_vid_pid(cli: str, vid_hex: str, pid_hex: str) -> HidHideResult:
    """通过 HidHide 隐藏指定 VID/PID 的设备（--dev-hide）。"""
    if not cli:
        return HidHideResult(
            HidHideStatus.NOT_INSTALLED,
            "HidHideCLI.exe 未找到",
            action_needed=True,
            install_cmd=_get_install_guide())

    if is_device_filtered(cli, vid_hex, pid_hex) is True:
        return HidHideResult(HidHideStatus.OK,
                             f"设备 vid_{vid_hex} pid_{pid_hex} 已隐藏",
                             cli_path=cli)

    inst = _find_instance_path(cli, vid_hex, pid_hex)
    if not inst:
        return HidHideResult(
            HidHideStatus.DEVICE_NOT_HIDDEN,
            f"未在 HidHide 设备列表中找到 vid_{vid_hex} pid_{pid_hex}（设备未连接或系统尚未重启）",
            cli_path=cli)

    rc, _, err = _run_cli(cli, "--dev-hide", inst)
    if rc == 0:
        return HidHideResult(HidHideStatus.OK,
                             f"已隐藏 {inst}",
                             cli_path=cli)
    return HidHideResult(
        HidHideStatus.DEVICE_NOT_HIDDEN,
        f"隐藏失败: {err.strip()[:200] or 'HidHideCLI 未响应（可能需要重启）'}",
        cli_path=cli)


def unhide_all(cli: str) -> bool:
    """取消所有 HidHide 隐藏（--dev-list 枚举后逐个 --dev-unhide）。"""
    if not cli:
        return False
    rc, out, _ = _run_cli(cli, "--dev-list")
    if rc != 0:
        return False
    ok = True
    for line in out.splitlines():
        tok = line.strip().split()
        if not tok:
            continue
        r2, _, _ = _run_cli(cli, "--dev-unhide", tok[0])
        if r2 != 0:
            ok = False
    return ok


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
            "HidHide 服务未运行。请重启电脑或手动启动服务 'HidHide'。",
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
