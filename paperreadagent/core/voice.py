"""
core/voice.py
CoreVoice — 统一语音入口。封装 OpenAI 兼容 Audio API (STT/TTS)。
所有模块的语音需求均通过此接口。
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .decorators import evolving

logger = logging.getLogger(__name__)

# ── 默认值 ────────────────────────────────────────────────────

_DEFAULT_STT_MODEL = "whisper-large-v3"
_DEFAULT_TTS_MODEL = "gemini-2.5-pro-preview-tts"
_DEFAULT_TTS_VOICE = "achird"
_DEFAULT_TTS_SPEED = 1.0
_DEFAULT_STT_LANGUAGE = ""
_DEFAULT_AUDIO_FORMAT = "webm"


class VoiceError(Exception):
    """语音 API 调用失败。"""


class CoreVoice:
    """核心语音引擎。STT (Whisper) + TTS (Gemini TTS) 通过 OpenAI 兼容 API。"""

    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str,
        stt_model: str = _DEFAULT_STT_MODEL,
        tts_model: str = _DEFAULT_TTS_MODEL,
        tts_voice: str = _DEFAULT_TTS_VOICE,
        tts_speed: float = _DEFAULT_TTS_SPEED,
        stt_language: str = _DEFAULT_STT_LANGUAGE,
        timeout: float = 60.0,
    ):
        self.api_key = api_key
        self.api_base_url = api_base_url
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice
        self.tts_speed = tts_speed
        self.stt_language = stt_language
        self.timeout = timeout
        self._client = None

    def _get_client(self):
        """延迟初始化 OpenAI client（避免空凭据时无意义建连）。"""
        if self._client is None:
            import openai
            self._client = openai.OpenAI(
                api_key=self.api_key, base_url=self.api_base_url, timeout=self.timeout,
            )
        return self._client

    @classmethod
    def from_config(cls, voice_cfg: dict) -> "CoreVoice | None":
        if not voice_cfg.get("api_key") or not voice_cfg.get("api_base_url"):
            logger.warning("[CoreVoice] 语音配置缺少 api_key 或 api_base_url，返回 None（语音服务不可用）")
            return None
        return cls(
            api_key=voice_cfg.get("api_key", ""),
            api_base_url=voice_cfg.get("api_base_url", ""),
            stt_model=voice_cfg.get("stt_model", _DEFAULT_STT_MODEL),
            tts_model=voice_cfg.get("tts_model", _DEFAULT_TTS_MODEL),
            tts_voice=voice_cfg.get("tts_voice", _DEFAULT_TTS_VOICE),
            tts_speed=voice_cfg.get("tts_speed", _DEFAULT_TTS_SPEED),
            stt_language=voice_cfg.get("stt_language", _DEFAULT_STT_LANGUAGE),
        )

    # ── STT ────────────────────────────────────────────────────

    @evolving
    async def transcribe(self, audio_bytes: bytes, format: str = _DEFAULT_AUDIO_FORMAT) -> str:
        return await self._transcribe_impl(audio_bytes, format, mode="transcribe")

    @evolving
    async def translate(self, audio_bytes: bytes, format: str = _DEFAULT_AUDIO_FORMAT) -> str:
        return await self._transcribe_impl(audio_bytes, format, mode="translate")

    async def _transcribe_impl(self, audio_bytes: bytes, format: str, *, mode: str) -> str:
        loop = asyncio.get_running_loop()

        def _call():
            client = self._get_client()
            create_fn = (
                client.audio.transcriptions.create
                if mode == "transcribe"
                else client.audio.translations.create
            )
            kwargs = {
                "model": self.stt_model,
                "file": (f"audio.{format}", audio_bytes, f"audio/{format}"),
                "response_format": "text",
            }
            if mode == "transcribe" and self.stt_language:
                kwargs["language"] = self.stt_language
            try:
                resp = create_fn(**kwargs)
            except Exception as exc:
                logger.error(f"[CoreVoice] {mode} API 调用失败: {exc}")
                raise VoiceError(f"语音转写失败: {exc}") from exc
            text = resp.strip() if isinstance(resp, str) else str(resp)
            return text or ""

        return await loop.run_in_executor(None, _call)

    @evolving
    async def transcribe_stream(self, audio_bytes: bytes, format: str = _DEFAULT_AUDIO_FORMAT):
        """
        SSE 兼容流式接口。底层 API 不支持真流式，先获取全文再按句子逐段 yield。
        """
        full_text = await self.transcribe(audio_bytes, format)
        if not full_text:
            return

        for sentence in _split_sentences(full_text):
            if sentence:
                yield sentence

    # ── TTS ────────────────────────────────────────────────────

    @evolving
    async def synthesize(self, text: str) -> bytes:
        loop = asyncio.get_running_loop()

        def _call():
            try:
                resp = self._get_client().audio.speech.create(
                    model=self.tts_model,
                    input=text,
                    voice=self.tts_voice,
                    speed=self.tts_speed,
                    response_format="mp3",
                )
                return resp.content
            except Exception as exc:
                logger.error(f"[CoreVoice] TTS API 调用失败: {exc}")
                raise VoiceError(f"语音合成失败: {exc}") from exc

        return await loop.run_in_executor(None, _call)


def _split_sentences(text: str):
    """按标点切分句子，保证每次 yield 一个完整句。"""
    # 在标点符号后添加分隔标记，然后 split
    parts = re.split(r"(?<=[。！？.!?])\s*", text)
    for part in parts:
        stripped = part.strip()
        if stripped:
            yield stripped
