"""
main.py
PaperReadAgent 主入口，串联全流程，提供三种运行模式：
  1. 完整流程：网络搜集+下载，并分析所有（新增+本地已有）的 PDF
  2. 仅下载模式：网络搜集+下载，只报告下载失败的清单
  3. 仅分析模式：跳过网络搜集，直接分析本地 papers 文件夹中的所有 PDF

Phase 1 新增：项目/会话隔离 + SQLite 状态管理。
用法：
  uv run python main.py                      # 交互模式
  uv run python main.py --project "足部触觉"   # 指定项目
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import hashlib
import io
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

import yaml

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from agent1.keyword_extractor import extract_keywords
from agent1.arxiv_searcher import search_papers, PaperMeta
from agent1.paper_filter import filter_papers
from agent1.semantic_scholar_searcher import search_semantic_scholar
from agent1.pwc_searcher import search_pwc
from agent1.openalex_searcher import search_openalex
from agent1.dblp_searcher import search_dblp
from agent2.parallel_runner import run_parallel
from utils.llm_client import LLMClient
from utils.arxiv_downloader import download_papers_batch  # 保留向后兼容
from utils.multi_downloader import download_papers_batch_multi
from utils.local_scanner import scan_and_merge_local_papers, scan_only_local_papers
from db.database import Database

# ── 日志配置 ──────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# ── 路径常量 ──────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
PROJECTS_DIR = BASE_DIR / "projects"
LEGACY_OUTPUT_DIR = BASE_DIR / "outputs"
DB_PATH = BASE_DIR / "paperreadagent.db"

# 旧目录（兼容迁移用）
LEGACY_PDF_DIR = LEGACY_OUTPUT_DIR / "papers"
LEGACY_SUMMARY_DIR = LEGACY_OUTPUT_DIR / "summaries"
LEGACY_REPORT_PATH = LEGACY_OUTPUT_DIR / "final_report.md"


# ── 工具 ────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _paper_to_dict(p: PaperMeta) -> dict:
    """将 PaperMeta 转为数据库插入用的 dict。"""
    return {
        "arxiv_id": p.arxiv_id,
        "doi": getattr(p, "doi", ""),
        "source_platform": getattr(p, "source_platform", ""),
        "title": p.title,
        "authors": p.authors,
        "published": p.published,
        "abstract": p.abstract,
        "relevance_score": p.relevance_score,
        "source_url": p.arxiv_url or p.pdf_url or "",
        "has_code": 1 if p.code_url else 0,
        "code_url": p.code_url or "",
        "venue": p.venue or "",
        "citation_count": p.citation_count or 0,
    }


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    logger.info("[Main] 配置文件加载成功")
    return cfg


def build_final_report(
    cfg: dict,
    keywords: list[str],
    queries: list[str],
    papers_with_summaries: list[tuple[PaperMeta, str]],
    failed_papers: list[PaperMeta] | None = None,
) -> str:
    """拼装最终 Markdown 报告。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    topic = cfg["research"]["topic"].strip()
    total_analyzed = len(papers_with_summaries)

    lines: list[str] = [
        "# 📚 文献调研报告",
        "",
        f"> 自动生成时间：{now}",
        "",
        "---",
        "",
        "## 研究主题",
        "",
        topic,
        "",
        "---",
        "",
        "## 检索策略",
        "",
    ]

    if keywords:
        lines.append(f"- **检索关键词**：{', '.join(f'`{k}`' for k in keywords)}")
    if queries:
        lines.append("- **检索 Query**：")
        for q in queries:
            lines.append(f"  - `{q}`")

    lines += [
        f"- **报告生成时间**：{now}",
        f"- **本次分析文献数**：{total_analyzed} 篇",
        "",
        "---",
        "",
        "## 文献分析",
        "",
    ]

    for i, (paper, summary) in enumerate(papers_with_summaries, 1):
        link = paper.arxiv_url or paper.pdf_url
        title_str = f"[{paper.title}]({link})" if link else paper.title

        lines += [
            f"### {i}. {title_str}",
            "",
            f"**作者**：{', '.join(paper.authors[:5])}"
            + ("等" if len(paper.authors) > 5 else "")
            + f"　**发表时间**：{paper.published}"
            + f"　**相关性评分**：{paper.relevance_score:.2f}",
            "",
        ]
        summary_body = _strip_card_header(summary)
        lines.append(summary_body)
        lines += ["", "---", ""]

    lines += [
        "## 综合总结",
        "",
        "> 以上为各文献的独立分析。请综合以上信息进行整体归纳。",
        "",
    ]

    if failed_papers:
        lines += [
            "---",
            "",
            "## 📎 附录：下载失败的文献（仅含元数据）",
            "",
            "> 以下文献因版权限制或反爬虫机制无法获取全文 PDF，仅凭标题与摘要保留。",
            "",
        ]
        for i, paper in enumerate(failed_papers, 1):
            link = paper.arxiv_url or paper.pdf_url or ""
            title_part = f"[{paper.title}]({link})" if link else paper.title
            authors_str = (
                ", ".join(paper.authors[:5]) + ("等" if len(paper.authors) > 5 else "")
                if paper.authors else "未知"
            )
            lines += [
                f"### F{i}. {title_part}",
                "",
                f"**作者**：{authors_str}　"
                f"**发表时间**：{paper.published}　"
                f"**相关性评分**：{paper.relevance_score:.2f}",
                "",
                f"**摘要**：{paper.abstract or '（无摘要）'}",
                "",
                "---",
                "",
            ]

    return "\n".join(lines)


