"""
utils/rate_limiter.py
Per-host rate limiter for academic data sources (arXiv, DBLP, Unpaywall, S2).

Assembles the following codereuse templates:
  - token_bucket          (TokenBucket — async/sync token bucket)
  - atomic_json_write     (atomic_write_json + safe_read_json)
  - build_user_agent      (build_user_agent)
  - http_retry_sync       (fetch_sync_with_retry)
  - http_retry_async      (fetch_async_with_retry)

Adds project-specific layers:
  - HostLimiter           (TokenBucket + persistent cooldown)
  - RateLimiterRegistry   (per-host policy lookup by URL)
  - limited_fetch / limited_fetch_sync (convenience wrappers that auto-wire
    pre_request/on_429/on_success to a HostLimiter)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlparse

import aiohttp
import requests

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Section 1: TokenBucket — copied from codereuse/token_bucket
# ═══════════════════════════════════════════════════════════════════════

class TokenBucket:
    """Async/sync token bucket. See codereuse/token_bucket for full docs."""

    def __init__(self, rate: float, capacity: int):
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self.rate = float(rate)
        self.capacity = int(capacity)
        self._tokens: float = float(capacity)
        self._last_refill: float = time.monotonic()
        self._lock = threading.Lock()

    def _refill_locked(self, now: float) -> None:
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_refill = now

    def _try_acquire_locked(self, n: float, now: float) -> float:
        self._refill_locked(now)
        if self._tokens >= n:
            self._tokens -= n
            return 0.0
        return (n - self._tokens) / self.rate

    async def acquire(self, n: float = 1.0) -> None:
        if n > self.capacity:
            raise ValueError(f"requested {n} > capacity {self.capacity}")
        while True:
            with self._lock:
                wait = self._try_acquire_locked(n, time.monotonic())
                if wait <= 0:
                    return
            await asyncio.sleep(wait)

    def acquire_sync(self, n: float = 1.0) -> None:
        if n > self.capacity:
            raise ValueError(f"requested {n} > capacity {self.capacity}")
        while True:
            with self._lock:
                wait = self._try_acquire_locked(n, time.monotonic())
                if wait <= 0:
                    return
            time.sleep(wait)


# ═══════════════════════════════════════════════════════════════════════
# Section 2: atomic JSON I/O — copied from codereuse/atomic_json_write
# ═══════════════════════════════════════════════════════════════════════

def atomic_write_json(path: str | Path, data: Any, *,
                      indent: int | None = 2,
                      ensure_ascii: bool = False,
                      fsync: bool = True) -> bool:
    """Atomic JSON write. See codereuse/atomic_json_write for full docs."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("atomic_write_json: cannot create parent dir %s: %s",
                       path.parent, exc)
        return False

    tmp_fd: int | None = None
    tmp_path: str | None = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii, default=str)
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("atomic_write_json: failed for %s: %s", path, exc)
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        return False


def safe_read_json(path: str | Path, default: Any = None) -> Any:
    """Read JSON with default fallback. See codereuse/atomic_json_write."""
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("safe_read_json: %s unreadable, default: %s", path, exc)
        return default


# ═══════════════════════════════════════════════════════════════════════
# Section 3: build_user_agent — copied from codereuse/build_user_agent
# ═══════════════════════════════════════════════════════════════════════

_FAKE_EMAIL_DOMAINS = ("@example.com", "@example.org", "@example.net",
                       "@test.com", "@localhost")


def build_user_agent(contact_email: str = "", *,
                     name: str = "PaperReadAgent",
                     version: str = "1.0",
                     purpose: str = "academic research tool",
                     env_var: Optional[str] = "PAPERREAD_CONTACT_EMAIL") -> str:
    """Polite UA with optional mailto. See codereuse/build_user_agent."""
    email = (contact_email or "").strip()
    if not email and env_var:
        email = os.environ.get(env_var, "").strip()
    if email and _is_real_email(email):
        return f"{name}/{version} ({purpose}; mailto:{email})"
    return f"{name}/{version} ({purpose})"


def _is_real_email(email: str) -> bool:
    if "@" not in email:
        return False
    local, _, domain = email.partition("@")
    if not local or "." not in domain:
        return False
    lowered = email.lower()
    return not any(fake in lowered for fake in _FAKE_EMAIL_DOMAINS)


# ═══════════════════════════════════════════════════════════════════════
# Section 4: HTTP retry helpers — copied from codereuse/http_retry_*
# ═══════════════════════════════════════════════════════════════════════

PreRequestHook = Callable[[str], "Awaitable[None] | None"]
On429Hook = Callable[[str, Optional[float]], "Awaitable[None] | None"]
OnSuccessHook = Callable[[str], "Awaitable[None] | None"]
ContentValidator = Callable[[bytes], bool]


