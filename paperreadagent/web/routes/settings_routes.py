"""
web/routes/settings_routes.py
设置页 — 修改密码、查看连接状态。
"""

from __future__ import annotations

import yaml
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from paperreadagent.web.auth import hash_password, verify_password, make_session_cookie, COOKIE_NAME
from web.template_config import templates

router = APIRouter(prefix="/settings", tags=["settings"])

BASE_DIR = Path(__file__).parent.parent.parent.parent


def _read_server_cfg():
    config_path = BASE_DIR / "config.yaml"
    host, port = "0.0.0.0", 8000
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        server_cfg = cfg.get("server", {})
        host = server_cfg.get("host", "0.0.0.0")
        port = server_cfg.get("port", 8000)
    return host, port


@router.get("/", response_class=HTMLResponse)
async def settings_page(request: Request):
    host, port = _read_server_cfg()
    return templates.TemplateResponse(request, "settings.html", {
        "server_host": host,
        "server_port": port,
        "pwd_error": None,
        "pwd_ok": None,
    })


@router.post("/change-password")
async def change_password(request: Request):
    """修改用户密码。

    CSRF 中间件已消费 body 并把 form_data 缓存在 request.state._csrf_form_data
    （Starlette 0.52.1 _form 缓存不可靠）。必须从 request.state 取，否则三个
    密码字段都会被识别为空字符串，让用户看到"新密码至少 6 位"的误导性错误。
    """
    form_data = getattr(request.state, "_csrf_form_data", None)
    if form_data is None:
        form_data = await request.form()
    old_password = str(form_data.get("old_password", ""))
    new_password = str(form_data.get("new_password", ""))
    new_password_confirm = str(form_data.get("new_password_confirm", ""))

    host, port = _read_server_cfg()

    if len(new_password) < 6:
        return templates.TemplateResponse(request, "settings.html", {
            "server_host": host,
            "server_port": port,
            "pwd_error": "新密码至少 6 位。",
            "pwd_ok": None,
        })

    if new_password != new_password_confirm:
        return templates.TemplateResponse(request, "settings.html", {
            "server_host": host,
            "server_port": port,
            "pwd_error": "两次密码输入不一致。",
            "pwd_ok": None,
        })

    row = request.app.state.core.db.conn.execute(
        "SELECT password_hash FROM core_users WHERE id = 1"
    ).fetchone()
    if not row or not verify_password(old_password, row["password_hash"]):
        return templates.TemplateResponse(request, "settings.html", {
            "server_host": host,
            "server_port": port,
            "pwd_error": "当前密码错误。",
            "pwd_ok": None,
        })

    new_hash = hash_password(new_password)
    request.app.state.core.db.conn.execute(
        "UPDATE core_users SET password_hash = ?, session_version = session_version + 1 WHERE id = 1",
        (new_hash,)
    )
    request.app.state.core.db.conn.commit()

    # Read new session_version and issue a fresh cookie so the user isn't logged out
    sv_row = request.app.state.core.db.conn.execute(
        "SELECT session_version FROM core_users WHERE id = 1"
    ).fetchone()
    new_sv = sv_row["session_version"] if sv_row else 1
    token = make_session_cookie(1, request.app.state.server_secret, new_sv)

    resp = templates.TemplateResponse(request, "settings.html", {
        "server_host": host,
        "server_port": port,
        "pwd_error": None,
        "pwd_ok": "密码已更新。",
    })
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
    )
    return resp
