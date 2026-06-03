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

import json
import logging
import re
import time
import xml.etree.ElementTree as ET

import requests

from agent1.arxiv_searcher import PaperMeta
from datetime import datetime

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
    active_queries = _expand_arxiv_queries(queries)[:max_queries]

    logger.info(f"[DBLP] 开始检索，共 {len(active_queries)} 条 query，每 query 上限 {limit}")

    for i, query in enumerate(active_queries):
        if i > 0:
            logger.info(f"[DBLP] 等待 {query_delay:.0f}s（防限流）...")
            time.sleep(query_delay)

        if not query:
            continue

        n_added = _run_query(
            query=query,
            limit=limit,
            min_year=min_year,
            seen_ids=seen_ids,
            papers=papers,
        )
        logger.info(f"[DBLP] {'[+]' if n_added > 0 else '[ ]'} {n_added:>3} 篇  |  {query[:80]}")

    logger.info(f"[DBLP] 检索完成，新增 {len(papers)} 篇（去重后）")
    _enrich_papers_from_dblp_records(papers)
    return papers


def search_dblp_with_conferences(
    queries: list[str],
    conferences: list[dict] | None = None,
    max_results_per_query: int = 100,
    min_year: int = 0,
    query_delay: float = 2.0,
    max_queries: int = 8,
    search_years: int = 3,
    search_all: bool = False,
) -> list[PaperMeta]:
    """
    会议感知的 DBLP 搜索：对每个 (keyword, conference) 组合生成查询，
    包括无年份宽匹配 + 逐年精确匹配，结果用 venue 字段后过滤。

    Args:
        queries:               检索查询串列表
        conferences:           会议配置列表 [{"name":"CCS","aliases":["ACM CCS"]}, ...]
                               None 或空列表时回退到全量搜索
        max_results_per_query: 每条 query 最多取回论文数（最大 1000）
        min_year:              过滤早于此年份的论文（0=不过滤）
        query_delay:           每条 query 之间的等待秒数
        max_queries:           最多执行的 query 数量（预算上限）
        search_years:          每个 (query, conference) 组合回溯的年数
        search_all:            是否同时执行无会议过滤的全量搜索

    Returns:
        去重后的 PaperMeta 列表
    """
    if not conferences:
        return search_dblp(
            queries=queries, max_results_per_query=max_results_per_query,
            min_year=min_year, query_delay=query_delay, max_queries=max_queries,
        )

    current_year = datetime.now().year
    years_to_search = list(range(current_year, max(min_year, current_year - search_years) - 1, -1))
    if not years_to_search:
        years_to_search = [current_year]

    # ── 校验会议配置 ──────────────────────────────────────────
    if conferences:
        validated = []
        for i, conf in enumerate(conferences):
            if not isinstance(conf, dict) or "name" not in conf:
                logger.error(
                    f"dblp conferences[{i}] 格式错误: 期望 {{name: str, aliases: [str]}}，"
                    f"实际: {json.dumps(conf, ensure_ascii=False) if isinstance(conf, dict) else repr(conf)}"
                )
                continue
            validated.append(conf)
        conferences = validated if validated else None

    # ── 展开 arXiv OR 语法为独立子查询 ──────────────────────
    expanded_queries = _expand_arxiv_queries(queries)
    if not expanded_queries:
        logger.warning("[DBLP] 所有 query 展开后为空，终止检索")
        return []

    logger.info(
        f"[DBLP] 原始 {len(queries)} 条 query → 展开为 {len(expanded_queries)} 条 DBLP 子查询"
    )

    # ── 构建查询计划 ──────────────────────────────────────────
    query_plan: list[tuple[str, dict | None]] = []

    for q in expanded_queries:
        if search_all:
            query_plan.append((q, None))  # 无 venue 过滤的全量查询
        if conferences:
            for conf in conferences:
                # 无年份宽匹配
                query_plan.append((f"{q} {conf['name']}", conf))
                # 逐年精确匹配
                for yr in years_to_search:
                    query_plan.append((f"{q} {conf['name']} {yr}", conf))

    # ── 预算裁剪 ──────────────────────────────────────────────
    if len(query_plan) > max_queries:
        query_plan = _trim_query_plan(query_plan, expanded_queries, conferences,
                                       years_to_search, max_queries)

    limit = min(max_results_per_query, 1000)
    seen_ids: set[str] = set()
    papers: list[PaperMeta] = []

    logger.info(
        f"[DBLP] 会议感知搜索启动: {len(queries)} query × {len(conferences)} 会议 "
        f"× {len(years_to_search)} 年 = {len(query_plan)} 次请求（上限 {max_queries}）"
    )

    for i, (q, venue_conf) in enumerate(query_plan):
        if i > 0:
            logger.info(f"[DBLP] 等待 {query_delay:.0f}s（防限流）...")
            time.sleep(query_delay)

        if not q:
            continue

        n_added = _run_query(
            query=q, limit=limit, min_year=min_year,
            seen_ids=seen_ids, papers=papers, venue_filter=venue_conf,
        )
        label = (venue_conf["name"][:30] if venue_conf else "ALL")
        logger.info(f"[DBLP] {'[+]' if n_added > 0 else '[ ]'} {n_added:>3} 篇 | {label} | {q[:50]}")

    logger.info(f"[DBLP] 会议感知搜索完成，新增 {len(papers)} 篇（去重后）")
    _enrich_papers_from_dblp_records(papers)
    return papers


