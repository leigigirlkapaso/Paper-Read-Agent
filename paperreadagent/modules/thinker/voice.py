"""
modules/thinker/voice.py
VoiceEngine — 语音输入/输出引擎（委托 core.voice）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level cache: WebM headers for chunk repair survive across requests
_webm_headers: dict[int, bytes] = {}


def _normalize_format(fmt: str) -> str:
    """Map 'opus' → 'webm'. 'webm' → 'wav' (api.gpt.ge rejects webm container).
    Other formats (mp4, ogg, mp3, wav, flac, m4a) pass through unchanged.
    """
    if fmt in ("opus", "webm"):
        return "wav"
    return fmt


def _webm_to_wav(webm_bytes: bytes) -> bytes:
    """Convert WebM/Opus container to WAV using pyav.
    api.gpt.ge rejects video/webm but accepts wav. No quality loss (PCM).
    Returns original bytes if conversion fails.
    """
    try:
        import av
        import io

        input_io = io.BytesIO(webm_bytes)
        output_io = io.BytesIO()
        container = av.open(input_io, format="webm")
        audio_stream = next((s for s in container.streams if s.type == "audio"), None)
        if audio_stream is None:
            return webm_bytes

        out_container = av.open(output_io, mode="w", format="wav")
        out_stream = out_container.add_stream("pcm_s16le", rate=16000)
        out_stream.channels = 1
        resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=16000,
        )

        for frame in container.decode(audio=0):
            for resampled in resampler.resample(frame):
                out_container.mux(out_stream.encode(resampled))

        out_container.close()
        container.close()
        return output_io.getvalue()
    except Exception:
        logger.warning("[Voice] WebM→WAV conversion failed, sending raw bytes", exc_info=True)
        return webm_bytes


class VoiceEngine:
    """语音引擎。委托 core.voice 执行实际的 STT/TTS。"""

    def __init__(self, core):
        self._voice = core.voice

    def _check_available(self) -> bool:
        if self._voice is None:
            logger.warning("[VoiceEngine] 语音服务未配置（core.voice 为 None），跳过")
            return False
        return True

    async def transcribe(self, audio_bytes: bytes, format: str = "webm") -> str:
        if not self._check_available():
            return ""
        format = _normalize_format(format)
        return await self._voice.transcribe(audio_bytes, format)

    async def transcribe_stream(self, audio_bytes: bytes, format: str = "webm"):
        if not self._check_available():
            return
        format = _normalize_format(format)
        async for chunk in self._voice.transcribe_stream(audio_bytes, format):
            yield chunk

    async def synthesize(self, text: str) -> bytes:
        if not self._check_available():
            return b""
        return await self._voice.synthesize(text)

    def _extract_webm_header(self, data: bytes) -> bytes:
        """Extract WebM header (everything before the first Cluster element)."""
        # WebM Cluster ID is 0x1F43B675
        cluster_marker = bytes([0x1F, 0x43, 0xB6, 0x75])
        idx = data.find(cluster_marker, 8)  # skip EBML header (min 8 bytes)
        if idx > 0:
            return data[:idx]
        return b""  # fallback: can't find cluster boundary

    # ── Chunked STT (for 15+ min presentations) ────────────────

    async def transcribe_chunk(
        self, audio_bytes: bytes, rehearsal_id: int = 0, format: str = "webm"
    ) -> str:
        """
        Transcribe a single audio chunk.

        For WebM/Opus: the first chunk contains the WebM headers; subsequent
        chunks from MediaRecorder timeslice are raw clusters without headers.
        We cache the first chunk's header and prepend it to later chunks so
        Whisper can parse them.
        """
        if not audio_bytes:
            return ""

        # Remember original container for header repair before normalization
        original_fmt = format

        # Normalize: opus/webm → wav (api.gpt.ge rejects webm, but accepts wav)
        format = _normalize_format(format)

        # For WebM container chunks: repair headers, then convert to WAV
        if original_fmt in ("webm", "opus") and rehearsal_id is not None:
            # Check if this chunk starts with a WebM EBML header (0x1A45DFA3)
            has_header = (
                len(audio_bytes) >= 4
                and audio_bytes[0:4] == bytes([0x1A, 0x45, 0xDF, 0xA3])
            )
            if has_header:
                # Cache the header for later chunks
                header = self._extract_webm_header(audio_bytes)
                if header:
                    _webm_headers[rehearsal_id] = header
            elif rehearsal_id in _webm_headers:
                # Later chunk without header — prepend cached header
                audio_bytes = _webm_headers[rehearsal_id] + audio_bytes

        # Convert WebM container → WAV (api.gpt.ge rejects video/webm)
        if original_fmt in ("webm", "opus"):
            audio_bytes = _webm_to_wav(audio_bytes)

        return await self._voice.transcribe(audio_bytes, format)

    def save_full_audio(
        self,
        rehearsal_id: int,
        chunks: list[bytes],
        output_dir: str,
    ) -> str:
        """
        Concatenate all audio chunks into a complete Opus file.

        Args:
            rehearsal_id: Rehearsal ID for file naming
            chunks: Ordered list of audio chunk bytes
            output_dir: Output directory path

        Returns:
            Full path to the saved audio file
        """
        from pathlib import Path

        dir_path = Path(output_dir)
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / f"rehearsal_{rehearsal_id}_full.opus"

        with open(file_path, "wb") as f:
            for chunk in chunks:
                f.write(chunk)

        logger.info(
            f"[Voice] Full audio saved: {file_path} "
            f"({len(chunks)} chunks, {file_path.stat().st_size} bytes)"
        )
        return str(file_path)
