"""
utils/multi_downloader.py
多源 PDF 下载器，按优先级级联尝试：
  1. arXiv PDF URL    — 有 arxiv_id 时优先
  2. 直接 URL          — 搜索结果中的开放获取链接
  3. Unpaywall API    — 通过 DOI 查找 OA 版本
  4. Semantic Scholar — Graph API DOI 查找 openAccessPdf
  5. Sci-Hub 镜像     — 最后兜底（默认关闭，需配置开启）

参考 paper-fetch skill 的分辨顺序设计。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

from utils.arxiv_downloader import _is_arxiv_id

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; PaperReadAgent/1.0; "
        "+https://github.com/yourname/PaperReadAgent)"
    ),
    "Accept": "application/pdf, text/html;q=0.9, */*;q=0.5",
}

UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}"
S2_GRAPH_API = "https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
EPRINT_SEARCH = "https://eprint.iacr.org/search"

DEFAULT_SCIHUB_MIRRORS = [
    "https://sci-hub.se",
    "https://sci-hub.st",
    "https://sci-hub.ru",
]


# ═══════════════════════════════════════════════════════════════════
# Public sync API（兼容现有 download_papers_batch 调用方式）
# ═══════════════════════════════════════════════════════════════════

def download_papers_batch_multi(
    papers: list,        # list[PaperMeta] — 避免循环导入，不强制类型
    output_dir: str | Path,
    unpaywall_email: str = "",
    enable_scihub: bool = False,
    enable_institutional: bool = False,
    scihub_mirrors: list[str] | None = None,
    max_concurrent: int = 5,
) -> dict[str, Path | None]:
    """
    同步批量多源下载。

    Args:
        papers:           PaperMeta 列表（需含 arxiv_id / doi / pdf_url）
        output_dir:       PDF 保存目录
        unpaywall_email:  Unpaywall API 邮箱（留空跳过）
        enable_scihub:    是否启用 Sci-Hub 兜底
        scihub_mirrors:   Sci-Hub 镜像列表（None 用默认）
        max_concurrent:   最大并发数

    Returns:
        {arxiv_id: Path or None}
    """
    output_dir = Path(output_dir)
    return asyncio.run(_download_batch_async(
        papers=papers,
        output_dir=output_dir,
        unpaywall_email=unpaywall_email,
        enable_scihub=enable_scihub,
        enable_institutional=enable_institutional,
        scihub_mirrors=scihub_mirrors,
        max_concurrent=max_concurrent,
    ))


# ═══════════════════════════════════════════════════════════════════
# Async internals
# ═══════════════════════════════════════════════════════════════════

async def _download_batch_async(
    papers: list,
    output_dir: Path,
    unpaywall_email: str,
    enable_scihub: bool,
    enable_institutional: bool,
    scihub_mirrors: list[str] | None,
    max_concurrent: int,
) -> dict[str, Path | None]:
    """异步批量下载，带并发控制。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(max_concurrent)

    async with aiohttp.ClientSession() as session:
        tasks = [
            _download_one_async(
                paper=p,
                output_dir=output_dir,
                session=session,
                sem=sem,
                unpaywall_email=unpaywall_email,
                enable_scihub=enable_scihub,
                enable_institutional=enable_institutional,
                scihub_mirrors=scihub_mirrors,
            )
            for p in papers
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: dict[str, Path | None] = {}
    for paper, result in zip(papers, results):
        aid = getattr(paper, "arxiv_id", str(id(paper)))
        if isinstance(result, Exception):
            logger.error(f"[MultiDL] 异常 ({aid}): {result}")
            out[aid] = None
        else:
            out[aid] = result
    return out


async def _download_one_async(
    paper,
    output_dir: Path,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    unpaywall_email: str,
    enable_scihub: bool,
    enable_institutional: bool,
    scihub_mirrors: list[str] | None,
) -> Path | None:
    """单篇论文多源级联下载。"""
    aid = getattr(paper, "arxiv_id", "")
    clean_id = aid.strip().split("v")[0] if aid else ""
    filename = f"{clean_id.replace('/', '_')}.pdf" if clean_id else "_unknown.pdf"
    dest = output_dir / filename

    # 断点续传
    if dest.exists() and dest.stat().st_size > 1024:
        logger.debug(f"[MultiDL] 已存在: {filename}")
        return dest

    # 收集 DOI / direct_url
    doi = getattr(paper, "doi", "") or ""
    direct_url = getattr(paper, "pdf_url", "") or ""

    # 构建级联
    cascade = _build_cascade(clean_id, direct_url, doi, unpaywall_email, enable_scihub,
                              enable_institutional, scihub_mirrors)

    # 动态解析 Unpaywall / S2
    resolved_urls: dict[str, str] = {}
    if doi:
        if unpaywall_email:
            up_url = await _resolve_unpaywall(doi, unpaywall_email, session, sem)
            if up_url:
                resolved_urls["unpaywall"] = up_url
        s2_url, arxiv_id = await _resolve_s2_oa(doi, session, sem)
        if s2_url:
            resolved_urls["s2_oa"] = s2_url
        if arxiv_id:
            resolved_urls["arxiv_via_s2"] = f"https://arxiv.org/pdf/{arxiv_id}"

    # IACR ePrint（按标题搜索，不依赖 DOI）
    title = getattr(paper, "title", "") or ""
    if title:
        authors = getattr(paper, "authors", []) or []
        eprint_url = await _resolve_eprint(title, authors, session, sem)
        if eprint_url:
            resolved_urls["eprint"] = eprint_url

    for source, url in cascade:
        actual_url = resolved_urls.get(source, url)
        if not actual_url:
            continue
        logger.info(f"[MultiDL] 尝试 {source}: {actual_url[:80]}")
        ok = await _download_from_url(session, sem, actual_url, dest)
        if ok:
            size_kb = dest.stat().st_size // 1024
            logger.info(f"[MultiDL] 成功 ({source}): {filename} ({size_kb} KB)")
            return dest
        logger.debug(f"[MultiDL] {source} 失败，继续下一个来源")

    logger.warning(f"[MultiDL] 所有来源均失败: {clean_id}")
    if dest.exists():
        dest.unlink()
    return None


def _build_cascade(
    clean_id: str,
    direct_url: str,
    doi: str,
    unpaywall_email: str,
    enable_scihub: bool,
    enable_institutional: bool,
    scihub_mirrors: list[str] | None,
) -> list[tuple[str, str]]:
    """构建 (source_name, url) 优先级列表（不含动态解析的 URL）。"""
    cascade: list[tuple[str, str]] = []

    # 1. arXiv
    if _is_arxiv_id(clean_id):
        cascade.append(("arxiv", f"https://arxiv.org/pdf/{clean_id}"))

    # 2. Direct URL
    if direct_url:
        cascade.append(("direct", direct_url))

    # 2.5. Publisher direct（机构 VPN 环境下通过 DOI 直接访问 IEEE/ACM 等）
    if doi and enable_institutional:
        cascade.append(("publisher", f"https://doi.org/{doi}"))

    # 3. Unpaywall（动态解析，占位）
    if doi and unpaywall_email:
        cascade.append(("unpaywall", ""))

    # 4. S2 OA（动态解析，占位）
    if doi:
        cascade.append(("s2_oa", ""))

    # 4.5. arXiv via S2（动态解析，占位 — S2 发现的 arXiv 预印本）
    #       当 S2 OA 指向付费墙（如 ACM Cloudflare）时的免费回退路径
    if doi:
        cascade.append(("arxiv_via_s2", ""))

    # 4.6. IACR ePrint（动态解析，占位 — 密码学/安全预印本）
    #       PDF 免费直链，CCS/S&P/USENIX Security 论文大量首发
    cascade.append(("eprint", ""))

    # 5. Sci-Hub
    if doi and enable_scihub:
        mirrors = scihub_mirrors or DEFAULT_SCIHUB_MIRRORS
        for mirror in mirrors[:3]:
            cascade.append(("scihub", f"{mirror.rstrip('/')}/{doi}"))

    return cascade


# ═══════════════════════════════════════════════════════════════════
# API 解析
# ═══════════════════════════════════════════════════════════════════

async def _resolve_unpaywall(
    doi: str,
    email: str,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
) -> str | None:
    """查询 Unpaywall，返回 best_oa_location.url_for_pdf。"""
    url = UNPAYWALL_API.format(doi=doi) + f"?email={email}"
    async with sem:
        try:
            async with session.get(url, headers=_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                best = data.get("best_oa_location") or {}
                pdf_url = best.get("url_for_pdf") or ""
                if pdf_url:
                    logger.info(f"[MultiDL] Unpaywall → {pdf_url[:80]}")
                return pdf_url
        except Exception as e:
            logger.debug(f"[MultiDL] Unpaywall 查询失败: {e}")
            return None


async def _resolve_s2_oa(
    doi: str,
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
) -> tuple[str | None, str | None]:
    """查询 Semantic Scholar Graph API，返回 (openAccessPdf.url, ArXiv ID)。

    同时提取 externalIds.ArXiv，用于绕过付费墙（如 ACM Cloudflare 阻断
    dl.acm.org 时回退到免费 arXiv 预印本）。
    """
    url = S2_GRAPH_API.format(doi=doi)
    params = {"fields": "openAccessPdf,externalIds"}
    async with sem:
        try:
            async with session.get(url, params=params, headers=_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None, None
                data = await resp.json()
                oap = data.get("openAccessPdf") or {}
                pdf_url = oap.get("url") or ""
                if pdf_url:
                    logger.info(f"[MultiDL] S2 OA → {pdf_url[:80]}")
                # 提取 arXiv ID 作为备用下载源
                ext_ids = data.get("externalIds") or {}
                arxiv_id = (ext_ids.get("ArXiv") or "").strip() or None
                if arxiv_id:
                    logger.info(f"[MultiDL] S2 arXiv → {arxiv_id}")
                return pdf_url, arxiv_id
        except Exception as e:
            logger.debug(f"[MultiDL] S2 OA 查询失败: {e}")
            return None, None


async def _resolve_eprint(
    title: str,
    authors: list[str],
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
) -> str | None:
    """在 IACR ePrint 上按标题搜索论文，返回 PDF 直链。

    IACR ePrint 是密码学/安全领域的核心预印本平台，CCS、S&P、
    USENIX Security 的论文大量首发于此。PDF 免费直链无需认证。

    搜索策略：用标题前 8 个词搜索，解析 HTML 结果页，标题包含匹配则返回 PDF。
    """
    # 取标题前若干关键词作为搜索词（太短查不准，太长查不到）
    keywords = " ".join(title.split()[:8])
    params = {"q": keywords, "search": "Search"}

    async with sem:
        try:
            async with session.get(
                EPRINT_SEARCH, params=params, headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()
        except Exception as e:
            logger.debug(f"[MultiDL] ePrint 搜索失败: {e}")
            return None

    # 解析 HTML：每个结果的结构为
    #   <a class="paperlink" href="/{id}">{id}</a>
    #   <a href="/{id}.pdf">(PDF)</a>
    #   ...
    #   <strong>{标题}</strong>
    import re as _re
    # 第一步：找到所有 paperlink 及其路径
    # HTML 格式: <a title="{id}" class="paperlink" href="/{id}">{id}</a>
    paper_ids: list[tuple[str, str]] = []  # [(path, id_text), ...]
    for m in _re.finditer(
        r'<a[^>]*class="paperlink"[^>]*href="/(\d{4}/\d+)"',
        html,
    ):
        paper_ids.append((m.group(1), ""))

    if not paper_ids:
        return None

    # 第二步：找到所有 <strong> 标题
    titles = _re.findall(r'<strong>([^<]+)</strong>', html)

    # 标题列表顺序与 paperlink 顺序一致
    title_lower = title.lower().strip().rstrip(".")
    for i, (path, _) in enumerate(paper_ids):
        if i >= len(titles):
            break
        result_title = titles[i].strip()
        result_lower = result_title.lower().rstrip(".")
        # 包含匹配（最短在最长中）
        shorter = title_lower if len(title_lower) < len(result_lower) else result_lower
        longer = result_lower if len(title_lower) < len(result_lower) else title_lower
        if shorter in longer:
            pdf_url = f"https://eprint.iacr.org/{path}.pdf"
            logger.info(f"[MultiDL] ePrint → {pdf_url}")
            return pdf_url

    return None


# ═══════════════════════════════════════════════════════════════════
# 底层下载
# ═══════════════════════════════════════════════════════════════════

async def _download_from_url(
    session: aiohttp.ClientSession,
    sem: asyncio.Semaphore,
    url: str,
    dest: Path,
    timeout: int = 60,
) -> bool:
    """从单个 URL 下载 PDF，%PDF 魔术字节校验。返回成功/失败。"""
    async with sem:
        try:
            async with session.get(url, headers=_HEADERS,
                                   timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                if resp.status != 200:
                    return False

                first_bytes = await resp.content.read(8)
                if not first_bytes.startswith(b"%PDF"):
                    logger.debug(f"[MultiDL] 非 PDF: {url[:60]}")
                    return False

                with open(dest, "wb") as f:
                    f.write(first_bytes)
                    while True:
                        chunk = await resp.content.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)

            if dest.stat().st_size < 1024:
                dest.unlink(missing_ok=True)
                return False
            return True

        except Exception as e:
            logger.debug(f"[MultiDL] 下载异常 ({url[:60]}): {e}")
            if dest.exists():
                dest.unlink(missing_ok=True)
            return False
