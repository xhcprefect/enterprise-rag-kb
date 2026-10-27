"""应用内模型调用用量与人民币计价快照；不保存问题、答案或 API Key。"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class ModelPrice(Base, TimestampMixin):
    __tablename__ = "model_prices"
    __table_args__ = (UniqueConstraint("provider", "model", "kind", name="uq_model_prices_identity"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    input_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    output_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cached_input_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ModelUsage(Base):
    __tablename__ = "model_usage"
    __table_args__ = (
        Index("ix_model_usage_time", "occurred_at"),
        Index("ix_model_usage_user_time", "user_id", "occurred_at"),
        Index("ix_model_usage_turn_operation", "turn_id", "operation"),
        UniqueConstraint("origin_key", name="uq_model_usage_origin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # 历史补录的稳定身份；正常调用每次尝试独立 UUID，重放不会创建新记录。
    origin_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    kb_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True)
    # 仅保留关联 UUID，删除对话/文档不删除已发生的用量，也不妨碍旧表迁移。
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    provider: Mapped[str] = mapped_column(String(128), nullable=False)
    model: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="started", nullable=False)
    source: Mapped[str] = mapped_column(String(16), default="provider", nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached_input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="CNY", nullable=False)
    price_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    output_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cached_input_per_million: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    input_cost: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    output_cost: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(30, 10), nullable=True)


class UsageTrackingState(Base):
    __tablename__ = "usage_tracking_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
