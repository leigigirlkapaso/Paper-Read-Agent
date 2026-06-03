"""tests for ideator CrossRecall"""

import pytest
from paperreadagent.modules.ideator.effort import EFFORT_PARAMS


class TestCrossRecall:
    def test_init_accepts_data_access(self):
        from paperreadagent.modules.ideator.cross_recall import CrossRecall
        assert hasattr(CrossRecall, '__init__')

    def test_has_six_recall_paths(self):
        from paperreadagent.modules.ideator.cross_recall import CrossRecall
        paths = ['_recall_similarity', '_recall_contradiction', '_recall_cross_project',
                 '_recall_cross_layer', '_recall_random_walk', '_recall_timeline']
        for p in paths:
            assert hasattr(CrossRecall, p), f"Missing recall path: {p}"

    def test_recall_respects_effort_paths(self):
        params = EFFORT_PARAMS["lite"]
        assert len(params["recall_paths"]) == 4  # similarity, contradiction, cross_layer, timeline

    def test_beast_effort_uses_all_paths(self):
        params = EFFORT_PARAMS["beast"]
        assert len(params["recall_paths"]) == 6
