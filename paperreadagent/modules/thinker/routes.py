"""
modules/thinker/routes.py
Thinker 模块 API 路由：对话管理、SSE 流式聊天。
"""

from __future__ import annotations

import asyncio
import io
import json

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
    """Fullscreen thinker page for mobile; desktop users get redirected."""
    is_mobile = getattr(request.state, "is_mobile", False)
    if not is_mobile:
        return RedirectResponse(url="/projects/", status_code=303)
    from pathlib import Path
    from fastapi.templating import Jinja2Templates
    tpl_dir = Path(__file__).parent / "templates"
    tpl = Jinja2Templates(directory=str(tpl_dir))
    return tpl.TemplateResponse("fullscreen.html", {"request": request})


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
    return {"module": "thinker", "status": "ok", "version": "0.1.0"}
