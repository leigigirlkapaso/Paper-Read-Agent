"""
core/tests/test_database.py
测试 CoreDatabase：核心表创建、迁移、模块迁移协调、LLM 用量记录。
"""

import sqlite3

from core.database import CoreDatabase


def test_database_creates_core_tables():
    db = CoreDatabase(":memory:")
    db.initialize()

    # 检查核心表是否存在
    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [r[0] for r in tables]

    assert "core_schema_version" in table_names
    assert "core_notes" in table_names
    assert "core_llm_usage" in table_names

    db.close()


def test_database_migration_records_version():
    db = CoreDatabase(":memory:")
    db.initialize()

    row = db.conn.execute(
        "SELECT MAX(version) FROM core_schema_version"
    ).fetchone()
    assert row[0] == 3

    db.close()


def test_database_migration_is_idempotent():
    db = CoreDatabase(":memory:")
    db.initialize()
    db.initialize()  # 第二次不应出错

    row = db.conn.execute(
        "SELECT MAX(version) FROM core_schema_version"
    ).fetchone()
    assert row[0] == 3

    db.close()


def test_record_llm_usage():
    db = CoreDatabase(":memory:")
    db.initialize()

    db.record_llm_usage(
        source_module="test",
        purpose="chat",
        model_name="test-model",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
    )

    row = db.conn.execute(
        "SELECT * FROM core_llm_usage ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["source_module"] == "test"
    assert row["purpose"] == "chat"
    assert row["prompt_tokens"] == 100
    assert row["total_tokens"] == 150

    db.close()


def test_run_module_migration():
    db = CoreDatabase(":memory:")
    db.initialize()

    module_migrations = {
        1: "CREATE TABLE IF NOT EXISTS testmod_data (id INTEGER PRIMARY KEY, value TEXT);",
    }
    db.run_module_migration("testmod", 1, module_migrations)

    tables = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [r[0] for r in tables]

    assert "testmod_data" in table_names
    assert "testmod_schema_version" in table_names

    db.close()


def test_database_reuses_existing_connection():
    existing = sqlite3.connect(":memory:")
    existing.row_factory = sqlite3.Row
    db = CoreDatabase(":memory:", existing_conn=existing)
    db.initialize()

    # 确认用的是同一连接
    row = db.conn.execute("SELECT 1 AS val").fetchone()
    assert row["val"] == 1

    db.close()
    existing.close()
