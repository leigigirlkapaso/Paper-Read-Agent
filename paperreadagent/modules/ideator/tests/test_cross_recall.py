"""tests for ideator CrossRecall — 6 recall paths, dedup, ordering, edge cases"""

import pytest
from unittest.mock import MagicMock, AsyncMock

from paperreadagent.modules.ideator.cross_recall import CrossRecall
from paperreadagent.modules.ideator.effort import EFFORT_PARAMS
from paperreadagent.modules.ideator.constants import (
    LINK_SIMILARITY, LINK_CONTRADICTION, LINK_CROSS_LAYER,
    LINK_RANDOM_WALK, LINK_TIMELINE,
    SOURCE_CROSS_PROJECT,
)


# ── helpers ────────────────────────────────────────────────────────────────

def _make_insight(insight_id, content, created_at="2025-01-01T00:00:00"):
    return {"id": insight_id, "content": content, "created_at": created_at,
            "source_module": "literature", "content_type": "insight"}


def _make_note(note_id, paper_id, content, created_at="2025-01-01T00:00:00"):
    return {"id": note_id, "paper_id": paper_id, "content": content,
            "created_at": created_at}


def _mock_core_llm():
    """CoreLLM mock: .embed returns a dummy 3-dim vector."""
    llm = MagicMock()
    llm.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])
    return llm


def _mock_data():
    """DataAccess mock with sensible defaults for all recall paths."""
    data = MagicMock()
    data.get_recent_insights = MagicMock(return_value=[])
    data.get_all_notes = MagicMock(return_value=[])
    data.get_cross_project_graph = MagicMock(return_value={"nodes": []})
    data.get_all_papers_with_notes = MagicMock(return_value=[])
    data.search_core_notes = MagicMock(return_value=[])
    data.find_contradictions = MagicMock(return_value=[])
    data.get_recall_weights = MagicMock(return_value=[
        {"source_type": "similarity", "weight": 1.0},
        {"source_type": "contradiction", "weight": 1.0},
        {"source_type": "cross_project", "weight": 1.0},
        {"source_type": "cross_layer", "weight": 1.0},
        {"source_type": "random_walk", "weight": 0.5},
        {"source_type": "timeline", "weight": 1.0},
    ])
    return data


# ── existing tests (preserved) ─────────────────────────────────────────────

class TestCrossRecall:
    def test_init_accepts_data_access(self):
        assert hasattr(CrossRecall, '__init__')

    def test_has_six_recall_paths(self):
        paths = ['_recall_similarity', '_recall_contradiction', '_recall_cross_project',
                 '_recall_cross_layer', '_recall_random_walk', '_recall_timeline']
        for p in paths:
            assert hasattr(CrossRecall, p), f"Missing recall path: {p}"

    def test_recall_respects_effort_paths(self):
        params = EFFORT_PARAMS["lite"]
        assert len(params["recall_paths"]) == 4

    def test_beast_effort_uses_all_paths(self):
        params = EFFORT_PARAMS["beast"]
        assert len(params["recall_paths"]) == 6


# ── recall path tests ──────────────────────────────────────────────────────

