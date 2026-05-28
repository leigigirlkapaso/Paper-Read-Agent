"""
web/routes/sessions.py
会话管理 + pipeline 启动 + SSE 进度推送。
"""

from __future__ import annotations

import asyncio
import json
import yaml
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from db.database import Database
from web.progress import get_progress, sse_event_generator, ProgressState

router = APIRouter(prefix="/sessions", tags=["sessions"])

from web.template_config import templates

BASE_DIR = Path(__file__).parent.parent.parent.parent


# ── 新建会话表单 ─────────────────────────────────────────────────

@router.get("/new/{project_id}", response_class=HTMLResponse)
async def session_new_form(request: Request, project_id: int):
    db = request.app.state.db
    project = db.get_project(project_id)
    if not project:
        return HTMLResponse("<h2>Project not found</h2>", status_code=404)

    # 尝试加载默认 config.yaml
    default_config = ""
    config_path = BASE_DIR / "config.yaml"
    if config_path.exists():
        default_config = config_path.read_text(encoding="utf-8")

    # 解析 config 注入前端表单默认值
    import json as _json
    _cfg = yaml.safe_load(default_config) if default_config else {}
    form_defaults = _json.dumps({
        "topic": (_cfg.get("research") or {}).get("topic", "").strip(),
        "api_base_url": (_cfg.get("llm") or {}).get("api_base_url", "https://api.deepseek.com"),
        "api_key": (_cfg.get("llm") or {}).get("api_key", ""),
        "model_name": (_cfg.get("llm") or {}).get("model_name", "deepseek-v4-pro"),
        "temperature": (_cfg.get("llm") or {}).get("temperature", 0.3),
        "max_search_results": (_cfg.get("research") or {}).get("max_search_results", 100),
        "min_search_results": (_cfg.get("research") or {}).get("min_search_results", 200),
        "max_queries": (_cfg.get("research") or {}).get("max_queries", 8),
        "query_delay": (_cfg.get("research") or {}).get("query_delay", 8),
        "relevance_threshold": (_cfg.get("research") or {}).get("relevance_threshold", 0.6),
        "max_download_papers": (_cfg.get("research") or {}).get("max_download_papers", 50),
        "min_year": (_cfg.get("research") or {}).get("min_year", 2022),
        "sort_by": (_cfg.get("research") or {}).get("sort_by", "date"),
        "max_concurrent": (_cfg.get("concurrency") or {}).get("max_concurrent", 100),
        "max_chars": (_cfg.get("pdf") or {}).get("max_chars", 80000),
        "summary_prompt": (_cfg.get("summary_prompt") or "").strip(),
        "source_arxiv": (_cfg.get("sources") or {}).get("arxiv", True),
        "source_s2": (_cfg.get("sources") or {}).get("semantic_scholar", False),
        "source_pwc": (_cfg.get("sources") or {}).get("papers_with_code", False),
        "source_oa": (_cfg.get("sources") or {}).get("openalex", False),
    })

    return templates.TemplateResponse("session_new.html", {
        "request": request,
        "project": project,
        "default_config": default_config,
        "form_defaults_json": form_defaults,
    })


# ── 启动 Pipeline ────────────────────────────────────────────────

