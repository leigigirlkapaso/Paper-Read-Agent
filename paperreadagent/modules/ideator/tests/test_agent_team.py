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


# ────────────────────────────────────────────────────────
# facts_block injection — Task 4
# ────────────────────────────────────────────────────────

def _make_team_with_facts(facts_block: str) -> AgentTeam:
    """Build an AgentTeam with mocked llm.load_prompt that echoes kwargs."""
    seats = [
        AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[]),
        AgentSeat(seat_id="rev1", role="reviewer_1", quota=800, tools=[]),
    ]
    mock_llm = MagicMock()
    # Echo back the kwargs so we can inspect what was passed in
    def fake_load(module, name, **kw):
        return f"[IDENTITY:{name}][facts_block={kw.get('facts_block', '<absent>')}]"
    mock_llm.load_prompt.side_effect = fake_load

    return AgentTeam(
        spark_id=1, spark_content="content", seats=seats, llm=mock_llm,
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(), tool_registry=create_default_registry(),
        facts_block=facts_block,
    )


def test_init_accepts_facts_block_kwarg_default_empty():
    """AgentTeam without facts_block kwarg → defaults to ''."""
    seats = [AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[])]
    team = AgentTeam(
        spark_id=1, spark_content="c", seats=seats, llm=MagicMock(),
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(), tool_registry=create_default_registry(),
    )
    assert team.facts_block == ""


def test_identity_prompt_includes_facts_block_when_provided():
    """When facts_block='## 📚 ...', _build_agent_system_prompt passes it
    through load_prompt for every agent."""
    team = _make_team_with_facts("## 📚 ROUNDTABLE FACTS HERE ##")
    seat = team.seats["rev1"]
    sysp = team._build_agent_system_prompt(seat)
    assert "ROUNDTABLE FACTS HERE" in sysp


def test_identity_prompt_omits_facts_block_when_empty():
    """facts_block='' → not present in rendered identity (fake_load shows it
    as '<absent>' or empty)."""
    team = _make_team_with_facts("")
    seat = team.seats["gen"]
    sysp = team._build_agent_system_prompt(seat)
    # The fake renderer shows [facts_block=] — should be empty after =
    assert "[facts_block=]" in sysp
    # And the real content marker shouldn't appear
    assert "ROUNDTABLE FACTS HERE" not in sysp


# ────────────────────────────────────────────────────────
# 3-section soft-check — Task 6
# ────────────────────────────────────────────────────────

import logging


def _team_for_check():
    seats = [
        AgentSeat(seat_id="rev1", role="reviewer_1", quota=800, tools=[]),
        AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[]),
        AgentSeat(seat_id="arb1", role="arbiter_1", quota=500, tools=[]),
    ]
    return AgentTeam(
        spark_id=1, spark_content="c", seats=seats, llm=MagicMock(),
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(), tool_registry=create_default_registry(),
    )


def test_three_section_check_logs_warning_on_violation(caplog):
    team = _team_for_check()
    bad = "this idea is interesting but I think you should reconsider"
    with caplog.at_level(logging.WARNING,
                         logger="paperreadagent.modules.ideator.agent_team"):
        team._check_three_section_format(bad, "rev1")
    assert any("3 段式" in r.message or "rev1" in r.message for r in caplog.records)
    assert len(caplog.records) >= 1


def test_three_section_check_silent_on_compliance(caplog):
    team = _team_for_check()
    good = "【问题点】饱和\n【事实依据】论文 P1\n【建议修复】加 X"
    with caplog.at_level(logging.WARNING,
                         logger="paperreadagent.modules.ideator.agent_team"):
        team._check_three_section_format(good, "rev2")
    relevant = [r for r in caplog.records if "3 段式" in r.message]
    assert relevant == []


def test_three_section_check_skipped_for_arb_role(caplog):
    team = _team_for_check()
    arb_text = "仲裁意见: 通过."
    with caplog.at_level(logging.WARNING,
                         logger="paperreadagent.modules.ideator.agent_team"):
        team._check_three_section_format(arb_text, "arb1")
    relevant = [r for r in caplog.records if "3 段式" in r.message]
    assert relevant == []


def test_three_section_check_skipped_for_gen_role(caplog):
    team = _team_for_check()
    with caplog.at_level(logging.WARNING,
                         logger="paperreadagent.modules.ideator.agent_team"):
        team._check_three_section_format("free-form gen reply", "gen")
    relevant = [r for r in caplog.records if "3 段式" in r.message]
    assert relevant == []


def test_three_section_check_allows_pass_keyword(caplog):
    """PASS is the documented escape hatch; don't warn on it."""
    team = _team_for_check()
    with caplog.at_level(logging.WARNING,
                         logger="paperreadagent.modules.ideator.agent_team"):
        team._check_three_section_format("PASS", "rev1")
    relevant = [r for r in caplog.records if "3 段式" in r.message]
    assert relevant == []


# ────────────────────────────────────────────────────────
# End-to-end seam test: AgentTeamManager → facts → AgentTeam
# ────────────────────────────────────────────────────────


