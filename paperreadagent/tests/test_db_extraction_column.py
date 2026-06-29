"""Verify migration v5 adds extraction_json + get_session_extractions reads it."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import Database


def _new_db(tmp_path: Path) -> Database:
    return Database(str(tmp_path / "test.db"))


def _insert_3_papers(db, sid):
    """Insert 3 papers using the project's insert_papers helper.
    Keys match the columns insert_papers reads via p.get(...) in database.py."""
    db.insert_papers(sid, [
        {"arxiv_id": "a1", "title": "T1", "authors": [], "published": "2024",
         "abstract": "x", "source_url": "", "doi": "",
         "relevance_score": 0.9, "source_platform": ""},
        {"arxiv_id": "a2", "title": "T2", "authors": [], "published": "2024",
         "abstract": "x", "source_url": "", "doi": "",
         "relevance_score": 0.8, "source_platform": ""},
        {"arxiv_id": "a3", "title": "T3", "authors": [], "published": "2024",
         "abstract": "x", "source_url": "", "doi": "",
         "relevance_score": 0.7, "source_platform": ""},
    ])


def test_extraction_column_exists(tmp_path):
    db = _new_db(tmp_path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(papers)").fetchall()}
    assert "extraction_json" in cols


def test_get_session_extractions_filters_null(tmp_path):
    db = _new_db(tmp_path)
    project_id = db.create_project("p", "desc")
    sid = db.create_session(project_id, "full", {}, str(tmp_path / "s1"))
    _insert_3_papers(db, sid)
    db.update_paper_by_arxiv_id(sid, "a1", extraction_json=json.dumps({"problem": "p1"}))
    db.update_paper_by_arxiv_id(sid, "a3", extraction_json=json.dumps({"problem": "p3"}))

    out = db.get_session_extractions(sid)
    assert len(out) == 2
    arxiv_ids = {r["arxiv_id"] for r in out}
    assert arxiv_ids == {"a1", "a3"}
    assert out[0]["extraction"]["problem"] in ("p1", "p3")
    # ordered by relevance desc
    assert out[0]["relevance_score"] >= out[1]["relevance_score"]


def test_get_session_extractions_empty(tmp_path):
    db = _new_db(tmp_path)
    project_id = db.create_project("p", "desc")
    sid = db.create_session(project_id, "full", {}, str(tmp_path / "s1"))
    assert db.get_session_extractions(sid) == []


def test_get_session_extractions_skips_null_literal(tmp_path):
    """extraction_json='null' (a JSON literal that parses to Python None) must
    not produce a row — would crash UI dereferences like extraction['problem']."""
    db = _new_db(tmp_path)
    project_id = db.create_project("p", "desc")
    sid = db.create_session(project_id, "full", {}, str(tmp_path / "s1"))
    _insert_3_papers(db, sid)
    db.update_paper_by_arxiv_id(sid, "a1", extraction_json="null")  # the literal string "null"
    out = db.get_session_extractions(sid)
    assert out == []


def test_get_session_extractions_skips_empty_string(tmp_path):
    """extraction_json='' is not NULL so it passes the IS NOT NULL filter.
    Must be skipped at the JSON-parse step."""
    db = _new_db(tmp_path)
    project_id = db.create_project("p", "desc")
    sid = db.create_session(project_id, "full", {}, str(tmp_path / "s1"))
    _insert_3_papers(db, sid)
    db.update_paper_by_arxiv_id(sid, "a1", extraction_json="")
    out = db.get_session_extractions(sid)
    assert out == []


def test_get_session_extractions_skips_malformed(tmp_path):
    """Malformed JSON must be skipped silently (logged at debug)."""
    db = _new_db(tmp_path)
    project_id = db.create_project("p", "desc")
    sid = db.create_session(project_id, "full", {}, str(tmp_path / "s1"))
    _insert_3_papers(db, sid)
    db.update_paper_by_arxiv_id(sid, "a1", extraction_json="{not valid json,,,}")
    out = db.get_session_extractions(sid)
    assert out == []
