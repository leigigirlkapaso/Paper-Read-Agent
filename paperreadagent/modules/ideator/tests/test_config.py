"""tests for ideator config"""

import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.default.yaml"


def test_config_has_model_routing():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    models = cfg["modules"]["ideator"]["models"]
    for key in ["scorer", "generator", "reviewer_1", "reviewer_2", "arbiter", "auditor"]:
        assert key in models, f"Missing model slot: {key}"


def test_arbitration_thresholds_valid():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    arb = cfg["modules"]["ideator"]["arbitration"]
    assert 0 < arb["both_high_threshold"] < 1.0
    assert 0 < arb["divergence_threshold"] < 0.5
    assert 0 < arb["both_low_threshold"] < 0.5
    assert arb["both_high_threshold"] > arb["both_low_threshold"]


def test_deepen_config_valid():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    deepen = cfg["modules"]["ideator"]["deepen"]
    assert 0 < deepen["pass_threshold"] < 1.0
    assert 1 <= deepen["max_rounds"] <= 5


def test_legacy_config_keys_preserved():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    ideator = cfg["modules"]["ideator"]
    assert "dedup_merge_threshold" in ideator
    assert "dedup_flag_threshold" in ideator
    assert "full_mine_hour" in ideator
    assert "gc_interval_minutes" in ideator
