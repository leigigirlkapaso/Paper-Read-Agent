"""
modules/thinker/routes.py
Thinker 模块 API 路由：对话管理、SSE 流式聊天。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging

from fastapi import APIRouter, Request, Form, UploadFile
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse, RedirectResponse

from .chat import ChatEngine
from .questions import QuestionGenerator
from .resolutions import ResolutionTracker
from .knowledge_linker import KnowledgeLinker
from .voice import VoiceEngine

router = APIRouter(prefix="", tags=["thinker"])


def _engine(request: Request) -> ChatEngine:
    return ChatEngine(request.app.state.core)


def _thinker_cfg(request: Request) -> dict:
    return request.app.state.core.module_config("thinker")


def _voice_engine(request: Request) -> VoiceEngine:
    return VoiceEngine(request.app.state.core)


@router.get("/", response_class=HTMLResponse)
async def thinker_page(request: Request):
    """Thinker full page (three tabs: Chat | Rehearsal | Records)."""
    from pathlib import Path as _Path
    from jinja2 import Environment, FileSystemLoader
    from fastapi.templating import Jinja2Templates

    tpl_dir = _Path(__file__).parent / "templates"
    web_tpl_dir = _Path(__file__).parent.parent.parent / "web" / "templates"
    env = Environment(loader=FileSystemLoader([str(tpl_dir), str(web_tpl_dir)]))
    tpl = Jinja2Templates(env=env)
    return tpl.TemplateResponse("thinker_page.html", {"request": request})


@router.get("/api/conversations")
async def list_conversations(request: Request):
    engine = _engine(request)
    convs = await engine.list_conversations()
    return JSONResponse(convs)


@router.post("/api/conversations")
async def create_conversation(request: Request, mode: str = Form("chat")):
    engine = _engine(request)
    conv_id = await engine.create_conversation(mode=mode)
    return JSONResponse({"id": conv_id, "mode": mode})


@router.get("/api/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: int):
    engine = _engine(request)
    conv = await engine.get_conversation(conversation_id)
    if not conv:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(conv)


@router.get("/api/conversations/{conversation_id}/messages")
async def get_messages(request: Request, conversation_id: int):
    engine = _engine(request)
    msgs = await engine.get_messages(conversation_id)
    return JSONResponse(msgs)


@router.post("/api/chat")
async def chat_message(
    request: Request,
    conversation_id: int = Form(...),
    message: str = Form(...),
):
    engine = _engine(request)

    async def _sse():
        async for event in engine.chat_stream(conversation_id, message):
            yield event

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/conversations/{conversation_id}/mode")
async def set_mode(request: Request, conversation_id: int, mode: str = Form("chat")):
    engine = _engine(request)
    await engine.update_mode(conversation_id, mode)
    return JSONResponse({"ok": True})


@router.post("/api/conversations/{conversation_id}/intensity")
async def set_intensity(request: Request, conversation_id: int, intensity: str = Form("moderate")):
    engine = _engine(request)
    await engine.update_intensity(conversation_id, intensity)
    return JSONResponse({"ok": True})


@router.post("/api/conversations/{conversation_id}/pause")
async def pause_conversation(request: Request, conversation_id: int, minutes: int = Form(30)):
    engine = _engine(request)
    await engine.pause(conversation_id, minutes)
    return JSONResponse({"ok": True, "snooze_minutes": minutes})


@router.post("/api/conversations/{conversation_id}/resume")
async def resume_conversation(request: Request, conversation_id: int):
    engine = _engine(request)
    await engine.resume(conversation_id)
    return JSONResponse({"ok": True})


@router.post("/api/conversations/{conversation_id}/close")
async def close_conversation(request: Request, conversation_id: int):
    engine = _engine(request)
    summary_id, resolution_ids = await asyncio.gather(
        engine.generate_summary(conversation_id),
        engine.extract_resolutions(conversation_id),
    )
    await engine.close_conversation(conversation_id)
    return JSONResponse({
        "ok": True,
        "summary_note_id": summary_id,
        "resolution_ids": resolution_ids,
    })


@router.get("/api/questions/pending")
async def get_pending_question(request: Request, conversation_id: int):
    """前端轮询端点。有则返回问题 JSON，无则返回 null。"""
    gen = QuestionGenerator(request.app.state.core)
    q = await gen.get_pending_question(conversation_id)
    if q is None:
        return JSONResponse(None)
    return JSONResponse(dict(q))


@router.post("/api/questions/{question_id}/dismiss")
async def dismiss_question(request: Request, question_id: int):
    gen = QuestionGenerator(request.app.state.core)
    await gen.dismiss_question(question_id)
    return JSONResponse({"ok": True})


@router.post("/api/resolutions/{resolution_id}/fulfill")
async def fulfill_resolution(request: Request, resolution_id: int):
    tracker = ResolutionTracker(request.app.state.core)
    await tracker.mark_fulfilled(resolution_id)
    return JSONResponse({"ok": True})


@router.post("/api/resolutions/{resolution_id}/abandon")
async def abandon_resolution(request: Request, resolution_id: int, reflection: str = Form("")):
    tracker = ResolutionTracker(request.app.state.core)
    await tracker.mark_abandoned(resolution_id, reflection)
    return JSONResponse({"ok": True})


@router.get("/api/messages/{message_id}/related")
async def get_related_notes(request: Request, message_id: int):
    linker = KnowledgeLinker(request.app.state.core)
    results = await linker.link_message_to_knowledge(message_id)
    return JSONResponse(results)


@router.post("/api/voice/transcribe")
async def transcribe_audio(request: Request, file: UploadFile):
    """接收前端录音 WAV，返回转写文本。"""
    audio_bytes = await file.read()
    voice = _voice_engine(request)
    text = await voice.transcribe(audio_bytes)
    return JSONResponse({"text": text})


@router.post("/api/voice/transcribe/stream")
async def transcribe_audio_stream(request: Request, file: UploadFile):
    """流式转写 SSE 端点：识别出多少推多少。"""
    audio_bytes = await file.read()
    voice = _voice_engine(request)

    async def _sse():
        async for text in voice.transcribe_stream(audio_bytes):
            yield f"data: {json.dumps({'chunk': text})}\n\n"
        yield "data: [done]\n\n"

    return StreamingResponse(
        _sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/voice/tts/{message_id}")
async def synthesize_speech(request: Request, message_id: int):
    """将指定消息转为 TTS MP3 音频流。"""
    row = request.app.state.core.db.conn.execute(
        "SELECT content FROM thinker_messages WHERE id = ?", (message_id,)
    ).fetchone()
    if not row:
        return JSONResponse({"error": "message not found"}, status_code=404)

    voice = _voice_engine(request)
    audio_bytes = await voice.synthesize(row["content"])

    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline"},
    )


@router.get("/api/status")
async def thinker_status():
    return {"module": "thinker", "status": "ok", "version": "0.2.0"}


# ── Rehearsal API ─────────────────────────────────────────────

from .rehearsal import RehearsalEngine


def _rehearsal_engine(request: Request) -> RehearsalEngine:
    return RehearsalEngine(request.app.state.core)


@router.post("/api/rehearsal/start")
async def rehearsal_start(
    request: Request,
    title: str = Form(""),
    question_list_path: str = Form(""),
    question_list_content: str = Form(""),
):
    """Create a rehearsal session. Loads the .md question list file and snapshots its content.

    If question_list_content is provided (from client-side file picker), use it directly.
    Otherwise, read from the server-side file path.
    """
    from pathlib import Path as _Path

    question_list_source = question_list_path
    loaded_content = question_list_content  # may be empty

    if not loaded_content and question_list_path:
        base_dir = (_Path(__file__).parent.parent.parent.parent).resolve()
        full_path = (base_dir / question_list_path).resolve()
        # Prevent path traversal: resolved path must stay within project root
        if base_dir not in (full_path, *full_path.parents):
            return JSONResponse(
                {"error": "Invalid file path"}, status_code=400,
            )
        if full_path.exists() and full_path.suffix == ".md":
            loaded_content = await asyncio.to_thread(full_path.read_text, encoding="utf-8")
        else:
            return JSONResponse(
                {"error": "File not found or not a .md file"},
                status_code=400,
            )

    # CRITICAL: prevent empty question bank — LLM will hallucinate questions from nothing
    if not loaded_content.strip():
        return JSONResponse(
            {"error": "问题列表为空。请上传一个 .md 问题文件或选择本地文件。"},
            status_code=400,
        )

    engine = _rehearsal_engine(request)
    rid = await engine.create(
        title=title or "Untitled Rehearsal",
        question_list_source=question_list_source,
        question_list_content=loaded_content,
    )
    return JSONResponse({"id": rid})


@router.get("/api/rehearsals")
async def list_rehearsals(
    request: Request,
    q: str = "",
    type: str = "",
):
    """List rehearsal history."""
    engine = _rehearsal_engine(request)
    items = await engine.list_rehearsals(q=q)
    return JSONResponse(items)


@router.get("/api/rehearsal/{rehearsal_id}")
async def get_rehearsal(request: Request, rehearsal_id: int):
    """Get a single rehearsal's details."""
    engine = _rehearsal_engine(request)
    result = await engine.get_rehearsal(rehearsal_id)
    if not result:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(result)


