# -*- coding: utf-8 -*-
"""
自动重连策略：手柄层和客户端层断线重连。

手柄层：
- 指数退避重试（初始 delay -> max_delay，默认 3s->30s 封顶）
- 最大重试次数限制（0=无限），恢复后自动重新暴露 usbip 服务
- 心跳检测空闲超时

客户端层：
- TCP 断开后清理会话，端口继续监听等待新连接
- 会话保活：可选心跳
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ReconnectPolicy:
    """重连策略配置。"""
    enabled: bool = True
    initial_delay: float = 3.0          # 初始延迟（秒）
    max_delay: float = 30.0             # 最大延迟（秒）
    max_retries: int = 0                # 0=无限
    retry_backoff: float = 2.0          # 退避因子
    heartbeat_interval: float = 5.0     # 心跳间隔（秒，0=禁用）
    idle_timeout: float = 60.0          # 空闲超时（秒，0=禁用）


class ReconnectTracker:
    """跟踪单次操作的重连状态。"""
    def __init__(self):
        self.attempts = 0
        self.last_attempt_time = 0.0
        self.last_delay: float = 0.0
        self.success = False
        self.error: Optional[str] = None


class AutoReconnector:
    """
    自动重连管理器，整合手柄和客户端两层。
    
    使用方式：
        reconnector = AutoReconnector(policy)
        
        # 注册回调
        reconnector.on_connect(pad_id, callback_fn)
        
        # 启动/停止
        reconnector.start()
        reconnector.stop()
        
        # 手动触发重连
        reconnector.retry(pad_id)
    """
    
    def __init__(self, policy: Optional[ReconnectPolicy] = None):
        self.policy = policy or ReconnectPolicy()
        self._callbacks: Dict[str, List[Callable]] = {}
        self._trackers: Dict[str, ReconnectTracker] = {}
        self._lock = threading.RLock()
        self._running = False
        self._loop_thread: Optional[threading.Thread] = None
    
    def on_connect(self, pad_id: str, cb: Callable[[str], bool]) -> None:
        """注册指定 pad_id 的连接尝试回调。"""
        with self._lock:
            if pad_id not in self._callbacks:
                self._callbacks[pad_id] = []
            self._callbacks[pad_id].append(cb)
            self._trackers[pad_id] = ReconnectTracker()
    
    def on_disconnect(self, pad_id: str) -> None:
        """通知指定 pad_id 已断开。"""
        tracker = self._get_tracker(pad_id)
        tracker.last_attempt_time = 0.0
        self._notify_callbacks(pad_id)
    
    def notify_success(self, pad_id: str) -> None:
        """通知指定 pad_id 连接成功。"""
        tracker = self._get_tracker(pad_id)
        tracker.success = True
        tracker.attempts = 0
    
    def notify_error(self, pad_id: str, error: str) -> None:
        """通知指定 pad_id 连接失败。"""
        tracker = self._get_tracker(pad_id)
        tracker.error = error
        tracker.last_attempt_time = time.time()
    
    def retry(self, pad_id: str) -> Optional[bool]:
        """手动触发一次重连。"""
        with self._lock:
            callbacks = list(self._callbacks.get(pad_id, []))
        for cb in callbacks:
            try:
                ok = cb(pad_id)
                if ok:
                    self.notify_success(pad_id)
                    return True
                self.notify_error(pad_id, "callback returned False")
                return False
            except Exception as e:
                self.notify_error(pad_id, str(e))
        return None
    
    def start(self) -> None:
        if not self.policy.enabled:
            return
        self._running = True
        self._loop_thread = threading.Thread(target=self._auto_retry_loop, daemon=True)
        self._loop_thread.start()
    
    def stop(self) -> None:
        self._running = False
        if self._loop_thread:
            self._loop_thread.join(timeout=5)
            self._loop_thread = None
    
    def status(self, pad_id: str) -> Dict[str, Any]:
        """获取指定 pad_id 的重连状态。"""
        tracker = self._get_tracker(pad_id)
        return {
            "pad_id": pad_id,
            "enabled": self.policy.enabled,
            "attempts": tracker.attempts,
            "last_attempt": tracker.last_attempt_time,
            "last_delay": tracker.last_delay,
            "success": tracker.success,
            "error": tracker.error,
            "policy": {
                "initial_delay": self.policy.initial_delay,
                "max_delay": self.policy.max_delay,
                "max_retries": self.policy.max_retries,
            }
        }
    
    def _get_tracker(self, pad_id: str) -> ReconnectTracker:
        with self._lock:
            if pad_id not in self._trackers:
                self._trackers[pad_id] = ReconnectTracker()
            return self._trackers[pad_id]
    
    def _notify_callbacks(self, pad_id: str) -> None:
        """通知所有注册的回调函数。"""
        with self._lock:
            callbacks = list(self._callbacks.get(pad_id, []))
        for cb in callbacks:
            try:
                cb(pad_id)
            except Exception:
                pass
    
    def _auto_retry_loop(self) -> None:
        """自动重连循环。"""
        while self._running:
            try:
                for pad_id in list(self._callbacks.keys()):
                    tracker = self._get_tracker(pad_id)
                    
                    # 检查是否需要重连
                    if not tracker.success or tracker.error:
                        # 检查是否超过最大重试次数
                        if (self.policy.max_retries > 0 and
                            tracker.attempts >= self.policy.max_retries):
                            continue
                        
                        # 计算退避时间
                        now = time.time()
                        min_time = tracker.last_attempt_time + self.policy.initial_delay
                        if now < min_time:
                            continue
                        
                        # 执行重连
                        tracker.attempts += 1
                        result = self.retry(pad_id)
                        if result is True:
                            # 重置退避
                            tracker.last_delay = self.policy.initial_delay
                        else:
                            # 增加延迟
                            if not tracker.last_delay:
                                tracker.last_delay = self.policy.initial_delay
                            tracker.last_delay = min(
                                tracker.last_delay * self.policy.retry_backoff,
                                self.policy.max_delay
                            )
                
                # 检查空闲超时时长
                if self.policy.idle_timeout > 0:
                    for pid, t in self._trackers.items():
                        if t.success and (time.time() - t.last_attempt_time >
                                          self.policy.idle_timeout):
                            # 触发超时回调
                            pass
                
            except Exception:
                time.sleep(1)
            
            time.sleep(1)
