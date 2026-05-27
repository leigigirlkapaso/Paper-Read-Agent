"""
web/routes/projects.py
Dashboard + 项目 CRUD 路由。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Form
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
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "projects": proj_data,
    })


# ── 创建项目 ─────────────────────────────────────────────────────

@router.post("/", response_class=HTMLResponse)
async def create_project(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
):
    db = request.app.state.db
    db.create_project(name.strip(), description.strip())
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
    return templates.TemplateResponse("project_detail.html", {
        "request": request,
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
