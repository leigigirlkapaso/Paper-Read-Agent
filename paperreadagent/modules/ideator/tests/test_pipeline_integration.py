"""Integration test for IdeatorPipeline: DB setup, note insertion, mocked run_full.

Verifies the pipeline can complete from recall through spark save with all
LLM dependencies mocked. Uses in-memory SQLite.
"""

import json
import sqlite3
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from paperreadagent.modules.ideator.schema import MIGRATIONS as IDEATOR_MIGRATIONS
from paperreadagent.modules.ideator.constants import LINK_SIMILARITY


# ── DB bootstrap ────────────────────────────────────────────────────────────

_CORE_TABLES = """
CREATE TABLE IF NOT EXISTS core_schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO core_schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS core_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_module TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    embedding TEXT DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'note',
    tags TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_core_notes_module
    ON core_notes(source_module, source_ref);
CREATE INDEX IF NOT EXISTS idx_core_notes_type
    ON core_notes(content_type);

CREATE TABLE IF NOT EXISTS core_llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_module TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _create_test_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with core + ideator tables (v1-v10)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_CORE_TABLES)
    for v in sorted(IDEATOR_MIGRATIONS.keys()):
        # Each migration may modify tables; v2's ALTER may fail on the v5
        # rebuilt table — we catch duplicates and continue.
        try:
            conn.executescript(IDEATOR_MIGRATIONS[v])
        except Exception:
            pass
    return conn


def _insert_test_notes(conn: sqlite3.Connection) -> list[int]:
    """Insert 4 test notes with realistic literature-review content. Returns IDs."""
    notes = [
        ("literature", "ref1", "Transformers achieve state-of-the-art results in NLP "
         "by leveraging self-attention mechanisms that capture long-range dependencies.",
         "insight"),
        ("literature", "ref2", "Knowledge distillation can compress large teacher models "
         "into smaller student models while retaining 97% of accuracy on GLUE benchmarks.",
         "note"),
        ("literature", "ref3", "Scaling laws show that model performance improves "
         "predictably with compute, dataset size, and parameter count.",
         "hypothesis"),
        ("literature", "ref4", "Retrieval-augmented generation combines a retriever "
         "with a generator to produce factually grounded outputs, reducing hallucination.",
         "insight"),
    ]
    ids = []
    for src_mod, src_ref, content, ctype in notes:
        cur = conn.execute(
            "INSERT INTO core_notes (source_module, source_ref, content, content_type) "
            "VALUES (?, ?, ?, ?)",
            (src_mod, src_ref, content, ctype),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return ids


# ── mock helpers ────────────────────────────────────────────────────────────

def _mock_core_for_pipeline(conn: sqlite3.Connection) -> MagicMock:
    """Build a MagicMock Core with enough wiring for IdeatorPipeline."""
    core = MagicMock()
    core.db = MagicMock()
    core.db.conn = conn

    # module_config returns ideator config dict
    core.module_config = MagicMock(return_value={
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
    })

    # LLM mock — embed returns dummy vector, chat returns spark JSON
    core.llm = MagicMock()
    core.llm.embed = AsyncMock(return_value=[0.1] * 1024)
    core.llm.load_prompt = MagicMock(return_value="mock prompt")

    # chat_with_tools: used by _generate_sparks
    core.llm.achat_with_tools = AsyncMock(return_value={
        "content": json.dumps({
            "content": "Synthetic spark: Combining distillation with RAG may "
                       "improve efficiency of deployed models.",
            "quality_score": 0.75,
        }),
        "tool_calls": None,
        "usage": {},
    })

    # achat: used by _deepen_sparks_for_review and fallback spark gen
    core.llm.achat = AsyncMock(return_value=(
        "Deepened research draft for the spark. This explores cross-modal "
        "knowledge transfer between distillation and retrieval methods.",
        MagicMock(),
    ))

    # knowledge layer — needed by DataAccess
    core.knowledge = MagicMock()
    core.knowledge.search_by_embedding = MagicMock(return_value=[])
    core.knowledge.find_contradictions = MagicMock(return_value=[])
    core.knowledge.get_notes_by_module = MagicMock(return_value=[])
    core.knowledge.get_note = MagicMock(return_value=None)

    # legacy_db bridge
    core.legacy_db = MagicMock()
    core.legacy_db.dict_row = MagicMock(return_value={})
    core.legacy_db.get_all_notes = MagicMock(return_value=[])
    core.legacy_db.get_cross_project_graph = MagicMock(return_value={"nodes": []})
    core.legacy_db.get_paper = MagicMock(return_value=None)

    # event bus
    core.event_bus = MagicMock()
    core.event_bus.emit = AsyncMock()
    core.event_bus.subscribe = MagicMock()

    return core


# ── tests ───────────────────────────────────────────────────────────────────

class TestPipelineIntegration:
    """Integration tests that exercise pipeline stages with real DB + mocks."""

    def test_db_tables_created(self):
        """Verify all necessary tables exist in the test DB."""
        conn = _create_test_db()
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [r[0] for r in tables]
        assert "core_notes" in names
        assert "ideator_sparks" in names
        assert "ideator_pipeline_runs" in names
        assert "ideator_recall_weights" in names

    def test_insert_and_query_notes(self):
        """Verify test notes can be inserted and queried."""
        conn = _create_test_db()
        ids = _insert_test_notes(conn)
        assert len(ids) == 4
        rows = conn.execute("SELECT * FROM core_notes").fetchall()
        assert len(rows) == 4
        assert rows[0]["content_type"] == "insight"
        assert "Transformers" in rows[0]["content"]

    def test_pipeline_construction_with_real_db(self):
        """Pipeline can be constructed with mock Core backed by real DB."""
        conn = _create_test_db()
        _insert_test_notes(conn)
        core = _mock_core_for_pipeline(conn)

        from paperreadagent.modules.ideator.data_access import DataAccess
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        data = DataAccess(core)
        pipeline = IdeatorPipeline(core, data)

        assert pipeline.recall is not None
        assert pipeline.store is not None
        assert pipeline.reviewer is not None
        assert pipeline.auditor is not None
        assert pipeline.debate_engine is not None

    @pytest.mark.asyncio
    async def test_pipeline_run_with_mocked_recall_and_sparks(self):
        """End-to-end: recall returns candidates → pipeline generates and saves.

        Mocks the LLM at the cross_recall level so no embedding API is needed.
        Pipeline still exercises S1 scoring (pass-through for <=10), S2 spark
        generation, and S4 dedup+save with real DB writes.
        """
        conn = _create_test_db()
        _insert_test_notes(conn)
        core = _mock_core_for_pipeline(conn)

        from paperreadagent.modules.ideator.data_access import DataAccess
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        data = DataAccess(core)

        # Wire up pipeline, then replace recall with a mock that returns
        # synthetic candidates — this bypasses the embedding-dependent S0.
        pipeline = IdeatorPipeline(core, data)

        # Inject synthetic candidates
        synthetic_candidates = [
            {
                "source_a": {
                    "type": "core_note", "id": 1,
                    "content": "Transformers achieve state-of-the-art results in NLP.",
                },
                "source_b": {
                    "type": "core_note", "id": 2,
                    "content": "Knowledge distillation compresses large models.",
                },
                "recall_path": LINK_SIMILARITY,
            },
        ]
        pipeline.recall.recall = AsyncMock(return_value=synthetic_candidates)

        # Mock SparkStore.save_spark to return a real DB id without embedding
        def _fake_save(content, source_type, source_refs, embedding,
                       quality_score, core_llm, run_id, metadata=None,
                       depth_content=""):
            cur = conn.execute(
                """INSERT INTO ideator_sparks
                   (content, status, source_type, source_refs, embedding,
                    quality_score, metadata, run_id, generator_score,
                    final_score, review_status, review_count, depth_content)
                   VALUES (?, 'seed', ?, '[]', '',
                           ?, '{}', ?, 0.0,
                           ?, 'pending', 0, ?)""",
                (content, source_type, quality_score, run_id, quality_score,
                 depth_content or ""),
            )
            conn.commit()
            return cur.lastrowid

        pipeline.store.save_spark = _fake_save

        # Mock _debate_review_sparks to pass-through (skip debate)
        pipeline._debate_review_sparks = AsyncMock(
            side_effect=lambda sparks, params, run_id: sparks,
        )

        # Run the pipeline
        spark_ids = await pipeline._run(trigger="test_integration")

        # Assertions
        assert isinstance(spark_ids, list), f"Expected list, got {type(spark_ids)}"
        assert len(spark_ids) >= 1, "Expected at least 1 spark to be saved"

        # Verify spark exists in DB
        for sid in spark_ids:
            row = conn.execute(
                "SELECT * FROM ideator_sparks WHERE id = ?", (sid,)
            ).fetchone()
            assert row is not None, f"Spark {sid} not found in DB"
            assert row["status"] == "seed"
            assert "distillation" in row["content"].lower() or \
                   "synthetic spark" in row["content"].lower()

        # Verify pipeline run recorded
        run_records = conn.execute(
            "SELECT * FROM ideator_pipeline_runs ORDER BY started_at DESC"
        ).fetchall()
        assert len(run_records) >= 1

    @pytest.mark.asyncio
    async def test_pipeline_empty_candidates_returns_empty(self):
        """When recall returns no candidates, pipeline exits early with [].

        This tests the early-exit path in _run without needing LLM mocks for
        later stages.
        """
        conn = _create_test_db()
        _insert_test_notes(conn)
        core = _mock_core_for_pipeline(conn)

        from paperreadagent.modules.ideator.data_access import DataAccess
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        data = DataAccess(core)
        pipeline = IdeatorPipeline(core, data)
        pipeline.recall.recall = AsyncMock(return_value=[])

        spark_ids = await pipeline._run(trigger="test_integration_empty")
        assert spark_ids == []

    @pytest.mark.asyncio
    async def test_pipeline_event_emission(self):
        """Verify the pipeline emits ideator:spark:created events on save."""
        conn = _create_test_db()
        _insert_test_notes(conn)
        core = _mock_core_for_pipeline(conn)

        from paperreadagent.modules.ideator.data_access import DataAccess
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        data = DataAccess(core)
        pipeline = IdeatorPipeline(core, data)

        pipeline.recall.recall = AsyncMock(return_value=[{
            "source_a": {"type": "core_note", "id": 1, "content": "Note A"},
            "source_b": {"type": "core_note", "id": 2, "content": "Note B"},
            "recall_path": LINK_SIMILARITY,
        }])

        def _fake_save(content, source_type, source_refs, embedding,
                       quality_score, core_llm, run_id, metadata=None,
                       depth_content=""):
            cur = conn.execute(
                """INSERT INTO ideator_sparks
                   (content, status, source_type, source_refs, embedding,
                    quality_score, metadata, run_id, generator_score,
                    final_score, review_status, review_count, depth_content)
                   VALUES (?, 'seed', ?, '[]', '',
                           ?, '{}', ?, 0.0,
                           ?, 'pending', 0, ?)""",
                (content, source_type, quality_score, run_id, quality_score,
                 depth_content or ""),
            )
            conn.commit()
            return cur.lastrowid

        pipeline.store.save_spark = _fake_save
        pipeline._debate_review_sparks = AsyncMock(
            side_effect=lambda sparks, params, run_id: sparks,
        )

        await pipeline._run(trigger="test_integration_event")

        # Check that event was emitted
        assert core.event_bus.emit.called, "Expected event_bus.emit to be called"
        call_args = core.event_bus.emit.call_args
        assert call_args is not None
        args, kwargs = call_args if call_args else ((), {})
        # First positional arg should be the event name
        if args:
            assert "spark:created" in args[0]

    @pytest.mark.asyncio
    async def test_pipeline_error_handling_graceful(self):
        """Pipeline._run catches exceptions and records error in state.

        Even when a stage fails unexpectedly, the pipeline should not crash
        and should write a pipeline_runs record with error info.
        """
        conn = _create_test_db()
        _insert_test_notes(conn)
        core = _mock_core_for_pipeline(conn)

        from paperreadagent.modules.ideator.data_access import DataAccess
        from paperreadagent.modules.ideator.pipeline import IdeatorPipeline

        data = DataAccess(core)
        pipeline = IdeatorPipeline(core, data)

        # recall succeeds, but _generate_sparks raises an exception
        pipeline.recall.recall = AsyncMock(return_value=[{
            "source_a": {"type": "core_note", "id": 1, "content": "A"},
            "source_b": {"type": "core_note", "id": 2, "content": "B"},
            "recall_path": LINK_SIMILARITY,
        }])

        # Make score_links fail
        pipeline._score_links = AsyncMock(side_effect=RuntimeError("Scoring failed"))
        # Also mock _save_sparks to prevent downstream issues
        pipeline._save_sparks = AsyncMock(return_value=[])

        # Should not raise
        result = await pipeline._run(trigger="test_integration_error")
        # Result is whatever _save_sparks returns in the finally block
        # Since we get exception in S1, saved_ids stays []

        # Pipeline run should still be recorded
        run_records = conn.execute(
            "SELECT * FROM ideator_pipeline_runs ORDER BY started_at DESC"
        ).fetchall()
        assert len(run_records) >= 1
