"""tests for /graph page + /api/graph/data JSON endpoint."""
import sqlite3
from unittest.mock import MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _make_app_with_seeded_db():
    """Build a FastAPI app with the papers router mounted, real templates,
    and an in-memory SQLite DB with minimal seed data."""
    from paperreadagent.web.routes.papers import router

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE projects (id INTEGER PRIMARY KEY, name TEXT,
            description TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, project_id INTEGER, topic TEXT);
        CREATE TABLE papers (id INTEGER PRIMARY KEY, session_id INTEGER,
            arxiv_id TEXT, title TEXT, extraction_json TEXT);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, paper_id INTEGER,
            content TEXT, updated_at TEXT);
        CREATE TABLE ideator_sparks (id INTEGER PRIMARY KEY, content TEXT,
            status TEXT, source_refs TEXT, quality_score REAL);
        CREATE TABLE ideator_cross_links (id INTEGER PRIMARY KEY,
            source_a_type TEXT, source_a_id INTEGER,
            source_b_type TEXT, source_b_id INTEGER,
            link_type TEXT, relevance_score REAL, reasoning TEXT, spark_id INTEGER);
        INSERT INTO projects (id, name) VALUES (1, 'Project A'), (2, 'Project B');
        INSERT INTO sessions (id, project_id) VALUES (1, 1), (2, 2);
        INSERT INTO papers (id, session_id, arxiv_id, title) VALUES
            (10, 1, '2401.0001', 'Shared'),
            (11, 2, '2401.0001', 'Shared'),
            (12, 1, '2401.0002', 'Only A');
    """)
    conn.commit()

    # Fake DB object compatible with GraphBuilderService and routes
    db = MagicMock()
    db.conn = conn

    app = FastAPI()
    app.state.db = db
    app.include_router(router)
    return app


def test_get_graph_page_renders_html():
    """GET /papers/graph returns 200 HTML containing cytoscape script."""
    app = _make_app_with_seeded_db()
    with TestClient(app) as client:
        resp = client.get("/papers/graph")
    assert resp.status_code == 200
    body = resp.text
    assert "cytoscape" in body.lower()
    # The page should NOT embed graph data (data comes from API now)
    assert "graphData" not in body or "fetch" in body  # fetch indicates API call


def test_get_graph_data_returns_json_with_default_layers():
    """GET /papers/api/graph/data with no params returns JSON with default layers."""
    app = _make_app_with_seeded_db()
    with TestClient(app) as client:
        resp = client.get("/papers/api/graph/data")
    assert resp.status_code == 200
    data = resp.json()
    assert "nodes" in data
    assert "edges" in data
    assert "truncated" in data
    # Default layers project+paper → expect 2 projects + 3 papers
    types = [n["type"] for n in data["nodes"]]
    assert types.count("project") == 2
    assert types.count("paper") == 3


def test_get_graph_data_handles_invalid_layer_gracefully():
    """Invalid layer string falls back to default project+paper, never 500."""
    app = _make_app_with_seeded_db()
    with TestClient(app) as client:
        resp = client.get("/papers/api/graph/data?layers=garbage,foo")
    assert resp.status_code == 200
    data = resp.json()
    types = {n["type"] for n in data["nodes"]}
    assert types == {"project", "paper"}
