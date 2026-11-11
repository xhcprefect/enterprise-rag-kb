"""反馈回归接口的请求/响应模型（pydantic v2）。

请求模型一律 ``extra="forbid"``；响应模型冻结。所有响应只含稳定 ID、摘要、
模式与计数，不返回问题/答案/引用/报告正文或原因说明以外的业务内容。
"""
import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RegressionCause = Literal[
    "parsing",
    "retrieval",
    "document_version",
    "generation",
    "scope_context",
    "other",
]
RequestedMode = Literal["offline", "live"]
ValidationLevel = Literal["consistency_only", "content_regraded"]
RunState = Literal["awaiting_report", "imported", "stale", "online"]
RunExecutionKind = Literal["manual", "online"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

_MAX_CAUSE_NOTE = 1000


class _FrozenResponse(BaseModel):
    model_config = ConfigDict(frozen=True)


class FeedbackRegressionRunRead(_FrozenResponse):
    """一轮重跑的白名单摘要；不含样例/报告正文。"""

    run_id: uuid.UUID
    attempt_no: int
    is_current: bool
    state: RunState
    execution_kind: RunExecutionKind
    can_import: bool
    job_id: uuid.UUID | None
    job_status: JobStatus | None
    job_revision: int | None
    requested_mode: RequestedMode
    protocol_version: int
    case_sha256: str
    dataset_sha256: str
    binding_sha256: str
    prepared_by_name: str
    created_at: datetime
    imported_at: datetime | None
    imported_by_name: str | None
    report_mode: str | None
    validation_level: ValidationLevel | None
    passed: bool | None
    failures: list[str]
    outcome: str | None
    http_status: int | None
    citation_count: int | None
    dataset_report_sha256: str | None
    report_sha256: str | None
    result_sha256: str | None
    machine_sha256: str | None


class FeedbackRegressionRead(_FrozenResponse):
    """回归记录；可用性按当前库内状态实时探测，不影响已保存的历史。"""

    feedback_id: uuid.UUID
    revision: int
    cause: RegressionCause
    cause_note: str
    created_by_name: str
    created_at: datetime
    current_attempt_no: int
    available: bool
    unavailable_reason: str | None
    current_run: FeedbackRegressionRunRead | None


class FeedbackRegressionCreateRequest(BaseModel):
    """显式纳入回归：原因分类与说明均由管理员填写。"""

    expected_feedback_revision: int = Field(ge=1, strict=True)
    cause: RegressionCause
    cause_note: str = ""

    model_config = ConfigDict(extra="forbid")

    @field_validator("cause_note")
    @classmethod
    def clean_note(cls, value: str) -> str:
        value = value.strip()
        if not 1 <= len(value) <= _MAX_CAUSE_NOTE:
            raise ValueError(f"回归原因说明需为 1–{_MAX_CAUSE_NOTE} 个字符")
        if any(
            unicodedata.category(char) == "Cc" and char not in "\n\t" for char in value
        ):
            raise ValueError("回归原因说明不能包含控制字符（换行与制表符除外）")
        return value


class FeedbackRegressionPrepareRequest(BaseModel):
    """准备下一轮：客户端 UUID 作为幂等键。"""

    expected_revision: int = Field(ge=1, strict=True)
    requested_mode: RequestedMode
    prepare_request_id: uuid.UUID

    model_config = ConfigDict(extra="forbid")


class FeedbackRegressionPrepareRead(_FrozenResponse):
    """单条 JSONL + 执行提示；服务端不执行评测、不调用模型。"""

    regression_revision: int
    run: FeedbackRegressionRunRead
    filename: str
    case_count: Literal[1] = 1
    dataset_sha256: str
    protocol_version: int
    jsonl: str
    cli_hint: str


class FeedbackRegressionImportRead(_FrozenResponse):
    """导入成功后的摘要；不返回报告正文。"""

    regression_revision: int
    run: FeedbackRegressionRunRead