@router.post("/start/{project_id}")
async def session_start(
    request: Request,
    project_id: int,
    mode: str = Form("full"),
    config_yaml: str = Form(""),
):
    db: Database = request.app.state.db

    # 解析配置
    if config_yaml.strip():
        cfg = yaml.safe_load(config_yaml)
    else:
        config_path = BASE_DIR / "config.yaml"
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    project = db.get_project(project_id)
    project_name = project["name"] if project else "Unknown"

    # 创建 session 目录（序号按项目内实际 session 数，非 DB 自增 ID）
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = db.create_session(project_id, mode, cfg, "")
    seq = len(db.list_sessions(project_id))
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in project_name).strip()
    session_dir = BASE_DIR / "projects" / safe_name / "sessions" / f"{seq:03d}_{ts}"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "papers").mkdir(exist_ok=True)
    (session_dir / "summaries").mkdir(exist_ok=True)

    rel_dir = str(session_dir.relative_to(BASE_DIR))
    db.update_session(session_id, session_dir=rel_dir)

    # 保存配置快照
    (session_dir / "config_snapshot.yaml").write_text(config_yaml, encoding="utf-8")

    # 初始化进度
    progress = get_progress(session_id)
    progress.stage = "running"
    progress.started_at = datetime.now().timestamp()

    # 后台启动 pipeline
    from paperreadagent import main as main_module
    from utils.llm_client import LLMClient

    def _on_progress(stage: str, message: str):
        """进度回调：由 run_online_search 在每个阶段调用。"""
        stage_map = {
            "keywords": (1, "🔑 关键词提取"),
            "searching": (2, "🔍 文献检索"),
            "filtering": (3, "🎯 相关性筛选"),
            "downloading": (4, "📥 下载 PDF"),
            "reading": (5, "📖 并发精读"),
            "reporting": (6, "📄 生成报告"),
        }
        idx, label = stage_map.get(stage, (0, stage))
        progress.stage = stage
        progress.stage_index = idx
        progress.messages.append(f"{label} — {message}")

    async def _run_pipeline():
        try:
            llm = LLMClient.from_config(cfg["llm"])
            topic = cfg["research"]["topic"]

            progress.stage = "init"
            progress.messages.append("初始化中...")

            if mode == "incremental":
                project_sessions = db.list_sessions(project_id)
                prev = next((s for s in project_sessions if s["status"] == "completed"), None)
                import json as _json
                if prev:
                    try:
                        kw = _json.loads(prev.get("keywords", "[]"))
                        qs = _json.loads(prev.get("queries", "[]"))
                    except Exception:
                        kw, qs = [], []

                existing_ids = set()
                for s in project_sessions:
                    for p in db.get_session_papers(s["id"]):
                        aid = (p.get("arxiv_id") or "").lower().split("v")[0]
                        if aid:
                            existing_ids.add(aid)

                downloadable, failed, kw, qs = await asyncio.to_thread(
                    main_module.run_online_search,
                    cfg, llm, topic, db, session_id, session_dir,
                    existing_ids, True, qs if qs else None,
                    _on_progress,
                )

            elif mode in ("full", "collect"):
                downloadable, failed, kw, qs = await asyncio.to_thread(
                    main_module.run_online_search,
                    cfg, llm, topic, db, session_id, session_dir,
                    None, False, None, _on_progress,
                )

            if mode in ("full", "incremental"):
                _on_progress("reading", f"{len(downloadable)} 篇论文排队精读...")

                from utils.local_scanner import scan_and_merge_local_papers
                papers_to_analyze = scan_and_merge_local_papers(
                    session_dir / "papers", downloadable,
                    db=db, project_id=project_id,
                )
                for p in papers_to_analyze:
                    if not any(d.arxiv_id == p.arxiv_id for d in downloadable):
                        db.insert_papers(session_id, [main_module._paper_to_dict(p)])
                progress.papers_total = len(papers_to_analyze)

                await main_module.run_analysis(
                    papers_to_analyze, failed, cfg, llm, topic,
                    kw, qs, db, session_id, session_dir,
                )

            elif mode == "analyze":
                _on_progress("reading", "扫描本地 PDF...")
                from utils.local_scanner import scan_only_local_papers
                papers_to_analyze = scan_only_local_papers(
                    session_dir / "papers", db=db, project_id=project_id
                )
                if papers_to_analyze:
                    db.insert_papers(session_id, [
                        main_module._paper_to_dict(p) for p in papers_to_analyze
                    ])
                progress.papers_total = len(papers_to_analyze)
                _on_progress("reading", f"开始精读 {len(papers_to_analyze)} 篇...")
                await main_module.run_analysis(
                    papers_to_analyze, [], cfg, llm, topic,
                    [], [], db, session_id, session_dir,
                )

            await asyncio.to_thread(db.rebuild_fts)

            progress.stage = "done"
            progress.stage_index = 6
            progress.messages.append("[完成] 调研结束")
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("[Sessions] Pipeline 执行异常")
            progress.stage = "error"
            progress.error = str(e)
            progress.messages.append(f"[错误] {e}")

    asyncio.create_task(_run_pipeline())

    # 直接返回 session 详情页，浏览器即时渲染，SSE 显示进度
    session = db.get_session(session_id)
    project = db.get_project(project_id)
    papers = db.get_session_papers(session_id)
    downloaded = sum(1 for p in papers if p["download_status"] == "success")
    analyzed = sum(1 for p in papers if p["summary_status"] in ("success", "cached"))
    failed = sum(1 for p in papers if p["download_status"] == "failed")

    return templates.TemplateResponse("session_detail.html", {
        "request": request,
        "session": session,
        "project": project,
        "papers": papers,
        "downloaded": downloaded,
        "analyzed": analyzed,
        "failed": failed,
    })


