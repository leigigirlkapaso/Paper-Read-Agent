"""
agent1/openalex_searcher.py
调用 OpenAlex 免费开放 API 检索学术文献。

覆盖范围：2 亿+ 学术论文，跨所有学科，无需 API Key。
API 文档：https://docs.openalex.org/

返回结构：
  List[PaperMeta]，与 arxiv_searcher 共用同一数据结构。
  - 若论文有 arxiv 版本，arxiv_id 取 arxiv 编号
  - 否则 arxiv_id 取 'oa_{openalex_id_short}' 作为内部标识
  - pdf_url 优先使用 open_access.oa_url（免费可下载的链接）

特性：
  - 按被引数+时间综合排序，适合发现高影响力论文
  - 支持年份下限过滤（通过 API filter 参数直接过滤，效率高）
  - 自动提供邮箱给 OpenAlex（获得更高速率限制）
  - 仅保留有摘要的论文（无摘要的论文对 AGENT2 无价值）
"""

from __future__ import annotations

import logging
import re
import time

import requests

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://api.openalex.org/works"
# 提供邮件可以获取更高的速率限制（polite pool：每秒 10 次，否则 10 次/秒共享）
_POLITE_EMAIL = "paperreadagent@example.com"
_HEADERS = {"User-Agent": f"PaperReadAgent/1.0 (mailto:{_POLITE_EMAIL})"}


def search_openalex(
    queries: list[str],
    max_results_per_query: int = 100,
    min_year: int = 0,
    query_delay: float = 2.0,
    max_queries: int = 8,
) -> list[PaperMeta]:
    """
    用多条 query 分别检索 OpenAlex，去重后返回候选论文列表。

    Args:
        queries:               检索查询串列表
        max_results_per_query: 每条 query 最多取回的论文数（最大 200）
        min_year:              过滤早于此年份的论文（0=不过滤）
        query_delay:           每条 query 之间的等待秒数
        max_queries:           最多执行的 query 数量

    Returns:
        去重后的 PaperMeta 列表
    """
    limit = min(max_results_per_query, 200)
    seen_ids: set[str] = set()
    papers: list[PaperMeta] = []
    active_queries = queries[:max_queries]

    logger.info(f"[OA] 开始检索，共 {len(active_queries)} 条 query")

    for i, query in enumerate(active_queries):
        if i > 0:
            logger.info(f"[OA] 等待 {query_delay:.0f}s（防限流）...")
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
        logger.info(f"[OA] {'[+]' if n_added > 0 else '[ ]'} {n_added:>3} 篇  |  {clean_query[:80]}")

    logger.info(f"[OA] OpenAlex 检索完成，新增 {len(papers)} 篇（去重后）")
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
        "search": query,
        "per-page": min(limit, 200),
        "mailto": _POLITE_EMAIL,
        "select": "id,title,abstract_inverted_index,authorships,publication_date,"
                  "open_access,primary_location,best_oa_location,ids",
        "sort": "relevance_score:desc",   # 相关性优先
    }
    if min_year > 0:
        params["filter"] = f"publication_year:>{min_year - 1}"

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
                logger.warning(f"[OA] HTTP 429，等待 {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[OA] 请求失败（第 {attempt} 次）: {e}")
            if attempt < 3:
                time.sleep(5 * attempt)
            else:
                logger.error(f"[OA] 放弃 query: {query[:60]}")
                return 0
    else:
        return 0

    n_added = 0
    for item in data.get("results", []):
        oa_id = item.get("id", "")   # https://openalex.org/W123456
        if not oa_id:
            continue

        # ── 确定唯一 ID ──────────────────────────────────────
        ext_ids = item.get("ids") or {}
        arxiv_raw = ext_ids.get("arxiv", "")
        doi_raw = ext_ids.get("doi", "")          # OpenAlex 返回 https://doi.org/10.xxx
        doi = doi_raw.replace("https://doi.org/", "") if doi_raw.startswith("https://doi.org/") else doi_raw
        if arxiv_raw:
            # arxiv_raw 格式：https://arxiv.org/abs/2301.07041
            arxiv_raw = arxiv_raw.replace("https://arxiv.org/abs/", "").strip()
            clean_id = arxiv_raw.split("v")[0]
            internal_key = f"arxiv_{clean_id}"
        else:
            short_id = oa_id.split("/")[-1]   # e.g. "W2963403868"
            clean_id = f"oa_{short_id}"
            internal_key = f"oa_{short_id}"

        if internal_key in seen_ids:
            continue

        # ── 摘要重建（OpenAlex 使用倒排索引格式）────────────
        abstract = _rebuild_abstract(item.get("abstract_inverted_index"))
        if not abstract:
            continue   # 无摘要的论文对 AGENT2 无参考价值

        seen_ids.add(internal_key)

        # ── PDF URL（仅开放获取）──────────────────────────────
        if arxiv_raw:
            pdf_url = f"https://arxiv.org/pdf/{clean_id}"
        else:
            oa_info = item.get("open_access") or {}
            pdf_url = oa_info.get("oa_url") or ""
            # best_oa_location 有时候有更直接的 PDF 链接
            if not pdf_url:
                best_oa = item.get("best_oa_location") or {}
                pdf_url = best_oa.get("pdf_url") or ""

        # ── 基本字段 ─────────────────────────────────────────
        title = (item.get("title") or "").replace("\n", " ").strip()
        if not title:
            continue

        authors = []
        for a in (item.get("authorships") or []):
            author = a.get("author") or {}
            name = author.get("display_name", "")
            if name:
                authors.append(name)

        pub_date = item.get("publication_date") or ""
        published = pub_date[:10] if len(pub_date) >= 10 else "unknown"

        paper_url = (
            f"https://arxiv.org/abs/{clean_id}"
            if arxiv_raw
            else oa_id
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
        ))
        n_added += 1

    return n_added


def _rebuild_abstract(inverted_index: dict | None) -> str:
    """
    将 OpenAlex 的倒排索引摘要重建为正常文本。
    格式：{"word": [pos1, pos2, ...], ...}
    """
    if not inverted_index:
        return ""
    try:
        # 将 {word: [positions]} 转换为位置列表，排序后拼接
        pos_word: dict[int, str] = {}
        for word, positions in inverted_index.items():
            for pos in positions:
                pos_word[pos] = word
        tokens = [pos_word[p] for p in sorted(pos_word)]
        return " ".join(tokens)
    except Exception:
        return ""


def _strip_arxiv_syntax(query: str) -> str:
    """去除 arxiv 专有语法，保留核心词组用于 OpenAlex 自然语言检索。"""
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    q = q.replace('"', '')
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    q = ' '.join(q.split())
    return q
