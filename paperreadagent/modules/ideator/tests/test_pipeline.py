"""tests for ideator IdeatorPipeline v2 (cross-model review)"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


class TestIdeatorPipeline:
    def test_pipeline_has_run_modes(self):
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        assert hasattr(IdeatorPipeline, 'run_full')
        assert hasattr(IdeatorPipeline, 'run_incremental')
        assert hasattr(IdeatorPipeline, 'run_targeted')
        assert hasattr(IdeatorPipeline, 'deepen')

    def test_pipeline_has_new_components_in_init(self):
        """Verify constructor wires up all new v2 components."""
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
        assert pipeline.debate_engine is not None
        assert pipeline._state_dir.name == "ideator"

    def test_resolve_sources_from_dict_refs(self):
        """Verify _resolve_sources handles dict source_refs."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        core = MagicMock()
        core.module_config.return_value = {
            "ideator_llm": {"api_key": ""},
            "models": {},
            "arbitration": {},
            "deepen": {"pass_threshold": 0.7, "max_rounds": 3},
        }
        data = MagicMock()
        data.get_paper.return_value = {
            "title": "Test Paper", "abstract": "An abstract.",
        }
        data._core.knowledge.get_note.return_value = {
            "content": "Note content here.",
        }

        pipeline = IdeatorPipeline(core, data)
        spark = {
            "source_refs": [
                {"type": "paper", "id": 1},
                {"type": "core_note", "id": 5},
            ],
        }
        ta, txt_a, tb, txt_b = pipeline._resolve_sources(spark)
        assert ta == "paper"
        assert "Test Paper" in txt_a
        assert tb == "core_note"
        assert "Note content here" in txt_b

    def test_resolve_sources_from_json_string(self):
        """Verify _resolve_sources handles JSON-encoded source_refs."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import json

        core = MagicMock()
        core.module_config.return_value = {
            "ideator_llm": {"api_key": ""},
            "models": {},
            "arbitration": {},
            "deepen": {"pass_threshold": 0.7, "max_rounds": 3},
        }
        data = MagicMock()
        data.get_paper.return_value = {"title": "P", "abstract": "A."}

        pipeline = IdeatorPipeline(core, data)
        spark = {
            "source_refs": json.dumps([{"type": "paper", "id": 1}]),
        }
        ta, txt_a, tb, txt_b = pipeline._resolve_sources(spark)
        assert ta == "paper"
        assert "P" in txt_a
        assert tb == ""  # only one ref

    def test_resolve_sources_empty_refs(self):
        """Verify _resolve_sources returns empty on empty/missing refs."""
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

        ta, txt_a, tb, txt_b = pipeline._resolve_sources({"source_refs": []})
        assert ta == ""
        assert txt_a == ""
        assert tb == ""
        assert txt_b == ""

    def test_resolve_all_sources_returns_structured_list(self):
        """Verify _resolve_all_sources returns list of dicts for audit."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        core = MagicMock()
        core.module_config.return_value = {
            "ideator_llm": {"api_key": ""},
            "models": {},
            "arbitration": {},
            "deepen": {"pass_threshold": 0.7, "max_rounds": 3},
        }
        data = MagicMock()
        data.get_paper.return_value = {"title": "P", "abstract": "Abs."}
        data._core.knowledge.get_note.return_value = {"content": "Note."}

        pipeline = IdeatorPipeline(core, data)
        spark = {
            "source_refs": [
                {"type": "paper", "id": 1},
                {"type": "core_note", "id": 2},
            ],
        }
        resolved = pipeline._resolve_all_sources(spark)
        assert len(resolved) == 2
        assert resolved[0]["type"] == "paper"
        assert resolved[0]["title"] == "P"
        assert resolved[1]["type"] == "core_note"

    def test_arbitration_gate_detects_divergence(self):
        """Verify reviewer._decide_action correctly flags disputes."""
        from paperreadagent.modules.ideator.reviewer import ReviewResult, SparkReviewer
        from unittest.mock import MagicMock

        r1 = ReviewResult(
            scores={"novelty": 0.9, "evidence": 0.9, "feasibility": 0.9},
            verdict="PASS", reasoning="good", reviewer_model="m1",
            reviewer_role="reviewer_1",
        )
        r2 = ReviewResult(
            scores={"novelty": 0.5, "evidence": 0.5, "feasibility": 0.5},
            verdict="REVISE", reasoning="meh", reviewer_model="m2",
            reviewer_role="reviewer_2",
        )
        mock_llm = MagicMock()
        reviewer = SparkReviewer(llm=mock_llm, arbitration_cfg={
            "both_high_threshold": 0.8, "divergence_threshold": 0.25, "both_low_threshold": 0.4,
        })
        action, _ = reviewer._decide_action(r1, r2)
        assert action.startswith("arbitrate_")

    def test_arbitration_gate_passes_consensus(self):
        """Verify reviewer._decide_action returns non-arbitrate for close scores."""
        from paperreadagent.modules.ideator.reviewer import ReviewResult, SparkReviewer
        from unittest.mock import MagicMock

        r1 = ReviewResult(
            scores={"novelty": 0.7, "evidence": 0.7, "feasibility": 0.7},
            verdict="PASS", reasoning="ok", reviewer_model="m1",
            reviewer_role="reviewer_1",
        )
        r2 = ReviewResult(
            scores={"novelty": 0.65, "evidence": 0.68, "feasibility": 0.72},
            verdict="PASS", reasoning="ok", reviewer_model="m2",
            reviewer_role="reviewer_2",
        )
        mock_llm = MagicMock()
        reviewer = SparkReviewer(llm=mock_llm, arbitration_cfg={
            "both_high_threshold": 0.8, "divergence_threshold": 0.25, "both_low_threshold": 0.4,
        })
        action, _ = reviewer._decide_action(r1, r2)
        assert not action.startswith("arbitrate_")

    def test_deepen_signature_preserved(self):
        """Verify deepen() can be called with just spark_id (routes.py compat)."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        import inspect

        sig = inspect.signature(IdeatorPipeline.deepen)
        params = list(sig.parameters.keys())
        assert "spark_id" in params
        # run_id is keyword-only optional
        assert "run_id" in params

    def test_write_pipeline_run_start_and_finish(self):
        """Verify _write_pipeline_run writes INSERT then UPDATE."""
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

        run_id = "test-run-001"
        pipeline._write_pipeline_run(run_id, "daily_cron", "max", start=True)
        assert data._core.db.conn.execute.call_count >= 1
        assert data._core.db.conn.commit.call_count >= 1

        # Reset mock and test finish
        data._core.db.conn.execute.reset_mock()
        data._core.db.conn.commit.reset_mock()
        pipeline._write_pipeline_run(
            run_id, "daily_cron", "max", start=False,
            stats={"candidates_count": 10, "stages_completed": ["recall"]},
        )
        assert data._core.db.conn.execute.call_count >= 1
        assert data._core.db.conn.commit.call_count >= 1

    def test_save_review_record_handles_both_types(self):
        """Verify _save_review_record writes ReviewResult and ArbitrationResult."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from paperreadagent.modules.ideator.reviewer import (
            ReviewResult, ArbitrationResult,
        )

        core = MagicMock()
        core.module_config.return_value = {
            "ideator_llm": {"api_key": ""},
            "models": {},
            "arbitration": {},
            "deepen": {"pass_threshold": 0.7, "max_rounds": 3},
        }
        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        r = ReviewResult(
            scores={"novelty": 0.8}, verdict="PASS", reasoning="ok",
            reviewer_model="m1", reviewer_role="reviewer_1",
        )
        pipeline._save_review_record(1, r, "review", "run-1")
        assert data._core.db.conn.execute.call_count >= 1

        data._core.db.conn.execute.reset_mock()
        arb = ArbitrationResult(
            scores={"novelty": 0.9}, verdict="OVERTURN",
            reasoning="arb", escalation_reason="divergence",
        )
        pipeline._save_review_record(1, arb, "arbitration", "run-1")
        assert data._core.db.conn.execute.call_count >= 1

    def test_save_audit_record(self):
        """Verify _save_audit_record writes audit results."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from paperreadagent.modules.ideator.auditor import AuditResult

        core = MagicMock()
        core.module_config.return_value = {
            "ideator_llm": {"api_key": ""},
            "models": {},
            "arbitration": {},
            "deepen": {"pass_threshold": 0.7, "max_rounds": 3},
        }
        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        audit = AuditResult(
            verdict="SUPPORTED", claims_check=[], reasoning="all good",
        )
        pipeline._save_audit_record(1, audit, "run-1")
        assert data._core.db.conn.execute.call_count >= 1


class TestGroupBySharedSource:
    """Tests for _group_by_shared_source — C1 greedy grouping."""

    @staticmethod
    def _make_link(a_type, a_id, a_content, b_type, b_id, b_content, score=0.5):
        return {
            "source_a": {"type": a_type, "id": a_id, "content": a_content},
            "source_b": {"type": b_type, "id": b_id, "content": b_content},
            "recall_path": "similarity",
            "relevance_score": score,
            "reasoning": "test reason",
        }

    def test_happy_path_shared_source_groups(self):
        """A×B, A×C, B×E → two groups: {A×B,A×C} around A, {B×E} around B."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock
        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B"),
            self._make_link("core_note", 1, "A", "core_note", 3, "C"),
            self._make_link("core_note", 2, "B", "paper", 5, "E"),
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        assert len(groups) == 2
        assert len(groups[0]) == 2
        assert len(groups[1]) == 1

    def test_single_pair(self):
        """One link → one group with one element."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock
        links = [self._make_link("core_note", 1, "A", "core_note", 2, "B")]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        assert len(groups) == 1
        assert len(groups[0]) == 1

    def test_empty_links(self):
        """Empty input → empty list."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source([])
        assert groups == []

    def test_orphan_pairs_each_own_group(self):
        """A×B, C×D (no shared source) → two groups of 1."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock
        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B"),
            self._make_link("core_note", 3, "C", "core_note", 4, "D"),
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        assert len(groups) == 2
        assert all(len(g) == 1 for g in groups)

    def test_max_per_group_truncation(self):
        """6 pairs sharing source A → group capped at 5, 6th becomes orphan."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock
        links = [
            self._make_link("core_note", 1, "A", "core_note", i, f"B{i}")
            for i in range(2, 8)
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links, max_per_group=5)
        assert len(groups) == 2
        assert len(groups[0]) == 5
        assert len(groups[1]) == 1

    def test_source_count_with_mixed_types(self):
        """Sources with same id but different type are NOT merged."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock
        links = [
            self._make_link("paper", 1, "A", "core_note", 2, "B"),
            self._make_link("core_note", 1, "A2", "core_note", 3, "C"),
        ]
        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        pipeline = IdeatorPipeline(core, MagicMock())
        groups = pipeline._group_by_shared_source(links)
        assert len(groups) == 2
        assert all(len(g) == 1 for g in groups)


class TestGenerateSparksPerGroup:
    """Tests for the rewritten _generate_sparks with per-group generation."""

    @staticmethod
    def _make_link(a_type, a_id, a_content, b_type, b_id, b_content, score=0.5):
        return {
            "source_a": {"type": a_type, "id": a_id, "content": a_content},
            "source_b": {"type": b_type, "id": b_id, "content": b_content},
            "recall_path": "similarity",
            "relevance_score": score,
            "reasoning": "test reason",
        }

    @pytest.mark.asyncio
    async def test_generates_sparks_from_groups(self):
        """Two groups → parallel LLM calls → sparks from both groups."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock, AsyncMock
        import json

        links = [
            self._make_link("core_note", 1, "Note A about transformers", "core_note", 2, "Note B about RLHF"),
            self._make_link("core_note", 1, "Note A about transformers", "core_note", 3, "Note C about attention"),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="generate sparks from these links")
        core.llm.achat_with_tools = AsyncMock()
        core.llm.achat_with_tools.return_value = {
            "content": json.dumps({"content": "Investigate RLHF+transformers", "quality_score": 0.8}),
            "tool_calls": None,
            "usage": {},
        }

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert len(sparks) >= 1
        for s in sparks:
            assert "source_refs" in s
            assert isinstance(s["source_refs"], list)
            assert len(s["source_refs"]) > 0
            assert "source_type" in s

    @pytest.mark.asyncio
    async def test_weak_pair_returns_empty(self):
        """LLM returns [] for a weak pair → that group contributes nothing."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock, AsyncMock
        import json

        links = [
            self._make_link("core_note", 1, "Weak A", "core_note", 2, "Weak B", score=0.1),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")
        core.llm.achat = AsyncMock()
        core.llm.achat.return_value = ("[]", MagicMock())

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert sparks == []

    @pytest.mark.asyncio
    async def test_parallel_failure_isolated(self):
        """One group's LLM call fails → other groups still produce sparks."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock, AsyncMock
        import json

        links = [
            self._make_link("core_note", 1, "Good A", "core_note", 2, "Good B", score=0.8),
            self._make_link("core_note", 3, "Bad C", "core_note", 4, "Bad D", score=0.3),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")

        call_count = [0]

        async def mock_achat_with_tools(messages, tools, tool_choice, module, purpose, max_tokens):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "content": json.dumps({"content": "Good spark", "quality_score": 0.8}),
                    "tool_calls": None,
                    "usage": {},
                }
            else:
                raise Exception("LLM timeout")

        core.llm.achat_with_tools = mock_achat_with_tools

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert len(sparks) >= 1

    @pytest.mark.asyncio
    async def test_source_refs_extracted_from_entire_group(self):
        """All unique sources in a group appear in each spark's source_refs."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock, AsyncMock
        import json

        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B"),
            self._make_link("core_note", 1, "A", "paper", 10, "D"),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")
        core.llm.achat_with_tools = AsyncMock()
        core.llm.achat_with_tools.return_value = {
            "content": json.dumps({"content": "Cross-source spark", "quality_score": 0.9}),
            "tool_calls": None,
            "usage": {},
        }

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert len(sparks) == 1
        refs = sparks[0]["source_refs"]
        assert len(refs) == 3

    @pytest.mark.asyncio
    async def test_json_parse_failure_returns_empty(self):
        """LLM returns invalid JSON → retries exhausted → group returns empty."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock, AsyncMock

        links = [
            self._make_link("core_note", 1, "A", "core_note", 2, "B", score=0.7),
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")

        async def mock_achat_with_tools(messages, tools, tool_choice, module, purpose, max_tokens):
            return {
                "content": "not valid json {{",
                "tool_calls": None,
                "usage": {},
            }

        core.llm.achat_with_tools = mock_achat_with_tools
        core.llm.achat = AsyncMock(return_value=("not valid json {{", MagicMock()))

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links)
        assert sparks == []

    @pytest.mark.asyncio
    async def test_respects_spark_pair_limit_from_params(self):
        """spark_pair_limit from params controls how many top links are used."""
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline
        from unittest.mock import MagicMock, AsyncMock
        import json

        links = [
            self._make_link("core_note", i, f"N{i}", "core_note", i + 10, f"M{i}", score=0.9 - i * 0.05)
            for i in range(1, 8)
        ]

        core = MagicMock()
        core.module_config.return_value = {"arbitration": {}, "deepen": {}}
        core.llm = MagicMock()
        core.llm.load_prompt = MagicMock(return_value="prompt")
        core.llm.achat_with_tools = AsyncMock()
        core.llm.achat_with_tools.return_value = {
            "content": json.dumps({"content": "spark", "quality_score": 0.5}),
            "tool_calls": None,
            "usage": {},
        }

        data = MagicMock()
        pipeline = IdeatorPipeline(core, data)

        sparks = await pipeline._generate_sparks(links, params={"spark_pair_limit": 3})
        assert len(sparks) == 3
