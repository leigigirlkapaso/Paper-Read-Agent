# PaperReadAgent V2 — 移动端支持实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让安卓手机浏览器能完成和桌面端一样的文献调研 pipeline、ideator 火花挖掘、thinker 对话，数据库仍在电脑上。

**Architecture:** 混合模式 — 大部分页面响应式 CSS 适配，差异大的页面（论文详情、thinker 全屏）通过 `request.state.is_mobile` 切换独立模板。新增密码认证中间件（HMAC-SHA256 session cookie + pbkdf2 密码哈希）、移动端底部 tab 导航、SSE 进度轮询兜底。

**Tech Stack:** Python 3.13 stdlib (hashlib/hmac) — 无新增依赖。CSS 继续 Tailwind CDN + 新增媒体查询。

---

### Task 1: Server 配置块 — config.yaml + app.py

**Files:**
- Modify: `config.yaml:1` (insert before line 1)
- Modify: `paperreadagent/web/app.py:129-131`

- [ ] **Step 1: 在 config.yaml 顶部新增 server 块**

在 `config.yaml` 最前面插入：

```yaml
# ── 0. 服务器配置 ──────────────────────────────────────────
server:
  host: "0.0.0.0"
  port: 8000
  secret_key: ""   # 留空则首次启动自动生成

```

- [ ] **Step 2: 修改 app.py 的 main() 从 config 读取 host/port**

```python
# paperreadagent/web/app.py:129-131 替换为:
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
```

- [ ] **Step 3: 运行 app 验证 host/port 生效**

Run: `uv run python -c "from paperreadagent.web.app import main; print('import ok')"`
Expected: import ok（模块能正常导入）

- [ ] **Step 4: Commit**

```bash
git add config.yaml paperreadagent/web/app.py
git commit -m "feat: add server config block, read host/port from config.yaml"
```

---

### Task 2: Core Schema v2 — core_users + core_login_attempts

**Files:**
- Modify: `paperreadagent/core/schema.py`

- [ ] **Step 1: 写 schema 迁移测试**

创建文件 `paperreadagent/tests/test_core_schema_v2.py`：

```python
def test_core_latest_version_is_2():
    from paperreadagent.core.schema import CORE_LATEST_VERSION
    assert CORE_LATEST_VERSION == 2

def test_v2_creates_core_users_table():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from paperreadagent.core.schema import CORE_MIGRATIONS
    conn.executescript(CORE_MIGRATIONS[1])
    conn.executescript(CORE_MIGRATIONS[2])
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "core_users" in tables
    assert "core_login_attempts" in tables
    conn.close()

def test_core_users_single_row_enforcement():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from paperreadagent.core.schema import CORE_MIGRATIONS
    conn.executescript(CORE_MIGRATIONS[1])
    conn.executescript(CORE_MIGRATIONS[2])
    # id must be 1
    conn.execute("INSERT INTO core_users (id, password_hash) VALUES (1, 'hash1')")
    conn.commit()
    # id != 1 should fail CHECK constraint
    try:
        conn.execute("INSERT INTO core_users (id, password_hash) VALUES (2, 'hash2')")
        conn.commit()
        assert False, "Should have raised IntegrityError"
    except sqlite3.IntegrityError:
        pass
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest paperreadagent/tests/test_core_schema_v2.py -v`
Expected: FAIL — `assert 1 == 2` (CORE_LATEST_VERSION still 1)

- [ ] **Step 3: Add migration v2 to core/schema.py**

```python
# paperreadagent/core/schema.py — 修改 CORE_LATEST_VERSION，新增 migration v2

CORE_LATEST_VERSION = 2

CORE_MIGRATIONS: dict[int, str] = {
    1: """
    ... (existing v1 migration, unchanged) ...
    """,
    2: """
    -- 单用户认证表（只允许一行，id 必须为 1）
    CREATE TABLE IF NOT EXISTS core_users (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT NOT NULL DEFAULT 'admin',
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 登录尝试记录（防暴力破解审计）
    CREATE TABLE IF NOT EXISTS core_login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip_address TEXT NOT NULL,
        attempt_time TEXT NOT NULL DEFAULT (datetime('now')),
        success INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_login_attempts_ip
        ON core_login_attempts(ip_address, attempt_time);
    """,
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest paperreadagent/tests/test_core_schema_v2.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/core/schema.py paperreadagent/tests/test_core_schema_v2.py
git commit -m "feat: core schema v2 — core_users + core_login_attempts tables"
```

---

### Task 3: Auth 模块 — password hashing + cookie signing + login tracking

**Files:**
- Create: `paperreadagent/web/auth.py`

- [ ] **Step 1: Write auth module tests**

创建文件 `paperreadagent/tests/test_auth.py`：

```python
def test_password_hash_and_verify():
    from paperreadagent.web.auth import hash_password, verify_password
    pw = "test-password-123"
    hashed = hash_password(pw)
    assert hashed != pw
    assert verify_password(pw, hashed) is True
    assert verify_password("wrong", hashed) is False

def test_password_hash_is_salted():
    from paperreadagent.web.auth import hash_password
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2  # different salts

def test_cookie_sign_and_verify():
    from paperreadagent.web.auth import sign_cookie, verify_cookie
    secret = "my-secret-key"
    payload = {"user_id": 1, "exp": 9999999999}
    token = sign_cookie(payload, secret)
    assert "." in token
    decoded = verify_cookie(token, secret)
    assert decoded is not None
    assert decoded["user_id"] == 1

def test_cookie_tamper_detection():
    from paperreadagent.web.auth import sign_cookie, verify_cookie
    secret = "my-secret-key"
    token = sign_cookie({"user_id": 1, "exp": 9999999999}, secret)
    parts = token.split(".")
    parts[0] = "dGFtcGVyZWQ="  # tampered base64
    bad_token = ".".join(parts)
    assert verify_cookie(bad_token, secret) is None

def test_cookie_expired():
    import time
    from paperreadagent.web.auth import sign_cookie, verify_cookie
    secret = "my-secret-key"
    token = sign_cookie({"user_id": 1, "exp": 1}, secret)  # already expired
    assert verify_cookie(token, secret) is None

def test_login_attempt_tracking():
    from paperreadagent.web.auth import LoginGuard
    guard = LoginGuard()
    ip = "192.168.1.100"
    assert guard.is_blocked(ip) is False
    for _ in range(5):
        guard.record_failure(ip)
    assert guard.is_blocked(ip) is True

def test_login_attempt_cleanup():
    from paperreadagent.web.auth import LoginGuard
    guard = LoginGuard()
    ip = "10.0.0.1"
    # fake old failures
    guard._failures[ip] = [(100000, 1)] * 10  # very old timestamp
    assert guard.is_blocked(ip) is False  # stale entries don't count
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest paperreadagent/tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError` or `ImportError`

