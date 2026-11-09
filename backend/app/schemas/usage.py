"""用量与费用接口；单价与金额用十进制字符串序列化。"""
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

UsageKind = Literal["chat", "embedding", "rerank"]
# summary 由普通对话摘要 worker 产生（kind 仍是 chat，但操作独立计量，不能借 rewrite 计费）；
# project_embedding 保留给阶段二项目索引，先加入枚举便于统计过滤与前端标签。
UsageOperation = Literal["generation", "rewrite", "query_embedding", "document_embedding", "rerank", "summary", "project_embedding"]
Granularity = Literal["hour", "day", "month"]
UsageVisibility = Literal["all", "mine"]
Rate = Annotated[Decimal, Field(ge=0, le=1_000_000, max_digits=18, decimal_places=6, allow_inf_nan=False)]


class PriceUpdate(BaseModel):
    provider: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=256)
    kind: UsageKind
    input_per_million: Rate | None
    output_per_million: Rate | None
    cached_input_per_million: Rate | None = None
    expected_revision: int = Field(ge=0, strict=True)

    @field_validator("provider", "model")
    @classmethod
    def clean_identity(cls, value, info):
        value = value.strip()
        if not value or any(ord(char) < 32 for char in value):
            raise ValueError("模型与服务商名称不能为空或包含控制字符")
        return value.lower() if info.field_name == "provider" else value

    @model_validator(mode="after")
    def validate_rates(self):
        if (self.input_per_million is None) != (self.output_per_million is None):
            raise ValueError("输入、输出单价必须同时填写或同时清空")
        if self.input_per_million is None and self.cached_input_per_million is not None:
            raise ValueError("清空单价时也需清空缓存单价")
        if self.kind != "chat" and self.output_per_million not in (None, Decimal(0)):
            raise ValueError("向量或重排模型的输出单价必须为 0")
        return self


class PriceRead(BaseModel):
    id: uuid.UUID | None = None
    provider: str
    model: str
    kind: UsageKind
    currency: str = "CNY"
    input_per_million: Decimal | None = None
    output_per_million: Decimal | None = None
    cached_input_per_million: Decimal | None = None
    revision: int = 0
    model_config = {"from_attributes": True}


class PriceList(BaseModel):
    currency: str = "CNY"
    items: list[PriceRead]


class UsageMetrics(BaseModel):
    requests: int = 0
    successful_requests: int = 0
    failed_requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    usage_known_requests: int = 0
    usage_missing_requests: int = 0
    priced_requests: int = 0
    unpriced_requests: int = 0
    estimated_cost: str | None = None
    input_cost: str | None = None
    output_cost: str | None = None


class UsagePeriod(UsageMetrics):
    period: datetime


class UsageBreakdown(UsageMetrics):
    provider: str
    model: str
    kind: UsageKind
    operation: UsageOperation


class UsageStatistics(BaseModel):
    start: date
    end: date
    timezone: str = "Asia/Shanghai"
    granularity: Granularity
    scope: UsageVisibility
    currency: str = "CNY"
    tracking_started_at: datetime | None = None
    totals: UsageMetrics
    series: list[UsagePeriod]
    models: list[str]
    breakdown: list[UsageBreakdown]


# ---------------------------------------------------------------------------
# 精确模型费用测量（管理员只读）
# ---------------------------------------------------------------------------

# 选择集与时间窗的硬界：查询前拒绝，避免无界读取长期 model_usage。
MEASUREMENT_MAX_REQUESTS = 50
MEASUREMENT_MAX_DOCUMENTS = 50
MEASUREMENT_MAX_WINDOW_DAYS = 31

# Pydantic 会把 "0"、"86400"、Unix 秒/毫秒/小数字符串解释为 UTC datetime。
# 测量窗口承诺的是“带时区的 ISO 8601 date-time”，所以在宽松解析前先限定格式。
_ISO_AWARE_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)

MeasurementCoverageStatus = Literal["complete", "partial", "unknown"]


class MeasurementRequestRef(BaseModel):
    """一个精确请求身份；user_id 与 request_id 一起才能定位 ChatTurn。

    request_id 是客户端为一次新问答生成的 UUID，不是 ChatTurn.id，也不是
    ModelUsage 的主键。同一个 request UUID 被不同用户使用时必须由 user_id 隔离。
    """

    user_id: uuid.UUID
    request_id: uuid.UUID

    model_config = {"extra": "forbid"}


