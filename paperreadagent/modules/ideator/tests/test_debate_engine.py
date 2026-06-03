"""tests for DebateEngine — 8-seat multi-round debate review"""
import pytest
from unittest.mock import MagicMock, AsyncMock
import json


def test_review_result_overall():
    from paperreadagent.modules.ideator.debate_engine import DebateReviewResult
    r = DebateReviewResult(
        scores={"novelty": 0.8, "evidence": 0.7, "feasibility": 0.6},
        key_concerns=[], strengths=[], verdict="PASS",
        reasoning="ok", reviewer="rev1",
    )
    assert r.overall == pytest.approx(0.7)


def test_review_result_empty_scores():
    from paperreadagent.modules.ideator.debate_engine import DebateReviewResult
    r = DebateReviewResult(
        scores={}, key_concerns=[], strengths=[],
        verdict="PASS", reasoning="", reviewer="rev1",
    )
    assert r.overall == 0.0


def test_debate_round_creation():
    from paperreadagent.modules.ideator.debate_engine import DebateRound
    dr = DebateRound(
        round_num=1,
        questions=[{"reviewer": "rev1", "content": "样本量太小"}],
        gen_response={"responses": [], "revised_draft": ""},
    )
    assert dr.round_num == 1
    assert len(dr.questions) == 1


def test_debate_outcome_fields():
    from paperreadagent.modules.ideator.debate_engine import DebateOutcome
    outcome = DebateOutcome(
        verdict="PASS", final_score=0.85, reasoning="good",
        debate_summary="summary", initial_reviews=[],
        debate_rounds=[], re_reviews=[], briefing=None,
        revised_draft="final draft",
    )
    assert outcome.verdict == "PASS"
    assert outcome.final_score == 0.85
    assert outcome.revised_draft == "final draft"


def test_reviewer_focus_covers_all_3():
    from paperreadagent.modules.ideator.debate_engine import REVIEWER_FOCUS
    for rid in ["rev1", "rev2", "rev3"]:
        assert rid in REVIEWER_FOCUS
        assert len(REVIEWER_FOCUS[rid]) > 10


@pytest.mark.asyncio
async def test_debate_engine_simple_pass():
    from paperreadagent.modules.ideator.debate_engine import DebateEngine

    mock_llm = MagicMock()
    review_json = json.dumps({
        "scores": {"novelty": 0.8, "evidence": 0.8, "feasibility": 0.8},
        "key_concerns": [], "strengths": ["good"], "verdict": "PASS",
        "reasoning": "solid",
    })
    mock_llm.chat = AsyncMock()
    # 8 calls: 3 initial reviews + 3 re-reviews + arb final + rec briefing
    mock_llm.chat.side_effect = [
        review_json, review_json, review_json,  # 3 reviewers
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"solid"}',  # rev1 re-review
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"solid"}',  # rev2 re-review
        '{"scores":{"novelty":0.8,"evidence":0.8,"feasibility":0.8},"key_concerns":[],"strengths":["good"],"verdict":"PASS","reasoning":"solid"}',  # rev3 re-review
        '{"verdict":"PASS","final_score":0.8,"reasoning":"ok","key_findings":["ok"],"debate_summary":"通过"}',  # arb final
        '{"background":"bg","breakthrough":"bt","innovation":"in","implementation":["s1"],"open_issues":[]}',  # rec briefing
    ]
    mock_llm.load_prompt = MagicMock(return_value="system prompt")
    mock_llm.model_for = MagicMock(return_value="deepseek-v4-pro")

    engine = DebateEngine(llm=mock_llm, data_access=MagicMock())
    result = await engine.run("spark", "draft text", "source context")
    assert result.verdict == "PASS"
    assert result.briefing is not None
    assert result.briefing["background"] == "bg"
    assert len(result.debate_rounds) == 0  # no debate needed