# ── 会话详情 ─────────────────────────────────────────────────────

@router.get("/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: int):
    db = request.app.state.db
    session = db.get_session(session_id)
    if not session:
        return HTMLResponse("<h2>Session not found</h2>", status_code=404)

    project = db.get_project(session["project_id"])
    papers = db.get_session_papers(session_id)

    # 统计
    downloaded = sum(1 for p in papers if p["download_status"] == "success")
    analyzed = sum(1 for p in papers if p["summary_status"] in ("success", "cached"))
    failed = sum(1 for p in papers if p["download_status"] == "failed")

    return templates.TemplateResponse("session_detail.html", {
        "request": request,
        "session": session,
        "project": project,
        "papers": papers,
        "downloaded": downloaded,
        "analyzed": analyzed,
        "failed": failed,
    })


# ── SSE 进度 ─────────────────────────────────────────────────────

@router.get("/{session_id}/progress")
async def session_progress(session_id: int):
    return StreamingResponse(
        sse_event_generator(session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── JSON 进度 (移动端轮询兜底) ───────────────────────────────────

@router.get("/{session_id}/progress/json")
async def session_progress_json(request: Request, session_id: int):
    """Non-SSE progress — for mobile polling fallback."""
    progress = get_progress(session_id)
    return {
        "stage": progress.stage,
        "stage_index": progress.stage_index,
        "total_stages": progress.total_stages,
        "papers_total": progress.papers_total,
        "papers_completed": progress.papers_completed,
        "papers_failed": progress.papers_failed,
        "current_title": progress.current_paper_title or "",
        "messages": progress.messages[-5:] if progress.messages else [],
        "error": progress.error or "",
    }


# ── 报告 ─────────────────────────────────────────────────────────

@router.get("/{session_id}/report", response_class=HTMLResponse)
async def session_report(request: Request, session_id: int):
    db = request.app.state.db
    session = db.get_session(session_id)
    if not session:
        return HTMLResponse("<h2>Session not found</h2>", status_code=404)

    project = db.get_project(session["project_id"])
    session_dir = BASE_DIR / session["session_dir"]
    report_path = session_dir / "final_report.md"

    report_content = ""
    if report_path.exists():
        report_content = report_path.read_text(encoding="utf-8")

    return templates.TemplateResponse("report.html", {
        "request": request,
        "session": session,
        "project": project,
        "report_content": report_content,
    })


# ── 导出 ─────────────────────────────────────────────────────────

@router.get("/{session_id}/export/{fmt}")
async def session_export(request: Request, session_id: int, fmt: str):
    db = request.app.state.db
    papers = db.get_session_papers(session_id)
    from utils.exporters import papers_to_bibtex, papers_to_ris

    if fmt == "bibtex":
        content = papers_to_bibtex(papers)
        media = "application/x-bibtex"
    elif fmt == "ris":
        content = papers_to_ris(papers)
        media = "application/x-research-info-systems"
    else:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("Unsupported format. Use bibtex or ris.", status_code=400)

    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content, media_type=media,
                              headers={"Content-Disposition": f"attachment; filename=export.{fmt}"})


# ── 重新分析 ─────────────────────────────────────────────────────

@router.post("/{session_id}/reanalyze")
async def session_reanalyze(request: Request, session_id: int):
    """对已有 PDF 但无 summary 的论文重新运行 AGENT2 分析。"""
    db: Database = request.app.state.db
    session = db.get_session(session_id)
    if not session:
        return HTMLResponse("Session not found", status_code=404)

    session_dir = BASE_DIR / session["session_dir"]
    db_papers = db.get_session_papers(session_id)

    # 查 DB：有 PDF 但 summary 缺失或内容为空的论文
    from agent1.arxiv_searcher import PaperMeta
    need_analysis: list[PaperMeta] = []
    skipped_no_pdf = 0
    skipped_has_content = 0

    # 先扫描 papers 目录建立文件名→路径映射
    papers_dir = session_dir / "papers"
    pdf_files: dict[str, Path] = {}
    if papers_dir.exists():
        for f in papers_dir.glob("*.pdf"):
            pdf_files[f.stem] = f

    for dp in db_papers:
        if dp["download_status"] != "success":
            continue
        # 跳过已成功完成且内容非空的
        if dp["summary_status"] in ("success", "cached"):
            existing = db.get_paper_summaries(dp["id"])
            if existing and existing[0].get("content"):
                skipped_has_content += 1
                continue
        # 找 PDF：先按 arxiv_id 匹配，再扫描目录
        arxiv_stem = dp["arxiv_id"].replace("/", "_")
        pdf_path = None
        if arxiv_stem in pdf_files:
            pdf_path = pdf_files[arxiv_stem]
        else:
            # 回退：从 pdf_path 字段找
            if dp.get("pdf_path"):
                alt = Path(dp["pdf_path"])
                if not alt.is_absolute():
                    alt = BASE_DIR / alt
                if alt.exists():
                    pdf_path = alt
            # 最后回退：目录中随便拿一个（用户可能只上传了这一个）
            if not pdf_path and len(pdf_files) == 1:
                pdf_path = next(iter(pdf_files.values()))
        if not pdf_path:
            skipped_no_pdf += 1
            continue
        p = PaperMeta(
            arxiv_id=dp["arxiv_id"],
            title=dp.get("title", ""),
            authors=dp.get("authors", []),
            published=dp.get("published", ""),
            abstract=dp.get("abstract", ""),
            pdf_url="",
            arxiv_url=dp.get("source_url", ""),
            relevance_score=dp.get("relevance_score", 1.0),
        )
        need_analysis.append(p)

    print(f"[Reanalyze] 目录PDF={len(pdf_files)}, 需分析={len(need_analysis)}, "
          f"跳过(无PDF)={skipped_no_pdf}, 跳过(有内容)={skipped_has_content}, 总={len(db_papers)}")

    if not need_analysis:
        return HTMLResponse("<p class='p-4 text-gray-500'>所有论文已分析完毕，无需重新分析。</p>")

    # 后台启动分析
    from paperreadagent import main as main_module
    from utils.llm_client import LLMClient
    import yaml as _yaml

    config_path = session_dir / "config_snapshot.yaml"
    cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}

    progress = get_progress(session_id)
    progress.stage = "reading"
    progress.stage_index = 5
    progress.papers_total = len(need_analysis)
    progress.messages.append(f"📖 重新分析 {len(need_analysis)} 篇...")

    async def _reanalyze():
        try:
            llm = LLMClient.from_config(cfg.get("llm", {}))
            topic = cfg.get("research", {}).get("topic", "")
            await main_module.run_analysis(
                need_analysis, [], cfg, llm, topic,
                [], [], db, session_id, session_dir,
            )
            await asyncio.to_thread(db.rebuild_fts)
            progress.stage = "done"
            progress.stage_index = 6
            progress.messages.append("✅ 重新分析完成")
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception("[Sessions] 重新分析异常")
            progress.stage = "error"
            progress.error = str(e)

    import asyncio
    asyncio.create_task(_reanalyze())

    return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)


# ── 会话删除 ─────────────────────────────────────────────────────

@router.post("/{session_id}/delete")
async def delete_session(request: Request, session_id: int):
    db = request.app.state.db
    session = db.get_session(session_id)
    project_id = session["project_id"] if session else 0
    if session:
        db.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        db.conn.commit()
    return RedirectResponse(url=f"/projects/{project_id}", status_code=303)