def _strip_card_header(summary: str) -> str:
    if not summary:
        return ""
    lines = summary.strip().splitlines()
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("### ") or (line.startswith("**作者**") and i < 3):
            start = i + 1
        else:
            if i >= 2:
                break
    return "\n".join(lines[start:]).strip()


def _dedup_candidates(papers: list[PaperMeta]) -> list[PaperMeta]:
    seen: set[str] = set()
    deduped: list[PaperMeta] = []
    for p in papers:
        if not p.arxiv_id:
            continue
        key = p.arxiv_id.lower().split("v")[0]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(p)
    return deduped


# ==============================================================================
# 项目 / 会话设置
# ==============================================================================

def _setup_project_interactive(db: Database) -> int:
    """交互式选择或创建项目，返回 project_id。"""
    projects = db.list_projects()

    if projects:
        print("\n已有项目：")
        for i, p in enumerate(projects, 1):
            print(f"  {i}. {p['name']} ({len(db.list_sessions(p['id']))} 个会话)")
        print(f"  N. 创建新项目")
        choice = input("请选择项目编号或输入 N 创建新项目 [默认: 1]: ").strip()
        if choice.upper() == "N":
            pass  # 创建新项目
        elif choice == "" or choice.isdigit():
            idx = int(choice or "1") - 1
            if 0 <= idx < len(projects):
                return projects[idx]["id"]
    else:
        print("\n未找到已有项目。")

    name = input("请输入新项目名称: ").strip()
    if not name:
        name = "Default"
    desc = input("项目描述（可选）: ").strip()
    return db.create_project(name, desc)