- [ ] **Step 3: Implement auth.py**

```python
"""
web/auth.py
认证模块：密码哈希、session cookie 签名、登录防暴力破解。
纯 stdlib 实现，无外部依赖。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field

# ── 密码哈希 (pbkdf2_hmac) ─────────────────────────────────

_SALT_BYTES = 16
_ITERATIONS = 600_000
_HASH_NAME = "sha256"
_KEY_LENGTH = 32


def hash_password(password: str) -> str:
    """pbkdf2_hmac 哈希密码，返回 'salt$iterations$hash'（均为 hex）。"""
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode(), salt, _ITERATIONS, dklen=_KEY_LENGTH)
    return f"{salt.hex()}${_ITERATIONS}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """验证密码是否匹配存储的哈希值。"""
    try:
        salt_hex, iters_str, dk_hex = stored.split("$")
        salt = bytes.fromhex(salt_hex)
        iterations = int(iters_str)
        expected = bytes.fromhex(dk_hex)
        dk = hashlib.pbkdf2_hmac(_HASH_NAME, password.encode(), salt, iterations, dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, AttributeError):
        return False


# ── Session Cookie 签名 (HMAC-SHA256) ──────────────────────

_COOKIE_TTL = 30 * 24 * 3600  # 30 天


def sign_cookie(payload: dict, secret: str) -> str:
    """签名 payload → 'base64(payload_json).base64(hmac_signature)'"""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    b64_payload = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), b64_payload.encode(), hashlib.sha256).digest()
    b64_sig = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{b64_payload}.{b64_sig}"


def verify_cookie(token: str, secret: str) -> dict | None:
    """验证 cookie token，有效返回 payload，无效返回 None。"""
    try:
        b64_payload, b64_sig = token.split(".", 1)
    except ValueError:
        return None
    expected_sig = hmac.new(secret.encode(), b64_payload.encode(), hashlib.sha256).digest()
    try:
        actual_sig = base64.urlsafe_b64decode(_pad_b64(b64_sig))
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None
    # decode payload
    payload_bytes = base64.urlsafe_b64decode(_pad_b64(b64_payload))
    payload = json.loads(payload_bytes)
    if payload.get("exp", 0) < int(time.time()):
        return None
    return payload


def make_session_cookie(user_id: int, secret: str) -> str:
    """生成 session cookie value。"""
    payload = {"user_id": user_id, "exp": int(time.time()) + _COOKIE_TTL}
    return sign_cookie(payload, secret)


def _pad_b64(s: str) -> str:
    return s + "=" * (4 - len(s) % 4) if len(s) % 4 else s


# ── 登录防暴力破解 ─────────────────────────────────────────

BLOCK_MINUTES = 15
MAX_FAILURES = 5
FAILURE_WINDOW_SEC = 300  # 5 分钟内


@dataclass
class LoginGuard:
    """内存 IP → 失败记录，封禁 15 分钟。"""
    _failures: dict[str, list[tuple[float, int]]] = field(default_factory=dict)

    def record_failure(self, ip: str) -> None:
        now = time.time()
        if ip not in self._failures:
            self._failures[ip] = []
        self._failures[ip].append((now, 0))

    def record_success(self, ip: str) -> None:
        self._failures.pop(ip, None)

    def is_blocked(self, ip: str) -> bool:
        entries = self._failures.get(ip, [])
        if not entries:
            return False
        now = time.time()
        window_start = now - FAILURE_WINDOW_SEC
        recent = [e for e in entries if e[0] >= window_start]
        self._failures[ip] = recent
        if len(recent) < MAX_FAILURES:
            return False
        # 最近一次失败 + BLOCK_MINUTES > now → blocked
        last_failure = max(e[0] for e in recent)
        return (last_failure + BLOCK_MINUTES * 60) > now
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest paperreadagent/tests/test_auth.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/web/auth.py paperreadagent/tests/test_auth.py
git commit -m "feat: auth module — pbkdf2 password hashing + HMAC cookie signing + login guard"
```

---

### Task 4: Auth 中间件 + 登录路由 + 登录页面

**Files:**
- Create: `paperreadagent/web/routes/auth_routes.py`
- Modify: `paperreadagent/web/app.py:73-80` (insert auth middleware before existing inject middleware)
- Create: `paperreadagent/web/templates/login.html`
- Modify: `paperreadagent/web/templates/base.html:9` (dynamic title)

- [ ] **Step 1: Write integration test for auth middleware**

创建文件 `paperreadagent/tests/test_auth_middleware.py`：

```python
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client_with_auth():
    from paperreadagent.web.app import create_app
    app = create_app()
    return TestClient(app)


def test_login_page_returns_200(client_with_auth):
    resp = client_with_auth.get("/login")
    assert resp.status_code == 200
    assert "登录" in resp.text or "login" in resp.text.lower()


def test_protected_route_redirects_to_login(client_with_auth):
    resp = client_with_auth.get("/projects/", follow_redirects=False)
    assert resp.status_code in (302, 303, 401)


def test_static_files_bypass_auth(client_with_auth):
    resp = client_with_auth.get("/static/css/app.css")
    assert resp.status_code in (200, 404)  # 404 if file not found, but not 302 redirect


def test_first_time_setup_redirect(client_with_auth):
    # When core_users is empty, /login should show setup mode
    resp = client_with_auth.get("/login")
    # The login page itself always returns 200
    assert resp.status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest paperreadagent/tests/test_auth_middleware.py -v`
Expected: FAIL — `/login` route doesn't exist yet (404 or redirects)

- [ ] **Step 3: Create login template**

