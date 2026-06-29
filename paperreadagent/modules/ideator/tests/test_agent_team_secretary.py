"""tests for AgentTeamManager.after_round (Task 4)."""
import asyncio
import logging
import pytest
from unittest.mock import MagicMock, AsyncMock

from paperreadagent.modules.ideator.agent_team import AgentTeamManager
from paperreadagent.modules.ideator.tool_registry import create_default_registry


class MockTeamMemory:
    def format_for_context(self, spark_id):
        return ""
    def write(self, **kw):
        pass


class MockGraduation:
    def __init__(self):
        self.layers = {"hot": type("L", (), {"pct": 40.0})(),
                       "warm": type("L", (), {"pct": 30.0})()}


class MockArbiter:
    def calculate_round_quotas(self, hot, warm):
        return {"gen": 2000, "rev1": 800, "rev2": 800, "rev3": 800,
                "arb1": 500, "arb2": 500}
    def reset_for_new_team(self):
        pass


def _make_manager(*, secretary=None):
    mock_data = MagicMock()
    mock_data.insert_roundtable = MagicMock(return_value=42)
    mock_data.gather_facts_for_spark = MagicMock(return_value=[])
    mock_data._core = MagicMock()
    mock_data._core.db.conn = MagicMock()

    mock_llm = MagicMock()
    mock_llm.load_prompt.return_value = "<identity>"

    return AgentTeamManager(
        llm=mock_llm, data_access=mock_data,
        tool_registry=create_default_registry(),
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(),
        secretary=secretary,
    )


@pytest.mark.asyncio
async def test_after_round_calls_secretary_update():
    """after_round invokes secretary.update with rt_id + team kwarg."""
    secretary = MagicMock()
    secretary.update = AsyncMock(return_value="# outline")
    mgr = _make_manager(secretary=secretary)
    rt_id = mgr.create_team(spark_id=99, spark_content="x")

    await mgr.after_round(rt_id)

    secretary.update.assert_awaited_once()
    call = secretary.update.await_args
    assert call.args == (rt_id,)
    assert call.kwargs.get("team") is mgr.get_team(rt_id)


@pytest.mark.asyncio
async def test_after_round_noop_when_secretary_is_none():
    """No secretary -> after_round returns silently without errors."""
    mgr = _make_manager(secretary=None)
    rt_id = mgr.create_team(spark_id=99, spark_content="x")
    await mgr.after_round(rt_id)


@pytest.mark.asyncio
async def test_after_round_swallows_secretary_exception(caplog):
    """Exception in secretary.update is logged, not propagated."""
    secretary = MagicMock()
    secretary.update = AsyncMock(side_effect=RuntimeError("boom"))
    mgr = _make_manager(secretary=secretary)
    rt_id = mgr.create_team(spark_id=99, spark_content="x")

    with caplog.at_level(
        logging.WARNING,
        logger="paperreadagent.modules.ideator.agent_team",
    ):
        await mgr.after_round(rt_id)

    secretary.update.assert_awaited_once()
    relevant = [r for r in caplog.records if "secretary" in r.message.lower()]
    assert len(relevant) >= 1


@pytest.mark.asyncio
async def test_after_round_noop_when_team_missing():
    """Calling after_round with unknown rt_id is a no-op (don't crash)."""
    secretary = MagicMock()
    secretary.update = AsyncMock()
    mgr = _make_manager(secretary=secretary)
    await mgr.after_round(9999)
    secretary.update.assert_not_awaited()