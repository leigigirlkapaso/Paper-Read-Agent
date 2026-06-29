"""tests for POST /api/roundtables/{rt_id}/close auto-generating project brief."""
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from paperreadagent.modules.ideator.routes import router


def _make_app():
    app = FastAPI()
    app.state.core = MagicMock()
    app.include_router(router, prefix="/ideator")
    return app


def test_close_route_generates_brief_when_outline_and_spark_exist():
    """Close path: spark_id > 0 + outline exists → ProjectBriefService.generate called."""
    app = _make_app()
    fake_team = MagicMock()
    fake_team.spark_id = 42
    fake_team.execute_graduation_cycle = AsyncMock()

    fake_da = MagicMock()
    fake_da.get_latest_outline = MagicMock(return_value="## 1. 研究问题\nfoo")

    fake_service = MagicMock()
    fake_service.generate = AsyncMock(return_value=99)  # brief_id

    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn, \
         patch("paperreadagent.modules.ideator.data_access.DataAccess", return_value=fake_da), \
         patch("paperreadagent.modules.ideator.project_brief.ProjectBriefService",
               return_value=fake_service):
        mock_mgr_fn.return_value.get_team.return_value = fake_team
        with TestClient(app) as client:
            resp = client.post("/ideator/api/roundtables/7/close")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "closed"
    assert data["brief_id"] == 99
    fake_service.generate.assert_awaited_once()
    call = fake_service.generate.await_args
    # First positional arg is spark_id, outline_markdown is keyword
    assert call.args == (42,)
    assert call.kwargs.get("outline_markdown") == "## 1. 研究问题\nfoo"


def test_close_route_skips_brief_when_spark_id_is_zero():
    """Direct-roundtable mode (spark_id=0): no brief generation."""
    app = _make_app()
    fake_team = MagicMock()
    fake_team.spark_id = 0
    fake_team.execute_graduation_cycle = AsyncMock()

    fake_service = MagicMock()
    fake_service.generate = AsyncMock()

    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn, \
         patch("paperreadagent.modules.ideator.routes.ProjectBriefService",
               return_value=fake_service):
        mock_mgr_fn.return_value.get_team.return_value = fake_team
        with TestClient(app) as client:
            resp = client.post("/ideator/api/roundtables/7/close")

    assert resp.status_code == 200
    data = resp.json()
    assert data["brief_id"] is None
    fake_service.generate.assert_not_awaited()


def test_close_route_skips_brief_when_outline_missing():
    """No outline (e.g., secretary disabled or zero-round close): no brief gen."""
    app = _make_app()
    fake_team = MagicMock()
    fake_team.spark_id = 42
    fake_team.execute_graduation_cycle = AsyncMock()

    fake_da = MagicMock()
    fake_da.get_latest_outline = MagicMock(return_value=None)

    fake_service = MagicMock()
    fake_service.generate = AsyncMock()

    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn, \
         patch("paperreadagent.modules.ideator.data_access.DataAccess", return_value=fake_da), \
         patch("paperreadagent.modules.ideator.project_brief.ProjectBriefService",
               return_value=fake_service):
        mock_mgr_fn.return_value.get_team.return_value = fake_team
        with TestClient(app) as client:
            resp = client.post("/ideator/api/roundtables/7/close")

    assert resp.status_code == 200
    assert resp.json()["brief_id"] is None
    fake_service.generate.assert_not_awaited()


def test_close_route_swallows_brief_generation_failure():
    """Brief generation failure logs + falls through with brief_id=None.
    The close response still succeeds — failure must not block close."""
    app = _make_app()
    fake_team = MagicMock()
    fake_team.spark_id = 42
    fake_team.execute_graduation_cycle = AsyncMock()

    fake_da = MagicMock()
    fake_da.get_latest_outline = MagicMock(return_value="## outline")

    fake_service = MagicMock()
    fake_service.generate = AsyncMock(side_effect=RuntimeError("boom"))

    with patch("paperreadagent.modules.ideator.get_roundtable_manager") as mock_mgr_fn, \
         patch("paperreadagent.modules.ideator.data_access.DataAccess", return_value=fake_da), \
         patch("paperreadagent.modules.ideator.project_brief.ProjectBriefService",
               return_value=fake_service):
        mock_mgr_fn.return_value.get_team.return_value = fake_team
        with TestClient(app) as client:
            resp = client.post("/ideator/api/roundtables/7/close")

    assert resp.status_code == 200
    assert resp.json()["status"] == "closed"
    assert resp.json()["brief_id"] is None
