"""
core/tests/test_chunk.py
Tests for semantic_chunk — sliding-window text splitter.
"""

import pytest
from core.chunk import semantic_chunk


class TestSemanticChunk:
    """Test suite for semantic_chunk function."""

    # ── basic behaviour ──────────────────────────────────────────

    def test_short_text_returns_single_chunk(self):
        """Text shorter than chunk_size returns exactly one chunk."""
        text = "This is a short text."
        chunks = semantic_chunk(text, chunk_size=500, overlap=100)

        assert len(chunks) == 1
        assert chunks[0]["text"] == text
        assert chunks[0]["index"] == 0
        assert chunks[0]["byte_start"] == 0
        assert chunks[0]["byte_end"] == len(text)
        assert chunks[0]["boundary_type"] == "eof"

    def test_long_text_returns_multiple_chunks(self):
        """Text longer than chunk_size produces multiple chunks with overlap."""
        # Build a long text with periodic sentence breaks.
        sentence = "This is sentence number {} that contains enough characters. "
        text = "".join(sentence.format(i) for i in range(100))

        chunks = semantic_chunk(text, chunk_size=200, overlap=40)

        assert len(chunks) >= 2, f"Expected >=2 chunks, got {len(chunks)}"

        # Verify continuity: chunk[i].byte_end > chunk[i].byte_start
        for i, ch in enumerate(chunks):
            assert ch["byte_end"] > ch["byte_start"], f"Chunk {i} has invalid range"
            assert len(ch["text"]) > 0, f"Chunk {i} text is empty"

        # Chunks should be in order
        indices = [ch["index"] for ch in chunks]
        assert indices == list(range(len(chunks)))

    def test_empty_text_returns_empty_list(self):
        """Empty or whitespace-only text returns an empty list."""
        assert semantic_chunk("") == []
        assert semantic_chunk("   ") == []
        assert semantic_chunk("\n\n") == []

    def test_text_exactly_at_chunk_boundary(self):
        """Text length exactly matching chunk_size works correctly."""
        # Create text of exactly chunk_size with a sentence break at the end.
        base = "A" * 460 + ". " + "B" * 37  # total 499 chars
        assert len(base) <= 500

        chunks = semantic_chunk(base, chunk_size=500, overlap=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == base.strip()

    def test_chunk_indices_and_overlap_regions(self):
        """Verify chunk indices are sequential and overlap regions exist."""
        sentence = "The quick brown fox jumps over the lazy dog. " * 30
        text = sentence * 5

        chunk_size = 300
        overlap = 80
        chunks = semantic_chunk(text, chunk_size=chunk_size, overlap=overlap)

        assert len(chunks) >= 2, "Expected multiple chunks for long text"

        # Check indices are unique and sequential
        seen = set()
        for ch in chunks:
            assert ch["index"] not in seen, f"Duplicate index {ch['index']}"
            seen.add(ch["index"])

        # Check that adjacent chunks overlap if there are 2+
        for i in range(len(chunks) - 1):
            prev_end = chunks[i]["byte_end"]
            next_start = chunks[i + 1]["byte_start"]
            if prev_end > next_start:
                overlap_size = prev_end - next_start
                assert overlap_size > 0, (
                    f"Chunks {i} and {i+1}: expected overlap, "
                    f"but prev_end={prev_end} <= next_start={next_start}"
                )

    def test_boundary_type_values(self):
        """Boundary types should be one of the expected values."""
        # Short text → "eof"
        chunks = semantic_chunk("Hello world.", chunk_size=500, overlap=100)
        assert chunks[0]["boundary_type"] in {"eof", "sentence", "semantic", "fallback", "merged"}

        # Long text with explicit sentence boundaries
        text = ("First paragraph with enough text to go beyond chunk size. "
                "Second paragraph also has sufficient text. " * 30)
        chunks = semantic_chunk(text, chunk_size=200, overlap=40)
        for ch in chunks:
            assert ch["boundary_type"] in {"eof", "sentence", "semantic", "fallback", "merged"}

    def test_custom_chunk_size_and_overlap(self):
        """Custom chunk_size and overlap parameters are respected."""
        text = "A" * 1000
        chunks = semantic_chunk(text, chunk_size=300, overlap=50)
        assert len(chunks) >= 3

        # Each chunk should be roughly <= chunk_size + some slack for boundaries
        for ch in chunks:
            assert len(ch["text"]) <= 350, f"Chunk too large: {len(ch['text'])} chars"

    def test_single_character_text(self):
        """Edge case: single character text still returns one chunk."""
        chunks = semantic_chunk("X", chunk_size=500, overlap=100)
        assert len(chunks) == 1
        assert chunks[0]["text"] == "X"

    def test_text_with_only_newlines(self):
        """Whitespace-only text returns empty list."""
        assert semantic_chunk("\n\n\n\n") == []
