# -*- coding: utf-8 -*-
"""
一键环境部署编排器（InstallOrchestrator）。

目标：目标机上自动完成官方组件（HidHide / usbip-win2）的获取、静默安装、
就绪验证与基础配置，达到"只装 DS5Hub、零手动配置"的体验。

流程状态机：
  IDLE -> PREPARING(获取安装包: 内嵌/本地 redist/GitHub Releases 下载)
       -> INSTALLING(usbip-win2 静默 /VERYSILENT；HidHide 引导式，各触发一次 UAC)
       -> VERIFYING(服务与 CLI 就绪验证)
       -> POST(DS5Hub 自动白名单)
       -> DONE | NEEDS_REBOOT | FAILED

许可合规：捆绑/下载的均为各项目原封不动的官方安装包（GPL 系开源许可），
DS5Hub 不修改、不链接其代码，仅作为独立组件安装。

开发机零驱动约束：dry_run=True 时完整走状态机但不执行真实安装
（安装/验证/白名单全部模拟），用于无驱动环境的自动化测试。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from . import logger


class OrchState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    INSTALLING = "installing"
    VERIFYING = "verifying"
    POST = "post"
    DONE = "done"
    NEEDS_REBOOT = "needs_reboot"
    FAILED = "failed"


# 官方组件元数据。
# kind: "msi" = msiexec /qn 静默安装；
#       "exe" 带 silent_args = Inno Setup /VERYSILENT 静默安装（如 usbip-win2）；
#       "exe" 无 silent_args = 官方 GUI 安装器（实测 HidHide_*.exe 为自定义 setup，
#       asInvoker 且不含 /quiet /silent，采用"启动安装器 + 轮询检测就绪"的引导式安装）。
_OFFICIAL = {
    "hidhide": {
        "kind": "exe",
        "repo": "nefarius/HidHide",
        "asset": "HidHide_",      # 资产名前缀
        "suffix": "_x64.exe",      # 精确匹配 x64
        "display": "HidHide",
    },
    "usbip_win2": {
        "kind": "exe",
        "repo": "vadimgrn/usbip-win2",
        "asset": "USBip-",
        "suffix": "-x64.exe",
        "display": "usbip-win2",
        "silent_args": ["/VERYSILENT", "/NORESTART"],
    },
}

MSIEXEC_EXIT_OK = 0
MSIEXEC_EXIT_REBOOT = 3010


# ============ 通用工具 ============
def is_admin() -> bool:
    """当前进程是否管理员权限。"""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def find_usbip_cli() -> str:
    """定位 usbip.exe（usbip-win2 提供的本机 USB/IP 客户端）。"""
    # 1) PATH
    try:
        out = subprocess.run(
            ["where", "usbip"], capture_output=True, text=True,
            timeout=5, encoding="utf-8", errors="replace")
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0].strip()
    except Exception:  # noqa: BLE001
        pass
    # 2) 默认安装目录 C:\Program Files\USBip
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    cand = Path(pf) / "USBip" / "usbip.exe"
    if cand.is_file():
        return str(cand)
    return ""


def service_running(name: str) -> bool:
    """检查 Windows 服务是否 RUNNING。"""
    try:
        out = subprocess.run(
            ["sc", "query", name], capture_output=True, text=True,
            timeout=5, encoding="utf-8", errors="replace")
        return "RUNNING" in out.stdout.upper()
    except Exception:  # noqa: BLE001
        return False


def run_elevated(exe: str, args: List[str], timeout: int = 900) -> tuple:
    """以管理员权限运行 exe（必要时触发 UAC），返回 (exitcode, output)。"""
    if is_admin():
        try:
            r = subprocess.run(
                [exe] + args, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace")
            return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
        except Exception as e:  # noqa: BLE001
            return -1, str(e)
    # 非管理员：PowerShell Start-Process -Verb RunAs（用户在 UAC 中确认）
    arg_str = " ".join(f"'{a}'" for a in args)
    ps = (f"$p = Start-Process -FilePath '{exe}' -ArgumentList {arg_str} "
          f"-Verb RunAs -Wait -PassThru; exit $p.ExitCode")
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


# ============ 编排器 ============
class InstallOrchestrator:
    def __init__(self, cfg, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.state = OrchState.IDLE
        self.progress = 0                 # 0-100
        self.message = "空闲"
        self.error = ""
        self.reboot_required = False
        self.installed = {"hidhide": False, "usbip_win2": False}
        self.verify_result: Dict = {}
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
                "installed": dict(self.installed),
                "verify": dict(self.verify_result),
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
                logger.error(f"[部署] {msg}")
            else:
                logger.info(f"[部署] {msg}")
        except AssertionError:  # logger 未初始化（独立自测场景）
            print(f"[部署][{level}] {msg}")

    def _set(self, state: OrchState, progress: int, message: str) -> None:
        with self._lock:
            self.state = state
            self.progress = progress
            self.message = message

    # ---------- 启动 ----------
    def start(self) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False, "error": "部署已在进行中"}
            self.error = ""
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="ds5hub-orchestrator")
            self._thread.start()
        return {"ok": True, "state": self.state.value}

    # ---------- 主体流程 ----------
    def _run(self) -> None:
        try:
            self.reboot_required = False
            self.installed = {"hidhide": False, "usbip_win2": False}
            self._log("环境部署开始" + ("（dry_run 模拟，不真实安装）" if self.dry_run else ""))

            # Step 1: 准备安装包
            self._set(OrchState.PREPARING, 5, "获取官方安装包…")
            installers: Dict[str, Path] = {}
            for key in _OFFICIAL:
                p = self._prepare_installer(key)
                if not p:
                    raise RuntimeError(
                        f"{key} 安装包获取失败；可手动下载放入 redist 目录")
                installers[key] = p
                self._log(f"安装包就绪: {key} -> {p.name}")

            # Step 2: 安装（msi 静默 / exe 静默或引导式）
            self._set(OrchState.INSTALLING, 15, "安装组件…")
            for i, (key, path) in enumerate(installers.items()):
                spec = _OFFICIAL[key]
                kind = spec["kind"]
                if kind == "msi":
                    self._log(f"安装 {key}: msiexec /qn /norestart")
                    code = self._msi_install_elevated(path)
                    self._log(f"{key} msiexec 退出码 {code}")
                    if code == MSIEXEC_EXIT_REBOOT:
                        self.reboot_required = True
                    elif code != MSIEXEC_EXIT_OK:
                        raise RuntimeError(f"{key} 安装失败（msiexec 退出码 {code}）")
                    self.installed[key] = True
                elif spec.get("silent_args"):
                    # exe 静默安装（如 usbip-win2 的 Inno Setup /VERYSILENT /NORESTART）
                    ok = self._exe_install_silent(key, path, spec["silent_args"])
                    if not ok:
                        raise RuntimeError(f"{key} 静默安装未在限时内完成")
                    self.installed[key] = True
                else:  # exe 引导式安装（官方安装器无静默参数）
                    ok = self._exe_install_guided(key, path)
                    if not ok:
                        raise RuntimeError(f"{key} 安装未在限时内完成")
                    self.installed[key] = True
                self._set(OrchState.INSTALLING,
                          15 + int(35 * (i + 1) / len(installers)),
                          f"{key} 安装完成")

            # Step 3: 就绪验证
            self._set(OrchState.VERIFYING, 60, "验证组件就绪…")
            time.sleep(0.5)  # 服务注册缓冲
            self.verify_result = self._verify()
            self._log(f"验证结果: {self.verify_result}")

            # Step 4: 自动配置
            self._set(OrchState.POST, 85, "自动配置（DS5Hub 白名单）…")
            if not self.dry_run:
                try:
                    from .hidhid_manager import get_cli_path, register_app_as_whitelisted
                    cli = get_cli_path(force_refresh=True)
                    if cli:
                        ok = register_app_as_whitelisted(cli)
                        self._log(f"HidHide 白名单 DS5Hub: {'成功' if ok else '失败'}")
                    else:
                        self._log("HidHide CLI 未找到，跳过白名单（重启后可重试）", "warn")
                except Exception as e:  # noqa: BLE001
                    self._log(f"白名单配置异常（不影响部署）: {e}", "warn")
            else:
                self._log("dry_run: 跳过白名单配置")

            # 收尾
            if self.reboot_required:
                self._set(OrchState.NEEDS_REBOOT, 100,
                          "部署完成，需重启系统使驱动生效")
            else:
                self._set(OrchState.DONE, 100, "部署完成，组件就绪")
            self._log(f"部署结束: {self.state.value}")
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self._log(f"部署失败: {e}", "error")
            self._set(OrchState.FAILED, self.progress, f"失败: {e}")

    # ---------- 安装包获取 ----------
    def _redist_dir(self) -> Path:
        """redist 目录：内嵌(PyInstaller) -> 项目内 -> LOCALAPPDATA(可写 fallback)。"""
        candidates: List[Path] = []
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(Path(meipass) / "redist")
        candidates.append(Path(__file__).resolve().parent.parent / "redist")
        appdata = os.environ.get("LOCALAPPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "DS5Hub" / "redist")
        for c in candidates:
            if c.is_dir():
                return c
        fallback = candidates[-1]
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            fallback = Path(".")
        return fallback

    def _prepare_installer(self, key: str) -> Optional[Path]:
        spec = _OFFICIAL[key]
        d = self._redist_dir()
        suffix = spec["suffix"]
        # 已有官方安装包（资产前缀 + 后缀精确匹配）
        try:
            for f in sorted(d.iterdir(), reverse=True):
                if not f.is_file():
                    continue
                if f.name.startswith(spec["asset"]) and f.name.endswith(suffix):
                    self._log(f"使用本地安装包: {f}")
                    return f
        except Exception:  # noqa: BLE001
            pass
        if self.dry_run:
            # 占位文件（_dryrun_ 前缀避免被后续真实运行误用）
            fake = d / f"_dryrun_{key}{suffix}"
            try:
                fake.write_bytes(b"dry-run-placeholder")
            except Exception:  # noqa: BLE001
                return None
            return fake
        # GitHub Releases 下载
        try:
            url = self._gh_latest_asset(spec)
            if not url:
                self._log(f"{key}: 未能解析最新 release 的安装资产", "warn")
                return None
            dest = d / url.split("/")[-1]
            self._log(f"下载 {key}: {url}")
            self._download(url, dest)
            self._log(f"下载完成: {dest.name} ({dest.stat().st_size // 1024} KB)")
            return dest
        except Exception as e:  # noqa: BLE001
            self._log(f"{key} 下载失败: {e}", "warn")
            return None

    def _gh_latest_asset(self, spec: dict) -> Optional[str]:
        api = f"https://api.github.com/repos/{spec['repo']}/releases/latest"
        req = urllib.request.Request(api, headers={
            "User-Agent": "DS5Hub",
            "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.load(r)
        for a in data.get("assets", []):
            n = a.get("name", "")
            if n.startswith(spec["asset"]) and n.endswith(spec["suffix"]):
                return a.get("browser_download_url")
        return None

    def _download(self, url: str, dest: Path) -> None:
        req = urllib.request.Request(url, headers={"User-Agent": "DS5Hub"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)

    # ---------- 安装 ----------
    def _msi_install_elevated(self, msi_path: Path) -> int:
        if self.dry_run:
            time.sleep(0.4)
            return MSIEXEC_EXIT_OK
        code, _ = run_elevated(
            "msiexec.exe",
            ["/i", str(msi_path), "/qn", "/norestart"], timeout=900)
        return code

    def _exe_install_guided(self, key: str, exe_path: Path,
                            timeout: int = 600) -> bool:
        """引导式安装 GUI 安装器：启动（提权）→ 轮询检测产物就绪。

        HidHide 官方安装器为自定义 setup（实测无 /quiet /silent 静默参数、
        manifest asInvoker），只能弹出安装向导由用户点击完成；
        编排器在后台轮询检测安装产物出现后自动继续。
        """
        if self.dry_run:
            time.sleep(0.4)
            return True
        display = _OFFICIAL[key]["display"]
        self._log(f"启动 {display} 安装向导（请在弹出的窗口中完成安装）")
        self._set(OrchState.INSTALLING, self.progress,
                  f"请在 {display} 安装向导中完成安装…")
        try:
            if is_admin():
                subprocess.Popen([str(exe_path)])
            else:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                     "-Command",
                     f"Start-Process -FilePath '{exe_path}' -Verb RunAs"])
        except Exception as e:  # noqa: BLE001
            self._log(f"启动 {display} 安装器失败: {e}", "error")
            return False
        # 后台轮询检测安装产物出现
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._component_ready(key):
                self._log(f"{display} 安装完成（已检测到产物）")
                return True
            time.sleep(2)
        self._log(f"{display} 未在 {timeout}s 内检测到安装完成", "warn")
        return self._component_ready(key)

    def _exe_install_silent(self, key: str, exe_path: Path,
                            silent_args: list, timeout: int = 900) -> bool:
        """静默安装 exe 安装器（如 usbip-win2 的 Inno Setup /VERYSILENT）。

        与 _exe_install_guided 的区别：安装器原生支持静默参数，
        提权同步运行后无需用户交互，产物应自动就绪。
        """
        if self.dry_run:
            time.sleep(0.4)
            return True
        display = _OFFICIAL[key]["display"]
        self._log(f"静默安装 {display}: {' '.join(silent_args)}")
        code, _ = run_elevated(str(exe_path), list(silent_args), timeout=timeout)
        self._log(f"{display} 静默安装退出码 {code}")
        if code != 0:
            self._log(f"{display} 静默安装非零退出码，仍尝试检测产物", "warn")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._component_ready(key):
                self._log(f"{display} 静默安装完成（已检测到产物）")
                return True
            time.sleep(2)
        self._log(f"{display} 未在 {timeout}s 内检测到安装完成", "warn")
        return self._component_ready(key)

    def _component_ready(self, key: str) -> bool:
        """检测组件安装产物是否就绪。"""
        try:
            if key == "hidhide":
                from .hidhid_manager import get_cli_path
                return bool(get_cli_path(force_refresh=True))
            if key == "usbip_win2":
                return bool(find_usbip_cli())
        except Exception:  # noqa: BLE001
            pass
        return False

    # ---------- 验证 ----------
    def _verify(self) -> dict:
        if self.dry_run:
            return {"hidhide_cli": "(dry-run)", "hidhide_service": True,
                    "usbip_cli": "(dry-run)", "usbip_win2_installed": True,
                    "dry_run": True}
        from .hidhid_manager import get_cli_path, get_service_running
        hh_cli = get_cli_path(force_refresh=True)
        hh_svc = get_service_running(hh_cli) if hh_cli else False
        usbip = find_usbip_cli()
        return {"hidhide_cli": hh_cli or "", "hidhide_service": hh_svc,
                "usbip_cli": usbip, "usbip_win2_installed": bool(usbip)}


def _selftest() -> None:
    """模块自测：dry_run 状态机全流程。"""
    from .logger import init
    init(level="INFO", ring_size=100)

    class _Cfg:
        def get(self, k, d=None):
            return d
    o = InstallOrchestrator(_Cfg(), dry_run=True)
    o.start()
    for _ in range(100):
        time.sleep(0.1)
        if o.status()["state"] in ("done", "failed", "needs_reboot"):
            break
    assert o.status()["state"] == "done", o.status()
    assert o.installed["hidhide"] and o.installed["usbip_win2"]
    print("install_orchestrator selftest OK")


if __name__ == "__main__":
    _selftest()
