"""
modules/thinker/schema.py
Thinker 模块数据库表 DDL，版本化迁移。
"""

LATEST_VERSION = 2

MIGRATIONS: dict[int, str] = {
    1: """
    -- 思考伙伴对话会话
    CREATE TABLE IF NOT EXISTS thinker_conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT DEFAULT '',
        mode TEXT NOT NULL DEFAULT 'chat'
            CHECK(mode IN ('chat','socratic','feynman','kpt','orid')),
        status TEXT NOT NULL DEFAULT 'active'
            CHECK(status IN ('active','paused','closed')),
        snooze_until TEXT DEFAULT NULL,
        intensity TEXT NOT NULL DEFAULT 'moderate'
            CHECK(intensity IN ('gentle','moderate','sharp')),
        model_name TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 对话消息
    CREATE TABLE IF NOT EXISTS thinker_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL
            REFERENCES thinker_conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
        content TEXT NOT NULL DEFAULT '',
        embedding TEXT DEFAULT '',
        token_count INTEGER DEFAULT 0,
        opener TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_thinker_msgs_conv
        ON thinker_messages(conversation_id);
    CREATE INDEX IF NOT EXISTS idx_thinker_msgs_role
        ON thinker_messages(conversation_id, role);

    -- 用户承诺/决心追踪
    CREATE TABLE IF NOT EXISTS thinker_resolutions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL
            REFERENCES thinker_conversations(id) ON DELETE CASCADE,
        message_id INTEGER
            REFERENCES thinker_messages(id) ON DELETE SET NULL,
        content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','fulfilled','abandoned')),
        asked_at TEXT DEFAULT NULL,
        asked_count INTEGER NOT NULL DEFAULT 0,
        reflection TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_thinker_resolutions_status
        ON thinker_resolutions(status, created_at);

    -- 主动问题队列
    CREATE TABLE IF NOT EXISTS thinker_pending_questions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL
            REFERENCES thinker_conversations(id) ON DELETE CASCADE,
        question TEXT NOT NULL,
        question_type TEXT NOT NULL DEFAULT 'inactivity'
            CHECK(question_type IN ('inactivity','resolution','conflict','random')),
        source_refs TEXT DEFAULT '[]',
        generated_at TEXT NOT NULL DEFAULT (datetime('now')),
        delivered INTEGER NOT NULL DEFAULT 0,
        delivered_at TEXT DEFAULT NULL,
        dismissed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS idx_thinker_questions_pending
        ON thinker_pending_questions(conversation_id, delivered, dismissed);
    """,
    2: """
-- 记忆索引表（指向 core_notes 的轻量索引）
CREATE TABLE IF NOT EXISTS thinker_memory_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    core_note_id INTEGER UNIQUE NOT NULL,
    memory_type TEXT NOT NULL DEFAULT 'insight'
        CHECK(memory_type IN ('insight','resolution','profile_snapshot','summary','spark')),
    importance REAL NOT NULL DEFAULT 0.5,
    last_recalled_at TEXT DEFAULT NULL,
    recall_count INTEGER NOT NULL DEFAULT 0,
    embedding TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_thinker_memory_type
    ON thinker_memory_index(memory_type);
CREATE INDEX IF NOT EXISTS idx_thinker_memory_importance
    ON thinker_memory_index(importance DESC);

-- 用户画像表（单行，持续更新）
CREATE TABLE IF NOT EXISTS thinker_user_profile (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    research_domains TEXT NOT NULL DEFAULT '[]',
    knowledge_level TEXT NOT NULL DEFAULT '{}',
    thinking_style TEXT NOT NULL DEFAULT '',
    long_term_goals TEXT NOT NULL DEFAULT '[]',
    interaction_prefs TEXT NOT NULL DEFAULT '{}',
    confidence_scores TEXT NOT NULL DEFAULT '{}',
    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 确保单行
INSERT OR IGNORE INTO thinker_user_profile (id) VALUES (1);

-- 承诺表增加 deadline 和 in_progress / done / cancelled 状态
CREATE TABLE thinker_resolutions_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id INTEGER NOT NULL
        REFERENCES thinker_conversations(id) ON DELETE CASCADE,
    message_id INTEGER
        REFERENCES thinker_messages(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','in_progress','done','cancelled','fulfilled','abandoned')),
    deadline TEXT DEFAULT NULL,
    asked_at TEXT DEFAULT NULL,
    asked_count INTEGER NOT NULL DEFAULT 0,
    reflection TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO thinker_resolutions_new
    (id, conversation_id, message_id, content, status, asked_at, asked_count, reflection, created_at)
    SELECT id, conversation_id, message_id, content, status, asked_at, asked_count, reflection, created_at
    FROM thinker_resolutions;
DROP TABLE thinker_resolutions;
ALTER TABLE thinker_resolutions_new RENAME TO thinker_resolutions;
CREATE INDEX IF NOT EXISTS idx_thinker_resolutions_status
    ON thinker_resolutions(status, created_at);
""",
}
