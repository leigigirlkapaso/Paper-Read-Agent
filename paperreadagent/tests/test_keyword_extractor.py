"""
tests/test_keyword_extractor.py
Unit tests for pure functions in agent1/keyword_extractor.py.
No external API calls, no database required.
"""

from __future__ import annotations

import pytest
from agent1.keyword_extractor import (
    _parse_json_response,
    _fallback_extract,
    _extract_keywords,
    _extract_queries,
)


# ═══════════════════════════════════════════════════════════════════
# _parse_json_response
# ═══════════════════════════════════════════════════════════════════

class TestParseJsonResponse:
    """Test _parse_json_response with v1, v2, and fallback formats."""

    def test_v2_format_angles_array(self):
        """Parse v2 format JSON with angles array."""
        raw = """{
            "keywords": ["haptic rendering", "vibrotactile feedback"],
            "angles": [
                {
                    "angle": "haptic rendering pipeline",
                    "broad": ["all:\\"haptic rendering\\" OR all:\\"vibrotactile display\\""],
                    "method": ["all:\\"haptic\\" AND all:\\"foot\\" AND all:rendering"],
                    "competitor": ["all:\\"electrotactile\\""],
                    "evaluation": ["all:\\"haptic\\" AND all:\\"user study\\""]
                },
                {
                    "angle": "LLM semantic parsing",
                    "broad": ["all:\\"LLM\\" OR all:\\"semantic parsing\\""],
                    "method": ["all:\\"transformer\\" AND all:\\"haptic\\""],
                    "competitor": ["all:\\"rule-based\\""],
                    "evaluation": ["all:\\"accuracy\\" AND all:\\"benchmark\\""]
                }
            ]
        }"""
        result = _parse_json_response(raw)
        assert "keywords" in result
        assert "queries" in result
        assert "queries_by_tier" in result
        assert len(result["keywords"]) == 2
        assert len(result["queries"]) == 8  # 2 angles x 4 dimensions
        assert len(result["queries_by_tier"]["broad"]) == 2
        assert len(result["queries_by_tier"]["method"]) == 2
        assert len(result["queries_by_tier"]["competitor"]) == 2
        assert len(result["queries_by_tier"]["evaluation"]) == 2

    def test_v1_format_queries_dict(self):
        """Parse v1 format JSON with queries dict (broad/mid/precise)."""
        raw = """{
            "keywords": ["machine learning", "neural network"],
            "queries": {
                "broad": ["all:deep learning", "all:neural"],
                "mid": ["all:CNN AND all:vision"],
                "precise": ["all:ResNet AND all:ImageNet"]
            }
        }"""
        result = _parse_json_response(raw)
        assert result["keywords"] == ["machine learning", "neural network"]
        assert len(result["queries"]) == 4
        assert result["queries_by_tier"]["broad"] == ["all:deep learning", "all:neural"]
        assert result["queries_by_tier"]["method"] == ["all:CNN AND all:vision"]
        assert result["queries_by_tier"]["competitor"] == ["all:ResNet AND all:ImageNet"]
        assert result["queries_by_tier"]["evaluation"] == []

    def test_v1_format_queries_list(self):
        """Parse v1 format where queries is a flat list instead of dict."""
        raw = """{
            "keywords": ["deep learning"],
            "queries": ["all:transformer", "all:attention"]
        }"""
        result = _parse_json_response(raw)
        assert result["queries"] == ["all:transformer", "all:attention"]
        assert result["queries_by_tier"]["broad"] == ["all:transformer", "all:attention"]

    def test_json_with_markdown_code_block(self):
        """Parse JSON wrapped in ```json ... ``` code block."""
        raw = """```json
        {
            "keywords": ["haptic"],
            "angles": [
                {
                    "angle": "test",
                    "broad": ["q1"],
                    "method": ["q2"],
                    "competitor": ["q3"],
                    "evaluation": ["q4"]
                }
            ]
        }
        ```"""
        result = _parse_json_response(raw)
        assert len(result["queries"]) == 4

    def test_invalid_json_falls_back(self):
        """Invalid JSON should trigger fallback regex extraction."""
        raw = "This is not JSON at all. But it has \"machine learning\" and \"deep learning\" phrases."
        result = _parse_json_response(raw)
        # Fallback extracts quoted tokens as keywords
        assert isinstance(result["keywords"], list)
        assert isinstance(result["queries"], list)
        # At minimum the fallback provides something
        assert len(result["queries"]) > 0

    def test_empty_string_falls_back(self):
        """Empty string should trigger fallback with default query."""
        result = _parse_json_response("")
        assert result["queries"] == ["machine learning"]
        assert result["keywords"] == []

    def test_json_with_extra_text(self):
        """JSON with extra text before/after triggers fallback (not parsed)."""
        raw = """Here is the result:
        {
            "keywords": ["test"],
            "angles": [
                {
                    "angle": "test angle",
                    "broad": ["q1"],
                    "method": [],
                    "competitor": [],
                    "evaluation": []
                }
            ]
        }
        Hope this helps!"""
        result = _parse_json_response(raw)
        # When JSON is embedded in non-JSON text, json.loads fails
        # and the function falls back to regex extraction.
        # The result should still have the expected structure from fallback.
        assert "keywords" in result
        assert "queries" in result
        assert "queries_by_tier" in result


