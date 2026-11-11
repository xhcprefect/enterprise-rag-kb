"""反馈回归在线作业：提交批次、持久作业与结果正文。

R1b 只负责持久状态机与人工比较所需的白名单正文；实际模型执行由 R2 独立 worker
调用 ``feedback_regression_jobs`` 的公共契约完成。

边界：

- 作业只保存冻结后的 ``ExecutionContext.summary()``（ID、版本、指纹、固定清单/
  项目身份），绝不保存原问题、临时 case 或旧答案。
- 新结果正文单独存放在 ``feedback_regression_job_bodies``，只含白名单
  ``answer`` / ``citations`` / 有限 ``trace``，并由反馈删除级联清理。
- 每个回归在任意时刻至多一个 ``queued``/``running`` 作业（PostgreSQL 部分唯一
  索引）；每个作业唯一关联一轮新的 ``online`` run。
- ``ModelUsage.regression_job_id`` 是裸 UUID（无级联外键）：反馈/作业删除后已发生
  的用量仍保留。
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# 作业状态机。
JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUSES = (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)
ACTIVE_JOB_STATUSES = (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)
TERMINAL_JOB_STATUSES = (
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
)

# 人工核验状态；每个新作业固定从 unknown 开始，不继承旧核验。
REVIEW_STATUS_UNKNOWN = "unknown"
REVIEW_STATUS_VERIFIED = "verified"
REVIEW_STATUS_REJECTED = "rejected"
REVIEW_STATUSES = (
    REVIEW_STATUS_UNKNOWN,
    REVIEW_STATUS_VERIFIED,
    REVIEW_STATUS_REJECTED,
)

# run.execution_kind：旧手工准备/导入轮次保持 manual；在线作业轮次固定 online。
EXECUTION_KIND_MANUAL = "manual"
EXECUTION_KIND_ONLINE = "online"
EXECUTION_KINDS = (EXECUTION_KIND_MANUAL, EXECUTION_KIND_ONLINE)


class FeedbackRegressionBatch(Base):
    """一次批量提交的幂等容器；成员由作业行本身构成，计数从实际作业推导。"""

    __tablename__ = "feedback_regression_batches"
    __table_args__ = (
        # 幂等键作用域是“发起管理员 + 客户端 UUID”：不同管理员可各自使用同一 UUID。
        Index(
            "uq_feedback_regression_batches_admin_request",
            "admin_id",
            "request_id",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 客户端 UUID 幂等键：同一 UUID 只能绑定一种路由种类和一份规范请求。
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 发起管理员：同键重放必须仍是同一管理员，避免跨管理员猜测/接管。
    admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    admin_name: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    # 只由显式批量操作（如取消）推进；成员作业自身状态变化不改变该值。
    revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FeedbackRegressionJob(Base):
    """一条反馈的一次在线重跑作业及其冻结绑定/结果摘要。"""

    __tablename__ = "feedback_regression_jobs"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_feedback_regression_jobs_run"),
        # 单条提交幂等键同样按管理员隔离；批量成员 request_id 为 NULL，不受约束。
        Index(
            "uq_feedback_regression_jobs_admin_request",
            "admin_id",
            "request_id",
            unique=True,
        ),
        # 每回归至多一个活动作业；并发提交由数据库唯一索引兜底。
        Index(
            "uq_feedback_regression_jobs_active",
            "regression_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_feedback_regression_jobs_claimable", "status", "available_at"
        ),
        Index(
            "ix_feedback_regression_jobs_feedback_created",
            "feedback_id",
            "created_at",
        ),
        Index(
            "ix_feedback_regression_jobs_batch_created", "batch_id", "created_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    regression_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_regressions.id", ondelete="CASCADE"),
        nullable=False,
    )
    feedback_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("answer_feedbacks.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 批量作业共享 batch；反馈删除导致作业级联删除后，batch 行保留但计数按实际作业推导。
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_regression_batches.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 每作业唯一关联一次新的 online run；attempt_no 是管理员可见的轮次序号。
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_regression_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    run_attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 单条提交的幂等键；批量成员为 NULL（批次持有唯一键）。
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    request_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), default=JOB_STATUS_QUEUED, server_default=JOB_STATUS_QUEUED,
        nullable=False,
    )
    # worker 对同一作业的有界重试次数；与 run.attempt_no（管理员每次新提交）独立。
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # 面向 UI/CAS 的 revision：状态变更与人工核验推进；heartbeat 不推进。
    revision: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )
    execution_kind: Mapped[str] = mapped_column(
        String(16), default=EXECUTION_KIND_ONLINE,
        server_default=EXECUTION_KIND_ONLINE, nullable=False,
    )
    requested_mode: Mapped[str] = mapped_column(
        String(8), default="live", server_default="live", nullable=False
    )

    admin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    admin_name: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    # 发起请求时从 API request.state 捕获的原始 token 版本；执行/发布复核使用，
    # 绝不从刷新后的 ORM 用户反推。
    admin_token_version: Mapped[int] = mapped_column(Integer, nullable=False)

    # ExecutionContext.summary() 白名单：不含原问题/临时 case/正文。
    context_summary: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    root_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
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
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 第一次 claim 建立整个作业的 started_at/deadline；重试与 heartbeat 不续期。
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_by_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 只保存白名单错误码，绝不保存原异常消息/traceback。
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ---- 新结果白名单摘要（正文在 body 表）----
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    machine_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    result_failures: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    result_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    citation_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_captured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- 人工核验（独立于原反馈核验与上一轮结果）----
    review_status: Mapped[str] = mapped_column(
        String(16), default=REVIEW_STATUS_UNKNOWN,
        server_default=REVIEW_STATUS_UNKNOWN, nullable=False,
    )
    review_notes: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 人工核验绑定的结果指纹；与 run/result 不一致时不得混用旧核验。
    review_result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)


class FeedbackRegressionJobBody(Base):
    """在线作业的新结果正文：严格白名单且由作业/反馈级联删除。"""

    __tablename__ = "feedback_regression_job_bodies"

    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("feedback_regression_jobs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    citations: Mapped[list] = mapped_column(
        JSONB, default=list, server_default="[]", nullable=False
    )
    trace: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    # 规范 UTF-8 JSON 的摘要与字节数；写入前已硬限 1 MiB。
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    body_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
