"""
utils/paper_dedup.py
Cross-platform, multi-identifier paper deduplication.

Each PaperMeta record may carry up to 6 identifiers (arxiv_id, doi, pmid,
pmcid, dblp_key, oa_id) plus a normalised title. Two records cluster
together if ANY identifier matches. Field-level merging preserves the
best metadata from every source (longest abstract, real DOI, all source
platforms, etc.).

Public API:
  - extract_identifiers(p)   PaperMeta -> dict[str, str]
  - merge_papers(a, b)       commutative field merger
  - dedup_papers(papers)     Union-Find clustering main entry point
"""
from __future__ import annotations

import logging
import re
from collections import Counter

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)


# --- Helpers ----------------------------------------------------------

def _is_real_arxiv_id(s: str | None) -> bool:
    """True iff *s* is a recognisable arxiv ID (modern YYMM.NNNNN or legacy cat/NNNNNNN)."""
    s = (s or "").lower().split("v")[0]
    return bool(
        re.match(r"^\d{4}\.\d{4,6}$", s)
        or re.match(r"^[a-z\-]+/\d{7}$", s)
    )


def _pdf_url_priority(url: str | None) -> int:
    """Lower wins. arXiv > other https > other > empty."""
    if not url:
        return 99
    if "arxiv.org/pdf/" in url:
        return 0
    if url.startswith("https://"):
        return 1
    return 2


def _longer_or_first(a: str | None, b: str | None) -> str:
    """Longer string wins; ties broken lexicographically (commutative)."""
    a, b = a or "", b or ""
    if len(a) > len(b):
        return a
    if len(b) > len(a):
        return b
    if a and b:
        return min(a, b)
    return a or b


def _real_or_first(a: str | None, b: str | None) -> str:
    """Date merger: 'unknown' < real date; ties take lex-smaller (commutative)."""
    a, b = a or "", b or ""
    a_real = bool(a) and a != "unknown"
    b_real = bool(b) and b != "unknown"
    if a_real and not b_real:
        return a
    if b_real and not a_real:
        return b
    if a_real and b_real:
        return min(a, b)
    return a or b


def _merge_platforms(a: str | None, b: str | None) -> str:
    """Comma-join, dedup, sort (commutative deterministic output)."""
    parts: set[str] = set()
    for x in (a, b):
        for tok in (x or "").split(","):
            tok = tok.strip()
            if tok:
                parts.add(tok)
    return ",".join(sorted(parts))


