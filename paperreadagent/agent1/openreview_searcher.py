"""
agent1/openreview_searcher.py
OpenReview API v2 searcher for robotics/embodied AI venues.

Scope: hardcoded whitelist of CoRL/ICLR/NeurIPS/ICML invitations across
2022-2025. Each query x each invitation -> one keyword search call.
Includes rejected papers with `[reject]` tag in venue field.

API: https://api2.openreview.net/notes/search
Rate limit: registered as 2 req/s in rate_limiter.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from agent1.arxiv_searcher import PaperMeta
from utils.rate_limiter import build_user_agent, limited_fetch_sync

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api2.openreview.net/notes/search"
_SESSION = requests.Session()
_UA_INITIALIZED = False


# Whitelist of invitations. Each is one venue x year.
ROBOTICS_INVITATIONS = [
    # CoRL - Conference on Robot Learning (THE robotics ML venue)
    "robot-learning.org/CoRL/2024/Conference",
    "robot-learning.org/CoRL/2023/Conference",
    "robot-learning.org/CoRL/2022/Conference",
    # ICLR - many embodied AI / world models / robotics policy papers
    "ICLR.cc/2025/Conference",
    "ICLR.cc/2024/Conference",
    "ICLR.cc/2023/Conference",
    # NeurIPS - robotics workshops + main conference
    "NeurIPS.cc/2024/Conference",
    "NeurIPS.cc/2023/Conference",
    # ICML
    "ICML.cc/2024/Conference",
    "ICML.cc/2023/Conference",
]

_REJECT_MARKERS = ("Reject", "Withdrawn", "Rejected")


def _ensure_ua() -> None:
    global _UA_INITIALIZED
    if not _UA_INITIALIZED:
        _SESSION.headers["User-Agent"] = build_user_agent()
        _UA_INITIALIZED = True


def _format_venue(invitation: str, decision: Optional[str]) -> str:
    """Build human-readable venue from invitation + decision."""
    parts = invitation.split("/")
    venue_name = parts[0].split(".")[0]   # "ICLR.cc" -> "ICLR"
    if venue_name.lower() == "robot-learning":
        venue_name = "CoRL"
    year_part = ""
    for p in parts:
        if p.isdigit() and len(p) == 4:
            year_part = p
            break
    venue = f"{venue_name} {year_part}".strip()

    if "Workshop" in invitation:
        wname = invitation.rsplit("/", 1)[-1]
        venue = f"{venue} Workshop/{wname}"

    if decision and any(marker in decision for marker in _REJECT_MARKERS):
        venue = f"{venue} [reject]"
    return venue


def _parse_note_to_paper(
    note: dict, invitation: str, decision: Optional[str],
) -> PaperMeta:
    """Convert one OpenReview note JSON into a PaperMeta."""
    nid = note.get("id", "")
    content = note.get("content") or {}

    def _get_value(key: str, default=""):
        field = content.get(key) or {}
        if isinstance(field, dict):
            return field.get("value", default)
        return field if field else default

    title = str(_get_value("title", "")).strip()
    authors_raw = _get_value("authors", [])
    if not isinstance(authors_raw, list):
        authors_raw = []
    authors = [str(a) for a in authors_raw]
    abstract = str(_get_value("abstract", "")).strip()

    cdate = note.get("cdate", 0)
    if cdate:
        try:
            published = datetime.fromtimestamp(cdate / 1000, timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError, OverflowError):
            published = "unknown"
    else:
        published = "unknown"

    venue = _format_venue(invitation, decision)
    pdf_url = f"https://openreview.net/pdf?id={nid}" if nid else ""
    forum_url = f"https://openreview.net/forum?id={nid}" if nid else ""

    return PaperMeta(
        arxiv_id=f"or_{nid}",
        title=title,
        authors=authors,
        published=published,
        abstract=abstract,
        pdf_url=pdf_url,
        arxiv_url=forum_url,
        doi="",
        relevance_score=0.0,
        source_platform="openreview",
        venue=venue,
        code_url="",
        citation_count=0,
    )


def _run_query_for_invitation(query: str, invitation: str) -> list[PaperMeta]:
    """Execute one (query, invitation) pair against the OpenReview API."""
    _ensure_ua()
    params = {
        "term": query,
        "content": "all",
        "group": "all",
        "source": "all",
        "limit": "50",
    }
    body, status = limited_fetch_sync(
        _SESSION, _SEARCH_URL, params=params,
        timeout=(10, 30), max_retries=2,
    )
    if body is None:
        logger.warning("[OpenReview] %s x %s — no response (status=%d)",
                       query[:40], invitation, status)
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[OpenReview] non-JSON: %s", exc)
        return []
    notes = data.get("notes") or []

    papers: list[PaperMeta] = []
    for note in notes:
        invitations = note.get("invitations") or []
        if isinstance(invitations, str):
            invitations = [invitations]
        # Filter by invitation match (exact or substring containment)
        if invitation not in invitations and not any(
            invitation in inv for inv in invitations if isinstance(inv, str)
        ):
            continue

        v_field = (note.get("content") or {}).get("venue") or {}
        if isinstance(v_field, dict):
            v_str = v_field.get("value", "")
        else:
            v_str = str(v_field)
        if any(m in v_str for m in _REJECT_MARKERS):
            decision = "Reject"
        else:
            decision = "Accept"

        papers.append(_parse_note_to_paper(note, invitation, decision))
    return papers


def search_openreview(
    queries: list[str],
    max_results_per_query: int = 50,
    min_year: int = 0,
    query_delay: float = 0.0,    # IGNORED — limiter handles pacing
    max_queries: int = 8,
) -> list[PaperMeta]:
    """Run multi-query OpenReview search across whitelisted invitations.

    Returns deduped list of PaperMeta. The query_delay parameter is kept for
    backward-compat with main.py callsites but ignored — pacing is enforced by
    the shared HostLimiter (2 req/s).
    """
    if not queries:
        return []

    active_queries = queries[:max_queries]
    seen_ids: set[str] = set()
    all_papers: list[PaperMeta] = []

    logger.info("[OpenReview] %d queries x %d invitations",
                len(active_queries), len(ROBOTICS_INVITATIONS))

    for query in active_queries:
        for invitation in ROBOTICS_INVITATIONS:
            try:
                papers = _run_query_for_invitation(query, invitation)
            except Exception as exc:
                logger.warning("[OpenReview] failure on %s x %s: %s",
                               query[:40], invitation, exc)
                continue

            for p in papers:
                if min_year > 0:
                    try:
                        year = int(p.published[:4])
                    except (ValueError, IndexError):
                        year = 0
                    if year > 0 and year < min_year:
                        continue

                key = p.arxiv_id
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                all_papers.append(p)

    logger.info("[OpenReview] done; %d unique papers", len(all_papers))
    return all_papers
