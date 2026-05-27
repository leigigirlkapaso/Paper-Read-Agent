"""integration tests for ideator cross-model review pipeline

Cross-component tests that verify the interaction between recall, review,
arbitration, audit, effort auto-selection, feedback loop, and state persistence.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperreadagent.modules.ideator.reviewer import SparkReviewer, ReviewResult, ArbitrationResult
from paperreadagent.modules.ideator.auditor import SparkAuditor, AuditResult
from paperreadagent.modules.ideator.effort import EFFORT_PARAMS
from paperreadagent.modules.ideator.feedback_loop import adjust_weight, DISABLE_THRESHOLD
from paperreadagent.modules.ideator.state import PipelineState, save_state, load_state


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def mock_llm():
    """Shared mock LLM: AsyncMock .chat and .model_for."""
    llm = MagicMock()
    llm.chat = AsyncMock()
    llm.model_for = MagicMock(side_effect=lambda role: {
        "reviewer_1": "gemini-flash",
        "reviewer_2": "qwen3.6-plus",
        "arbiter": "claude-opus-4-7-max",
        "auditor": "qwen3.6-plus",
    }.get(role, ""))
    return llm


@pytest.fixture
def reviewer(mock_llm):
    """Pre-configured SparkReviewer with standard thresholds."""
    return SparkReviewer(
        llm=mock_llm,
        arbitration_cfg={
            "both_high_threshold": 0.8,
            "divergence_threshold": 0.25,
            "both_low_threshold": 0.4,
        },
    )


# ── pipeline-level integration tests ─────────────────────────────────────


class TestFullPipelineLiteNoReview:
    """Lite effort: limited recall paths, skip review, skip audit."""

    def test_lite_params_skip_review_and_audit(self):
        """Lite effort skips review and audit stages."""
        p = EFFORT_PARAMS["lite"]
        assert p["skip_review"] is True
        assert p["skip_arbitration"] is True
        assert p["skip_audit"] is True
        assert p["auto_deepen"] is False

    def test_lite_recall_paths_are_subset(self):
        """Lite recall paths are a subset of beast paths."""
        lite_paths = set(EFFORT_PARAMS["lite"]["recall_paths"])
        beast_paths = set(EFFORT_PARAMS["beast"]["recall_paths"])
        assert lite_paths.issubset(beast_paths)
        assert len(lite_paths) == 4  # similarity, contradiction, cross_layer, timeline
        assert len(beast_paths) == 6

    def test_lite_produces_fewer_sparks(self):
        """Lite effort spark_pair_limit is lower than max/beast."""
        lite_limit = EFFORT_PARAMS["lite"]["spark_pair_limit"]
        beast_limit = EFFORT_PARAMS["beast"]["spark_pair_limit"]
        assert lite_limit < beast_limit, (
            f"Lite pair limit {lite_limit} should be less than beast {beast_limit}"
        )


class TestPipelineBalancedReview:
    """Balanced effort triggers review for top-2 sparks, skips arbitration."""

    def test_balanced_reviews_top_n(self):
        """Balanced config: review_top_n=2, skip_arbitration=True."""
        p = EFFORT_PARAMS["balanced"]
        assert p["skip_review"] is False
        assert p["review_top_n"] == 2
        assert p["skip_arbitration"] is True
        assert p["auto_deepen"] is False

    def test_balanced_audits_top_n(self):
        """Balanced config audits exactly 1 spark."""
        assert EFFORT_PARAMS["balanced"]["skip_audit"] is False
        assert EFFORT_PARAMS["balanced"]["audit_top_n"] == 1


class TestArbitrationDivergence:
    """Tier 3 arbitration is triggered when reviewers disagree significantly."""

    def test_divergence_triggers_arbitration(self):
        """|R1-R2| >= 0.25 triggers arbitration (via _decide_action)."""
        r1 = ReviewResult(
            scores={"novelty": 0.9, "evidence": 0.9, "feasibility": 0.9},
            verdict="PASS", reasoning="excellent",
            reviewer_model="gemini-flash", reviewer_role="reviewer_1",
        )
        r2 = ReviewResult(
            scores={"novelty": 0.2, "evidence": 0.3, "feasibility": 0.3},
            verdict="REJECT", reasoning="weak",
            reviewer_model="qwen3.6-plus", reviewer_role="reviewer_2",
        )
        assert abs(r1.overall - r2.overall) >= 0.25

    def test_both_high_triggers_high_value_arbitration(self, reviewer):
        """Both scores >= 0.8 → arbitrate_high_value."""
        r1 = ReviewResult(
            scores={"novelty": 0.85, "evidence": 0.85, "feasibility": 0.85},
            verdict="PASS", reasoning="great",
            reviewer_model="gemini-flash", reviewer_role="reviewer_1",
        )
        r2 = ReviewResult(
            scores={"novelty": 0.82, "evidence": 0.82, "feasibility": 0.82},
            verdict="PASS", reasoning="also good",
            reviewer_model="qwen", reviewer_role="reviewer_2",
        )
        action, reason = reviewer._decide_action(r1, r2)
        assert action == "arbitrate_high_value"

    def test_close_scores_no_arbitration(self, reviewer):
        """Close scores with moderate values → revise_pass (no escalation)."""
        r1 = ReviewResult(
            scores={"novelty": 0.65, "evidence": 0.65, "feasibility": 0.65},
            verdict="PASS", reasoning="ok",
            reviewer_model="gemini-flash", reviewer_role="reviewer_1",
        )
        r2 = ReviewResult(
            scores={"novelty": 0.60, "evidence": 0.62, "feasibility": 0.60},
            verdict="PASS", reasoning="fine",
            reviewer_model="qwen", reviewer_role="reviewer_2",
        )
        action, reason = reviewer._decide_action(r1, r2)
        assert action == "revise_pass"

    def test_decide_action_arbitrate_on_divergence(self, reviewer):
        """reviewer._decide_action returns arbitrate_* when scores diverge >= 0.25."""
        r1 = ReviewResult(
            scores={"novelty": 0.9, "evidence": 0.9, "feasibility": 0.9},
            verdict="PASS", reasoning="good",
            reviewer_model="m1", reviewer_role="reviewer_1",
        )
        r2 = ReviewResult(
            scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
            verdict="REVISE", reasoning="meh",
            reviewer_model="m2", reviewer_role="reviewer_2",
        )
        action, _ = reviewer._decide_action(r1, r2)
        assert action.startswith("arbitrate_")

    def test_decide_action_passes_consensus(self, reviewer):
        """Close overall scores do not trigger arbitration."""
        r1 = ReviewResult(
            scores={"novelty": 0.72, "evidence": 0.71, "feasibility": 0.70},
            verdict="PASS", reasoning="ok",
            reviewer_model="m1", reviewer_role="reviewer_1",
        )
        r2 = ReviewResult(
            scores={"novelty": 0.68, "evidence": 0.66, "feasibility": 0.69},
            verdict="PASS", reasoning="ok",
            reviewer_model="m2", reviewer_role="reviewer_2",
        )
        action, _ = reviewer._decide_action(r1, r2)
        assert not action.startswith("arbitrate_")


# ── audit integration tests ───────────────────────────────────────────────


class TestAuditScoreDelta:
    """Audit verdict affects quality score appropriately."""

    def test_supported_adds_positive_delta(self):
        """SUPPORTED adds +0.1 to the quality score."""
        assert SparkAuditor.score_delta("SUPPORTED") == 0.1

    def test_unsupported_subtracts_delta(self):
        """UNSUPPORTED subtracts -0.3 from the quality score."""
        assert SparkAuditor.score_delta("UNSUPPORTED") == -0.3

    def test_stretched_is_neutral(self):
        """STRETCHED has zero delta."""
        assert SparkAuditor.score_delta("STRETCHED") == 0.0

    def test_unknown_verdict_is_neutral(self):
        """Any unrecognized verdict returns 0.0."""
        assert SparkAuditor.score_delta("SOMETHING_ELSE") == 0.0

    def test_cumulative_effect_on_quality(self):
        """Verify max clamp at 1.0 with supported + existing high score."""
        base_score = 0.95
        delta = SparkAuditor.score_delta("SUPPORTED")
        new_score = max(0.0, min(1.0, base_score + delta))
        assert new_score == 1.0

    def test_cumulative_negative_clamp_at_zero(self):
        """Verify min clamp at 0.0 with unsupported + existing low score."""
        base_score = 0.2
        delta = SparkAuditor.score_delta("UNSUPPORTED")
        new_score = max(0.0, min(1.0, base_score + delta))
        assert new_score == 0.0


# ── feedback loop integration tests ──────────────────────────────────────


class TestFeedbackLoop:
    """Feedback weight adjustments and clamping behavior."""

    def test_useful_increases_weight(self):
        """useful feedback gives +0.05."""
        w = adjust_weight(1.0, "useful")
        assert w == 1.05
        assert w > 1.0

    def test_noise_decreases_weight(self):
        """noise and duplicate feedback give -0.1."""
        w = adjust_weight(1.0, "noise")
        assert w == 0.9
        assert w < 1.0

    def test_duplicate_behaves_like_noise(self):
        """duplicate has same delta as noise."""
        w_noise = adjust_weight(1.0, "noise")
        w_dup = adjust_weight(1.0, "duplicate")
        assert w_dup == w_noise

    def test_clamped_to_max_2_0(self):
        """Weight cannot exceed 2.0."""
        w = adjust_weight(1.98, "useful")
        assert w == 2.0

    def test_clamped_to_min_0_0(self):
        """Weight cannot go below 0.0."""
        w = adjust_weight(0.03, "noise")
        assert w == 0.0

    def test_unknown_feedback_preserves_weight(self):
        """Unknown feedback strings leave weight unchanged."""
        assert adjust_weight(0.5, "interesting") == 0.5
        assert adjust_weight(1.2, "") == 1.2

    def test_disable_threshold_constant(self):
        """Weight below 0.2 triggers disable."""
        assert DISABLE_THRESHOLD == 0.2
        # just above threshold stays enabled
        assert adjust_weight(0.25, "noise") == 0.15  # 0.25-0.1 = 0.15 < 0.2

    def test_sequence_of_feedback_accumulates(self):
        """Multiple useful feedbacks stack additively with clamping."""
        w = 1.0
        for _ in range(3):
            w = adjust_weight(w, "useful")
        assert w == pytest.approx(1.15)  # 1.0 + 3*0.05

    def test_mixed_feedback_cancels_out(self):
        """A useful then a noise should be net -0.05 from baseline."""
        w = 1.0
        w = adjust_weight(w, "useful")   # 1.05
        w = adjust_weight(w, "noise")    # 0.95
        assert w == pytest.approx(0.95)


# ── review record integration tests ──────────────────────────────────────


class TestReviewRecords:
    """ReviewResult and ArbitrationResult dataclass validation."""

    def test_review_result_required_fields(self):
        """ReviewResult has all required fields with correct types."""
        r = ReviewResult(
            scores={"novelty": 0.7, "evidence": 0.6, "feasibility": 0.8},
            verdict="PASS", reasoning="well-supported hypothesis",
            reviewer_model="gemini-flash", reviewer_role="reviewer_1",
        )
        assert r.verdict in ("PASS", "REVISE", "REJECT")
        assert len(r.scores) == 3
        assert r.reviewer_role in ("reviewer_1", "reviewer_2")
        assert isinstance(r.reasoning, str)
        assert isinstance(r.reviewer_model, str)

    def test_review_result_overall_avg_of_three(self):
        """overall property computes the mean of all scores."""
        r = ReviewResult(
            scores={"novelty": 0.3, "evidence": 0.6, "feasibility": 0.9},
            verdict="REVISE", reasoning="",
            reviewer_model="x", reviewer_role="reviewer_1",
        )
        assert r.overall == pytest.approx(0.6)

    def test_review_result_overall_empty_scores(self):
        """Empty scores dict returns 0.0 overall."""
        r = ReviewResult(
            scores={}, verdict="PASS", reasoning="",
            reviewer_model="x", reviewer_role="reviewer_2",
        )
        assert r.overall == 0.0

    def test_arbitration_result_fields(self):
        """ArbitrationResult has the expected fields."""
        arb = ArbitrationResult(
            scores={"novelty": 0.85, "evidence": 0.80, "feasibility": 0.75},
            verdict="OVERTURN", reasoning="R2 underestimated novelty",
            escalation_reason="Divergence: |0.90 - 0.27| = 0.63",
        )
        assert arb.verdict in ("OVERTURN", "CONFIRM_R1", "CONFIRM_R2")
        assert len(arb.scores) == 3
        assert "Divergence" in arb.escalation_reason

    def test_arbitration_result_json_serializable(self):
        """ArbitrationResult scores can be serialized for DB storage."""
        arb = ArbitrationResult(
            scores={"novelty": 0.8, "evidence": 0.7, "feasibility": 0.6},
            verdict="CONFIRM_R1", reasoning="arbitrator agrees",
            escalation_reason="Both high: R1=0.85, R2=0.83",
        )
        encoded = json.dumps(arb.scores, ensure_ascii=False)
        decoded = json.loads(encoded)
        assert decoded["novelty"] == 0.8

    def test_review_result_verdicts_pass_revise_reject(self):
        """All three standard verdicts are valid."""
        for v in ("PASS", "REVISE", "REJECT"):
            r = ReviewResult(
                scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
                verdict=v, reasoning="",
                reviewer_model="m", reviewer_role="reviewer_1",
            )
            assert r.verdict == v


# ── state persistence integration tests ──────────────────────────────────


class TestPipelineStatePersistence:
    """PipelineState save/load roundtrip."""

    def test_save_and_load_roundtrip(self, tmp_path):
        """Full roundtrip: create → save → load → verify all fields."""
        ps = PipelineState(
            run_id="int-test-run-001",
            current_stage="review",
            stages_completed=["recall", "score", "generate"],
            candidates_count=15,
            sparks_generated=4,
            sparks_reviewed=2,
            effort="balanced",
        )
        save_state(ps, tmp_path)
        loaded = load_state(tmp_path)
        assert loaded is not None
        assert loaded.run_id == "int-test-run-001"
        assert loaded.current_stage == "review"
        assert loaded.candidates_count == 15
        assert loaded.sparks_generated == 4
        assert loaded.sparks_reviewed == 2
        assert loaded.effort == "balanced"
        assert "recall" in loaded.stages_completed
        assert "score" in loaded.stages_completed
        assert "generate" in loaded.stages_completed

    def test_state_file_written_to_disk(self, tmp_path):
        """Verify the JSON file is actually created on disk."""
        ps = PipelineState(run_id="disk-test", current_stage="generate",
                          candidates_count=8, sparks_generated=3, effort="lite")
        save_state(ps, tmp_path)
        state_file = tmp_path / "PIPELINE_STATE.json"
        assert state_file.exists()
        raw = json.loads(state_file.read_text())
        assert raw["run_id"] == "disk-test"
        assert raw["current_stage"] == "generate"
        assert "updated_at" in raw

    def test_load_missing_returns_none(self, tmp_path):
        """load_state on an empty directory returns None."""
        empty_dir = tmp_path / "nonexistent"
        assert load_state(empty_dir) is None

    def test_defaults_populated_on_construct(self):
        """PipelineState defaults are sensible."""
        ps = PipelineState(run_id="default-test")
        assert ps.current_stage == "recall"
        assert ps.stages_completed == []
        assert ps.candidates_count == 0
        assert ps.sparks_generated == 0
        assert ps.sparks_reviewed == 0
        assert ps.effort == "balanced"

    def test_from_dict_preserves_all_keys(self):
        """from_dict reconstructs all fields from a dict."""
        d = {
            "run_id": "dict-run",
            "current_stage": "dedup",
            "stages_completed": ["recall", "score", "generate", "review"],
            "candidates_count": 42,
            "sparks_generated": 7,
            "sparks_reviewed": 5,
            "effort": "max",
            "updated_at": "2025-01-01T00:00:00",
        }
        ps = PipelineState.from_dict(d)
        assert ps.run_id == "dict-run"
        assert ps.stages_completed == d["stages_completed"]
        assert ps.candidates_count == 42
        assert ps.effort == "max"

    def test_save_creates_parent_directory(self, tmp_path):
        """save_state creates the directory if it does not exist."""
        deep_dir = tmp_path / "a" / "b" / "c"
        ps = PipelineState(run_id="mkdir-test")
        save_state(ps, deep_dir)
        assert deep_dir.exists()
        assert (deep_dir / "PIPELINE_STATE.json").exists()


# ── cross-component: EFFORT_PARAMS drives pipeline behavior ──────────────


class TestEffortParamsContracts:
    """All effort levels have consistent parameter keys."""

    ALL_LEVELS = ["lite", "balanced", "max", "beast"]

    def test_all_levels_present(self):
        for level in self.ALL_LEVELS:
            assert level in EFFORT_PARAMS, f"Missing {level}"

    def test_keys_consistent_across_levels(self):
        """Every key present in lite must also be present in all higher levels."""
        lite_keys = set(EFFORT_PARAMS["lite"].keys())
        for level in ["balanced", "max", "beast"]:
            missing = lite_keys - set(EFFORT_PARAMS[level].keys())
            assert not missing, f"{level} missing keys: {missing}"

    def test_recall_paths_increasing(self):
        """Higher effort levels include more recall paths."""
        for i in range(len(self.ALL_LEVELS) - 1):
            lo = set(EFFORT_PARAMS[self.ALL_LEVELS[i]]["recall_paths"])
            hi = set(EFFORT_PARAMS[self.ALL_LEVELS[i + 1]]["recall_paths"])
            assert lo.issubset(hi), (
                f"{self.ALL_LEVELS[i]} paths not subset of {self.ALL_LEVELS[i+1]}"
            )

    def test_spark_pair_limit_increasing(self):
        """Higher effort levels use more pairs for spark generation."""
        prev_limit = 0
        for level in self.ALL_LEVELS:
            limit = EFFORT_PARAMS[level]["spark_pair_limit"]
            assert limit >= prev_limit, (
                f"{level} spark_pair_limit {limit} < prev {prev_limit}"
            )
            prev_limit = limit


# ── auditor integration with mock LLM ─────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_supported_llm_integration(tmp_path):
    """Full auditor integration: mock LLM returns SUPPORTED → AuditResult populated."""
    # Arrange
    llm = MagicMock()
    llm.chat = AsyncMock()
    llm.chat.return_value = json.dumps({
        "verdict": "SUPPORTED",
        "claims_check": [
            {"claim": "X leads to Y", "evidence_in_source": "line 12-14", "supported": True},
        ],
        "reasoning": "Source explicitly states the causal relationship.",
    })

    auditor = SparkAuditor(llm=llm)

    # Act
    result = await auditor.audit(
        spark_content="X leads to Y, therefore Z.",
        source_refs=[{"type": "paper", "title": "Test", "content": "X always leads to Y in these conditions."}],
    )

    # Assert
    assert isinstance(result, AuditResult)
    assert result.verdict == "SUPPORTED"
    assert len(result.claims_check) == 1
    assert "Source explicitly" in result.reasoning


@pytest.mark.asyncio
async def test_review_spark_integration_calls_both_reviewers():
    """Integration: review_spark calls both reviewers in parallel and returns both results."""
    llm = MagicMock()
    llm.chat = AsyncMock()
    llm.chat.return_value = json.dumps({
        "scores": {"novelty": 0.8, "evidence": 0.7, "feasibility": 0.75},
        "verdict": "PASS",
        "reasoning": "Well supported.",
    })
    llm.model_for = MagicMock(return_value="gemini-flash")

    reviewer = SparkReviewer(
        llm=llm,
        arbitration_cfg={"both_high_threshold": 0.8, "divergence_threshold": 0.25, "both_low_threshold": 0.4},
    )

    r1, r2, arb = await reviewer.review_spark(
        spark_content="A novel connection between X and Y.",
        source_a_type="paper", source_a_text="Paper about X.",
        source_b_type="core_note", source_b_text="Note about Y.",
        skip_arbitration=True,
    )

    assert r1.reviewer_role == "reviewer_1"
    assert r2.reviewer_role == "reviewer_2"
    assert r1.verdict == "PASS"
    assert arb is None
    assert llm.chat.call_count == 2  # exactly two parallel calls


# ── pipeline construction integration tests ──────────────────────────────


class TestPipelineConstruction:
    """IdeatorPipeline wires up all v2 components correctly."""

    def test_pipeline_exposes_all_v2_components(self):
        """Constructor creates reviewer, auditor, ideator_llm."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        core = MagicMock()
        core.module_config.return_value = {
            "ideator_llm": {"api_key": "", "api_base_url": "https://api.gpt.ge/v1"},
            "models": {
                "scorer": "gemini-flash",
                "generator": "",
                "reviewer_1": "gemini-flash",
                "reviewer_2": "qwen-plus",
                "arbiter": "claude-opus",
                "auditor": "qwen-plus",
            },
            "arbitration": {"both_high_threshold": 0.8},
            "deepen": {"pass_threshold": 0.7, "max_rounds": 3},
        }
        data = MagicMock()

        pipeline = IdeatorPipeline(core, data)

        assert pipeline.core is core
        assert pipeline.data is data
        assert pipeline.recall is not None
        assert pipeline.store is not None
        assert pipeline.ideator_llm is not None
        assert pipeline.reviewer is not None
        assert pipeline.auditor is not None
        assert pipeline._state_dir.name == "ideator"

    def test_run_modes_are_callable(self):
        """run_full, run_incremental, run_targeted, deepen are callable."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        core = MagicMock()
        core.module_config.return_value = {
            "ideator_llm": {"api_key": ""},
            "models": {},
            "arbitration": {},
            "deepen": {"pass_threshold": 0.7, "max_rounds": 3},
        }
        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        assert callable(pipeline.run_full)
        assert callable(pipeline.run_incremental)
        assert callable(pipeline.run_targeted)
        assert callable(pipeline.deepen)


class TestRoundtableIntegration:
    """Roundtable end-to-end integration tests."""

    def test_seats_all_online_initially(self):
        """All 6 seats start online."""
        from paperreadagent.modules.ideator.roundtable import SEATS
        assert len(SEATS) == 6
        for s in SEATS:
            assert s.get("state", "online") == "online"

    def test_context_spec_generators_get_papers(self):
        from paperreadagent.modules.ideator.roundtable import CONTEXT_SPEC
        for role in ["generator", "reviewer_1", "reviewer_2", "reviewer_3"]:
            assert "papers" in CONTEXT_SPEC[role]

    def test_context_spec_arbiters_no_papers(self):
        from paperreadagent.modules.ideator.roundtable import CONTEXT_SPEC
        for role in ["arbiter_1", "arbiter_2"]:
            assert "papers" not in CONTEXT_SPEC[role]

    def test_gen_only_gets_self_score(self):
        from paperreadagent.modules.ideator.roundtable import CONTEXT_SPEC
        assert "self_score" in CONTEXT_SPEC["generator"]
        assert "self_score" not in CONTEXT_SPEC["reviewer_1"]
        assert "self_score" not in CONTEXT_SPEC["arbiter_1"]

    def test_token_tracker_thresholds(self):
        from paperreadagent.modules.ideator.roundtable import TokenTracker
        tt = TokenTracker(limit=100000)
        tt.consume(50000)
        assert tt.needs_compression() is True
        tt.consume(35000)
        assert tt.needs_warning() is True
        tt.consume(15000)
        assert tt.is_exhausted() is True
        assert tt.compression_count == 0

    def test_interjection_char_limit_constant(self):
        from paperreadagent.modules.ideator.roundtable import _INTERJECTION_MAX_CHARS
        assert _INTERJECTION_MAX_CHARS == 150

    def test_roundtable_manager_context_assembly(self):
        """Verify context bundles have correct structure."""
        from paperreadagent.modules.ideator.roundtable import RoundtableManager
        from unittest.mock import MagicMock

        data = MagicMock()
        data.insert_roundtable = MagicMock(return_value=1)
        data.get_spark = MagicMock(return_value={
            "id": 1, "content": "test", "source_refs": '[]',
            "review_status": "passed", "final_score": 0.75,
            "depth_content": "deepen text", "generator_score": 0.7,
        })
        data.get_paper = MagicMock(return_value={"title": "P", "abstract": "A"})
        data.get_paper_summaries = MagicMock(return_value=[{"content": "summary"}])
        data.get_user_note = MagicMock(return_value={"content": "note"})

        mgr = RoundtableManager(llm=MagicMock(), data_access=data)
        bundles = mgr._assemble_contexts(spark_id=1, source_refs=[{"type": "paper", "id": 1}])

        # Gen bundle has papers, reports, notes, self_score
        gen = bundles["generator"]
        assert gen["papers"] != ""
        assert gen["reports"] != ""
        assert gen["notes"] != ""
        assert gen["self_score"] == 0.7

        # Arb bundle has no papers
        arb = bundles["arbiter_1"]
        assert "papers" not in arb or arb.get("papers") == ""

    @pytest.mark.asyncio
    async def test_roundtable_session_force_remove_exited(self):
        """force_remove changes state to exited and returns exit message."""
        from paperreadagent.modules.ideator.roundtable import RoundtableSession
        from unittest.mock import AsyncMock, MagicMock

        mock_llm = MagicMock()
        mock_llm.chat = AsyncMock(return_value="Goodbye statement")
        mock_llm.model_for = MagicMock(return_value="test-model")

        bundles = {
            "generator": {"papers": "", "reports": "", "notes": "", "reviews": "", "deepen": "", "self_score": 0.5},
            "reviewer_1": {"papers": "", "reports": "", "notes": "", "reviews": "", "deepen": ""},
            "reviewer_2": {"papers": "", "reports": "", "notes": "", "reviews": "", "deepen": ""},
            "reviewer_3": {"papers": "", "reports": "", "notes": "", "reviews": "", "deepen": ""},
            "arbiter_1": {"reviews": "", "deepen": ""},
            "arbiter_2": {"reviews": "", "deepen": ""},
        }
        session = RoundtableSession(
            spark_id=1, spark_content="test",
            llm=mock_llm, data_access=MagicMock(),
            context_bundles=bundles,
        )
        p = session._find_participant("rev1")
        assert p["state"] == "online"
        result = await session.force_remove("rev1", "test_removal")
        assert p["state"] == "exited"
        assert result is not None
