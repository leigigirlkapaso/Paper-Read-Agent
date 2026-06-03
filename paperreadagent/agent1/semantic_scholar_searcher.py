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

import logging
import time
import unicodedata

import requests

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_FIELDS = ",".join([
    "title", "abstract", "authors", "year", "publicationDate",
    "openAccessPdf", "externalIds", "venue", "url",
])
_HEADERS = {
    "User-Agent": "PaperReadAgent/1.0 (research tool; contact: paperreadagent@example.com)",
}


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

    for i, query in enumerate(active_queries):
        if i > 0:
            logger.info(f"[S2] 等待 {query_delay:.0f}s（防限流）...")
            time.sleep(query_delay)

        n_added = _run_query(
            query=query,
            limit=limit,
            min_year=min_year,
            headers=headers,
            seen_ids=seen_ids,
            papers=papers,
        )
        logger.info(f"[S2] {'[+]' if n_added > 0 else '[ ]'} {n_added:>3} 篇  |  {query[:80]}")

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

    for attempt in range(1, 4):
        try:
            resp = requests.get(
                _SEARCH_URL,
                params=params,
                headers=headers,
                timeout=30,
            )
            if resp.status_code == 429:
                wait = 30 * attempt
                logger.warning(f"[S2] HTTP 429，等待 {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[S2] 请求失败（第 {attempt} 次）: {e}")
            if attempt < 3:
                time.sleep(5 * attempt)
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
