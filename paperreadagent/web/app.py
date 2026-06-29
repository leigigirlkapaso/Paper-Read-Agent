"""
web/app.py
FastAPI 应用工厂，挂载路由和静态文件，集成核心层。
启动: uv run uvicorn paperreadagent.web.app:app --reload
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# 确保 paperreadagent 包和项目根在 sys.path
BASE_DIR = Path(__file__).parent.parent.parent
PACKAGE_DIR = Path(__file__).parent.parent
if str(PACKAGE_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGE_DIR))
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from db.database import Database
from web.routes import projects, sessions, papers
from paperreadagent.core import create_core
from paperreadagent.web.auth import (
    verify_cookie, LoginGuard, COOKIE_NAME, _get_server_secret,
    generate_csrf_token, validate_csrf, CSRF_COOKIE, CSRF_HEADER,
)
from web.template_config import templates


def create_app() -> FastAPI:
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        # startup
        import os, webbrowser
        app.state.core.scheduler.start()  # 幂等；确保在事件循环中启动
        # 补跑：后台执行，不阻塞启动
        async def _catch_up():
            try:
                from paperreadagent.modules.thinker.questions import QuestionGenerator
                from paperreadagent.modules.thinker.resolutions import ResolutionTracker
                core = app.state.core
                await QuestionGenerator(core).check_inactivity()
                await ResolutionTracker(core).check_daily_resolutions()
            except Exception:
                logger.warning("[App] 启动时补跑检查失败（LLM 可能不可用），调度器稍后会重试")
        asyncio.create_task(_catch_up())
        # 自动打开浏览器（测试/CI 环境跳过）
        if not os.environ.get("PRA_NO_BROWSER"):
            webbrowser.open("http://127.0.0.1:8000")
        yield
        # shutdown
        await app.state.core.scheduler.shutdown()
        app.state.core.db.close()
        app.state.db.close()

    app = FastAPI(title="PaperReadAgent", version="0.3.0", lifespan=_lifespan)

    # ── 数据库实例（应用生命周期内共享）────────────────────────
    db_path = BASE_DIR / "paperreadagent.db"
    app.state.db = Database(db_path)

    # ── 核心层单例 ─────────────────────────────────────────────
    config_path = BASE_DIR / "config.yaml"
    app.state.core = create_core(config_path=config_path, db_path=db_path)
    app.state.core.mount_app(app)
    app.state.core.legacy_db = app.state.db

    # ── 认证状态（cookie 签名密钥 + 登录防暴力破解）─────────────
    import yaml as _yaml_module
    _cfg = {}
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as _f:
            _cfg = _yaml_module.safe_load(_f) or {}
    app.state.server_secret = _get_server_secret(
        _cfg.get("server", {}), str(config_path) if config_path.exists() else ""
    )
    app.state.login_guard = LoginGuard()

    # ── 核心层前端注入中间件 ──────────────────────────────────
    @app.middleware("http")
    async def _inject_core_context(request: Request, call_next):
        core = request.app.state.core
        request.state.core_head_inject = core.frontend.get_head_inject()
        request.state.core_body_end_inject = core.frontend.get_body_end_inject()
        request.state.core_scripts_inject = core.frontend.get_scripts_inject()
        response = await call_next(request)
        return response

    # ── Auth 中间件 (注册在 inject 之后 = 外层 = 请求先经过) ─────
    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        path = request.url.path

        # 白名单放行: /login, /logout, /static/**
        _whitelisted = (
            path == "/login" or path == "/logout" or
            path.startswith("/static/")
        )
        request.state.is_authenticated = False

        if not _whitelisted:
            token = request.cookies.get(COOKIE_NAME)
            if not token or not verify_cookie(token, app.state.server_secret):
                return RedirectResponse(url=f"/login?next={path}", status_code=303)

            # 检查 session_version：改密码后旧 cookie 失效
            payload = verify_cookie(token, app.state.server_secret)
            if payload:
                sv_row = app.state.core.db.conn.execute(
                    "SELECT session_version FROM core_users WHERE id = 1"
                ).fetchone()
                db_sv = sv_row["session_version"] if sv_row else 0
                if payload.get("sv", 0) != db_sv:
                    resp = RedirectResponse(url=f"/login?next={path}", status_code=303)
                    resp.delete_cookie(COOKIE_NAME)
                    return resp

            request.state.is_authenticated = True

        # 移动端检测：仅用于服务端优化（如选择模板），实际显示由 CSS media query 控制
        # desktop-only / mobile-only 类处理显示/隐藏，无需依赖 UA
        request.state.is_mobile = False  # 不再基于 UA 检测；移动端全由 CSS 控制

        response = await call_next(request)
        return response

    # ── CSRF 中间件（double-submit cookie）─────────────────────
    @app.middleware("http")
    async def _csrf_middleware(request: Request, call_next):
        path = request.url.path

        # 豁免：安全方法（GET/HEAD/OPTIONS）仅设置 cookie
        if request.method in ("GET", "HEAD", "OPTIONS"):
            csrf_token = request.cookies.get(CSRF_COOKIE)
            if not csrf_token:
                csrf_token = generate_csrf_token()
            response = await call_next(request)
            response.set_cookie(
                CSRF_COOKIE, csrf_token,
                max_age=86400,  # 24h
                httponly=False,  # JS 可读（前端可放入 header）
                samesite="lax",
                secure=False,
            )
            return response

        # 豁免路径：登录登出、静态资源、ideator API（已受 auth cookie 保护）
        if path in ("/login", "/logout") or path.startswith("/ideator/api/"):
            return await call_next(request)

        # 校验 POST/PUT/PATCH/DELETE
        cookie_token = request.cookies.get(CSRF_COOKIE)
        client_token = None

        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            try:
                # max_part_size=50MB：PDF 上传默认限制 1MB，必须调大
                form_data = await request.form(max_part_size=50 * 1024 * 1024)
                client_token = form_data.get("_csrf_token")
                # Starlette 0.52.1 _form 缓存不稳定，手动挂到 request.state 供路由层使用
                request.state._csrf_form_data = form_data
            except Exception:
                pass

        if not client_token:
            client_token = request.headers.get(CSRF_HEADER)

        if not validate_csrf(request, cookie_token, client_token):
            from fastapi.responses import HTMLResponse
            return HTMLResponse(
                "<html><body style='font-family:sans-serif;max-width:500px;margin:80px auto;text-align:center'>"
                "<h2 style='color:#e53e3e'>CSRF 验证失败</h2>"
                "<p>请刷新页面后重试。如果问题持续，请清除浏览器缓存和 Cookie。</p>"
                "<a href='/' style='color:#4f46e5'>← 返回首页</a>"
                "</body></html>",
                status_code=403,
            )

        response = await call_next(request)
        return response

    # 根路径 → Dashboard
    @app.get("/")
    async def root():
        return RedirectResponse(url="/projects/")

    # ── 模块自动发现与注册 ─────────────────────────────────────
    modules_dir = PACKAGE_DIR / "modules"
    if modules_dir.exists():
        for mod_dir in sorted(modules_dir.iterdir()):
            if not mod_dir.is_dir() or mod_dir.name.startswith("_"):
                continue
            init_file = mod_dir / "__init__.py"
            if not init_file.exists():
                continue
            try:
                mod = importlib.import_module(f"modules.{mod_dir.name}")
                if hasattr(mod, "register"):
                    info = mod.register(app.state.core)
                    logger.info(f"[App] 模块已加载: {info.name} v{info.version}")
            except Exception:
                logger.exception(f"[App] 模块加载失败: {mod_dir.name}")

    # 挂载静态文件
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 挂载模块静态文件目录
    if modules_dir.exists():
        for mod_dir in modules_dir.iterdir():
            if mod_dir.is_dir() and (mod_dir / "static").exists():
                app.mount(
                    f"/{mod_dir.name}/static",
                    StaticFiles(directory=str(mod_dir / "static")),
                    name=f"static_{mod_dir.name}",
                )

    # 挂载路由
    app.include_router(projects.router, tags=["projects"])
    app.include_router(sessions.router, tags=["sessions"])
    app.include_router(papers.router, tags=["papers"])
    from web.routes import auth_routes
    app.include_router(auth_routes.router, tags=["auth"])
    from web.routes import settings_routes
    app.include_router(settings_routes.router, tags=["settings"])

    # ── 统一错误页面 ─────────────────────────────────────────────
    @app.exception_handler(404)
    async def _not_found_handler(request: Request, exc):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": 404, "message": "页面不存在"},
            status_code=404,
        )

    @app.exception_handler(500)
    async def _server_error_handler(request: Request, exc):
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": 500, "message": "服务器内部错误"},
            status_code=500,
        )

    return app


app = create_app()


def main():
    import uvicorn
    import yaml

    config_path = BASE_DIR / "config.yaml"
    host = "0.0.0.0"
    port = 8000
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        server_cfg = cfg.get("server", {})
        host = server_cfg.get("host", "0.0.0.0")
        port = server_cfg.get("port", 8000)

    uvicorn.run("paperreadagent.web.app:app", host=host, port=port, reload=True)
