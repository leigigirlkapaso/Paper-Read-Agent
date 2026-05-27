"""
utils/local_scanner.py
扫描本地 outputs/papers/ 目录中已有的 PDF 文件，
将它们与本次在线检索到的论文元信息合并，
对于没有元信息的本地 PDF 构造基础 PaperMeta，
以便统一交给 AGENT2 进行分析。

Phase 3 fix: 数据库感知——扫描时查询同项目下已有 paper 元数据，
复用历史 session 的相关性评分、标题、作者等，避免 Mode 2→3 断联。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from paperreadagent.agent1.arxiv_searcher import PaperMeta

if TYPE_CHECKING:
    from db.database import Database

logger = logging.getLogger(__name__)


def _lookup_paper_in_project(db: "Database | None", project_id: int, arxiv_id: str) -> dict | None:
    """
    在同一个 project 的所有 session 中查找 arxiv_id 匹配的论文，
    返回元数据 dict 或 None。可用于复用历史评分。
    """
    if db is None or not project_id:
        return None
    clean = arxiv_id.lower().split("v")[0]
    row = db.conn.execute(
        """
        SELECT p.* FROM papers p
        JOIN sessions s ON p.session_id = s.id
        WHERE s.project_id = ?
          AND (LOWER(p.arxiv_id) = ? OR LOWER(p.arxiv_id) = ?)
        ORDER BY p.relevance_score DESC
        LIMIT 1
        """,
        (project_id, clean, clean + "v1"),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["authors"] = json.loads(d.get("authors", "[]"))
    except (json.JSONDecodeError, TypeError):
        d["authors"] = ["未知"]
    return d


def _papermeta_from_db(meta: dict, stem: str) -> PaperMeta:
    """从数据库元数据构建 PaperMeta。"""
    return PaperMeta(
        arxiv_id=meta.get("arxiv_id", stem),
        title=meta.get("title") or _stem_to_title(stem),
        authors=meta.get("authors") or ["未知"],
        published=meta.get("published") or "未知",
        abstract=meta.get("abstract") or "（本地文件，无在线摘要，详见全文）",
        pdf_url=meta.get("source_url") or "",
        arxiv_url=meta.get("source_url") or "",
        relevance_score=float(meta.get("relevance_score", 1.0)),
    )


def _papermeta_default(stem: str) -> PaperMeta:
    """构造默认最小化 PaperMeta（无历史记录时）。"""
    return PaperMeta(
        arxiv_id=stem,
        title=_stem_to_title(stem),
        authors=["未知"],
        published="未知",
        abstract="（本地文件，无在线摘要，详见全文）",
        pdf_url="",
        arxiv_url="",
        relevance_score=1.0,  # 手动放入视为高度相关
    )


def scan_and_merge_local_papers(
    pdf_dir: Path,
    online_papers: list[PaperMeta],
    db: "Database | None" = None,
    project_id: int = 0,
) -> list[PaperMeta]:
    """
    扫描 PDF 目录，将本次在线检索论文（already-downloaded subset）
    与目录中额外的本地 PDF 合并，统一返回待分析列表。

    规则：
    1. 遍历 pdf_dir 中所有 *.pdf 文件
    2. 若文件 stem 能与 online_papers 中某篇的 arxiv_id 对应
       → 使用该篇的完整元信息（有标题/摘要/相关性分数等）
    3. 否则认为是手动放入 / 以前 session 下载的额外 PDF
       → 查数据库同项目历史记录复用评分/标题
       → 找不到历史记录才给默认值

    Args:
        pdf_dir:       outputs/papers/ 目录
        online_papers: 本次经过在线检索、筛选、下载成功的论文列表
        db:            数据库实例（可选，用于复用历史元数据）
        project_id:    当前项目 ID

    Returns:
        合并后的 PaperMeta 列表（去重，按先 online 后 local 顺序排列）
    """
    if not pdf_dir.exists():
        logger.warning(f"[Scanner] PDF 目录不存在: {pdf_dir}")
        return online_papers

    # 构建已知论文的快速查找表：文件名 stem → PaperMeta
    known: dict[str, PaperMeta] = {
        p.arxiv_id.replace("/", "_"): p for p in online_papers
    }

    merged: list[PaperMeta] = []
    seen_stems: set[str] = set()
    from_db_count = 0
    default_count = 0

    # 先放 online 论文（保持顺序，且 pdf 确实存在）
    for p in online_papers:
        stem = p.arxiv_id.replace("/", "_")
        pdf_path = pdf_dir / f"{stem}.pdf"
        if pdf_path.exists():
            merged.append(p)
            seen_stems.add(stem)
        else:
            logger.debug(f"[Scanner] 在线论文 PDF 不存在，跳过: {stem}.pdf")

    # 再扫描目录中其他 PDF
    all_pdfs = sorted(pdf_dir.glob("*.pdf"))
    for pdf_path in all_pdfs:
        stem = pdf_path.stem
        if stem in seen_stems:
            continue  # 已在 online 列表中

        # 尝试从数据库复用历史元数据
        db_meta = _lookup_paper_in_project(db, project_id, stem)
        if db_meta and db_meta.get("title"):
            local_meta = _papermeta_from_db(db_meta, stem)
            from_db_count += 1
            logger.debug(
                f"[Scanner] 复用历史评分: {stem} → score={local_meta.relevance_score:.2f}"
            )
        else:
            local_meta = _papermeta_default(stem)
            default_count += 1

        merged.append(local_meta)
        seen_stems.add(stem)

    if from_db_count > 0:
        print(
            f"  [Scanner] 从历史记录复用 {from_db_count} 篇论文元数据"
        )
    if default_count > 0:
        print(
            f"  [Scanner] {default_count} 篇本地 PDF 无历史记录，使用默认评分 1.0"
        )

    return merged


def scan_only_local_papers(
    pdf_dir: Path,
    db: "Database | None" = None,
    project_id: int = 0,
) -> list[PaperMeta]:
    """
    仅扫描本地 PDF 目录，全部构造为 PaperMeta。
    用于"直接分析本地文件"模式（跳过在线检索步骤）。
    优先从数据库复用同项目下的历史论文元数据。

    Args:
        pdf_dir:    outputs/papers/ 目录
        db:         数据库实例（可选）
        project_id: 当前项目 ID

    Returns:
        所有本地 PDF 对应的 PaperMeta 列表
    """
    if not pdf_dir.exists() or not any(pdf_dir.glob("*.pdf")):
        return []

    papers: list[PaperMeta] = []
    from_db = 0
    default = 0

    for pdf_path in sorted(pdf_dir.glob("*.pdf")):
        stem = pdf_path.stem

        db_meta = _lookup_paper_in_project(db, project_id, stem)
        if db_meta and db_meta.get("title"):
            papers.append(_papermeta_from_db(db_meta, stem))
            from_db += 1
        else:
            papers.append(_papermeta_default(stem))
            default += 1

    if from_db > 0:
        print(f"  [Scanner] 从历史记录复用 {from_db} 篇论文元数据")
    if default > 0:
        print(f"  [Scanner] {default} 篇本地 PDF 无历史记录，使用默认评分 1.0")

    return papers


def _stem_to_title(stem: str) -> str:
    """
    将文件名（去除.pdf后缀的 stem）转换为可读标题。
    - arxiv ID 如 "2301.12345" → 保留原样
    - 下划线替换为空格，首字母大写
    """
    import re
    if re.match(r"^\d{4}\.\d{4,5}(v\d+)?$", stem):
        return f"[arxiv {stem}]"
    clean = re.sub(r"v\d+$", "", stem)
    readable = clean.replace("_", " ").replace("-", " ").strip()
    return readable.title() if readable else stem
