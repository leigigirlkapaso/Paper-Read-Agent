"""
web/routes/auth_routes.py
登录 / 登出路由。
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from paperreadagent.web.auth import (
    hash_password, verify_password, make_session_cookie, COOKIE_NAME,
)
from web.template_config import templates

router = APIRouter(prefix="", tags=["auth"])


def _user_exists(request: Request) -> bool:
    row = request.app.state.core.db.conn.execute(
        "SELECT id FROM core_users WHERE id = 1"
    ).fetchone()
    return row is not None


def _validate_redirect(next_url: str) -> str:
    """Only allow relative paths to prevent open redirect attacks."""
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc or not next_url.startswith("/"):
        return "/projects/"
    return next_url


def _log_attempt(request: Request, ip: str, success: bool) -> None:
    """Write login attempt to core_login_attempts audit table."""
    try:
        request.app.state.core.db.conn.execute(
            "INSERT INTO core_login_attempts (ip_address, success) VALUES (?, ?)",
            (ip, 1 if success else 0),
        )
        request.app.state.core.db.conn.commit()
    except Exception:
        pass  # audit best-effort, don't block login flow


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    is_setup = not _user_exists(request)
    return templates.TemplateResponse(request, "login.html", {
        "is_setup": is_setup,
        "error": None,
    })


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...),
                       password_confirm: str = Form("")):
    is_setup = not _user_exists(request)
    guard = request.app.state.login_guard
    # 优先取 X-Forwarded-For 最左端（真实客户端 IP），支持反向代理
    xff = request.headers.get("X-Forwarded-For", "")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")

    # 初始设置阶段不限制（没有用户记录 = 首次启动）
    if not is_setup and guard.is_blocked(ip):
        _log_attempt(request, ip, False)
        return templates.TemplateResponse(request, "login.html", {
            "is_setup": False,
            "error": "尝试次数过多，请 15 分钟后再试。",
        }, status_code=429)

    if is_setup:
        if len(password) < 6:
            return templates.TemplateResponse(request, "login.html", {
                "is_setup": True,
                "error": "密码至少 6 位。",
            })
        if password != password_confirm:
            return templates.TemplateResponse(request, "login.html", {
                "is_setup": True,
                "error": "两次密码不一致。",
            })
        pwd_hash = hash_password(password)
        # INSERT OR REPLACE 避免两标签页同时设置密码时的 UNIQUE 冲突
        request.app.state.core.db.conn.execute(
            "INSERT OR REPLACE INTO core_users (id, password_hash) VALUES (1, ?)",
            (pwd_hash,)
        )
        request.app.state.core.db.conn.commit()
        guard.record_success(ip)
        _log_attempt(request, ip, True)
    else:
        row = request.app.state.core.db.conn.execute(
            "SELECT password_hash FROM core_users WHERE id = 1"
        ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            guard.record_failure(ip)
            _log_attempt(request, ip, False)
            return templates.TemplateResponse(request, "login.html", {
                "is_setup": False,
                "error": "密码错误。",
            })
        guard.record_success(ip)
        _log_attempt(request, ip, True)

    # Set cookie and redirect (validate next param to prevent open redirect)
    sv_row = request.app.state.core.db.conn.execute(
        "SELECT session_version FROM core_users WHERE id = 1"
    ).fetchone()
    session_version = sv_row["session_version"] if sv_row else 0
    token = make_session_cookie(1, request.app.state.server_secret, session_version)
    redirect_to = _validate_redirect(request.query_params.get("next", "/projects/"))
    resp = RedirectResponse(url=redirect_to, status_code=303)
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.get("/logout")
async def logout(request: Request):
    # 递增 session_version 使该用户所有旧 cookie 失效
    try:
        request.app.state.core.db.conn.execute(
            "UPDATE core_users SET session_version = session_version + 1 WHERE id = 1"
        )
        request.app.state.core.db.conn.commit()
    except Exception:
        pass  # best-effort
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp
