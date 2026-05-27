"""
core/schema.py
核心层 DDL，独立版本号管理，不与 paperreadagent/db/schema.py 冲突。
"""

CORE_LATEST_VERSION = 3

CORE_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS core_schema_version (
        version INTEGER PRIMARY KEY
    );

    -- 统一笔记表（跨模块知识中心）
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

    -- LLM 调用追踪
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
    CREATE INDEX IF NOT EXISTS idx_core_llm_usage_module
        ON core_llm_usage(source_module);
    CREATE INDEX IF NOT EXISTS idx_core_llm_usage_date
        ON core_llm_usage(created_at);
    """,
    2: """
    -- 单用户认证表（只允许一行，id 必须为 1）
    CREATE TABLE IF NOT EXISTS core_users (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        username TEXT NOT NULL DEFAULT 'admin',
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 登录尝试记录（防暴力破解审计）
    CREATE TABLE IF NOT EXISTS core_login_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip_address TEXT NOT NULL,
        attempt_time TEXT NOT NULL DEFAULT (datetime('now')),
        success INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_login_attempts_ip
        ON core_login_attempts(ip_address, attempt_time);
    """,
    3: """
    -- session_version: 改密码时递增，使所有旧 cookie 失效
    ALTER TABLE core_users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0;
    """,
}
