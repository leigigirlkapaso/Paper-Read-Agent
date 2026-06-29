"""
web/routes/papers.py
论文查看、PDF 服务、上传、搜索路由。
"""

from __future__ import annotations

import html
import shutil
from pathlib import Path

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse

router = APIRouter(prefix="/papers", tags=["papers"])

import sys as _sys
print(">>> MODULE_LOADED: paperreadagent.web.routes.papers (save_note debug version) <<<", file=_sys.stderr, flush=True)

from web.template_config import templates
from utils.ai_style_critique import critique_ai_style, rewrite_deai

BASE_DIR = Path(__file__).parent.parent.parent.parent

import re
import uuid as _uuid

_SAFE_ARXIV_ID_RE = re.compile(r'^[a-zA-Z0-9._-]+\Z')

def _safe_arxiv_id(raw: str | None) -> str:
    """Sanitize arxiv_id for use in filenames. Returns a UUID if invalid."""
    if not raw:
        return _uuid.uuid4().hex[:12]
    sanitized = raw.replace("/", "_")
    if '..' in sanitized or '\\' in sanitized or not _SAFE_ARXIV_ID_RE.match(sanitized):
        return _uuid.uuid4().hex[:12]
    return sanitized


def _load_summary_content(db, paper, session) -> str:
    """读取论文精读 summary 内容（多重回退，与 paper_detail 一致）。

    1. summaries 数据库表  2. summary_path 字段文件  3. session summaries 目录 glob。
    无内容时返回空字符串。
    """
    paper_id = paper["id"]

    # 1. 从 summaries 数据库表读取
    summaries = db.get_paper_summaries(paper_id)
    if summaries and summaries[0].get("content"):
        return summaries[0]["content"]

    # 2. 从 summary_path 字段读取
    summary_path_str = paper.get("summary_path", "")
    if summary_path_str:
        sp = Path(summary_path_str)
        if not sp.is_absolute():
            sp = BASE_DIR / sp
        if sp.exists():
            return sp.read_text(encoding="utf-8")

    # 3. 搜索 session summaries 目录
    if session:
        sdir = BASE_DIR / session["session_dir"] / "summaries"
        if sdir.exists():
            aid = _safe_arxiv_id(paper.get("arxiv_id"))
            for f in sdir.glob(f"{aid}*.md"):
                return f.read_text(encoding="utf-8")

    return ""


@router.post("/{paper_id}/ai-critique")
async def ai_critique(request: Request, paper_id: int):
    db = request.app.state.db
    paper = db.get_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="paper not found")
    session = db.get_session(paper["session_id"]) if paper.get("session_id") else None
    summary = _load_summary_content(db, paper, session)
    llm = request.app.state.core.llm
    return await critique_ai_style(summary, llm)


@router.post("/{paper_id}/ai-rewrite")
async def ai_rewrite(request: Request, paper_id: int):
    db = request.app.state.db
    paper = db.get_paper(paper_id)
    if not paper:
        raise HTTPException(status_code=404, detail="paper not found")
    session = db.get_session(paper["session_id"]) if paper.get("session_id") else None
    summary = _load_summary_content(db, paper, session)
    body = await request.json()
    critique = body.get("critique") or {}
    llm = request.app.state.core.llm
    rewritten = await rewrite_deai(summary, critique, llm)
    return {"rewritten": rewritten}


# ── 笔记索引 ─────────────────────────────────────────────────────

@router.get("/notes", response_class=HTMLResponse)
async def notes_index(request: Request, project_id: int | None = None):
    db = request.app.state.db
    notes = db.get_all_notes(project_id)
    projects = db.list_projects()
    return templates.TemplateResponse(request, "notes.html", {
        "notes": notes,
        "projects": projects,
        "selected_project": project_id,
    })


# ── 项目关系图 ─────────────────────────────────────────────────

@router.get("/graph", response_class=HTMLResponse)
async def project_graph(request: Request):
    """Render the graph page shell. Data is loaded asynchronously via
    GET /api/graph/data (see below)."""
    return templates.TemplateResponse(request, "graph.html", {})


