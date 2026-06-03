import pytest
from paperreadagent.modules.ideator.team_memory import TeamMemory, MEMORY_TYPES, MEMORY_TYPE_LABELS


def test_memory_types_complete():
    assert set(MEMORY_TYPES) == {
        "consensus", "disagreement", "decision", "spark_evolution",
        "evidence", "user_feedback", "open_question", "assumption", "watermark",
    }
    assert len(MEMORY_TYPE_LABELS) == 9


def test_validate_type_rejects_invalid():
    tm = TeamMemory(db_conn=None)
    with pytest.raises(ValueError):
        tm._validate_type("invalid_type")


def test_validate_type_accepts_all_valid():
    tm = TeamMemory(db_conn=None)
    for mtype in MEMORY_TYPES:
        tm._validate_type(mtype)  # should not raise


def test_format_for_context_empty():
    tm = TeamMemory(db_conn=None)
    tm.read_all_types = lambda spark_id: {m: [] for m in MEMORY_TYPES}
    result = tm.format_for_context(1)
    assert result == "暂无团队记忆"


def test_format_for_context_with_memories():
    tm = TeamMemory(db_conn=None)
    tm.read_all_types = lambda spark_id: {
        "consensus": [{"content": "延迟<50ms可接受"}, {"content": "渲染方案可行"}],
        "disagreement": [{"content": "是否需要真实数据"}],
    }
    result = tm.format_for_context(1)
    assert "共识" in result or "consensus" in result