@router.delete("/api/rehearsal/{rehearsal_id}")
async def delete_rehearsal(request: Request, rehearsal_id: int):
    """Delete a rehearsal record."""
    engine = _rehearsal_engine(request)
    await engine.delete_rehearsal(rehearsal_id)
    # Clean up cached WebM header
    from .voice import _webm_headers
    _webm_headers.pop(rehearsal_id, None)
    return JSONResponse({"ok": True})


@router.post("/api/rehearsal/{rehearsal_id}/transcribe-chunk")
async def transcribe_chunk(
    request: Request,
    rehearsal_id: int,
    file: UploadFile,
    format: str = "webm",
):
    """
    Receive a single audio chunk (~30s Opus), transcribe it via STT,
    append the result to the presentation transcript in DB.

    Retries up to 3 times with exponential backoff for transient API failures.
    Non-retryable errors (auth, empty result) return immediately.
    """
    # Validate rehearsal exists before doing expensive STT
    engine = _rehearsal_engine(request)
    if not await engine.validate_rehearsal(rehearsal_id):
        return JSONResponse({"error": "rehearsal not found"}, status_code=404)

    audio_bytes = await file.read()
    if len(audio_bytes) > 10 * 1024 * 1024:  # 10MB
        return JSONResponse({"error": "Audio chunk too large"}, status_code=413)
    voice = _voice_engine(request)

    # Whitelist client format. voice.py normalizes webm/opus → wav for api.gpt.ge.
    _allowed_formats = {"webm", "mp4", "ogg", "mp3", "wav", "m4a", "flac", "opus"}
    _stt_format = format if format in _allowed_formats else "webm"

    # Skip chunks that are too small to contain meaningful speech
    # (MediaRecorder may fire on near-silent intervals, producing tiny chunks)
    chunk_size = len(audio_bytes)
    if chunk_size < 500:
        return JSONResponse({"text": "", "info": f"chunk too small ({chunk_size}B), skipped"})

    max_attempts = 2
    for attempt in range(max_attempts):
        try:
            text = await voice.transcribe_chunk(audio_bytes, rehearsal_id=rehearsal_id, format=_stt_format)
            if text:
                engine = _rehearsal_engine(request)
                await engine.append_presentation_transcript(rehearsal_id, text)
                return JSONResponse({"text": text})
            # Empty result: no speech detected — don't retry
            return JSONResponse({"text": ""})
        except Exception as e:
            _logger = logging.getLogger(__name__)
            error_str = str(e)
            # Non-retryable errors: auth (401/403), format (400), upstream crash (500)
            # Retrying the same audio bytes against an upstream 500 is wasteful —
            # the server already failed to process this specific data.
            non_retryable = any(
                f"Error code: {code}" in error_str for code in ("400", "401", "403", "500")
            )
            if non_retryable:
                _logger.error(
                    f"[Rehearsal] STT chunk non-retryable error "
                    f"(size={chunk_size}B, attempt={attempt + 1}/{max_attempts}): {error_str[:200]}"
                )
                return JSONResponse({"text": "", "error": "Speech service error — chunk skipped"})
            # Transient: retry on network errors, timeouts
            if attempt < max_attempts - 1:
                delay = 2 ** attempt  # 1s, 2s
                _logger.warning(
                    f"[Rehearsal] STT chunk attempt {attempt + 1}/{max_attempts} failed "
                    f"(size={chunk_size}B), retrying in {delay}s: {error_str[:120]}"
                )
                await asyncio.sleep(delay)
            else:
                _logger.error(
                    f"[Rehearsal] STT chunk exhausted all {max_attempts} attempts "
                    f"(size={chunk_size}B): {error_str[:200]}"
                )

    return JSONResponse({"text": "", "error": "Transcription failed after retries"})