def fetch_sync_with_retry(
    session: requests.Session, url: str, *,
    method: str = "GET",
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    data: Optional[dict | bytes] = None,
    timeout: tuple[int, int] = (10, 30),
    max_retries: int = 3,
    base_backoff: float = 1.0,
    max_backoff: float = 60.0,
    pre_request: Optional[Callable[[str], None]] = None,
    on_429: Optional[Callable[[str, Optional[float]], None]] = None,
    on_success: Optional[Callable[[str], None]] = None,
    content_validator: Optional[ContentValidator] = None,
) -> tuple[Optional[bytes], int]:
    """Sync fetch with retry. See codereuse/http_retry_sync."""
    last_status = 0
    for attempt in range(1, max_retries + 1):
        if pre_request is not None:
            pre_request(url)
        try:
            resp = session.request(method, url, params=params, headers=headers,
                                   data=data, timeout=timeout)
        except (requests.RequestException, OSError) as exc:
            logger.warning("fetch_sync: %s attempt %d/%d net error: %s",
                           url[:80], attempt, max_retries, exc)
            if attempt < max_retries:
                _sync_backoff(base_backoff, max_backoff, attempt)
            continue

        last_status = resp.status_code
        if resp.status_code == 200:
            body = resp.content
            if content_validator is not None and not content_validator(body):
                logger.warning("fetch_sync: %s content invalid, no retry", url[:80])
                return None, resp.status_code
            if on_success is not None:
                on_success(url)
            return body, resp.status_code

        if resp.status_code in (429, 503):
            retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
            if on_429 is not None:
                on_429(url, retry_after)
            logger.warning("fetch_sync: %s %d attempt %d/%d (Retry-After=%s)",
                           url[:80], resp.status_code, attempt, max_retries,
                           retry_after)
            if attempt < max_retries:
                wait = retry_after if retry_after is not None else \
                    _compute_backoff(base_backoff, max_backoff, attempt)
                time.sleep(min(wait, max_backoff))
            continue

        if 400 <= resp.status_code < 500:
            logger.info("fetch_sync: %s permanent %d", url[:80], resp.status_code)
            return None, resp.status_code

        logger.warning("fetch_sync: %s %d attempt %d/%d",
                       url[:80], resp.status_code, attempt, max_retries)
        if attempt < max_retries:
            _sync_backoff(base_backoff, max_backoff, attempt)
    return None, last_status


async def fetch_async_with_retry(
    session: aiohttp.ClientSession, url: str, *,
    method: str = "GET",
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    data: Optional[dict | bytes] = None,
    timeout: float = 60.0,
    max_retries: int = 3,
    base_backoff: float = 1.0,
    max_backoff: float = 60.0,
    pre_request: Optional[PreRequestHook] = None,
    on_429: Optional[On429Hook] = None,
    on_success: Optional[OnSuccessHook] = None,
    content_validator: Optional[ContentValidator] = None,
) -> tuple[Optional[bytes], int]:
    """Async fetch with retry. See codereuse/http_retry_async."""
    last_status = 0
    timeout_obj = aiohttp.ClientTimeout(total=timeout)
    for attempt in range(1, max_retries + 1):
        if pre_request is not None:
            await _maybe_await(pre_request(url))
        try:
            async with session.request(method, url, params=params,
                                       headers=headers, data=data,
                                       timeout=timeout_obj) as resp:
                last_status = resp.status
                if resp.status == 200:
                    body = await resp.read()
                    if content_validator is not None and not content_validator(body):
                        logger.warning("fetch_async: %s content invalid", url[:80])
                        return None, resp.status
                    if on_success is not None:
                        await _maybe_await(on_success(url))
                    return body, resp.status
                if resp.status in (429, 503):
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    if on_429 is not None:
                        await _maybe_await(on_429(url, retry_after))
                    logger.warning("fetch_async: %s %d attempt %d/%d "
                                   "(Retry-After=%s)", url[:80], resp.status,
                                   attempt, max_retries, retry_after)
                    if attempt < max_retries:
                        wait = retry_after if retry_after is not None else \
                            _compute_backoff(base_backoff, max_backoff, attempt)
                        await asyncio.sleep(min(wait, max_backoff))
                    continue
                if 400 <= resp.status < 500:
                    logger.info("fetch_async: %s permanent %d",
                                url[:80], resp.status)
                    return None, resp.status
                logger.warning("fetch_async: %s %d attempt %d/%d",
                               url[:80], resp.status, attempt, max_retries)
                if attempt < max_retries:
                    await asyncio.sleep(_compute_backoff(base_backoff, max_backoff, attempt))
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("fetch_async: %s attempt %d/%d net error: %s",
                           url[:80], attempt, max_retries, exc)
            if attempt < max_retries:
                await asyncio.sleep(_compute_backoff(base_backoff, max_backoff, attempt))
    return None, last_status


