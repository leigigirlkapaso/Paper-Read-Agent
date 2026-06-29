"""tests for DataAccess.gather_facts_for_spark"""
import json
from unittest.mock import MagicMock
from paperreadagent.modules.ideator.data_access import DataAccess


# ──────────────────────────────────────────────────────────────
# gather_facts_for_spark — facts-layer collection for roundtable
# ──────────────────────────────────────────────────────────────

def _mk_extraction(problem="P", methods=("M1",), datasets=("D1",),
                   metrics=(), baselines=(), limitations=(), contributions=()):
    """Minimal valid 7-field extraction dict (matches Section 4.1 of upstream spec)."""
    return {
        "problem": problem,
        "methods": list(methods),
        "datasets": list(datasets),
        "metrics": list(metrics),
        "baselines": list(baselines),
        "limitations": list(limitations),
        "contributions": list(contributions),
    }


def _mk_dataaccess_with_mocks(*, spark_refs=None, cross_links_rows=(), papers=None,
                              notes=None):
    """Build a DataAccess where _core / _legacy / _spark_lance are mocked.

    spark_refs: list of {"type": "paper"/"core_note", "id": N} dicts (serialized to JSON in spark)
    cross_links_rows: tuples (source_a_type, source_a_id, source_b_type, source_b_id, relevance_score)
    papers: dict paper_id -> {"id", "title", "arxiv_id", "extraction_json"}; extraction_json is a STR or None
    notes:  dict note_id  -> {"metadata": {"paper_id": int_or_None}, ...} (core_notes shape)
    """
    papers = papers or {}
    notes = notes or {}

    da = DataAccess.__new__(DataAccess)  # bypass __init__ (skips LanceDB)
    da._spark_lance_ready = False
    da._spark_lance_table = None

    # _core.db.conn.execute(sql, params).fetchone() / fetchall()
    spark_row = {"id": 1, "source_refs": json.dumps(spark_refs or [])}

    def core_execute(sql, params=()):
        result = MagicMock()
        if "ideator_sparks" in sql:
            result.fetchone.return_value = spark_row
        elif "ideator_cross_links" in sql:
            rows = [
                {"source_a_type": a_t, "source_a_id": a_i,
                 "source_b_type": b_t, "source_b_id": b_i,
                 "relevance_score": r}
                for (a_t, a_i, b_t, b_i, r) in cross_links_rows
            ]
            result.fetchall.return_value = rows
        else:
            result.fetchone.return_value = None
            result.fetchall.return_value = []
        return result

    da._core = MagicMock()
    da._core.db.conn.execute.side_effect = core_execute
    da._core.db.dict_row.side_effect = lambda r: dict(r) if r else None
    da._core.db.dict_rows.side_effect = lambda rs: [dict(r) for r in rs]
    da._core.knowledge.get_note.side_effect = lambda nid: notes.get(nid)

    da._legacy = MagicMock()
    da._legacy.get_paper.side_effect = lambda pid: papers.get(pid)
    return da


def test_gather_facts_basic_three_papers_with_extraction():
    """3 papers via source_refs, all with extraction -> 3 facts back, sorted by score."""
    papers = {
        10: {"id": 10, "title": "Paper A", "arxiv_id": "2401.1",
             "extraction_json": json.dumps(_mk_extraction(problem="alpha"))},
        20: {"id": 20, "title": "Paper B", "arxiv_id": "2401.2",
             "extraction_json": json.dumps(_mk_extraction(problem="beta"))},
        30: {"id": 30, "title": "Paper C", "arxiv_id": "2401.3",
             "extraction_json": json.dumps(_mk_extraction(problem="gamma"))},
    }
    da = _mk_dataaccess_with_mocks(
        spark_refs=[{"type": "paper", "id": 10}, {"type": "paper", "id": 20},
                    {"type": "paper", "id": 30}],
        papers=papers,
    )
    facts = da.gather_facts_for_spark(spark_id=1, max_papers=8)
    assert len(facts) == 3
    titles = {f["title"] for f in facts}
    assert titles == {"Paper A", "Paper B", "Paper C"}
    # each item is a dict with the expected keys
    for f in facts:
        assert set(f.keys()) >= {"paper_id", "title", "arxiv_id",
                                 "relevance_score", "extraction"}
        assert isinstance(f["extraction"], dict)
        assert "problem" in f["extraction"]


