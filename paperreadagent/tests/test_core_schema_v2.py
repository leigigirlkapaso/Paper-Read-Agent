def test_core_latest_version_is_3():
    from paperreadagent.core.schema import CORE_LATEST_VERSION
    assert CORE_LATEST_VERSION == 3

def test_v2_creates_core_users_table():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from paperreadagent.core.schema import CORE_MIGRATIONS
    conn.executescript(CORE_MIGRATIONS[1])
    conn.executescript(CORE_MIGRATIONS[2])
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    assert "core_users" in tables
    assert "core_login_attempts" in tables
    conn.close()

def test_v3_adds_session_version_column():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from paperreadagent.core.schema import CORE_MIGRATIONS
    conn.executescript(CORE_MIGRATIONS[1])
    conn.executescript(CORE_MIGRATIONS[2])
    conn.executescript(CORE_MIGRATIONS[3])
    # verify session_version exists and defaults to 0
    conn.execute("INSERT INTO core_users (id, password_hash) VALUES (1, 'hash1')")
    conn.commit()
    row = conn.execute("SELECT session_version FROM core_users WHERE id = 1").fetchone()
    assert row is not None
    assert row["session_version"] == 0
    conn.close()

def test_core_users_single_row_enforcement():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    from paperreadagent.core.schema import CORE_MIGRATIONS
    conn.executescript(CORE_MIGRATIONS[1])
    conn.executescript(CORE_MIGRATIONS[2])
    conn.executescript(CORE_MIGRATIONS[3])
    conn.execute("INSERT INTO core_users (id, password_hash) VALUES (1, 'hash1')")
    conn.commit()
    try:
        conn.execute("INSERT INTO core_users (id, password_hash) VALUES (2, 'hash2')")
        conn.commit()
        assert False, "Should have raised IntegrityError"
    except sqlite3.IntegrityError:
        pass
    conn.close()
