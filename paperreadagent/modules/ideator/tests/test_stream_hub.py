"""tests for RoundtableStreamHub — pub/sub primitive."""
import asyncio
import pytest

from paperreadagent.modules.ideator.stream_hub import (
    RoundtableStreamHub,
    get_stream_hub,
)


@pytest.mark.asyncio
async def test_publish_subscribe_delivers_event():
    hub = RoundtableStreamHub()
    received = []

    async def consumer():
        sub = hub.subscribe(1)
        async for evt in sub:
            received.append(evt)
            break
        sub.close()

    consumer_task = asyncio.create_task(consumer())
    await asyncio.sleep(0.01)
    await hub.publish(1, {"type": "delta", "seat_id": "rev1", "delta": "X"})
    await asyncio.wait_for(consumer_task, timeout=1.0)
    assert received == [{"type": "delta", "seat_id": "rev1", "delta": "X"}]


@pytest.mark.asyncio
async def test_publish_fans_out_to_all_subscribers():
    hub = RoundtableStreamHub()
    received = [[], [], []]

    async def consume(idx):
        sub = hub.subscribe(42)
        async for evt in sub:
            received[idx].append(evt)
            break
        sub.close()

    tasks = [asyncio.create_task(consume(i)) for i in range(3)]
    await asyncio.sleep(0.01)
    await hub.publish(42, {"type": "delta", "delta": "X"})
    await asyncio.gather(*tasks)
    assert all(r == [{"type": "delta", "delta": "X"}] for r in received)


@pytest.mark.asyncio
async def test_subscribers_isolated_by_rt_id():
    hub = RoundtableStreamHub()
    received_a, received_b = [], []

    async def consume_a():
        sub = hub.subscribe(1)
        async for evt in sub:
            received_a.append(evt)
            break
        sub.close()

    async def consume_b():
        sub = hub.subscribe(2)
        try:
            await asyncio.wait_for(sub.__anext__(), timeout=0.1)
            received_b.append("got_something")
        except asyncio.TimeoutError:
            pass
        sub.close()

    task_a = asyncio.create_task(consume_a())
    task_b = asyncio.create_task(consume_b())
    await asyncio.sleep(0.01)
    await hub.publish(1, {"x": 1})
    await task_a
    await task_b
    assert received_a == [{"x": 1}]
    assert received_b == []  # rt_id=2 saw nothing


@pytest.mark.asyncio
async def test_subscribe_auto_cleanup_on_disconnect():
    hub = RoundtableStreamHub()

    async def consume():
        sub = hub.subscribe(7)
        try:
            async for _evt in sub:
                return  # exit generator after first event
        finally:
            sub.close()

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    assert hub.subscriber_count(7) == 1
    await hub.publish(7, {"x": 1})
    await task
    # explicit close() in finally should have run
    assert hub.subscriber_count(7) == 0


@pytest.mark.asyncio
async def test_publish_drops_on_full_queue():
    """Producer must never block — full subscriber queues drop overflow.
    Earlier events that fit in the queue survive."""
    hub = RoundtableStreamHub(queue_maxsize=2)

    sub = hub.subscribe(5)
    # Fill queue + 1 overflow without consuming
    await hub.publish(5, {"e": 1})
    await hub.publish(5, {"e": 2})
    await hub.publish(5, {"e": 3})  # dropped (queue full)

    # Producer never blocked, subscriber still registered
    assert hub.subscriber_count(5) == 1

    # The 2 fitting events should still be readable in order
    e1 = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    e2 = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert e1 == {"e": 1}
    assert e2 == {"e": 2}
    # No event 3 — was dropped
    sub.close()


@pytest.mark.asyncio
async def test_subscriber_close_after_cancel_cleans_up():
    """Realistic SSE-disconnect: consumer task is cancelled mid-await,
    caller's finally block calls close() for deterministic cleanup."""
    hub = RoundtableStreamHub()
    sub = hub.subscribe(11)
    assert hub.subscriber_count(11) == 1

    # Start a consumer that will block on empty queue
    consumer_task = asyncio.create_task(sub.__anext__())
    await asyncio.sleep(0.01)  # ensure task is awaiting q.get

    # Simulate browser-disconnect: cancel the consumer
    consumer_task.cancel()
    try:
        await consumer_task
    except asyncio.CancelledError:
        pass

    # Caller's finally calls close()
    sub.close()
    assert hub.subscriber_count(11) == 0

    # close() is idempotent
    sub.close()
    assert hub.subscriber_count(11) == 0


@pytest.mark.asyncio
async def test_close_rt_terminates_all_subscribers():
    hub = RoundtableStreamHub()
    exited = []

    async def consume(idx):
        sub = hub.subscribe(9)
        try:
            async for _evt in sub:
                pass  # consume until sentinel
        finally:
            sub.close()
        exited.append(idx)

    t1 = asyncio.create_task(consume(1))
    t2 = asyncio.create_task(consume(2))
    await asyncio.sleep(0.01)
    await hub.close_rt(9)
    await asyncio.wait_for(asyncio.gather(t1, t2), timeout=1.0)
    assert sorted(exited) == [1, 2]
    assert hub.subscriber_count(9) == 0


def test_get_stream_hub_returns_singleton():
    a = get_stream_hub()
    b = get_stream_hub()
    assert a is b