async def _maybe_await(value):
    if asyncio.iscoroutine(value) or asyncio.isfuture(value):
        await value


def _compute_backoff(base: float, cap: float, attempt: int) -> float:
    return min(cap, base * (2 ** (attempt - 1)) + random.random() * base)


def _sync_backoff(base: float, cap: float, attempt: int) -> None:
    time.sleep(_compute_backoff(base, cap, attempt))


def _parse_retry_after(header: Optional[str]) -> Optional[float]:
    if not header:
        return None
    try:
        return float(header.strip())
    except (ValueError, TypeError):
        return None


# Module-level locks keyed by state-file path. Used by HostLimiter._save_state
# to serialise concurrent writes from different host instances within the same
# process. (Cross-process safety is NOT provided — single-pipeline use case.)
_STATE_FILE_LOCKS: dict[str, threading.Lock] = {}
_STATE_FILE_LOCKS_GUARD = threading.Lock()


def _get_state_file_lock(state_file: Path) -> threading.Lock:
    key = str(state_file.resolve())
    with _STATE_FILE_LOCKS_GUARD:
        lock = _STATE_FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _STATE_FILE_LOCKS[key] = lock
        return lock


# ═══════════════════════════════════════════════════════════════════════
# Section 5: Project-specific (HostLimiter, Registry, limited_fetch)
# Filled in by Tasks 2-4 below.
# ═══════════════════════════════════════════════════════════════════════


class HostLimiter:
    """Per-host rate limiter combining TokenBucket and persistent cooldown.

    The cooldown timestamp is shared via *state_file* (one record per host).
    Within a single process, concurrent writes to the same state_file are
    serialised by a module-level lock — so multiple HostLimiter instances
    on the same state_file coexist safely.

    Cross-process safety is NOT provided. The project's use case is a single
    long-running pipeline, so this trade-off keeps the implementation simple.
    """

    def __init__(self, host: str, *, rate: float, capacity: int,
                 default_cooldown: float, state_file: Path):
        self.host = host
        self.bucket = TokenBucket(rate, capacity)
        self.default_cooldown = float(default_cooldown)
        self.state_file = Path(state_file)
        self._cooldown_until: float = 0.0
        self._lock = threading.Lock()
        self._file_lock = _get_state_file_lock(self.state_file)
        self._load_state()

    def _load_state(self) -> None:
        """Load this host's cooldown timestamp from the shared state file."""
        all_state = safe_read_json(self.state_file, default={}) or {}
        if not isinstance(all_state, dict):
            all_state = {}
        record = all_state.get(self.host) or {}
        try:
            self._cooldown_until = float(record.get("cooldown_until", 0))
        except (TypeError, ValueError):
            self._cooldown_until = 0.0

    def _save_state(self) -> None:
        """Persist this host's cooldown record to the shared state file.

        Holds the file lock during read-modify-write so concurrent calls from
        different HostLimiter instances on the same file don't lose updates.
        """
        with self._file_lock:
            all_state = safe_read_json(self.state_file, default={}) or {}
            if not isinstance(all_state, dict):
                all_state = {}
            all_state[self.host] = {
                "cooldown_until": self._cooldown_until,
                "last_429": time.time(),
                "reason": "429",
            }
            atomic_write_json(self.state_file, all_state)

    def _wait_cooldown(self) -> float:
        """Return seconds until cooldown expires (0 if none active)."""
        with self._lock:
            return max(0.0, self._cooldown_until - time.time())

    async def acquire(self) -> None:
        """Async: wait out any cooldown, then acquire one token from the bucket."""
        wait = self._wait_cooldown()
        if wait > 0:
            logger.info("[Limiter] %s cooldown %.1fs, waiting...",
                        self.host, wait)
            await asyncio.sleep(wait)
        await self.bucket.acquire()

    def acquire_sync(self) -> None:
        """Sync: wait out any cooldown, then acquire one token from the bucket."""
        wait = self._wait_cooldown()
        if wait > 0:
            logger.info("[Limiter] %s cooldown %.1fs, waiting...",
                        self.host, wait)
            time.sleep(wait)
        self.bucket.acquire_sync()

    def report_429(self, retry_after: Optional[float] = None) -> None:
        """Record a 429/503 response — extend cooldown_until and persist.

        Multiple concurrent calls take the *max* of all proposed cooldowns —
        a longer existing cooldown is never shortened.
        """
        cooldown = retry_after if retry_after is not None else self.default_cooldown
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, time.time() + cooldown)
        self._save_state()

    def report_success(self) -> None:
        """No-op. Reserved for future "consecutive-success clears stale cooldown" logic."""
        pass


