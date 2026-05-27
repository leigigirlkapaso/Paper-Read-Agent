"""tests for SparkReviewer dual-review engine"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from paperreadagent.modules.ideator.reviewer import SparkReviewer, ReviewResult


@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat = AsyncMock()
    llm.model_name = "deepseek-v4-pro"
    return llm


@pytest.fixture
def reviewer(mock_llm):
    return SparkReviewer(
        llm=mock_llm,
        arbitration_cfg={
            "both_high_threshold": 0.8,
            "divergence_threshold": 0.25,
            "both_low_threshold": 0.4,
        },
    )


def test_review_result_overall_score():
    r = ReviewResult(
        scores={"novelty": 0.6, "evidence": 0.7, "feasibility": 0.8},
        verdict="PASS", reasoning="ok",
        reviewer_model="test-model", reviewer_role="reviewer_1",
    )
    assert r.overall == pytest.approx(0.7)


def test_decide_action_both_high(reviewer):
    r1 = ReviewResult(
        scores={"novelty": 0.9, "evidence": 0.9, "feasibility": 0.9},
        verdict="PASS", reasoning="great",
        reviewer_model="gemini-flash", reviewer_role="reviewer_1",
    )
    r2 = ReviewResult(
        scores={"novelty": 0.85, "evidence": 0.8, "feasibility": 0.9},
        verdict="PASS", reasoning="good",
        reviewer_model="qwen", reviewer_role="reviewer_2",
    )
    action, reason = reviewer._decide_action(r1, r2)
    assert action == "arbitrate_high_value"


def test_decide_action_divergence(reviewer):
    r1 = ReviewResult(
        scores={"novelty": 0.8, "evidence": 0.8, "feasibility": 0.8},
        verdict="PASS", reasoning="",
        reviewer_model="gemini-flash", reviewer_role="reviewer_1",
    )
    r2 = ReviewResult(
        scores={"novelty": 0.3, "evidence": 0.2, "feasibility": 0.3},
        verdict="REJECT", reasoning="",
        reviewer_model="qwen", reviewer_role="reviewer_2",
    )
    action, reason = reviewer._decide_action(r1, r2)
    assert action == "arbitrate_dispute"


def test_decide_action_both_low(reviewer):
    r1 = ReviewResult(
        scores={"novelty": 0.3, "evidence": 0.3, "feasibility": 0.3},
        verdict="REJECT", reasoning="",
        reviewer_model="gemini-flash", reviewer_role="reviewer_1",
    )
    r2 = ReviewResult(
        scores={"novelty": 0.2, "evidence": 0.3, "feasibility": 0.2},
        verdict="REJECT", reasoning="",
        reviewer_model="qwen", reviewer_role="reviewer_2",
    )
    action, reason = reviewer._decide_action(r1, r2)
    assert action == "reject"


def test_decide_action_moderate(reviewer):
    r1 = ReviewResult(
        scores={"novelty": 0.6, "evidence": 0.6, "feasibility": 0.6},
        verdict="PASS", reasoning="",
        reviewer_model="gemini-flash", reviewer_role="reviewer_1",
    )
    r2 = ReviewResult(
        scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
        verdict="REVISE", reasoning="",
        reviewer_model="qwen", reviewer_role="reviewer_2",
    )
    action, reason = reviewer._decide_action(r1, r2)
    assert action == "revise_pass"


@pytest.mark.asyncio
async def test_review_spark_parallel_calls(reviewer, mock_llm):
    mock_llm.chat.return_value = (
        '{"scores":{"novelty":0.7,"evidence":0.6,"feasibility":0.8},'
        '"verdict":"PASS","reasoning":"good spark"}'
    )
    r1, r2, arb = await reviewer.review_spark(
        spark_content="test spark",
        source_a_type="paper", source_a_text="source A text",
        source_b_type="note", source_b_text="source B text",
        skip_arbitration=True,
    )
    assert r1.reviewer_role == "reviewer_1"
    assert r2.reviewer_role == "reviewer_2"
    assert r1.scores["novelty"] == 0.7
    assert arb is None
    assert mock_llm.chat.call_count == 2
