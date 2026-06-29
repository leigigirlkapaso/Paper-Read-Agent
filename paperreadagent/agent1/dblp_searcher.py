"""
agent1/dblp_searcher.py
DBLP CS literature search — uses paperreadagent.utils.rate_limiter for polite,
persistent rate limiting.

Coverage: HCI/CS top conferences/journals not indexed on arXiv (CHI, UIST,
CSCW, IMWUT, etc.). API doc: https://dblp.org/faq/How+to+use+the+dblp+search+API.html
"""
from __future__ import annotations

import json
import logging
import re

import requests

from agent1.arxiv_searcher import PaperMeta
from utils.rate_limiter import build_user_agent, limited_fetch_sync

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://dblp.org/search/publ/api"
_SESSION = requests.Session()
_UA_INITIALIZED = False


def _ensure_ua() -> None:
    """Lazily set UA on first use — env var may not be set at import time."""
    global _UA_INITIALIZED
    if not _UA_INITIALIZED:
        _SESSION.headers["User-Agent"] = build_user_agent()
        _UA_INITIALIZED = True
        logger.info("[DBLP] UA: %s", _SESSION.headers["User-Agent"])


def search_dblp(
    queries: list[str],
    max_results_per_query: int = 100,
    min_year: int = 0,
    query_delay: float = 0.0,    # IGNORED — limiter handles pacing
    max_queries: int = 8,
) -> list[PaperMeta]:
    """Run multi-query DBLP search; return deduped PaperMeta list.

    The query_delay parameter is kept for backward compatibility with
    main.py callsites but is now ignored — pacing is enforced by the
    shared HostLimiter for dblp.org (0.4 req/s + 600s cooldown on 429).
    """
    _ensure_ua()
    limit = min(max_results_per_query, 1000)
    seen_ids: set[str] = set()
    papers: list[PaperMeta] = []
    active_queries = queries[:max_queries]
    logger.info("[DBLP] %d queries, limit %d each", len(active_queries), limit)

    for query in active_queries:
        clean = _strip_arxiv_syntax(query)
        if not clean:
            continue
        n_added = _run_query(clean, limit, min_year, seen_ids, papers)
        if n_added == 0 and len(clean.split()) > 2:
            # Downgrade fallback: try the first 2 words.
            short = " ".join(clean.split()[:2])
            logger.info("[DBLP] downgrade %r → %r", clean, short)
            n_added = _run_query(short, limit, min_year, seen_ids, papers)
        mark = "[+]" if n_added > 0 else "[ ]"
        logger.info("[DBLP] %s %3d papers | %s", mark, n_added, clean[:80])

    logger.info("[DBLP] done; %d unique papers", len(papers))
    return papers


def _run_query(
    query: str, limit: int, min_year: int,
    seen_ids: set[str], papers: list[PaperMeta],
) -> int:
    params = {"q": query, "format": "json", "h": min(limit, 1000)}
    body, status = limited_fetch_sync(
        _SESSION, _SEARCH_URL,
        params=params, timeout=(10, 15), max_retries=3,
    )
    if body is None:
        logger.warning("[DBLP] query failed (status=%d): %s", status, query[:60])
        return 0

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        logger.warning("[DBLP] non-JSON response: %s", exc)
        return 0

    hits = (data.get("result") or {}).get("hits") or {}
    hit_list = hits.get("hit") or []

    n_added = 0
    for item in hit_list:
        info = item.get("info") or {}
        title = (info.get("title") or "").replace("\n", " ").strip()
        if not title:
            continue
        year_str = info.get("year", "")
        try:
            year = int(year_str) if year_str else 0
        except (ValueError, TypeError):
            year = 0
        if min_year > 0 and year > 0 and year < min_year:
            continue

        doi = (info.get("doi") or "").strip()
        dblp_key = (item.get("@id") or "").strip()
        if doi:
            clean_id = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
            internal_key = f"doi_{clean_id}"
        elif dblp_key:
            clean_id = f"dblp_{dblp_key.replace('/', '_')}"
            internal_key = f"dblp_{dblp_key}"
        else:
            continue

        if internal_key in seen_ids:
            continue
        seen_ids.add(internal_key)

        authors_data = info.get("authors") or {}
        author_list = authors_data.get("author", [])
        if isinstance(author_list, dict):
            author_list = [author_list]
        authors = [a.get("text", "") for a in author_list if a.get("text")]
        published = f"{year}-01-01" if year > 0 else "unknown"
        venue = (info.get("venue") or "").strip()
        pub_type = (info.get("type") or "").strip()
        abstract = f"[DBLP 元数据] {title}"
        if venue:
            abstract += f" — Published in {venue}"
        if year > 0:
            abstract += f" ({year})"
        if pub_type:
            abstract += f" [{pub_type}]"
        paper_url = info.get("url", "") or f"https://dblp.org/rec/{dblp_key}"
        pdf_url = f"https://doi.org/{doi}" if doi else ""

        papers.append(PaperMeta(
            arxiv_id=clean_id, title=title, authors=authors,
            published=published, abstract=abstract,
            pdf_url=pdf_url, arxiv_url=paper_url, doi=doi, venue=venue,
        ))
        n_added += 1

    return n_added


def _strip_arxiv_syntax(query: str) -> str:
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    q = q.replace('"', '')
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    q = ' '.join(q.split())
    return q
