"""
modules/thinker/voice.py
VoiceEngine — 语音输入/输出引擎（委托 core.voice）。
"""

from __future__ import annotations


class VoiceEngine:
    """语音引擎。委托 core.voice 执行实际的 STT/TTS。"""

    def __init__(self, core):
        self._voice = core.voice

    async def transcribe(self, audio_bytes: bytes, format: str = "webm") -> str:
        return await self._voice.transcribe(audio_bytes, format)

    async def transcribe_stream(self, audio_bytes: bytes, format: str = "webm"):
        async for chunk in self._voice.transcribe_stream(audio_bytes, format):
            yield chunk

    async def synthesize(self, text: str) -> bytes:
        return await self._voice.synthesize(text)