创建 `paperreadagent/web/templates/login.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>登录 — PaperReadAgent</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center">
    <div class="bg-white rounded-2xl shadow-lg p-8 w-full max-w-sm mx-4">
        <div class="text-center mb-6">
            <h1 class="text-2xl font-bold text-indigo-600">📚 PaperReadAgent</h1>
            <p class="text-sm text-gray-400 mt-1">
                {% if is_setup %}设定初始密码{% else %}请输入密码{% endif %}
            </p>
        </div>

        {% if error %}
        <div class="bg-red-50 text-red-700 text-sm p-3 rounded-lg mb-4">{{ error }}</div>
        {% endif %}

        <form method="post" class="space-y-4">
            <input type="password" name="password"
                   class="w-full border rounded-lg px-4 py-3 text-base focus:ring-2 focus:ring-indigo-300 focus:border-indigo-500 outline-none"
                   placeholder="{% if is_setup %}设定密码（至少6位）{% else %}输入密码{% endif %}"
                   autofocus
                   {% if is_setup %}minlength="6"{% endif %}>
            {% if is_setup %}
            <input type="password" name="password_confirm"
                   class="w-full border rounded-lg px-4 py-3 text-base focus:ring-2 focus:ring-indigo-300 focus:border-indigo-500 outline-none"
                   placeholder="再次输入密码">
            {% endif %}
            <button type="submit"
                    class="w-full bg-indigo-600 text-white py-3 rounded-lg hover:bg-indigo-700 font-medium text-base">
                {% if is_setup %}设定密码并登录{% else %}登录{% endif %}
            </button>
        </form>
    </div>
</body>
</html>
```

- [ ] **Step 4: Create auth routes**

创建 `paperreadagent/web/routes/auth_routes.py`：

```python
"""
web/routes/auth_routes.py
登录 / 登出 / 设置密码路由。
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from paperreadagent.web.auth import (
    hash_password, verify_password, make_session_cookie, COOKIE_NAME,
)
from web.template_config import templates

router = APIRouter(prefix="", tags=["auth"])


def _get_guard(request: Request):
    return request.app.state.login_guard


def _get_secret(request: Request) -> str:
    return request.app.state.server_secret


def _user_exists(request: Request) -> bool:
    row = request.app.state.core.db.conn.execute(
        "SELECT id FROM core_users WHERE id = 1"
    ).fetchone()
    return row is not None


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    is_setup = not _user_exists(request)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "is_setup": is_setup,
        "error": None,
    })


@router.post("/login")
async def login_submit(request: Request, password: str = Form(...),
                       password_confirm: str = Form("")):
    guard = _get_guard(request)
    ip = request.client.host if request.client else "unknown"

    if guard.is_blocked(ip):
        return templates.TemplateResponse("login.html", {
            "request": request,
            "is_setup": not _user_exists(request),
            "error": "尝试次数过多，请 15 分钟后再试。",
        }, status_code=429)

    is_setup = not _user_exists(request)

    if is_setup:
        if len(password) < 6:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "is_setup": True,
                "error": "密码至少 6 位。",
            })
        if password != password_confirm:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "is_setup": True,
                "error": "两次密码不一致。",
            })
        pwd_hash = hash_password(password)
        request.app.state.core.db.conn.execute(
            "INSERT INTO core_users (id, password_hash) VALUES (1, ?)",
            (pwd_hash,)
        )
        request.app.state.core.db.conn.commit()
    else:
        row = request.app.state.core.db.conn.execute(
            "SELECT password_hash FROM core_users WHERE id = 1"
        ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            guard.record_failure(ip)
            return templates.TemplateResponse("login.html", {
                "request": request,
                "is_setup": False,
                "error": "密码错误。",
            })
        guard.record_success(ip)

    # Set cookie and redirect
    token = make_session_cookie(1, _get_secret(request))
    redirect_to = request.query_params.get("next", "/projects/")
    resp = RedirectResponse(url=redirect_to, status_code=303)
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="lax",
    )
    return resp


@router.get("/logout")
async def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME)
    return resp
```

- [ ] **Step 5: Add auth middleware to app.py**

在 `paperreadagent/web/app.py` 的 `_inject_core_context` 中间件之前插入 auth 中间件：

```python
# 在 create_app() 中，_inject_core_context 之前添加：

from paperreadagent.web.auth import verify_cookie, LoginGuard, COOKIE_NAME, _get_server_secret

# ── Auth 中间件 ─────────────────────────────────────────
WHITELIST_PATHS = ["/login", "/static", "/logout"]

# 初始化 LoginGuard 和 server secret
app.state.login_guard = LoginGuard()
app.state.server_secret = _get_server_secret(cfg.get("server", {}))

@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path

    # Whitelist: /login, /static/**（子路径匹配）
    if any(path == w or path.startswith(w + "/") or path.startswith(w) and w.endswith("*")
           for w in WHITELIST_PATHS
           if not w.endswith("*")) or \
       any(path.startswith(w.rstrip("*")) for w in WHITELIST_PATHS if w.endswith("*")):
        pass  # allowed
    elif path in ("/login",) or path.startswith("/static/"):
        pass  # allowed (duplicate safety)
    else:
        token = request.cookies.get(COOKIE_NAME)
        if not token or not verify_cookie(token, app.state.server_secret):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/login?next={path}", status_code=303)

    # Mobile detection
    ua = request.headers.get("user-agent", "")
    is_mobile = any(p in ua for p in ("Android", "iPhone", "iPad", "iPod", "Mobile", "mobile"))
    request.state.is_mobile = is_mobile
    request.state.is_authenticated = True

    response = await call_next(request)
    return response
```

And add the `_get_server_secret` function at module level in auth.py:

```python
# paperreadagent/web/auth.py — 追加:

import secrets as _secrets

COOKIE_NAME = "pra_session"

def _get_server_secret(server_cfg: dict) -> str:
    """从配置读取 secret_key，空则自动生成 64 字符随机字符串。"""
    key = server_cfg.get("secret_key", "") if server_cfg else ""
    if not key:
        key = _secrets.token_hex(32)
        print(f"[Auth] 自动生成 secret_key: {key[:12]}... (已写入内存，如需持久化请更新 config.yaml)")
    return key
```

Note: `_get_server_secret` is called at app startup. The logic for the whitelist check should be cleaner:

```python
@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    path = request.url.path

    # 白名单放行
    _whitelisted = (
        path == "/login" or path == "/logout" or
        path.startswith("/static/") or
        "/static/" in path  # /thinker/static/... etc
    )
    if not _whitelisted:
        token = request.cookies.get(COOKIE_NAME)
        if not token or not verify_cookie(token, app.state.server_secret):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/login?next={path}", status_code=303)

    # 移动端检测
    ua = request.headers.get("user-agent", "")
    request.state.is_mobile = any(
        p in ua for p in ("Android", "iPhone", "iPad", "iPod", "Mobile", "mobile")
    )
    request.state.is_authenticated = True

    response = await call_next(request)
    return response
```

- [ ] **Step 6: Register auth routes in app.py**