@router.post("/api/rehearsal/{rehearsal_id}/finish-presentation")
async def finish_presentation(request: Request, rehearsal_id: int):
    """End the presentation phase, transition to Q&A."""
    engine = _rehearsal_engine(request)
    await engine.update_status(rehearsal_id, "qa")
    return JSONResponse({"ok": True})


@router.get("/api/rehearsal/{rehearsal_id}/next-question")
async def get_next_question(request: Request, rehearsal_id: int):
    """LLM audience picks the next question from the question bank."""
    engine = _rehearsal_engine(request)
    rehearsal = await engine.get_rehearsal(rehearsal_id)
    if not rehearsal:
        return JSONResponse({"error": "not found"}, status_code=404)

    presentation_text = rehearsal.get("presentation_transcript", "")
    qa_text = rehearsal.get("qa_transcript", "")

    previous_qa = _parse_qa_transcript(qa_text)

    question = await engine.next_question(
        rehearsal_id=rehearsal_id,
        presentation_text=presentation_text,
        previous_qa=previous_qa,
    )
    return JSONResponse({"question": question})


def _parse_qa_transcript(qa_text: str) -> list[tuple[str, str]]:
    """Parse Q&A transcript text into (question, answer) pairs."""
    import re as _re
    pairs = []
    pattern = _re.compile(
        r"\[Q · 🤖\]\s*(.*?)\n\[A · 🎤\]\s*(.*?)(?=\n\[Q · 🤖\]|\Z)",
        _re.DOTALL,
    )
    for match in pattern.finditer(qa_text):
        q = match.group(1).strip()
        a = match.group(2).strip()
        if q and a:
            pairs.append((q, a))
    return pairs


