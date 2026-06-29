"""
utils/arxiv_downloader.py
arXiv PDF downloader — sync + async paths, both routed through the shared
rate_limiter so they cooperate with the global per-host budgets.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

import aiohttp
import requests

from utils.rate_limiter import (
    build_user_agent, limited_fetch, limited_fetch_sync,
)

logger = logging.getLogger(__name__)

_PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{arxiv_id}"
_SYNC_SESSION = requests.Session()
_UA_INITIALIZED = False


def _ensure_ua() -> None:
    global _UA_INITIALIZED
    if not _UA_INITIALIZED:
        _SYNC_SESSION.headers["User-Agent"] = build_user_agent()
        _UA_INITIALIZED = True


# ──────────────────────────────────────────────────────────────────
# Sync (legacy)
# ──────────────────────────────────────────────────────────────────

def download_paper(
    arxiv_id: str, output_dir: str | Path,
    retries: int = 3, timeout: int = 60, direct_url: str = "",
) -> Path | None:
    """Sync single-paper PDF download."""
    _ensure_ua()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    clean_id = arxiv_id.strip().split("v")[0]
    filename = f"{clean_id.replace('/', '_')}.pdf"
    dest = output_dir / filename
    if dest.exists() and dest.stat().st_size > 1024:
        return dest

    if direct_url:
        url = direct_url
    elif _is_arxiv_id(clean_id):
        url = _PDF_URL_TEMPLATE.format(arxiv_id=clean_id)
    else:
        logger.warning("[Downloader] no URL: %s", clean_id)
        return None

    body, status = limited_fetch_sync(
        _SYNC_SESSION, url,
        timeout=(10, timeout), max_retries=retries,
        content_validator=lambda b: b.startswith(b"%PDF"),
    )
    if body is None:
        logger.warning("[Downloader] failed status=%d: %s", status, clean_id)
        return None
    try:
        dest.write_bytes(body)
    except OSError as exc:
        logger.warning("[Downloader] write fail (%s): %s", filename, exc)
        return None
    if dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        return None
    logger.info("[Downloader] done: %s (%d KB)", filename, dest.stat().st_size // 1024)
    return dest


def download_papers_batch(
    arxiv_ids: list[str], output_dir: str | Path,
    delay: float = 0.0,        # IGNORED — limiter handles pacing
    pdf_urls: dict[str, str] | None = None,
) -> dict[str, Path | None]:
    """Sync batch — serial, paced by the dblp.org/arxiv.org limiter."""
    results: dict[str, Path | None] = {}
    url_map = pdf_urls or {}
    for arxiv_id in arxiv_ids:
        results[arxiv_id] = download_paper(
            arxiv_id, output_dir,
            direct_url=url_map.get(arxiv_id, ""),
        )
    return results


# ──────────────────────────────────────────────────────────────────
# Async (Phase 4.1)
# ──────────────────────────────────────────────────────────────────

async def download_paper_async(
    arxiv_id: str, output_dir: Path,
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    retries: int = 3, timeout: int = 60, direct_url: str = "",
) -> Path | None:
    """Async single-paper PDF download."""
    clean_id = arxiv_id.strip().split("v")[0]
    filename = f"{clean_id.replace('/', '_')}.pdf"
    dest = output_dir / filename
    if dest.exists() and dest.stat().st_size > 1024:
        return dest

    if direct_url:
        url = direct_url
    elif _is_arxiv_id(clean_id):
        url = _PDF_URL_TEMPLATE.format(arxiv_id=clean_id)
    else:
        return None

    async with semaphore:
        body, status = await limited_fetch(
            session, url, timeout=timeout, max_retries=retries,
            content_validator=lambda b: b.startswith(b"%PDF"),
        )
    if body is None:
        return None
    try:
        dest.write_bytes(body)
    except OSError:
        return None
    if dest.stat().st_size < 1024:
        dest.unlink(missing_ok=True)
        return None
    logger.info("[AsyncDL] done: %s (%d KB)", filename, dest.stat().st_size // 1024)
    return dest


async def download_papers_batch_async(
    arxiv_ids: list[str], output_dir: Path,
    max_concurrent: int = 4, delay_between: float = 0.0,
    pdf_urls: dict[str, str] | None = None,
) -> dict[str, Path | None]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    url_map = pdf_urls or {}
    sem = asyncio.Semaphore(max_concurrent)

    connector = aiohttp.TCPConnector(limit=20, limit_per_host=4)
    headers = {"User-Agent": build_user_agent()}
    async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
        tasks = [
            download_paper_async(
                arxiv_id=aid, output_dir=output_dir,
                session=session, semaphore=sem,
                direct_url=url_map.get(aid, ""),
            ) for aid in arxiv_ids
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, Path | None] = {}
    for aid, result in zip(arxiv_ids, results_list):
        out[aid] = None if isinstance(result, Exception) else result
    return out


def _is_arxiv_id(id_str: str) -> bool:
    if id_str.startswith(("s2_", "oa_", "pwc_")):
        return False
    return bool(re.match(r'^[\d]{4}\.\d{4,5}$', id_str) or
                re.match(r'^[a-z\-]+/\d{7}$', id_str))
