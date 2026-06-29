"""tests for GET /api/roundtables/{rt_id}/stream — SSE endpoint."""
import asyncio
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from paperreadagent.modules.ideator.routes import router


def _make_app():
    """Minimal FastAPI app mounting the ideator router with a mock state.core."""
    app = FastAPI()
    app.state.core = MagicMock()
    app.include_router(router, prefix="/ideator")
    return app


def test_stream_route_returns_404_when_team_missing():
    """If RoundtableManager has no team for rt_id, the endpoint returns 404."""
    app = _make_app()
    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn:
        mock_mgr_fn.return_value.get_team.return_value = None
        with TestClient(app) as client:
            resp = client.get("/ideator/api/roundtables/9999/stream")
            assert resp.status_code == 404


def test_stream_route_yields_sse_format_events():
    """When hub.subscribe yields events, the route formats them as SSE."""
    app = _make_app()

    # 1) Manager returns a real team object (just truthy)
    fake_team = MagicMock()

    # 2) Fake hub.subscribe returns a Subscription-like async iterator
    # yielding 2 events then closing.
    class _FakeSub:
        def __init__(self):
            self._events = [
                {"type": "delta", "seat_id": "rev1", "delta": "A"},
                {"type": "end", "seat_id": "rev1", "raw": "A", "msg_id": 1},
            ]
            self._idx = 0
            self.closed = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self._idx >= len(self._events):
                raise StopAsyncIteration
            evt = self._events[self._idx]
            self._idx += 1
            return evt
        def close(self):
            self.closed = True

    fake_hub = MagicMock()
    fake_sub = _FakeSub()
    fake_hub.subscribe = MagicMock(return_value=fake_sub)

    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn, \
         patch("paperreadagent.modules.ideator.stream_hub.get_stream_hub", return_value=fake_hub):
        mock_mgr_fn.return_value.get_team.return_value = fake_team
        with TestClient(app) as client:
            with client.stream("GET", "/ideator/api/roundtables/42/stream") as resp:
                assert resp.status_code == 200
                # Read enough bytes to capture both events
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if body.count(b"\n\n") >= 3:  # connected + 2 events
                        break
                text = body.decode()
                assert "event: connected" in text
                assert "event: delta" in text
                assert "event: end" in text
                # SSE format: blank-line-terminated frames
                assert text.count("\n\n") >= 3


def test_stream_route_returns_500_when_manager_uninitialized():
    """If get_roundtable_manager() returns None (module init failed), 500."""
    app = _make_app()
    with patch("paperreadagent.modules.ideator.get_roundtable_manager", return_value=None):
        with TestClient(app) as client:
            resp = client.get("/ideator/api/roundtables/9999/stream")
            assert resp.status_code == 500