@router.post("/api/rehearsal/{rehearsal_id}/answer")
async def submit_answer(
    request: Request,
    rehearsal_id: int,
    question: str = Form(""),
    answer_text: str = Form(""),
    file: UploadFile | None = None,
):
    """
    Submit an answer for the current Q&A round.
    If a file is provided, transcribe it via STT first.
    Otherwise uses answer_text directly.
    Appends the Q&A turn to the DB.
    """
    final_answer = answer_text

    if file and file.filename:
        audio_bytes = await file.read()
        voice = _voice_engine(request)
        # Accept client format (iOS mp4, Firefox ogg, Chrome webm)
        qa_format = (file.content_type or "").split("/")[-1] or "webm"
        # Retry STT up to 2 times for Q&A answers (shorter — user is waiting)
        for attempt in range(2):
            try:
                final_answer = await voice.transcribe_chunk(audio_bytes, rehearsal_id=rehearsal_id, format=qa_format)
                if final_answer:
                    break
            except Exception:
                if attempt < 1:
                    await asyncio.sleep(1)
        final_answer = final_answer or ""

    if not final_answer.strip():
        return JSONResponse({"error": "answer is empty"}, status_code=400)

    engine = _rehearsal_engine(request)
    await engine.append_qa_turn(rehearsal_id, question, final_answer)
    return JSONResponse({"answer": final_answer})


@router.get("/api/rehearsal/{rehearsal_id}/tts/question")
async def tts_question(request: Request, rehearsal_id: int):
    """Convert a question to TTS audio. Question text passed via query param."""
    text = request.query_params.get("text", "")
    if not text:
        return JSONResponse({"error": "text is required"}, status_code=400)

    voice = _voice_engine(request)
    audio_bytes = await voice.synthesize(text)

    return StreamingResponse(
        io.BytesIO(audio_bytes),
        media_type="audio/mpeg",
        headers={"Content-Disposition": "inline"},
    )


