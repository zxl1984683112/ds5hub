# -*- coding: utf-8 -*-
"""
DS5Hub 日志系统：
- RotatingFileHandler 写文件（%LOCALAPPDATA% 下 DS5Hub/logs/ds5hub.log）
- RingHandler 内存环形缓冲（供 Web /api/logs 轮询）
- 支持运行期调整级别
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

_RING = deque(maxlen=2000)          # (ts, level, msg)
_RING_LOCK = threading.Lock()

LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
          "WARN": logging.WARNING, "WARNING": logging.WARNING,
          "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL}


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            with _RING_LOCK:
                _RING.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                              record.levelname, msg))
        except Exception:  # noqa: BLE001
            pass


class DS5Logger:
    def __init__(self, log_dir: str | None = None,
                 level: str = "INFO",
                 max_bytes: int = 10 * 1024 * 1024,
                 backup_count: int = 5,
                 ring_size: int = 2000):
        global _RING
        _RING = deque(maxlen=ring_size)

        self._log = logging.getLogger("ds5hub")
        self._log.setLevel(LEVELS.get(level.upper(), logging.INFO))
        self._log.handlers.clear()
        self._log.propagate = False

        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")

        if log_dir:
            self.log_dir = Path(log_dir)
        else:
            self.log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "DS5Hub" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            self.log_dir / "ds5hub.log",
            maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        file_handler.setFormatter(fmt)
        self._log.addHandler(file_handler)

        ring_handler = RingHandler()
        ring_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        self._log.addHandler(ring_handler)

    @property
    def logger(self) -> logging.Logger:
        return self._log

    def set_level(self, level: str) -> None:
        lv = LEVELS.get(level.upper())
        if lv is not None:
            self._log.setLevel(lv)

    def get_level(self) -> str:
        return logging.getLevelName(self._log.level)

    def recent(self, limit: int = 500) -> list[dict[str, Any]]:
        with _RING_LOCK:
            items = list(_RING)[-limit:]
        return [{"ts": t, "level": lv, "msg": m} for t, lv, m in items]

    def tail_file(self, lines: int = 200) -> str:
        fp = self.log_dir / "ds5hub.log"
        if not fp.exists():
            return ""
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                data = f.readlines()
            return "".join(data[-lines:])
        except Exception as e:  # noqa: BLE001
            return f"(读取日志失败: {e})"


# 便捷单例（由 main 初始化后使用）
_instance: DS5Logger | None = None


def init(log_dir=None, level="INFO", max_bytes=10*1024*1024,
         backup_count=5, ring_size=2000) -> DS5Logger:
    global _instance
    _instance = DS5Logger(log_dir, level, max_bytes, backup_count, ring_size)
    return _instance


def get() -> DS5Logger:
    assert _instance is not None, "logger 未初始化"
    return _instance


def debug(msg: str) -> None:
    get().logger.debug(msg)


def info(msg: str) -> None:
    get().logger.info(msg)


def warn(msg: str) -> None:
    get().logger.warning(msg)


def error(msg: str) -> None:
    get().logger.error(msg)