"""会话摘要压缩：会话摘要（conversation_memories）与摘要任务（summary_jobs）。

F 基础实施契约定稿：

- ``ConversationMemory``：每会话至多一行，保存已发布的结构化摘要与水印。
  ``covered_turn_seq`` 是已纳入摘要的最大完整轮次 seq（发布水位）；读取未覆盖历史
  用 ``turn_seq > covered_turn_seq`` 且只取两消息均 completed 的完整轮。
- ``SummaryJob``：一次摘要任务的领料单/租约；领取时固定 ``input_snapshot``/``input_digest``
  后不可再变。每会话至多一个 pending 和一个 processing（部分唯一索引）。
  失败/取消的终态轮可留空号并记录跳过范围，不修改摘要水位。
"""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class SummaryJobStatus(str, enum.Enum):
    PENDING = "pending"        # 可被 worker 领取
    PROCESSING = "processing"  # 已领取，持有租约/心跳
    READY = "ready"            # 输出原始消息已生成但尚未发布（非本期必须）
    FAILED = "failed"          # 失败（有 error_code），不再重试（可新建任务）
    CANCELLED = "cancelled"    # 用户/上限明确取消

    __str__ = enum.Enum.__str__


class ConversationMemory(Base, TimestampMixin):
    """每会话一份的已发布结构化摘要与水印。原始 Message 从不删除/覆写。"""

    __tablename__ = "conversation_memories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    # 版本号：每次发布 +1；CAS 用 base_memory_version 防止过期任务覆盖新摘要。
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 记忆代次：会话重置（清空记忆）时 +1，旧任务/旧 job 不得越过新代次复活。
    epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 已纳入摘要的最大完整轮次 seq（发布水位）。历史读取用 turn_seq > covered_turn_seq。
    covered_turn_seq: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # 结构化摘要内容：{goals, constraints, confirmed_facts, decisions, preferences,
    # latest_corrections, open_items, names_numbers, source_role: {user/assistant...}}。
    structured: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    # 来源范围与内容摘要：{start_seq, end_seq, message_ids:[...]} + 内容 digest（字节级）。
    source_range: Mapped[dict] = mapped_column(JSONB, default=dict, server_default="{}", nullable=False)
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 生成所用的提示词版本与模型指纹（区分服务商/模型，避免跨配置误复用）。
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 估算（UTF-8 字节保守估算，不冒充真实 tokenizer 数）与实际用量。
    estimated_input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 覆盖抵减快照：本次发布抵减的完整轮次数/字符/估算 token，用于与会话增量计数对账。
    covered_turns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    covered_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    covered_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    conversation: Mapped["Conversation"] = relationship(back_populates="memory")


class SummaryJob(Base, TimestampMixin):
    """一次摘要任务的领料单与租约。领取（processing）后 input_snapshot/digest 冻结。"""

    __tablename__ = "summary_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_summary_jobs_idempotency_key"),
        # 每会话至多一个 pending 摘要（新增目标只 upsert pending，不重复排队）。
        Index(
            "uq_summary_jobs_one_pending",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        # 每会话至多一个 processing 摘要；领取须确认无 processing。
        Index(
            "uq_summary_jobs_one_processing",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'processing'"),
        ),
        Index("ix_summary_jobs_claimable", "status", "available_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 任务针对的摘要版本（领取时核对，防止覆盖较新摘要）。
    base_memory_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 本任务计划纳入摘要的目标轮次 seq；发布水位仍要核对来源，不能直接取它。
    target_turn_seq: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 幂等键：同一目标/来源只入队一次；重复回放不创建新工作。
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=SummaryJobStatus.PENDING.value, index=True, nullable=False
    )
    # 租约：领取后写入；到期/心跳超时可被其他 worker 回收。
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # SKIP LOCKED 领取可领取的最早时间；退避/失败后延迟，置于未来时间。
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 领取时固定的输入快照（已发布摘要 + 未覆盖旧前缀完整轮次），领取后不可变。
    input_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    conversation: Mapped["Conversation"] = relationship()