def test_create_team_with_spark_id_threads_facts_block_end_to_end():
    """Integration: spark_id>0 → gather_facts_for_spark → _facts_block render
    → AgentTeam.facts_block. Verifies the wiring across Tasks 1/2/4/6."""
    from paperreadagent.modules.ideator.agent_team import AgentTeamManager

    # Mock data_access.gather_facts_for_spark to return a single paper's facts
    mock_data = MagicMock()
    mock_data.insert_roundtable = MagicMock(return_value=42)
    mock_data.gather_facts_for_spark = MagicMock(return_value=[
        {
            "paper_id": 1, "title": "Wired Paper", "arxiv_id": "2401.0001",
            "relevance_score": 0.9,
            "extraction": {
                "problem": "X", "methods": ["m"], "datasets": ["d"],
                "metrics": [], "baselines": [], "limitations": [],
                "contributions": [],
            },
        },
    ])
    # Mock data_access._core for _resolve_source_context (called when spark_id>0)
    mock_data._core = MagicMock()
    mock_data._core.db.conn = MagicMock()
    mock_data.get_spark = MagicMock(return_value=None)

    # llm.load_prompt: fake it so _facts_block echoes facts count, and identity
    # prompts echo the facts_block kwarg, so we can assert end-to-end flow.
    mock_llm = MagicMock()
    def fake_load_prompt(module, name, **kw):
        if name == "_facts_block":
            facts = kw.get("facts", [])
            return f"<<FACTS:{len(facts)}>>"
        # identity prompts: echo facts_block so it appears in the system prompt
        fb = kw.get("facts_block", "")
        return f"<<{name}>>{fb}"
    mock_llm.load_prompt.side_effect = fake_load_prompt

    mgr = AgentTeamManager(
        llm=mock_llm, data_access=mock_data,
        tool_registry=create_default_registry(),
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(),
    )
    rt_id = mgr.create_team(spark_id=99, spark_content="content")
    team = mgr.get_team(rt_id)

    # Facts were collected
    mock_data.gather_facts_for_spark.assert_called_once_with(99, max_papers=8)
    # Facts were rendered through load_prompt
    assert team.facts_block == "<<FACTS:1>>"
    # Facts flow into agent system prompts
    rev1 = team.seats["rev1"]
    sysp = team._build_agent_system_prompt(rev1)
    assert "<<FACTS:1>>" in sysp or "FACTS:1" in sysp


def test_create_team_with_spark_id_zero_skips_facts_collection():
    """Direct-roundtable mode (spark_id=0): facts collection must be skipped."""
    from paperreadagent.modules.ideator.agent_team import AgentTeamManager

    mock_data = MagicMock()
    mock_data.insert_roundtable = MagicMock(return_value=7)
    mock_data.gather_facts_for_spark = MagicMock(return_value=[])

    mock_llm = MagicMock()
    mock_llm.load_prompt.return_value = "<identity>"

    mgr = AgentTeamManager(
        llm=mock_llm, data_access=mock_data,
        tool_registry=create_default_registry(),
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(),
    )
    rt_id = mgr.create_team(spark_id=0, spark_content="direct content")
    team = mgr.get_team(rt_id)

    mock_data.gather_facts_for_spark.assert_not_called()
    assert team.facts_block == ""


def test_create_team_facts_collection_failure_falls_through_to_empty():
    """If gather_facts_for_spark raises, manager logs warning + facts_block=''.
    Roundtable must still be created and usable."""
    from paperreadagent.modules.ideator.agent_team import AgentTeamManager

    mock_data = MagicMock()
    mock_data.insert_roundtable = MagicMock(return_value=11)
    mock_data.gather_facts_for_spark = MagicMock(side_effect=RuntimeError("DB exploded"))
    mock_data._core = MagicMock()
    mock_data._core.db.conn = MagicMock()
    mock_data.get_spark = MagicMock(return_value=None)

    mock_llm = MagicMock()
    mock_llm.load_prompt.return_value = "<identity>"

    mgr = AgentTeamManager(
        llm=mock_llm, data_access=mock_data,
        tool_registry=create_default_registry(),
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(),
    )
    rt_id = mgr.create_team(spark_id=55, spark_content="x")
    team = mgr.get_team(rt_id)
    # Despite the exception, the team was created
    assert team is not None
    assert team.facts_block == ""


# ────────────────────────────────────────────────────────
# 3-section soft-check: empty text regression (Minor #3)
# ────────────────────────────────────────────────────────


def test_three_section_check_skips_empty_text(caplog):
    """Empty LLM response shouldn't trigger spurious 3-段式 warning."""
    team = _team_for_check()
    with caplog.at_level(logging.WARNING,
                         logger="paperreadagent.modules.ideator.agent_team"):
        team._check_three_section_format("", "rev1")
        team._check_three_section_format("   \n  ", "rev1")
    relevant = [r for r in caplog.records if "3 段式" in r.message]
    assert relevant == []


# ────────────────────────────────────────────────────────
# Manager injects stream_hub + rt_id (Task 4)
# ────────────────────────────────────────────────────────


def test_create_team_injects_stream_hub_and_rt_id():
    """AgentTeamManager.create_team must pass get_stream_hub() and the
    issued rt_id to the new AgentTeam."""
    from paperreadagent.modules.ideator.agent_team import AgentTeamManager
    from paperreadagent.modules.ideator.stream_hub import get_stream_hub

    mock_data = MagicMock()
    mock_data.insert_roundtable = MagicMock(return_value=123)
    mock_data.gather_facts_for_spark = MagicMock(return_value=[])
    mock_data._core = MagicMock()
    mock_data._core.db.conn = MagicMock()

    mock_llm = MagicMock()
    mock_llm.load_prompt.return_value = "<identity>"

    mgr = AgentTeamManager(
        llm=mock_llm, data_access=mock_data,
        tool_registry=create_default_registry(),
        team_memory=MockTeamMemory(), graduation=MockGraduation(),
        arbiter=MockArbiter(),
    )
    rt_id = mgr.create_team(spark_id=99, spark_content="x")
    team = mgr.get_team(rt_id)

    assert team._rt_id == rt_id == 123
    assert team._stream_hub is get_stream_hub()
