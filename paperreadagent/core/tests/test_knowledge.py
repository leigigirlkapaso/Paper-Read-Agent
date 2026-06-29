"""
core/tests/test_knowledge.py
Tests for KnowledgeLayer — CRUD, semantic search, and contradictions via
in-memory SQLite. LanceDB is intentionally not available in these tests
(we pass a temp dir that won't trigger a real LanceDB connection), so the
brute-force fallback path is exercised.
"""

import tempfile
import sqlite3
from pathlib import Path

import pytest

from core.database import CoreDatabase
from core.knowledge import KnowledgeLayer
from core.embedding import pack_embedding, unpack_embedding, cosine_similarity


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """In-memory CoreDatabase with core_notes table ready."""
    database = CoreDatabase(":memory:")
    database.initialize()
    yield database
    database.close()


@pytest.fixture
def knowledge(db):
    """KnowledgeLayer backed by in-memory SQLite, no LanceDB."""
    with tempfile.TemporaryDirectory() as tmpdir:
        kl = KnowledgeLayer(db, data_dir=tmpdir)
        # LanceDB should be unavailable → _lance_ready = False
        yield kl


# ── Helpers ───────────────────────────────────────────────────────────


def _make_vec(dim=4):
    """Return a simple unit vector for testing."""
    import math
    vec = [1.0 / math.sqrt(dim)] * dim
    return vec


def _insert_sample(knowledge, module="test", content="sample note", **kwargs):
    """Insert a note with defaults and return its id."""
    return knowledge.insert_note(source_module=module, content=content, **kwargs)


# ── CRUD Tests ────────────────────────────────────────────────────────


class TestInsertNote:
    """insert_note tests."""

    def test_insert_minimal_fields(self, knowledge, db):
        """Insert with only required fields; verify return id and stored content."""
        note_id = knowledge.insert_note(
            source_module="thinker",
            content="This is a test note.",
        )

        assert isinstance(note_id, int)
        assert note_id > 0

        # Verify directly in DB
        row = db.conn.execute(
            "SELECT id, source_module, content, content_type, tags, metadata "
            "FROM core_notes WHERE id = ?",
            (note_id,),
        ).fetchone()

        assert row is not None
        assert row["source_module"] == "thinker"
        assert row["content"] == "This is a test note."
        assert row["content_type"] == "note"
        assert row["tags"] == "[]"
        assert row["metadata"] == "{}"

    def test_insert_returns_unique_ids(self, knowledge):
        """Each insert returns a unique, incrementing id."""
        id1 = knowledge.insert_note(source_module="a", content="first")
        id2 = knowledge.insert_note(source_module="b", content="second")
        id3 = knowledge.insert_note(source_module="c", content="third")

        assert id2 == id1 + 1
        assert id3 == id2 + 1

    def test_insert_with_source_ref(self, knowledge):
        """source_ref is stored correctly."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="referenced note",
            source_ref="arxiv:1234.5678",
        )

        row = knowledge.get_note(note_id)
        assert row["source_ref"] == "arxiv:1234.5678"

    def test_insert_with_tags_and_metadata(self, knowledge):
        """Tags and metadata are serialized and stored correctly."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="tagged note",
            tags=["important", "urgent"],
            metadata={"priority": 1, "author": "Alice"},
        )

        row = knowledge.get_note(note_id)
        assert row["tags"] == ["important", "urgent"]
        assert row["metadata"] == {"priority": 1, "author": "Alice"}


