"""tests for feedback loop"""
from modules.ideator.feedback_loop import adjust_weight, DISABLE_THRESHOLD


def test_useful_increases_weight():
    assert adjust_weight(1.0, "useful") == 1.05


def test_noise_decreases_weight():
    assert adjust_weight(1.0, "noise") == 0.9


def test_duplicate_decreases_weight():
    assert adjust_weight(1.0, "duplicate") == 0.9


def test_weight_clamped_at_max():
    assert adjust_weight(1.98, "useful") == 2.0


def test_weight_clamped_at_min():
    assert adjust_weight(0.03, "noise") == 0.0


def test_unknown_feedback_no_change():
    assert adjust_weight(0.5, "other") == 0.5


def test_disable_threshold_is_0_2():
    assert DISABLE_THRESHOLD == 0.2