def _create_session_dir(project_name: str, db: Database, project_id: int) -> Path:
    """创建会话目录并返回路径。序号按项目内已有 session 数递增，而非 DB 自增 ID。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in project_name).strip()
    if not safe_name:
        safe_name = "project"
    # 序号 = 当前项目 session 数（新建 session 已入库，计数即序号）
    seq = len(db.list_sessions(project_id))
    dir_name = f"{seq:03d}_{ts}"
    session_dir = PROJECTS_DIR / safe_name / "sessions" / dir_name
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "papers").mkdir(exist_ok=True)
    (session_dir / "summaries").mkdir(exist_ok=True)
    return session_dir


def _migrate_legacy_outputs(db: Database, auto_confirm: bool = False) -> int | None:
    """检测旧 outputs/ 目录，提示迁移。返回创建的 project_id 或 None。"""
    if not LEGACY_OUTPUT_DIR.exists():
        return None
    if not any(LEGACY_PDF_DIR.glob("*.pdf")):
        return None

    if not auto_confirm:
        print("\n" + "!" * 60)
        print("  检测到旧版 outputs/ 目录中有 PDF 文件。")
        print("  是否需要迁移到新的项目管理系统？(y/n)")
        choice = input("  [默认: y]: ").strip().lower()
        if choice and choice != "y":
            return None

    proj_id = db.create_project("Legacy", "旧版 outputs/ 自动迁移")
    sid = db.create_session(proj_id, "full", {"migrated": True}, "projects/Legacy/sessions/001_legacy")

    session_dir = _create_session_dir("Legacy", db, proj_id)
    db.update_session(sid, session_dir=str(session_dir))

    # 移动 PDF
    dest_papers = session_dir / "papers"
    for pdf in LEGACY_PDF_DIR.glob("*.pdf"):
        shutil.copy2(pdf, dest_papers / pdf.name)
    print(f"  已迁移 {len(list(dest_papers.glob('*.pdf')))} 个 PDF 到 {dest_papers}")

    # 移动 summaries
    if LEGACY_SUMMARY_DIR.exists():
        dest_summaries = session_dir / "summaries"
        for md in LEGACY_SUMMARY_DIR.glob("*.md"):
            shutil.copy2(md, dest_summaries / md.name)
        print(f"  已迁移 {len(list(dest_summaries.glob('*.md')))} 个 summary")

    # 移动 report
    if LEGACY_REPORT_PATH.exists():
        shutil.copy2(LEGACY_REPORT_PATH, session_dir / "final_report.md")
        print("  已迁移 final_report.md")

    db.update_session(sid, status="completed")
    print("  迁移完成！旧文件保留在原位置。")
    return proj_id


def _create_new_session(
    db: Database, project_id: int, project_name: str, mode: str, cfg: dict
) -> int:
    """创建新会话并写入配置快照。返回 session_id。"""
    session_id = db.create_session(project_id, mode, cfg, "")
    session_dir = _create_session_dir(project_name, db, project_id)
    db.update_session(session_id, session_dir=str(session_dir.relative_to(BASE_DIR)))
    _migrate_logging(session_dir)

    config_snapshot_path = session_dir / "config_snapshot.yaml"
    with open(config_snapshot_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, sort_keys=False)
    config_json = json.dumps(cfg, ensure_ascii=False, sort_keys=True)
    db.update_session(session_id, config_hash=_sha256(config_json)[:16])
    return session_id


def _resolve_session_dir(db: Database, session_id: int) -> Path:
    """根据数据库记录解析会话目录的绝对路径。"""
    session = db.get_session(session_id)
    session_dir = Path(session["session_dir"]) if session else Path(".")
    if not session_dir.is_absolute():
        session_dir = BASE_DIR / session_dir
    return session_dir


def _migrate_logging(session_dir: Path) -> None:
    """将会话日志重定向到 session 目录。"""
    session_log = session_dir / "run.log"
    file_handler = logging.FileHandler(str(session_log), encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s", datefmt="%H:%M:%S")
    )
    # 替换根 logger 的文件 handler
    for h in logging.root.handlers[:]:
        if isinstance(h, logging.FileHandler) and h.baseFilename.endswith("outputs/run.log"):
            logging.root.removeHandler(h)
    logging.root.addHandler(file_handler)
    logger.info(f"[Main] 日志文件: {session_log}")


# ==============================================================================
# 核心流程步骤
# ==============================================================================

def run_online_search(
    cfg: dict,
    llm: LLMClient,
    topic: str,
    db: Database,
    session_id: int,
    session_dir: Path,
    existing_ids: set[str] | None = None,
    skip_keywords: bool = False,
    cached_queries: list[str] | None = None,
    progress_callback: "callable | None" = None,
) -> tuple[list[PaperMeta], list[PaperMeta], list[str], list[str]]:
    """执行网络搜集，返回 (下载成功, 下载失败, 关键词, 查询串)。"""
    research_cfg = cfg["research"]
    downloader_cfg = cfg.get("downloader", {})
    pdf_dir = session_dir / "papers"
    cb = progress_callback or (lambda s, m: None)

    print("\n" + "═" * 60)
    print("  [流程] 关键词提取 (AGENT1-A)")
    print("═" * 60)
    db.update_session(session_id, status="running", started_at=datetime.now().isoformat())
    db.log(session_id, "INFO", "开始关键词提取")

    if skip_keywords and cached_queries:
        keywords = []
        queries = cached_queries
        print(f"  跳过关键词提取，复用 {len(queries)} 条历史 query")
        cb("keywords", f"复用 {len(queries)} 条历史检索词")
    else:
        cb("keywords", "AI 分析研究构想，生成检索关键词...")
        kw_result = extract_keywords(topic, llm)
        keywords = kw_result["keywords"]
        queries = kw_result["queries"]
        print(f"  关键词：{keywords}")
        cb("keywords", f"生成 {len(keywords)} 个关键词, {len(queries)} 条检索式")

    db.update_session(
        session_id,
        keywords=json.dumps(keywords, ensure_ascii=False),
        queries=json.dumps(queries, ensure_ascii=False),
    )
    db.log(session_id, "INFO", f"关键词提取完成: {len(keywords)} 个关键词, {len(queries)} 条 query")

    print("\n" + "═" * 60)
    print("  [流程] 多平台文献检索")
    print("═" * 60)
    sources_cfg = cfg.get("sources", {})
    min_year = research_cfg.get("min_year", 0)
    sort_by = research_cfg.get("sort_by", "date")

    # 构建并行搜索任务（四平台独立，可完全并发）
    search_tasks: list[tuple[str, callable]] = []

    arxiv_enabled = sources_cfg.get("arxiv", True)
    s2_enabled = sources_cfg.get("semantic_scholar", False)
    pwc_enabled = sources_cfg.get("papers_with_code", False)
    oa_enabled = sources_cfg.get("openalex", False)

    if arxiv_enabled:
        def _run_arxiv():
            return search_papers(
                queries=queries, max_results=research_cfg.get("max_search_results", 100),
                keywords=keywords, min_results=research_cfg.get("min_search_results", 200),
                max_queries=research_cfg.get("max_queries", 8),
                query_delay=research_cfg.get("query_delay", 3.0),
                sort_by=sort_by, min_year=min_year,
            )
        search_tasks.append(("arxiv", _run_arxiv))

    if s2_enabled:
        def _run_s2():
            return search_semantic_scholar(
                queries=queries, max_results_per_query=sources_cfg.get("s2_max_per_query", 100),
                min_year=min_year, api_key=sources_cfg.get("s2_api_key", ""),
                query_delay=sources_cfg.get("s2_query_delay", 3.0),
                max_queries=research_cfg.get("max_queries", 8),
            )
        search_tasks.append(("s2", _run_s2))

    if pwc_enabled:
        def _run_pwc():
            return search_pwc(
                queries=queries, max_results_per_query=sources_cfg.get("pwc_max_per_query", 100),
                min_year=min_year, query_delay=sources_cfg.get("pwc_query_delay", 2.0),
                max_queries=research_cfg.get("max_queries", 8),
            )
        search_tasks.append(("pwc", _run_pwc))

    if oa_enabled:
        def _run_oa():
            return search_openalex(
                queries=queries, max_results_per_query=sources_cfg.get("oa_max_per_query", 100),
                min_year=min_year, query_delay=sources_cfg.get("oa_query_delay", 2.0),
                max_queries=research_cfg.get("max_queries", 8),
            )
        search_tasks.append(("oa", _run_oa))

    dblp_enabled = sources_cfg.get("dblp", False)
    if dblp_enabled:
        def _run_dblp():
            return search_dblp(
                queries=queries, max_results_per_query=sources_cfg.get("dblp_max_per_query", 100),
                min_year=min_year, query_delay=sources_cfg.get("dblp_query_delay", 2.0),
                max_queries=research_cfg.get("max_queries", 8),
            )
        search_tasks.append(("dblp", _run_dblp))

    all_candidates: list[PaperMeta] = []
    platform_map = {"arxiv": "arxiv", "s2": "s2", "pwc": "pwc", "oa": "oa", "dblp": "dblp"}

    if len(search_tasks) > 1:
        # 多平台并行检索
        cb("searching", f"并行检索 {len(search_tasks)} 个平台...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(search_tasks)) as executor:
            future_map = {executor.submit(task): name for name, task in search_tasks}
            for future in concurrent.futures.as_completed(future_map):
                name = future_map[future]
                try:
                    results = future.result()
                except Exception as exc:
                    logger.error(f"[{name}] 检索异常: {exc}", exc_info=True)
                    db.log(session_id, "ERROR", f"{name} 检索失败: {exc}")
                    continue
                for p in results:
                    p.source_platform = platform_map.get(name, name)
                all_candidates.extend(results)
                db.log(session_id, "INFO", f"{name} 检索完成: {len(results)} 篇")
    else:
        # 单平台：保持原有顺序执行路径
        for name, task in search_tasks:
            cb("searching", f"{name} 检索中...")
            db.log(session_id, "INFO", f"开始 {name} 检索")
            try:
                results = task()
            except Exception as exc:
                logger.error(f"[{name}] 检索异常: {exc}", exc_info=True)
                db.log(session_id, "ERROR", f"{name} 检索失败: {exc}")
                continue
            for p in results:
                p.source_platform = platform_map.get(name, name)
            all_candidates.extend(results)
            db.log(session_id, "INFO", f"{name} 检索完成: {len(results)} 篇")

    candidates = _dedup_candidates(all_candidates)
    cb("searching", f"检索完成，去重后 {len(candidates)} 篇候选")

    # 增量模式：排除已存在于本项目其他 session 的论文
    if existing_ids:
        new_candidates = [p for p in candidates
                          if p.arxiv_id.lower().split("v")[0] not in existing_ids]
        skipped = len(candidates) - len(new_candidates)
        if skipped > 0:
            print(f"  增量过滤：跳过 {skipped} 篇已存在论文，剩余 {len(new_candidates)} 篇新候选")
        candidates = new_candidates

    print(f"  跨平台去重后共 {len(candidates)} 篇候选文献")
    db.update_session(session_id, total_candidates=len(candidates))

    if not candidates:
        logger.error("未检索到任何文献。")
        db.update_session(session_id, status="failed")
        db.log(session_id, "ERROR", "未检索到任何文献")
        return [], [], keywords, queries

    # 插入候选论文到数据库
    db.insert_papers(session_id, [_paper_to_dict(p) for p in candidates])
    db.log(session_id, "INFO", f"候选文献入库: {len(candidates)} 篇")

    print("\n" + "═" * 60)
    print("  [流程] 相关性筛选 (AGENT1-B)")
    print("═" * 60)
    cb("filtering", f"AI 筛选 {len(candidates)} 篇论文相关性...")
    db.log(session_id, "INFO", "开始相关性筛选")

    filtered_papers = filter_papers(
        papers=candidates, topic=topic, llm=llm,
        relevance_threshold=research_cfg.get("relevance_threshold", 0.8),
        max_download_papers=research_cfg.get("max_download_papers", 20),
        batch_size=research_cfg.get("search_batch_size", 10),
        max_concurrent=research_cfg.get("filter_max_concurrent", 100),
    )
    print(f"  筛选后保留：{len(filtered_papers)} 篇")
    cb("filtering", f"筛选完成，保留 {len(filtered_papers)} 篇（阈值 ≥{research_cfg.get('relevance_threshold', 0.8)}）")
    db.update_session(session_id, total_filtered=len(filtered_papers))

    # 更新筛选状态
    for p in candidates:
        db.update_paper_by_arxiv_id(session_id, p.arxiv_id, relevance_score=p.relevance_score)

    if not filtered_papers:
        logger.warning("筛选后无符合条件的文献。")
        db.update_session(session_id, status="completed")
        return [], [], keywords, queries

    print("\n" + "═" * 60)
    print("  [流程] 下载 PDF")
    print("═" * 60)
    cb("downloading", f"下载 {len(filtered_papers)} 篇 PDF...")
    db.log(session_id, "INFO", f"开始下载 {len(filtered_papers)} 篇 PDF")

    print("  使用多源级联下载（arXiv → 直接URL → Unpaywall → S2 → Sci-Hub）...")
    download_results = download_papers_batch_multi(
        papers=filtered_papers,
        output_dir=pdf_dir,
        unpaywall_email=downloader_cfg.get("unpaywall_email", ""),
        enable_scihub=downloader_cfg.get("enable_scihub", False),
        scihub_mirrors=downloader_cfg.get("scihub_mirrors", None),
        max_concurrent=5,
    )

    downloadable = [p for p in filtered_papers if download_results.get(p.arxiv_id) is not None]
    failed_papers = [p for p in filtered_papers if download_results.get(p.arxiv_id) is None]

    # 更新下载状态
    for p in downloadable:
        pdf_path = download_results[p.arxiv_id]
        db.update_paper_by_arxiv_id(
            session_id, p.arxiv_id,
            download_status="success",
            pdf_path=str(pdf_path) if pdf_path else "",
        )
    for p in failed_papers:
        db.update_paper_by_arxiv_id(session_id, p.arxiv_id, download_status="failed")

    cb("downloading", f"下载完成：{len(downloadable)} 成功 / {len(failed_papers)} 失败")
    print(f"  成功下载：{len(downloadable)} / {len(filtered_papers)} 篇")
    if failed_papers:
        print(f"  下载失败：{len(failed_papers)} 篇（需自行下载）")
        for f in failed_papers:
            link = f.arxiv_url or f.pdf_url or "无链接"
            print(f"    - {f.title}\n      URL: {link}")

    db.update_session(
        session_id,
        total_downloaded=len(downloadable),
        total_failed_downloads=len(failed_papers),
    )
    db.log(session_id, "INFO", f"下载完成: {len(downloadable)} 成功, {len(failed_papers)} 失败")

    return downloadable, failed_papers, keywords, queries


async def run_analysis(
    papers_to_analyze: list[PaperMeta],
    failed_papers: list[PaperMeta],
    cfg: dict,
    llm: LLMClient,
    topic: str,
    keywords: list[str],
    queries: list[str],
    db: Database,
    session_id: int,
    session_dir: Path,
) -> None:
    """执行并发精读，生成最终报告。"""
    if not papers_to_analyze:
        print("\n[警告] 没有可供分析的 PDF 文献。")
        return

    concurrency = cfg.get("concurrency", {})
    pdf_cfg = cfg.get("pdf", {})
    summary_prompt = cfg.get("summary_prompt", "请对该论文进行结构化总结。")

    pdf_dir = session_dir / "papers"
    summary_dir = session_dir / "summaries"
    report_path = session_dir / "final_report.md"

    print("\n" + "═" * 60)
    print(f"  [流程] 并发精读 (AGENT2) - 共 {len(papers_to_analyze)} 篇")
    print("═" * 60)
    db.log(session_id, "INFO", f"开始并发精读: {len(papers_to_analyze)} 篇, 并发={concurrency.get('max_concurrent', 100)}")

    # 更新待分析论文的数据库状态
    for p in papers_to_analyze:
        db.update_paper_by_arxiv_id(session_id, p.arxiv_id, parse_status="pending")

    papers_with_summaries = await run_parallel(
        papers=papers_to_analyze,
        pdf_dir=pdf_dir,
        summary_dir=summary_dir,
        summary_prompt=summary_prompt,
        topic=topic,
        llm=llm,
        max_concurrent=concurrency.get("max_concurrent", 100),
        max_chars=pdf_cfg.get("max_chars", 110000),
        db=db,
        session_id=session_id,
    )

    # 更新总结状态
    for paper, _summary in papers_with_summaries:
        db.update_paper_by_arxiv_id(session_id, paper.arxiv_id, summary_status="success")

    print("\n" + "═" * 60)
    print("  [流程] 生成最终报告")
    print("═" * 60)
    report_md = build_final_report(
        cfg=cfg,
        keywords=keywords,
        queries=queries,
        papers_with_summaries=papers_with_summaries,
        failed_papers=failed_papers,
    )
    report_path.write_text(report_md, encoding="utf-8")
    print(f"\n  [完成] 报告已生成：{report_path.resolve()}")

    db.update_session(
        session_id,
        status="completed",
        total_analyzed=len(papers_with_summaries),
        completed_at=datetime.now().isoformat(),
    )
    db.log(session_id, "INFO", f"报告生成完成: {len(papers_with_summaries)} 篇分析")


# ==============================================================================
# 异步主函数
# ==============================================================================

async def async_main() -> None:
    # ── CLI 参数 ──────────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="PaperReadAgent - 自动文献调研多智能体系统")
    parser.add_argument("--project", type=str, help="项目名称")
    parser.add_argument("--session", type=int, help="复用已有会话 ID（仅分析模式）")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="数据库路径")
    parser.add_argument("--migrate", action="store_true", help="仅执行旧版迁移")
    args = parser.parse_args()

    # ── 数据库 ────────────────────────────────────────────────────
    db = Database(args.db)

    # ── 旧版迁移 ──────────────────────────────────────────────────
    _migrate_legacy_outputs(db, auto_confirm=args.migrate)
    if args.migrate:
        print("迁移模式完成。")
        db.close()
        return

    # ── 项目选择 ──────────────────────────────────────────────────
    if args.project:
        project_id = db.get_or_create_project(args.project)
        print(f"\n项目: {args.project} (id={project_id})")
    else:
        project_id = _setup_project_interactive(db)

    project = db.get_project(project_id)
    project_name = project["name"] if project else "Unknown"

    # ── 模式选择 ──────────────────────────────────────────────────
    print("\n" + "═" * 60)
    print("  PaperReadAgent - 自动文献调研多智能体系统")
    print("═" * 60)
    print("请选择运行模式：")
    print("  1. 【完整模式】 网络搜集 + 下载，并分析 papers 文件夹中所有 PDF")
    print("  2. 【仅搜集模式】 网络搜集 + 下载，报告下载失败清单（不进行精读）")
    print("  3. 【仅分析模式】 跳过网络搜集，直接精读分析 papers 文件夹中现存的 PDF")
    print("  4. 【增量模式】 复用上次关键词，仅下载和分析本项目中未出现的新论文")
    print("═" * 60)

    choice = input("请输入对应数字 (1/2/3) [默认: 1]: ").strip()
    if not choice:
        choice = "1"

    if choice not in ("1", "2", "3", "4"):
        print("输入无效，退出。")
        db.close()
        sys.exit(1)

    # ── 加载配置 ──────────────────────────────────────────────────
    cfg = load_config()
    llm = LLMClient.from_config(cfg["llm"])
    topic = cfg["research"]["topic"]

    # ── 创建或复用会话 ──────────────────────────────────────────
    if args.session:
        # 复用已有会话（Mode 3 直接从已有 session 的 papers/ 分析）
        session_id = args.session
        session = db.get_session(session_id)
        if not session:
            print(f"会话 {session_id} 不存在，退出。")
            db.close()
            sys.exit(1)
        session_dir = Path(session["session_dir"])
        if not session_dir.is_absolute():
            session_dir = BASE_DIR / session_dir
        # 强制使用仅分析模式
        choice = "3"
        print(f"复用会话 #{session_id}，目录: {session_dir}")
    elif choice in ("1", "2", "4"):
        mode_map = {"1": "full", "2": "collect", "4": "incremental"}
        session_id = _create_new_session(db, project_id, project_name,
                                          mode_map[choice], cfg)
        session_dir = _resolve_session_dir(db, session_id)
    else:
        session_id = _create_new_session(db, project_id, project_name, "analyze", cfg)
        session_dir = _resolve_session_dir(db, session_id)

    print(f"\n会话目录: {session_dir}")
    db.log(session_id, "INFO", f"会话启动, 模式={choice}")

    # ── 变量初始化 ────────────────────────────────────────────────
    online_downloaded: list[PaperMeta] = []
    failed_papers: list[PaperMeta] = []
    keywords: list[str] = []
    queries: list[str] = []

    # ── 执行 ──────────────────────────────────────────────────────
    try:
        if choice == "4":
            # ── 增量模式：复用关键词，只处理新论文 ──────────────────
            print("\n" + "═" * 60)
            print("  [增量模式] 查找上次会话关键词...")
            print("═" * 60)
            prev_sessions = db.list_sessions(project_id)
            prev_completed = [s for s in prev_sessions if s["status"] == "completed"]
            if prev_completed:
                prev = prev_completed[0]
                try:
                    keywords = json.loads(prev.get("keywords", "[]"))
                    queries = json.loads(prev.get("queries", "[]"))
                except (json.JSONDecodeError, TypeError):
                    keywords, queries = [], []
                print(f"  复用 #{prev['id']} 的关键词 ({len(keywords)} 个) 和 query ({len(queries)} 条)")
            if not queries:
                kw_result = extract_keywords(topic, llm)
                keywords = kw_result["keywords"]
                queries = kw_result["queries"]
                print(f"  无历史关键词，重新提取: {keywords}")

            # 获取本项目中已有的 arxiv_id 集合
            existing_ids = set()
            for s in db.list_sessions(project_id):
                for p in db.get_session_papers(s["id"]):
                    aid = (p.get("arxiv_id") or "").lower().split("v")[0]
                    if aid:
                        existing_ids.add(aid)
            print(f"  已有论文: {len(existing_ids)} 篇")

            online_downloaded, failed_papers, keywords, queries = run_online_search(
                cfg, llm, topic, db, session_id, session_dir,
                existing_ids=existing_ids, skip_keywords=True, cached_queries=queries,
            )
        elif choice in ("1", "2"):
            online_downloaded, failed_papers, keywords, queries = run_online_search(
                cfg, llm, topic, db, session_id, session_dir
            )

        if choice == "2":
            print("\n  [完成] 搜集并下载结束。请查看失败清单自行补齐 PDF。")
            db.update_session(session_id, status="completed")
            return

        if choice == "1":
            print("\n  [系统] 正在扫描本地论文...")
            pdf_dir = session_dir / "papers"
            papers_to_analyze = scan_and_merge_local_papers(
                pdf_dir, online_downloaded, db=db, project_id=project_id
            )
            # 将本地 PDF 也入库
            for p in papers_to_analyze:
                if not any(d.arxiv_id == p.arxiv_id for d in online_downloaded):
                    db.insert_papers(session_id, [_paper_to_dict(p)])
        else:  # choice == "3"
            print("\n  [系统] 正在扫描本地论文...")
            pdf_dir = session_dir / "papers"
            papers_to_analyze = scan_only_local_papers(
                pdf_dir, db=db, project_id=project_id
            )
            # 全部入库
            if papers_to_analyze:
                db.insert_papers(session_id, [_paper_to_dict(p) for p in papers_to_analyze])

        await run_analysis(
            papers_to_analyze, failed_papers, cfg, llm, topic,
            keywords, queries, db, session_id, session_dir,
        )

    except Exception as e:
        logger.error(f"[Main] 流程异常: {e}")
        db.update_session(session_id, status="failed")
        db.log(session_id, "ERROR", f"流程异常: {e}")
        raise
    else:
        db.update_session(session_id, status="completed")
    finally:
        db.log(session_id, "INFO", "会话结束")
        db.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
