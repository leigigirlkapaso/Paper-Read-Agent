"""tests for GET /api/roundtables/{rt_id}/outline (Task 6)."""
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from paperreadagent.modules.ideator.routes import router


def _make_app():
    """Minimal FastAPI app mounting the ideator router with a mock state.core."""
    app = FastAPI()
    app.state.core = MagicMock()
    app.include_router(router, prefix="/ideator")
    return app


def test_get_outline_returns_latest_for_rt():
    """When team exists and outline rows exist, route returns latest outline
    plus the round_number of the latest version."""
    app = _make_app()

    fake_da = MagicMock()
    fake_da.get_latest_outline = MagicMock(return_value="## 1. 研究问题\nfoo")
    fake_da.get_outline_history = MagicMock(return_value=[
        {"id": 1, "round_number": 1, "outline_markdown": "old", "created_at": "..."},
        {"id": 2, "round_number": 2, "outline_markdown": "old2", "created_at": "..."},
        {"id": 3, "round_number": 3, "outline_markdown": "## 1. 研究问题\nfoo",
         "created_at": "..."},
    ])

    fake_team = MagicMock()

    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn, \
         patch("paperreadagent.modules.ideator.data_access.DataAccess", return_value=fake_da):
        mock_mgr_fn.return_value.get_team.return_value = fake_team
        with TestClient(app) as client:
            resp = client.get("/ideator/api/roundtables/42/outline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["rt_id"] == 42
    assert data["outline"] == "## 1. 研究问题\nfoo"
    assert data["round_number"] == 3


def test_get_outline_returns_404_when_team_missing():
    app = _make_app()
    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn:
        mock_mgr_fn.return_value.get_team.return_value = None
        with TestClient(app) as client:
            resp = client.get("/ideator/api/roundtables/9999/outline")
    assert resp.status_code == 404


def test_get_outline_returns_empty_when_no_rows_yet():
    """First round not yet finished -> no outline row exists -> return empty string."""
    app = _make_app()
    fake_da = MagicMock()
    fake_da.get_latest_outline = MagicMock(return_value=None)
    fake_da.get_outline_history = MagicMock(return_value=[])
    fake_team = MagicMock()

    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn, \
         patch("paperreadagent.modules.ideator.data_access.DataAccess", return_value=fake_da):
        mock_mgr_fn.return_value.get_team.return_value = fake_team
        with TestClient(app) as client:
            resp = client.get("/ideator/api/roundtables/42/outline")
    assert resp.status_code == 200
    data = resp.json()
    assert data["outline"] == ""
    assert data["round_number"] == 0