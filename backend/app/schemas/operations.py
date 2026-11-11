"""运维接口的冻结响应结构：worker 角色心跳与队列状态。

字段顺序与 ``IMPLEMENTATION-PLAN.md`` 的 O 契约一致。未知值必须保持 ``None``
（JSON ``null``），绝不能用 0 冒充“队列为空/进程健康”。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

WorkerRoleName = Literal["ingest", "summary", "project", "regression"]
WorkerRoleState = Literal[
    "disabled",
    "not_started",
    "running",
    "stale",
    "stopped",
    "unknown",
]


class WorkerLastFailureRead(BaseModel):
    """最近一次失败：只有稳定安全码与记录更新时间。"""

    model_config = ConfigDict(frozen=True)

    updated_at: datetime
    code: str


class WorkerRoleStatusRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: WorkerRoleName
    enabled: bool
    state: WorkerRoleState
    alive_count: int | None
    last_heartbeat_at: datetime | None
    pending: int | None
    processing: int | None
    stale_tasks: int | None
    oldest_pending_age_seconds: float | None
    last_failure: WorkerLastFailureRead | None


class WorkersStatusRead(BaseModel):
    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    stale_after_seconds: int
    degraded: bool
    roles: list[WorkerRoleStatusRead]
