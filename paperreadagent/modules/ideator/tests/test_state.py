import json
from pathlib import Path
from modules.ideator.state import PipelineState, save_state, load_state


def test_pipeline_state_defaults():
    ps = PipelineState(run_id="test-123")
    assert ps.run_id == "test-123"
    assert ps.current_stage == "recall"
    assert ps.stages_completed == []


def test_save_and_load_state(tmp_path):
    ps = PipelineState(run_id="test-456", current_stage="review",
                       stages_completed=["recall", "score", "generate"],
                       candidates_count=24, sparks_generated=5,
                       effort="max")
    save_state(ps, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.run_id == "test-456"
    assert loaded.current_stage == "review"
    assert loaded.candidates_count == 24
    assert loaded.effort == "max"


def test_load_state_missing_file(tmp_path):
    ps = load_state(tmp_path)
    assert ps is None
