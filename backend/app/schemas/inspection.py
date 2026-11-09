"""管理员入库质量预览与检索诊断的响应/请求结构。"""
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 单次检索诊断最多允许限定的文档数量；与 document_scopes.MAX_SCOPE_DOCUMENTS 一致。
MAX_DIAGNOSTIC_DOCUMENTS = 5
MAX_QUESTION_CHARS = 500
MAX_DOCUMENT_IDS = MAX_DIAGNOSTIC_DOCUMENTS


# ---------------- 入库质量 ----------------

class QualityIssueRead(BaseModel):
    code: str
    severity: Literal["error", "warning", "info"]
    count: int
    details: dict[str, Any] | None = None


class QualityStatsRead(BaseModel):
    empty_chunks: int
    short_chunks: int
    duplicate_chunks: int
    replacement_chunks: int
    control_chunks: int
    oversized_chunks: int
    min_chars: int
    max_chars: int
    avg_chars: int
    page_chunks: int
    section_chunks: int
    line_chunks: int
    sheet_chunks: int
    table_chunks: int


class DocumentQualityRead(BaseModel):
    document_id: uuid.UUID
    filename: str
    file_ext: str
    status: str
    error_message: str | None
    index_version: int
    parse_done: bool
    cancel_requested: bool
    chunk_count: int
    char_count: int
    scanned_chunks: int
    scan_limit: int
    scan_truncated: bool
    embedded_count: int
    unembedded_count: int
    file_exists: bool
    stats: QualityStatsRead
    issues: list[QualityIssueRead]


class DocumentChunkItemRead(BaseModel):
    chunk_index: int
    content: str
    content_chars: int
    truncated: bool
    embedded: bool
    location: dict


class DocumentChunkPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[DocumentChunkItemRead]


# ---------------- 检索诊断 ----------------

class RetrievalDiagnosticsRequest(BaseModel):
    """检索诊断请求：问题 trim 后 1..500 字符；文档范围去重保序、最多 5 份。"""

    model_config = ConfigDict(extra="forbid")

    question: str
    document_ids: list[uuid.UUID] = Field(default_factory=list)

    @field_validator("question")
    @classmethod
    def _trim_question(cls, value: str) -> str:
        text = value.strip()
        if not 1 <= len(text) <= MAX_QUESTION_CHARS:
            raise ValueError(f"问题长度必须在 1 到 {MAX_QUESTION_CHARS} 个字符之间")
        return text

    @field_validator("document_ids")
    @classmethod
    def _dedupe_document_ids(cls, values: list[uuid.UUID]) -> list[uuid.UUID]:
        unique = list(dict.fromkeys(values))
        if len(unique) > MAX_DOCUMENT_IDS:
            raise ValueError(f"最多选择 {MAX_DOCUMENT_IDS} 份文档")
        return unique


class RetrievalScopeRead(BaseModel):
    document_id: uuid.UUID
    filename: str
    index_version: int


class RetrievalKbRead(BaseModel):
    total_documents: int
    ready_documents: int
    retrievable_documents: int


class RetrievalConfigRead(BaseModel):
    top_k: int
    min_similarity: float
    max_per_doc: int
    hybrid_retrieval_enabled: bool
    keyword_candidates: int
    rerank_enabled: bool
    neighbor_chunks: int


class RetrievalStageRead(BaseModel):
    name: str
    status: Literal["ok", "error", "skipped", "fallback"]
    ms: int
    candidates: int
    error_type: str | None
    applied: bool | None
    reason: str | None


class RetrievalCandidateRead(BaseModel):
    rank: int
    document_id: uuid.UUID | None
    filename: str | None
    chunk_index: int | None
    chunk_start: int | None
    chunk_end: int | None
    index_version: int | None
    location: dict
    excerpt: str
    excerpt_chars: int
    excerpt_truncated: bool
    scores: dict[str, float]
    retrieval_method: str | None
    lifecycle_state: str | None
    retrievable: bool | None


class RetrievalDiagnosticsRead(BaseModel):
    status: Literal["ok", "error"]
    error_type: str | None
    question: str
    kb_id: uuid.UUID
    collection_name: str
    scope: list[RetrievalScopeRead]
    kb: RetrievalKbRead
    config: RetrievalConfigRead
    stages: list[RetrievalStageRead]
    candidates: list[RetrievalCandidateRead]
    total_ms: int
    answer_generated: bool = False
