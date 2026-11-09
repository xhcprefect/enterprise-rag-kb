"""已完成回答的用户反馈与管理员核实结果。

反馈行只保存反馈分类、说明与冻结的结果指纹；提问/回答/引用正文一律从原消息
读取，不复制进本表。核实后的标注字段供显式导出为既有评测 case 使用。
"""
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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 反馈分类：回答不准 / 引用错误 / 资料缺失。
FEEDBACK_CATEGORY_INACCURATE_ANSWER = "inaccurate_answer"
FEEDBACK_CATEGORY_CITATION_ERROR = "citation_error"
FEEDBACK_CATEGORY_MISSING_MATERIAL = "missing_material"
FEEDBACK_CATEGORIES = (
    FEEDBACK_CATEGORY_INACCURATE_ANSWER,
    FEEDBACK_CATEGORY_CITATION_ERROR,
    FEEDBACK_CATEGORY_MISSING_MATERIAL,
)

# 处理状态：待处理 / 已核实 / 已驳回。
FEEDBACK_STATUS_PENDING = "pending"
FEEDBACK_STATUS_VERIFIED = "verified"
FEEDBACK_STATUS_REJECTED = "rejected"
FEEDBACK_STATUSES = (
    FEEDBACK_STATUS_PENDING,
    FEEDBACK_STATUS_VERIFIED,
    FEEDBACK_STATUS_REJECTED,
)


class AnswerFeedback(Base):
    __tablename__ = "answer_feedbacks"
    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_answer_feedbacks_user_message"),
        Index("ix_answer_feedbacks_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True, nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=FEEDBACK_STATUS_PENDING, server_default=FEEDBACK_STATUS_PENDING,
        index=True, nullable=False,
    )
    revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    # 提交时冻结的结果指纹；原消息内容/引用/归属任一变化都会使它过期。
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 核验人代号快照：用户改名/删除后仍能还原核实时的身份。
    reviewer_name: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    corrected_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expected_sources: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    required_phrases: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    forbidden_phrases: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    unknowns: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    evidence_note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    reviewer_note: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
