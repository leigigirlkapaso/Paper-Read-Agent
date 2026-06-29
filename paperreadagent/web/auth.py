"""
web/auth.py
认证模块：密码哈希、session cookie 签名、登录防暴力破解。
纯 stdlib 实现，无外部依赖。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
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
    try:
        payload_bytes = base64.urlsafe_b64decode(_pad_b64(b64_payload))
        payload = json.loads(payload_bytes)
        if not isinstance(payload, dict):
            return None
        if payload.get("exp", 0) < int(time.time()):
            return None
    except (json.JSONDecodeError, AttributeError, UnicodeDecodeError):
        return None
    return payload


def make_session_cookie(user_id: int, secret: str, session_version: int = 0) -> str:
    """生成 session cookie value，含 session_version 用于改密码失效。"""
    payload = {
        "user_id": user_id,
        "exp": int(time.time()) + _COOKIE_TTL,
        "sv": session_version,
    }
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


# ── CSRF 保护（Double-Submit Cookie 模式）────────────────────

CSRF_COOKIE = "pra_csrf"
CSRF_HEADER = "X-CSRF-Token"
_CSRF_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_CSRF_EXEMPT_PATHS = frozenset({"/login", "/logout"})
_CSRF_TOKEN_BYTES = 32


def generate_csrf_token() -> str:
    """生成随机 CSRF token（url-safe base64）。"""
    import secrets as _s
    return _s.token_urlsafe(_CSRF_TOKEN_BYTES)


def validate_csrf(request, cookie_token: str | None, form_token: str | None) -> bool:
    """验证 double-submit cookie 一致性。使用 hmac.compare_digest 防时序攻击。"""
    if not cookie_token or not form_token:
        return False
    return hmac.compare_digest(cookie_token, form_token)


# ── Task 4 集成辅助 ────────────────────────────────────────

logger = logging.getLogger(__name__)

COOKIE_NAME = "pra_session"


def _get_server_secret(server_cfg: dict, config_path: str = "") -> str:
    """从配置读取 secret_key，空则自动生成并持久化到 config.yaml。"""
    key = server_cfg.get("secret_key", "") if server_cfg else ""
    if not key:
        import secrets as _s
        key = _s.token_hex(32)
        if config_path:
            try:
                _persist_secret_key(config_path, key)
                logger.info("[Auth] 自动生成 secret_key 并已持久化到 config.yaml")
            except Exception:
                logger.warning("[Auth] 自动生成 secret_key（仅内存，持久化失败）")
        else:
            logger.info("[Auth] 自动生成 secret_key（仅内存）")
    return key


def _persist_secret_key(config_path: str, key: str) -> None:
    """将 secret_key 写入 config.yaml（保持原格式，仅替换 server.secret_key）。"""
    import re
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()
    if 'secret_key: ""' in content:
        content = content.replace('secret_key: ""', f'secret_key: "{key}"')
    elif 'secret_key: ""' not in content:
        # fallback: regex replace
        content = re.sub(
            r'(secret_key:\s*")[^"]*(")',
            f'\\g<1>{key}\\g<2>',
            content,
            count=1,
        )
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)