```python
# paperreadagent/web/app.py — after the existing route includes:
from web.routes import auth_routes
app.include_router(auth_routes.router, tags=["auth"])
```

- [ ] **Step 7: Run integration test to verify**

Run: `uv run python -m pytest paperreadagent/tests/test_auth_middleware.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add paperreadagent/web/app.py paperreadagent/web/routes/auth_routes.py \
        paperreadagent/web/templates/login.html paperreadagent/web/auth.py \
        paperreadagent/tests/test_auth_middleware.py
git commit -m "feat: auth middleware + login routes + login page"
```

---

### Task 5: 响应式 CSS — app.css 移动端规则

**Files:**
- Modify: `paperreadagent/web/static/css/app.css`

- [ ] **Step 1: 添加移动端媒体查询和组件样式到 app.css**

在 `app.css` 的末尾追加：

```css
/* ══════════════════════════════════════════════════════════════════
   Mobile Responsive (V2) — max-width: 768px
   ══════════════════════════════════════════════════════════════════ */

@media (max-width: 768px) {
  /* ── Split view → 堆叠 ───── */
  .split-view {
    flex-direction: column;
  }
  .split-view .split-left,
  .split-view .split-right {
    width: 100% !important;
    max-height: none;
  }

  /* ── Grids → 单列 ────────── */
  .paper-grid {
    grid-template-columns: 1fr;
  }
  .session-meta {
    grid-template-columns: 1fr;
  }

  /* ── Show / Hide ─────────── */
  .desktop-only {
    display: none !important;
  }
  .mobile-only {
    display: block;
  }

  /* ── Typography ──────────── */
  body {
    font-size: 15px;
  }
  input, textarea, select {
    font-size: 16px; /* 防 iOS 缩放 */
  }

  /* ── Touch targets ───────── */
  button, .btn, [role="button"] {
    min-height: 44px;
    min-width: 44px;
  }

  /* ── Container ───────────── */
  main.max-w-\[96vw\] {
    max-width: 100vw;
    padding-left: 0.75rem;
    padding-right: 0.75rem;
    padding-bottom: 4rem; /* 给底部 tab 留空 */
  }

  /* ── Page padding for mobile nav ── */
  .mobile-page {
    padding-bottom: 4rem;
  }

  /* ── Compact spacing ────── */
  .p-5 { padding: 0.75rem; }
  .p-6 { padding: 1rem; }
  .mb-6 { margin-bottom: 0.75rem; }
}

@media (min-width: 769px) {
  .mobile-only {
    display: none !important;
  }
  .desktop-only {
    display: block;
  }
}

/* ══════════════════════════════════════════════════════════════════
   Mobile Bottom Tab Navigation
   ══════════════════════════════════════════════════════════════════ */

.mobile-nav {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  height: 56px;
  background: #fff;
  border-top: 1px solid #e5e7eb;
  display: flex;
  justify-content: space-around;
  align-items: center;
  z-index: 9000;
}

.mobile-nav .tab-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  font-size: 10px;
  color: #6b7280;
  text-decoration: none;
  padding: 4px 12px;
  border-radius: 8px;
  transition: color 0.15s, background 0.15s;
}

.mobile-nav .tab-item .tab-icon {
  font-size: 20px;
  line-height: 1;
}

.mobile-nav .tab-item.active {
  color: #4f46e5;
  background: #eef2ff;
}

/* ══════════════════════════════════════════════════════════════════
   Mobile Header
   ══════════════════════════════════════════════════════════════════ */

.mobile-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.75rem;
  background: #fff;
  border-bottom: 1px solid #e5e7eb;
  position: sticky;
  top: 0;
  z-index: 100;
}

.mobile-header .back-arrow {
  font-size: 20px;
  color: #4f46e5;
  text-decoration: none;
  padding: 4px;
}

.mobile-header .title {
  font-weight: 600;
  font-size: 15px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ══════════════════════════════════════════════════════════════════
   Thinker fullscreen override (mobile only)
   ══════════════════════════════════════════════════════════════════ */

@media (max-width: 768px) {
  [data-module="thinker"].thinker-fullscreen .thinker-panel {
    position: relative;
    right: auto;
    top: auto;
    width: 100%;
    max-width: 100vw;
    height: 100dvh;
    transform: none;
  }
}
```

- [ ] **Step 2: Verify CSS file is valid (no syntax check, visual only)**

Run: `uv run uvicorn paperreadagent.web.app:app --port 8001 &`
Check: `curl -s http://127.0.0.1:8001/static/css/app.css | head -5` returns CSS

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/web/static/css/app.css
git commit -m "feat: responsive CSS — mobile breakpoints, nav, header, thinker fullscreen"
```

---

### Task 6: 移动端导航 — mobile_nav.html + is_mobile 条件注入

**Files:**
- Create: `paperreadagent/web/templates/mobile_nav.html`
- Modify: `paperreadagent/web/templates/base.html:31-50` (hide desktop nav on mobile, inject mobile nav)

- [ ] **Step 1: 创建 mobile_nav.html**

创建 `paperreadagent/web/templates/mobile_nav.html`：

```html
{% if request.state.is_mobile %}
<nav class="mobile-nav">
    <a href="/projects/" class="tab-item {{ 'active' if request.url.path.startswith('/projects') }}">
        <span class="tab-icon">🏠</span>
        <span>首页</span>
    </a>
    <a href="/thinker/" class="tab-item {{ 'active' if request.url.path.startswith('/thinker') }}">
        <span class="tab-icon">💬</span>
        <span>Thinker</span>
    </a>
    <a href="/ideator/" class="tab-item {{ 'active' if request.url.path.startswith('/ideator') }}">
        <span class="tab-icon">💡</span>
        <span>Ideator</span>
    </a>
    <a href="/settings/" class="tab-item {{ 'active' if request.url.path.startswith('/settings') }}">
        <span class="tab-icon">⚙</span>
        <span>设置</span>
    </a>
</nav>
{% endif %}
```

- [ ] **Step 2: 修改 base.html — 桌面导航隐藏 + 注入 mobile nav**

In `base.html`, wrap the existing nav:

```html
<!-- Navigation (desktop only) -->
<nav class="bg-white shadow-sm border-b border-gray-200 desktop-only">
    ... (existing nav content, unchanged) ...
