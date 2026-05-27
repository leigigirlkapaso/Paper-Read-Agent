"""
web/routes/papers.py
论文查看、PDF 服务、上传、搜索路由。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Request, File, UploadFile, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse

router = APIRouter(prefix="/papers", tags=["papers"])

from web.template_config import templates

BASE_DIR = Path(__file__).parent.parent.parent.parent


# ── 笔记索引 ─────────────────────────────────────────────────────

@router.get("/notes", response_class=HTMLResponse)
async def notes_index(request: Request, project_id: int | None = None):
    db = request.app.state.db
    notes = db.get_all_notes(project_id)
    projects = db.list_projects()
    return templates.TemplateResponse("notes.html", {
        "request": request,
        "notes": notes,
        "projects": projects,
        "selected_project": project_id,
    })


# ── 项目关系图 ─────────────────────────────────────────────────

@router.get("/graph", response_class=HTMLResponse)
async def project_graph(request: Request):
    db = request.app.state.db
    graph_data = db.get_cross_project_graph()
    return templates.TemplateResponse("graph.html", {
        "request": request,
        "graph_data": graph_data,
    })


# ── 全文搜索 ─────────────────────────────────────────────────────

@router.get("/search", response_class=HTMLResponse)
async def search_papers(
    request: Request,
    q: str = "",
    project_id: int | None = None,
):
    db = request.app.state.db
    results = db.search_papers(q, project_id) if q else []
    projects = db.list_projects()
    return templates.TemplateResponse("history.html", {
        "request": request,
        "query": q,
        "results": results,
        "projects": projects,
        "selected_project": project_id,
    })


# ── 论文详情（侧边对照阅读器）──────────────────────────────────

@router.get("/{paper_id}", response_class=HTMLResponse)
async def paper_detail(request: Request, paper_id: int):
    import markdown as _md
    db = request.app.state.db
    paper = db.get_paper(paper_id)
    if not paper:
        return HTMLResponse("<h2>Paper not found</h2>", status_code=404)

    session = db.get_session(paper["session_id"])
    project = db.get_project(session["project_id"]) if session else None

    # 读取 summary markdown（多重回退）
    summary_content = ""
    debug_info = []

    # 1. 从 summaries 数据库表读取
    summaries = db.get_paper_summaries(paper_id)
    debug_info.append(f"DB summaries: {len(summaries) if summaries else 0}")
    if summaries and summaries[0].get("content"):
        summary_content = summaries[0]["content"]
        debug_info.append("→ 从数据库表加载")

    # 2. 从 summary_path 字段读取
    if not summary_content:
        summary_path_str = paper.get("summary_path", "")
        debug_info.append(f"summary_path={summary_path_str[:80] if summary_path_str else '(空)'}")
        if summary_path_str:
            sp = Path(summary_path_str)
            if not sp.is_absolute():
                sp = BASE_DIR / sp
            debug_info.append(f"绝对路径={sp}, 存在={sp.exists()}")
            if sp.exists():
                summary_content = sp.read_text(encoding="utf-8")
                debug_info.append("→ 从文件加载")

    # 3. 搜索 session summaries 目录
    if not summary_content and session:
        sd = BASE_DIR / session["session_dir"]
        sdir = sd / "summaries"
        debug_info.append(f"搜索目录={sdir}, 存在={sdir.exists()}")
        if sdir.exists():
            aid = paper.get("arxiv_id", "").replace("/", "_")
            files = list(sdir.glob(f"{aid}*.md"))
            debug_info.append(f"glob({aid}*.md)={len(files)} 个文件")
            for f in files:
                summary_content = f.read_text(encoding="utf-8")
                debug_info.append(f"→ 从 {f.name} 加载")
                break
            # 兜底：列出目录所有文件看看
            if not files:
                all_md = list(sdir.glob("*.md"))
                debug_info.append(f"目录共 {len(all_md)} 个 .md: {[f.name[:30] for f in all_md[:5]]}")

    debug_info.append(f"最终内容长度: {len(summary_content)}")

    # 服务端 Markdown → HTML 渲染
    summary_html = ""
    if summary_content:
        try:
            summary_html = _md.markdown(summary_content, extensions=['extra', 'nl2br', 'sane_lists'])
        except Exception as e:
            summary_html = f"<p>Markdown 渲染失败: {e}</p><pre>{summary_content[:500]}</pre>"
            debug_info.append(f"渲染失败: {e}")

    # 读取笔记
    note = db.get_note(paper_id)
    note_content = note["content"] if note else ""
    note_updated = note["updated_at"] if note else ""

    # PDF 路径（用于 PDF.js）
    pdf_url = f"/papers/{paper_id}/pdf" if paper.get("pdf_path") else ""

    is_mobile = getattr(request.state, "is_mobile", False)
    template_name = "paper_detail_mobile.html" if is_mobile else "paper_detail.html"
    return templates.TemplateResponse(template_name, {
        "request": request,
        "paper": paper,
        "session": session,
        "project": project,
        "summary_content": summary_content,
        "summary_html": summary_html,
        "debug_info": " | ".join(debug_info),
        "pdf_url": pdf_url,
        "note_content": note_content,
        "note_updated": note_updated,
    })


# ── 保存笔记 ────────────────────────────────────────────────────

@router.post("/{paper_id}/note")
async def save_note(request: Request, paper_id: int):
    from fastapi import Form
    db = request.app.state.db
    content = (await request.form()).get("content", "")
    db.save_note(paper_id, str(content))
    return RedirectResponse(url=f"/papers/{paper_id}", status_code=303)


# ── 补填下载失败的 PDF ──────────────────────────────────────────

@router.post("/{paper_id}/retry-upload")
async def retry_upload_pdf(request: Request, paper_id: int, file: UploadFile = File(...)):
    import uuid
    db = request.app.state.db
    paper = db.get_paper(paper_id)
    if not paper:
        return HTMLResponse("Paper not found", status_code=404)

    session = db.get_session(paper["session_id"])
    if not session:
        return HTMLResponse("Session not found", status_code=404)

    session_dir = BASE_DIR / session["session_dir"]
    papers_dir = session_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    # 用 arxiv_id 命名 PDF，确保 AGENT2 能找到
    arxiv_id = (paper.get("arxiv_id") or "unknown").replace("/", "_")
    safe_name = f"{arxiv_id}.pdf"
    dest = papers_dir / safe_name
    content = await file.read()
    dest.write_bytes(content)

    db.update_paper(paper_id, download_status="success", pdf_path=str(dest.relative_to(BASE_DIR)))
    return RedirectResponse(url=f"/sessions/{paper['session_id']}", status_code=303)


# ── 多源级联重试下载 ─────────────────────────────────────────

@router.post("/{paper_id}/retry-download")
async def retry_download_pdf(request: Request, paper_id: int):
    """用多源级联重新尝试下载单篇论文 PDF。"""
    db = request.app.state.db
    paper = db.get_paper(paper_id)
    if not paper:
        return HTMLResponse("Paper not found", status_code=404)

    session = db.get_session(paper["session_id"])
    if not session:
        return HTMLResponse("Session not found", status_code=404)

    session_dir = BASE_DIR / session["session_dir"]
    papers_dir = session_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    # 读取 config snapshot
    import yaml as _yaml
    config_path = session_dir / "config_snapshot.yaml"
    dl_cfg = {}
    if config_path.exists():
        try:
            cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            dl_cfg = cfg.get("downloader", {})
        except Exception:
            pass

    # 构造 PaperMeta
    from agent1.arxiv_searcher import PaperMeta
    from utils.multi_downloader import download_papers_batch_multi

    p = PaperMeta(
        arxiv_id=paper.get("arxiv_id", ""),
        title=paper.get("title", ""),
        authors=[],  # 下载不需要作者
        published=paper.get("published", ""),
        abstract=paper.get("abstract", ""),
        pdf_url=paper.get("source_url", ""),
        arxiv_url=paper.get("source_url", ""),
        doi=paper.get("doi", ""),
    )

    import asyncio
    results = await asyncio.to_thread(
        download_papers_batch_multi,
        papers=[p],
        output_dir=papers_dir,
        unpaywall_email=dl_cfg.get("unpaywall_email", ""),
        enable_scihub=dl_cfg.get("enable_scihub", False),
        scihub_mirrors=dl_cfg.get("scihub_mirrors", None),
        max_concurrent=1,
    )

    pdf_path = results.get(p.arxiv_id)
    if pdf_path:
        db.update_paper(paper_id, download_status="success",
                       pdf_path=str(pdf_path.relative_to(BASE_DIR)))
        return HTMLResponse(
            f"<div class='p-2 text-green-600 text-sm'>PDF 下载成功: {pdf_path.name}</div>"
        )
    else:
        return HTMLResponse(
            "<div class='p-2 text-red-500 text-sm'>所有来源均无法获取该论文 PDF</div>",
            status_code=502,
        )


# ── 服务 PDF 文件 ────────────────────────────────────────────────

@router.get("/{paper_id}/pdf")
async def serve_pdf(paper_id: int, request: Request):
    db = request.app.state.db
    paper = db.get_paper(paper_id)
    if not paper or not paper.get("pdf_path"):
        return HTMLResponse("PDF not available", status_code=404)

    pdf_path = Path(paper["pdf_path"])
    if not pdf_path.is_absolute():
        # 相对于项目根或 session 目录
        pdf_path = BASE_DIR / pdf_path

    if not pdf_path.exists():
        # 尝试从 session 目录的 papers/ 子目录找
        session = db.get_session(paper["session_id"])
        if session:
            session_dir = BASE_DIR / session["session_dir"]
            alt_path = session_dir / "papers" / pdf_path.name
            if alt_path.exists():
                pdf_path = alt_path

    if not pdf_path.exists():
        return HTMLResponse("PDF file not found on disk", status_code=404)

    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename=pdf_path.name,
    )


# ── 批量上传本地 PDF ─────────────────────────────────────────────

@router.post("/upload/{session_id}")
async def upload_papers(
    request: Request,
    session_id: int,
    files: list[UploadFile] = File(...),
):
    db = request.app.state.db
    session = db.get_session(session_id)
    if not session:
        return HTMLResponse("Session not found", status_code=404)

    session_dir = BASE_DIR / session["session_dir"]
    papers_dir = session_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    uploaded = 0
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            continue
        # UUID 命名防碰撞
        import uuid
        safe_name = f"{uuid.uuid4().hex[:12]}_{Path(f.filename).name}"
        dest = papers_dir / safe_name

        with open(dest, "wb") as out:
            content = await f.read()
            out.write(content)

        # 入库
        stem = dest.stem
        db.insert_papers(session_id, [{
            "arxiv_id": f"local_{stem}",
            "title": stem.replace("_", " ").replace("-", " "),
            "authors": ["未知"],
            "published": "",
            "abstract": "（本地上传文件）",
            "source_platform": "local",
            "pdf_path": str(dest.relative_to(BASE_DIR)),
            "download_status": "success",
        }])
        uploaded += 1

    return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)
