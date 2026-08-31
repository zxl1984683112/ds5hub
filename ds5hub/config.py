# -*- coding: utf-8 -*-
"""
DS5Hub 配置管理。

支持：
- JSON 配置文件（~/.ds5hub/config.json）
- 环境变量覆盖
- 默认值 fallback
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


_DEFAULTS = {
    # ---- 日志 ----
    "log.dir": None,                       # None=自动到 APPDATA
    "log.level": "INFO",
    "log.max_bytes": 10 * 1024 * 1024,     # 10MB
    "log.backup_count": 5,
    "log.ring_size": 2000,                 # 内存环形缓存条数

    # ---- USB/IP 服务 ----
    "usbip_host": "0.0.0.0",
    "usbip_base_port": 3240,
    "usbip_backlog": 16,

    # ---- Web UI ----
    "web_host": "127.0.0.1",
    "web_port": 8080,
    "web_token": "",                       # Bearer token，空=不鉴权

    # ---- 手柄 ----
    # （模拟模式已移除，手柄一律通过 hidapi 真实枚举）

    # ---- 自动重连 ----
    "reconnect.enabled": True,
    "reconnect.initial_delay": 3.0,        # 初始重连延迟（秒）
    "reconnect.max_delay": 30.0,           # 最大重连延迟
    "reconnect.max_retries": 0,            # 最大重试次数（0=无限）
    "reconnect.retry_backoff": 2.0,        # 退避因子

    # ---- HidHide ----
    "hidhide.auto_whitelist": True,        # 启动时自动白名单
    "hidhide.auto_hide": True,             # 启动时自动隐藏设备

    # ---- 一键环境部署 ----
    "orchestrator.dry_run": False,         # True=模拟安装流程（开发机零驱动测试）
    "orchestrator.auto_attach": True,      # 部署完成后自动对本机 attach 手柄
    "orchestrator.attach_interval": 3.0,   # 自动 attach 扫描间隔（秒）

    # ---- 托盘 ----
    "tray.icon_path": "",                  # 自定义图标路径，空=程序内置
}


def _deep_set(d: dict, key: str, value: Any) -> None:
    """深层字典设置（支持 dotted key）。"""
    parts = key.split(".")
    curr = d
    for part in parts[:-1]:
        if part not in curr or not isinstance(curr[part], dict):
            curr[part] = {}
        curr = curr[part]
    curr[parts[-1]] = value


class Config:
    def __init__(self, path: str | None = None):
        self._data = dict(_DEFAULTS)
        self._path = self._resolve_path(path)
        self._load_file()
        self._apply_env()

    def _resolve_path(self, path: str | None) -> Path:
        if path:
            return Path(path)
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "DS5Hub" / "config.json"

    def _load_file(self) -> None:
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                override = json.load(f)
            self._merge(self._data, override)
        except Exception as e:  # noqa: BLE001
            from . import logger
            logger.warn(f"加载配置失败: {e}")

    def _merge(self, base: dict, override: dict) -> None:
        """递归合并配置。"""
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge(base[k], v)
            else:
                base[k] = v

    def _apply_env(self) -> None:
        """从环境变量加载配置（KEY_PREFIX_XXX 格式）。"""
        prefix = "DS5HUB_"
        for key, default_val in _DEFAULTS.items():
            env_key = prefix + key.upper().replace(".", "_")
            env_val = os.environ.get(env_key)
            if env_val is not None:
                # 类型转换
                if isinstance(default_val, int):
                    self.set(key, int(env_val))
                elif isinstance(default_val, float):
                    self.set(key, float(env_val))
                elif isinstance(default_val, bool):
                    self.set(key, env_val.lower() in ("true", "1", "yes"))
                else:
                    self.set(key, env_val)

    def get(self, dotted: str, default: Any = None) -> Any:
        parts = dotted.split(".")
        curr = self._data
        for part in parts:
            if isinstance(curr, dict) and part in curr:
                curr = curr[part]
            else:
                return default
        return curr

    def all(self) -> dict[str, Any]:
        return dict(self._data)

    def set(self, dotted: str, value: Any) -> None:
        _deep_set(self._data, dotted, value)

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False, sort_keys=True)

    @property
    def path(self) -> Path:
        return self._path
