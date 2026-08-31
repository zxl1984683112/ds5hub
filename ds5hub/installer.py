# -*- coding: utf-8 -*-
"""
目标机组件安装引导：检测并引导安装必要的外部组件。

当前支持：
1. HidHide 驱动——隐藏真实手柄（必需：本机游戏场景防止物理手柄双重输入）
2. usbip-win2——提供本机 USB/IP 客户端 + WHLK 认证签名的 vhci 驱动（免测试模式）
   （必需：DS5Hub 自带 usbip 服务端，但单机回环 attach 需要客户端与 vhci）

架构说明：服务端与客户端运行在同一台主机（回环）——
物理 DualSense → HidHide 隐藏 → DS5Hub hidapi 读取 → usbip 服务端(localhost:3240+N)
→ usbip-win2 客户端 attach → Windows 出现完整功能的虚拟 DualSense → 游戏全功能识别。

注意：本模块只做检测和引导，不执行实际安装操作（需要用户手动确认）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class ComponentStatus(str, Enum):
    INSTALLED = "installed"
    MISSING = "missing"
    UPDATE_REQUIRED = "update_required"
    PARTIAL = "partial"


@dataclass
class ComponentCheck:
    name: str
    status: ComponentStatus
    message: str = ""
    install_url: str = ""
    required: bool = True


class SystemInfo:
    """系统信息收集。"""
    
    @staticmethod
    def get_exe_path() -> str:
        return getattr(sys, "_MEIPASS", sys.executable)
    
    @staticmethod
    def is_admin() -> bool:
        try:
            import ctypes
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except Exception:
            return False
    
    @staticmethod
    def windows_version() -> str:
        try:
            import platform
            return platform.win32_ver()[0]
        except Exception:
            return "unknown"
    
    @staticmethod
    def program_files_dir() -> str:
        return os.environ.get("PROGRAMFILES", "C:\\Program Files")
    
    @staticmethod
    def local_appdata_dir() -> str:
        return os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
    
    @staticmethod
    def user_appdata_dir() -> str:
        return os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))


class ComponentDetector:
    """外部组件检测器。"""
    
    # ---- HidHide 检测 ----
    @staticmethod
    def check_hidhide() -> ComponentCheck:
        """检查 HidHide 是否已安装且服务运行中。"""
        # 查找 HidHideCLI.exe
        cli_found = False
        candidates = [
            os.path.join(os.environ.get("APPDATA", ""), "Programs",
                        "Nefarius Software Solutions", "eVasive Systems Supports Pvt Ltd",
                        "HidHide", "HidHideCLI.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", ""),
                        "Nefarius Software Solutions", "HidHide", "HidHideCLI.exe"),
        ]
        for path in candidates:
            if os.path.exists(path):
                cli_found = True
                break
        
        if not cli_found:
            return ComponentCheck(
                name="HidHide",
                status=ComponentStatus.MISSING,
                message="未找到 HidHide 驱动程序",
                install_url="https://github.com/nefarius/HidHide/releases",
                required=True,
            )
        
        # 检查服务是否运行（兼容中文 Windows 输出编码）
        try:
            out = subprocess.run(
                ["sc", "query", "HidHide"],
                capture_output=True, text=True, timeout=5,
                encoding="utf-8", errors="replace")
            if "RUNNING" not in out.stdout.upper():
                return ComponentCheck(
                    name="HidHide",
                    status=ComponentStatus.PARTIAL,
                    message="HidHide 驱动已安装但服务未运行",
                    install_url="",
                    required=True,
                )
        except Exception:
            pass
        
        return ComponentCheck(
            name="HidHide",
            status=ComponentStatus.INSTALLED,
            message="HidHide 驱动正常运行",
            required=True,
        )
    
    # ---- usbip-win2 检测 ----
    @staticmethod
    def check_usbip_win2() -> ComponentCheck:
        """检查 usbip-win2 是否已安装（本机回环 attach 的 USB/IP 客户端 + WHLK 签名 vhci 驱动来源）。"""
        # 1) PATH 中的 usbip.exe
        exe_found = False
        try:
            out = subprocess.run(
                ["where", "usbip"],
                capture_output=True, text=True, timeout=3,
                encoding="utf-8", errors="replace")
            if out.returncode == 0 and out.stdout.strip():
                exe_found = True
        except Exception:
            pass
        # 2) 标准安装目录 C:\Program Files\USBip\usbip.exe
        if not exe_found:
            pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
            if os.path.isfile(os.path.join(pf, "USBip", "usbip.exe")):
                exe_found = True

        if not exe_found:
            return ComponentCheck(
                name="usbip-win2",
                status=ComponentStatus.MISSING,
                message="未发现 usbip-win2（本机 attach 虚拟手柄必需，提供 WHLK 签名 vhci 驱动，免测试模式）",
                install_url="https://github.com/vadimgrn/usbip-win2/releases",
                required=True,
            )

        return ComponentCheck(
            name="usbip-win2",
            status=ComponentStatus.INSTALLED,
            message="usbip-win2 已安装（USB/IP 客户端 + WHLK 签名 vhci 驱动）",
            required=True,
        )
    
    # ---- Python 环境检测 ----
    @staticmethod
    def check_python_env() -> ComponentCheck:
        """检查运行环境。"""
        py_ver = sys.version_info
        if py_ver < (3, 10):
            return ComponentCheck(
                name="Python 运行时",
                status=ComponentStatus.UPDATE_REQUIRED,
                message=f"当前 Python {py_ver.major}.{py_ver.minor}，推荐 3.10+",
                required=True,
            )
        
        return ComponentCheck(
            name="Python 运行时",
            status=ComponentStatus.INSTALLED,
            message=f"Python {py_ver.major}.{py_ver.minor} OK",
            required=True,
        )
    
    @classmethod
    def check_all(cls) -> Dict[str, ComponentCheck]:
        """执行所有检测。"""
        results = {}
        results["hidhide"] = cls.check_hidhide()
        results["usbip_win2"] = cls.check_usbip_win2()
        results["python"] = cls.check_python_env()
        return results
    
    @property
    def has_critical_missing(self) -> bool:
        return any(
            c.status == ComponentStatus.MISSING and c.required
            for c in self._components.values()
        )


class InstallationHelper:
    """安装辅助工具。"""
    
    @staticmethod
    def open_download_page(url: str) -> None:
        """打开浏览器下载页面。"""
        import webbrowser
        webbrowser.open(url)
    
    @staticmethod
    def create_installer_script(components: Dict[str, ComponentCheck]) -> str:
        """生成一键安装脚本内容。"""
        lines = [
            "@echo off",
            "chcp 65001 >nul",
            "title DS5Hub 组件安装向导",
            "",
            "echo ========================================",
            "echo   DS5Hub 组件自动安装",
            "echo ========================================",
            "",
        ]
        
        for name, comp in components.items():
            if comp.install_url:
                lines.append(f'echo.')
                lines.append(f'echo [?] 检测到缺失组件: {name}')
                lines.append(f'echo     请访问以下网址下载安装:')
                lines.append(f'echo     {comp.install_url}')
                lines.append(f'echo.')
                lines.append(f'start "" "{comp.install_url}"')
        
        lines.extend([
            "",
            "echo.",
            "echo ========================================",
            "echo   安装完成后请重启电脑",
            "echo ========================================",
            "",
            "pause",
        ])
        
        return "\r\n".join(lines)
    
    @staticmethod
    def write_installer_script(path: str) -> None:
        """写出生成好的安装脚本到指定路径。"""
        detector = ComponentDetector()
        components = detector.check_all()
        script_content = InstallationHelper.create_installer_script(components)
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write(script_content)


def run_startup_detection() -> Dict[str, str]:
    """启动时快速检测各组件状态。"""
    detector = ComponentDetector()
    results = detector.check_all()
    
    output = {}
    for key, comp in results.items():
        output[key] = f"{comp.name}: {comp.status.value}"
    
    return output


if __name__ == "__main__":
    print("=" * 40)
    print("DS5Hub 组件检测")
    print("=" * 40)
    
    detector = ComponentDetector()
    results = detector.check_all()
    
    for key, comp in results.items():
        emoji = {"installed": "✅", "missing": "❌", "update_required": "⚠️",
                 "partial": "🟡"}.get(comp.status.value, "?")
        print(f"\n{emoji} {comp.name}")
        print(f"   状态: {comp.status.value}")
        print(f"   说明: {comp.message}")
        if comp.install_url:
            print(f"   下载: {comp.install_url}")
    
    print("\n" + "=" * 40)
