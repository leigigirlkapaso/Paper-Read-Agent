"""
agent1/semantic_scholar_searcher.py
调用 Semantic Scholar 官方 API 检索学术文献。

覆盖范围：NeurIPS / ICML / CVPR / ICCV / ECCV / AAAI / ACL / EMNLP 等所有顶会。
API 文档：https://api.semanticscholar.org/api-docs/graph

返回结构：
  List[PaperMeta]，与 arxiv_searcher 共用同一数据结构：
    arxiv_id, title, authors, published, abstract, pdf_url, arxiv_url
  - 若论文在 arxiv 上有版本，arxiv_id 取 arxiv 编号
  - 否则 arxiv_id 取 's2_{s2id[:12]}' 作为内部唯一标识
  - pdf_url 仅保留开放获取链接（非开放获取则留空，下载时跳过）

特性：
  - 按引用数×时间综合排序，每条 query 最多取 100 篇
  - 支持年份下限过滤
  - 请求失败自动重试，超出速率限制等待 30s
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import time
import unicodedata

import requests

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)

_S2_BASE = "https://api.semanticscholar.org/graph/v1"
_SEARCH_URL = f"{_S2_BASE}/paper/search"
_FIELDS = ",".join([
    "title", "abstract", "authors", "year", "publicationDate",
    "openAccessPdf", "externalIds", "venue", "url",
])
_HEADERS = {
    "User-Agent": "PaperReadAgent/1.0 (research tool; contact: paperreadagent@example.com)",
}
_RETRY_COUNT = 3
_RETRY_BASE_DELAY = 5.0
_RATE_LIMIT_BASE_DELAY = 10.0


def search_semantic_scholar(
    queries: list[str],
    max_results_per_query: int = 100,
    min_year: int = 0,
    api_key: str = "",
    query_delay: float = 3.0,
    max_queries: int = 8,
) -> list[PaperMeta]:
    """
    用多条 query 分别检索 Semantic Scholar，去重后返回候选论文列表。

    Args:
        queries:               检索查询串列表（与 arxiv 同一批 queries）
        max_results_per_query: 每条 query 最多取回的论文数（最大 100）
        min_year:              过滤早于此年份的论文（0=不过滤）
        api_key:               Semantic Scholar API Key（可选，提升速率上限）
        query_delay:           每条 query 之间的等待秒数
        max_queries:           最多执行的 query 数量

    Returns:
        去重后的 PaperMeta 列表
    """
    headers = dict(_HEADERS)
    if api_key:
        headers["x-api-key"] = api_key

    limit = min(max_results_per_query, 100)
    seen_ids: set[str] = set()    # 用于去重（s2id 或 arxiv clean_id）
    papers: list[PaperMeta] = []
    active_queries = queries[:max_queries]

    if min_year > 0:
        logger.info(f"[S2] 年份过滤: >= {min_year}，每条 query 取前 {limit} 篇")
    else:
        logger.info(f"[S2] 每条 query 取前 {limit} 篇（无年份过滤）")

    def _run_isolated(query: str) -> tuple[str, list[PaperMeta], set[str]]:
        local_papers: list[PaperMeta] = []
        local_seen: set[str] = set()
        clean_query = _strip_arxiv_syntax(query)
        _run_query(
            query=clean_query, limit=limit, min_year=min_year,
            headers=headers, seen_ids=local_seen, papers=local_papers,
        )
        return (query, local_papers, local_seen)

    max_workers = min(len(active_queries), 4)
    if max_workers <= 1:
        for query in active_queries:
            q, new_papers, new_ids = _run_isolated(query)
            for p in new_papers:
                if p.arxiv_id not in seen_ids:
                    seen_ids.add(p.arxiv_id)
                    papers.append(p)
            seen_ids.update(new_ids)
            logger.info(f"[S2] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")
            time.sleep(query_delay)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_run_isolated, q): q for q in active_queries}
            for future in concurrent.futures.as_completed(future_map):
                q, new_papers, new_ids = future.result()
                for p in new_papers:
                    if p.arxiv_id not in seen_ids:
                        seen_ids.add(p.arxiv_id)
                        papers.append(p)
                seen_ids.update(new_ids)
                logger.info(f"[S2] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")

    logger.info(f"[S2] Semantic Scholar 检索完成，新增 {len(papers)} 篇（去重后）")
    return papers


def _run_query(
    query: str,
    limit: int,
    min_year: int,
    headers: dict,
    seen_ids: set[str],
    papers: list[PaperMeta],
) -> int:
    """执行单条 query，返回新增篇数。"""
    params = {
        "query": query,
        "limit": limit,
        "fields": _FIELDS,
    }
    if min_year > 0:
        params["year"] = f"{min_year}-"   # e.g. "2022-" 表示 2022 年及以后

    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            resp = requests.get(
                _SEARCH_URL,
                params=params,
                headers=headers,
                timeout=(10, 15),  # (connect, read) fast-fail + retry
            )
            if resp.status_code == 429:
                wait = _RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"[S2] HTTP 429，等待 {wait:.0f}s（指数退避）...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[S2] 请求失败（第 {attempt} 次）: {e}")
            if attempt < _RETRY_COUNT:
                time.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            else:
                logger.error(f"[S2] 放弃 query: {query[:60]}")
                return 0
    else:
        return 0

    n_added = 0
    for item in data.get("data", []):
        paper_id = item.get("paperId", "")
        if not paper_id:
            continue

        # ── 确定唯一 ID ──────────────────────────────────────
        external = item.get("externalIds") or {}
        arxiv_raw = external.get("ArXiv", "")
        doi = external.get("DOI", "")             # 提取 DOI 用于多源下载
        if arxiv_raw:
            clean_id = arxiv_raw.strip().split("v")[0]
            internal_key = f"arxiv_{clean_id}"
        else:
            clean_id = f"s2_{paper_id[:12]}"
            internal_key = f"s2_{paper_id}"

        if internal_key in seen_ids:
            continue
        seen_ids.add(internal_key)

        # ── PDF URL（仅开放获取）──────────────────────────────
        oap = item.get("openAccessPdf") or {}
        open_pdf_url = oap.get("url", "")
        if arxiv_raw:
            # arxiv 版本永远优先
            pdf_url = f"https://arxiv.org/pdf/{clean_id}"
        elif open_pdf_url:
            pdf_url = open_pdf_url
        else:
            pdf_url = ""   # 无可用 PDF，下载时会跳过

        # ── 基本字段 ─────────────────────────────────────────
        title = _clean(item.get("title") or "")
        if not title:
            continue

        authors = [
            a.get("name", "") for a in (item.get("authors") or []) if a.get("name")
        ]

        pub_date = item.get("publicationDate") or ""
        if pub_date and len(pub_date) >= 10:
            published = pub_date[:10]   # "YYYY-MM-DD"
        elif item.get("year"):
            published = str(item["year"]) + "-01-01"
        else:
            published = "unknown"

        abstract = _clean(item.get("abstract") or "")
        venue = item.get("venue") or ""
        s2_url = item.get("url") or f"https://www.semanticscholar.org/paper/{paper_id}"
        # arxiv URL 若有 arxiv ID 则给 arxiv 链接，否则给 S2 链接
        paper_url = (
            f"https://arxiv.org/abs/{clean_id}"
            if arxiv_raw
            else s2_url
        )

        papers.append(PaperMeta(
            arxiv_id=clean_id,
            title=title,
            authors=authors,
            published=published,
            abstract=abstract,
            pdf_url=pdf_url,
            arxiv_url=paper_url,
            doi=doi,
            venue=venue,
        ))
        n_added += 1

    return n_added


def _clean(text: str) -> str:
    """去除多余空白和控制字符。"""
    text = unicodedata.normalize("NFKC", text)
    return " ".join(text.split())


def _strip_arxiv_syntax(query: str) -> str:
    """去除 arxiv 专有语法，保留核心词组用于 Semantic Scholar 自然语言检索。"""
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    q = q.replace('"', '')
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    q = ' '.join(q.split())
    return q


def _s2_external_id(paper) -> str | None:
    """S2 graph id for a PaperMeta: ARXIV:<id> or DOI:<doi>, else None."""
    from utils.paper_dedup import extract_identifiers
    ids = extract_identifiers(paper)
    if ids.get("arxiv_id"):
        return f"ARXIV:{ids['arxiv_id']}"
    if ids.get("doi"):
        return f"DOI:{ids['doi']}"
    return None


def _s2_obj_to_paper(obj: dict):
    """Map an S2 paper object to PaperMeta. Returns None without title/abstract."""
    title = (obj.get("title") or "").strip()
    abstract = (obj.get("abstract") or "").strip()
    if not title or not abstract:
        return None
    ext = obj.get("externalIds") or {}
    arxiv = ext.get("ArXiv")
    doi = (ext.get("DOI") or "")
    # synthetic key uses s2_ prefix (matches main path) to avoid the OpenAlex oa_ namespace,
    # which extract_identifiers would otherwise mis-tag as an OpenAlex entity id
    aid = arxiv if arxiv else f"s2_{obj.get('paperId', '')}"
    year = obj.get("year")
    return PaperMeta(
        arxiv_id=aid, title=title,
        authors=[a.get("name", "") for a in (obj.get("authors") or []) if a.get("name")],
        published=(f"{year}-01-01" if year else "unknown"),
        abstract=abstract,
        pdf_url=(f"https://arxiv.org/pdf/{arxiv}" if arxiv else ""),
        arxiv_url=(f"https://arxiv.org/abs/{arxiv}" if arxiv else ""),
        doi=doi.replace("https://doi.org/", ""),
        citation_count=int(obj.get("citationCount") or 0),
    )


def fetch_neighbors_s2(paper, limit: int = 25) -> list:
    """Fallback bidirectional 1-hop neighbors via Semantic Scholar graph API.
    Returns [] on any failure (best-effort)."""
    ext_id = _s2_external_id(paper)
    if not ext_id:
        return []
    fields = "title,abstract,externalIds,year,authors,citationCount"
    out = []
    try:
        for direction, key in (("references", "citedPaper"), ("citations", "citingPaper")):
            url = f"{_S2_BASE}/paper/{ext_id}/{direction}"
            resp = requests.get(url, params={"fields": fields, "limit": min(limit, 100)},
                                headers=_HEADERS, timeout=(10, 15))
            resp.raise_for_status()
            for row in resp.json().get("data", []):
                p = _s2_obj_to_paper(row.get(key) or {})
                if p is not None:
                    out.append(p)
    except Exception as e:
        logger.warning(f"[S2-graph] fallback failed: {e}")
        return []
    return out