class UsageMeasurementRequest(BaseModel):
    """POST /usage/measurement 的只读选择集；窗口为 UTC 半开区间 [start, end)。"""

    start: datetime
    end: datetime
    requests: list[MeasurementRequestRef] = Field(default_factory=list,
                                                  max_length=MEASUREMENT_MAX_REQUESTS)
    document_ids: list[uuid.UUID] = Field(default_factory=list,
                                          max_length=MEASUREMENT_MAX_DOCUMENTS)

    model_config = {"extra": "forbid"}

    @field_validator("start", "end", mode="before")
    @classmethod
    def datetime_input_type(cls, value):
        # 只接受 JSON 字符串或 datetime；拒绝数字时间戳等隐式宽松转换。
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise ValueError("测量窗口必须是带时区的 ISO 8601 字符串")
        if not _ISO_AWARE_DATETIME.fullmatch(value):
            raise ValueError("测量窗口必须是带时区的 ISO 8601 date-time")
        return value

    @field_validator("start", "end")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("测量窗口必须包含时区")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_selection(self):
        if self.end <= self.start:
            raise ValueError("结束时间必须晚于开始时间")
        if self.end - self.start > timedelta(days=MEASUREMENT_MAX_WINDOW_DAYS):
            raise ValueError(f"测量窗口最多 {MEASUREMENT_MAX_WINDOW_DAYS} 天")
        if not self.requests and not self.document_ids:
            raise ValueError("至少需要 requests 或 document_ids，不能把空范围当作零费用")
        pairs = [(item.user_id, item.request_id) for item in self.requests]
        if len(set(pairs)) != len(pairs):
            raise ValueError("requests 不能包含重复的 (user_id, request_id)")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("document_ids 不能重复")
        return self


class UsageMeasurementMetrics(BaseModel):
    """实际尝试的聚合；缺失值保持 missing/unpriced，不折算成 0。"""

    attempts: int = 0
    succeeded_attempts: int = 0
    failed_attempts: int = 0
    cancelled_attempts: int = 0
    interrupted_attempts: int = 0
    started_attempts: int = 0
    other_status_attempts: int = 0

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    usage_known_attempts: int = 0
    usage_missing_attempts: int = 0

    priced_attempts: int = 0
    unpriced_attempts: int = 0
    # 仅聚合调用时价格快照中已存在的金额；没有已计价尝试时为 null，绝不写 0。
    known_cost_subtotal: str | None = None
    # 仅在整体覆盖完整（无缺失/未计价/未完成/截断/币种冲突）时才给出总费用。
    total_cost: str | None = None


class UsageMeasurementOperationMetrics(UsageMeasurementMetrics):
    operation: str


class UsageMeasurementRequestResult(BaseModel):
    user_id: uuid.UUID
    request_id: uuid.UUID
    found: bool
    turn_status: str | None = None
    attempts: int = 0
    operations: list[str] = Field(default_factory=list)
    known_cost_subtotal: str | None = None
    coverage_status: MeasurementCoverageStatus = "unknown"
    reasons: list[str] = Field(default_factory=list)


class UsageMeasurementDocumentResult(BaseModel):
    document_id: uuid.UUID
    exists: bool
    document_status: str | None = None
    attempts: int = 0
    operations: list[str] = Field(default_factory=list)
    known_cost_subtotal: str | None = None
    coverage_status: MeasurementCoverageStatus = "unknown"
    reasons: list[str] = Field(default_factory=list)


class UsageMeasurementCoverage(BaseModel):
    status: MeasurementCoverageStatus
    complete: bool
    truncated: bool
    # 选择集中查不到的身份/文档；以及找到了但窗口内没有计量记录的数量。
    missing_requests: int = 0
    missing_documents: int = 0
    attempts_missing_requests: int = 0
    attempts_missing_documents: int = 0
    usage_missing_attempts: int = 0
    unpriced_attempts: int = 0
    unfinished_attempts: int = 0
    # 请求/文档生命周期尚未到可信终态，或出现本项目不认识的终态值。
    nonterminal_requests: int = 0
    nonterminal_documents: int = 0
    unknown_request_statuses: int = 0
    unknown_document_statuses: int = 0
    # 尝试状态不在已知终结/未完成集合内；时间戳和费用都不能证明其语义。
    unknown_attempt_statuses: int = 0
    association_mismatch_attempts: int = 0
    currency_mismatch: bool = False
    currencies: list[str] = Field(default_factory=list)
    background_operations_unmeasured: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class UsageMeasurementReport(BaseModel):
    # 字段名避开 BaseModel.schema，同时保持对外 JSON 的 "schema" 键。
    schema_version: str = Field(default="rag-usage-measurement/v1", alias="schema",
                                serialization_alias="schema")
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    currency: str | None = None
    totals: UsageMeasurementMetrics
    by_operation: list[UsageMeasurementOperationMetrics] = Field(default_factory=list)
    requests: list[UsageMeasurementRequestResult] = Field(default_factory=list)
    documents: list[UsageMeasurementDocumentResult] = Field(default_factory=list)
    coverage: UsageMeasurementCoverage
    boundaries: list[str] = Field(default_factory=list)
