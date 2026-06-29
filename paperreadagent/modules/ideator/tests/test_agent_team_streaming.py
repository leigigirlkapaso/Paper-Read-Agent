"""tests for AgentTeam._agent_speak streaming branch (with stream_hub)."""
import asyncio
import logging
import pytest
from unittest.mock import MagicMock

from paperreadagent.modules.ideator.agent_team import AgentTeam, AgentSeat
from paperreadagent.modules.ideator.tool_registry import create_default_registry


class MockTeamMemory:
    def format_for_context(self, spark_id):
        return ""
    def write(self, **kw):
        pass


class MockGraduation:
    def __init__(self):
        self.layers = {
            "hot": type("L", (), {"pct": 40.0})(),
            "warm": type("L", (), {"pct": 30.0})(),
        }


class MockArbiter:
    def calculate_round_quotas(self, hot, warm):
        return {"gen": 2000, "rev1": 800, "rev2": 800, "rev3": 800,
                "arb1": 500, "arb2": 500}
    def reset_for_new_team(self):
        pass


class FakeStreamHub:
    """Records all publish() calls. Provides close_rt no-op."""
    def __init__(self):
        self.published = []  # list of (rt_id, event_dict)
    async def publish(self, rt_id, event):
        self.published.append((rt_id, event))
    async def close_rt(self, rt_id):
        pass


def _make_team(*, stream_hub=None, rt_id=0, llm=None):
    seats = [
        AgentSeat(seat_id="rev1", role="reviewer_1", quota=800, tools=[]),
    ]
    return AgentTeam(
        spark_id=1,
        spark_content="content",
        seats=seats,
        llm=llm or MagicMock(),
        team_memory=MockTeamMemory(),
        graduation=MockGraduation(),
        arbiter=MockArbiter(),
        tool_registry=create_default_registry(),
        stream_hub=stream_hub,
        rt_id=rt_id,
    )


class _FakeLLM:
    """Stub IdeatorLLM. chat_stream yields configured deltas; chat returns full."""
    def __init__(self, *, stream_deltas=None, stream_raises=False,
                 chat_return="non-stream-reply"):
        self._stream_deltas = stream_deltas or []
        self._stream_raises = stream_raises
        self._chat_return = chat_return

    async def chat_stream(self, *, model_role, messages,
                           temperature=None, max_tokens=32768):
        for d in self._stream_deltas:
            yield d
        if self._stream_raises:
            raise RuntimeError("simulated mid-stream failure")

    async def chat(self, *, model_role, messages,
                    temperature=0.7, max_tokens=32768):
        return self._chat_return

    def load_prompt(self, module, name, **kw):
        return f"[IDENTITY:{name}]"


@pytest.mark.asyncio
async def test_agent_speak_streaming_publishes_deltas_and_end():
    """Stream path: publishes delta events + a terminal end event."""
    hub = FakeStreamHub()
    llm = _FakeLLM(stream_deltas=["alpha", "beta", "gamma"])
    team = _make_team(stream_hub=hub, rt_id=42, llm=llm)
    team.round_number = 3

    seat = team.seats["rev1"]
    msg = await team._agent_speak(seat, question="q?", mentioned=["all"])

    # 3 deltas + 1 end
    types = [e["type"] for (_rt, e) in hub.published]
    assert types == ["delta", "delta", "delta", "end"]
    # Every event carries rt_id=42 + seat_id=rev1
    assert all(rt == 42 for (rt, _e) in hub.published)
    assert all(e["seat_id"] == "rev1" for (_rt, e) in hub.published)
    # end carries the full concatenated text
    end_event = hub.published[-1][1]
    assert end_event["raw"] == "alphabetagamma"
    assert end_event["round_number"] == 3
    # Message was recorded into team.messages
    assert msg["sender_type"] == "model"
    assert msg["content"] == "alphabetagamma"
    assert team.messages[-1] is msg


@pytest.mark.asyncio
async def test_agent_speak_streaming_publishes_error_on_exception():
    """Mid-stream error: publishes type=error with partial; records system fallback."""
    hub = FakeStreamHub()
    llm = _FakeLLM(stream_deltas=["alpha", "beta"], stream_raises=True)
    team = _make_team(stream_hub=hub, rt_id=42, llm=llm)
    team.round_number = 1

    seat = team.seats["rev1"]
    msg = await team._agent_speak(seat, question="q?", mentioned=["all"])

    types = [e["type"] for (_rt, e) in hub.published]
    # 2 deltas, then an error event (no end)
    assert types == ["delta", "delta", "error"]
    error_event = hub.published[-1][1]
    assert error_event["partial"] == "alphabeta"
    assert error_event["seat_id"] == "rev1"
    # System fallback message recorded
    assert msg["sender_type"] == "system"
    assert "暂时无法回应" in msg["content"]


@pytest.mark.asyncio
async def test_agent_speak_falls_back_to_non_streaming_when_no_hub():
    """No stream_hub injected → uses llm.chat() path; no publishes happen."""
    llm = _FakeLLM(chat_return="full reply here")
    team = _make_team(stream_hub=None, rt_id=0, llm=llm)

    seat = team.seats["rev1"]
    msg = await team._agent_speak(seat, question="q?", mentioned=["all"])

    assert msg["sender_type"] == "model"
    assert msg["content"] == "full reply here"


@pytest.mark.asyncio
async def test_agent_team_streaming_three_section_check_runs_on_completed_raw(caplog):
    """Three-section soft-check runs once on raw concatenation, not per delta."""
    hub = FakeStreamHub()
    # Stream yields chunks that, when concatenated, lack 3-section markers
    llm = _FakeLLM(stream_deltas=["this is", " a reply without", " markers"])
    team = _make_team(stream_hub=hub, rt_id=42, llm=llm)

    seat = team.seats["rev1"]
    with caplog.at_level(
        logging.WARNING,
        logger="paperreadagent.modules.ideator.agent_team",
    ):
        await team._agent_speak(seat, question="q?", mentioned=["all"])

    # Exactly one 3-段式 warning fired (not three)
    matching = [r for r in caplog.records if "3 段式" in r.message]
    assert len(matching) == 1
    assert "rev1" in matching[0].message


@pytest.mark.asyncio
async def test_agent_speak_streaming_skips_quota_exhausted_seat():
    """Quota-exhausted seat returns None early, even with stream_hub present."""
    hub = FakeStreamHub()
    llm = _FakeLLM(stream_deltas=["should", "not", "appear"])
    team = _make_team(stream_hub=hub, rt_id=42, llm=llm)
    seat = team.seats["rev1"]
    seat.remaining_quota = 0  # exhaust

    result = await team._agent_speak(seat, question="q?", mentioned=["all"])
    assert result is None
    assert hub.published == []  # no events published
