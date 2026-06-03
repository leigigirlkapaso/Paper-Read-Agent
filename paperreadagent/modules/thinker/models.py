"""
modules/thinker/models.py
Pydantic 数据模型，用于类型安全的请求/响应和内部传递。
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── 会话 ────────────────────────────────────────────────────────

class ConversationCreate(BaseModel):
    mode: str = Field(default="chat", pattern=r"^(chat|socratic|feynman|kpt|orid)$")
    title: str = ""


class ConversationUpdate(BaseModel):
    mode: str | None = Field(default=None, pattern=r"^(chat|socratic|feynman|kpt|orid)$")
    status: str | None = Field(default=None, pattern=r"^(active|paused|closed)$")
    intensity: str | None = Field(default=None, pattern=r"^(gentle|moderate|sharp)$")
    snooze_until: str | None = None
    title: str | None = None


class ConversationResponse(BaseModel):
    id: int
    title: str
    mode: str
    status: str
    intensity: str
    snooze_until: str | None = None
    created_at: str
    updated_at: str
    message_count: int = 0


# ── 消息 ────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    conversation_id: int
    message: str = Field(min_length=1, max_length=10000)


class MessageResponse(BaseModel):
    id: int
    conversation_id: int
    role: str
    content: str
    opener: str = ""
    created_at: str


# ── 承诺 ────────────────────────────────────────────────────────

class ResolutionResponse(BaseModel):
    id: int
    conversation_id: int
    content: str
    status: str
    asked_count: int
    reflection: str = ""
    created_at: str


class ResolutionUpdate(BaseModel):
    status: str = Field(pattern=r"^(fulfilled|abandoned)$")
    reflection: str = ""


# ── 主动问题 ────────────────────────────────────────────────────

class PendingQuestionResponse(BaseModel):
    id: int
    conversation_id: int
    question: str
    question_type: str
    generated_at: str


# ── 模式切换 ────────────────────────────────────────────────────

class ModeSwitchRequest(BaseModel):
    mode: str = Field(pattern=r"^(chat|socratic|feynman|kpt|orid)$")


class IntensityRequest(BaseModel):
    intensity: str = Field(pattern=r"^(gentle|moderate|sharp)$")


class PauseRequest(BaseModel):
    duration_minutes: int = Field(ge=1, le=480, default=30)