def _trim_query_plan(
    plan: list[tuple[str, dict | None]],
    queries: list[str],
    conferences: list[dict],
    years: list[int],
    max_queries: int,
) -> list[tuple[str, dict | None]]:
    """裁剪查询计划至 max_queries 以内。策略：按优先级分层删除。"""
    # 分层：全量（venue_filter=None）→ 无年份 → 逐年（从旧到新可删）
    all_queries = [p for p in plan if p[1] is None]
    no_year = [p for p in plan if p[1] is not None and not any(str(y) in p[0][-4:] for y in years)]
    by_year_newest_first = sorted(
        [p for p in plan if p not in all_queries and p not in no_year],
        key=lambda p: _year_from_query(p[0]),
        reverse=True,
    )

    # 1. 优先保留全量 + 无年份
    result = all_queries + no_year
    if len(result) >= max_queries:
        return result[:max_queries]

    # 2. 逐年，再从旧到新填充
    for p in by_year_newest_first:
        if len(result) >= max_queries:
            break
        result.append(p)

    return result[:max_queries]


def _year_from_query(query: str) -> int:
    """从查询串中提取年份数字（若有）。"""
    import re as _re
    m = _re.search(r'(\d{4})', query)
    return int(m.group(1)) if m else 0


def _venue_matches(venue: str, conf: dict) -> bool:
    """检查 DBLP venue 字段是否匹配目标会议（大小写不敏感）。"""
    if not venue:
        return False
    venue_lower = venue.lower()
    candidates = [conf["name"].lower()] + [a.lower() for a in conf.get("aliases", [])]
    return any(cand in venue_lower for cand in candidates)


def _run_query(
    query: str,
    limit: int,
    min_year: int,
    seen_ids: set[str],
    papers: list[PaperMeta],
    venue_filter: dict | None = None,
) -> int:
    """执行单条 query，返回新增篇数。可选 venue_filter 对结果做会议匹配。"""
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
        dblp_key = (info.get("key") or "").strip()  # path-based: "conf/soups/NgongSNF24"
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
        venue_raw = info.get("venue") or ""
        venue = venue_raw if isinstance(venue_raw, str) else (
            ", ".join(venue_raw) if isinstance(venue_raw, list) else str(venue_raw)
        )
        venue = venue.strip()

        # ── 会议过滤 ──────────────────────────────────────────
        if venue_filter is not None and not _venue_matches(venue, venue_filter):
            continue

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
        # DBLP 搜索结果里 info.ee 是外部链接（可能是 OA 落地页或直接 PDF），
        # 由 _enrich_papers_from_dblp_records 解析真实 PDF 链接。
        ee_url = (info.get("ee") or "").strip()
        pdf_url = ee_url if ee_url else ""

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


