import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

ChatMode = Literal["rag", "chat"]


class ChatRequest(BaseModel):
    # 普通对话（mode=chat）不需要知识库
    kb_id: uuid.UUID | None = None
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    include_trace: bool = False
    top_k: int = Field(default=6, ge=1, le=20)
    # 仅在新建会话时生效；续聊以会话里已存的模式为准
    mode: ChatMode = "rag"


class Citation(BaseModel):
    # 保留参考资料的原始编号，筛选后不能把 [3] 重新标成 [1]。
    source_index: int | None = Field(default=None, ge=1)
    # 用于跨轮引用重新校验；不修改表结构，保存在现有 JSON 引用字段中。
    index_version: int | None = Field(default=None, ge=0)
    document_id: str | None = None
    filename: str | None = None
    chunk_index: int | None = None
    score: float | None = None
    snippet: str | None = None
    chunk_start: int | None = None
    chunk_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class MessageRead(BaseModel):
    id: uuid.UUID
    role: str
    status: str = "completed"
    turn_id: uuid.UUID | None = None
    reply_to_id: uuid.UUID | None = None
    content: str
    citations: list[Citation] | None = None
    latency_ms: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationRead(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID | None = None
    mode: str = "rag"
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationRead):
    messages: list[MessageRead] = []