# ═══════════════════════════════════════════════════════════════════
# _fallback_extract
# ═══════════════════════════════════════════════════════════════════

class TestFallbackExtract:
    """Test _fallback_extract regex-based fallback parsing."""

    def test_extracts_quoted_phrases_as_queries(self):
        """Multi-word quoted phrases become queries."""
        raw = 'The model uses "deep reinforcement learning" and "graph neural network" for prediction.'
        result = _fallback_extract(raw)
        assert len(result["queries"]) >= 1
        # Should find at least one multi-word quoted phrase
        any_multi_word = any(" " in q for q in result["queries"])
        assert any_multi_word

    def test_extracts_quoted_phrases_as_keywords(self):
        """Quoted tokens up to 5 words become keywords."""
        raw = '"transformer architecture" "attention mechanism" "BERT" "GPT"'
        result = _fallback_extract(raw)
        assert len(result["keywords"]) > 0

    def test_no_quoted_phrases_uses_fallback_query(self):
        """When no quoted phrases found, fallback to 'machine learning'."""
        raw = "This text has no quoted strings."
        result = _fallback_extract(raw)
        assert result["queries"] == ["machine learning"]
        assert result["keywords"] == []

    def test_short_quoted_tokens_become_keywords(self):
        """Single-word quoted tokens become keywords."""
        raw = '"robot" "sensor" "actuator" "feedback" "control"'
        result = _fallback_extract(raw)
        assert len(result["keywords"]) > 0
        assert "robot" in result["keywords"]

    def test_keywords_limited_to_eight(self):
        """Keyword extraction is capped at 8 items."""
        raw = '"a1" "a2" "a3" "a4" "a5" "a6" "a7" "a8" "a9" "a10" "a11" "a12"'
        result = _fallback_extract(raw)
        assert len(result["keywords"]) <= 8

    def test_queries_limited_to_five(self):
        """Query extraction is capped at 5 items."""
        multi_word = " ".join(
            f'"long phrase number {i}"' for i in range(1, 10)
        )
        result = _fallback_extract(multi_word)
        assert len(result["queries"]) <= 5

    def test_returns_expected_structure(self):
        """Verify the returned dict has all required keys."""
        raw = '"test phrase"'
        result = _fallback_extract(raw)
        assert set(result.keys()) == {"keywords", "queries", "queries_by_tier"}
        assert isinstance(result["keywords"], list)
        assert isinstance(result["queries"], list)
        assert isinstance(result["queries_by_tier"], dict)
        assert set(result["queries_by_tier"].keys()) == {
            "broad", "method", "competitor", "evaluation",
        }


# ═══════════════════════════════════════════════════════════════════
# _extract_keywords
# ═══════════════════════════════════════════════════════════════════

class TestExtractKeywords:
    """Test _extract_keywords with v1 and v2 data formats."""

    def test_v1_keywords_direct(self):
        """v1 format: keywords at top level."""
        data = {"keywords": ["haptic", "rendering", "vibrotactile"]}
        result = _extract_keywords(data)
        assert result == ["haptic", "rendering", "vibrotactile"]

    def test_v2_keywords_from_angles(self):
        """v2 format: keywords scattered across angles."""
        data = {
            "keywords": [],
            "angles": [
                {"angle": "a1", "keywords": ["kw1", "kw2"]},
                {"angle": "a2", "keywords": ["kw3", "kw4"]},
            ],
        }
        result = _extract_keywords(data)
        assert set(result) == {"kw1", "kw2", "kw3", "kw4"}

    def test_v2_keywords_dedup(self):
        """Duplicate keywords across angles are deduplicated."""
        data = {
            "keywords": [],
            "angles": [
                {"angle": "a1", "keywords": ["kw1", "kw2"]},
                {"angle": "a2", "keywords": ["kw2", "kw3"]},
            ],
        }
        result = _extract_keywords(data)
        assert set(result) == {"kw1", "kw2", "kw3"}

    def test_no_keywords_anywhere(self):
        """No keywords in either location returns empty list."""
        data = {}
        result = _extract_keywords(data)
        assert result == []

    def test_keywords_not_a_list(self):
        """keywords key exists but is not a list."""
        data = {"keywords": "not a list"}
        result = _extract_keywords(data)
        assert result == []

    def test_angles_without_keywords_field(self):
        """Angles present but none have keywords field."""
        data = {
            "keywords": [],
            "angles": [
                {"angle": "a1", "broad": ["q1"]},
                {"angle": "a2", "method": ["q2"]},
            ],
        }
        result = _extract_keywords(data)
        assert result == []

    def test_angles_keywords_not_list(self):
        """Angle has keywords but they are not a list — iterates over string chars."""
        data = {
            "keywords": [],
            "angles": [
                {"angle": "a1", "keywords": "not_a_list"},
            ],
        }
        # When keywords is a string, iterating over it yields characters.
        # The function does not crash, but the result is individual chars.
        result = _extract_keywords(data)
        # Each character from "not_a_list" becomes a "keyword"
        assert len(result) == len(set("not_a_list"))


