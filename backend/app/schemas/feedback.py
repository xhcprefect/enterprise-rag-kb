"""答案反馈接口的请求与响应模型（pydantic v2）。"""
import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

FeedbackCategory = Literal["inaccurate_answer", "citation_error", "missing_material"]
FeedbackStatus = Literal["pending", "verified", "rejected"]
CorrectedOutcome = Literal["answer", "refuse", "clarify"]

# 短语与来源的硬上限由服务层映射为 400，schema 只负责结构与类型。
_MAX_NOTE = 1000


class FeedbackCreate(BaseModel):
    """提交反馈：只允许反馈身份、分类与说明，任何正文/状态字段都拒绝。"""

    message_id: uuid.UUID
    category: FeedbackCategory
    note: str = ""

    model_config = {"extra": "forbid"}

    @field_validator("note")
    @classmethod
    def clean_note(cls, value: str) -> str:
        value = value.strip()
        if len(value) > _MAX_NOTE:
            raise ValueError(f"反馈说明最多 {_MAX_NOTE} 个字符")
        if any(
            unicodedata.category(char) == "Cc" and char not in "\n\t" for char in value
        ):
            raise ValueError("反馈说明不能包含控制字符（换行与制表符除外）")
        return value


class AnswerFeedbackOwnerItem(BaseModel):
    """本人可见字段；不回显问题/答案/引用正文。"""

    id: uuid.UUID
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    category: str
    note: str
    status: str
    revision: int
    created_at: datetime
    reviewed_at: datetime | None

    model_config = {"from_attributes": True}


class AnswerFeedbackAdminItem(BaseModel):
    """管理员列表项：默认不返回任何正文。"""

    id: uuid.UUID
    category: str
    status: str
    revision: int
    created_at: datetime
    reviewed_at: datetime | None
    reviewer_name: str
    note_present: bool

    model_config = {"from_attributes": True}


class AnswerFeedbackAdminDetail(BaseModel):
    """管理员详情：正文从原消息实时读取，引用只保留白名单键。"""

    id: uuid.UUID
    category: str
    status: str
    mode: str
    revision: int
    created_at: datetime
    reviewed_at: datetime | None
    reviewer_name: str
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    question: str | None
    answer: str
    note: str
    citations: list[dict]
    invalid_source_ids: list[str]
    result_stale: bool
    corrected_outcome: str | None
    expected_sources: list[dict]
    required_phrases: list[str]
    forbidden_phrases: list[str]
    unknowns: list[str]
    evidence_note: str
    reviewer_note: str


class FeedbackExpectedSource(BaseModel):
    """核实标注的期望来源：至少一个身份字段；不接受任何额外键。"""

    document_id: uuid.UUID | None = None
    filename: str | None = None
    chunk_index: int | None = Field(default=None, ge=0, strict=True)
    index_version: int | None = Field(default=None, ge=0, strict=True)

    model_config = {"extra": "forbid"}

    @field_validator("filename")
    @classmethod
    def clean_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def require_identity(self):
        if self.document_id is None and not self.filename:
            raise ValueError("期望来源至少需要 document_id 或 filename")
        return self


class FeedbackReviewRequest(BaseModel):
    """核实/驳回请求体；未知字段一律 422。"""

    expected_revision: int = Field(ge=1, strict=True)
    decision: Literal["verified", "rejected"]
    corrected_outcome: CorrectedOutcome | None = None
    expected_sources: list[FeedbackExpectedSource] = Field(default_factory=list)
    required_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    evidence_note: str = ""
    reviewer_note: str = ""

    model_config = {"extra": "forbid"}


class FeedbackExportPayload(BaseModel):
    """显式导出的 JSONL 内容；不写服务器文件。"""

    filename: str
    case_count: int
    dataset_sha256: str
    jsonl: str
