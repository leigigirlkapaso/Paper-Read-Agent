"""
utils/abstract_resolver.py
Abstract resolver cascade for papers with missing abstracts.

When CrossRef returns metadata without an abstract (common for IEEE/Sage
journals), or when other searchers find a paper but no abstract was
populated, this resolver tries multiple sources in order:

  Tier 1: OpenAlex by DOI (free, ~85% coverage, 10 req/s)
  Tier 2: Semantic Scholar by DOI without API key (1 req/s, ~95% coverage)
  Tier 3: CORE API by DOI (with free key, fills the remaining ~3%)

Returns the first non-empty abstract found, or empty string if all fail.
Each tier is gated by rate_limiter automatically.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import aiohttp

from utils.rate_limiter import limited_fetch

logger = logging.getLogger(__name__)

# One-shot flag so an expired/invalid CORE key warns only once per process,
# not once per paper. Reset is unnecessary — a WARNING-level line is enough
# to surface the issue in logs; the admin fixes the key and restarts.
_CORE_KEY_WARNED = False


def _reconstruct_from_inverted(inv: dict) -> str:
    """Reconstruct text from OpenAlex's abstract_inverted_index format.

    OpenAlex stores abstracts as {word: [positions]} to optimize storage.
    To reconstruct: invert to {position: word}, sort by position, join.
    """
    if not inv:
        return ""
    word_at: dict[int, str] = {}
    for word, positions in inv.items():
        for pos in positions:
            word_at[pos] = word
    return " ".join(word_at[i] for i in sorted(word_at))


async def _try_openalex(doi: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Tier 1: OpenAlex by DOI."""
    url = f"https://api.openalex.org/works/doi:{doi}"
    body, status = await limited_fetch(
        session, url, timeout=15, max_retries=2,
    )
    if body is None:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    inv = data.get("abstract_inverted_index")
    if inv:
        text = _reconstruct_from_inverted(inv)
        if text:
            return text
    return None


async def _try_semantic_scholar(doi: str, session: aiohttp.ClientSession) -> Optional[str]:
    """Tier 2: Semantic Scholar by DOI (anonymous, 1 req/s)."""
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
    body, status = await limited_fetch(
        session, url, params={"fields": "abstract"},
        timeout=15, max_retries=2,
    )
    if body is None:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    abstract = data.get("abstract")
    if abstract and isinstance(abstract, str) and abstract.strip():
        return abstract.strip()
    return None


async def _try_core(doi: str, session: aiohttp.ClientSession,
                     api_key: str) -> Optional[str]:
    """Tier 3: CORE API by DOI (requires free API key)."""
    if not api_key:
        return None
    url = "https://api.core.ac.uk/v3/search/works"
    body, status = await limited_fetch(
        session, url,
        params={"q": f"doi:{doi}", "limit": "1"},
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15, max_retries=2,
    )
    # Detect expired / invalid CORE key (auth failure). Warn ONCE per process so
    # we don't spam across hundreds of papers. The cascade degrades gracefully:
    # CORE is skipped, OpenAlex + Semantic Scholar (Tiers 1-2) keep working, and
    # the pipeline is unaffected.
    if status in (401, 403):
        global _CORE_KEY_WARNED
        if not _CORE_KEY_WARNED:
            _CORE_KEY_WARNED = True
            logger.warning(
                "[AbstractResolver] CORE API key 失效或已过期 (HTTP %d)。"
                "摘要级联自动跳过 CORE 层——OpenAlex + Semantic Scholar 仍正常工作，"
                "pipeline 不受影响。如需恢复 CORE 兜底，请到 "
                "https://core.ac.uk/services/api 续期，并更新 config.yaml 的 "
                "sources.core_api_key。",
                status,
            )
        return None
    if body is None:
        return None
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    results = data.get("results") or []
    if results:
        abstract = results[0].get("abstract")
        if abstract and isinstance(abstract, str) and abstract.strip():
            return abstract.strip()
    return None


async def resolve_abstract(
    doi: str,
    *,
    session: aiohttp.ClientSession,
    core_api_key: str = "",
    skip_sources: Optional[set[str]] = None,
) -> str:
    """Try multiple sources in order; return the first non-empty abstract found.

    Args:
        doi:           DOI string (no URL prefix).
        session:       aiohttp session to reuse.
        core_api_key:  CORE API key; empty disables Tier 3.
        skip_sources:  Set of source names to skip ('openalex', 's2', 'core').

    Returns:
        The resolved abstract, or '' if all tiers failed.
    """
    if not doi:
        return ""
    skip = skip_sources or set()

    # Tier 1: OpenAlex
    if "openalex" not in skip:
        try:
            result = await _try_openalex(doi, session)
            if result:
                logger.debug("[AbstractResolver] OpenAlex hit for %s", doi[:40])
                return result
        except Exception as exc:
            logger.debug("[AbstractResolver] OpenAlex error for %s: %s", doi[:40], exc)

    # Tier 2: Semantic Scholar (anonymous)
    if "s2" not in skip:
        try:
            result = await _try_semantic_scholar(doi, session)
            if result:
                logger.debug("[AbstractResolver] S2 hit for %s", doi[:40])
                return result
        except Exception as exc:
            logger.debug("[AbstractResolver] S2 error for %s: %s", doi[:40], exc)

    # Tier 3: CORE
    if "core" not in skip and core_api_key:
        try:
            result = await _try_core(doi, session, core_api_key)
            if result:
                logger.debug("[AbstractResolver] CORE hit for %s", doi[:40])
                return result
        except Exception as exc:
            logger.debug("[AbstractResolver] CORE error for %s: %s", doi[:40], exc)

    return ""