@router.get("/api/graph/data")
async def graph_data(
    request: Request,
    layers: str = "project,paper",
    limit: int = 200,
    center: str | None = None,
    hops: int = 1,
    project_id: int | None = None,
):
    """Return nodes+edges for the project relationship graph.

    Query params:
      - layers: comma-separated, subset of {project,paper,note,spark}
      - limit:  total node budget (1..500)
      - center: optional 'paper_<id>' / 'project_<id>' / 'spark_<id>' for
                1-hop neighborhood mode (overrides full panorama)
      - hops:   neighbor depth (currently only 1 supported)
      - project_id: optional, limits panorama to one project
    """
    from fastapi.responses import JSONResponse
    from paperreadagent.web.services.graph_builder import (
        GraphBuilderService, GraphOptions,
    )

    db = request.app.state.db
    valid_layers = {"project", "paper", "note", "spark"}
    requested_layers = set((layers or "").split(",")) & valid_layers
    if not requested_layers:
        requested_layers = {"project", "paper"}

    options = GraphOptions(
        layers=requested_layers,
        limit=max(1, min(limit, 500)),
        center=center,
        hops=hops,
        project_id=project_id,
    )
    builder = GraphBuilderService(db)
    result = builder.build(options)
    return JSONResponse({
        "nodes": result.nodes,
        "edges": result.edges,
        "truncated": result.truncated,
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
    return templates.TemplateResponse(request, "history.html", {
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
            aid = _safe_arxiv_id(paper.get("arxiv_id"))
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
    return templates.TemplateResponse(request, template_name, {
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
    db = request.app.state.db
    # CSRF 中间件已把 form_data 挂到 request.state._csrf_form_data
    # Starlette 0.52.1 _form 缓存不可靠，从这里取最安全
    form_data = getattr(request.state, "_csrf_form_data", None)
    if form_data is not None:
        content = form_data.get("content", "")
    else:
        content = (await request.form()).get("content", "")
    db.save_note(paper_id, str(content))
    return RedirectResponse(url=f"/papers/{paper_id}", status_code=303)


# ── 补填下载失败的 PDF ──────────────────────────────────────────

@router.post("/{paper_id}/retry-upload")
async def retry_upload_pdf(request: Request, paper_id: int):
    """补填下载失败的 PDF。

    CSRF 中间件已消费 multipart body 并把 form_data 缓存在 request.state.
    Starlette 0.52.1 _form 缓存不可靠，FastAPI 的 File(...) 会拿不到文件。
    """
    import uuid
    db = request.app.state.db
    paper = db.get_paper(paper_id)
    if not paper:
        return HTMLResponse("Paper not found", status_code=404)

    session = db.get_session(paper["session_id"])
    if not session:
        return HTMLResponse("Session not found", status_code=404)

    form_data = getattr(request.state, "_csrf_form_data", None)
    if form_data is None:
        form_data = await request.form(max_part_size=50 * 1024 * 1024)
    file = form_data.get("file")
    if file is None or not hasattr(file, "read"):
        return HTMLResponse("缺少上传文件", status_code=400)

    session_dir = BASE_DIR / session["session_dir"]
    papers_dir = session_dir / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)

    # 用 arxiv_id 命名 PDF，确保 AGENT2 能找到
    safe_name = f"{_safe_arxiv_id(paper.get('arxiv_id'))}.pdf"
    dest = papers_dir / safe_name
    content = await file.read()
    if content[:4] != b'%PDF':
        return HTMLResponse("Invalid PDF file", status_code=400)
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
    cfg = {}
    dl_cfg = {}
    if config_path.exists():
        try:
            cfg = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
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
        contact_email=cfg.get("contact_email", "") or "",
    )

    pdf_path = results.get(p.arxiv_id)
    if pdf_path:
        db.update_paper(paper_id, download_status="success",
                       pdf_path=str(pdf_path.relative_to(BASE_DIR)))
        return HTMLResponse(
            f"<div class='p-2 text-green-600 text-sm'>PDF 下载成功: {html.escape(pdf_path.name)}</div>"
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
async def upload_papers(request: Request, session_id: int):
    db = request.app.state.db
    session = db.get_session(session_id)
    if not session:
        return HTMLResponse("Session not found", status_code=404)

    # Starlette 0.52.1 _form 缓存不可靠，从 CSRF 中间件的 request.state 读取
    form_data = getattr(request.state, "_csrf_form_data", None)
    if form_data is None:
        form_data = await request.form(max_part_size=50 * 1024 * 1024)
    files = form_data.getlist("files")

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
            if content[:4] != b'%PDF':
                continue
            out.write(content)

        # 入库
        stem = dest.stem
        pdf_rel = str(dest.relative_to(BASE_DIR))
        paper_ids = db.insert_papers(session_id, [{
            "arxiv_id": stem,
            "title": stem.replace("_", " ").replace("-", " "),
            "authors": ["未知"],
            "published": "unknown",
            "abstract": "（本地上传文件，无在线摘要，详见全文）",
            "source_platform": "local",
        }])
        # insert_papers 不含 download_status/pdf_path 列，手动补更新
        if paper_ids:
            db.update_paper(paper_ids[0], download_status="success", pdf_path=pdf_rel)
        uploaded += 1

    return RedirectResponse(url=f"/sessions/{session_id}", status_code=303)