class TestInsertNoteWithEmbedding:
    """insert_note embedding tests."""

    def test_insert_with_embedding_stored_in_sqlite(self, knowledge):
        """Embedding is serialized and stored in core_notes.embedding column."""
        vec = [0.1, 0.2, 0.3, 0.4]
        note_id = knowledge.insert_note(
            source_module="test",
            content="embedded note",
            embedding=vec,
        )

        # Verify via get_note (which does NOT unpack embedding)
        row = knowledge.get_note(note_id)
        assert row is not None, f"Note {note_id} not found"

        # Check raw DB for the embedding column
        raw = knowledge._db.conn.execute(
            "SELECT embedding FROM core_notes WHERE id = ?", (note_id,)
        ).fetchone()
        stored_vec = unpack_embedding(raw["embedding"])
        assert stored_vec == pytest.approx(vec)

    def test_insert_with_empty_embedding(self, knowledge):
        """Empty embedding list is stored as '[]'."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="no embedding",
            embedding=[],
        )

        raw = knowledge._db.conn.execute(
            "SELECT embedding FROM core_notes WHERE id = ?", (note_id,)
        ).fetchone()
        assert raw["embedding"] == "[]"

    def test_insert_with_none_embedding_defaults_to_empty(self, knowledge):
        """None embedding is treated the same as empty list (pack_embedding([]))."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="no embedding",
            embedding=None,
        )

        raw = knowledge._db.conn.execute(
            "SELECT embedding FROM core_notes WHERE id = ?", (note_id,)
        ).fetchone()
        assert raw["embedding"] == "[]"


class TestGetNote:
    """get_note tests."""

    def test_get_note_returns_all_fields(self, knowledge):
        """Retrieved note contains all expected keys and correct values."""
        note_id = knowledge.insert_note(
            source_module="thinker",
            content="retrieval test",
            source_ref="ref-1",
            content_type="summary",
            tags=["tag1"],
            metadata={"k": "v"},
        )

        note = knowledge.get_note(note_id)

        assert note is not None
        assert note["id"] == note_id
        assert note["source_module"] == "thinker"
        assert note["content"] == "retrieval test"
        assert note["source_ref"] == "ref-1"
        assert note["content_type"] == "summary"
        assert note["tags"] == ["tag1"]
        assert note["metadata"] == {"k": "v"}
        assert "created_at" in note

    def test_get_note_nonexistent_returns_none(self, knowledge):
        """Querying a note id that does not exist returns None."""
        result = knowledge.get_note(99999)
        assert result is None

    def test_get_note_zero_id(self, knowledge):
        """Querying id=0 (which should never exist) returns None."""
        result = knowledge.get_note(0)
        assert result is None

    def test_get_note_unpacks_json_fields(self, knowledge):
        """Tags and metadata are returned as Python objects, not strings."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="json test",
        )
        note = knowledge.get_note(note_id)

        assert isinstance(note["tags"], list)
        assert isinstance(note["metadata"], dict)


class TestUpdateNote:
    """update_note tests."""

    def test_update_content(self, knowledge):
        """Updating content changes the stored value."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="original content",
        )

        knowledge.update_note(note_id, content="updated content")
        note = knowledge.get_note(note_id)

        assert note["content"] == "updated content"

    def test_update_tags(self, knowledge):
        """Updating tags changes them from list to new list."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="x",
            tags=["old"],
        )

        knowledge.update_note(note_id, tags=["new", "extra"])
        note = knowledge.get_note(note_id)

        assert note["tags"] == ["new", "extra"]

    def test_update_metadata(self, knowledge):
        """Updating metadata works."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="x",
        )

        knowledge.update_note(note_id, metadata={"status": "done"})
        note = knowledge.get_note(note_id)

        assert note["metadata"] == {"status": "done"}

    def test_update_multiple_fields_simultaneously(self, knowledge):
        """Multiple fields can be updated in a single call."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="old",
            tags=["old-tag"],
        )

        knowledge.update_note(
            note_id,
            content="new",
            content_type="summary",
            source_ref="ref-new",
            tags=["new-tag"],
        )
        note = knowledge.get_note(note_id)

        assert note["content"] == "new"
        assert note["content_type"] == "summary"
        assert note["source_ref"] == "ref-new"
        assert note["tags"] == ["new-tag"]

    def test_update_nonexistent_note_no_error(self, knowledge):
        """Updating a non-existent id does not raise an exception."""
        knowledge.update_note(99999, content="should not exist")
        # No exception → pass

    def test_update_embedding_syncs_to_lance(self, knowledge):
        """Updating embedding triggers _write_chunks_to_lance (no crash)."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="will get embedding",
        )

        new_vec = [0.5, 0.5, 0.5, 0.5]
        # This should not crash even though LanceDB is unavailable.
        knowledge.update_note(note_id, embedding=new_vec)

        raw = knowledge._db.conn.execute(
            "SELECT embedding FROM core_notes WHERE id = ?", (note_id,)
        ).fetchone()
        stored = unpack_embedding(raw["embedding"])
        assert stored == pytest.approx(new_vec)

    def test_update_clearing_embedding(self, knowledge):
        """Clearing embedding (passing empty list) should remove it."""
        note_id = knowledge.insert_note(
            source_module="test",
            content="x",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )

        knowledge.update_note(note_id, embedding=[])

        raw = knowledge._db.conn.execute(
            "SELECT embedding FROM core_notes WHERE id = ?", (note_id,)
        ).fetchone()
        assert raw["embedding"] == "[]"


