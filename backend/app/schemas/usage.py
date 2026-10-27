"""用量与费用接口；单价与金额用十进制字符串序列化。"""
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

UsageKind = Literal["chat", "embedding", "rerank"]
UsageOperation = Literal["generation", "rewrite", "query_embedding", "document_embedding", "rerank"]
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
