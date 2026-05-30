"""
agent1/dblp_searcher.py
调用 DBLP 公开搜索 API 检索计算机科学文献。

覆盖范围：CS 领域所有顶会/期刊论文（CHI、UIST、CSCW、IMWUT 等 HCI
顶会通常不在 arXiv，但 DBLP 完整收录）。
API 文档：https://dblp.org/faq/How+to+use+the+dblp+search+API.html

返回结构：
  List[PaperMeta]，与 arxiv_searcher 共用同一数据结构。
  - arxiv_id 取 'dblp_{dblp_key}' 或 DOI 作为内部标识
  - pdf_url 通过 DOI 推导（不保证可用，下载器会级联尝试）
  - DBLP 无摘要字段，abstract 从标题+venue 拼接简要描述

特性：
  - 免费，无需 API Key
  - 每次查询最多返回 1000 条（h 参数）
  - 礼貌速率：每条 query 间隔 ≥ 1s
"""

from __future__ import annotations

import logging
import re
import time

import requests

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://dblp.org/search/publ/api"
_HEADERS = {"User-Agent": "PaperReadAgent/1.0 (research tool; mailto:paperreadagent@example.com)"}


def search_dblp(
    queries: list[str],
    max_results_per_query: int = 100,
    min_year: int = 0,
    query_delay: float = 2.0,
    max_queries: int = 8,
) -> list[PaperMeta]:
    """
    用多条 query 分别检索 DBLP，去重后返回候选论文列表。

    Args:
        queries:               检索查询串列表
        max_results_per_query: 每条 query 最多取回的论文数（最大 1000）
        min_year:              过滤早于此年份的论文（0=不过滤）
        query_delay:           每条 query 之间的等待秒数
        max_queries:           最多执行的 query 数量

    Returns:
        去重后的 PaperMeta 列表
    """
    limit = min(max_results_per_query, 1000)
    seen_ids: set[str] = set()
    papers: list[PaperMeta] = []
    active_queries = queries[:max_queries]

    logger.info(f"[DBLP] 开始检索，共 {len(active_queries)} 条 query，每 query 上限 {limit}")

    for i, query in enumerate(active_queries):
        if i > 0:
            logger.info(f"[DBLP] 等待 {query_delay:.0f}s（防限流）...")
            time.sleep(query_delay)

        clean_query = _strip_arxiv_syntax(query)
        if not clean_query:
            continue

        n_added = _run_query(
            query=clean_query,
            limit=limit,
            min_year=min_year,
            seen_ids=seen_ids,
            papers=papers,
        )
        logger.info(f"[DBLP] {'[+]' if n_added > 0 else '[ ]'} {n_added:>3} 篇  |  {clean_query[:80]}")

    logger.info(f"[DBLP] 检索完成，新增 {len(papers)} 篇（去重后）")
    return papers


def _run_query(
    query: str,
    limit: int,
    min_year: int,
    seen_ids: set[str],
    papers: list[PaperMeta],
) -> int:
    """执行单条 query，返回新增篇数。"""
    params = {
        "q": query,
        "format": "json",
        "h": min(limit, 1000),
    }

    for attempt in range(1, 4):
        try:
            resp = requests.get(
                _SEARCH_URL,
                params=params,
                headers=_HEADERS,
                timeout=30,
            )
            if resp.status_code == 429:
                wait = 30 * attempt
                logger.warning(f"[DBLP] HTTP 429，等待 {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[DBLP] 请求失败（第 {attempt} 次）: {e}")
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                logger.error(f"[DBLP] 放弃 query: {query[:60]}")
                return 0
    else:
        return 0

    hits = (data.get("result") or {}).get("hits") or {}
    hit_list = hits.get("hit") or []

    n_added = 0
    for item in hit_list:
        info = item.get("info") or {}
        title = (info.get("title") or "").replace("\n", " ").strip()
        if not title:
            continue

        # ── 年份过滤 ──────────────────────────────────────────
        year_str = info.get("year", "")
        try:
            year = int(year_str) if year_str else 0
        except (ValueError, TypeError):
            year = 0
        if min_year > 0 and year > 0 and year < min_year:
            continue

        # ── 唯一 ID ────────────────────────────────────────────
        doi = (info.get("doi") or "").strip()
        dblp_key = (item.get("@id") or "").strip()  # e.g. "conf/chi/Smith2024"
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

        # ── 作者 ────────────────────────────────────────────────
        authors_data = info.get("authors") or {}
        author_list = authors_data.get("author", [])
        if isinstance(author_list, dict):
            author_list = [author_list]
        authors = [a.get("text", "") for a in author_list if a.get("text")]

        # ── 日期 ────────────────────────────────────────────────
        published = f"{year}-01-01" if year > 0 else "unknown"

        # ── 出处 ────────────────────────────────────────────────
        venue = (info.get("venue") or "").strip()

        # ── 摘要 ────────────────────────────────────────────────
        pub_type = (info.get("type") or "").strip()
        abstract = f"[DBLP] {title}"
        if venue:
            abstract += f" — Published in {venue}"
        if year > 0:
            abstract += f" ({year})"
        if pub_type:
            abstract += f" [{pub_type}]"

        # ── URL ─────────────────────────────────────────────────
        paper_url = info.get("url", "") or f"https://dblp.org/rec/{dblp_key}"

        # ── PDF ─────────────────────────────────────────────────
        pdf_url = ""
        if doi:
            pdf_url = f"https://doi.org/{doi}"  # 下载器会尝试 Unpaywall/Sci-Hub

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


def _strip_arxiv_syntax(query: str) -> str:
    """去除 arxiv 专有语法，保留自然语言核心词用于 DBLP 检索。"""
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    q = q.replace('"', '')
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    q = ' '.join(q.split())
    return q
