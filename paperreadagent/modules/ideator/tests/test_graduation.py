import pytest
from paperreadagent.modules.ideator.graduation import GraduationManager, ContextLayer, HOT_MAX, WARM_MAX, ROLES


def test_context_layer_usage_pct():
    hot = ContextLayer("hot", max_tokens=300_000)
    assert hot.usage_pct(0) == 0.0
    assert hot.usage_pct(150_000) == 50.0
    assert hot.usage_pct(300_000) == 100.0


def test_context_layer_pct_property():
    hot = ContextLayer("hot", max_tokens=100)
    hot.current_tokens = 75
    assert hot.pct == 75.0


def test_graduation_manager_triggers_at_50_percent():
    gm = GraduationManager(db_conn=None, team_memory=None)
    gm.update_layer("hot", 160_000)  # >50% of 300K
    assert gm.needs_graduation() is True


def test_graduation_manager_no_trigger_below_50():
    gm = GraduationManager(db_conn=None, team_memory=None)
    gm.update_layer("hot", 100_000)  # <50%
    assert gm.needs_graduation() is False


def test_recommend_quota_loosens_when_low():
    gm = GraduationManager(db_conn=None, team_memory=None)
    gm.update_layer("hot", 50_000)   # ~16%
    gm.update_layer("warm", 30_000)  # ~15%
    quotas = gm.recommend_quota()
    assert quotas["gen"] == 1.5


def test_recommend_quota_tightens_when_high():
    gm = GraduationManager(db_conn=None, team_memory=None)
    gm.update_layer("hot", 200_000)  # ~66%
    gm.update_layer("warm", 50_000)  # ~25%
    quotas = gm.recommend_quota()
    assert quotas["gen"] == 0.7


def test_hard_compression_detection():
    gm = GraduationManager(db_conn=None, team_memory=None)
    gm.update_layer("hot", 250_000)
    gm.update_layer("warm", 180_000)  # total > 85% of 500K
    assert gm.needs_hard_compression() is True


def test_report_contains_key_info():
    gm = GraduationManager(db_conn=None, team_memory=None)
    gm.update_layer("hot", 100_000)
    gm.update_layer("warm", 50_000)
    report = gm.report()
    assert "热层" in report
    assert "温层" in report
    assert "33.3%" in report  # 100_000/300_000
