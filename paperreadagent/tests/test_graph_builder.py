"""tests for GraphBuilderService (panorama + neighborhood modes)."""
import json
import sqlite3
import pytest

from paperreadagent.web.services.graph_builder import (
    GraphBuilderService, GraphOptions,
)


# ── Test DB bootstrap ──────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    topic TEXT
);
CREATE TABLE IF NOT EXISTS papers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    arxiv_id TEXT,
    title TEXT,
    extraction_json TEXT
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id INTEGER,
    content TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS ideator_sparks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'seed',
    source_type TEXT DEFAULT '',
    source_refs TEXT NOT NULL DEFAULT '[]',
    quality_score REAL NOT NULL DEFAULT 0.5
);
CREATE TABLE IF NOT EXISTS ideator_cross_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_a_type TEXT, source_a_id INTEGER,
    source_b_type TEXT, source_b_id INTEGER,
    link_type TEXT, relevance_score REAL DEFAULT 0.0,
    reasoning TEXT DEFAULT '',
    spark_id INTEGER
);
"""


class _FakeDB:
    """Minimal DB stub matching paperreadagent.db.database.Database interface
    required by GraphBuilderService (just exposes .conn)."""
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)


def _seed_two_projects_with_shared_paper(db):
    """Project A (id=1) and Project B (id=2) each have a session, and both
    sessions contain a paper with the same arxiv_id (cross-shared)."""
    db.conn.executescript("""
        INSERT INTO projects (id, name) VALUES
            (1, 'Project A'), (2, 'Project B');
        INSERT INTO sessions (id, project_id, topic) VALUES
            (1, 1, 'topic A'), (2, 2, 'topic B');
        -- Same arxiv_id 2401.0001 in both projects → shared edge
        INSERT INTO papers (id, session_id, arxiv_id, title) VALUES
            (10, 1, '2401.0001', 'Shared Paper'),
            (11, 2, '2401.0001', 'Shared Paper'),
            (12, 1, '2401.0002', 'Only in A'),
            (13, 2, '2401.0003', 'Only in B');
    """)
    db.conn.commit()


def test_build_default_returns_projects_and_papers():
    db = _FakeDB()
    _seed_two_projects_with_shared_paper(db)
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper'}, limit=200,
    ))
    types = {n["type"] for n in result.nodes}
    assert types == {"project", "paper"}
    # 2 projects + 4 papers (we expect distinct paper rows; cross-project shared
    # papers stay separate rows since they have separate paper.id)
    project_count = sum(1 for n in result.nodes if n["type"] == "project")
    paper_count = sum(1 for n in result.nodes if n["type"] == "paper")
    assert project_count == 2
    assert paper_count == 4


def test_build_with_only_project_layer_no_edges():
    """layers={'project'} alone: contains/shared edges need papers → no edges."""
    db = _FakeDB()
    _seed_two_projects_with_shared_paper(db)
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project'}, limit=200,
    ))
    project_count = sum(1 for n in result.nodes if n["type"] == "project")
    assert project_count == 2
    assert result.edges == []


def test_build_papers_show_cross_project_shared_flag():
    """Papers sharing arxiv_id across projects get shared='true' on both rows."""
    db = _FakeDB()
    _seed_two_projects_with_shared_paper(db)
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper'}, limit=200,
    ))
    paper_nodes = [n for n in result.nodes if n["type"] == "paper"]
    shared_papers = [n for n in paper_nodes if n.get("shared") == "true"]
    # Both rows with arxiv 2401.0001 should be flagged shared
    assert len(shared_papers) == 2
    for p in shared_papers:
        assert p["arxiv_id"] == "2401.0001"
        # project_count counts distinct project_ids the arxiv appears in
        assert p["project_count"] == 2


def test_build_includes_shared_edges_between_papers():
    """Two paper rows with same arxiv_id should produce a 'shared' edge."""
    db = _FakeDB()
    _seed_two_projects_with_shared_paper(db)
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper'}, limit=200,
    ))
    shared_edges = [e for e in result.edges if e["etype"] == "shared"]
    assert len(shared_edges) == 1
    edge = shared_edges[0]
    # Endpoints should be the two paper nodes with arxiv 2401.0001
    endpoints = {edge["source"], edge["target"]}
    assert endpoints == {"paper_10", "paper_11"}

    # Also contains edges: 2 projects × papers each
    contains_edges = [e for e in result.edges if e["etype"] == "contains"]
    # Project A → papers 10, 12 (2 edges); Project B → papers 11, 13 (2 edges)
    assert len(contains_edges) == 4


def _seed_note_on_paper(db):
    """Add a note on paper 10 (assumes _seed_two_projects_with_shared_paper ran)."""
    db.conn.executescript("""
        INSERT INTO notes (id, paper_id, content, updated_at) VALUES
            (100, 10, 'My note on paper 10', '2026-01-01');
    """)
    db.conn.commit()


def _seed_spark_citing_paper(db):
    """Spark 55 (deep_done) cites paper id=10 via source_refs JSON."""
    refs = json.dumps([{"type": "paper", "id": 10}])
    db.conn.execute(
        """INSERT INTO ideator_sparks (id, content, status, quality_score, source_refs)
           VALUES (55, 'spark content', 'deep_done', 0.8, ?)""",
        (refs,),
    )
    db.conn.commit()


def test_build_with_notes_layer_adds_has_note_edges():
    db = _FakeDB()
    _seed_two_projects_with_shared_paper(db)
    _seed_note_on_paper(db)
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper', 'note'}, limit=200,
    ))
    # Note node present
    note_nodes = [n for n in result.nodes if n["type"] == "note"]
    assert len(note_nodes) == 1
    assert note_nodes[0]["id"] == "note_100"
    assert note_nodes[0]["paper_id"] == 10
    # has_note edge: paper 10 → note 100
    has_note_edges = [e for e in result.edges if e["etype"] == "has_note"]
    assert len(has_note_edges) == 1
    assert has_note_edges[0]["source"] == "paper_10"
    assert has_note_edges[0]["target"] == "note_100"


def test_build_with_sparks_layer_adds_cites_edges():
    db = _FakeDB()
    _seed_two_projects_with_shared_paper(db)
    _seed_spark_citing_paper(db)
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper', 'spark'}, limit=200,
    ))
    # Spark node present
    spark_nodes = [n for n in result.nodes if n["type"] == "spark"]
    assert len(spark_nodes) == 1
    assert spark_nodes[0]["id"] == "spark_55"
    assert spark_nodes[0]["size"] == 80  # quality_score 0.8 * 100
    # cites edge: spark_55 → paper_10
    cites_edges = [e for e in result.edges if e["etype"] == "cites"]
    assert len(cites_edges) == 1
    assert cites_edges[0]["source"] == "spark_55"
    assert cites_edges[0]["target"] == "paper_10"


def test_build_truncates_at_limit():
    """When papers exceed limit, only top-N are returned with truncated=True."""
    db = _FakeDB()
    db.conn.executescript("""
        INSERT INTO projects (id, name) VALUES (1, 'Big Project');
        INSERT INTO sessions (id, project_id) VALUES (1, 1);
    """)
    # 60 papers, all in same session
    for i in range(60):
        db.conn.execute(
            "INSERT INTO papers (id, session_id, arxiv_id, title) VALUES (?, 1, ?, ?)",
            (1000 + i, f"2401.{1000+i:04d}", f"Paper {i}"),
        )
    db.conn.commit()
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper'}, limit=50,
    ))
    paper_count = sum(1 for n in result.nodes if n["type"] == "paper")
    assert paper_count == 50
    assert result.truncated is True


def test_build_neighborhood_for_paper_center():
    """center='paper_10' → returns paper 10 + its project + its note + spark citing it."""
    db = _FakeDB()
    _seed_two_projects_with_shared_paper(db)
    _seed_note_on_paper(db)
    _seed_spark_citing_paper(db)
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper', 'note', 'spark'},
        center='paper_10',
    ))
    node_ids = {n["id"] for n in result.nodes}
    # paper 10 itself
    assert "paper_10" in node_ids
    # its containing project (project 1)
    assert "project_1" in node_ids
    # its note
    assert "note_100" in node_ids
    # spark citing it
    assert "spark_55" in node_ids
    # the other paper sharing arxiv 2401.0001 (paper 11)
    assert "paper_11" in node_ids


def test_build_does_not_set_truncated_when_papers_exactly_equal_limit():
    """When total papers == limit, no false-positive truncation flag."""
    db = _FakeDB()
    db.conn.executescript("""
        INSERT INTO projects (id, name) VALUES (1, 'P');
        INSERT INTO sessions (id, project_id) VALUES (1, 1);
    """)
    # Exactly 50 papers, limit = 50 → should NOT be truncated
    for i in range(50):
        db.conn.execute(
            "INSERT INTO papers (id, session_id, arxiv_id, title) VALUES (?, 1, ?, ?)",
            (2000 + i, f"2402.{2000+i:04d}", f"Paper {i}"),
        )
    db.conn.commit()
    builder = GraphBuilderService(db)
    result = builder.build(GraphOptions(
        layers={'project', 'paper'}, limit=50,
    ))
    paper_count = sum(1 for n in result.nodes if n["type"] == "paper")
    assert paper_count == 50
    assert result.truncated is False  # exact-fit, no truncation