"""
agent1/pwc_searcher.py
调用 Papers With Code 官方 API 检索机器学习论文。

覆盖范围：所有在 paperswithcode.com 收录的 ML/AI 论文，含代码链接。
API 文档：https://paperswithcode.com/api/v1/docs/

返回结构：
  List[PaperMeta]，与 arxiv_searcher 共用同一数据结构。
  - PwC 论文绝大多数有 arxiv 版本，arxiv_id 取 arxiv 编号
  - 无 arxiv 版本的论文取 'pwc_{id[:12]}' 作为内部唯一标识
  - pdf_url 优先使用 arxiv PDF，其次使用 url_pdf（若有）

特性：
  - 按相关性排序（PwC 默认），每条 query 最多取 500 篇
  - 支持年份下限过滤（在取回结果后本地过滤）
  - 额外返回 GitHub 代码链接（存于 abstract 末尾，格式：[Code: <url>]）
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
import time

import requests

from agent1.arxiv_searcher import PaperMeta

logger = logging.getLogger(__name__)

_BASE_URL = "https://paperswithcode.com/api/v1"
_HEADERS = {
    "User-Agent": "PaperReadAgent/1.0 (research tool)",
}
_RETRY_429_MAX = 3
_RATE_LIMIT_BASE_DELAY = 15.0


def search_pwc(
    queries: list[str],
    max_results_per_query: int = 100,
    min_year: int = 0,
    query_delay: float = 2.0,
    max_queries: int = 8,
) -> list[PaperMeta]:
    """
    用多条 query 分别检索 Papers With Code，去重后返回候选论文列表。

    Args:
        queries:               检索查询串列表
        max_results_per_query: 每条 query 最多取回的论文数（PwC 分页，每页 50 条）
        min_year:              过滤早于此年份的论文（0=不过滤）
        query_delay:           每条 query 之间的等待秒数
        max_queries:           最多执行的 query 数量

    Returns:
        去重后的 PaperMeta 列表
    """
    seen_ids: set[str] = set()
    papers: list[PaperMeta] = []
    active_queries = queries[:max_queries]

    logger.info(f"[PwC] 开始检索，共 {len(active_queries)} 条 query")

    def _run_isolated(query: str) -> tuple[str, list[PaperMeta], set[str]]:
        clean_query = _strip_arxiv_syntax(query)
        if not clean_query:
            return (query, [], set())
        local_papers: list[PaperMeta] = []
        local_seen: set[str] = set()
        _run_query(
            query=clean_query, limit=max_results_per_query, min_year=min_year,
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
            logger.info(f"[PwC] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")
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
                logger.info(f"[PwC] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")

    logger.info(f"[PwC] Papers With Code 检索完成，新增 {len(papers)} 篇（去重后）")
    return papers


def _run_query(
    query: str,
    limit: int,
    min_year: int,
    seen_ids: set[str],
    papers: list[PaperMeta],
) -> int:
    """执行单条 query（支持分页），返回新增篇数。"""
    n_added = 0
    page = 1
    per_page = 50
    collected = 0
    retry_429 = 0

    while collected < limit:
        try:
            resp = requests.get(
                f"{_BASE_URL}/papers/",
                params={
                    "q": query,
                    "page": page,
                    "items_per_page": min(per_page, limit - collected),
                    "ordering": "-arxiv_id",
                },
                headers=_HEADERS,
                timeout=(10, 15),  # (connect, read) fast-fail + retry
            )
            if resp.status_code == 429:
                if retry_429 >= _RETRY_429_MAX:
                    logger.error("[PwC] HTTP 429 重试耗尽，放弃此查询")
                    break
                retry_429 += 1
                wait = _RATE_LIMIT_BASE_DELAY * (2 ** (retry_429 - 1))
                logger.warning(f"[PwC] HTTP 429，等待 {wait:.0f}s（指数退避）...（{retry_429}/{_RETRY_429_MAX}）")
                time.sleep(wait)
                continue
            if resp.status_code == 404:
                break
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[PwC] 请求失败: {e}")
            break

        results = data.get("results", [])
        if not results:
            break

        for item in results:
            paper_id = item.get("id", "")
            arxiv_raw = (item.get("arxiv_id") or "").strip()

            if arxiv_raw:
                clean_id = arxiv_raw.split("v")[0]
                internal_key = f"arxiv_{clean_id}"
            else:
                clean_id = f"pwc_{paper_id[:12]}" if paper_id else ""
                if not clean_id:
                    continue
                internal_key = f"pwc_{paper_id}"

            if internal_key in seen_ids:
                continue

            # 年份过滤
            pub_date = item.get("published") or ""
            year = _parse_year(pub_date)
            if min_year > 0 and year > 0 and year < min_year:
                continue

            seen_ids.add(internal_key)

            # PDF URL
            if arxiv_raw:
                pdf_url = f"https://arxiv.org/pdf/{clean_id}"
            else:
                pdf_url = item.get("url_pdf") or ""

            # 代码链接附在摘要末尾（方便 AGENT1-B 看到）
            abstract = (item.get("abstract") or "").replace("\n", " ").strip()
            repo = item.get("url_code") or ""
            if repo:
                abstract += f"  [Code: {repo}]"

            paper_url = (
                f"https://arxiv.org/abs/{clean_id}"
                if arxiv_raw
                else (item.get("url") or f"https://paperswithcode.com/paper/{paper_id}")
            )

            papers.append(PaperMeta(
                arxiv_id=clean_id,
                title=(item.get("title") or "").replace("\n", " ").strip(),
                authors=["Unknown"],  # PwC /papers/ 列表不含作者，需 /papers/{id}/ 二次查询
                published=pub_date[:10] if len(pub_date) >= 10 else (str(year) if year else "unknown"),
                abstract=abstract,
                pdf_url=pdf_url,
                arxiv_url=paper_url,
                code_url=repo,
            ))
            n_added += 1
            collected += 1

        # 翻页
        if not data.get("next") or collected >= limit:
            break
        page += 1
        time.sleep(0.5)   # 分页间短暂等待

    return n_added


def _strip_arxiv_syntax(query: str) -> str:
    """
    去除 arxiv 专有语法（all:, ti:, abs:, AND, OR 引号等），
    保留核心词组用于 PwC 自然语言检索。
    """
    # 去掉 field: 前缀
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    # 去掉引号
    q = q.replace('"', '')
    # AND / OR 替换为空格
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    # 压缩空白
    q = ' '.join(q.split())
    return q


def _parse_year(date_str: str) -> int:
    """从 'YYYY-MM-DD' 格式字符串中提取年份，失败返回 0。"""
    if date_str and len(date_str) >= 4:
        try:
            return int(date_str[:4])
        except ValueError:
            pass
    return 0