def _enrich_papers_from_dblp_records(
    papers: list[PaperMeta],
    delay: float = 1.0,
) -> int:
    """对 DBLP 论文的下载链接进行补全。

    优先级：
      1. 如果 pdf_url 已是真实 PDF 链接（以 .pdf 结尾或含 /pdf/），直接使用
      2. 如果 pdf_url 是落地页 URL（来自 info.ee），抓取 citation_pdf_url
      3. 如果既没有 pdf_url 也没有 DOI，通过 XML 记录兜底获取

    Returns:
        成功补全的论文数
    """
    enriched = 0
    for p in papers:
        pdf_url = getattr(p, "pdf_url", "") or ""

        # 已经是真实 PDF 链接，无需处理
        if pdf_url and (pdf_url.lower().endswith(".pdf") or "/pdf/" in pdf_url.lower()):
            enriched += 1
            continue

        # 有落地页 URL，尝试解析为 PDF
        if pdf_url:
            try:
                if enriched > 0:
                    time.sleep(delay)
                resolved = _resolve_pdf_from_landing_page(pdf_url)
                if resolved:
                    p.pdf_url = resolved
                    logger.debug(
                        f"[DBLP] EE→PDF 解析成功: {p.title[:50]}..."
                    )
                # 即使解析失败也保留原 EE 链接，让下载器级联尝试
            except Exception as e:
                logger.warning(f"[DBLP] EE 解析异常 ({p.title[:30]}...): {e}")
            enriched += 1
            continue

        # 有 DOI，下载器级联会处理，无需额外操作
        if p.doi:
            enriched += 1
            continue

        # 既无 pdf_url 也无 DOI → XML 兜底
        dblp_key = _extract_dblp_key(p.arxiv_id)
        if not dblp_key:
            continue

        try:
            if enriched > 0:
                time.sleep(delay)
            xml_text = _fetch_dblp_record_xml(dblp_key)
            if not xml_text:
                continue

            doi, ee_urls = _parse_dblp_xml(xml_text)

            if doi:
                p.doi = doi
                clean_id = doi.replace("https://doi.org/", "").replace(
                    "http://doi.org/", ""
                )
                p.arxiv_id = f"doi_{clean_id}"

            if ee_urls and not getattr(p, "pdf_url", ""):
                for ee_url in ee_urls:
                    resolved = _resolve_pdf_from_landing_page(ee_url)
                    if resolved:
                        p.pdf_url = resolved
                        break
                else:
                    p.pdf_url = ee_urls[0]

            enriched += 1
            logger.debug(
                f"[DBLP] XML 兜底富化: {p.title[:50]}..."
            )
        except Exception as e:
            logger.warning(f"[DBLP] XML 兜底失败 ({dblp_key}): {e}")

    if enriched > 0:
        logger.info(f"[DBLP] 下载链接补全: {enriched}/{len(papers)} 篇")
    return enriched


def _extract_dblp_key(arxiv_id: str) -> str | None:
    """从内部 ID 反向提取 DBLK key。
    'dblp_conf_soups_NgongSNF24' → 'conf/soups/NgongSNF24'
    纯数字 key（旧格式）返回 None，需要完整记录才能解析。
    """
    if not arxiv_id.startswith("dblp_"):
        return None
    raw = arxiv_id[5:]  # 去掉 'dblp_'
    # 纯数字 key（如 "1209342"）无法用于 XML 接口，跳过
    if raw.isdigit():
        return None
    # DBLP key 格式: type/venue/paperId（三段，/ 分隔）
    # 内部存储: type_venue_paperId
    parts = raw.rsplit("_", 2)
    if len(parts) == 3:
        return "/".join(parts)
    # 可能只有两段（极少见），直接还原
    return raw.replace("_", "/")


