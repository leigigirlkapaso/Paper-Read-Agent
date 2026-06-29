"""
core/event_bus.py
EventBus — 模块间松耦合事件通信。基于 asyncio.Event + 回调列表。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Awaitable

from .decorators import stable, evolving

logger = logging.getLogger(__name__)

EventHandler = Callable[..., Awaitable[None]]


class EventBus:
    """
    极简事件总线。模块通过 subscribe 订阅，通过 emit 发送。

    白名单模式：模块只能订阅核心事件和显式声明的依赖模块事件。
    实际上当前阶段不做硬限制，仅记录日志。
    """

    def __init__(self):
        self._subscribers: dict[str, list[tuple[str, EventHandler]]] = {}
        self._dead_letter_count = 0

    @evolving
    def subscribe(self, module: str, event_pattern: str, handler: EventHandler) -> None:
        """
        订阅事件。
        - module: 订阅者模块名（用于日志和卸载）
        - event_pattern: 事件名，支持前缀匹配（如 "thinker:*"）
        - handler: async callable(event_name, **data)
        """
        if event_pattern not in self._subscribers:
            self._subscribers[event_pattern] = []
        self._subscribers[event_pattern].append((module, handler))
        logger.debug(f"[EventBus] {module} 订阅 {event_pattern}")

    @evolving
    async def emit(self, event: str, **data) -> None:
        """
        发送事件，通知所有匹配的订阅者。
        处理器异常不会影响其他订阅者。
        """
        data.setdefault("event_id", uuid.uuid4().hex)
        data.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

        tasks = []
        for pattern, handlers in self._subscribers.items():
            if _match_event(pattern, event):
                for module, handler in handlers:
                    tasks.append((module, event, handler, data))

        if not tasks:
            return

        async def _safe_invoke(module, event_name, handler, payload):
            try:
                await handler(event_name, **payload)
            except Exception:
                self._dead_letter_count += 1
                logger.exception(
                    f"[EventBus] {module} 处理 {event_name} 时出错 "
                    f"(死信累计: {self._dead_letter_count})"
                )

        try:
            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(_safe_invoke(m, e, h, d))
                    for m, e, h, d in tasks
                ],
                timeout=30,
            )
            if pending:
                logger.error(
                    f"[EventBus] 事件 {event} 处理超时 (30s)，"
                    f"已完成 {len(done)}/{len(tasks)} 个处理器，"
                    f"{len(pending)} 个未完成（未取消）"
                )
        except Exception:
            logger.exception(f"[EventBus] 事件 {event} 分发异常")

    @evolving
    def unsubscribe_module(self, module: str) -> None:
        """移除某模块的所有订阅。模块卸载时调用。"""
        for pattern in list(self._subscribers.keys()):
            self._subscribers[pattern] = [
                (m, h) for m, h in self._subscribers[pattern] if m != module
            ]
            if not self._subscribers[pattern]:
                del self._subscribers[pattern]
        logger.info(f"[EventBus] 已移除 {module} 的所有订阅")


def _match_event(pattern: str, event: str) -> bool:
    if pattern == event:
        return True
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        return event.startswith(prefix)
    if pattern.endswith(">"):
        # 精确前缀（不含通配符）
        prefix = pattern[:-1]
        return event.startswith(prefix) and event != prefix
    return False
