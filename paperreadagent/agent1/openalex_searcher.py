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

import concurrent.futures
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
_RETRY_COUNT = 3
_RETRY_BASE_DELAY = 5.0
_RATE_LIMIT_BASE_DELAY = 10.0


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
            logger.info(f"[OA] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")
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
                logger.info(f"[OA] {'[+]' if new_papers else '[ ]'} {len(new_papers):>3} 篇  |  {q[:80]}")

    logger.info(f"[OA] OpenAlex 检索完成，新增 {len(papers)} 篇（去重后）")
    return papers


def work_to_paper(item: dict) -> PaperMeta | None:
    """Parse one OpenAlex /works result into a PaperMeta. Returns None if it has
    no usable abstract or title (same drop rules as the search path)."""
    oa_id = item.get("id", "")
    if not oa_id:
        return None
    ext_ids = item.get("ids") or {}
    arxiv_raw = ext_ids.get("arxiv", "")
    doi_raw = ext_ids.get("doi", "")
    doi = doi_raw.replace("https://doi.org/", "") if doi_raw.startswith("https://doi.org/") else doi_raw
    if arxiv_raw:
        arxiv_raw = arxiv_raw.replace("https://arxiv.org/abs/", "").strip()
        clean_id = arxiv_raw.split("v")[0]
    else:
        clean_id = f"oa_{oa_id.split('/')[-1]}"

    abstract = _rebuild_abstract(item.get("abstract_inverted_index"))
    if not abstract:
        return None
    title = (item.get("title") or "").replace("\n", " ").strip()
    if not title:
        return None

    if arxiv_raw:
        pdf_url = f"https://arxiv.org/pdf/{clean_id}"
    else:
        pdf_url = (item.get("open_access") or {}).get("oa_url") or ""
        if not pdf_url:
            pdf_url = (item.get("best_oa_location") or {}).get("pdf_url") or ""

    authors = [a.get("author", {}).get("display_name", "")
               for a in (item.get("authorships") or [])]
    authors = [a for a in authors if a]
    pub_date = item.get("publication_date") or ""
    published = pub_date[:10] if len(pub_date) >= 10 else "unknown"
    paper_url = f"https://arxiv.org/abs/{clean_id}" if arxiv_raw else oa_id

    return PaperMeta(
        arxiv_id=clean_id, title=title, authors=authors, published=published,
        abstract=abstract, pdf_url=pdf_url, arxiv_url=paper_url, doi=doi,
        citation_count=int(item.get("cited_by_count") or 0),
    )


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
                logger.warning(f"[OA] HTTP 429，等待 {wait:.0f}s（指数退避）...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[OA] 请求失败（第 {attempt} 次）: {e}")
            if attempt < _RETRY_COUNT:
                time.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
            else:
                logger.error(f"[OA] 放弃 query: {query[:60]}")
                return 0
    else:
        return 0

    n_added = 0
    for item in data.get("results", []):
        p = work_to_paper(item)
        if p is None:
            continue
        internal_key = p.arxiv_id
        if internal_key in seen_ids:
            continue
        seen_ids.add(internal_key)
        papers.append(p)
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
        logger.warning("[OpenAlex] _rebuild_abstract failed, returning empty", exc_info=True)
        return ""


def _strip_arxiv_syntax(query: str) -> str:
    """去除 arxiv 专有语法，保留核心词组用于 OpenAlex 自然语言检索。"""
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    q = q.replace('"', '')
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    q = ' '.join(q.split())
    return q


_GRAPH_SELECT = ("id,title,abstract_inverted_index,authorships,publication_date,"
                 "open_access,best_oa_location,ids,referenced_works,cited_by_count")


def _oa_get(url: str, params: dict | None = None) -> dict | None:
    """Single GET with polite headers + exponential-backoff retry. Returns parsed JSON or None."""
    params = dict(params or {})
    params.setdefault("mailto", _POLITE_EMAIL)
    for attempt in range(1, _RETRY_COUNT + 1):
        try:
            resp = requests.get(url, params=params, headers=_HEADERS, timeout=(10, 15))
            if resp.status_code == 429:
                time.sleep(_RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1)))
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning(f"[OA-graph] request failed (try {attempt}): {e}")
            if attempt < _RETRY_COUNT:
                time.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))
    return None


def resolve_work(paper: PaperMeta) -> dict | None:
    """Resolve a PaperMeta to its full OpenAlex Work JSON (incl. referenced_works),
    via oa_id -> doi -> arxiv-doi. Returns None if unresolvable."""
    from utils.paper_dedup import extract_identifiers
    ids = extract_identifiers(paper)
    entity = None
    if ids.get("oa_id"):
        entity = ids["oa_id"].upper()            # w123 -> W123
    elif ids.get("doi"):
        entity = f"doi:{ids['doi']}"
    elif ids.get("arxiv_id"):
        entity = f"doi:10.48550/arXiv.{ids['arxiv_id']}"
    if not entity:
        return None
    return _oa_get(f"{_SEARCH_URL}/{entity}", {"select": _GRAPH_SELECT})


def fetch_referenced_works(work: dict, limit: int = 25) -> list[PaperMeta]:
    """Backward: details of the works this seed cites (capped to `limit`)."""
    limit = min(limit, 50)  # OpenAlex per-page hard max
    refs = (work.get("referenced_works") or [])[:limit]
    if not refs:
        return []
    short_ids = [r.split("/")[-1] for r in refs]
    data = _oa_get(_SEARCH_URL, {
        "filter": "openalex_id:" + "|".join(short_ids),
        "per-page": min(limit, 50),
        "select": _GRAPH_SELECT,
    })
    if not data:
        return []
    out = [work_to_paper(it) for it in data.get("results", [])]
    return [p for p in out if p is not None]


def fetch_citing_works(work_id: str, limit: int = 25) -> list[PaperMeta]:
    """Forward: the most-cited works that cite this seed (capped to `limit`)."""
    limit = min(limit, 50)  # OpenAlex per-page hard max
    short_id = work_id.split("/")[-1]
    data = _oa_get(_SEARCH_URL, {
        "filter": f"cites:{short_id}",
        "sort": "cited_by_count:desc",
        "per-page": min(limit, 50),
        "select": _GRAPH_SELECT,
    })
    if not data:
        return []
    out = [work_to_paper(it) for it in data.get("results", [])]
    return [p for p in out if p is not None]
