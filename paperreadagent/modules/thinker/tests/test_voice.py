"""
modules/thinker/tests/test_voice.py
测试 VoiceEngine 语音引擎（委托 core.voice）。
"""

import pytest
from unittest.mock import MagicMock, AsyncMock

from modules.thinker.voice import VoiceEngine


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