def _fetch_dblp_record_xml(dblp_key: str, timeout: int = 15) -> str | None:
    """获取 DBLP 单条完整记录的 XML。"""
    url = f"https://dblp.org/rec/{dblp_key}.xml"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        if resp.status_code == 429:
            logger.warning(f"[DBLP] 富化 HTTP 429，放弃此条")
            return None
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as e:
        logger.warning(f"[DBLP] 获取记录失败 ({dblp_key}): {e}")
        return None


def _parse_dblp_xml(xml_text: str) -> tuple[str | None, list[str]]:
    """从 DBLP XML 记录中提取 DOI 和开放获取链接。

    Returns:
        (doi_or_none, list_of_ee_urls)
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning(f"[DBLP] XML 解析失败: {e}")
        return None, []

    # 提取 DOI
    doi = None
    doi_el = root.find(".//doi")
    if doi_el is not None and doi_el.text:
        doi = doi_el.text.strip()

    # 提取 ee 链接（优先 type="oa" 的开放获取链接）
    oa_urls: list[str] = []
    other_urls: list[str] = []
    for ee in root.findall(".//ee"):
        url = (ee.text or "").strip()
        if not url:
            continue
        if ee.get("type") == "oa":
            oa_urls.append(url)
        else:
            other_urls.append(url)

    return doi, oa_urls + other_urls


def _resolve_pdf_from_landing_page(ee_url: str, timeout: int = 15) -> str | None:
    """从出版商的 HTML 落地页中解析真实 PDF 链接。

    尝试以下策略（按优先级）：
      1. <meta name="citation_pdf_url" content="...">  — 学术出版界通用约定
      2. <a href="...pdf"> 中包含 "PDF" 关键词的链接
      3. 如果 URL 本身就以 .pdf 结尾，直接返回
    """
    # 直接就是 PDF 链接
    if ee_url.lower().endswith(".pdf") or "/pdf/" in ee_url.lower():
        return ee_url

    try:
        resp = requests.get(
            ee_url, headers=_HEADERS, timeout=timeout, allow_redirects=True,
        )
        if resp.status_code != 200:
            return None
        html = resp.text
    except requests.RequestException as e:
        logger.debug(f"[DBLP] 落地页请求失败: {e}")
        return None

    # 策略 1: <meta name="citation_pdf_url">
    m = re.search(
        r'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"',
        html, re.IGNORECASE,
    )
    if m:
        pdf_url = m.group(1)
        logger.debug(f"[DBLP] citation_pdf_url 命中: {pdf_url[:80]}")
        return pdf_url

    # 策略 2: href 中以 .pdf 结尾且链接文本含 "PDF"
    for link_m in re.finditer(
        r'<a[^>]+href="([^"]+\.pdf)"[^>]*>([^<]*)</a>',
        html, re.IGNORECASE,
    ):
        label = link_m.group(2).strip().lower()
        if "pdf" in label:
            return link_m.group(1)

    return None


def _expand_arxiv_queries(queries: list[str]) -> list[str]:
    """将 arXiv 语法的 OR 查询拆分为独立子查询，每个子句去语法后作为独立 DBLP query。
    例如 'all:"X" OR all:"Y"' → ['X', 'Y']
    去重并保持顺序。
    """
    expanded: list[str] = []
    seen: set[str] = set()
    for q in queries:
        # 按 OR 拆分为独立子句
        parts = q.split(" OR ")
        for part in parts:
            clean = _strip_arxiv_syntax(part)
            if clean and clean not in seen:
                expanded.append(clean)
                seen.add(clean)
    return expanded


def _strip_arxiv_syntax(query: str) -> str:
    """去除 arxiv 专有语法，保留自然语言核心词用于 DBLP 检索。"""
    q = re.sub(r'\b(all|ti|abs|au|cat):', '', query)
    q = q.replace('"', '')
    q = re.sub(r'\bAND\b|\bOR\b', ' ', q)
    q = ' '.join(q.split())
    return q
