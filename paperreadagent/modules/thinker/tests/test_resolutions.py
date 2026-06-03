"""tests for thinker ResolutionTracker"""

import pytest


class TestResolutionTracker:
    def test_mark_in_progress(self):
        """in_progress 状态转换 (不依赖 DB，仅验证方法存在)。"""
        from paperreadagent.modules.thinker.resolutions import ResolutionTracker
        assert hasattr(ResolutionTracker, "mark_in_progress")
        assert hasattr(ResolutionTracker, "mark_done")
        assert hasattr(ResolutionTracker, "mark_cancelled")
        assert hasattr(ResolutionTracker, "get_pending")
