# -*- coding: utf-8 -*-
r"""
一键卸载编排器（UninstallOrchestrator）。

与一键部署对等的完整反操作，顺序经过安全设计：
任何一步失败即中止并报告，绝不出现"驱动已卸、隐藏仍在"的死锁态。

步骤链：
  UNHIDING    解除所有 HidHide 隐藏（最关键第一步——若先卸驱动，
              真实手柄会直接从系统消失，用户会以为手柄坏了）
  STOPPING    停止 DS5Hub 全部服务（usbip/attach/重连/托盘）
  AUTOSTART   移除 HKCU 开机自启
  COMPONENTS  可选：静默卸载官方组件（默认关闭——HidHide 是通用过滤驱动，
              Steam 输入映射等第三方工具可能依赖；从注册表 Uninstall 键
              枚举官方 MSI 的 ProductCode 后 msiexec /x {GUID} /qn）
  CLEANUP     可选：清理 %APPDATA%\DS5Hub（配置/日志）与 redist 缓存
  SELF_DELETE 延迟自删 exe（仅 PyInstaller frozen 单文件模式；
              源码运行绝不自删，保护开发环境）
  DONE | NEEDS_REBOOT | FAILED

开发机零驱动约束：orchestrator.dry_run=True 时全流程模拟，不做任何真实
系统变更（不删注册表/不卸组件/不删文件）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import logger
from .install_orchestrator import MSIEXEC_EXIT_OK, MSIEXEC_EXIT_REBOOT, run_elevated


class UninstallState(str, Enum):
    IDLE = "idle"
    UNHIDING = "unhiding"
    STOPPING = "stopping"
    AUTOSTART = "autostart"
    COMPONENTS = "components"
    CLEANUP = "cleanup"
    SELF_DELETE = "self_delete"
    DONE = "done"
    NEEDS_REBOOT = "needs_reboot"
    FAILED = "failed"


# 组件在"应用和功能"里的显示名关键词（小写匹配）
_COMPONENT_KEYWORDS = {
    "hidhide": ["hidhide"],
    "usbipd": ["usbipd"],
}


def _read_uninstall_entry(key) -> tuple:
    """读取一个 Uninstall 子键的 (DisplayName, UninstallString)。"""
    try:
        import winreg
    except ImportError:
        return "", ""
    try:
        display = str(winreg.QueryValueEx(key, "DisplayName")[0])
        uninst = str(winreg.QueryValueEx(key, "UninstallString")[0])
        return display, uninst
    except OSError:
        return "", ""


def find_component_uninstall() -> Dict[str, dict]:
    """从注册表 Uninstall 键枚举官方组件的 MSI 卸载信息。

    返回 {组件: {"display_name", "product_code", "uninstall_string"}}，
    仅返回 MsiExec 系（可静默重放）的条目；未安装则不含该键。
    """
    results: Dict[str, dict] = {}
    if os.name != "nt":
        return results
    try:
        import winreg
    except ImportError:
        return results

    roots = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for root in roots:
        try:
            hkey = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        with hkey:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(hkey, i)
                    i += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(hkey, sub) as k:
                        display, uninst = _read_uninstall_entry(k)
                except OSError:
                    continue
                if not display or not uninst:
                    continue
                low = display.lower()
                for comp, kws in _COMPONENT_KEYWORDS.items():
                    if comp in results:
                        continue
                    if not any(kw in low for kw in kws):
                        continue
                    # MsiExec.exe /X{GUID} 或 /I{GUID} → 提取 GUID
                    guid = ""
                    if ("{" in uninst and "}" in uninst
                            and "msiexec" in uninst.lower()):
                        guid = uninst[uninst.index("{"):
                                      uninst.index("}") + 1]
                    results[comp] = {
                        "display_name": display,
                        "product_code": guid,
                        "uninstall_string": uninst,
                    }
    return results


class UninstallOrchestrator:
    def __init__(self, cfg, stop_callback: Optional[Callable] = None,
                 dry_run: bool = False):
        self.cfg = cfg
        self.stop_callback = stop_callback
        self.dry_run = dry_run
        self.state = UninstallState.IDLE
        self.progress = 0
        self.message = "空闲"
        self.error = ""
        self.reboot_required = False
        self.removed_components: Dict[str, bool] = {}
        self.steps_log: List[dict] = []
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()

    # ---------- 状态视图 ----------
    def status(self) -> dict:
        with self._lock:
            return {
                "state": self.state.value,
                "progress": self.progress,
                "message": self.message,
                "error": self.error,
                "reboot_required": self.reboot_required,
                "removed_components": dict(self.removed_components),
                "dry_run": self.dry_run,
                "running": self._thread is not None and self._thread.is_alive(),
                "log": self.steps_log[-40:],
            }

    def _log(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self.steps_log.append(
                {"t": time.strftime("%H:%M:%S"), "level": level, "msg": msg})
        try:
            if level == "error":
                logger.error(f"[卸载] {msg}")
            else:
                logger.info(f"[卸载] {msg}")
        except AssertionError:  # logger 未初始化（独立自测场景）
            print(f"[卸载][{level}] {msg}")

    def _set(self, state: UninstallState, progress: int, message: str) -> None:
        with self._lock:
            self.state = state
            self.progress = progress
            self.message = message

    # ---------- 启动 ----------
    def start(self, remove_components: bool = False,
              remove_config: bool = True) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "卸载已在进行中"}
            self.error = ""
            self.reboot_required = False
            self.removed_components = {}
            self._thread = threading.Thread(
                target=self._run,
                args=(remove_components, remove_config),
                daemon=True, name="ds5hub-uninstaller")
            self._thread.start()
        return {"ok": True, "state": self.state.value}

    # ---------- 主体流程 ----------
    def _run(self, remove_components: bool, remove_config: bool) -> None:
        try:
            self._log("一键卸载开始" +
                      ("（dry_run 模拟，不做真实变更）" if self.dry_run else ""))

            # Step 1: 解除隐藏（必须最先做）
            self._set(UninstallState.UNHIDING, 10, "解除手柄隐藏…")
            self._step_unhide()

            # Step 2: 停止服务
            self._set(UninstallState.STOPPING, 25, "停止 DS5Hub 服务…")
            if self.stop_callback:
                try:
                    self.stop_callback()
                    self._log("DS5Hub 服务已停止")
                except Exception as e:  # noqa: BLE001
                    self._log(f"停止服务异常（继续卸载）: {e}", "warn")

            # Step 3: 移除自启
            self._set(UninstallState.AUTOSTART, 40, "移除开机自启…")
            if not self.dry_run:
                from .autostart import set_enabled as autostart_set
                ok = autostart_set(False)
                self._log(f"开机自启移除: {'成功' if ok else '失败/不存在'}")
            else:
                self._log("dry_run: 跳过自启移除")

            # Step 4: 卸载官方组件（可选）
            self._set(UninstallState.COMPONENTS, 55, "卸载官方组件…")
            if remove_components:
                self._step_remove_components()
            else:
                self._log("保留 HidHide / usbipd-win（未勾选组件卸载）")

            # Step 5: 清理数据
            self._set(UninstallState.CLEANUP, 80, "清理配置与缓存…")
            if remove_config:
                self._step_cleanup()
            else:
                self._log("保留配置数据（未勾选）")

            # Step 6: 自删 exe
            self._set(UninstallState.SELF_DELETE, 95, "安排程序自删…")
            self._self_delete()

            # 收尾
            if self.reboot_required:
                self._set(UninstallState.NEEDS_REBOOT, 100,
                          "卸载完成，建议重启系统彻底清除驱动")
            else:
                self._set(UninstallState.DONE, 100, "卸载完成")
            self._log(f"卸载结束: {self.state.value}")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self._log(f"卸载失败: {e}", "error")
            self._set(UninstallState.FAILED, self.progress, f"失败: {e}")

    # ---------- 各步骤 ----------
    def _step_unhide(self) -> None:
        if self.dry_run:
            self._log("dry_run: 跳过解除隐藏")
            return
        from .hidhid_manager import get_cli_path, unhide_all
        cli = get_cli_path()
        if not cli:
            self._log("HidHide CLI 不存在（未安装或已卸载），跳过解除隐藏")
            return
        ok = unhide_all(cli)
        self._log(f"解除全部隐藏: {'成功' if ok else '失败'}")
        if not ok:
            self._log("解除隐藏失败——继续卸载可能导致手柄不可见，"
                      "建议先手动解除（仪表盘 → 取消所有隐藏）", "warn")

    def _step_remove_components(self) -> None:
        if self.dry_run:
            self._log("dry_run: 模拟卸载组件 hidhide/usbipd")
            self.removed_components = {"hidhide": True, "usbipd": True}
            return
        entries = find_component_uninstall()
        if not entries:
            self._log("注册表中未找到可卸载的官方组件")
            return
        for comp, info in entries.items():
            guid = info.get("product_code", "")
            if not guid:
                self._log(f"{comp} 非 MSI 安装（{info['uninstall_string']}），"
                          "跳过静默卸载，请手动处理", "warn")
                continue
            self._log(f"卸载 {info['display_name']}（msiexec /x /qn）")
            code, _ = run_elevated(
                "msiexec.exe", ["/x", guid, "/qn", "/norestart"], timeout=600)
            self._log(f"{comp} msiexec 退出码 {code}")
            ok = code in (MSIEXEC_EXIT_OK, MSIEXEC_EXIT_REBOOT)
            if code == MSIEXEC_EXIT_REBOOT:
                self.reboot_required = True
            self.removed_components[comp] = ok
            if not ok:
                self._log(f"{comp} 卸载失败，中止后续组件卸载", "warn")
                break

    def _step_cleanup(self) -> None:
        if self.dry_run:
            self._log("dry_run: 跳过配置/缓存清理")
            return
        targets = []
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            targets.append(Path(appdata) / "DS5Hub")      # 配置 + 日志
        localapp = os.environ.get("LOCALAPPDATA", "")
        if localapp:
            targets.append(Path(localapp) / "DS5Hub" / "redist")  # msi 缓存
        for t in targets:
            if t.exists():
                try:
                    shutil.rmtree(t, ignore_errors=True)
                    self._log(f"已清理: {t}")
                except Exception as e:  # noqa: BLE001
                    self._log(f"清理失败 {t}: {e}", "warn")

    def _self_delete(self) -> None:
        if self.dry_run:
            self._log("dry_run: 跳过自删")
            return
        if not getattr(sys, "frozen", False):
            self._log("源码运行环境，跳过 exe 自删（安全保护）", "warn")
            return
        exe = sys.executable
        script = ('@echo off\r\n'
                  'timeout /t 3 /nobreak >nul\r\n'
                  f'del /f /q "{exe}"\r\n')
        bat = Path(os.environ.get("TEMP", ".")) / "ds5hub_uninstall_selfdel.bat"
        try:
            bat.write_text(script, encoding="ascii")
            flags = 0
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags = (subprocess.DETACHED_PROCESS |
                         subprocess.CREATE_NEW_PROCESS_GROUP)
            subprocess.Popen(
                ["cmd", "/c", str(bat)],
                creationflags=flags, close_fds=True,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            self._log("自删已安排：程序退出 3 秒后删除 exe")
        except Exception as e:  # noqa: BLE001
            self._log(f"自删安排失败（不影响卸载，可手动删除 exe）: {e}", "warn")


def _selftest() -> None:
    """模块自测：dry_run 全流程。"""
    from .logger import init
    init(level="INFO", ring_size=100)
    stopped = []
    o = UninstallOrchestrator(
        cfg=None, stop_callback=lambda: stopped.append(1), dry_run=True)
    o.start(remove_components=True)
    for _ in range(100):
        time.sleep(0.1)
        if o.status()["state"] in ("done", "failed", "needs_reboot"):
            break
    st = o.status()
    assert st["state"] == "done", st
    assert stopped == [1], "stop_callback 未调用"
    assert o.removed_components == {"hidhide": True, "usbipd": True}
    print("uninstall_orchestrator selftest OK")


if __name__ == "__main__":
    _selftest()
