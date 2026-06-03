"""
web/progress.py
SSE 进度管理器 — 追踪 pipeline 各阶段进展，供前端实时展示。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field


@dataclass
class ProgressState:
    stage: str = "init"  # init | keywords | searching | filtering | downloading | reading | reporting | done
    stage_index: int = 0
    total_stages: int = 6
    papers_total: int = 0
    papers_completed: int = 0
    papers_failed: int = 0
    current_paper_title: str = ""
    messages: list[str] = field(default_factory=list)
    started_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "stage_index": self.stage_index,
            "total_stages": self.total_stages,
            "papers_total": self.papers_total,
            "papers_completed": self.papers_completed,
            "papers_failed": self.papers_failed,
            "current_paper_title": self.current_paper_title,
            "message": self.messages[-1] if self.messages else "",
            "error": self.error,
        }


# 全局进度存储 {session_id: ProgressState}
_progress: dict[int, ProgressState] = {}


def get_progress(session_id: int) -> ProgressState:
    if session_id not in _progress:
        _progress[session_id] = ProgressState()
    return _progress[session_id]


def remove_progress(session_id: int) -> None:
    _progress.pop(session_id, None)


async def sse_event_generator(session_id: int):
    """生成 SSE 事件流，每秒推送一次进度。"""
    try:
        while True:
            state = get_progress(session_id)
            data = json.dumps(state.to_dict(), ensure_ascii=False)
            yield f"data: {data}\n\n"
            if state.stage in ("done", "error"):
                break
            await asyncio.sleep(1.0)
        # 完成后保留 30 秒再清理
        await asyncio.sleep(30)
    finally:
        remove_progress(session_id)