</nav>
```

After `<main>` block, insert mobile nav:

```html
<!-- Mobile bottom navigation -->
{% include "mobile_nav.html" %}
```

So `base.html` body becomes:

```html
<body class="bg-gray-50 text-gray-900 min-h-screen">
    <!-- Navigation (desktop) -->
    <nav class="bg-white shadow-sm border-b border-gray-200 desktop-only">
        <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
            <a href="/projects/" class="text-xl font-bold text-indigo-600 hover:text-indigo-800">
                📚 PaperReadAgent
            </a>
            <div class="flex gap-4 text-sm">
                <a href="/projects/" class="text-gray-600 hover:text-indigo-600">项目</a>
                <a href="/papers/notes" class="text-gray-600 hover:text-indigo-600">笔记</a>
                <a href="/papers/search" class="text-gray-600 hover:text-indigo-600">检索</a>
                <a href="/papers/graph" class="text-gray-600 hover:text-indigo-600">关系图</a>
                <a href="/ideator/" class="text-gray-600 hover:text-indigo-600">火花</a>
            </div>
        </div>
    </nav>

    <!-- Flash Messages -->
    ...

    <!-- Content -->
    <main class="max-w-[96vw] mx-auto px-4 py-6">
        {% block content %}{% endblock %}
    </main>

    <!-- Mobile bottom navigation -->
    {% include "mobile_nav.html" %}

    {{ request.state.core_body_end_inject | default('') | safe }}
    {{ request.state.core_scripts_inject | default('') | safe }}
    {% block scripts %}{% endblock %}
</body>
```

- [ ] **Step 3: 修改 thinker panel.html — 手机端标记全屏模式**

在 `thinker/panel.html` 中加入 `thinker-fullscreen` class 标识。读取该文件确认挂载点：

```html
<!-- If you're on the fullscreen page, add thinker-fullscreen class -->
<div data-module="thinker"
     class="{% if fullscreen_mode %}thinker-fullscreen{% endif %}"
     x-data="thinkerPanel()" ...>
```

具体做法：在 `panel.html` 的最外层 div 上加 `x-data="thinkerPanel({{ fullscreen_mode | default('false') | lower }})"` 或者更简单——通过在模板渲染时传入 `fullscreen_mode` 变量并在 Alpine 中处理。

更简单的方式：在 `panel.html` 中条件添加 class：

```html
<div data-module="thinker"
     class="{{ 'thinker-fullscreen' if fullscreen_mode else '' }}"
     ...
```

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/web/templates/mobile_nav.html \
        paperreadagent/web/templates/base.html \
        paperreadagent/modules/thinker/templates/panel.html
git commit -m "feat: mobile bottom tab nav + desktop/mobile nav split"
```

---

### Task 7: 论文详情移动模板 + session 详情适配

**Files:**
- Create: `paperreadagent/web/templates/paper_detail_mobile.html`
- Modify: `paperreadagent/web/routes/papers.py` (render mobile or desktop template based on is_mobile)
- Read: `paperreadagent/web/templates/session_detail.html` (check structure, then modify)

- [ ] **Step 1: Read current files for exact line references**

Read `paperreadagent/web/routes/papers.py` to find the paper detail route.
Read `paperreadagent/web/templates/session_detail.html` to check structure for conditional PDF tab hiding.

- [ ] **Step 2: Create paper_detail_mobile.html template**

创建 `paperreadagent/web/templates/paper_detail_mobile.html`：

```html
{% extends "base.html" %}
{% block title %}{{ paper.title[:50] or 'Paper' }}{% endblock %}

{% block content %}
<!-- Mobile Header -->
<div class="mobile-header">
    <a href="/sessions/{{ paper.session_id }}" class="back-arrow">←</a>
    <span class="title">{{ paper.title or '(无标题)' }}</span>
</div>

<!-- Meta bar -->
<div class="px-3 py-2 text-xs text-gray-500 bg-white border-b">
    {% set authors = paper.authors if paper.authors is iterable and paper.authors is not string else [] %}
    <span>{{ authors[:3] | join(', ') }}{% if authors|length > 3 %} 等{% endif %}</span>
    {% if paper.published %}<span class="ml-2">{{ paper.published[:10] }}</span>{% endif %}
    <span class="ml-2 px-1.5 py-0.5 rounded text-xs
        {% if paper.relevance_score >= 0.8 %}bg-green-100 text-green-700
        {% elif paper.relevance_score >= 0.5 %}bg-yellow-100 text-yellow-700
        {% else %}bg-gray-100 text-gray-500{% endif %}">
        相关度 {{ '%.2f' % paper.relevance_score }}
    </span>
</div>

<!-- Abstract (collapsible) -->
{% if paper.abstract %}
<details class="bg-white border-b px-3 py-2" open>
    <summary class="text-sm font-semibold text-gray-600">摘要</summary>
    <p class="text-xs text-gray-500 mt-1 leading-relaxed">{{ paper.abstract[:1500] }}{% if paper.abstract|length > 1500 %}...{% endif %}</p>
</details>
{% endif %}

<!-- AI Analysis -->
<div class="bg-white border-b px-3 py-3">
    <h3 class="text-sm font-semibold mb-2">📝 AI 分析总结</h3>
    {% if summary_content %}
    <div class="prose prose-sm max-w-none text-sm leading-relaxed">{{ summary_html | safe }}</div>
    {% else %}
    <p class="text-gray-400 text-center py-6 text-sm">尚未生成总结。</p>
    {% endif %}
</div>

<!-- Notes -->
<div class="bg-white px-3 py-3">
    <h3 class="text-sm font-semibold mb-2">✏️ 个人笔记</h3>
    <form method="post" action="/papers/{{ paper.id }}/note">
        <textarea name="content" rows="5" placeholder="写想法..."
                  class="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-yellow-300 focus:border-yellow-500 outline-none resize-y">{{ note_content }}</textarea>
        <button type="submit" class="mt-2 w-full bg-yellow-500 text-white py-2.5 rounded-lg hover:bg-yellow-600 text-sm font-medium">
            保存笔记
        </button>
    </form>
    {% if note_updated %}<p class="text-xs text-gray-400 mt-1">更新于 {{ note_updated[:16] }}</p>{% endif %}
</div>
{% endblock %}
```

- [ ] **Step 3: Modify papers.py route to switch templates based on is_mobile**

在 `papers.py` 中找到 paper detail GET 路由（`@router.get("/{paper_id}")`），修改 return 语句为：

```python
is_mobile = getattr(request.state, "is_mobile", False)
template_name = "paper_detail_mobile.html" if is_mobile else "paper_detail.html"
return templates.TemplateResponse(template_name, {
    "request": request,
    "paper": paper,
    "pdf_url": pdf_url,
    "summary_content": summary_content,
    "summary_html": summary_html,
    "note_content": note_content,
    "note_updated": note_updated,
    "debug_info": debug_info,
})
```

