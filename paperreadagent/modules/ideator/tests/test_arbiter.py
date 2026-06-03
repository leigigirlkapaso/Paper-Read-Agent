import pytest
from unittest.mock import AsyncMock
from paperreadagent.modules.ideator.arbiter import Arbiter, DEFAULT_QUOTAS, ROLES


class MockToolRegistry:
    def __init__(self):
        self.grants = {}
    def can_call(self, role, tool):
        return tool in ("search_papers", "read_memory")
    def grant_tool(self, role, tool, reason="", duration_rounds=1):
        self.grants[(role, tool)] = True


class MockGraduation:
    def __init__(self, hot_pct=40.0, warm_pct=30.0):
        self.layers = {
            "hot": type("L", (), {"pct": hot_pct})(),
            "warm": type("L", (), {"pct": warm_pct})(),
        }
        self.snapshots = []
    def store_cold_snapshot(self, **kw):
        self.snapshots.append(kw)
        return 1


class MockTeamMemory:
    def __init__(self):
        self.writes = []
    def write(self, **kw):
        self.writes.append(kw)
        return 1


def test_calculate_quotas_normal():
    reg = MockToolRegistry()
    grad = MockGraduation(40.0, 30.0)
    mem = MockTeamMemory()
    arb = Arbiter(llm=None, graduation=grad, tool_registry=reg, team_memory=mem)
    quotas = arb.calculate_round_quotas(40.0, 30.0)
    assert quotas["gen"] == 2000
    assert quotas["arb1"] == 500


def test_calculate_quotas_tight():
    reg = MockToolRegistry()
    grad = MockGraduation(70.0, 30.0)
    mem = MockTeamMemory()
    arb = Arbiter(llm=None, graduation=grad, tool_registry=reg, team_memory=mem)
    quotas = arb.calculate_round_quotas(70.0, 30.0)
    assert quotas["gen"] == 1400
    assert quotas["rev1"] == 560


def test_evaluate_tool_request_denied():
    reg = MockToolRegistry()
    grad = MockGraduation()
    mem = MockTeamMemory()
    arb = Arbiter(llm=None, graduation=grad, tool_registry=reg, team_memory=mem)
    result = arb.evaluate_tool_request("rev1", "create_spark", "need spark")
    assert result["approved"] is False


def test_evaluate_tool_request_trigger_recall():
    reg = MockToolRegistry()
    grad = MockGraduation()
    mem = MockTeamMemory()
    arb = Arbiter(llm=None, graduation=grad, tool_registry=reg, team_memory=mem)
    result = arb.evaluate_tool_request("gen", "trigger_recall", "need more")
    assert result["approved"] is True
    assert arb._recall_count == 1


def test_max_recalls_enforced():
    reg = MockToolRegistry()
    grad = MockGraduation()
    mem = MockTeamMemory()
    arb = Arbiter(llm=None, graduation=grad, tool_registry=reg, team_memory=mem)
    arb._recall_count = 2
    result = arb.evaluate_tool_request("gen", "trigger_recall", "need more")
    assert result["approved"] is False
    assert "Maximum" in result["reason"]


@pytest.mark.asyncio
async def test_execute_graduation_persists():
    reg = MockToolRegistry()
    grad = MockGraduation(50.0, 40.0)
    mem = MockTeamMemory()
    arb = Arbiter(llm=None, graduation=grad, tool_registry=reg, team_memory=mem)

    mock_llm = type("M", (), {
        "chat": AsyncMock(return_value='{"verdict":"ok","consensus":["test"]}'),
    })()
    arb._llm = mock_llm
    decision = await arb.execute_graduation(
        roundtable_id=1, spark_id=1, round_number=1,
        round_content="test content", existing_memories="",
    )
    assert len(grad.snapshots) == 1
    assert len(mem.writes) >= 1
    assert "verdict" in decision
