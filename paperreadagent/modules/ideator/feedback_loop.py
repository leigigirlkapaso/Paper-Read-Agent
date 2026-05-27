"""
modules/ideator/feedback_loop.py
FeedbackLoop -- user feedback drives recall path weight adjustment.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WEIGHT_MIN = 0.0
WEIGHT_MAX = 2.0
DISABLE_THRESHOLD = 0.2
USEFUL_DELTA = 0.05
NOISE_DELTA = -0.1


def adjust_weight(current: float, feedback: str) -> float:
    if feedback == "useful":
        new_w = current + USEFUL_DELTA
    elif feedback in ("duplicate", "noise"):
        new_w = current + NOISE_DELTA
    else:
        return current
    return max(WEIGHT_MIN, min(WEIGHT_MAX, new_w))


class FeedbackLoop:
    def __init__(self, data_access):
        self._data = data_access

    def record_feedback(self, source_type: str, feedback: str) -> None:
        cur = self._data.get_recall_weight(source_type)
        if cur is None:
            return
        new_weight = adjust_weight(cur["weight"], feedback)
        useful_inc = 1 if feedback == "useful" else 0
        noise_inc = 1 if feedback in ("duplicate", "noise") else 0
        self._data.update_recall_weight(
            source_type=source_type,
            weight=new_weight,
            useful_inc=useful_inc,
            noise_inc=noise_inc,
        )
        if new_weight < DISABLE_THRESHOLD:
            logger.info(f"[FeedbackLoop] {source_type} weight dropped to {new_weight:.2f}, disabled")
