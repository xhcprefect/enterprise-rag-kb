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
    # 省略时用服务端 RETRIEVAL_TOP_K；显式传入仍以调用方为准（前端滑块）。
    top_k: int | None = Field(default=None, ge=1, le=20)
    # 仅在新建会话时生效；续聊以会话里已存的模式为准
    mode: ChatMode = "rag"
    # 仅新会话接受草稿范围；已有会话通过带版本校验的 PATCH 更新。
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=5)
    scope_revision: int | None = Field(default=None, ge=0, strict=True)


class RetrievalConfig(BaseModel):
    # 问答页滑块的初值。与 RETRIEVAL_TOP_K 同源，避免前后端各硬编码一个数字。
    top_k: int = Field(ge=1, le=20)


class DocumentScopeItem(BaseModel):
    document_id: str
    filename: str
    index_version: int = Field(ge=0)


class DocumentScopeState(BaseModel):
    document_scope: list[DocumentScopeItem] = Field(default_factory=list)
    scope_revision: int = 0


class DocumentScopeUpdate(BaseModel):
    document_ids: list[uuid.UUID] = Field(max_length=5)
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


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
    # 来源为该轮 trace，不新增消息正文列；旧消息按未截断解释。
    truncated: bool = False
    finish_reason: str | None = None

    model_config = {"from_attributes": True}


class ConversationRead(DocumentScopeState):
    id: uuid.UUID
    kb_id: uuid.UUID | None = None
    mode: str = "rag"
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationDetail(ConversationRead):
    messages: list[MessageRead] = []


class ChatRequestState(DocumentScopeState):
    """只返回请求本人在当前权限下可读取的结果，不返回诊断或部分生成正文。"""
    request_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    status: Literal["processing", "completed", "failed", "cancelled"]
    message: MessageRead | None = None
    truncated: bool = False
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
