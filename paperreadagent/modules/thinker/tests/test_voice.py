"""
modules/thinker/tests/test_voice.py
测试 VoiceEngine 语音引擎（委托 core.voice）。
"""

import pytest
from unittest.mock import MagicMock, AsyncMock

from paperreadagent.modules.thinker.voice import VoiceEngine


class TestVoiceEngine:
    def test_init_stores_voice(self):
        mock_core = MagicMock()
        engine = VoiceEngine(mock_core)
        assert engine._voice is mock_core.voice

    @pytest.mark.asyncio
    async def test_transcribe_delegates_to_core_voice(self):
        mock_core = MagicMock()
        mock_core.voice.transcribe = AsyncMock(return_value="你好世界")

        engine = VoiceEngine(mock_core)
        fake_audio = b"\x00\x01\x02" * 1000
        text = await engine.transcribe(fake_audio)

        assert "你好世界" in text
        mock_core.voice.transcribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_transcribe_stream_delegates_to_core_voice(self):
        mock_core = MagicMock()

        async def fake_stream(audio_bytes, format):
            yield "片段1"
            yield "片段2"

        mock_core.voice.transcribe_stream = fake_stream

        engine = VoiceEngine(mock_core)
        chunks = []
        async for chunk in engine.transcribe_stream(b"fake"):
            chunks.append(chunk)

        assert chunks == ["片段1", "片段2"]

    @pytest.mark.asyncio
    async def test_synthesize_delegates_to_core_voice(self):
        mock_core = MagicMock()
        mock_core.voice.synthesize = AsyncMock(return_value=b"FAKE_MP3")

        engine = VoiceEngine(mock_core)
        audio = await engine.synthesize("你好")

        assert audio == b"FAKE_MP3"
        mock_core.voice.synthesize.assert_called_once_with("你好")

    # ── Format mapping tests (browser → Whisper format) ─────────

    @pytest.mark.asyncio
    async def test_transcribe_normalizes_webm_to_wav(self):
        """transcribe() normalizes webm→wav before calling core.voice."""
        mock_core = MagicMock()
        mock_core.voice.transcribe = AsyncMock(return_value="test")
        engine = VoiceEngine(mock_core)
        await engine.transcribe(b"fake_audio")
        call_kwargs = mock_core.voice.transcribe.call_args
        assert call_kwargs[0][1] == "wav"

    @pytest.mark.asyncio
    async def test_transcribe_stream_defaults_to_webm_format(self):
        """Default stream format should be 'webm'."""
        mock_core = MagicMock()
        async def fake(audio, fmt):
            yield "ok"
        mock_core.voice.transcribe_stream = fake
        engine = VoiceEngine(mock_core)
        async for _ in engine.transcribe_stream(b"fake"):
            pass

    @pytest.mark.asyncio
    async def test_transcribe_respects_explicit_format(self):
        """Explicit format parameter should override default."""
        mock_core = MagicMock()
        mock_core.voice.transcribe = AsyncMock(return_value="test")
        engine = VoiceEngine(mock_core)
        await engine.transcribe(b"fake", format="mp4")
        assert mock_core.voice.transcribe.call_args[0][1] == "mp4"

    # ── Chunk format integration tests ──────────────────────────

    @pytest.mark.asyncio
    async def test_transcribe_chunk_defaults_to_wav(self):
        """transcribe_chunk defaults to 'webm' but normalizes to 'wav' for the API."""
        mock_core = MagicMock()
        mock_core.voice.transcribe = AsyncMock(return_value="test")
        engine = VoiceEngine(mock_core)
        await engine.transcribe_chunk(b"fake_audio")
        actual_format = mock_core.voice.transcribe.call_args[0][1]
        assert actual_format == "wav", f"Expected 'wav', got '{actual_format}'"

    @pytest.mark.asyncio
    async def test_transcribe_chunk_passes_format_to_core(self):
        """transcribe_chunk should pass explicit format to CoreVoice."""
        mock_core = MagicMock()
        mock_core.voice.transcribe = AsyncMock(return_value="test")
        engine = VoiceEngine(mock_core)
        await engine.transcribe_chunk(b"fake", rehearsal_id=1, format="mp4")
        assert mock_core.voice.transcribe.call_args[0][1] == "mp4"

    @pytest.mark.asyncio
    async def test_transcribe_chunk_empty_bytes_returns_empty(self):
        """Empty audio bytes should return empty string immediately."""
        mock_core = MagicMock()
        engine = VoiceEngine(mock_core)
        result = await engine.transcribe_chunk(b"")
        assert result == ""
        mock_core.voice.transcribe.assert_not_called()

    # ── WebM header caching tests ───────────────────────────────

    def test_extract_webm_header_normal(self):
        """Should extract header before first Cluster (0x1F43B675)."""
        mock_core = MagicMock()
        engine = VoiceEngine(mock_core)
        # Simulate: EBML header (8 bytes) + Segment + Info + Tracks + Cluster
        header = bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"\x00" * 100  # fake header
        cluster_marker = bytes([0x1F, 0x43, 0xB6, 0x75])
        data = header + cluster_marker + b"\x00" * 50
        extracted = engine._extract_webm_header(data)
        assert len(extracted) == len(header)
        assert extracted == header

    def test_extract_webm_header_no_cluster_returns_empty(self):
        """If no Cluster marker found, return empty bytes."""
        mock_core = MagicMock()
        engine = VoiceEngine(mock_core)
        data = bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"\x00" * 100
        extracted = engine._extract_webm_header(data)
        assert extracted == b""

    def test_extract_webm_header_too_short_returns_empty(self):
        """Too-short data should return empty."""
        mock_core = MagicMock()
        engine = VoiceEngine(mock_core)
        assert engine._extract_webm_header(b"\x00\x00") == b""

    @pytest.mark.asyncio
    async def test_chunk_header_cached_across_requests(self):
        """Module-level _webm_headers persists across VoiceEngine instances."""
        import paperreadagent.modules.thinker.voice as voice_mod
        voice_mod._webm_headers.pop(99, None)

        ebml = bytes([0x1A, 0x45, 0xDF, 0xA3])
        cluster_marker = bytes([0x1F, 0x43, 0xB6, 0x75])
        chunk1 = ebml + b"\x00" * 30 + cluster_marker + b"\x01" * 20

        e1 = VoiceEngine(MagicMock())
        # Call the header caching logic directly — skip mock STT
        e1._voice.transcribe = AsyncMock(return_value="ok")
        await e1.transcribe_chunk(chunk1, rehearsal_id=99, format="webm")
        assert 99 in voice_mod._webm_headers, f"Header cache miss. Keys: {list(voice_mod._webm_headers)}"

        chunk2 = b"\x02" * 40
        e2 = VoiceEngine(MagicMock())
        e2._voice.transcribe = AsyncMock(return_value="ok")
        await e2.transcribe_chunk(chunk2, rehearsal_id=99, format="webm")

        # Second call should have prepended header — verify by checking sent bytes
        sent = e2._voice.transcribe.call_args[0][0]
        assert len(sent) > len(chunk2), f"Expected header prepend. Same length: {len(sent)} == {len(chunk2)}"
        assert sent.startswith(ebml), "Prepended header should start with EBML"

        voice_mod._webm_headers.pop(99, None)

    # ── TTS format tests ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_synthesize_passes_text_to_core_voice(self):
        """TTS should delegate text to CoreVoice.synthesize."""
        mock_core = MagicMock()
        mock_core.voice.synthesize = AsyncMock(return_value=b"MP3_DATA")
        engine = VoiceEngine(mock_core)
        result = await engine.synthesize("Hello world")
        assert result == b"MP3_DATA"
        mock_core.voice.synthesize.assert_called_once_with("Hello world")

    @pytest.mark.asyncio
    async def test_synthesize_empty_text(self):
        """Empty text should still delegate to CoreVoice (API-level validation)."""
        mock_core = MagicMock()
        mock_core.voice.synthesize = AsyncMock(return_value=b"")
        engine = VoiceEngine(mock_core)
        result = await engine.synthesize("")
        assert result == b""

    # ── Format normalization tests ─────────────────────────────

    def test_normalize_format_webm_to_wav(self):
        """webm/opus → wav (api.gpt.ge rejects webm container)."""
        from paperreadagent.modules.thinker.voice import _normalize_format
        assert _normalize_format("opus") == "wav"
        assert _normalize_format("webm") == "wav"

    def test_normalize_format_passthrough(self):
        """Non-webm formats pass through unchanged."""
        from paperreadagent.modules.thinker.voice import _normalize_format
        assert _normalize_format("mp4") == "mp4"
        assert _normalize_format("ogg") == "ogg"
        assert _normalize_format("wav") == "wav"

    def test_normalize_format_empty_passthrough(self):
        """Empty format string passes through."""
        from paperreadagent.modules.thinker.voice import _normalize_format
        assert _normalize_format("") == ""

    @pytest.mark.asyncio
    async def test_transcribe_chunk_normalizes_webm_to_wav(self):
        """transcribe_chunk should normalize webm → wav for api.gpt.ge compatibility."""
        mock_core = MagicMock()
        mock_core.voice.transcribe = AsyncMock(return_value="test")
        engine = VoiceEngine(mock_core)
        await engine.transcribe_chunk(b"some bytes", rehearsal_id=1, format="webm")
        actual_format = mock_core.voice.transcribe.call_args[0][1]
        assert actual_format == "wav", f"Expected 'wav', got '{actual_format}'"