class TestDeleteByModule:
    """delete_by_module tests."""

    def test_delete_removes_all_notes_for_module(self, knowledge, db):
        """All notes for a module are deleted; notes from other modules remain."""
        # Insert 3 notes: 2 for "mod-a", 1 for "mod-b"
        knowledge.insert_note(source_module="mod-a", content="a1")
        knowledge.insert_note(source_module="mod-a", content="a2")
        knowledge.insert_note(source_module="mod-b", content="b1")

        deleted = knowledge.delete_by_module("mod-a")

        assert deleted == 2

        # Verify mod-a notes are gone
        remaining = db.conn.execute(
            "SELECT COUNT(*) FROM core_notes WHERE source_module = ?", ("mod-a",)
        ).fetchone()
        assert remaining[0] == 0

        # Verify mod-b note still exists
        mod_b_count = db.conn.execute(
            "SELECT COUNT(*) FROM core_notes WHERE source_module = ?", ("mod-b",)
        ).fetchone()
        assert mod_b_count[0] == 1

    def test_delete_empty_module_returns_zero(self, knowledge):
        """Deleting a module with no notes returns 0."""
        deleted = knowledge.delete_by_module("nonexistent")
        assert deleted == 0

    def test_delete_then_reinsert(self, knowledge):
        """After deletion, new notes for the same module get fresh ids."""
        knowledge.insert_note(source_module="mod-x", content="before")
        knowledge.delete_by_module("mod-x")

        new_id = knowledge.insert_note(source_module="mod-x", content="after")
        note = knowledge.get_note(new_id)
        assert note["content"] == "after"


class TestGetNotesByModule:
    """get_notes_by_module tests."""

    def test_retrieves_all_notes_for_module(self, knowledge):
        """Returns notes only for the specified module."""
        knowledge.insert_note(source_module="alpha", content="a1")
        knowledge.insert_note(source_module="alpha", content="a2")
        knowledge.insert_note(source_module="beta", content="b1")

        notes = knowledge.get_notes_by_module("alpha")
        assert len(notes) == 2
        assert all(n["source_module"] == "alpha" for n in notes)

    def test_filter_by_content_type(self, knowledge):
        """content_type filter is applied when provided."""
        knowledge.insert_note(source_module="m", content="c1", content_type="note")
        knowledge.insert_note(source_module="m", content="c2", content_type="summary")
        knowledge.insert_note(source_module="m", content="c3", content_type="note")

        notes = knowledge.get_notes_by_module("m", content_type="note")
        assert len(notes) == 2
        assert all(n["content_type"] == "note" for n in notes)

    def test_respects_limit(self, knowledge):
        """Limit parameter caps the number of returned notes."""
        for i in range(10):
            knowledge.insert_note(source_module="bulk", content=f"note {i}")

        notes = knowledge.get_notes_by_module("bulk", limit=3)
        assert len(notes) == 3

    def test_empty_module_returns_empty_list(self, knowledge):
        """Module with no notes returns []. """
        notes = knowledge.get_notes_by_module("empty")
        assert notes == []