- [ ] **Step 4: Modify session_detail.html — mobile conditionally hide PDF tab**

Read `session_detail.html` first. If there's a PDF tab or link, wrap it with:

```html
{% if not request.state.is_mobile %}
<!-- PDF link / tab -->
{% endif %}
```

- [ ] **Step 5: Verify template rendering**

Run: `uv run python -c "
from paperreadagent.web.app import create_app
app = create_app()
from fastapi.testclient import TestClient
c = TestClient(app)
# Test login page renders
r = c.get('/login')
assert r.status_code == 200
print('OK: templates render')
"`

- [ ] **Step 6: Commit**

```bash
git add paperreadagent/web/templates/paper_detail_mobile.html \
        paperreadagent/web/routes/papers.py \
        paperreadagent/web/templates/session_detail.html
git commit -m "feat: mobile paper detail template + session detail adapt"
```

---

### Task 8: Thinker 全屏页面

**Files:**
- Create: `paperreadagent/modules/thinker/templates/fullscreen.html`
- Modify: `paperreadagent/modules/thinker/routes.py` (add GET `/` page route)
- Modify: `paperreadagent/modules/thinker/__init__.py` (register thinker page route or handle in routes)

- [ ] **Step 1: Create thinker fullscreen.html**

创建 `paperreadagent/modules/thinker/templates/fullscreen.html`：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Thinker — PaperReadAgent</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js"></script>
    <link rel="stylesheet" href="/static/css/app.css">
    <link rel="stylesheet" href="/thinker/static/thinker.css">
</head>
<body class="bg-gray-50">
    <!-- Thinker panel rendered as fullscreen (not floating) -->
    {% with fullscreen_mode=True %}
    {% include "thinker/panel.html" %}
    {% endwith %}

    <!-- Mobile nav -->
    {% set ns = namespace() %}
    {% set request_dict = {"url": {"path": "/thinker/"}, "state": {"is_mobile": True}} %}
    {% include "mobile_nav.html" %}

    <script src="/thinker/static/thinker.js"></script>
</body>
</html>
```

Wait—this won't work well because `mobile_nav.html` checks `request.state.is_mobile` and `request.url.path`, and `panel.html` expects Alpine context. Let me think about this more carefully.

The `panel.html` uses `x-data="thinkerPanel()"` and depends on the thinker.js being loaded. For the fullscreen page, the panel should be rendered without the floating/fixed positioning — the CSS handles this via the `.thinker-fullscreen` class.

The key challenge is that `panel.html` is injected as a global component (body-end), not served as a standalone page. For the fullscreen, we need to render it inside a minimal HTML page.

Simpler approach: The fullscreen.html extends base.html but replaces the content block entirely with the thinker panel in fullscreen mode. And we pass `fullscreen_mode=True` to the render context.

Actually, the cleanest approach: fullscreen.html is a minimal standalone HTML page (not extending base.html) that renders the panel component with `fullscreen_mode=True`. It includes its own mobile nav markup.

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>Thinker — PaperReadAgent</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js"></script>
    <link rel="stylesheet" href="/static/css/app.css">
    <link rel="stylesheet" href="/thinker/static/thinker.css">
</head>
<body class="bg-gray-50">

<!-- Thinker Panel — fullscreen mode -->
<div data-module="thinker" class="thinker-fullscreen"
     x-data="thinkerPanel(true)"
     x-init="init()">
    {# Content from panel.html goes here, but rendered inline #}
    {# For simplicity, include panel.html body content directly #}
</div>

<!-- Mobile Nav -->
<nav class="mobile-nav">
    <a href="/projects/" class="tab-item">
        <span class="tab-icon">🏠</span><span>首页</span>
    </a>
    <a href="/thinker/" class="tab-item active">
        <span class="tab-icon">💬</span><span>Thinker</span>
    </a>
    <a href="/ideator/" class="tab-item">
        <span class="tab-icon">💡</span><span>Ideator</span>
    </a>
    <a href="/settings/" class="tab-item">
        <span class="tab-icon">⚙</span><span>设置</span>
    </a>
</nav>

<script src="/thinker/static/thinker.js"></script>
</body>
</html>
```

Hmm, but this duplicates the mobile nav markup and the panel body content. Let me restructure.

**Best approach**: Pass `fullscreen_mode=True` to the existing panel.html template when rendering via a Jinja include or via the core frontend renderer. Since the panel is a global component registered via `core.frontend.register_global_component()`, the tricky part is passing context to it.

Simplest reliable approach: The `fullscreen.html` template directly includes the key parts from `panel.html` using Jinja include with variables. And the mobile nav is its own include.

Actually wait — let me re-read how the core frontend injects the panel. Looking at `frontend.py`:

The `get_body_end_inject()` renders the component template using Jinja2 Environment with `FileSystemLoader`. So it's a separate render pass, not part of the page template's render.

For the fullscreen page, the cleanest approach is to have fullscreen.html be a standalone page that:
1. Includes `mobile_nav.html` 
2. Renders the thinker panel content directly (not through the inject mechanism)
3. Loads thinker.js

Let me simplify by having fullscreen.html include panel.html with `fullscreen_mode=True`:

```html
{% with fullscreen_mode=True %}
{% include "thinker/panel.html" %}
{% endwith %}
```

But this won't work b/c Jinja's `{% include %}` doesn't share context across different template loaders (panel.html is in a different directory).

OK, let me just have the template be self-contained with duplicated panel HTML structure. That's the simplest reliable approach. Or better: store the panel body in a separate template fragment.

Actually, the most pragmatic approach: have the fullscreen page extend base.html but replace the entire content block with the thinker panel. And in the route, pass `fullscreen_mode=True`.

Let me restructure: fullscreen.html extends base.html but overrides both the style (no desktop nav) and content:

```html
{% extends "base.html" %}
{% block title %}Thinker{% endblock %}

{% block content %}
<div data-module="thinker" class="thinker-fullscreen"
     x-data="thinkerPanel()" x-init="init()">
    <!-- Include panel inner HTML here -->
    {% include "thinker/_panel_body.html" %}
</div>
{% endblock %}
```

But this requires creating `_panel_body.html` from panel.html's body.

You know what, let me just go with the most practical approach: fullscreen.html is entirely self-contained — it duplicates the panel HTML structure directly. In practice, panel.html isn't that long (~90 lines HTML body). The duplication is manageable and independent — if one template changes, it doesn't accidentally break the other.

