"""
agent1/citation_expander.py
Citation snowballing: expand recall by fetching the bidirectional 1-hop citation
neighbors of the top-K filtered seeds, dedup against the existing pool, re-score
through the shared filter, and admit the best into extra download slots.

Synchronous (matches agent1's requests + llm.chat convention). Best-effort:
any failure degrades to "no expansion" — never raises into the pipeline.

Note: discovered_via provenance survives only for un-merged neighbors;
duplicates merged by dedup_papers lose the tag (provenance-only, does not
affect admission).
"""
from __future__ import annotations

import logging

from agent1.arxiv_searcher import PaperMeta
from agent1.paper_filter import filter_papers
from agent1.hybrid_prefilter import hybrid_prefilter
from agent1.openalex_searcher import resolve_work, fetch_referenced_works, fetch_citing_works
from agent1.semantic_scholar_searcher import fetch_neighbors_s2
from utils.paper_dedup import extract_identifiers, dedup_papers

logger = logging.getLogger(__name__)

_NON_TITLE_KINDS = ("arxiv_id", "doi", "pmid", "pmcid", "dblp_key", "oa_id", "or_id")


def _identifier_values(p: PaperMeta) -> set[str]:
    ids = extract_identifiers(p)
    return {ids[k] for k in _NON_TITLE_KINDS if ids.get(k)}


def expand_by_citations(seeds, candidate_pool, topic, llm, cfg) -> list[PaperMeta]:
    """Return up to `snowball_extra_slots` new, deduped, re-scored neighbor papers
    (tagged with discovered_via). Empty list if disabled or on any failure."""
    if not cfg.get("enable_citation_snowball", True):
        return []
    try:
        seed_n = int(cfg.get("snowball_seed_count", 8))
        per_dir = int(cfg.get("snowball_per_seed_per_direction", 25))
        max_neighbors = int(cfg.get("snowball_max_neighbors", 300))
        extra_slots = int(cfg.get("snowball_extra_slots", 10))
        threshold = float(cfg.get("relevance_threshold", 0.8))
        qweight = float(cfg.get("snowball_quality_weight", 0.15))

        seeds = list(seeds)[:seed_n]
        existing: set[str] = set()
        for p in candidate_pool:
            existing |= _identifier_values(p)

        neighbors: list[PaperMeta] = []
        for seed in seeds:
            sid = seed.arxiv_id
            try:
                work = resolve_work(seed)
                if work:
                    src = "oa"
                    back = fetch_referenced_works(work, limit=per_dir)
                    fwd = fetch_citing_works(work.get("id", ""), limit=per_dir)
                else:
                    src = "s2"
                    s2 = fetch_neighbors_s2(seed, limit=per_dir)
                    back, fwd = s2, []   # S2 fallback returns both dirs mixed
                for p in back:
                    p.source_platform = src
                    p.discovered_via = f"snowball:{sid}:backward"
                for p in fwd:
                    p.source_platform = src
                    p.discovered_via = f"snowball:{sid}:forward"
                neighbors.extend(back)
                neighbors.extend(fwd)
            except Exception as e:
                logger.warning(f"[snowball] seed {sid} expansion failed: {e}")
            if len(neighbors) >= max_neighbors:
                neighbors = neighbors[:max_neighbors]
                break

        if not neighbors:
            return []

        # dedup among neighbors, then drop any already in the candidate pool
        neighbors = dedup_papers(neighbors)
        fresh = [p for p in neighbors if not (_identifier_values(p) & existing)]
        if not fresh:
            return []

        logger.info(f"[snowball] {len(fresh)} fresh neighbors -> rescoring (extra_slots={extra_slots})")
        # Hybrid 粗排同样作用于 snowball rescoring（best-effort）
        prefiltered = hybrid_prefilter(fresh, topic, cfg)
        admitted = filter_papers(
            papers=prefiltered, topic=topic, llm=llm,
            relevance_threshold=threshold,
            max_download_papers=extra_slots,
            batch_size=int(cfg.get("search_batch_size", 10)),
            quality_weight=qweight,
        )
        logger.info(f"[snowball] admitted {len(admitted)} papers into extra slots")
        return admitted
    except Exception as e:
        logger.warning(f"[snowball] expansion aborted: {e}")
        return []
