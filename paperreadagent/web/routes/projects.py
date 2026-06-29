"""
web/routes/projects.py
Dashboard + 项目 CRUD 路由。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

router = APIRouter(prefix="/projects", tags=["projects"])

from web.template_config import templates


# ── Dashboard ──────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """项目列表 + 最近会话概览。"""
    db = request.app.state.db
    projects = db.list_projects()
    # 每个项目附带最近 5 个会话
    proj_data = []
    for p in projects:
        sessions = db.list_sessions(p["id"])[:5]
        stats = db.get_project_stats(p["id"])
        proj_data.append({
            **p,
            "sessions": sessions,
            "stats": stats,
        })
    return templates.TemplateResponse(request, "dashboard.html", {
        "projects": proj_data,
    })


# ── 创建项目 ─────────────────────────────────────────────────────

@router.post("/", response_class=HTMLResponse)
async def create_project(request: Request):
    """创建新项目。

    CSRF 中间件已消费 body 并把 form_data 缓存在 request.state._csrf_form_data
    （Starlette 0.52.1 _form 缓存不可靠，FastAPI 的 Form(...) 在此场景下会拿不到字段）。
    必须从 request.state 取，回退到再读一次 form 作为兜底。
    """
    form_data = getattr(request.state, "_csrf_form_data", None)
    if form_data is None:
        form_data = await request.form()
    name = str(form_data.get("name", "")).strip()
    description = str(form_data.get("description", "")).strip()
    if not name:
        return HTMLResponse("项目名称不能为空", status_code=400)
    db = request.app.state.db
    db.create_project(name, description)
    return RedirectResponse(url="/projects/", status_code=303)


# ── 项目详情 ─────────────────────────────────────────────────────

@router.get("/{project_id}", response_class=HTMLResponse)
async def project_detail(request: Request, project_id: int):
    db = request.app.state.db
    project = db.get_project(project_id)
    if not project:
        return HTMLResponse("<h2>Project not found</h2>", status_code=404)
    sessions = db.list_sessions(project_id)
    stats = db.get_project_stats(project_id)
    return templates.TemplateResponse(request, "project_detail.html", {
        "project": project,
        "sessions": sessions,
        "stats": stats,
    })


# ── 删除项目 ─────────────────────────────────────────────────────

@router.post("/{project_id}/delete")
async def delete_project(request: Request, project_id: int):
    db = request.app.state.db
    db.delete_project(project_id)
    return RedirectResponse(url="/projects/", status_code=303)
