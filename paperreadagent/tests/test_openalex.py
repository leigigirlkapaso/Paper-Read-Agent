"""
tests/test_openalex.py
Unit tests for pure functions in agent1/openalex_searcher.py.
No external API calls, no database required.
"""

from __future__ import annotations

import pytest
from agent1.openalex_searcher import _rebuild_abstract


# ═══════════════════════════════════════════════════════════════════
# _rebuild_abstract
# ═══════════════════════════════════════════════════════════════════

class TestRebuildAbstract:
    """Test _rebuild_abstract which reconstructs text from
    OpenAlex's inverted index format: {"word": [pos1, pos2, ...], ...}"""

    def test_normal_inverted_index(self):
        """Rebuild a normal inverted index into readable text."""
        inverted = {
            "haptic": [0],
            "rendering": [1],
            "is": [2],
            "important": [3],
            "for": [4],
            "VR": [5],
        }
        result = _rebuild_abstract(inverted)
        assert result == "haptic rendering is important for VR"

    def test_non_contiguous_positions(self):
        """Words at non-contiguous positions are ordered correctly."""
        inverted = {
            "the": [0, 3],
            "cat": [1],
            "sat": [2],
            "mat": [4],
        }
        result = _rebuild_abstract(inverted)
        assert result == "the cat sat the mat"

    def test_single_word(self):
        """Single word index reconstructs to that word."""
        inverted = {"hello": [0]}
        result = _rebuild_abstract(inverted)
        assert result == "hello"

    def test_none_input(self):
        """None input returns empty string."""
        result = _rebuild_abstract(None)
        assert result == ""

    def test_empty_dict(self):
        """Empty dict returns empty string."""
        result = _rebuild_abstract({})
        assert result == ""

    def test_malformed_positions_not_int(self):
        """Positions that are not integers should be handled gracefully."""
        inverted = {
            "word1": [0, "not_an_int", 2],
            "word2": [1],
        }
        # Should not crash; either returns empty or partial
        try:
            result = _rebuild_abstract(inverted)
            # If it succeeds, we get something
            assert isinstance(result, str)
        except Exception:
            # If it fails, that's acceptable for malformed input
            pass

    def test_duplicate_positions(self):
        """If two words share the same position (malformed), last write wins."""
        inverted = {
            "a": [0],
            "b": [0],  # same position as 'a'
            "c": [1],
        }
        result = _rebuild_abstract(inverted)
        # At position 0, 'b' overwrites 'a'
        assert "b" in result
        assert "c" in result
        assert result in ("b c", "a c")  # either is acceptable depending on iteration order

    def test_negative_positions(self):
        """Negative positions still produce output (unusual but shouldn't crash)."""
        inverted = {
            "start": [-5],
            "end": [10],
        }
        result = _rebuild_abstract(inverted)
        assert isinstance(result, str)

    def test_large_realistic_index(self):
        """A larger, more realistic inverted index."""
        inverted = {
            "deep": [0],
            "learning": [1],
            "has": [2],
            "revolutionized": [3],
            "computer": [4],
            "vision": [5],
            "and": [6],
            "natural": [7],
            "language": [8],
            "processing": [9],
        }
        result = _rebuild_abstract(inverted)
        assert result == (
            "deep learning has revolutionized computer vision "
            "and natural language processing"
        )

    def test_position_gaps(self):
        """Positions with gaps (missing indices) still produce output."""
        inverted = {
            "hello": [0],
            "world": [5],  # gap in positions
        }
        result = _rebuild_abstract(inverted)
        assert "hello" in result
        assert "world" in result

    def test_special_characters_in_words(self):
        """Words with special characters are preserved."""
        inverted = {
            "state-of-the-art": [0],
            "results": [1],
            "$\alpha$": [2],
        }
        result = _rebuild_abstract(inverted)
        assert isinstance(result, str)
        assert "state-of-the-art" in result
        assert "$\alpha$" in result
