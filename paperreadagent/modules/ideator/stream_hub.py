"""stream_hub.py — In-process pub/sub for roundtable streaming events.

Pure primitive: no SSE format, no LLM, no agent knowledge. Just asyncio.Queue
fan-out with drop-on-full backpressure and sentinel-based clean shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

_SENTINEL = object()  # graceful subscriber shutdown marker


class Subscription:
    """Async iterator + close() handle for a roundtable stream subscription.

    Use as::

        sub = hub.subscribe(rt_id)
        try:
            async for evt in sub:
                ...
        finally:
            sub.close()

    Registered eagerly on creation. The recommended cleanup path is an
    explicit ``close()`` call in the consumer's ``finally`` block (e.g. the
    SSE handler). ``__del__`` is retained only as a safety net for accidental
    leaks — it is fragile on PyPy, during cyclic GC, and at interpreter
    shutdown, so callers must not rely on it.
    """

    def __init__(
        self, hub: "RoundtableStreamHub", rt_id: int, q: asyncio.Queue
    ) -> None:
        self._hub = hub
        self._rt_id = rt_id
        self._q = q
        self._closed = False

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> dict:
        event = await self._q.get()
        if event is _SENTINEL:
            raise StopAsyncIteration
        return event

    def close(self) -> None:
        """Explicitly unregister this subscriber. Safe to call multiple times.

        Task 5's SSE handler should call this in a ``finally`` block for
        deterministic cleanup that doesn't rely on GC finalization.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._hub._unregister(self._rt_id, self._q)
        except Exception:
            logger.debug(
                "[StreamHub] Subscription.close swallowed exception",
                exc_info=True,
            )

    def __del__(self) -> None:
        """Safety net: if caller forgets close(), GC will eventually clean up.

        Don't rely on this — call close() explicitly. Stays silent here
        because the logging module may already be gone during interpreter
        shutdown.
        """
        if not self._closed:
            try:
                self._hub._unregister(self._rt_id, self._q)
            except Exception:
                pass  # interpreter shutdown / hub gone


class RoundtableStreamHub:
    """In-process fan-out by rt_id.

    publish(rt_id, event): non-blocking; drops chunk if subscriber queue full.
    subscribe(rt_id): synchronously registers a queue and returns a
        :class:`Subscription`. Callers should ``close()`` the subscription
        in a ``finally`` block for deterministic cleanup.
    close_rt(rt_id): sends sentinel to all subscribers of rt_id.
    """

    def __init__(self, *, queue_maxsize: int = 1000):
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._queue_maxsize = queue_maxsize
        # No _lock: all _subscribers mutations happen synchronously in the
        # single asyncio event loop; no preemption between non-await ops.

    def subscribe(self, rt_id: int) -> Subscription:
        """Synchronously register a new subscriber queue and return its iterator.

        Returns a :class:`Subscription` usable with ``async for``. The queue
        is registered before returning, so even if the consumer is cancelled
        before its first ``__anext__`` completes the producer can still
        enqueue chunks. Callers should call ``Subscription.close()`` in a
        ``finally`` block for deterministic cleanup.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._subscribers[rt_id].add(q)
        return Subscription(self, rt_id, q)

    async def publish(self, rt_id: int, event: dict) -> None:
        queues = list(self._subscribers.get(rt_id, ()))  # snapshot
        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    "[StreamHub] subscriber queue full for rt=%s, dropping chunk",
                    rt_id,
                )

    async def close_rt(self, rt_id: int) -> None:
        queues = list(self._subscribers.get(rt_id, ()))
        for q in queues:
            try:
                q.put_nowait(_SENTINEL)
            except asyncio.QueueFull:
                pass  # subscriber will eventually see sentinel after draining

    def subscriber_count(self, rt_id: int) -> int:
        return len(self._subscribers.get(rt_id, ()))

    def _unregister(self, rt_id: int, q: asyncio.Queue) -> None:
        """Remove a specific queue from the subscriber set.

        Called from :meth:`Subscription.close` (explicit, recommended) or
        ``Subscription.__del__`` (safety net). Synchronous — safe to call
        from GC finalizer.
        """
        self._subscribers[rt_id].discard(q)
        if not self._subscribers[rt_id]:
            del self._subscribers[rt_id]


_HUB: RoundtableStreamHub | None = None


def get_stream_hub() -> RoundtableStreamHub:
    """Module-level singleton accessor."""
    global _HUB
    if _HUB is None:
        _HUB = RoundtableStreamHub()
    return _HUB
