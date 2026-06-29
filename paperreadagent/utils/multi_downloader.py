"""
utils/multi_downloader.py
Multi-source PDF downloader with cascade fallback:
  1. arXiv PDF URL    — when arxiv_id present
  2. Direct URL        — open access link from search result
  3. Unpaywall API    — DOI → OA PDF
  4. Semantic Scholar — DOI → openAccessPdf
  5. Sci-Hub mirrors  — last-resort, default off

All HTTP calls go through paperreadagent.utils.rate_limiter, so per-host
rate limits and persistent cooldowns are enforced uniformly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from utils.arxiv_downloader import _is_arxiv_id
from utils.rate_limiter import build_user_agent, limited_fetch

logger = logging.getLogger(__name__)

UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}"
S2_GRAPH_API = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"

DEFAULT_SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
]


def download_papers_batch_multi(
    papers: list,
    output_dir: str | Path,
    unpaywall_email: str = "",
    enable_scihub: bool = False,
    scihub_mirrors: list[str] | None = None,
    max_concurrent: int = 4,
    contact_email: str = "",
) -> dict[str, Path | None]:
    """Sync entry point — runs the async batch in a fresh loop.

    Args:
        papers:            PaperMeta list (needs arxiv_id / doi / pdf_url).
        output_dir:        Where PDFs land.
        unpaywall_email:   Email for Unpaywall API; empty disables Unpaywall.
        enable_scihub:     Enable Sci-Hub fallback (last resort).
        scihub_mirrors:    Override default mirror list.
        max_concurrent:    PDF download semaphore size; 4 is the safe arXiv cap.
        contact_email:     Used in HTTP UA. Should be the project's contact_email.

    Returns:
        {arxiv_id: Path or None}
    """
    output_dir = Path(output_dir)
    return asyncio.run(_download_batch_async(
        papers=papers,
        output_dir=output_dir,
        unpaywall_email=unpaywall_email,
        enable_scihub=enable_scihub,
        scihub_mirrors=scihub_mirrors,
        max_concurrent=max_concurrent,
        contact_email=contact_email,
    ))


async def _download_batch_async(
    papers, output_dir: Path, unpaywall_email: str,
    enable_scihub: bool, scihub_mirrors: list[str] | None,
    max_concurrent: int, contact_email: str,
) -> dict[str, Path | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max_concurrent)

    # limit_per_host=4 is a SECOND-LINE defence: even if max_concurrent is
    # mis-set, the TCP layer will not open >4 connections to arxiv.org.
    connector = aiohttp.TCPConnector(
        limit=20, limit_per_host=4, ttl_dns_cache=300,
    )
    timeout = aiohttp.ClientTimeout(total=90, connect=15)
    headers = {"User-Agent": build_user_agent(contact_email)}

    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, headers=headers,
    ) as session:
        tasks = [
            _download_one_async(
                paper=p, output_dir=output_dir, session=session, sem=sem,
                unpaywall_email=unpaywall_email,
                enable_scihub=enable_scihub, scihub_mirrors=scihub_mirrors,
            ) for p in papers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, Path | None] = {}
    for paper, result in zip(papers, results):
        aid = getattr(paper, "arxiv_id", str(id(paper)))
        if isinstance(result, Exception):
            logger.error("[MultiDL] exception (%s): %s", aid, result)
            out[aid] = None
        else:
            out[aid] = result
    return out


async def _download_one_async(
    paper, output_dir: Path, session: aiohttp.ClientSession,
    sem: asyncio.Semaphore, unpaywall_email: str,
    enable_scihub: bool, scihub_mirrors: list[str] | None,
) -> Path | None:
    aid = getattr(paper, "arxiv_id", "")
    clean_id = aid.strip().split("v")[0] if aid else ""
    filename = f"{clean_id.replace('/', '_')}.pdf" if clean_id else "_unknown.pdf"
    dest = output_dir / filename

    if dest.exists() and dest.stat().st_size > 1024:
        return dest

    doi = getattr(paper, "doi", "") or ""
    direct_url = getattr(paper, "pdf_url", "") or ""
    cascade = _build_cascade(clean_id, direct_url, doi, unpaywall_email,
                             enable_scihub, scihub_mirrors)

    async def _try_url(url: str, source: str) -> Path | None:
        async with sem:
            body, status = await limited_fetch(
                session, url, timeout=60, max_retries=3,
                content_validator=lambda b: b.startswith(b"%PDF"),
            )
            if body is None:
                return None
            try:
                with open(dest, "wb") as f:
                    f.write(body)
            except OSError as exc:
                logger.warning("[MultiDL] write fail (%s): %s", filename, exc)
                return None
            if dest.stat().st_size < 1024:
                dest.unlink(missing_ok=True)
                return None
            logger.info("[MultiDL] success (%s): %s (%d KB)",
                        source, filename, dest.stat().st_size // 1024)
            return dest

    async def _resolve_dynamic() -> dict[str, str]:
        if not doi:
            return {}
        named = []
        if unpaywall_email:
            named.append(("unpaywall",
                          _resolve_unpaywall(doi, unpaywall_email, session)))
        named.append(("s2_oa", _resolve_s2_oa(doi, session)))
        results = await asyncio.gather(
            *[c for _, c in named], return_exceptions=True,
        )
        return {name: r for (name, _), r in zip(named, results)
                if isinstance(r, str) and r}

    first_url = next((u for _, u in cascade if u), "")
    download_task = (asyncio.create_task(_try_url(first_url, cascade[0][0]))
                     if first_url else None)
    resolve_task = asyncio.create_task(_resolve_dynamic())

    if download_task is not None:
        result = await download_task
        if result is not None:
            resolve_task.cancel()
            try:
                await resolve_task
            except asyncio.CancelledError:
                pass
            return result

    resolved = await resolve_task

    for source, url in cascade:
        actual = resolved.get(source, url)
        if not actual or actual == first_url:
            continue
        result = await _try_url(actual, source)
        if result is not None:
            return result

    logger.warning("[MultiDL] all sources failed: %s", clean_id)
    if dest.exists():
        dest.unlink(missing_ok=True)
    return None


def _build_cascade(
    clean_id: str, direct_url: str, doi: str,
    unpaywall_email: str, enable_scihub: bool,
    scihub_mirrors: list[str] | None,
) -> list[tuple[str, str]]:
    """Build (source, url) cascade with URL-level dedup."""
    cascade: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    def _add(source: str, url: str) -> None:
        if url:
            if url in seen_urls:
                return
            seen_urls.add(url)
            cascade.append((source, url))
        else:
            # Placeholder for dynamically-resolved URLs (Unpaywall, S2).
            cascade.append((source, ""))

    if _is_arxiv_id(clean_id):
        _add("arxiv", f"https://arxiv.org/pdf/{clean_id}")
    if direct_url:
        _add("direct", direct_url)
    if doi and unpaywall_email:
        _add("unpaywall", "")
    if doi:
        _add("s2_oa", "")
    if doi and enable_scihub:
        for mirror in (scihub_mirrors or DEFAULT_SCIHUB_MIRRORS)[:3]:
            _add("scihub", f"{mirror.rstrip('/')}/{doi}")
    return cascade


async def _resolve_unpaywall(doi: str, email: str,
                              session: aiohttp.ClientSession) -> str | None:
    url = UNPAYWALL_API.format(doi=doi) + f"?email={email}"
    body, _status = await limited_fetch(session, url, timeout=15, max_retries=2)
    if body is None:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    pdf_url = (data.get("best_oa_location") or {}).get("url_for_pdf") or ""
    if pdf_url:
        logger.info("[MultiDL] Unpaywall → %s", pdf_url[:80])
    return pdf_url or None


async def _resolve_s2_oa(doi: str,
                         session: aiohttp.ClientSession) -> str | None:
    url = S2_GRAPH_API.format(doi=doi)
    params = {"fields": "openAccessPdf,externalIds"}
    body, _status = await limited_fetch(session, url, params=params,
                                        timeout=15, max_retries=2)
    if body is None:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    pdf_url = (data.get("openAccessPdf") or {}).get("url") or ""
    if pdf_url:
        logger.info("[MultiDL] S2 OA → %s", pdf_url[:80])
    return pdf_url or None
