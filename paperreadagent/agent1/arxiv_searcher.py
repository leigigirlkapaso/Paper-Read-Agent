"""
agent1/arxiv_searcher.py
调用 arxiv Python 库，根据关键词/查询串检索文献，返回候选论文列表。

返回结构：
  List[PaperMeta]，每项包含：
    arxiv_id, title, authors, published, abstract, pdf_url, arxiv_url

特性：
  - 多条 query 逐一检索，去重后合并
  - 若结果不足 min_results，自动从 keywords 生成 fallback query 补充
  - 每条 query 记录命中数，方便分析
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import arxiv

logger = logging.getLogger(__name__)


@dataclass
class PaperMeta:
    arxiv_id: str
    title: str
    authors: list[str]
    published: str              # ISO 格式日期字符串
    abstract: str
    pdf_url: str
    arxiv_url: str
    doi: str = ""                       # DOI 标识符（用于多源下载）
    relevance_score: float = 0.0    # 由 AGENT1-B 填充
    source_platform: str = ""       # arxiv / s2 / pwc / oa / local
    venue: str = ""                 # 发表场合
    code_url: str = ""              # 代码链接
    citation_count: int = 0         # 引用数


def search_papers(
    queries: list[str],
    max_results: int = 50,
    keywords: list[str] | None = None,
    min_results: int = 200,
    max_queries: int = 10,          # 最多执行的 query 数量，避免触发限流
    query_delay: float = 8.0,       # 每条 query 之间的等待秒数（防 429）
    rate_limit_backoff: float = 90.0, # 遇到 429 时额外等待秒数
    sort_by: str = "date",          # "date"=按发表时间降序 | "relevance"=按相关性
    min_year: int = 0,              # 过滤早于此年的论文（0=不过滤）
) -> list[PaperMeta]:
    """
    对多条 query 分别检索 arxiv，去重后返回候选论文列表。

    Args:
        queries:             检索查询串列表（来自 keyword_extractor）
        max_results:         每条 query 最多返回篇数（上限 300，走分页）
        keywords:            关键词列表，用于 fallback 兜底
        min_results:         期望的最小候选总量，不足时触发 fallback 扩展
        max_queries:         最多执行的 query 数量（防止查询过多触发限流）
        query_delay:         每条 query 之间的礼貌等待秒数
        rate_limit_backoff:  遇到 HTTP 429 后额外等待秒数再继续
        sort_by:             排序方式："date"=按发表时间降序（最新优先）| "relevance"=相关性
        min_year:            过滤早于此年份的论文（0=不过滤）

    Returns:
        去重后的 PaperMeta 列表
    """
    # 解析排序方式
    sort_criterion = (
        arxiv.SortCriterion.SubmittedDate
        if sort_by == "date"
        else arxiv.SortCriterion.Relevance
    )
    sort_label = "发表时间↓" if sort_by == "date" else "相关性↓"
    if min_year > 0:
        logger.info(f"[AGENT1] 排序方式: {sort_label}，年份过滤: >= {min_year}")
    else:
        logger.info(f"[AGENT1] 排序方式: {sort_label}，年份过滤: 无")

    # arxiv API 每页最多 100 条；>100 时 Client 自动分页（delay_seconds 间隔）
    per_query_max = min(max_results, 300)
    page_size = min(per_query_max, 100)

    seen_ids: set[str] = set()
    papers: list[PaperMeta] = []
    query_stats: list[tuple[str, int]] = []  # (query, hits)
    queries_run: int = 0             # 已执行 query 计数

    client = arxiv.Client(
        page_size=page_size,       # 每页 100 条，超出时自动翻页
        delay_seconds=5,           # 翻页间隔（分页请求需更礼貌）
        num_retries=1,             # 失败只重试1次，429 后由外层 backoff 处理
    )

    def _run(query: str) -> int:
        """执行单条 query，返回新增篇数；遇 429 返回 -1（调用方处理）。"""
        nonlocal queries_run
        logger.info(f"[AGENT1] arxiv 检索: {query!r}")
        n_added = 0
        search = arxiv.Search(
            query=query,
            max_results=per_query_max,
            sort_by=sort_criterion,
            sort_order=arxiv.SortOrder.Descending,
        )
        try:
            for result in client.results(search):
                arxiv_id = result.entry_id.split("/abs/")[-1]
                clean_id = arxiv_id.split("v")[0]   # 去掉版本号
                if clean_id in seen_ids:
                    continue
                # ── 年份过滤 ────────────────────────────────
                if min_year > 0 and result.published:
                    if result.published.year < min_year:
                        continue   # 跳过太旧的论文
                # ────────────────────────────────────────────
                seen_ids.add(clean_id)
                papers.append(
                    PaperMeta(
                        arxiv_id=clean_id,
                        title=result.title.replace("\n", " ").strip(),
                        authors=[a.name for a in result.authors],
                        published=result.published.strftime("%Y-%m-%d")
                        if result.published
                        else "unknown",
                        abstract=result.summary.replace("\n", " ").strip(),
                        pdf_url=result.pdf_url or f"https://arxiv.org/pdf/{clean_id}",
                        arxiv_url=f"https://arxiv.org/abs/{clean_id}",
                        doi=getattr(result, "doi", "") or "",
                    )
                )
                n_added += 1
            query_stats.append((query, n_added))
            queries_run += 1
            return n_added
        except Exception as e:
            err_str = str(e)
            if "429" in err_str:
                logger.warning(
                    f"[AGENT1] HTTP 429 限流，等待 {rate_limit_backoff:.0f}s 后继续..."
                )
                time.sleep(rate_limit_backoff)
                query_stats.append((query, 0))
                return -1   # 跳过本条，继续后续 query
            else:
                logger.error(f"[AGENT1] 检索失败 ({query!r}): {e}")
                query_stats.append((query, 0))
                return 0

    def _run_with_delay(query: str, first: bool = False) -> int:
        """执行带前置等待的单条 query。"""
        if not first:
            logger.info(f"[AGENT1] 等待 {query_delay:.0f}s（防限流）...")
            time.sleep(query_delay)
        return _run(query)

    # ── 第一阶段：执行 LLM 生成的 query（最多 max_queries 条）─
    active_queries = queries[:max_queries]
    if len(queries) > max_queries:
        logger.info(
            f"[AGENT1] 共 {len(queries)} 条 query，限流保护截取前 {max_queries} 条"
        )
    for i, query in enumerate(active_queries):
        _run_with_delay(query, first=(i == 0))

    # ── 第二阶段：兜底扩展（结果不足 + 还有 query 配额时）────
    if len(papers) < min_results and keywords and queries_run < max_queries:
        fallback_queries = _build_fallback_queries(keywords)
        remaining_slots = max_queries - queries_run
        fallback_queries = fallback_queries[:remaining_slots]
        logger.info(
            f"[AGENT1] 候选仅 {len(papers)} 篇 < {min_results}，"
            f"启动兜底扩展：{len(fallback_queries)} 条 fallback query"
        )
        for query in fallback_queries:
            _run_with_delay(query)

    # ── 汇总统计 ────────────────────────────────────────────
    _log_query_stats(query_stats)

    logger.info(
        f"[AGENT1] arxiv 检索完成，去重后共 {len(papers)} 篇候选文献"
    )
    return papers


# ──────────────────────────────────────────────────────────


def _build_fallback_queries(keywords: list[str]) -> list[str]:
    """
    从关键词自动生成一批简单的 fallback 查询串。
    策略：
      1. 前 5 个关键词各自用 all:"kw" 单独检索
      2. 取前 5 个关键词两两组合 all:"kw1" AND all:"kw2"
    去重后返回。
    """
    fallback: list[str] = []
    seen = set()

    # 单关键词
    for kw in keywords[:5]:
        q = f'all:"{kw}"'
        if q not in seen:
            seen.add(q)
            fallback.append(q)

    # 双词组合（取前 4 个，避免太多）
    for i in range(min(4, len(keywords))):
        for j in range(i + 1, min(4, len(keywords))):
            q = f'all:"{keywords[i]}" AND all:"{keywords[j]}"'
            if q not in seen:
                seen.add(q)
                fallback.append(q)

    return fallback


def _log_query_stats(stats: list[tuple[str, int]]) -> None:
    """打印每条 query 的命中统计，方便诊断。"""
    if not stats:
        return
    logger.info("[AGENT1] 查询命中统计：")
    for query, hits in stats:
        mark = "[+] " if hits > 0 else "[ ] "
        logger.info(f"  {mark}{hits:>3} 篇  |  {query[:100]}")
