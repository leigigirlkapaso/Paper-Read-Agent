"""
utils/json_utils.py
Shared safe-JSON helpers used across the codebase.
"""

from __future__ import annotations

import json


def safe_json_loads(raw: str | None, default=None) -> list | dict:
    """Parse JSON safely, returning default on failure."""
    if default is None:
        default = []
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def safe_json_dumps(obj, **kwargs) -> str:
    return json.dumps(obj, ensure_ascii=False, **kwargs)


def clean_json(raw: str) -> str:
    """Strip markdown code fences from raw LLM output.

    Handles:
      ```json\\n{...}\\n```
      ```\\n{...}\\n```
      {\\n  "key": "value"\\n}  (no fence, already clean)

    More robust than simple removeprefix chains because it handles
    trailing whitespace inside fences and multi-line variants.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)
    return raw.strip()


def extract_json_list(raw: str) -> list[str]:
    """Extract a list of strings from a potentially messy LLM JSON output."""
    import re

    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(item) for item in data if str(item).strip()]
        if isinstance(data, dict):
            return [str(v) for v in data.values() if str(v).strip()]
    except (json.JSONDecodeError, TypeError):
        pass

    m = re.search(r"\[.*?\]", raw, re.DOTALL)
    if m:
        try:
            return [str(item) for item in json.loads(m.group()) if str(item).strip()]
        except json.JSONDecodeError:
            pass

    stripped = (l.strip("- *").strip() for l in raw.split("\n"))
    lines = [s for s in stripped if s]
    return lines or [raw.strip()[:200]]
