"""
utils/extraction_parser.py
Parse and normalize the <JSON>...</JSON> structured-extraction block emitted by
agent2's per-paper LLM call. Best-effort: any parse failure returns None.

Schema (the 7-field minimal set from spec §4.1):
  problem: str
  methods: list[str]                                    (≤5)
  datasets: list[str]                                   (≤5)
  metrics: list[dict{name,value,condition}]             (≤6, each tuple complete)
  baselines: list[str]                                  (≤5)
  limitations: list[str]                                (≤4)
  contributions: list[str]                              (≤4)
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from utils.json_utils import clean_json

logger = logging.getLogger(__name__)

_JSON_TAG_RE = re.compile(r"<JSON>(.*?)</JSON>", re.DOTALL)

# Field caps (lists)
_CAPS = {
    "methods": 5,
    "datasets": 5,
    "metrics": 6,
    "baselines": 5,
    "limitations": 4,
    "contributions": 4,
}
def parse_extraction(raw: str | None) -> dict | None:
    """Pull <JSON>...</JSON> from raw, parse, normalize. None on any failure."""
    if not raw:
        return None
    m = _JSON_TAG_RE.search(raw)
    if not m:
        return None
    try:
        data = json.loads(clean_json(m.group(1)))
    except Exception as e:
        logger.debug("[extraction_parser] json.loads failed: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    return _normalize(data)


def _normalize(data: dict) -> dict:
    """Force the 7-field schema shape: defaults, types, caps, metric integrity."""
    out: dict[str, Any] = {}

    # problem: str
    p = data.get("problem")
    out["problem"] = p if isinstance(p, str) else ""

    # list-of-str fields (every _CAPS entry except metrics, which is handled below)
    for field, cap in _CAPS.items():
        if field == "metrics":
            continue
        v = data.get(field)
        if isinstance(v, list):
            out[field] = [s for s in v if isinstance(s, str) and s][:cap]
        else:
            out[field] = []

    # metrics: list of {name, value, condition} all required, all strings
    metrics_in = data.get("metrics")
    metrics_out: list[dict] = []
    if isinstance(metrics_in, list):
        for m in metrics_in:
            if not isinstance(m, dict):
                continue
            name, value, condition = m.get("name"), m.get("value"), m.get("condition")
            if all(isinstance(x, str) and x for x in (name, value, condition)):
                metrics_out.append({"name": name, "value": value, "condition": condition})
    out["metrics"] = metrics_out[:_CAPS["metrics"]]

    return out
