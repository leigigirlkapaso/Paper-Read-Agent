"""
modules/thinker/tests/test_schema.py
验证 Thinker 四张表的创建和结构。
"""

from core import create_core
from modules.thinker import register


def _create_registered_core():
    core = create_core(config_path="config.yaml", db_path=":memory:")
    register(core)
    return core


def test_all_tables_created():
    core = _create_registered_core()

    tables = core.db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'thinker_%' ORDER BY name"
    ).fetchall()
    table_names = [r[0] for r in tables]

    assert "thinker_conversations" in table_names
    assert "thinker_messages" in table_names
    assert "thinker_resolutions" in table_names
    assert "thinker_pending_questions" in table_names
    assert "thinker_memory_index" in table_names
    assert "thinker_user_profile" in table_names


def test_conversations_columns():
    core = _create_registered_core()

    cols = core.db.conn.execute("PRAGMA table_info('thinker_conversations')").fetchall()
    col_names = [r[1] for r in cols]

    for c in ("id", "title", "mode", "status", "snooze_until", "intensity", "created_at", "updated_at"):
        assert c in col_names, f"Missing column: {c}"


def test_messages_columns():
    core = _create_registered_core()

    cols = core.db.conn.execute("PRAGMA table_info('thinker_messages')").fetchall()
    col_names = [r[1] for r in cols]

    for c in ("id", "conversation_id", "role", "content", "embedding", "token_count", "opener", "created_at"):
        assert c in col_names, f"Missing column: {c}"


def test_resolutions_columns():
    core = _create_registered_core()

    cols = core.db.conn.execute("PRAGMA table_info('thinker_resolutions')").fetchall()
    col_names = [r[1] for r in cols]

    for c in ("id", "conversation_id", "content", "status", "deadline", "asked_at", "asked_count", "reflection"):
        assert c in col_names, f"Missing column: {c}"


def test_pending_questions_columns():
    core = _create_registered_core()

    cols = core.db.conn.execute("PRAGMA table_info('thinker_pending_questions')").fetchall()
    col_names = [r[1] for r in cols]

    for c in ("id", "conversation_id", "question", "question_type", "source_refs", "delivered", "dismissed"):
        assert c in col_names, f"Missing column: {c}"


def test_migration_version_recorded():
    core = _create_registered_core()

    row = core.db.conn.execute(
        "SELECT MAX(version) FROM thinker_schema_version"
    ).fetchone()
    assert row[0] == 2
