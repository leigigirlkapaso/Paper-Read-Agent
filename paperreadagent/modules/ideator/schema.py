"""
modules/ideator/schema.py
"""

LATEST_VERSION = 10

MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE IF NOT EXISTS ideator_sparks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'seed'
            CHECK(status IN ('seed','deepening','deep_done')),
        source_type TEXT NOT NULL DEFAULT '',
        source_refs TEXT NOT NULL DEFAULT '[]',
        embedding TEXT NOT NULL DEFAULT '',
        quality_score REAL NOT NULL DEFAULT 0.5,
        depth_content TEXT NOT NULL DEFAULT '',
        user_feedback TEXT DEFAULT NULL
            CHECK(user_feedback IN ('useful','duplicate','noise')),
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        deepened_at TEXT DEFAULT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_ideator_sparks_status
        ON ideator_sparks(status);
    CREATE INDEX IF NOT EXISTS idx_ideator_sparks_quality
        ON ideator_sparks(quality_score DESC);
    CREATE INDEX IF NOT EXISTS idx_ideator_sparks_type
        ON ideator_sparks(source_type);

    CREATE TABLE IF NOT EXISTS ideator_cross_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_a_type TEXT NOT NULL
            CHECK(source_a_type IN ('paper','core_note','resolution')),
        source_a_id INTEGER NOT NULL,
        source_b_type TEXT NOT NULL
            CHECK(source_b_type IN ('paper','core_note','resolution')),
        source_b_id INTEGER NOT NULL,
        link_type TEXT NOT NULL
            CHECK(link_type IN ('similarity','contradiction','temporal','random','cross_layer')),
        relevance_score REAL NOT NULL DEFAULT 0.0,
        reasoning TEXT NOT NULL DEFAULT '',
        spark_id INTEGER REFERENCES ideator_sparks(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_ideator_links_score
        ON ideator_cross_links(relevance_score DESC);
    CREATE INDEX IF NOT EXISTS idx_ideator_links_spark
        ON ideator_cross_links(spark_id);
    """,
    2: """
    -- 新增管道运行表
    CREATE TABLE IF NOT EXISTS ideator_pipeline_runs (
        run_id TEXT PRIMARY KEY,
        trigger TEXT NOT NULL DEFAULT 'manual',
        effort TEXT NOT NULL DEFAULT 'balanced',
        stages_completed TEXT NOT NULL DEFAULT '[]',
        stats TEXT NOT NULL DEFAULT '{}',
        total_tokens INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at TEXT
    );

    -- 新增审查记录表
    CREATE TABLE IF NOT EXISTS ideator_review_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spark_id INTEGER NOT NULL REFERENCES ideator_sparks(id),
        stage TEXT NOT NULL CHECK(stage IN ('review','arbitration','audit')),
        reviewer_model TEXT NOT NULL,
        reviewer_role TEXT NOT NULL CHECK(reviewer_role IN ('reviewer_1','reviewer_2','arbiter','auditor')),
        scores TEXT NOT NULL DEFAULT '{}',
        verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REVISE','REJECT','ARBITRATE','OVERTURN','SUPPORTED','STRETCHED','UNSUPPORTED')),
        reasoning TEXT NOT NULL DEFAULT '',
        prompt_snapshot TEXT NOT NULL DEFAULT '',
        raw_response TEXT NOT NULL DEFAULT '',
        token_usage TEXT NOT NULL DEFAULT '{}',
        escalation_reason TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL REFERENCES ideator_pipeline_runs(run_id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_review_spark ON ideator_review_records(spark_id);
    CREATE INDEX IF NOT EXISTS idx_review_run ON ideator_review_records(run_id);

    -- 新增召回权重表
    CREATE TABLE IF NOT EXISTS ideator_recall_weights (
        source_type TEXT PRIMARY KEY,
        weight REAL NOT NULL DEFAULT 1.0,
        useful_count INTEGER NOT NULL DEFAULT 0,
        noise_count INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- 插入默认权重
    INSERT OR IGNORE INTO ideator_recall_weights (source_type, weight) VALUES
        ('similarity', 1.0),
        ('contradiction', 1.0),
        ('cross_project', 1.0),
        ('cross_layer', 1.0),
        ('random_walk', 0.5),
        ('timeline', 1.0);

    -- 扩展 ideator_sparks 表
    ALTER TABLE ideator_sparks ADD COLUMN run_id TEXT DEFAULT '';
    ALTER TABLE ideator_sparks ADD COLUMN generator_score REAL DEFAULT 0.0;
    ALTER TABLE ideator_sparks ADD COLUMN final_score REAL DEFAULT 0.0;
    ALTER TABLE ideator_sparks ADD COLUMN review_status TEXT DEFAULT 'pending'
        CHECK(review_status IN ('pending','passed','revised','rejected','escalated','flagged'));
    ALTER TABLE ideator_sparks ADD COLUMN review_count INTEGER DEFAULT 0;
    """,
    3: """
    CREATE TABLE IF NOT EXISTS ideator_roundtables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spark_id INTEGER NOT NULL REFERENCES ideator_sparks(id),
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','closed')),
        participants TEXT NOT NULL DEFAULT '[]',
        round_count INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        closed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS ideator_roundtable_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
        round_number INTEGER NOT NULL DEFAULT 1,
        sender_type TEXT NOT NULL CHECK(sender_type IN ('user','model','system')),
        sender_name TEXT NOT NULL,
        sender_role TEXT,
        message_type TEXT NOT NULL CHECK(message_type IN ('question','answer','interjection','compression','exit_statement','divergence_report')),
        content TEXT NOT NULL DEFAULT '',
        word_count INTEGER NOT NULL DEFAULT 0,
        mentioned_by TEXT NOT NULL DEFAULT '[]',
        parent_id INTEGER REFERENCES ideator_roundtable_messages(id),
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_rt_msg_roundtable ON ideator_roundtable_messages(roundtable_id, round_number);

    CREATE TABLE IF NOT EXISTS ideator_roundtable_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
        message_id INTEGER REFERENCES ideator_roundtable_messages(id),
        model_name TEXT NOT NULL,
        model_role TEXT NOT NULL,
        round_number INTEGER NOT NULL DEFAULT 1,
        prompt_sent TEXT NOT NULL DEFAULT '',
        raw_response TEXT NOT NULL DEFAULT '',
        tokens_input INTEGER NOT NULL DEFAULT 0,
        tokens_output INTEGER NOT NULL DEFAULT 0,
        tokens_total INTEGER NOT NULL DEFAULT 0,
        token_pct_used REAL NOT NULL DEFAULT 0.0,
        compression_triggered INTEGER NOT NULL DEFAULT 0,
        compression_summary TEXT NOT NULL DEFAULT '',
        exit_reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_rt_snap_roundtable ON ideator_roundtable_snapshots(roundtable_id);

    ALTER TABLE ideator_sparks ADD COLUMN roundtable_id INTEGER;
    """,
    4: """
    CREATE TABLE IF NOT EXISTS ideator_team_memory (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
        spark_id INTEGER NOT NULL REFERENCES ideator_sparks(id),
        memory_type TEXT NOT NULL
            CHECK(memory_type IN ('consensus','disagreement','decision',
                  'spark_evolution','evidence','user_feedback',
                  'open_question','assumption','watermark')),
        content TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        round_number INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_team_memory_spark
        ON ideator_team_memory(spark_id, memory_type);
    CREATE INDEX IF NOT EXISTS idx_team_memory_rt
        ON ideator_team_memory(roundtable_id);
    """,
    5: """
    -- 移除 source_type CHECK 约束：改为开放式枚举，召回路径可自由扩展
    -- SQLite 不支持 DROP CHECK，需重建表
    CREATE TABLE IF NOT EXISTS ideator_sparks_v5 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'seed'
            CHECK(status IN ('seed','deepening','deep_done')),
        source_type TEXT NOT NULL DEFAULT '',
        source_refs TEXT NOT NULL DEFAULT '[]',
        embedding TEXT NOT NULL DEFAULT '',
        quality_score REAL NOT NULL DEFAULT 0.5,
        depth_content TEXT NOT NULL DEFAULT '',
        user_feedback TEXT DEFAULT NULL
            CHECK(user_feedback IN ('useful','duplicate','noise')),
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        deepened_at TEXT DEFAULT NULL,
        run_id TEXT DEFAULT '',
        generator_score REAL DEFAULT 0.0,
        final_score REAL DEFAULT 0.0,
        review_status TEXT DEFAULT 'pending'
            CHECK(review_status IN ('pending','passed','revised','rejected','escalated','flagged')),
        review_count INTEGER DEFAULT 0,
        roundtable_id INTEGER
    );

    INSERT OR IGNORE INTO ideator_sparks_v5
        (id, content, status, source_type, source_refs, embedding, quality_score,
         depth_content, user_feedback, metadata, created_at, deepened_at,
         run_id, generator_score, final_score, review_status, review_count, roundtable_id)
        SELECT id, content, status, source_type, source_refs, embedding, quality_score,
               depth_content, user_feedback, metadata, created_at, deepened_at,
               run_id, generator_score, final_score, review_status, review_count, roundtable_id
        FROM ideator_sparks;

    DROP TABLE ideator_sparks;
    ALTER TABLE ideator_sparks_v5 RENAME TO ideator_sparks;

    CREATE INDEX IF NOT EXISTS idx_ideator_sparks_status
        ON ideator_sparks(status);
    CREATE INDEX IF NOT EXISTS idx_ideator_sparks_quality
        ON ideator_sparks(quality_score DESC);
    CREATE INDEX IF NOT EXISTS idx_ideator_sparks_type
        ON ideator_sparks(source_type);
    """,
    6: """
    -- 直接圆桌支持：ideator_roundtables.spark_id 改为可空
    CREATE TABLE IF NOT EXISTS ideator_roundtables_v6 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spark_id INTEGER,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','closed')),
        participants TEXT NOT NULL DEFAULT '[]',
        round_count INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL DEFAULT (datetime('now')),
        closed_at TEXT
    );

    INSERT OR IGNORE INTO ideator_roundtables_v6
        SELECT * FROM ideator_roundtables;

    DROP TABLE ideator_roundtables;
    ALTER TABLE ideator_roundtables_v6 RENAME TO ideator_roundtables;

    -- ideator_team_memory.spark_id 同样改为可空
    CREATE TABLE IF NOT EXISTS ideator_team_memory_v6 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
        spark_id INTEGER,
        memory_type TEXT NOT NULL
            CHECK(memory_type IN ('consensus','disagreement','decision',
                  'spark_evolution','evidence','user_feedback',
                  'open_question','assumption','watermark')),
        content TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        round_number INTEGER,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    INSERT OR IGNORE INTO ideator_team_memory_v6
        SELECT * FROM ideator_team_memory;

    DROP TABLE ideator_team_memory;
    ALTER TABLE ideator_team_memory_v6 RENAME TO ideator_team_memory;

    CREATE INDEX IF NOT EXISTS idx_team_memory_spark
        ON ideator_team_memory(spark_id, memory_type);
    CREATE INDEX IF NOT EXISTS idx_team_memory_rt
        ON ideator_team_memory(roundtable_id);
    """,
    7: """
    -- 扩展 ideator_roundtable_messages.message_type CHECK：增加 supplement
    CREATE TABLE IF NOT EXISTS ideator_roundtable_messages_v7 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
        round_number INTEGER NOT NULL DEFAULT 1,
        sender_type TEXT NOT NULL CHECK(sender_type IN ('user','model','system')),
        sender_name TEXT NOT NULL,
        sender_role TEXT,
        message_type TEXT NOT NULL CHECK(message_type IN ('question','answer','interjection','compression','exit_statement','divergence_report','supplement')),
        content TEXT NOT NULL DEFAULT '',
        word_count INTEGER NOT NULL DEFAULT 0,
        mentioned_by TEXT NOT NULL DEFAULT '[]',
        parent_id INTEGER REFERENCES ideator_roundtable_messages(id),
        metadata TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    INSERT OR IGNORE INTO ideator_roundtable_messages_v7
        SELECT * FROM ideator_roundtable_messages;

    DROP TABLE ideator_roundtable_messages;
    ALTER TABLE ideator_roundtable_messages_v7 RENAME TO ideator_roundtable_messages;

    CREATE INDEX IF NOT EXISTS idx_rt_msg_roundtable ON ideator_roundtable_messages(roundtable_id, round_number);
    """,
    8: """
    -- 扩展 ideator_review_records CHECK 约束：覆盖辩论系统的 stage / reviewer_role / verdict
    CREATE TABLE IF NOT EXISTS ideator_review_records_v8 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        spark_id INTEGER NOT NULL REFERENCES ideator_sparks(id),
        stage TEXT NOT NULL CHECK(stage IN ('review','arbitration','audit','debate_initial','debate_re_review')),
        reviewer_model TEXT NOT NULL,
        reviewer_role TEXT NOT NULL CHECK(reviewer_role IN ('reviewer_1','reviewer_2','arbiter','auditor','rev1','rev2','rev3','arb1','arb2','rec','gen')),
        scores TEXT NOT NULL DEFAULT '{}',
        verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REVISE','REJECT','ARBITRATE','OVERTURN','CONFIRM_R1','CONFIRM_R2','SUPPORTED','STRETCHED','UNSUPPORTED')),
        reasoning TEXT NOT NULL DEFAULT '',
        prompt_snapshot TEXT NOT NULL DEFAULT '',
        raw_response TEXT NOT NULL DEFAULT '',
        token_usage TEXT NOT NULL DEFAULT '{}',
        escalation_reason TEXT NOT NULL DEFAULT '',
        run_id TEXT NOT NULL REFERENCES ideator_pipeline_runs(run_id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    INSERT OR IGNORE INTO ideator_review_records_v8
        SELECT * FROM ideator_review_records;

    DROP TABLE ideator_review_records;
    ALTER TABLE ideator_review_records_v8 RENAME TO ideator_review_records;

    CREATE INDEX IF NOT EXISTS idx_review_spark ON ideator_review_records(spark_id);
    CREATE INDEX IF NOT EXISTS idx_review_run ON ideator_review_records(run_id);
    """,
    9: """
    -- idea 级 embedding：每条笔记拆分为多个独立 idea，各自 embedding
    -- 支持 MaxSim 聚合召回（ColBERT-style late interaction）
    CREATE TABLE IF NOT EXISTS ideator_note_ideas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        note_source TEXT NOT NULL,  -- 'legacy' | 'core'
        note_id INTEGER NOT NULL,
        idea_index INTEGER NOT NULL,
        content TEXT NOT NULL,
        embedding TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(note_source, note_id, idea_index)
    );

    CREATE INDEX IF NOT EXISTS idx_note_ideas_source
        ON ideator_note_ideas(note_source, note_id);

    CREATE INDEX IF NOT EXISTS idx_note_ideas_embedding
        ON ideator_note_ideas(embedding)
        WHERE embedding != '';
    """,
    10: """
    -- 扩展 ideator_cross_links.link_type CHECK：补充 cross_project / random_walk / timeline
    CREATE TABLE IF NOT EXISTS ideator_cross_links_v10 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_a_type TEXT NOT NULL
            CHECK(source_a_type IN ('paper','core_note','resolution')),
        source_a_id INTEGER NOT NULL,
        source_b_type TEXT NOT NULL
            CHECK(source_b_type IN ('paper','core_note','resolution')),
        source_b_id INTEGER NOT NULL,
        link_type TEXT NOT NULL
            CHECK(link_type IN ('similarity','contradiction','temporal','random',
                  'cross_layer','cross_project','random_walk','timeline')),
        relevance_score REAL NOT NULL DEFAULT 0.0,
        reasoning TEXT NOT NULL DEFAULT '',
        spark_id INTEGER REFERENCES ideator_sparks(id),
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );

    INSERT OR IGNORE INTO ideator_cross_links_v10
        SELECT * FROM ideator_cross_links;

    DROP TABLE ideator_cross_links;
    ALTER TABLE ideator_cross_links_v10 RENAME TO ideator_cross_links;

    CREATE INDEX IF NOT EXISTS idx_ideator_links_score
        ON ideator_cross_links(relevance_score DESC);
    CREATE INDEX IF NOT EXISTS idx_ideator_links_spark
        ON ideator_cross_links(spark_id);
    """,
}