@router.post("/api/rehearsal/{rehearsal_id}/finish-qa")
async def finish_qa(request: Request, rehearsal_id: int):
    """End Q&A, trigger LLM summary generation."""
    engine = _rehearsal_engine(request)

    rehearsal = await engine.get_rehearsal(rehearsal_id)
    if not rehearsal:
        return JSONResponse({"error": "not found"}, status_code=404)

    # Count completed Q&A rounds from transcript
    qa_text = rehearsal.get("qa_transcript", "")
    qa_rounds = qa_text.count("[Q · 🤖]")
    min_rounds = 3
    if qa_rounds < min_rounds:
        return JSONResponse({
            "error": f"至少需要 {min_rounds} 轮问答才能生成有意义的总结。当前已完成 {qa_rounds} 轮。"
        }, status_code=400)

    await engine.update_status(rehearsal_id, "summarizing")

    presentation_text = rehearsal.get("presentation_transcript", "")

    try:
        summary = await engine.generate_summary(
            rehearsal_id=rehearsal_id,
            presentation_text=presentation_text,
            qa_text=qa_text,
        )
    except Exception:
        _logger = logging.getLogger(__name__)
        _logger.exception("[Rehearsal] Summary generation failed, reverting to qa")
        await engine.update_status(rehearsal_id, "qa", force=True)
        return JSONResponse(
            {"error": "Summary generation failed. Please try again."},
            status_code=500,
        )

    await engine.save_summary(
        rehearsal_id=rehearsal_id,
        briefing=summary["briefing"],
        grammar_corrections=summary["grammar_corrections"],
        suggestions=summary["suggestions"],
    )

    return JSONResponse(summary)


@router.get("/api/rehearsal/{rehearsal_id}/summary")
async def get_rehearsal_summary(request: Request, rehearsal_id: int):
    """Get the complete four-part rehearsal summary."""
    engine = _rehearsal_engine(request)
    rehearsal = await engine.get_rehearsal(rehearsal_id)
    if not rehearsal:
        return JSONResponse({"error": "not found"}, status_code=404)

    return JSONResponse({
        "id": rehearsal["id"],
        "title": rehearsal["title"],
        "created_at": rehearsal["created_at"],
        "part1_transcript": {
            "presentation": rehearsal.get("presentation_transcript", ""),
            "qa": rehearsal.get("qa_transcript", ""),
        },
        "part2_briefing": rehearsal.get("summary_briefing", ""),
        "part3_grammar_corrections": rehearsal.get("summary_grammar_corrections", []),
        "part4_suggestions": rehearsal.get("summary_suggestions", []),
        "audio_path": rehearsal.get("full_audio_path", ""),
    })


@router.post("/api/rehearsal/{rehearsal_id}/save-full-audio")
async def save_full_audio(request: Request, rehearsal_id: int):
    """
    After presentation ends, receive all audio chunks and concatenate
    into a complete Opus file for archival.
    """
    form = await request.form()
    chunks_data: list[bytes] = []

    # Collect chunks from form fields
    for i in range(len(form)):
        field = form.get(f"chunk_{i}")
        if field is not None:
            if hasattr(field, "file"):
                chunks_data.append(field.file.read())
            elif isinstance(field, bytes):
                chunks_data.append(field)

    if not chunks_data:
        return JSONResponse({"error": "no audio chunks provided"}, status_code=400)

    from pathlib import Path as _Path

    base_dir = _Path(__file__).parent.parent.parent.parent
    engine = _rehearsal_engine(request)
    rehearsal = await engine.get_rehearsal(rehearsal_id)
    if not rehearsal:
        return JSONResponse({"error": "not found"}, status_code=404)

    output_dir = base_dir / "outputs" / "rehearsal_audio"
    voice = _voice_engine(request)
    audio_path = await asyncio.to_thread(
        voice.save_full_audio, rehearsal_id, chunks_data, str(output_dir)
    )

    await engine.set_audio_path(rehearsal_id, audio_path)

    return JSONResponse({"ok": True, "filename": _Path(audio_path).name})
