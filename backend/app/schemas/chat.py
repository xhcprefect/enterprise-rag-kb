import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    kb_id: uuid.UUID
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    top_k: int = Field(default=6, ge=1, le=20)


class Citation(BaseModel):
    document_id: str | None = None
    filename: str | None = None
    chunk_index: int | None = None
    score: float | None = None
    snippet: str | None = None


class MessageRead(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[Citation] | None = None
    latency_ms: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationRead(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationRead):
    messages: list[MessageRead] = []
