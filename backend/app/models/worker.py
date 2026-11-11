"""worker 实例心跳：与任务心跳分离的进程存活证据。

- 每个 worker 进程（每个角色）拥有独立的 ``instance_id``，启动时登记一行；
- 独立线程每 10 秒短事务续期 ``heartbeat_at``，空闲与长任务期间都有效；
- 退出时尽力写 ``stopped_at``，让管理员能区分“正常停止”和“心跳超时/崩溃”；
- 任务表里的 ``heartbeat_at`` 只说明该任务被领取，不能在没有任务时推断进程存活，
  因此不能替代本表。

本表由 ``Base.metadata.create_all`` 幂等创建，不新增破坏性迁移。
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        # 运维查询按固定 role 聚合，最近心跳也用于展示；复合索引覆盖两种读取。
        Index("ix_worker_heartbeats_role_heartbeat", "role", "heartbeat_at"),
    )

    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, server_default=func.now(), nullable=False
    )
    # NULL = 尚未观测到正常退出；进程崩溃或被杀时保持 NULL，由端点按 45 秒判定 stale。
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
