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


def test_first_time_setup_page(client_with_auth):
    # When core_users is empty, /login should show setup mode
    resp = client_with_auth.get("/login")
    assert resp.status_code == 200
