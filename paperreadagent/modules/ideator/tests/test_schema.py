from modules.ideator.schema import LATEST_VERSION, MIGRATIONS


def test_latest_version_is_10():
    assert LATEST_VERSION == 10


def test_migrations_have_v1_through_v10():
    assert 1 in MIGRATIONS
    assert 2 in MIGRATIONS
    assert 3 in MIGRATIONS
    assert 4 in MIGRATIONS
    assert 5 in MIGRATIONS
    assert 6 in MIGRATIONS
    assert 7 in MIGRATIONS
    assert 8 in MIGRATIONS
    assert 9 in MIGRATIONS
    assert 10 in MIGRATIONS


def test_v2_creates_pipeline_runs_table():
    assert "ideator_pipeline_runs" in MIGRATIONS[2]


def test_v2_creates_review_records_table():
    assert "ideator_review_records" in MIGRATIONS[2]


def test_v2_creates_recall_weights_table():
    assert "ideator_recall_weights" in MIGRATIONS[2]


def test_v2_adds_spark_columns():
    sql = MIGRATIONS[2]
    assert "run_id" in sql
    assert "generator_score" in sql
    assert "final_score" in sql
    assert "review_status" in sql
    assert "review_count" in sql


def test_v3_creates_roundtables_table():
    assert "ideator_roundtables" in MIGRATIONS[3]


def test_v3_creates_roundtable_messages_table():
    assert "ideator_roundtable_messages" in MIGRATIONS[3]


def test_v3_creates_roundtable_snapshots_table():
    assert "ideator_roundtable_snapshots" in MIGRATIONS[3]


def test_v3_creates_message_index():
    sql = MIGRATIONS[3]
    assert "idx_rt_msg_roundtable" in sql


def test_v3_creates_snapshot_index():
    sql = MIGRATIONS[3]
    assert "idx_rt_snap_roundtable" in sql


def test_v3_adds_roundtable_id_to_sparks():
    sql = MIGRATIONS[3]
    assert "roundtable_id" in sql


def test_v3_check_constraints_roundtable_status():
    sql = MIGRATIONS[3]
    assert "CHECK(status IN ('active','paused','closed'))" in sql


def test_v3_check_constraints_sender_type():
    sql = MIGRATIONS[3]
    assert "CHECK(sender_type IN ('user','model','system'))" in sql


def test_v3_check_constraints_message_type():
    sql = MIGRATIONS[3]
    assert "CHECK(message_type IN ('question','answer','interjection','compression','exit_statement','divergence_report'))" in sql


def test_v4_team_memory_table():
    assert 4 in MIGRATIONS
    assert "ideator_team_memory" in MIGRATIONS[4]
    assert "consensus" in MIGRATIONS[4]


def test_v5_removes_source_type_check():
    assert 5 in MIGRATIONS
    assert "ideator_sparks_v5" in MIGRATIONS[5]
    assert "DROP TABLE ideator_sparks" in MIGRATIONS[5]


def test_v6_makes_spark_id_nullable():
    assert 6 in MIGRATIONS
    sql = MIGRATIONS[6]
    assert "ideator_roundtables_v6" in sql
    assert "ideator_team_memory_v6" in sql
    # spark_id should be plain INTEGER (no NOT NULL, no REFERENCES)
    assert "spark_id INTEGER," in sql


def test_v7_adds_supplement_message_type():
    assert 7 in MIGRATIONS
    sql = MIGRATIONS[7]
    assert "ideator_roundtable_messages_v7" in sql
    assert "supplement" in sql


def test_v8_expands_review_records_check():
    assert 8 in MIGRATIONS
    sql = MIGRATIONS[8]
    assert "ideator_review_records_v8" in sql
    assert "debate_initial" in sql
    assert "debate_re_review" in sql
    assert "rev1" in sql
    assert "rev2" in sql
    assert "rev3" in sql
    assert "arb1" in sql
    assert "arb2" in sql
    assert "rec" in sql
    assert "gen" in sql
    assert "CONFIRM_R1" in sql
    assert "CONFIRM_R2" in sql


def test_v9_creates_note_ideas_table():
    assert 9 in MIGRATIONS
    sql = MIGRATIONS[9]
    assert "ideator_note_ideas" in sql
    assert "note_source" in sql
    assert "note_id" in sql
    assert "idea_index" in sql
    assert "UNIQUE(note_source, note_id, idea_index)" in sql
    assert "idx_note_ideas_source" in sql
    assert "idx_note_ideas_embedding" in sql