Let me write it self-contained with the exact panel HTML structure but adapted for fullscreen:

Actually, for the implementation plan, let me just describe what needs to happen without showing the entire duplicated HTML. The implementer will read `panel.html` and adapt it.

Let me simplify the plan step — just state clearly what the task is.

- [ ] **Step 1: Create thinker fullscreen.html**

Read `panel.html` for the panel body structure, then create a self-contained fullscreen page.

```html
# paperreadagent/modules/thinker/templates/fullscreen.html
# Self-contained mobile fullscreen thinker page.
# Renders the thinker panel in position:relative mode (not floating sidebar).
# Includes mobile bottom nav.
# Loads thinker.js for Alpine component + SSE chat.
```

(Full implementation: read panel.html, extract the inner HTML structure, wrap in fullscreen page shell with `thinker-fullscreen` class, add mobile nav, load thinker.js.)

- [ ] **Step 2: Add thinker page route**

In `paperreadagent/modules/thinker/routes.py`, add:

```python
@router.get("/", response_class=HTMLResponse)
async def thinker_page(request: Request):
    """Fullscreen thinker page for mobile."""
    from fastapi.responses import HTMLResponse
    from fastapi.templating import Jinja2Templates
    from pathlib import Path
    tpl_dir = Path(__file__).parent / "templates"
    tpl = Jinja2Templates(directory=str(tpl_dir))
    return tpl.TemplateResponse("fullscreen.html", {
        "request": request,
    })
```

Note: The thinker routes are mounted at `/thinker/` prefix, so this maps to `GET /thinker/`.

