"""
agent1/pmc_searcher.py
调用 Europe PMC 免费开放 API 检索生物医学文献。

覆盖范围：4200 万+ 生物医学论文，跨神经科学、脑机接口、fNIRS/EEG 等。
API 文档：https://europepmc.org/RestfulWebService

返回结构：
  List[PaperMeta]，与 arxiv_searcher 共用同一数据结构。
  - arxiv_id 取 f"pmid_{pmid}" 或 f"pmcid_{pmcid}" 作为内部标识
  - pdf_url 留空（下载路径走级联，不在搜索阶段设置）
  - source_platform 固定为 "pmc"

特性：
  - 无需 API Key，无 IP 限制
  - 结果按 RELEVANCE 排序
  - 仅保留有摘要的论文（无摘要的论文对 AGENT2 无价值）
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import time

import requests

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_POLITE_EMAIL = "paperreadagent@example.com"
_HEADERS = {"User-Agent": f"PaperReadAgent/1.0 (mailto:{_POLITE_EMAIL})"}
_RETRY_COUNT = 3
_RETRY_BASE_DELAY = 5.0
_RATE_LIMIT_BASE_DELAY = 10.0


def search_pmc(
    queries: list[str],
    max_results_per_query: int = 100,
    min_year: int = 0,
    query_delay: float = 1.0,
    max_queries: int = 8,
) -> list[PaperMeta]:
    """
    用多条 query 分别检索 Europe PMC，去重后返回候选论文列表。

    Args:
        queries:               检索查询串列表
        max_results_per_query: 每条 query 最多取回的论文数（最大 100）
        min_year:              过滤早于此年份的论文（0=不过滤）
        query_delay:           每条 query 之间的等待秒数
        max_queries:           最多执行的 query 数量

    Returns:
        去重后的 PaperMeta 列表
    """
    limit = min(max_results_per_query, 100)
    seen_ids: set[str] = set()
    papers: list[PaperMeta] = []
    active_queries = queries[:max_queries]

    logger.info(f"[PMC] 开始检索，共 {len(active_queries)} 条 query")

    def _run_isolated(query: str) -> tuple[str, list[PaperMeta], set[str]]:
        clean_query = _strip_arxiv_syntax(query)
        if not clean_query:
            return (query, [], set())
        local_papers: list[PaperMeta] = []
        local_seen: set[str] = set()
        _run_query(
            query=clean_query, limit=limit, min_year=min_year,
            seen_ids=local_seen, papers=local_papers,
        )
        return (clean_query, local_papers, local_seen)

    max_workers = min(len(active_queries), 4)
    if max_workers <= 1:
        for query in active_queries:
            q, new_papers, new_ids = _run_isolated(query)
            for p in new_papers:
                if p.arxiv_id not in seen_ids:
                    seen_ids.add(p.arxiv_id)
                    papers.append(p)
            seen_ids.update(new_ids)
            logger.info(f"[PMC] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")
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
                logger.info(f"[PMC] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")

    logger.info(f"[PMC] Europe PMC 检索完成，新增 {len(papers)} 篇（去重后）")
    return papers


def _run_query(
    query: str,
    limit: int,
    min_year: int,
    seen_ids: set[str],
    papers: list[PaperMeta],
) -> int:
    """执行单条 query，返回新增篇数。"""
    params: dict = {
        "query": query,
        "resultType": "core",
        "pageSize": min(limit, 100),
        "format": "json",
        "sort": "RELEVANCE",
    }

    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            resp = requests.get(
                _SEARCH_URL,
                params=params,
                headers=_HEADERS,
                timeout=(10, 15),  # (connect, read) fast-fail + retry
            )
            if resp.status_code == 429:
                wait = _RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"[PMC] HTTP 429，等待 {wait:.0f}s（指数退避）...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[PMC] 请求失败（第 {attempt} 次）: {e}")
            if attempt < _RETRY_COUNT:
                time.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            else:
                logger.error(f"[PMC] 放弃 query: {query[:60]}")
                return 0
    else:
        return 0

    n_added = 0
    for item in data.get("resultList", {}).get("result", []):
        # ── 提取 PMID 和 PMCID ─────────────────────────────────
        pmid = item.get("pmid") or ""
        pmcid = item.get("pmcid") or ""

        # ── 确定唯一 ID ──────────────────────────────────────
        if pmid:
            internal_key = f"pmid_{pmid}"
            clean_id = f"pmid_{pmid}"
        elif pmcid:
            internal_key = f"pmcid_{pmcid}"
            clean_id = f"pmcid_{pmcid}"
        else:
            continue   # 无 PMID 且无 PMCID，跳过

        if internal_key in seen_ids:
            continue

        # ── 摘要 ─────────────────────────────────────────────
        abstract = (item.get("abstractText") or "").strip()
        if not abstract:
            continue   # 无摘要的论文对 AGENT2 无价值

        seen_ids.add(internal_key)

        # ── 基本字段 ─────────────────────────────────────────
        title = (item.get("title") or "").replace("\n", " ").strip()
        if not title:
            continue

        author_string = item.get("authorString") or ""
        authors = [a.strip() for a in author_string.split(", ") if a.strip()]

        pub_year = item.get("pubYear") or 0
        if pub_year:
            published = f"{pub_year}-01-01"
        else:
            published = "unknown"

        journal_title = item.get("journalTitle") or ""

        papers.append(PaperMeta(
            arxiv_id=clean_id,
            title=title,
            authors=authors,
            published=published,
            abstract=abstract,
            pdf_url="",
            arxiv_url=f"https://europepmc.org/article/MED/{pmid}" if pmid else f"https://europepmc.org/article/PMC/{pmcid}",
            doi=pmid or pmcid,   # 将 PMID 或 PMCID 暂存在 doi 字段，供 paper_reader 提取
            source_platform="pmc",
            venue=journal_title,
        ))
        n_added += 1

    return n_added


def _strip_arxiv_syntax(query: str) -> str:
    """去除 arxiv 专有语法，保留核心词组用于 Europe PMC 自然语言检索。"""
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    q = q.replace('"', '')
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    q = ' '.join(q.split())
    return q