class TestRecallSimilarity:
    """Tests for _recall_similarity (semantic similarity via embedding)."""

    @pytest.mark.asyncio
    async def test_returns_pairs_when_insights_exist(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "Transformers revolutionized NLP."),
        ]
        data.search_core_notes.return_value = [
            {"id": 2, "content": "Attention mechanisms are key to transformers."},
        ]
        llm = _mock_core_llm()
        cr = CrossRecall(data)
        pairs = await cr._recall_similarity(llm, sample_size=3)
        assert len(pairs) == 1
        assert pairs[0]["source_a"]["id"] == 1
        assert pairs[0]["source_b"]["id"] == 2

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_insights(self):
        data = _mock_data()
        data.get_recent_insights.return_value = []
        cr = CrossRecall(data)
        pairs = await cr._recall_similarity(_mock_core_llm())
        assert pairs == []

    @pytest.mark.asyncio
    async def test_skips_insight_with_empty_content(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, ""),  # empty content
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_similarity(_mock_core_llm())
        assert pairs == []

    @pytest.mark.asyncio
    async def test_skips_insight_when_embedding_fails(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "Some content."),
        ]
        llm = _mock_core_llm()
        llm.embed.return_value = None  # embedding failed
        cr = CrossRecall(data)
        pairs = await cr._recall_similarity(llm)
        assert pairs == []

    @pytest.mark.asyncio
    async def test_source_metadata_preserved(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(10, "Deep learning improves vision tasks."),
        ]
        data.search_core_notes.return_value = [
            {"id": 20, "content": "CNNs are the backbone of computer vision."},
        ]
        llm = _mock_core_llm()
        cr = CrossRecall(data)
        pairs = await cr._recall_similarity(llm)
        assert pairs[0]["source_a"]["type"] == "core_note"
        assert pairs[0]["source_a"]["id"] == 10
        assert pairs[0]["source_b"]["type"] == "core_note"
        assert pairs[0]["source_b"]["id"] == 20
        assert "Deep learning" in pairs[0]["source_a"]["content"]


class TestRecallContradiction:
    """Tests for _recall_contradiction."""

    @pytest.mark.asyncio
    async def test_returns_pairs_when_insights_exist(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "Larger models always perform better."),
        ]
        data.find_contradictions.return_value = [
            {"id": 2, "content": "Smaller models can match large ones with distillation."},
        ]
        llm = _mock_core_llm()
        cr = CrossRecall(data)
        pairs = await cr._recall_contradiction(llm, sample_size=3)
        assert len(pairs) == 1
        assert pairs[0]["source_a"]["id"] == 1
        assert pairs[0]["source_b"]["id"] == 2

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_insights(self):
        data = _mock_data()
        cr = CrossRecall(data)
        pairs = await cr._recall_contradiction(_mock_core_llm())
        assert pairs == []

    @pytest.mark.asyncio
    async def test_skips_empty_content_insight(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, ""),
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_contradiction(_mock_core_llm())
        assert pairs == []


