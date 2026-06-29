"""Tests for utils.extraction_parser."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from utils.extraction_parser import parse_extraction, _normalize


def _valid_block() -> str:
    return """### Paper title

Some body text...

<JSON>
{
  "problem": "Studying X under Y",
  "methods": ["MethodA", "MethodB"],
  "datasets": ["DS1"],
  "metrics": [{"name": "Acc", "value": "92.3%", "condition": "DS1 val"}],
  "baselines": ["BaselineX"],
  "limitations": ["Limited to English"],
  "contributions": ["Faster training"]
}
</JSON>
"""


class TestParseExtraction:
    def test_parse_happy(self):
        out = parse_extraction(_valid_block())
        assert out is not None
        assert out["problem"] == "Studying X under Y"
        assert out["methods"] == ["MethodA", "MethodB"]
        assert out["metrics"][0]["name"] == "Acc"
        assert all(k in out for k in
                   ["problem","methods","datasets","metrics","baselines","limitations","contributions"])

    def test_parse_no_tag(self):
        assert parse_extraction("just markdown no json tag") is None

    def test_parse_malformed_json(self):
        bad = "<JSON>{not valid json,,,}</JSON>"
        assert parse_extraction(bad) is None

    def test_parse_empty_input(self):
        assert parse_extraction("") is None
        assert parse_extraction(None) is None  # defensive

    def test_parse_json_in_codefence_inside_tag(self):
        """LLM sometimes wraps JSON in ```json fences even when told not to.
        clean_json should strip them."""
        wrapped = """<JSON>
```json
{"problem":"x","methods":[],"datasets":[],"metrics":[],"baselines":[],"limitations":[],"contributions":[]}
```
</JSON>"""
        out = parse_extraction(wrapped)
        assert out is not None
        assert out["problem"] == "x"

    def test_parse_whitespace_only_tag(self):
        assert parse_extraction("<JSON>   \n  </JSON>") is None

    def test_parse_multiple_json_blocks_first_wins(self):
        """If LLM emits a draft + corrected JSON, current behavior takes the
        first match. Pin this so future changes are intentional."""
        raw = """<JSON>
{"problem": "draft", "methods": [], "datasets": [], "metrics": [],
 "baselines": [], "limitations": [], "contributions": []}
</JSON>
... corrected version below ...
<JSON>
{"problem": "corrected", "methods": [], "datasets": [], "metrics": [],
 "baselines": [], "limitations": [], "contributions": []}
</JSON>"""
        out = parse_extraction(raw)
        assert out is not None
        assert out["problem"] == "draft"  # first-wins (pinned behavior)


class TestNormalize:
    def test_caps_lists(self):
        data = {"problem": "p", "methods": [f"m{i}" for i in range(8)],
                "datasets": [], "metrics": [], "baselines": [],
                "limitations": [], "contributions": []}
        out = _normalize(data)
        assert len(out["methods"]) == 5  # capped

    def test_fills_missing_fields(self):
        out = _normalize({"problem": "p"})
        assert out["problem"] == "p"
        assert out["methods"] == []
        assert out["datasets"] == []
        assert out["metrics"] == []
        assert out["baselines"] == []
        assert out["limitations"] == []
        assert out["contributions"] == []

    def test_drops_bad_metrics(self):
        data = {
            "problem": "p", "methods": [], "datasets": [],
            "metrics": [
                {"name": "Acc", "value": "92%", "condition": "DS1"},   # OK
                {"name": "F1", "value": "0.8"},                         # missing condition -> drop
                {"value": "100", "condition": "x"},                     # missing name -> drop
                "not a dict",                                            # wrong type -> drop
            ],
            "baselines": [], "limitations": [], "contributions": []
        }
        out = _normalize(data)
        assert len(out["metrics"]) == 1
        assert out["metrics"][0]["name"] == "Acc"

    def test_drops_wrong_types(self):
        data = {"problem": ["should be str"], "methods": "should be list",
                "datasets": None, "metrics": [], "baselines": [],
                "limitations": [], "contributions": []}
        out = _normalize(data)
        assert out["problem"] == ""
        assert out["methods"] == []
        assert out["datasets"] == []
