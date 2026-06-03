"""
db/schema.py
数据库 DDL，按版本号管理，支持增量迁移。
"""

LATEST_VERSION = 6

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    );

    -- 研究项目
    CREATE TABLE IF NOT EXISTS projects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        description TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 调研会话
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        mode TEXT NOT NULL CHECK(mode IN ('full','collect','analyze','incremental')),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','running','completed','failed','cancelled')),
        config_snapshot TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        keywords TEXT,
        queries TEXT,
        total_candidates INTEGER DEFAULT 0,
        total_filtered INTEGER DEFAULT 0,
        total_downloaded INTEGER DEFAULT 0,
        total_analyzed INTEGER DEFAULT 0,
        total_failed_downloads INTEGER DEFAULT 0,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        session_dir TEXT NOT NULL UNIQUE,
        notes TEXT DEFAULT ''
    );

    -- 论文
    CREATE TABLE IF NOT EXISTS papers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        arxiv_id TEXT NOT NULL,
        source_platform TEXT CHECK(source_platform IN ('arxiv','s2','pwc','oa','local') OR source_platform IS NULL OR source_platform = ''),
        title TEXT DEFAULT '',
        authors TEXT DEFAULT '[]',
        published TEXT DEFAULT '',
        abstract TEXT DEFAULT '',
        relevance_score REAL DEFAULT 0.0,
        pdf_path TEXT DEFAULT '',
        summary_path TEXT DEFAULT '',
        download_status TEXT DEFAULT 'pending'
            CHECK(download_status IN ('pending','success','failed','skipped')),
        parse_status TEXT DEFAULT 'pending'
            CHECK(parse_status IN ('pending','success','failed')),
        summary_status TEXT DEFAULT 'pending'
            CHECK(summary_status IN ('pending','success','failed','cached')),
        source_url TEXT DEFAULT '',
        has_code INTEGER DEFAULT 0,
        code_url TEXT DEFAULT '',
        venue TEXT DEFAULT '',
        citation_count INTEGER DEFAULT 0,
        UNIQUE(session_id, arxiv_id)
    );

    -- 总结（与 prompt + PDF 内容绑定）
    CREATE TABLE IF NOT EXISTS summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
        summary_prompt_hash TEXT NOT NULL,
        model_name TEXT NOT NULL,
        temperature REAL NOT NULL,
        max_chars INTEGER NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        token_count INTEGER,
        pdf_text_hash TEXT NOT NULL,
        UNIQUE(paper_id, summary_prompt_hash, pdf_text_hash)
    );

    -- 运行日志
    CREATE TABLE IF NOT EXISTS run_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        level TEXT NOT NULL,
        message TEXT NOT NULL,
        timestamp TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    2: """
    -- 论文笔记（每篇论文一个笔记）
    CREATE TABLE IF NOT EXISTS notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id) ON DELETE CASCADE,
        content TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    3: """
    -- 修复 sessions.mode CHECK 约束，增加 'incremental'
    -- SQLite 无法直接 ALTER CHECK，通过重建表实现
    PRAGMA foreign_keys = OFF;
    CREATE TABLE sessions_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        mode TEXT NOT NULL CHECK(mode IN ('full','collect','analyze','incremental')),
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK(status IN ('pending','running','completed','failed','cancelled')),
        config_snapshot TEXT NOT NULL,
        config_hash TEXT NOT NULL,
        keywords TEXT,
        queries TEXT,
        total_candidates INTEGER DEFAULT 0,
        total_filtered INTEGER DEFAULT 0,
        total_downloaded INTEGER DEFAULT 0,
        total_analyzed INTEGER DEFAULT 0,
        total_failed_downloads INTEGER DEFAULT 0,
        started_at TEXT,
        completed_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        session_dir TEXT NOT NULL UNIQUE,
        notes TEXT DEFAULT ''
    );
    INSERT INTO sessions_new SELECT * FROM sessions;
    DROP TABLE sessions;
    ALTER TABLE sessions_new RENAME TO sessions;
    PRAGMA foreign_keys = ON;
    """,
    4: """
    -- 论文表增加 doi 列，支持多源 PDF 下载
    ALTER TABLE papers ADD COLUMN doi TEXT DEFAULT '';
    """,
    5: """
    -- 收藏夹
    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        paper_id INTEGER NOT NULL UNIQUE REFERENCES papers(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_favorites_paper_id ON favorites(paper_id);
    CREATE INDEX IF NOT EXISTS idx_favorites_created_at ON favorites(created_at);
    """,
    6: """
    -- 修复 papers.source_platform CHECK 约束，增加 'dblp'
    -- SQLite 无法直接 ALTER CHECK，通过重建表实现
    PRAGMA foreign_keys = OFF;
    CREATE TABLE papers_new (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        arxiv_id TEXT NOT NULL,
        source_platform TEXT CHECK(source_platform IN ('arxiv','s2','pwc','oa','dblp','local') OR source_platform IS NULL OR source_platform = ''),
        title TEXT DEFAULT '',
        authors TEXT DEFAULT '[]',
        published TEXT DEFAULT '',
        abstract TEXT DEFAULT '',
        relevance_score REAL DEFAULT 0.0,
        pdf_path TEXT DEFAULT '',
        summary_path TEXT DEFAULT '',
        download_status TEXT DEFAULT 'pending'
            CHECK(download_status IN ('pending','success','failed','skipped')),
        parse_status TEXT DEFAULT 'pending'
            CHECK(parse_status IN ('pending','success','failed')),
        summary_status TEXT DEFAULT 'pending'
            CHECK(summary_status IN ('pending','success','failed','cached')),
        source_url TEXT DEFAULT '',
        has_code INTEGER DEFAULT 0,
        code_url TEXT DEFAULT '',
        venue TEXT DEFAULT '',
        citation_count INTEGER DEFAULT 0,
        doi TEXT DEFAULT '',
        UNIQUE(session_id, arxiv_id)
    );
    INSERT INTO papers_new SELECT * FROM papers;
    DROP TABLE papers;
    ALTER TABLE papers_new RENAME TO papers;
    PRAGMA foreign_keys = ON;
    """,
}
