"""
agent1/hybrid_prefilter.py
Hybrid dense (bge-m3) + sparse (BM25) pre-filter via RRF fusion. Inserted
BEFORE agent1.paper_filter.filter_papers to cut a large candidate pool to
prefilter_top_k items, so the downstream LLM scorer makes ~3x fewer calls.

Synchronous (matches agent1 convention). Best-effort: any failure /
disabled / too-small pool -> returns the input list unchanged, never raises.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import threading
from typing import Any

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)

_RRF_K = 60  # standard Reciprocal Rank Fusion constant
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_CORE_LLM_SINGLETON: dict[str, Any] = {"obj": None, "fp": None}
_CORE_LLM_LOCK = threading.Lock()


def _get_core_llm(llm_cfg: dict):
    """Lazy module-singleton CoreLLM, keyed by llm_cfg fingerprint. Re-created if
    config changes (e.g. between tests / model swaps)."""
    from core.llm import CoreLLM
    fp = (llm_cfg.get("api_base_url"), llm_cfg.get("embedding_model"),
          llm_cfg.get("embedding_provider"))
    with _CORE_LLM_LOCK:
        if _CORE_LLM_SINGLETON["obj"] is None or _CORE_LLM_SINGLETON["fp"] != fp:
            _CORE_LLM_SINGLETON["obj"] = CoreLLM.from_config(llm_cfg)
            _CORE_LLM_SINGLETON["fp"] = fp
        return _CORE_LLM_SINGLETON["obj"]


def _tokenize(text: str) -> list[str]:
    """Lowercase + \\w+ split. Works for English and CJK (each Han char is a token)."""
    return _TOKEN_RE.findall((text or "").lower())


def _cosine_unit(a: list[float], b: list[float]) -> float:
    """Cosine for unit-norm vectors = dot product. Both must be same length."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def _passage(p: PaperMeta, passage_chars: int) -> str:
    return f"{p.title}\n{(p.abstract or '')[:passage_chars]}"


async def _embed_all(llm, texts: list[str], concurrency: int) -> list[list[float]]:
    return await llm.embed_batch(texts, module="agent1", concurrency=concurrency)


def _rank_desc(scores: list[float]) -> list[float]:
    """Return rank (1=best) for each index, ranked by score descending.
    Tied items get the AVERAGE rank of their tie-group (fractional ranking).
    This makes RRF symmetric: a single outlier surrounded by N tied papers
    competes against the tie-group's mid-rank, not the best of them."""
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    rank: list[float] = [0.0] * len(scores)
    n = len(scores)
    i = 0
    while i < n:
        j = i
        # Extend j over a run of equal scores
        while j + 1 < n and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg_rank = (i + 1 + j + 1) / 2.0  # 1-indexed average of [i+1..j+1]
        for k in range(i, j + 1):
            rank[order[k]] = avg_rank
        i = j + 1
    return rank


def hybrid_prefilter(
    papers: list[PaperMeta],
    topic: str,
    cfg: dict,
) -> list[PaperMeta]:
    """Cut `papers` to top `prefilter_top_k` via dense+BM25 RRF fusion.
    Returns `papers` unchanged on disable / too-small pool / any failure.

    `cfg` must contain a `_llm_cfg` dict (== cfg["llm"] in the project)
    so this module can construct CoreLLM. Falls back to cfg["llm"] if
    `_llm_cfg` absent.
    """
    if not cfg.get("enable_hybrid_prefilter", True):
        return papers

    top_k = int(cfg.get("prefilter_top_k", 150))
    min_input = int(cfg.get("prefilter_min_input", 60))
    dense_w = float(cfg.get("prefilter_dense_weight", 0.5))
    concurrency = int(cfg.get("prefilter_embed_concurrency", 16))
    passage_chars = int(cfg.get("prefilter_passage_chars", 1500))

    if len(papers) <= min_input or len(papers) <= top_k:
        return papers

    llm_cfg = cfg.get("_llm_cfg") or cfg.get("llm") or {}
    try:
        llm = _get_core_llm(llm_cfg)
        passages = [_passage(p, passage_chars) for p in papers]

        # asyncio.run requires NO running event loop. We're called from agent1's
        # sync path; web routes call us through asyncio.to_thread, so the worker
        # thread has no running loop. If a future caller invokes us from inside
        # a coroutine, bypass gracefully rather than raising.
        try:
            asyncio.get_running_loop()
            logger.warning("[hybrid_prefilter] called from running event loop — bypass")
            return papers
        except RuntimeError:
            pass  # no running loop, safe to use asyncio.run
        embeds = asyncio.run(_embed_all(llm, passages + [topic], concurrency))
        if not embeds or len(embeds) != len(passages) + 1:
            logger.warning("[hybrid_prefilter] embed_batch shape mismatch — bypass")
            return papers
        query_vec = embeds[-1]
        paper_vecs = embeds[:-1]

        # Dense scores (cosine == dot, since bge-m3 is unit-norm).
        # Papers with empty embedding get -inf so they sink in dense rank
        # (BM25 channel still has a fair shot via RRF).
        dense_scores = [
            (_cosine_unit(v, query_vec) if v else -math.inf)
            for v in paper_vecs
        ]

        # BM25 over tokenized passages
        try:
            from rank_bm25 import BM25Okapi
            corpus = [_tokenize(p) for p in passages]
            if any(corpus):
                bm25 = BM25Okapi(corpus)
                bm25_scores = list(bm25.get_scores(_tokenize(topic)))
            else:
                bm25_scores = [0.0] * len(passages)
        except Exception:
            logger.warning("[hybrid_prefilter] BM25 failed - bypass", exc_info=True)
            return papers

        # Rank each channel (1 = best). Stable ties don't matter for RRF.
        dense_rank = _rank_desc(dense_scores)
        bm25_rank = _rank_desc(bm25_scores)

        # RRF fused score (higher = better)
        fused = [
            dense_w * (1.0 / (_RRF_K + dense_rank[i])) +
            (1.0 - dense_w) * (1.0 / (_RRF_K + bm25_rank[i]))
            for i in range(len(papers))
        ]

        order = sorted(range(len(papers)), key=lambda i: fused[i], reverse=True)
        kept = [papers[i] for i in order[:top_k]]
        logger.info(
            f"[hybrid_prefilter] {len(papers)} -> {len(kept)} via RRF "
            f"(dense_w={dense_w}, embed_ok={sum(1 for v in paper_vecs if v)}/{len(paper_vecs)})"
        )
        return kept
    except Exception:
        logger.warning("[hybrid_prefilter] aborted - bypass", exc_info=True)
        return papers