class RateLimiterRegistry:
    """Per-host limiter registry. Matches by exact host or subdomain suffix.

    Register hosts at construction time; lookups by URL fall through to a
    permissive default when no registered host matches.
    """

    def __init__(self, state_file: Path):
        self.state_file = Path(state_file)
        self._limiters: dict[str, HostLimiter] = {}
        # Permissive default for unregistered hosts: 10/s, 30s cooldown.
        self._permissive = HostLimiter(
            "*", rate=10.0, capacity=10,
            default_cooldown=30.0, state_file=self.state_file,
        )

    def register(self, host_suffix: str, *,
                 rate: float, capacity: int, cooldown: float) -> None:
        """Register a HostLimiter for *host_suffix* (matches exact + subdomains)."""
        self._limiters[host_suffix.lower()] = HostLimiter(
            host_suffix.lower(), rate=rate, capacity=capacity,
            default_cooldown=cooldown, state_file=self.state_file,
        )

    def get(self, url: str) -> HostLimiter:
        """Return the HostLimiter matching *url*'s host (or permissive default)."""
        try:
            netloc = urlparse(url).netloc.lower()
        except (ValueError, AttributeError):
            return self._permissive
        host = netloc.split(":")[0]   # strip port
        for suffix, limiter in self._limiters.items():
            if host == suffix or host.endswith("." + suffix):
                return limiter
        return self._permissive


# ─── Module-level singleton ─────────────────────────────────────────────
# State file lives at <project_root>/data/rate_limit_state.json
# parents[0]=utils, parents[1]=paperreadagent, parents[2]=project root
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_STATE_FILE = _PROJECT_ROOT / "data" / "rate_limit_state.json"

_REGISTRY = RateLimiterRegistry(_STATE_FILE)
_REGISTRY.register("arxiv.org",                rate=4.0,  capacity=4,  cooldown=60)
_REGISTRY.register("dblp.org",                 rate=0.4,  capacity=1,  cooldown=600)
_REGISTRY.register("api.unpaywall.org",        rate=10.0, capacity=10, cooldown=30)
_REGISTRY.register("api.semanticscholar.org",  rate=1.0,  capacity=1,  cooldown=60)
_REGISTRY.register("api.crossref.org",     rate=10.0, capacity=10, cooldown=60)
_REGISTRY.register("api.openreview.net",   rate=2.0,  capacity=2,  cooldown=120)
_REGISTRY.register("api2.openreview.net",  rate=2.0,  capacity=2,  cooldown=120)
_REGISTRY.register("api.core.ac.uk",       rate=10.0, capacity=10, cooldown=60)


def get_limiter(url: str) -> HostLimiter:
    """Return the HostLimiter for *url*'s host (or permissive default)."""
    return _REGISTRY.get(url)


# ═══════════════════════════════════════════════════════════════════════
# Section 6: limited_fetch wrappers — auto-wire HostLimiter to fetch helpers
# ═══════════════════════════════════════════════════════════════════════

async def limited_fetch(
    session: aiohttp.ClientSession, url: str, **kwargs,
) -> tuple[Optional[bytes], int]:
    """Async fetch with the per-host HostLimiter auto-wired.

    Forwards all kwargs to fetch_async_with_retry except pre_request/on_429/
    on_success — those are filled in from get_limiter(url). Pass other args
    (timeout, max_retries, content_validator, etc.) freely.
    """
    limiter = get_limiter(url)
    return await fetch_async_with_retry(
        session, url,
        pre_request=lambda u: limiter.acquire(),
        on_429=lambda u, ra: limiter.report_429(ra),
        on_success=lambda u: limiter.report_success(),
        **kwargs,
    )


def limited_fetch_sync(
    session: requests.Session, url: str, **kwargs,
) -> tuple[Optional[bytes], int]:
    """Sync version of limited_fetch."""
    limiter = get_limiter(url)
    return fetch_sync_with_retry(
        session, url,
        pre_request=lambda u: limiter.acquire_sync(),
        on_429=lambda u, ra: limiter.report_429(ra),
        on_success=lambda u: limiter.report_success(),
        **kwargs,
    )


# Public exports
__all__ = [
    "TokenBucket", "atomic_write_json", "safe_read_json", "build_user_agent",
    "fetch_sync_with_retry", "fetch_async_with_retry",
    "HostLimiter", "RateLimiterRegistry",
    "get_limiter", "limited_fetch", "limited_fetch_sync",
]