# ── Search Tests ──────────────────────────────────────────────────────


class TestSearchByEmbedding:
    """search_by_embedding tests (brute-force fallback path)."""

    def test_search_returns_similar_notes(self, knowledge):
        """Search with a query vector returns notes sorted by similarity."""
        # Insert notes with known embeddings
        knowledge.insert_note(
            source_module="test",
            content="positive note",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        knowledge.insert_note(
            source_module="test",
            content="neutral note",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )
        knowledge.insert_note(
            source_module="test",
            content="negative note",
            embedding=[-1.0, 0.0, 0.0, 0.0],
        )

        # Query with [1, 0, 0, 0] — should match "positive note" best
        results = knowledge.search_by_embedding(
            [1.0, 0.0, 0.0, 0.0],
            top_k=2,
            min_similarity=0.0,
        )

        assert len(results) >= 1
        # Best match should be "positive note" (cosine_sim = 1.0)
        best = results[0]
        assert best["content"] == "positive note"
        assert best["_similarity"] == pytest.approx(1.0, abs=1e-6)

    def test_search_respects_min_similarity(self, knowledge):
        """Notes below min_similarity are excluded."""
        knowledge.insert_note(
            source_module="test",
            content="target",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        knowledge.insert_note(
            source_module="test",
            content="orthogonal",
            embedding=[0.0, 1.0, 0.0, 0.0],
        )

        # With high min_similarity, orthogonal note should be filtered out
        results = knowledge.search_by_embedding(
            [1.0, 0.0, 0.0, 0.0],
            min_similarity=0.9,
        )

        assert len(results) >= 1
        contents = [r["content"] for r in results]
        assert "target" in contents

    def test_search_respects_top_k(self, knowledge):
        """top_k limits the number of results."""
        for i in range(5):
            knowledge.insert_note(
                source_module="test",
                content=f"note {i}",
                embedding=[float(i + 1) / 10.0, 0.0, 0.0, 0.0],
            )

        results = knowledge.search_by_embedding(
            [1.0, 0.0, 0.0, 0.0],
            top_k=2,
            min_similarity=0.0,
        )
        assert len(results) == 2

    def test_search_skips_notes_without_embedding(self, knowledge):
        """Notes without embedding are excluded from search results."""
        knowledge.insert_note(source_module="test", content="no embedding")
        knowledge.insert_note(
            source_module="test",
            content="has embedding",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )

        results = knowledge.search_by_embedding(
            [1.0, 0.0, 0.0, 0.0],
            min_similarity=0.0,
        )
        contents = [r["content"] for r in results]
        assert "no embedding" not in contents
        assert "has embedding" in contents

    def test_search_filter_by_module(self, knowledge):
        """source_module filter restricts results to that module."""
        knowledge.insert_note(
            source_module="mod-a",
            content="a1",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        knowledge.insert_note(
            source_module="mod-b",
            content="b1",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )

        results = knowledge.search_by_embedding(
            [1.0, 0.0, 0.0, 0.0],
            source_module="mod-a",
            min_similarity=0.0,
        )
        assert len(results) >= 1
        # All results should be from mod-a
        for r in results:
            assert r["source_module"] == "mod-a"

    def test_search_cosine_similarity_values(self, knowledge):
        """Verify that _similarity field contains valid cosine similarity."""
        # Orthogonal vectors: sim = 0.0
        knowledge.insert_note(
            source_module="test",
            content="orthogonal",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )

        results = knowledge.search_by_embedding(
            [0.0, 1.0, 0.0, 0.0],
            min_similarity=-0.1,
        )
        assert len(results) == 1
        # cosine_sim([1,0,0,0], [0,1,0,0]) = 0.0
        assert results[0]["_similarity"] == pytest.approx(0.0, abs=1e-6)


class TestFindContradictions:
    """find_contradictions tests."""

    def test_find_contradictions_returns_opposite_direction(self, knowledge):
        """find_contradictions returns notes sorted by absolute similarity."""
        # Same direction
        knowledge.insert_note(
            source_module="test",
            content="aligned",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )
        # Opposite direction
        knowledge.insert_note(
            source_module="test",
            content="opposite",
            embedding=[-1.0, 0.0, 0.0, 0.0],
        )

        # Query with [1, 0, 0, 0] in contradictions mode
        results = knowledge.find_contradictions(
            [1.0, 0.0, 0.0, 0.0],
            top_k=5,
        )

        assert len(results) >= 1

    def test_find_contradictions_skips_notes_without_embedding(self, knowledge):
        """Notes without embeddings are excluded."""
        knowledge.insert_note(source_module="test", content="no emb")
        knowledge.insert_note(
            source_module="test",
            content="with emb",
            embedding=[1.0, 0.0, 0.0, 0.0],
        )

        results = knowledge.find_contradictions([1.0, 0.0, 0.0, 0.0])
        for r in results:
            assert "_similarity" in r


# ── Module listing ────────────────────────────────────────────────────


class TestModuleListing:
    """Verify distinct modules can be queried from the notes table."""

    def test_distinct_modules_with_notes(self, knowledge, db):
        """After inserting notes from different modules, query returns distinct modules."""
        knowledge.insert_note(source_module="thinker", content="t1")
        knowledge.insert_note(source_module="thinker", content="t2")
        knowledge.insert_note(source_module="ideator", content="i1")
        knowledge.insert_note(source_module="agent1", content="a1")

        rows = db.conn.execute(
            "SELECT DISTINCT source_module FROM core_notes ORDER BY source_module"
        ).fetchall()
        modules = [r[0] for r in rows]

        assert "agent1" in modules
        assert "ideator" in modules
        assert "thinker" in modules
        assert len(modules) == 3  # exactly 3 distinct modules


# ── Edge Cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    """Miscellaneous edge-case tests."""

    def test_insert_empty_content(self, knowledge):
        """Insert with empty content string succeeds."""
        note_id = knowledge.insert_note(source_module="test", content="")
        note = knowledge.get_note(note_id)
        assert note["content"] == ""

    def test_insert_very_long_content(self, knowledge):
        """Insert with content larger than _CHUNK_SIZE works (LanceDB write skipped)."""
        long_content = "x" * 10000
        note_id = knowledge.insert_note(source_module="test", content=long_content)
        note = knowledge.get_note(note_id)
        assert len(note["content"]) == 10000

    def test_insert_special_characters(self, knowledge):
        """Unicode and special characters are preserved."""
        content = "你好世界 🌍 émoji test — em-dash 'single' \"double\" <tag>"
        note_id = knowledge.insert_note(source_module="test", content=content)
        note = knowledge.get_note(note_id)
        assert note["content"] == content

    def test_delete_by_module_handles_special_module_names(self, knowledge):
        """Module names with special characters work for deletion."""
        mod_name = "module-with-hyphens_and_underscores.123"
        knowledge.insert_note(source_module=mod_name, content="x")
        deleted = knowledge.delete_by_module(mod_name)
        assert deleted == 1

    def test_multiple_operations_in_sequence(self, knowledge):
        """Insert, update, retrieve, delete sequence works correctly."""
        nid = knowledge.insert_note(source_module="seq", content="start")
        assert knowledge.get_note(nid) is not None

        knowledge.update_note(nid, content="middle")
        assert knowledge.get_note(nid)["content"] == "middle"

        # Delete a different module — this note should be untouched
        knowledge.delete_by_module("other")
        assert knowledge.get_note(nid) is not None

        # Delete the actual module
        knowledge.delete_by_module("seq")
        assert knowledge.get_note(nid) is None