def _normalize_title(title: str | None) -> str:
    """Conservative title normalisation: lowercase + strip LaTeX/HTML/punct/whitespace."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"\\[a-z]+\{([^}]*)\}", r"\1", t)   # \textit{foo} -> foo
    t = re.sub(r"\\[a-z]+\b", "", t)               # bare \LaTeX -> (removed)
    t = re.sub(r"<[^>]+>", "", t)                  # strip HTML tags
    t = re.sub(r"[^\w\s]", " ", t)                 # punct -> space
    t = " ".join(t.split())                        # collapse whitespace
    return t.strip()


# --- extract_identifiers ----------------------------------------------

def extract_identifiers(p: PaperMeta) -> dict[str, str]:
    """Extract all available identifiers + normalised title from a PaperMeta.

    Returns a dict like {kind: normalised_value}. Kinds:
      arxiv_id, doi, pmid, pmcid, dblp_key, oa_id, title_norm

    Only non-empty fields are present. The arxiv-DOI reverse-lookup
    (`10.48550/arxiv.X` -> arxiv_id `X`) bridges arxiv preprint records
    with DBLP/OpenAlex records that carry the same paper's official DOI.
    """
    ids: dict[str, str] = {}
    raw = (p.arxiv_id or "").strip().lower()

    # (1) arxiv_id field - only a real arxiv ID counts.
    if raw and _is_real_arxiv_id(raw):
        ids["arxiv_id"] = raw.split("v")[0]

    # (2) DOI normalisation
    doi = (p.doi or "").strip().lower()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.rstrip("/")
    if doi:
        ids["doi"] = doi
        # arxiv DOI reverse-lookup
        m = re.match(r"^10\.48550/arxiv\.(\S+)$", doi)
        if m:
            ids.setdefault("arxiv_id", m.group(1).split("v")[0])

    # (3) Prefix-tagged identifiers (mutually exclusive)
    if raw.startswith("pmid_"):
        ids["pmid"] = raw[5:]
    elif raw.startswith("pmcid_"):
        ids["pmcid"] = raw[6:].lower()
    elif raw.startswith("dblp_"):
        ids["dblp_key"] = raw[5:]
    elif raw.startswith("oa_"):
        ids["oa_id"] = raw[3:].lower()
    elif raw.startswith("or_"):
        ids["or_id"] = raw[3:].lower()

    # (4) Title fallback
    title = _normalize_title(p.title or "")
    if title:
        ids["title_norm"] = title

    return ids


# --- Field-by-field merger --------------------------------------------

def merge_papers(a: PaperMeta, b: PaperMeta) -> PaperMeta:
    """Combine two records of the same paper. Commutative.

    Field rules (see spec §3.2):
      arxiv_id          real arxiv ID > anything else
      title             longer (with subtitle) wins; ties -> lex-smaller
      authors           longer list wins; ties -> lex-smaller list
      published         real date > 'unknown'; ties -> lex-smaller
      abstract          longer non-empty wins; ties -> lex-smaller
      pdf_url           arxiv URL > other https > other > empty
      arxiv_url         first non-empty
      doi               first non-empty
      relevance_score   reset to 0.0 (scoring happens after dedup)
      source_platform   union, sorted, comma-joined
      venue             longer non-empty wins; ties -> lex-smaller
      code_url          first non-empty
      citation_count    max
    """
    # arxiv_id: real ID priority
    if _is_real_arxiv_id(a.arxiv_id) and not _is_real_arxiv_id(b.arxiv_id):
        new_id = a.arxiv_id
    elif _is_real_arxiv_id(b.arxiv_id) and not _is_real_arxiv_id(a.arxiv_id):
        new_id = b.arxiv_id
    else:
        new_id = a.arxiv_id or b.arxiv_id

    # pdf_url: priority-based
    new_pdf = (a.pdf_url
               if _pdf_url_priority(a.pdf_url) <= _pdf_url_priority(b.pdf_url)
               else b.pdf_url)

    # authors: longer list, ties broken by lex order (commutative)
    if len(a.authors) > len(b.authors):
        new_authors = a.authors
    elif len(b.authors) > len(a.authors):
        new_authors = b.authors
    else:
        new_authors = min(a.authors, b.authors)   # lex-smaller list

    return PaperMeta(
        arxiv_id=new_id,
        title=_longer_or_first(a.title, b.title),
        authors=new_authors,
        published=_real_or_first(a.published, b.published),
        abstract=_longer_or_first(a.abstract, b.abstract),
        pdf_url=new_pdf,
        arxiv_url=a.arxiv_url or b.arxiv_url,
        doi=a.doi or b.doi,
        relevance_score=0.0,
        source_platform=_merge_platforms(a.source_platform, b.source_platform),
        venue=_longer_or_first(a.venue, b.venue),
        code_url=a.code_url or b.code_url,
        citation_count=max(a.citation_count or 0, b.citation_count or 0),
    )


# --- Union-Find clustering main entry point ---------------------------

def dedup_papers(papers: list[PaperMeta]) -> list[PaperMeta]:
    """Cluster papers by overlapping identifiers; merge each cluster's records.

    Algorithm (single pass, Union-Find on dict-of-clusters):
      For each paper p:
        1. Extract its identifiers (arxiv_id, doi, pmid, ..., title_norm).
        2. Look up which existing clusters those IDs map to.
        3a. None matched: create a new cluster keyed by all p's IDs.
        3b. Exactly one cluster matched: merge p into it; register any new IDs.
        3c. Multiple clusters matched: BRIDGE - fold all matched clusters into
            the lowest-numbered one, re-point id_index entries, then merge p.

    Papers with NO identifiers (no IDs and no normalisable title) are dropped.

    Returns a list of merged super-records, one per equivalence class.
    """
    clusters: dict[int, PaperMeta] = {}
    id_index: dict[str, int] = {}
    next_cid = 0

    for p in papers:
        ids = extract_identifiers(p)
        if not ids:
            continue   # no identifiers - skip (extremely rare)

        hit_cids = {id_index[v] for v in ids.values() if v in id_index}

        if not hit_cids:
            # New cluster
            cid = next_cid
            next_cid += 1
            clusters[cid] = p
            for v in ids.values():
                id_index[v] = cid

        elif len(hit_cids) == 1:
            # Extend existing cluster
            cid = next(iter(hit_cids))
            clusters[cid] = merge_papers(clusters[cid], p)
            for v in ids.values():
                id_index.setdefault(v, cid)

        else:
            # Bridge: collapse all matched clusters into the lowest-numbered one
            sorted_cids = sorted(hit_cids)
            keep_cid = sorted_cids[0]
            merged = clusters[keep_cid]
            for cid in sorted_cids[1:]:
                merged = merge_papers(merged, clusters.pop(cid))
                # Re-point any id_index entries that pointed to absorbed clusters
                for k, v in list(id_index.items()):
                    if v == cid:
                        id_index[k] = keep_cid
            merged = merge_papers(merged, p)
            clusters[keep_cid] = merged
            for v in ids.values():
                id_index.setdefault(v, keep_cid)

    out = list(clusters.values())
    _log_dedup_stats(papers, out)
    return out


def _log_dedup_stats(input_papers: list[PaperMeta], output_papers: list[PaperMeta]) -> None:
    """Emit a 3-line diagnostic summary so users can see the savings."""
    n_in, n_out = len(input_papers), len(output_papers)
    pct = (1 - n_out / n_in) * 100 if n_in else 0
    logger.info("[Dedup] 输入 %d 篇 → 输出 %d 篇（去重 %d 篇，节省 %.1f%%）",
                n_in, n_out, n_in - n_out, pct)

    plat_count: Counter[str] = Counter(
        (p.source_platform or "unknown") for p in input_papers
    )
    for plat, n in plat_count.most_common():
        logger.info("[Dedup]   %-10s 输入 %d 篇", plat, n)

    multi_src = sum(1 for p in output_papers if "," in p.source_platform)
    logger.info("[Dedup] 多源覆盖：%d 篇被 ≥2 个平台同时找到", multi_src)
