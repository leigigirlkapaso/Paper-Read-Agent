"""
agent1/crossref_searcher.py
CrossRef REST API searcher for DOI-indexed academic papers.

Scope: any paper with a registered DOI — covers IEEE Xplore (IROS, ICRA,
T-RO), Sage (IJRR), Springer, Elsevier, Wiley, ACM, etc. CrossRef itself
sometimes returns abstracts (~30-80% coverage depending on publisher);
when missing, abstract_resolver.py runs the cascade.

API: https://api.crossref.org/works
Rate limit: 10 req/s in rate_limiter (CrossRef polite pool tolerates 50+).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

import requests

from agent1.arxiv_searcher import PaperMeta
from utils.rate_limiter import build_user_agent, limited_fetch_sync

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.crossref.org/works"
_SESSION = requests.Session()
_UA_INITIALIZED = False

# Only keep these CrossRef item types (filter out datasets, peer reviews, etc.)
_ALLOWED_TYPES = frozenset([
    "journal-article",
    "proceedings-article",
    "book-chapter",
])

# Fields to request from CrossRef (saves bandwidth)
_SELECT_FIELDS = ",".join([
    "DOI", "title", "author", "abstract",
    "issued", "container-title", "URL", "type",
    "is-referenced-by-count",
])

_JATS_PATTERN = re.compile(r"</?jats:[^>]+>")


def _ensure_ua() -> None:
    global _UA_INITIALIZED
    if not _UA_INITIALIZED:
        _SESSION.headers["User-Agent"] = build_user_agent()
        _UA_INITIALIZED = True


def _strip_jats(s: Optional[str]) -> str:
    """Strip JATS XML tags from CrossRef abstract field; collapse whitespace."""
    if not s:
        return ""
    out = _JATS_PATTERN.sub("", s)
    out = " ".join(out.split())
    return out.strip()


def _parse_item_to_paper(item: dict) -> Optional[PaperMeta]:
    """Convert one CrossRef item to a PaperMeta. Returns None if unparseable."""
    doi = (item.get("DOI") or "").strip()
    titles = item.get("title") or []
    title = titles[0].strip() if titles else ""
    if not title:
        return None  # Unparseable

    authors_raw = item.get("author") or []
    authors = []
    for a in authors_raw:
        if isinstance(a, dict):
            given = (a.get("given") or "").strip()
            family = (a.get("family") or "").strip()
            full = f"{given} {family}".strip()
            if full:
                authors.append(full)

    # year from issued.date-parts
    year = 0
    issued = item.get("issued") or {}
    date_parts = issued.get("date-parts") or [[None]]
    if date_parts and date_parts[0]:
        try:
            year = int(date_parts[0][0]) if date_parts[0][0] else 0
        except (ValueError, TypeError):
            year = 0
    published = f"{year}-01-01" if year > 0 else "unknown"

    abstract = _strip_jats(item.get("abstract", ""))
    container = item.get("container-title") or []
    venue = container[0] if container else ""

    return PaperMeta(
        arxiv_id="",   # No arxiv mapping; DOI is primary key
        title=title,
        authors=authors,
        published=published,
        abstract=abstract,
        pdf_url="",   # CrossRef doesn't directly serve PDFs
        arxiv_url=item.get("URL", ""),
        doi=doi,
        relevance_score=0.0,
        source_platform="crossref",
        venue=str(venue) if venue else "",
        code_url="",
        citation_count=int(item.get("is-referenced-by-count", 0) or 0),
    )


def _run_query(
    query: str, max_results: int, min_year: int,
) -> list[PaperMeta]:
    """Execute one CrossRef query."""
    _ensure_ua()
    params = {
        "query": query,
        "rows": str(min(max_results, 1000)),
        "select": _SELECT_FIELDS,
    }
    if min_year > 0:
        params["filter"] = f"from-pub-date:{min_year}"

    body, status = limited_fetch_sync(
        _SESSION, _SEARCH_URL, params=params,
        timeout=(10, 30), max_retries=3,
    )
    if body is None:
        logger.warning("[CrossRef] query failed (status=%d): %s", status, query[:60])
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[CrossRef] non-JSON response: %s", exc)
        return []

    items = (data.get("message") or {}).get("items") or []
    papers: list[PaperMeta] = []

    for item in items:
        if item.get("type") not in _ALLOWED_TYPES:
            continue
        p = _parse_item_to_paper(item)
        if p is None:
            continue
        if min_year > 0:
            try:
                year = int(p.published[:4])
            except (ValueError, IndexError):
                year = 0
            if year > 0 and year < min_year:
                continue
        papers.append(p)
    return papers


def search_crossref(
    queries: list[str],
    max_results_per_query: int = 100,
    min_year: int = 0,
    query_delay: float = 0.0,    # IGNORED — limiter handles pacing
    max_queries: int = 8,
) -> list[PaperMeta]:
    """Run multi-query CrossRef search; return deduped PaperMeta list.

    The query_delay parameter is kept for backward-compat with main.py
    callsites but is ignored — pacing is enforced by the shared HostLimiter
    (10 req/s).
    """
    if not queries:
        return []

    active_queries = queries[:max_queries]
    seen_dois: set[str] = set()
    all_papers: list[PaperMeta] = []

    logger.info("[CrossRef] %d queries, %d max results each",
                len(active_queries), max_results_per_query)

    for query in active_queries:
        try:
            papers = _run_query(query, max_results_per_query, min_year)
        except Exception as exc:
            logger.warning("[CrossRef] error on %s: %s", query[:60], exc)
            continue

        n_added = 0
        for p in papers:
            if not p.doi or p.doi in seen_dois:
                continue
            seen_dois.add(p.doi)
            all_papers.append(p)
            n_added += 1
        logger.info("[CrossRef] %s %3d new | %s",
                    "[+]" if n_added > 0 else "[ ]",
                    n_added, query[:80])

    logger.info("[CrossRef] done; %d unique papers", len(all_papers))
    return all_papers
