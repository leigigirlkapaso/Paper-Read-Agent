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
    for path in ["/projects/", "/settings/"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 303, 401), f"{path} should redirect"


def test_static_routes_not_protected(client):
    r = client.get("/static/css/app.css")
    assert r.status_code != 302


# ── Mobile detection ──

def test_is_mobile_flag_set_with_android_ua(client):
    r = client.get("/login", headers={"User-Agent": "Mozilla/5.0 Android 14"})
    assert r.status_code == 200


# ── Templates ──

def test_paper_detail_mobile_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "paper_detail_mobile.html"
    assert tpl.exists()


def test_login_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "login.html"
    assert tpl.exists()


def test_settings_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "settings.html"
    assert tpl.exists()


def test_mobile_nav_template_exists():
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "web" / "templates" / "mobile_nav.html"
    assert tpl.exists()


def test_thinker_page_template_exists():
    """v0.2.0: fullscreen.html replaced by thinker_page.html (full-page app)."""
    from pathlib import Path
    tpl = Path(__file__).parent.parent / "modules" / "thinker" / "templates" / "thinker_page.html"
    assert tpl.exists()
