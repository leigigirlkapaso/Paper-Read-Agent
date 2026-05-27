"""
core/tests/test_event_bus.py
测试事件总线：订阅、发送、模式匹配、异常隔离。
"""

import pytest

from core.event_bus import EventBus, _match_event


class TestEventMatching:
    def test_exact_match(self):
        assert _match_event("thinker:message:sent", "thinker:message:sent")

    def test_prefix_wildcard(self):
        assert _match_event("thinker:*", "thinker:message:sent")
        assert _match_event("thinker:*", "thinker:resolution:extracted")

    def test_prefix_no_match(self):
        assert not _match_event("thinker:*", "literature:paper:imported")

    def test_exact_no_match(self):
        assert not _match_event("thinker:message:sent", "thinker:message:received")


class TestEventBus:
    @pytest.mark.asyncio
    async def test_emit_and_subscribe(self):
        bus = EventBus()
        received = []

        async def handler(event, **data):
            received.append((event, data))

        bus.subscribe("test", "test:event", handler)
        await bus.emit("test:event", payload="hello")

        assert len(received) == 1
        assert received[0][0] == "test:event"
        assert received[0][1]["payload"] == "hello"
        assert "event_id" in received[0][1]

    @pytest.mark.asyncio
    async def test_wildcard_subscription(self):
        bus = EventBus()
        received = []

        async def handler(event, **data):
            received.append(event)

        bus.subscribe("test", "test:*", handler)
        await bus.emit("test:event_a")
        await bus.emit("test:event_b")

        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self):
        bus = EventBus()
        results = set()

        async def h1(event, **data):
            results.add("h1")

        async def h2(event, **data):
            results.add("h2")

        bus.subscribe("a", "test:event", h1)
        bus.subscribe("b", "test:event", h2)
        await bus.emit("test:event")

        assert results == {"h1", "h2"}

    @pytest.mark.asyncio
    async def test_handler_exception_isolation(self):
        bus = EventBus()
        results = []

        async def bad(event, **data):
            raise RuntimeError("boom")

        async def good(event, **data):
            results.append("ok")

        bus.subscribe("a", "test:event", bad)
        bus.subscribe("b", "test:event", good)
        await bus.emit("test:event")

        assert results == ["ok"]

    @pytest.mark.asyncio
    async def test_unsubscribe_module(self):
        bus = EventBus()
        received = []

        async def handler(event, **data):
            received.append(event)

        bus.subscribe("mod", "test:*", handler)
        bus.unsubscribe_module("mod")
        await bus.emit("test:event")

        assert len(received) == 0
