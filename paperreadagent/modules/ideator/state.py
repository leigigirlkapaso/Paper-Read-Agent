"""
modules/ideator/state.py
管道状态持久化 — 阶段间存盘，支持压缩恢复和断点续传。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

STATE_FILENAME = "PIPELINE_STATE.json"


@dataclass
class PipelineState:
    run_id: str
    current_stage: str = "recall"
    stages_completed: list[str] = field(default_factory=list)
    candidates_count: int = 0
    sparks_generated: int = 0
    sparks_reviewed: int = 0
    effort: str = "balanced"
    updated_at: str = ""

    def to_dict(self) -> dict:
        import datetime
        return {
            "run_id": self.run_id,
            "current_stage": self.current_stage,
            "stages_completed": self.stages_completed,
            "candidates_count": self.candidates_count,
            "sparks_generated": self.sparks_generated,
            "sparks_reviewed": self.sparks_reviewed,
            "effort": self.effort,
            "updated_at": datetime.datetime.now().isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PipelineState":
        return cls(
            run_id=d["run_id"],
            current_stage=d.get("current_stage", "recall"),
            stages_completed=d.get("stages_completed", []),
            candidates_count=d.get("candidates_count", 0),
            sparks_generated=d.get("sparks_generated", 0),
            sparks_reviewed=d.get("sparks_reviewed", 0),
            effort=d.get("effort", "balanced"),
            updated_at=d.get("updated_at", ""),
        )


def save_state(state: PipelineState, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    state_file = directory / STATE_FILENAME
    state_file.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))


def load_state(directory: Path) -> PipelineState | None:
    state_file = directory / STATE_FILENAME
    if not state_file.exists():
        return None
    return PipelineState.from_dict(json.loads(state_file.read_text()))