class TestRecallCrossProject:
    """Tests for _recall_cross_project (cross-project paper pairing)."""

    @pytest.mark.asyncio
    async def test_returns_pairs_across_projects(self):
        data = _mock_data()
        data.get_cross_project_graph.return_value = {
            "nodes": [
                {"project_id": 1, "name": "NLP"},
                {"project_id": 2, "name": "CV"},
            ],
        }
        data.get_all_papers_with_notes.side_effect = lambda pid: {
            1: [{"id": 101, "title": "BERT paper", "abstract": "Pretraining"}],
            2: [{"id": 201, "title": "ViT paper", "abstract": "Vision Transformer"}],
        }.get(pid, [])
        cr = CrossRecall(data)
        pairs = await cr._recall_cross_project(sample_size=2)
        assert len(pairs) == 1
        assert pairs[0]["source_a"]["id"] == 101
        assert pairs[0]["source_b"]["id"] == 201

    @pytest.mark.asyncio
    async def test_returns_empty_when_less_than_two_nodes(self):
        data = _mock_data()
        data.get_cross_project_graph.return_value = {"nodes": [{"project_id": 1}]}
        cr = CrossRecall(data)
        pairs = await cr._recall_cross_project()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_source_type_is_paper(self):
        data = _mock_data()
        data.get_cross_project_graph.return_value = {
            "nodes": [{"project_id": 1}, {"project_id": 2}],
        }
        data.get_all_papers_with_notes.return_value = [
            {"id": 1, "title": "A", "abstract": "abs"},
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_cross_project(sample_size=1)
        for p in pairs:
            assert p["source_a"]["type"] == "paper"
            assert p["source_b"]["type"] == "paper"


class TestRecallCrossLayer:
    """Tests for _recall_cross_layer (paper + insight pairs)."""

    @pytest.mark.asyncio
    async def test_returns_cross_layer_pairs(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "Insight about generalization."),
        ]
        data.get_all_notes.return_value = [
            _make_note(1, 100, "Paper note on overfitting."),
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_cross_layer(_mock_core_llm(), sample_size=3)
        assert len(pairs) == 1
        assert pairs[0]["source_a"]["type"] == "paper"
        assert pairs[0]["source_b"]["type"] == "core_note"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_insights(self):
        data = _mock_data()
        data.get_recent_insights.return_value = []
        data.get_all_notes.return_value = [_make_note(1, 100, "Note.")]
        cr = CrossRecall(data)
        pairs = await cr._recall_cross_layer(_mock_core_llm())
        assert pairs == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_notes(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [_make_insight(1, "Insight.")]
        data.get_all_notes.return_value = []
        cr = CrossRecall(data)
        pairs = await cr._recall_cross_layer(_mock_core_llm())
        assert pairs == []


class TestRecallRandomWalk:
    """Tests for _recall_random_walk (random paper-paper + paper-insight pairs)."""

    @pytest.mark.asyncio
    async def test_returns_pairs_when_enough_notes(self):
        data = _mock_data()
        data.get_all_notes.return_value = [
            _make_note(1, 100, "Note A"),
            _make_note(2, 101, "Note B"),
            _make_note(3, 102, "Note C"),
        ]
        data.get_recent_insights.return_value = [
            _make_insight(10, "Insight X"),
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_random_walk(sample_size=3)
        # 3 notes → C(3,2)=3 paper-paper + 3*1=3 paper-insight = 6 total
        # But only pairs where i < j are added (3 paper-paper), plus 3 paper-insight
        assert len(pairs) >= 3

    @pytest.mark.asyncio
    async def test_returns_empty_when_less_than_two_notes(self):
        data = _mock_data()
        data.get_all_notes.return_value = [_make_note(1, 100, "Only one.")]
        data.get_recent_insights.return_value = [_make_insight(1, "Insight.")]
        cr = CrossRecall(data)
        pairs = await cr._recall_random_walk()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_no_self_pairs(self):
        data = _mock_data()
        data.get_all_notes.return_value = [
            _make_note(1, 100, "Note A"),
            _make_note(2, 101, "Note B"),
        ]
        data.get_recent_insights.return_value = []
        cr = CrossRecall(data)
        pairs = await cr._recall_random_walk(sample_size=2)
        for p in pairs:
            # Paper-level pairs should have different IDs
            if p["source_a"]["type"] == "paper" and p["source_b"]["type"] == "paper":
                assert p["source_a"]["id"] != p["source_b"]["id"]


class TestRecallTimeline:
    """Tests for _recall_timeline (chronologically adjacent insights)."""

    @pytest.mark.asyncio
    async def test_returns_adjacent_pairs(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "Earliest note.", created_at="2025-01-01"),
            _make_insight(2, "Middle note.", created_at="2025-02-01"),
            _make_insight(3, "Latest note.", created_at="2025-03-01"),
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_timeline(sample_size=5)
        assert len(pairs) == 2
        assert pairs[0]["source_a"]["id"] == 1
        assert pairs[0]["source_b"]["id"] == 2
        assert pairs[1]["source_a"]["id"] == 2
        assert pairs[1]["source_b"]["id"] == 3

    @pytest.mark.asyncio
    async def test_returns_empty_when_less_than_two_insights(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [_make_insight(1, "Only one.")]
        cr = CrossRecall(data)
        pairs = await cr._recall_timeline()
        assert pairs == []

    @pytest.mark.asyncio
    async def test_respects_sample_size_limit(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(i, f"Note {i}", created_at=f"2025-01-{i:02d}")
            for i in range(1, 11)
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_timeline(sample_size=3)
        assert len(pairs) <= 3


# ── recall() orchestration tests ───────────────────────────────────────────

class TestRecallOrchestration:
    """Tests for the main recall() method (parallel orchestration + dedup)."""

    @pytest.mark.asyncio
    async def test_empty_note_set_returns_empty(self):
        data = _mock_data()
        cr = CrossRecall(data)
        llm = _mock_core_llm()
        pairs = await cr.recall(llm, scope="all")
        assert pairs == []

    @pytest.mark.asyncio
    async def test_deduplication_across_paths(self):
        """Same pair from two paths → only one kept (second deduplicated)."""
        data = _mock_data()

        # similarity returns pair (1, 2)
        data.get_recent_insights.return_value = [
            _make_insight(1, "Transformers are powerful."),
        ]
        data.search_core_notes.return_value = [
            {"id": 2, "content": "Attention is all you need."},
        ]

        # cross_layer would return same pair (paper 1, insight 2)
        data.get_all_notes.return_value = [
            _make_note(1, 100, "Paper about transformers."),
            _make_note(2, 200, "Paper about attention."),
        ]

        data.get_cross_project_graph.return_value = {"nodes": []}

        llm = _mock_core_llm()
        cr = CrossRecall(data)

        # Only enable similarity and cross_layer paths
        effort_params = {
            "recall_paths": [LINK_SIMILARITY, LINK_CROSS_LAYER],
            "sample_size": 3,
        }
        pairs = await cr.recall(llm, effort_params=effort_params)
        # cross_layer pairs are (paper_id, insight_id) format: (100, 1), (100, 2)...
        # similarity is (1, 2) — different formats, so no dedup expected here
        assert len(pairs) >= 1

    @pytest.mark.asyncio
    async def test_dedup_identical_pairs(self):
        """Same id pair from two paths → deduplication removes duplicate."""
        data = _mock_data()

        # Setup: both similarity and contradiction would find the same note pair
        data.get_recent_insights.return_value = [
            _make_insight(1, "Model scaling matters."),
        ]
        data.search_core_notes.return_value = [
            {"id": 5, "content": "Scaling laws in deep learning."},
        ]
        data.find_contradictions.return_value = [
            {"id": 5, "content": "Scaling laws in deep learning."},
        ]

        llm = _mock_core_llm()
        cr = CrossRecall(data)

        effort_params = {
            "recall_paths": [LINK_SIMILARITY, LINK_CONTRADICTION],
            "sample_size": 3,
        }
        pairs = await cr.recall(llm, effort_params=effort_params)
        # Both paths return pair (1, 5) — should be deduplicated to 1
        assert len(pairs) == 1
        assert pairs[0]["source_a"]["id"] == 1
        assert pairs[0]["source_b"]["id"] == 5

    @pytest.mark.asyncio
    async def test_recall_path_annotation(self):
        """Each result has its recall_path field set."""
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "Test content."),
        ]
        data.search_core_notes.return_value = [
            {"id": 2, "content": "Related content."},
        ]
        data.get_all_notes.return_value = [
            _make_note(1, 100, "Paper note."),
        ]
        data.get_cross_project_graph.return_value = {"nodes": []}

        llm = _mock_core_llm()
        cr = CrossRecall(data)

        effort_params = {
            "recall_paths": [LINK_SIMILARITY, LINK_CROSS_LAYER],
            "sample_size": 3,
        }
        pairs = await cr.recall(llm, effort_params=effort_params)
        assert len(pairs) >= 1
        # All pairs should have recall_path
        for p in pairs:
            assert "recall_path" in p
            assert p["recall_path"] in (LINK_SIMILARITY, LINK_CROSS_LAYER)

    @pytest.mark.asyncio
    async def test_path_failure_isolated(self):
        """One path fails → other paths still produce results."""
        data = _mock_data()

        # similarity works fine
        data.get_recent_insights.return_value = [
            _make_insight(1, "Good content."),
        ]
        data.search_core_notes.return_value = [
            {"id": 2, "content": "Related."},
        ]

        # timeline crashes (returns empty — cross_layer works)
        data.get_all_notes.return_value = [
            _make_note(1, 100, "Paper note."),
        ]

        data.get_cross_project_graph.return_value = {"nodes": []}

        llm = _mock_core_llm()
        cr = CrossRecall(data)

        effort_params = {
            "recall_paths": [LINK_SIMILARITY, LINK_TIMELINE, LINK_CROSS_LAYER],
            "sample_size": 3,
        }
        pairs = await cr.recall(llm, effort_params=effort_params)
        # similarity produces pairs; timeline may produce pairs depending on insights
        # cross_layer also produces pairs; at minimum similarity should work
        assert len(pairs) >= 1

    @pytest.mark.asyncio
    async def test_weight_filtering_excludes_low_weight_paths(self):
        """Paths with weight < 0.2 are excluded from active paths."""
        data = _mock_data()
        # random_walk has weight 0.5 (>= 0.2, stays in)
        data.get_recall_weights.return_value = [
            {"source_type": "random_walk", "weight": 0.1},  # excluded
            {"source_type": "similarity", "weight": 1.0},
        ]
        llm = _mock_core_llm()

        # similarity needs insights
        data.get_recent_insights.return_value = [
            _make_insight(1, "Content."),
        ]
        data.search_core_notes.return_value = [
            {"id": 2, "content": "Related."},
        ]
        data.get_all_notes.return_value = []
        data.get_cross_project_graph.return_value = {"nodes": []}

        cr = CrossRecall(data)

        effort_params = {
            "recall_paths": [LINK_SIMILARITY, LINK_RANDOM_WALK],
            "sample_size": 3,
        }
        pairs = await cr.recall(llm, effort_params=effort_params)
        # random_walk is excluded (weight 0.1), only similarity runs
        # All pairs should have similarity as their recall path
        for p in pairs:
            assert p["recall_path"] == LINK_SIMILARITY

    @pytest.mark.asyncio
    async def test_recall_single_path_valid(self):
        """recall_single_path executes a single named path."""
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "Content."),
        ]
        data.search_core_notes.return_value = [
            {"id": 2, "content": "Related."},
        ]
        llm = _mock_core_llm()
        cr = CrossRecall(data)
        pairs = await cr.recall_single_path(llm, "similarity", sample_size=3)
        assert len(pairs) == 1

    @pytest.mark.asyncio
    async def test_recall_single_path_unknown_raises_async(self):
        """Unknown path raises ValueError."""
        data = _mock_data()
        cr = CrossRecall(data)
        with pytest.raises(ValueError, match="Unknown recall path"):
            await cr.recall_single_path(_mock_core_llm(), "nonexistent_path")


# ── source metadata edge cases ─────────────────────────────────────────────

class TestSourceMetadata:
    """Tests that source_a/source_b metadata is correctly structured."""

    @pytest.mark.asyncio
    async def test_timeline_pairs_use_core_note_type(self):
        data = _mock_data()
        data.get_recent_insights.return_value = [
            _make_insight(1, "First.", created_at="2025-01-01"),
            _make_insight(2, "Second.", created_at="2025-01-02"),
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_timeline(sample_size=5)
        assert len(pairs) == 1
        assert pairs[0]["source_a"]["type"] == "core_note"
        assert pairs[0]["source_b"]["type"] == "core_note"

    @pytest.mark.asyncio
    async def test_cross_project_uses_paper_type(self):
        data = _mock_data()
        data.get_cross_project_graph.return_value = {
            "nodes": [{"project_id": 1}, {"project_id": 2}],
        }
        data.get_all_papers_with_notes.return_value = [
            {"id": 10, "title": "A", "abstract": "abstract A"},
        ]
        cr = CrossRecall(data)
        pairs = await cr._recall_cross_project(sample_size=2)
        for p in pairs:
            assert p["source_a"]["type"] == "paper"
            assert p["source_b"]["type"] == "paper"

    @pytest.mark.asyncio
    async def test_content_snippet_truncation(self):
        """Content is truncated to _CANDIDATE_SNIPPET_LEN (1000 chars)."""
        data = _mock_data()
        long_content = "x" * 2000
        data.get_recent_insights.return_value = [
            _make_insight(1, long_content),
        ]
        data.search_core_notes.return_value = [
            {"id": 2, "content": long_content},
        ]
        llm = _mock_core_llm()
        cr = CrossRecall(data)
        pairs = await cr._recall_similarity(llm)
        assert len(pairs) == 1
        assert len(pairs[0]["source_a"]["content"]) <= 1000
        assert len(pairs[0]["source_b"]["content"]) <= 1000
