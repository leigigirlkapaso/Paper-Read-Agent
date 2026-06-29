"""tests for DataAccess outline methods (Task 2)."""
import sqlite3
from unittest.mock import MagicMock

from paperreadagent.modules.ideator.data_access import DataAccess


def _mk_dataaccess_in_memory_db():
    """Build a DataAccess with an in-memory sqlite that has the outlines table."""
    da = DataAccess.__new__(DataAccess)  # bypass __init__ (skips LanceDB)
    da._spark_lance_ready = False
    da._spark_lance_table = None

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ideator_roundtable_outlines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rt_id INTEGER NOT NULL,
            round_number INTEGER NOT NULL DEFAULT 1,
            outline_markdown TEXT NOT NULL DEFAULT '',
            facts_block TEXT NOT NULL DEFAULT '',
            model_name TEXT NOT NULL DEFAULT '',
            token_usage TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)

    da._core = MagicMock()
    da._core.db.conn = conn
    da._core.db.dict_row.side_effect = lambda r: dict(r) if r else None
    da._core.db.dict_rows.side_effect = lambda rs: [dict(r) for r in rs]
    da._legacy = MagicMock()
    return da


def test_insert_outline_creates_row():
    da = _mk_dataaccess_in_memory_db()
    new_id = da.insert_outline(
        rt_id=42, round_number=1, outline_markdown="# Hello",
        facts_block="...", model_name="deepseek",
    )
    assert isinstance(new_id, int) and new_id >= 1
    row = da._core.db.conn.execute(
        "SELECT * FROM ideator_roundtable_outlines WHERE id=?", (new_id,)
    ).fetchone()
    assert row["rt_id"] == 42
    assert row["round_number"] == 1
    assert row["outline_markdown"] == "# Hello"
    assert row["facts_block"] == "..."
    assert row["model_name"] == "deepseek"


def test_get_latest_outline_returns_most_recent_by_round():
    da = _mk_dataaccess_in_memory_db()
    da.insert_outline(rt_id=7, round_number=1, outline_markdown="r1")
    da.insert_outline(rt_id=7, round_number=2, outline_markdown="r2")
    da.insert_outline(rt_id=7, round_number=3, outline_markdown="r3")
    # Also a different rt_id should not interfere
    da.insert_outline(rt_id=99, round_number=5, outline_markdown="other")

    latest = da.get_latest_outline(7)
    assert latest == "r3"


def test_get_latest_outline_returns_none_for_unknown_rt():
    da = _mk_dataaccess_in_memory_db()
    assert da.get_latest_outline(404) is None


def test_get_outline_history_returns_rows_ordered_by_round_asc():
    da = _mk_dataaccess_in_memory_db()
    da.insert_outline(rt_id=11, round_number=3, outline_markdown="c")
    da.insert_outline(rt_id=11, round_number=1, outline_markdown="a")
    da.insert_outline(rt_id=11, round_number=2, outline_markdown="b")

    history = da.get_outline_history(11)
    assert [r["round_number"] for r in history] == [1, 2, 3]
    assert [r["outline_markdown"] for r in history] == ["a", "b", "c"]


def test_get_outline_history_returns_empty_for_unknown_rt():
    da = _mk_dataaccess_in_memory_db()
    assert da.get_outline_history(404) == []
