"""Unit tests for paperreadagent.utils.rate_limiter project-specific layer."""
import asyncio
import json
import time
from pathlib import Path

import pytest

from utils.rate_limiter import HostLimiter


class TestHostLimiterCooldown:
    def test_no_cooldown_initially(self, tmp_path):
        state = tmp_path / "state.json"
        h = HostLimiter("test.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)
        # acquire_sync should not block when no cooldown
        t0 = time.monotonic()
        h.acquire_sync()
        assert time.monotonic() - t0 < 0.1

    def test_report_429_persists_cooldown(self, tmp_path):
        state = tmp_path / "state.json"
        h = HostLimiter("test.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)
        h.report_429(retry_after=30)
        # File written
        assert state.exists()
        data = json.loads(state.read_text(encoding="utf-8"))
        assert "test.local" in data
        assert data["test.local"]["cooldown_until"] > time.time() + 25

    def test_cooldown_loaded_on_init(self, tmp_path):
        state = tmp_path / "state.json"
        # Pre-populate state file
        future = time.time() + 300
        state.write_text(json.dumps({
            "test.local": {"cooldown_until": future, "last_429": time.time(),
                           "reason": "429"}
        }))
        h = HostLimiter("test.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)
        # Should see future cooldown
        assert h._cooldown_until == pytest.approx(future, abs=1)

    def test_cooldown_blocks_acquire(self, tmp_path):
        state = tmp_path / "state.json"
        h = HostLimiter("test.local", rate=100, capacity=10,
                        default_cooldown=60, state_file=state)
        h.report_429(retry_after=0.5)
        t0 = time.monotonic()
        h.acquire_sync()
        elapsed = time.monotonic() - t0
        assert 0.4 < elapsed < 1.0, f"elapsed={elapsed}"

    def test_default_cooldown_when_no_retry_after(self, tmp_path):
        state = tmp_path / "state.json"
        h = HostLimiter("test.local", rate=10, capacity=2,
                        default_cooldown=120, state_file=state)
        h.report_429(retry_after=None)
        assert h._cooldown_until > time.time() + 110

    def test_corrupted_state_file_ignored(self, tmp_path):
        state = tmp_path / "state.json"
        state.write_text("{not json", encoding="utf-8")
        # Should not raise
        h = HostLimiter("test.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)
        assert h._cooldown_until == 0

    def test_multiple_hosts_share_state_file(self, tmp_path):
        state = tmp_path / "state.json"
        a = HostLimiter("a.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)
        b = HostLimiter("b.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)
        a.report_429(retry_after=10)
        b.report_429(retry_after=20)
        data = json.loads(state.read_text(encoding="utf-8"))
        assert "a.local" in data and "b.local" in data

    @pytest.mark.asyncio
    async def test_async_acquire_respects_cooldown(self, tmp_path):
        state = tmp_path / "state.json"
        h = HostLimiter("test.local", rate=100, capacity=10,
                        default_cooldown=60, state_file=state)
        h.report_429(retry_after=0.5)
        t0 = asyncio.get_event_loop().time()
        await h.acquire()
        elapsed = asyncio.get_event_loop().time() - t0
        assert 0.4 < elapsed < 1.0

    def test_concurrent_report_429_takes_max(self, tmp_path):
        """Multiple threads calling report_429 — final cooldown_until must be the max."""
        import threading as _t

        state = tmp_path / "state.json"
        h = HostLimiter("test.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)

        retries = [5.0, 30.0, 10.0, 60.0, 15.0, 45.0]  # max = 60
        threads = []
        for ra in retries:
            threads.append(_t.Thread(target=h.report_429, args=(ra,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Final in-memory state should reflect the max retry_after
        assert h._cooldown_until > time.time() + 55, \
            f"expected ~60s cooldown, got {h._cooldown_until - time.time():.1f}s"
        # JSON file record should match
        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["test.local"]["cooldown_until"] == h._cooldown_until

    def test_concurrent_multi_host_writes_no_loss(self, tmp_path):
        """Two threads, two hosts, concurrent report_429 — both records present."""
        import threading as _t

        state = tmp_path / "state.json"
        a = HostLimiter("a.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)
        b = HostLimiter("b.local", rate=10, capacity=2,
                        default_cooldown=60, state_file=state)

        # Run many concurrent rounds — race-prone path
        threads = []
        for _ in range(20):
            threads.append(_t.Thread(target=a.report_429, args=(10.0,)))
            threads.append(_t.Thread(target=b.report_429, args=(20.0,)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = json.loads(state.read_text(encoding="utf-8"))
        assert "a.local" in data, "a.local record was clobbered by concurrent writes"
        assert "b.local" in data, "b.local record was clobbered by concurrent writes"


from utils.rate_limiter import (
    RateLimiterRegistry, get_limiter,
)


class TestRateLimiterRegistry:
    def test_exact_host_match(self, tmp_path):
        state = tmp_path / "s.json"
        reg = RateLimiterRegistry(state_file=state)
        reg.register("arxiv.org", rate=4, capacity=4, cooldown=60)
        lim = reg.get("https://arxiv.org/pdf/2401.12345")
        assert lim.host == "arxiv.org"

    def test_subdomain_matches_suffix(self, tmp_path):
        state = tmp_path / "s.json"
        reg = RateLimiterRegistry(state_file=state)
        reg.register("arxiv.org", rate=4, capacity=4, cooldown=60)
        lim = reg.get("https://export.arxiv.org/api/query")
        assert lim.host == "arxiv.org"

    def test_unrelated_host_returns_permissive(self, tmp_path):
        state = tmp_path / "s.json"
        reg = RateLimiterRegistry(state_file=state)
        reg.register("arxiv.org", rate=4, capacity=4, cooldown=60)
        lim = reg.get("https://random.example.com/x")
        assert lim.host == "*"  # permissive default

    def test_url_with_port_stripped(self, tmp_path):
        state = tmp_path / "s.json"
        reg = RateLimiterRegistry(state_file=state)
        reg.register("dblp.org", rate=0.4, capacity=1, cooldown=600)
        lim = reg.get("https://dblp.org:443/search/publ/api")
        assert lim.host == "dblp.org"


class TestGlobalRegistry:
    def test_get_limiter_arxiv(self):
        lim = get_limiter("https://arxiv.org/pdf/abc")
        assert lim.host == "arxiv.org"
        assert lim.bucket.rate == 4.0

    def test_get_limiter_dblp(self):
        lim = get_limiter("https://dblp.org/search/publ/api")
        assert lim.host == "dblp.org"
        assert lim.bucket.rate == 0.4

    def test_get_limiter_unpaywall(self):
        lim = get_limiter("https://api.unpaywall.org/v2/10.1/foo")
        assert lim.host == "api.unpaywall.org"
        assert lim.bucket.rate == 10.0

    def test_get_limiter_s2(self):
        lim = get_limiter("https://api.semanticscholar.org/graph/v1/paper/x")
        assert lim.host == "api.semanticscholar.org"
        assert lim.bucket.rate == 1.0

    def test_get_limiter_crossref(self):
        lim = get_limiter("https://api.crossref.org/works?query=foo")
        assert lim.host == "api.crossref.org"
        assert lim.bucket.rate == 10.0

    def test_get_limiter_openreview_v1(self):
        lim = get_limiter("https://api.openreview.net/notes/search")
        assert lim.host == "api.openreview.net"
        assert lim.bucket.rate == 2.0

    def test_get_limiter_openreview_v2(self):
        lim = get_limiter("https://api2.openreview.net/notes/search")
        assert lim.host == "api2.openreview.net"
        assert lim.bucket.rate == 2.0

    def test_get_limiter_core(self):
        lim = get_limiter("https://api.core.ac.uk/v3/search/works")
        assert lim.host == "api.core.ac.uk"
        assert lim.bucket.rate == 10.0


from unittest.mock import MagicMock, patch

from utils.rate_limiter import (
    limited_fetch_sync,
)


class TestLimitedFetchSync:
    def test_invokes_limiter_acquire_before_request(self, tmp_path, monkeypatch):
        # Use a fresh registry pointing at a tmp state file
        state = tmp_path / "s.json"
        from utils import rate_limiter as rl
        monkeypatch.setattr(rl, "_REGISTRY",
                            rl.RateLimiterRegistry(state_file=state))
        rl._REGISTRY.register("test.local", rate=100, capacity=10, cooldown=60)

        session = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"ok"
        resp.headers = {}
        session.request.return_value = resp

        body, status = limited_fetch_sync(session, "https://test.local/x")
        assert body == b"ok"
        assert status == 200

    def test_429_triggers_cooldown_persistence(self, tmp_path, monkeypatch):
        state = tmp_path / "s.json"
        from utils import rate_limiter as rl
        monkeypatch.setattr(rl, "_REGISTRY",
                            rl.RateLimiterRegistry(state_file=state))
        rl._REGISTRY.register("test.local", rate=100, capacity=10, cooldown=10)

        session = MagicMock()
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "0.1"}
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.content = b"ok"
        resp_200.headers = {}
        session.request.side_effect = [resp_429, resp_200]

        with patch("utils.rate_limiter.time.sleep"):
            limited_fetch_sync(session, "https://test.local/x", max_retries=2,
                               base_backoff=0.01)

        # State file should now contain test.local cooldown record
        assert state.exists()
        data = json.loads(state.read_text(encoding="utf-8"))
        assert "test.local" in data
        assert data["test.local"]["cooldown_until"] > time.time()


class TestLimitedFetchAsync:
    @pytest.mark.asyncio
    async def test_async_invokes_limiter_acquire_before_request(self, tmp_path, monkeypatch):
        """Verify limited_fetch awaits limiter.acquire() before sending request."""
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        import aiohttp

        from utils import rate_limiter as rl
        from utils.rate_limiter import limited_fetch

        # Inject fresh registry pointed at tmp state file
        state = tmp_path / "s.json"
        monkeypatch.setattr(rl, "_REGISTRY",
                            rl.RateLimiterRegistry(state_file=state))
        rl._REGISTRY.register("127.0.0.1", rate=100, capacity=10, cooldown=60)

        # Track ordering: limiter.acquire must complete before HTTP request
        order: list[str] = []
        original_acquire = rl._REGISTRY.get("http://127.0.0.1/").acquire

        async def tracking_acquire(*args, **kwargs):
            order.append("acquire")
            return await original_acquire(*args, **kwargs)

        monkeypatch.setattr(rl._REGISTRY.get("http://127.0.0.1/"),
                            "acquire", tracking_acquire)

        async def handler(request):
            order.append("request")
            return web.Response(body=b"hello", status=200)

        app = web.Application()
        app.router.add_get("/data", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            url = str(server.make_url("/data"))
            async with aiohttp.ClientSession() as session:
                body, status = await limited_fetch(session, url)
            assert body == b"hello"
            assert status == 200
            assert order == ["acquire", "request"], f"got {order}"
        finally:
            await server.close()

    @pytest.mark.asyncio
    async def test_async_429_triggers_cooldown_persistence(self, tmp_path, monkeypatch):
        """Verify limited_fetch reports 429 to limiter (state file written)."""
        from aiohttp import web
        from aiohttp.test_utils import TestServer
        import aiohttp

        from utils import rate_limiter as rl
        from utils.rate_limiter import limited_fetch

        state = tmp_path / "s.json"
        monkeypatch.setattr(rl, "_REGISTRY",
                            rl.RateLimiterRegistry(state_file=state))
        # Large default_cooldown so cooldown_until stays well in the future
        # even after the retry's asyncio.sleep (which we patch out anyway).
        rl._REGISTRY.register("127.0.0.1", rate=100, capacity=10, cooldown=300)

        # Patch asyncio.sleep inside rate_limiter to a no-op so the retry's
        # cooldown wait doesn't actually consume 300s of wall time.
        async def _fast_sleep(_):
            return None
        monkeypatch.setattr(rl.asyncio, "sleep", _fast_sleep)

        # Server returns 429 once, then 200
        counts = {"n": 0}

        async def handler(request):
            counts["n"] += 1
            if counts["n"] == 1:
                return web.Response(status=429)
            return web.Response(body=b"ok", status=200)

        app = web.Application()
        app.router.add_get("/x", handler)
        server = TestServer(app)
        await server.start_server()
        try:
            url = str(server.make_url("/x"))
            async with aiohttp.ClientSession() as session:
                body, status = await limited_fetch(
                    session, url, max_retries=2, base_backoff=0.01, max_backoff=1,
                )
            assert body == b"ok"
            # State file should now contain 127.0.0.1 cooldown record
            assert state.exists()
            data = json.loads(state.read_text(encoding="utf-8"))
            assert "127.0.0.1" in data
            assert data["127.0.0.1"]["cooldown_until"] > time.time()
        finally:
            await server.close()