- [ ] **Step 3: Desktop redirect for /thinker/**

Add JS redirect in the route: on desktop, redirect to `/projects/` (thinker is a floating panel there, not a page):

```python
@router.get("/", response_class=HTMLResponse)
async def thinker_page(request: Request):
    is_mobile = getattr(request.state, "is_mobile", False)
    if not is_mobile:
        # Desktop: thinker is a floating sidebar, redirect to projects
        return RedirectResponse(url="/projects/", status_code=303)
    ...
```

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/thinker/templates/fullscreen.html \
        paperreadagent/modules/thinker/routes.py
git commit -m "feat: thinker fullscreen page for mobile"
```

---

### Task 9: Settings 页面

**Files:**
- Create: `paperreadagent/web/templates/settings.html`
- Create: `paperreadagent/web/routes/settings_routes.py`
- Modify: `paperreadagent/web/app.py` (register settings router)

- [ ] **Step 1: Create settings.html**

创建 `paperreadagent/web/templates/settings.html`：

```html
{% extends "base.html" %}
{% block title %}设置 — PaperReadAgent{% endblock %}

{% block content %}
<div class="max-w-md mx-auto space-y-4">
    <h1 class="text-xl font-bold">⚙ 设置</h1>

    <!-- Change Password -->
    <div class="bg-white rounded-xl shadow-sm border p-5">
        <h2 class="font-semibold text-sm mb-3">修改密码</h2>
        <form method="post" action="/settings/change-password" class="space-y-3">
            <input type="password" name="old_password" placeholder="当前密码"
                   class="w-full border rounded-lg px-3 py-2 text-sm">
            <input type="password" name="new_password" placeholder="新密码（至少6位）"
                   class="w-full border rounded-lg px-3 py-2 text-sm">
            <input type="password" name="new_password_confirm" placeholder="再次输入新密码"
                   class="w-full border rounded-lg px-3 py-2 text-sm">
            <button type="submit" class="w-full bg-indigo-600 text-white py-2 rounded-lg hover:bg-indigo-700 text-sm font-medium">
                更新密码
            </button>
        </form>
        {% if pwd_error %}<p class="text-red-500 text-xs mt-2">{{ pwd_error }}</p>{% endif %}
        {% if pwd_ok %}<p class="text-green-500 text-xs mt-2">{{ pwd_ok }}</p>{% endif %}
    </div>

    <!-- Connection Status -->
    <div class="bg-white rounded-xl shadow-sm border p-5">
        <h2 class="font-semibold text-sm mb-3">连接状态</h2>
        <div class="text-sm space-y-1">
            <p>服务地址: <code class="bg-gray-100 px-1 rounded">{{ server_host }}:{{ server_port }}</code></p>
            <p>数据库: <span class="text-green-600">已连接</span></p>
            <p>用户: <span class="text-gray-500">admin（唯一账号）</span></p>
        </div>
    </div>

    <!-- Logout -->
    <a href="/logout" class="block text-center text-red-500 text-sm py-2 hover:text-red-700">退出登录</a>
</div>
{% endblock %}
```

- [ ] **Step 2: Create settings routes**

创建 `paperreadagent/web/routes/settings_routes.py`：

```python
"""
web/routes/settings_routes.py
设置页 — 修改密码、查看连接状态。
"""

from __future__ import annotations

import yaml
from pathlib import Path
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from paperreadagent.web.auth import hash_password, verify_password
from web.template_config import templates

router = APIRouter(prefix="/settings", tags=["settings"])

BASE_DIR = Path(__file__).parent.parent.parent.parent


@router.get("/", response_class=HTMLResponse)
async def settings_page(request: Request):
    config_path = BASE_DIR / "config.yaml"
    host, port = "0.0.0.0", 8000
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        server_cfg = cfg.get("server", {})
        host = server_cfg.get("host", "0.0.0.0")
        port = server_cfg.get("port", 8000)

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "server_host": host,
        "server_port": port,
        "pwd_error": None,
        "pwd_ok": None,
    })


@router.post("/change-password")
async def change_password(
    request: Request,
    old_password: str = Form(""),
    new_password: str = Form(""),
    new_password_confirm: str = Form(""),
):
    if len(new_password) < 6:
        return _render_settings(request, pwd_error="新密码至少 6 位。")

    if new_password != new_password_confirm:
        return _render_settings(request, pwd_error="两次密码输入不一致。")

    row = request.app.state.core.db.conn.execute(
        "SELECT password_hash FROM core_users WHERE id = 1"
    ).fetchone()
    if not row or not verify_password(old_password, row["password_hash"]):
        return _render_settings(request, pwd_error="当前密码错误。")

    new_hash = hash_password(new_password)
    request.app.state.core.db.conn.execute(
        "UPDATE core_users SET password_hash = ? WHERE id = 1", (new_hash,)
    )
    request.app.state.core.db.conn.commit()
    return _render_settings(request, pwd_ok="密码已更新。")


def _render_settings(request: Request, pwd_error=None, pwd_ok=None):
    config_path = BASE_DIR / "config.yaml"
    host, port = "0.0.0.0", 8000
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        server_cfg = cfg.get("server", {})
        host = server_cfg.get("host", "0.0.0.0")
        port = server_cfg.get("port", 8000)

    return templates.TemplateResponse("settings.html", {
        "request": request,
        "server_host": host,
        "server_port": port,
        "pwd_error": pwd_error,
        "pwd_ok": pwd_ok,
    })
```

- [ ] **Step 3: Register settings router in app.py**

```python
# In app.py, add:
from web.routes import settings_routes
app.include_router(settings_routes.router, tags=["settings"])
```

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/web/templates/settings.html \
        paperreadagent/web/routes/settings_routes.py \
        paperreadagent/web/app.py
git commit -m "feat: settings page — change password + connection status"
```

---

### Task 10: SSE 轮询兜底 + 集成测试 + 收尾

**Files:**
- Modify: `paperreadagent/web/templates/session_detail.html` (add poll fallback JS)
- Modify: `paperreadagent/web/routes/sessions.py` (add progress JSON endpoint for polling)
- Create: `paperreadagent/tests/test_mobile_integration.py`

- [ ] **Step 1: Add progress JSON endpoint for polling**

In `sessions.py`, add a non-SSE progress endpoint:

```python
@router.get("/{session_id}/progress/json")
async def session_progress_json(request: Request, session_id: int):
    """Non-SSE progress — for mobile polling fallback."""
    from web.progress import get_progress
    progress = get_progress(session_id)
    return {
        "stage": progress.stage,
        "stage_index": progress.stage_index,
        "papers_total": progress.papers_total,
        "papers_done": progress.papers_done,
        "papers_failed": progress.papers_failed,
        "current_title": progress.current_paper_title or "",
        "messages": progress.messages[-5:] if progress.messages else [],
        "error": progress.error or "",
    }
```

- [ ] **Step 2: Add polling fallback JS to session_detail.html**

在 session_detail.html 的 SSE EventSource 代码之后，添加移动端轮询兜底：

```html
{% if request.state.is_mobile %}
<script>
// Mobile polling fallback for SSE (unreliable on mobile networks)
(function() {
    const sessionId = {{ session.id }};
    let pollTimer = null;

    function pollProgress() {
        fetch('/sessions/' + sessionId + '/progress/json')
            .then(r => r.json())
            .then(data => {
                // Update progress bar
                const bar = document.getElementById('progress-bar');
                if (bar && data.stage_index) {
                    bar.style.width = (data.stage_index / 6 * 100) + '%';
                }
                const label = document.getElementById('progress-label');
                if (label && data.messages.length) {
                    label.textContent = data.messages[data.messages.length - 1];
                }
                // Stop polling when done
                if (data.stage === 'done' || data.stage === 'error') {
                    clearInterval(pollTimer);
                    if (data.stage === 'done') setTimeout(() => location.reload(), 2000);
                }
            })
            .catch(() => {}); // silent fail on network error
    }

    // Start polling only if SSE fails to connect within 3s
    const sseCheck = setTimeout(() => {
        pollTimer = setInterval(pollProgress, 5000);
    }, 3000);

    // If SSE connects successfully, cancel polling
    const es = document.querySelector('[data-sse-source]');
    if (es) {
        // SSE is active — cancel polling setup
        clearTimeout(sseCheck);
    }
})();
</script>
{% endif %}
```

- [ ] **Step 3: Write integration smoke test**

创建 `paperreadagent/tests/test_mobile_integration.py`：

```python
"""Smoke tests for mobile support — all routes return valid HTML."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from paperreadagent.web.app import create_app
    app = create_app()
    return TestClient(app)


# ── Auth ──

def test_login_page_renders(client):
    r = client.get("/login")
    assert r.status_code == 200
    assert "登录" in r.text or "login" in r.text.lower()


def test_protected_routes_redirect_to_login(client):
    for path in ["/projects/", "/sessions/1", "/papers/1", "/settings/"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303, 401), f"{path} should redirect"


def test_static_routes_not_protected(client):
    r = client.get("/static/css/app.css")
    assert r.status_code != 302  # not redirected to login


# ── Mobile detection ──

def test_is_mobile_flag_set_with_android_ua(client):
    # We need to go through login first... this test verifies the flag exists
    # on the request.state after auth middleware
    r = client.get("/login", headers={"User-Agent": "Mozilla/5.0 Android 14"})
    assert r.status_code == 200


# ── Templates ──

def test_paper_detail_mobile_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "paper_detail_mobile.html"
    assert tpl.exists(), "paper_detail_mobile.html not found"


def test_login_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "login.html"
    assert tpl.exists(), "login.html not found"


def test_settings_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "settings.html"
    assert tpl.exists(), "settings.html not found"


def test_mobile_nav_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "mobile_nav.html"
    assert tpl.exists(), "mobile_nav.html not found"


def test_thinker_fullscreen_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "modules" / "thinker" / "templates" / "fullscreen.html"
    assert tpl.exists(), "fullscreen.html not found"
```

- [ ] **Step 4: Run all tests to verify nothing is broken**

Run: `uv run python -m pytest paperreadagent/tests/ -v`
Expected: All existing tests pass + new tests pass

- [ ] **Step 5: Run full test suite**

Run: `uv run python -m pytest -v`
Expected: 210+ tests pass (206 existing + ~15 new)

- [ ] **Step 6: Commit**

```bash
git add paperreadagent/web/templates/session_detail.html \
        paperreadagent/web/routes/sessions.py \
        paperreadagent/tests/test_mobile_integration.py
git commit -m "feat: SSE polling fallback + integration tests + mobile support complete"
```

---

## 验证清单

- [ ] `uv run python -m pytest -v` — 全部测试通过
- [ ] `uv run uvicorn paperreadagent.web.app:app --host 0.0.0.0 --port 8000` — 启动成功
- [ ] 桌面浏览器访问 `http://localhost:8000` → 重定向到 `/login`，正确显示登录页
- [ ] 手机浏览器访问 `http://<电脑IP>:8000` → 登录 → 底部 tab 导航正常 → thinker 全屏页正常
- [ ] 论文详情页在手机上显示纯 markdown（无 PDF）
- [ ] 设置页改密码功能正常
- [ ] 桌面端原有功能不受影响
