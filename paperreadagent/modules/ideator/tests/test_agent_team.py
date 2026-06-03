"""tests for AgentTeam"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from paperreadagent.modules.ideator.agent_team import AgentTeam, AgentSeat
from paperreadagent.modules.ideator.tool_registry import create_default_registry


class MockTeamMemory:
    def __init__(self):
        self.writes = []

    def format_for_context(self, spark_id):
        return "test memory"

    def write(self, **kw):
        self.writes.append(kw)


class MockGraduation:
    def __init__(self):
        self.layers = {
            "hot": type("L", (), {"pct": 40.0})(),
            "warm": type("L", (), {"pct": 30.0})(),
        }


class MockArbiter:
    def calculate_round_quotas(self, hot, warm):
        return {"gen": 2000, "rev1": 800, "rev2": 800, "rev3": 800, "arb1": 500, "arb2": 500}

    async def execute_graduation(self, **kw):
        return {"verdict": "ok", "warm_summary": "test summary"}

    def reset_for_new_team(self):
        pass


def test_agent_seat_quota_tracking():
    seat = AgentSeat(seat_id="gen", role="generator", quota=2000, tools=["search_papers"])
    assert seat.remaining_quota == 2000
    seat.consume_quota(500)
    assert seat.remaining_quota == 1500
    assert not seat.quota_exhausted()
    seat.consume_quota(2000)
    assert seat.quota_exhausted()


def test_agent_seat_reset_quota():
    seat = AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[])
    seat.consume_quota(1500)
    seat.reset_quota()
    assert seat.remaining_quota == 2000


def test_agent_team_creation():
    seats = [
        AgentSeat(seat_id="gen", role="generator", quota=2000, tools=["search_papers"]),
        AgentSeat(seat_id="rev1", role="reviewer_1", quota=800, tools=["search_papers"]),
    ]
    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock(return_value="test response")
    mock_llm.load_prompt = MagicMock(return_value="test system prompt")

    team = AgentTeam(
        spark_id=1, spark_content="test spark",
        seats=seats, llm=mock_llm,
        team_memory=MockTeamMemory(),
        graduation=MockGraduation(),
        arbiter=MockArbiter(),
        tool_registry=create_default_registry(),
    )
    assert len(team.seats) == 2
    assert team.round_number == 0


@pytest.mark.asyncio
async def test_start_round_returns_results():
    seats = [
        AgentSeat(seat_id="gen", role="generator", quota=2000, tools=["search_papers"]),
        AgentSeat(seat_id="rev1", role="reviewer_1", quota=800, tools=["search_papers"]),
    ]
    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock(return_value="test response")
    mock_llm.load_prompt = MagicMock(return_value="test system prompt")

    team = AgentTeam(
        spark_id=1, spark_content="test spark",
        seats=seats, llm=mock_llm,
        team_memory=MockTeamMemory(),
        graduation=MockGraduation(),
        arbiter=MockArbiter(),
        tool_registry=create_default_registry(),
    )

    results = await team.start_round(question="Test?", mentioned=["gen", "rev1"])
    assert len(results) >= 1
    assert team.round_number == 1


@pytest.mark.asyncio
async def test_execute_graduation_cycle():
    seats = [
        AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[]),
    ]
    mock_llm = MagicMock()
    mock_llm.chat = AsyncMock(return_value="test response")
    mock_llm.load_prompt = MagicMock(return_value="test system prompt")

    team = AgentTeam(
        spark_id=1, spark_content="test spark",
        seats=seats, llm=mock_llm,
        team_memory=MockTeamMemory(),
        graduation=MockGraduation(),
        arbiter=MockArbiter(),
        tool_registry=create_default_registry(),
    )

    decision = await team.execute_graduation_cycle(roundtable_id=1)
    assert decision["verdict"] == "ok"
    assert decision["warm_summary"] == "test summary"
    assert team._warm_context == "test summary"


def test_create_default_seats_returns_6():
    from paperreadagent.modules.ideator.agent_team import create_default_seats
    seats = create_default_seats()
    assert len(seats) == 6
    seat_ids = {s.seat_id for s in seats}
    assert seat_ids == {"gen", "rev1", "rev2", "rev3", "arb1", "arb2"}

def test_create_default_seats_arbiters_have_equal_tools():
    from paperreadagent.modules.ideator.agent_team import create_default_seats
    seats = create_default_seats()
    arb1 = next(s for s in seats if s.seat_id == "arb1")
    arb2 = next(s for s in seats if s.seat_id == "arb2")
    assert set(arb1.tools) == set(arb2.tools)

def test_create_default_seats_quotas():
    from paperreadagent.modules.ideator.agent_team import create_default_seats
    seats = create_default_seats()
    gen = next(s for s in seats if s.seat_id == "gen")
    assert gen.quota == 2000
    for rev in [s for s in seats if s.seat_id.startswith("rev")]:
        assert rev.quota == 800

def test_agent_system_prompt_includes_source_context():
    from paperreadagent.modules.ideator.agent_team import AgentTeam, AgentSeat
    from unittest.mock import MagicMock

    mock_llm = MagicMock()
    mock_llm.load_prompt = MagicMock(return_value="identity prompt")
    mock_memory = MagicMock()
    mock_memory.format_for_context = MagicMock(return_value="memory text")
    mock_graduation = MagicMock()
    mock_graduation.layers = {}
    mock_arbiter = MagicMock()

    team = AgentTeam(
        spark_id=1, spark_content="test spark",
        seats=[], llm=mock_llm,
        team_memory=mock_memory,
        graduation=mock_graduation,
        arbiter=mock_arbiter,
        tool_registry=None,
        source_context="标题: Test Paper\n摘要: Abstract text here",
    )

    seat = AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[])
    prompt = team._build_agent_system_prompt(seat)
    assert "Test Paper" in prompt
    assert "Abstract text here" in prompt
    assert "memory text" in prompt


def test_create_team_direct_mode():
    """直接圆桌：spark_id=0, spark_content_override 传入用户内容，6 坐席全保留"""
    from paperreadagent.modules.ideator.agent_team import AgentTeamManager
    from unittest.mock import MagicMock

    mock_data = MagicMock()
    mock_data.insert_roundtable = MagicMock(return_value=99)
    mock_data.get_paper = MagicMock(return_value=None)
    mock_data.get_user_note = MagicMock(return_value=None)
    mock_data.get_spark = MagicMock(return_value=None)
    mock_data._core = MagicMock()
    mock_data._core.knowledge = MagicMock()
    mock_data._core.knowledge.get_note = MagicMock(return_value=None)

    mgr = AgentTeamManager(
        llm=MagicMock(),
        data_access=mock_data,
        tool_registry=MagicMock(),
        team_memory=MagicMock(),
        graduation=MagicMock(),
        arbiter=MagicMock(),
    )

    rt_id = mgr.create_team(
        spark_id=0,
        spark_content="",
        source_refs=[],
        spark_content_override="用户的研究想法：探索量子计算在NLP中的应用",
    )

    assert rt_id == 99
    team = mgr.get_team(rt_id)
    assert team is not None
    assert team.spark_id == 0
    assert team.spark_content == "用户的研究想法：探索量子计算在NLP中的应用"
    assert len(team.seats) == 6
    assert "gen" in team.seats
