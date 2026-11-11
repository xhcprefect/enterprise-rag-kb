"""反馈回归闭环：纳入记录与重跑轮次。

这里只保存稳定身份与摘要；原问题/回答/引用正文一律从原消息读取，上传的
评测报告正文也只在导入请求内校验，不落库、不进审计/日志。两表都通过
``answer_feedbacks`` / ``feedback_regressions`` 的数据库级联删除，避免历史
反馈删除后留下孤立回归数据。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 回归协议版本；case 内的 ``regression_protocol`` 与轮次记录共用该值。
REGRESSION_PROTOCOL_VERSION = 1


class FeedbackRegression(Base):
    """一条已核实反馈的回归纳入记录（每条反馈最多一条）。

    ``case_sha256`` / ``dataset_sha256`` / ``binding_sha256`` 描述纳入时的
    基准 case（不含执行模式）；新一轮准备只允许在该基准未变化时进行。
    """

    __tablename__ = "feedback_regressions"
    __table_args__ = (
        UniqueConstraint("feedback_id", name="uq_feedback_regressions_feedback"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    feedback_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("answer_feedbacks.id", ondelete="CASCADE"),
        nullable=False,
    )
    cause: Mapped[str] = mapped_column(String(32), nullable=False)
    cause_note: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_name: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    current_attempt_no: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    case_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FeedbackRegressionRun(Base):
    """一次“准备重跑”轮次及其导入后的白名单摘要。"""

    __tablename__ = "feedback_regression_runs"
    __table_args__ = (
        UniqueConstraint(
            "regression_id", "attempt_no", name="uq_feedback_regression_runs_attempt"
        ),
        UniqueConstraint(
            "regression_id",
            "prepare_request_id",
            name="uq_feedback_regression_runs_prepare_request",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    regression_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_regressions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 客户端 UUID 幂等键：同一准备请求重试不能产生两轮。
    prepare_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    protocol_version: Mapped[int] = mapped_column(
        Integer,
        default=REGRESSION_PROTOCOL_VERSION,
        server_default=str(REGRESSION_PROTOCOL_VERSION),
        nullable=False,
    )
    requested_mode: Mapped[str] = mapped_column(String(8), nullable=False)
    # 轮次执行种类：manual = 旧准备/导出/导入；online = 持久作业执行。
    # 老库由 init_db 幂等补列并以 ``manual`` 回填，旧轮次语义不变。
    execution_kind: Mapped[str] = mapped_column(
        String(16), default="manual", server_default="manual", nullable=False
    )
    # 准备时冻结的反馈版本与结果指纹：绑定变化后该轮只能作为历史。
    feedback_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    feedback_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 模式化 case 的三项摘要（含 regression_protocol / regression_mode）。
    case_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prepared_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    prepared_by_name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # ---- 导入结果（只存白名单摘要，不存报告正文）----
    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    imported_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    imported_by_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report_mode: Mapped[str | None] = mapped_column(String(8), nullable=True)
    validation_level: Mapped[str | None] = mapped_column(String(32), nullable=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    failures: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dataset_report_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    machine_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