def test_gather_facts_drops_papers_without_extraction():
    """5 papers linked, 2 have NULL extraction_json -> only 3 returned."""
    papers = {
        10: {"id": 10, "title": "A", "arxiv_id": None,
             "extraction_json": json.dumps(_mk_extraction())},
        20: {"id": 20, "title": "B", "arxiv_id": None, "extraction_json": None},
        30: {"id": 30, "title": "C", "arxiv_id": None,
             "extraction_json": json.dumps(_mk_extraction())},
        40: {"id": 40, "title": "D", "arxiv_id": None, "extraction_json": None},
        50: {"id": 50, "title": "E", "arxiv_id": None,
             "extraction_json": json.dumps(_mk_extraction())},
    }
    da = _mk_dataaccess_with_mocks(
        spark_refs=[{"type": "paper", "id": pid} for pid in (10, 20, 30, 40, 50)],
        papers=papers,
    )
    facts = da.gather_facts_for_spark(spark_id=1)
    assert len(facts) == 3
    assert {f["paper_id"] for f in facts} == {10, 30, 50}


def test_gather_facts_includes_note_to_paper():
    """source_refs has only a core_note; note.metadata.paper_id=99; paper 99 has extraction -> included."""
    papers = {
        99: {"id": 99, "title": "From Note", "arxiv_id": None,
             "extraction_json": json.dumps(_mk_extraction(problem="note-paper"))},
    }
    notes = {7: {"metadata": {"paper_id": 99}, "content": "anything"}}
    da = _mk_dataaccess_with_mocks(
        spark_refs=[{"type": "core_note", "id": 7}],
        papers=papers, notes=notes,
    )
    facts = da.gather_facts_for_spark(spark_id=1)
    assert len(facts) == 1
    assert facts[0]["paper_id"] == 99
    assert facts[0]["extraction"]["problem"] == "note-paper"


def test_gather_facts_includes_cross_links():
    """source_refs empty, but cross_links has paper rows -> those papers included."""
    papers = {
        100: {"id": 100, "title": "X", "arxiv_id": None,
              "extraction_json": json.dumps(_mk_extraction())},
        200: {"id": 200, "title": "Y", "arxiv_id": None,
              "extraction_json": json.dumps(_mk_extraction())},
    }
    da = _mk_dataaccess_with_mocks(
        spark_refs=[],
        cross_links_rows=[
            ("paper", 100, "spark", 1, 0.9),
            ("spark", 1, "paper", 200, 0.7),
        ],
        papers=papers,
    )
    facts = da.gather_facts_for_spark(spark_id=1)
    assert {f["paper_id"] for f in facts} == {100, 200}
    # sorted by relevance DESC: 100 (0.9) before 200 (0.7)
    assert facts[0]["paper_id"] == 100
    assert facts[1]["paper_id"] == 200


def test_gather_facts_caps_at_max_papers():
    """20 papers all with extraction -> capped at default max_papers=8."""
    papers = {pid: {"id": pid, "title": f"P{pid}", "arxiv_id": None,
                    "extraction_json": json.dumps(_mk_extraction())}
              for pid in range(1, 21)}
    refs = [{"type": "paper", "id": pid} for pid in range(1, 21)]
    da = _mk_dataaccess_with_mocks(spark_refs=refs, papers=papers)
    facts = da.gather_facts_for_spark(spark_id=1)
    assert len(facts) == 8


def test_gather_facts_empty_when_no_linked_papers():
    """Spark exists but has no source_refs and no cross_links -> []."""
    da = _mk_dataaccess_with_mocks(spark_refs=[], cross_links_rows=())
    facts = da.gather_facts_for_spark(spark_id=1)
    assert facts == []


def test_gather_facts_handles_corrupt_extraction_json():
    """One paper has malformed extraction_json -> that paper dropped, others returned."""
    papers = {
        10: {"id": 10, "title": "Good", "arxiv_id": None,
             "extraction_json": json.dumps(_mk_extraction())},
        20: {"id": 20, "title": "Broken", "arxiv_id": None,
             "extraction_json": "{not valid json"},
        30: {"id": 30, "title": "AlsoGood", "arxiv_id": None,
             "extraction_json": json.dumps(_mk_extraction())},
    }
    da = _mk_dataaccess_with_mocks(
        spark_refs=[{"type": "paper", "id": pid} for pid in (10, 20, 30)],
        papers=papers,
    )
    facts = da.gather_facts_for_spark(spark_id=1)
    assert {f["paper_id"] for f in facts} == {10, 30}
