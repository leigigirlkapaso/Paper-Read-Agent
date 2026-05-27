"""
utils/exporters.py
Phase 4.5：文献导出 — BibTeX / RIS 格式。
"""

from __future__ import annotations

import re
from datetime import datetime


def papers_to_bibtex(papers: list[dict]) -> str:
    """将论文列表导出为 BibTeX 格式。"""
    entries: list[str] = []
    for i, p in enumerate(papers):
        cite_key = _make_cite_key(p, i)
        entry_type = _guess_bibtex_type(p)
        authors = _parse_authors(p.get("authors", []))
        title = p.get("title", "Untitled").replace("{", "\\{").replace("}", "\\}")
        year = _extract_year(p.get("published", ""))
        venue = p.get("venue", "") or ""
        arxiv_id = p.get("arxiv_id", "")

        lines = [f"@{entry_type}{{{cite_key},"]
        lines.append(f"  title = {{{title}}},")
        if authors:
            lines.append(f"  author = {{{' and '.join(authors)}}},")
        if year:
            lines.append(f"  year = {{{year}}},")
        if venue:
            lines.append(f"  journal = {{{venue}}},")
        if arxiv_id and arxiv_id.startswith("2"):
            lines.append(f"  eprint = {{{arxiv_id}}},")
            lines.append(f"  archiveprefix = {{arXiv}},")
        lines.append(f"  note = {{Relevance: {p.get('relevance_score', 0):.2f}}},")
        lines.append("}")
        entries.append("\n".join(lines))

    return "\n\n".join(entries)


def papers_to_ris(papers: list[dict]) -> str:
    """将论文列表导出为 RIS 格式。"""
    entries: list[str] = []
    for p in papers:
        lines: list[str] = []
        lines.append(f"TY  - {'CONF' if _is_conference(p) else 'JOUR'}")
        lines.append(f"TI  - {p.get('title', 'Untitled')}")
        authors = _parse_authors(p.get("authors", []))
        for a in authors:
            lines.append(f"AU  - {a}")
        year = _extract_year(p.get("published", ""))
        if year:
            lines.append(f"PY  - {year}")
        abstract = p.get("abstract", "")
        if abstract:
            lines.append(f"AB  - {abstract[:500]}")
        arxiv_id = p.get("arxiv_id", "")
        if arxiv_id and arxiv_id.startswith("2"):
            lines.append(f"UR  - https://arxiv.org/abs/{arxiv_id}")
        lines.append(f"N2  - Relevance: {p.get('relevance_score', 0):.2f}")
        lines.append("ER  - ")
        entries.append("\n".join(lines))

    return "\n\n".join(entries)


# ── 工具 ──────────────────────────────────────────────────────────

def _make_cite_key(paper: dict, index: int) -> str:
    """生成 BibTeX cite key。"""
    authors = _parse_authors(paper.get("authors", []))
    year = _extract_year(paper.get("published", ""))
    first_author = authors[0].split()[-1] if authors else "unknown"
    # 取标题首个有意义的词
    title = paper.get("title", "")
    title_word = re.findall(r"[A-Za-z]{3,}", title)
    title_key = title_word[0].lower() if title_word else f"paper{index}"
    return f"{first_author}{year or 'XXXX'}{title_key}"


def _parse_authors(authors: list | str) -> list[str]:
    if isinstance(authors, str):
        import json
        try:
            authors = json.loads(authors)
        except (json.JSONDecodeError, TypeError):
            return [a.strip() for a in authors.split(",") if a.strip()]
    if not isinstance(authors, list):
        return []
    return [str(a).strip() for a in authors if str(a).strip()]


def _extract_year(date_str: str) -> str:
    if not date_str:
        return ""
    m = re.search(r"(19|20)\d{2}", str(date_str))
    return m.group(0) if m else date_str[:4] if len(date_str) >= 4 else ""


def _guess_bibtex_type(paper: dict) -> str:
    venue = (paper.get("venue") or "").lower()
    conf_keywords = ("conference", "proceedings", "neurips", "icml", "cvpr", "iccv",
                     "acl", "emnlp", "aaai", "chi", "siggraph", "uist", "www")
    if any(kw in venue for kw in conf_keywords):
        return "inproceedings"
    return "article"


def _is_conference(paper: dict) -> bool:
    venue = (paper.get("venue") or "").lower()
    return any(kw in venue for kw in ("conference", "neurips", "icml", "cvpr", "chi"))