# ═══════════════════════════════════════════════════════════════════
# _extract_queries
# ═══════════════════════════════════════════════════════════════════

class TestExtractQueries:
    """Test _extract_queries with v1 and v2 data formats."""

    def test_v2_angle_dimension_matrix(self):
        """v2 format generates query matrix from angles x dimensions."""
        data = {
            "angles": [
                {
                    "broad": ["b1", "b2"],
                    "method": ["m1"],
                    "competitor": [],
                    "evaluation": ["e1", "e2"],
                },
                {
                    "broad": ["b3"],
                    "method": ["m2", "m3"],
                    "competitor": ["c1"],
                    "evaluation": [],
                },
            ],
        }
        by_tier, flat = _extract_queries(data)
        assert by_tier["broad"] == ["b1", "b2", "b3"]
        assert by_tier["method"] == ["m1", "m2", "m3"]
        assert by_tier["competitor"] == ["c1"]
        assert by_tier["evaluation"] == ["e1", "e2"]
        # flat order: broad → method → competitor → evaluation
        assert flat == ["b1", "b2", "b3", "m1", "m2", "m3", "c1", "e1", "e2"]

    def test_v2_empty_angles(self):
        """v2 format with empty angles array falls through to no queries."""
        data = {"angles": []}
        by_tier, flat = _extract_queries(data)
        assert flat == []
        assert by_tier["broad"] == []
        assert by_tier["method"] == []
        assert by_tier["competitor"] == []
        assert by_tier["evaluation"] == []

    def test_v1_queries_dict_broad_mid_precise(self):
        """v1 format: queries dict with broad/mid/precise keys."""
        data = {
            "queries": {
                "broad": ["b1", "b2"],
                "mid": ["m1"],
                "precise": ["p1", "p2", "p3"],
            },
        }
        by_tier, flat = _extract_queries(data)
        assert by_tier["broad"] == ["b1", "b2"]
        assert by_tier["method"] == ["m1"]
        assert by_tier["competitor"] == ["p1", "p2", "p3"]
        assert by_tier["evaluation"] == []
        assert flat == ["b1", "b2", "m1", "p1", "p2", "p3"]

    def test_v1_queries_as_list(self):
        """v1 format: queries is a flat list (legacy)."""
        data = {"queries": ["q1", "q2", "q3"]}
        by_tier, flat = _extract_queries(data)
        assert by_tier["broad"] == ["q1", "q2", "q3"]
        assert flat == ["q1", "q2", "q3"]

    def test_v1_queries_partial_keys(self):
        """v1 format with only some keys present."""
        data = {
            "queries": {
                "broad": ["b1"],
            },
        }
        by_tier, flat = _extract_queries(data)
        assert by_tier["broad"] == ["b1"]
        assert by_tier["method"] == []
        assert by_tier["competitor"] == []
        assert flat == ["b1"]

    def test_no_queries_no_angles(self):
        """Data with neither queries nor angles."""
        data = {"keywords": ["test"]}
        by_tier, flat = _extract_queries(data)
        assert flat == []

    def test_v2_angle_missing_dimensions(self):
        """Angle missing some dimension keys handles gracefully."""
        data = {
            "angles": [
                {
                    "broad": ["b1"],
                    # method, competitor, evaluation missing
                },
            ],
        }
        by_tier, flat = _extract_queries(data)
        assert flat == ["b1"]

    def test_v2_dimension_not_list(self):
        """Dimension value is not a list - should skip gracefully."""
        data = {
            "angles": [
                {
                    "broad": "not_a_list",
                    "method": ["m1"],
                    "competitor": [],
                    "evaluation": [],
                },
            ],
        }
        by_tier, flat = _extract_queries(data)
        # "not_a_list" should be skipped, only m1 picked up
        assert flat == ["m1"]

    def test_output_is_tuple_of_dict_and_list(self):
        """Verify return type is (dict, list)."""
        data = {"angles": []}
        result = _extract_queries(data)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], dict)
        assert isinstance(result[1], list)
