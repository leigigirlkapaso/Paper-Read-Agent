"""
utils/pdf_parser.py
PDF → Markdown 文本转换，供 LLM 精读使用。

策略：
- arXiv 论文（有 arxiv_id）：优先从 ar5iv 获取 HTML 版（公式完美保留）
- 其他所有论文：pymupdf4llm PDF → Markdown
- 超出 max_chars 时自动截取摘要 + 引言 + 结论
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pymupdf4llm

logger = logging.getLogger(__name__)

_ARXIV_ID_PATTERN = re.compile(r"(\d{4}\.\d{4,5})")

# 匹配 pymupdf4llm 实际输出：## **1 Introduction** / ## Introduction / ## 1. Background
_INTRO_PATTERN = re.compile(
    r"^#{1,3}\s+\**(?:\s*\d+[\.\s]+)?\s*(introduction|background)\b",
    re.IGNORECASE | re.MULTILINE,
)

_CONCLUSION_PATTERN = re.compile(
    r"^#{1,3}\s+\**(?:\s*\d+[\.\s]+)?\s*(conclusion|conclusions|concluding remarks|summary and conclusion)\b",
    re.IGNORECASE | re.MULTILINE,
)

_REFERENCES_PATTERN = re.compile(
    r"^#{1,3}\s+\**(?:\s*\d+[\.\s]+)?\s*(references|bibliography)\b",
    re.IGNORECASE | re.MULTILINE,
)

# 任意一级标题（## / ### / # 开头行）
_ANY_HEADING = re.compile(r"^#{1,3}\s", re.MULTILINE)


def parse_pdf(
    pdf_path: str | Path,
    max_chars: int = 110000,
    arxiv_id: str = "",
) -> str:
    """
    解析 PDF 文件，返回 Markdown 格式字符串。

    arXiv 论文优先从 ar5iv 获取 HTML 版（公式完美保留为 LaTeX）。
    失败时回退到 pymupdf4llm。

    Args:
        pdf_path:   PDF 文件路径
        max_chars:  送入 LLM 的最大字符数
        arxiv_id:   可选的 arXiv ID（如 "2604.20210"），用于尝试 ar5iv

    Returns:
        Markdown 字符串
    """
    pdf_path = Path(pdf_path)

    # ── arXiv 论文：尝试 ar5iv HTML ──────────────────────────────
    if not arxiv_id and not pdf_path.exists():
        raise FileNotFoundError(f"PDF 文件不存在: {pdf_path}")

    if arxiv_id:
        md_text = _try_ar5iv(arxiv_id)
        if md_text:
            if len(md_text) <= max_chars:
                return md_text
            return _truncate_smart(md_text, max_chars)
        logger.info(f"[PDFParser] ar5iv 不可用，回退 PDF 解析: {arxiv_id}")

    # ── PDF 解析 ─────────────────────────────────────────────
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF 文件不存在: {pdf_path}")

    try:
        md_text: str = pymupdf4llm.to_markdown(str(pdf_path))
    except Exception as e:
        logger.error(f"[PDFParser] 解析失败 {pdf_path.name}: {e}")
        raise

    if len(md_text) <= max_chars:
        return md_text

    logger.warning(
        f"[PDFParser] {pdf_path.name} 正文 {len(md_text)} 字符，"
        f"超过 {max_chars}，自动截取关键段落。"
    )

    return _truncate_smart(md_text, max_chars)


# ── ar5iv HTML parser ────────────────────────────────────────

def _try_ar5iv(arxiv_id: str) -> str | None:
    """Attempt to fetch and parse ar5iv HTML version of an arXiv paper.

    ar5iv preserves LaTeX formulas natively, unlike PDF extraction.
    Returns None if the HTML version is unavailable or parsing fails.
    """
    import urllib.request
    import urllib.error

    url = f"https://ar5iv.labs.arxiv.org/html/{arxiv_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PaperReadAgent/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        logger.warning(f"[PDFParser] ar5iv fetch failed for {arxiv_id}: {e}")
        return None

    if not html or len(html) < 1000:
        return None

    try:
        from markdownify import markdownify
        md = markdownify(html, heading_style="ATX", strip=["nav", "footer", "script", "style"])
    except ImportError:
        # Fallback: use markdown library
        import markdown
        md = markdown.markdown(html)
    except Exception as e:
        logger.warning(f"[PDFParser] ar5iv HTML→MD conversion failed: {e}")
        return None

    # Strip ar5iv chrome (nav bars, copyright, etc.)
    md = _clean_ar5iv_markdown(md)
    if md and len(md) > 500:
        logger.info(f"[PDFParser] ar5iv OK: {arxiv_id}, {len(md)} chars")
        return md
    return None


def _clean_ar5iv_markdown(md: str) -> str:
    """Remove ar5iv navigation chrome from the markdown output."""
    # Remove the initial navigation/header blocks
    md = re.sub(r'^# arXiv:[^\n]*\n', '', md)
    md = re.sub(r'\n\[←\]\([^)]+\)\s*\[→\]\([^)]+\)', '', md)
    # Remove excessive blank lines
    md = re.sub(r'\n{4,}', '\n\n\n', md)
    return md.strip()


# ── Truncation helpers ───────────────────────────────────────

def _truncate_smart(md_text: str, max_chars: int) -> str:
    """Truncate to max_chars, preferring abstract + intro + conclusion."""
    # 优先用正则定位章节
    sections = _extract_key_sections(md_text, max_chars)

    # 回退：正则匹配不足时，使用 PDF 内建目录
    if len(sections) <= 1:
        logger.info("[PDFParser] 正则匹配不足，尝试 TOC 回退...")
        try:
            toc_sections = _extract_key_sections_from_toc(md_text, max_chars)
            if len(toc_sections) > len(sections):
                sections = toc_sections
        except Exception as e:
            logger.warning(f"[PDFParser] TOC 回退失败: {e}")

    combined = "\n\n---\n\n".join(sections)

    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n\n[...内容已截断...]"

    return combined


def _extract_key_sections(md_text: str, max_chars: int) -> list[str]:
    """
    从完整 Markdown 中提取：摘要 + 引言 + 结论，尊重章节边界。

    Returns:
        段落列表，按顺序：摘要 / 引言 / 结论
    """
    budget_abstract = max(1000, max_chars // 10)
    budget_intro = max(2000, max_chars * 50 // 100)
    budget_conclusion = max_chars - budget_abstract - budget_intro

    sections: list[str] = []

    # ── 摘要：文档开头 ──────────────────────────────────────────
    abstract_end = _find_next_heading_start(md_text, 0)
    if abstract_end > 0:
        sections.append(md_text[:abstract_end][:budget_abstract])
    else:
        sections.append(md_text[:budget_abstract])

    # ── 引言 ────────────────────────────────────────────────────
    intro_match = _INTRO_PATTERN.search(md_text)
    if intro_match:
        intro_start = intro_match.start()
        intro_end = _find_next_heading_start(md_text, intro_match.end())
        if intro_end > intro_start:
            sections.append(md_text[intro_start:intro_end][:budget_intro])
        else:
            sections.append(md_text[intro_start:intro_start + budget_intro])

    # ── 结论 ────────────────────────────────────────────────────
    conclusion_match = _CONCLUSION_PATTERN.search(md_text)
    if conclusion_match:
        conclusion_start = conclusion_match.start()
        # 结论结束于 References 或文档末尾
        ref_match = _REFERENCES_PATTERN.search(md_text, pos=conclusion_match.end())
        conclusion_end = ref_match.start() if ref_match else len(md_text)
        conclusion_text = md_text[conclusion_start:conclusion_end]
        sections.append(conclusion_text[:budget_conclusion])

    return sections


def _find_next_heading_start(md_text: str, start_pos: int) -> int:
    """从 start_pos 位置开始找到下一个同级/superior 标题，返回其起始位置。"""
    match = _ANY_HEADING.search(md_text, pos=start_pos)
    return match.start() if match else -1


def _extract_key_sections_from_toc(pdf_path: Path, max_chars: int) -> list[str]:
    """
    使用 pymupdf4llm page_chunks 模式读取 PDF 内建目录，
    按 TOC 条目精确定位 Introduction/Conclusion 的页码范围。
    """
    chunks = pymupdf4llm.to_markdown(str(pdf_path), page_chunks=True)

    intro_page: int | None = None
    conclusion_page: int | None = None
    ref_page: int | None = None

    # 从第一个 chunk 的 toc_items 中找章节页码
    for chunk in chunks:
        for toc_item in chunk.get("toc_items", []):
            level, title, page = toc_item
            title_lower = title.lower().strip("* ")

            if intro_page is None and level == 1 and any(
                kw in title_lower for kw in ("introduction", "background")
            ):
                intro_page = page
            if conclusion_page is None and level == 1 and any(
                kw in title_lower for kw in ("conclusion", "discussion")
            ):
                conclusion_page = page
            if ref_page is None and any(
                kw in title_lower for kw in ("references", "bibliography")
            ):
                ref_page = page

    if intro_page is None and conclusion_page is None:
        return []

    budget_abstract = max(1000, max_chars // 10)
    budget_intro = max(2000, max_chars * 50 // 100)
    budget_conclusion = max_chars - budget_abstract - budget_intro

    sections: list[str] = []

    # 摘要：introduction 之前的页面
    abstract_start = 0
    abstract_end = intro_page if intro_page else (conclusion_page or 1) - 1
    abstract_pages = [
        chunks[i] for i in range(abstract_start, min(abstract_end + 1, len(chunks)))
        if i < len(chunks)
    ]
    if abstract_pages:
        abstract_text = "\n".join(p["text"] for p in abstract_pages)
        sections.append(abstract_text[:budget_abstract])

    # 引言
    if intro_page:
        intro_end_page = (conclusion_page or ref_page or intro_page) - 1
        intro_start = max(0, intro_page - 1)  # 0-indexed
        intro_end = min(intro_end_page + 1, len(chunks))
        intro_pages = [
            chunks[i] for i in range(intro_start, intro_end) if i < len(chunks)
        ]
        if intro_pages:
            intro_text = "\n".join(p["text"] for p in intro_pages)
            sections.append(intro_text[:budget_intro])

    # 结论
    if conclusion_page:
        concl_start = max(0, conclusion_page - 1)
        concl_end = ref_page if ref_page else len(chunks)
        concl_pages = [
            chunks[i]
            for i in range(concl_start, min(concl_end, len(chunks)))
            if i < len(chunks)
        ]
        if concl_pages:
            concl_text = "\n".join(p["text"] for p in concl_pages)
            sections.append(concl_text[:budget_conclusion])

    return sections
