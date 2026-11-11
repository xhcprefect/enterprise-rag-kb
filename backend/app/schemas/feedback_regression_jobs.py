"""反馈回归在线作业接口的请求/响应模型（pydantic v2）。

只暴露稳定 ID、状态、摘要、指纹与人工核验元数据；列表/详情/找回接口绝不返回
答案、引用或 trace 正文。正文仅在显式比较接口返回，且由作业/反馈级联清理。
"""
import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
ReviewStatus = Literal["unknown", "verified", "rejected"]
ExecutionKind = Literal["manual", "online"]
RequestKind = Literal["job", "batch"]

_MAX_NOTES = 1000
_SHA256 = r"^[0-9a-f]{64}$"


class _FrozenResponse(BaseModel):
    model_config = ConfigDict(frozen=True)


# ---------------------------------------------------------------------------
# 请求
# ---------------------------------------------------------------------------


class FeedbackRegressionJobCreateRequest(BaseModel):
    """单条在线回归作业提交；request_id 是路由+内容绑定的幂等键。"""

    feedback_id: uuid.UUID
    expected_revision: int = Field(ge=1, strict=True)
    request_id: uuid.UUID

    model_config = ConfigDict(extra="forbid")


class FeedbackRegressionBatchItem(BaseModel):
    feedback_id: uuid.UUID
    expected_revision: int = Field(ge=1, strict=True)

    model_config = ConfigDict(extra="forbid")


class FeedbackRegressionBatchCreateRequest(BaseModel):
    """批量提交（1–20 条唯一反馈）；整批验证通过后一次提交。"""

    request_id: uuid.UUID
    items: list[FeedbackRegressionBatchItem] = Field(min_length=1, max_length=20)

    model_config = ConfigDict(extra="forbid")


class FeedbackRegressionCancelRequest(BaseModel):
    expected_revision: int = Field(ge=1, strict=True)

    model_config = ConfigDict(extra="forbid")


class FeedbackRegressionReviewRequest(BaseModel):
    """人工核验：显式绑定 revision / run / 结果指纹，notes 有界且不记审计正文。"""

    expected_revision: int = Field(ge=1, strict=True)
    run_id: uuid.UUID
    result_sha256: str = Field(pattern=_SHA256)
    status: ReviewStatus
    notes: str = ""

    model_config = ConfigDict(extra="forbid")

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: str) -> str:
        value = value.strip()
        if len(value) > _MAX_NOTES:
            raise ValueError(f"核验说明不能超过 {_MAX_NOTES} 个字符")
        if any(
            unicodedata.category(char) == "Cc" and char not in "\n\t" for char in value
        ):
            raise ValueError("核验说明不能包含控制字符（换行与制表符除外）")
        return value


# ---------------------------------------------------------------------------
# 响应
# ---------------------------------------------------------------------------


class FeedbackRegressionJobBindingRead(_FrozenResponse):
    root_sha256: str
    case_sha256: str
    dataset_sha256: str
    binding_sha256: str


class FeedbackRegressionJobResultRead(_FrozenResponse):
    passed: bool | None
    failures: list[str]
    outcome: str | None
    http_status: int | None
    citation_count: int | None
    result_sha256: str | None
    machine_sha256: str | None
    captured_at: datetime | None


class FeedbackRegressionJobReviewRead(_FrozenResponse):
    status: ReviewStatus
    notes: str
    reviewed_by_name: str | None
    reviewed_at: datetime | None
    result_sha256: str | None


class FeedbackRegressionJobUsageRead(_FrozenResponse):
    """按 ``ModelUsage.regression_job_id`` 聚合的实际调用用量（含全部操作与重试）。

    语义与 usage 统计的“未知不补 0”一致，但更严格：

    * ``attempts`` 是实际模型调用行数（每次重试、每个 operation 各一行）；
    * 只要有一条调用缺 input/output token，三个 token 总量均为 ``null``，
      同时 ``usage_missing_attempts`` 给出缺失条数；
    * ``known_input_tokens`` / ``known_output_tokens`` 只汇总已测到 token 的调用，
      部分缺测时保留实际已测部分；一次都没测到则为 ``null``；
    * ``known_cost_subtotal`` 只在单一币种下汇总“已定价调用”的已知小计；
      多币种/缺币种时为 ``null``；
    * ``total_cost`` 只在全部调用 token 已知、全部已定价、全部已结束且单一
      币种时给出；任何部分缺失/未定价/未完成都保持 ``null``。
    """

    attempts: int
    succeeded_attempts: int
    failed_attempts: int
    cancelled_attempts: int
    interrupted_attempts: int
    started_attempts: int
    other_attempts: int
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    total_tokens: int | None
    known_input_tokens: int | None
    known_output_tokens: int | None
    usage_known_attempts: int
    usage_missing_attempts: int
    priced_attempts: int
    unpriced_attempts: int
    unfinished_attempts: int
    currency: str | None
    currency_mismatch: bool
    known_cost_subtotal: str | None
    total_cost: str | None


class FeedbackRegressionJobRead(_FrozenResponse):
    """作业白名单元数据；不含答案/引用/trace/原问题。"""

    job_id: uuid.UUID
    feedback_id: uuid.UUID
    regression_id: uuid.UUID
    batch_id: uuid.UUID | None
    run_id: uuid.UUID
    run_attempt_no: int
    status: JobStatus
    revision: int
    attempt_count: int
    execution_kind: Literal["online"] = "online"
    requested_mode: Literal["live"] = "live"
    can_import: bool = False
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    started_at: datetime | None
    deadline_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    error_code: str | None
    binding: FeedbackRegressionJobBindingRead
    result: FeedbackRegressionJobResultRead | None
    review: FeedbackRegressionJobReviewRead
    usage: FeedbackRegressionJobUsageRead


class FeedbackRegressionBatchCounts(_FrozenResponse):
    total: int
    queued: int
    running: int
    succeeded: int
    failed: int
    cancelled: int


class FeedbackRegressionBatchRead(_FrozenResponse):
    batch_id: uuid.UUID
    request_id: uuid.UUID
    revision: int
    created_at: datetime
    updated_at: datetime
    counts: FeedbackRegressionBatchCounts
    jobs: list[FeedbackRegressionJobRead]


class FeedbackRegressionRequestRead(_FrozenResponse):
    """按原 request_id 只读找回已提交的单条/批次结果（响应丢失恢复用）。"""

    request_id: uuid.UUID
    kind: RequestKind
    job: FeedbackRegressionJobRead | None = None
    batch: FeedbackRegressionBatchRead | None = None


class FeedbackRegressionComparisonOldRead(_FrozenResponse):
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    answer: str
    citations: list[dict]
    result_sha256: str


class FeedbackRegressionComparisonNewRead(_FrozenResponse):
    answer: str
    citations: list[dict]
    trace: dict
    passed: bool
    failures: list[str]
    outcome: str | None
    http_status: int | None
    citation_count: int
    result_sha256: str
    machine_sha256: str


class FeedbackRegressionComparisonRunRead(_FrozenResponse):
    run_id: uuid.UUID
    attempt_no: int
    execution_kind: Literal["online"]
    requested_mode: Literal["live"]


class FeedbackRegressionJobComparisonRead(_FrozenResponse):
    """经当前权限/绑定/来源复核后返回的可读新旧结果。"""

    job: FeedbackRegressionJobRead
    run: FeedbackRegressionComparisonRunRead
    binding: FeedbackRegressionJobBindingRead
    old: FeedbackRegressionComparisonOldRead
    new: FeedbackRegressionComparisonNewRead
