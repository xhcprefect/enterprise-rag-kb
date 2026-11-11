"""worker 进程心跳与角色队列观测。

两条独立通道：

1. ``WorkerHeartbeatReporter``：worker 进程内独立守护线程，每 10 秒用一个短事务
   续期本实例的 ``worker_heartbeats`` 行；空闲、长任务、模型网络等待期间都有效。
   心跳写入失败只记日志，绝不影响真实任务；退出时有界 join 并尽力写 ``stopped_at``。
   任务表的 ``heartbeat_at`` 只证明“有任务被领取”，没有任务时无法证明进程存活，
   因此不能替代本表。

2. ``collect_worker_status``：管理员/Prometheus 使用的只读聚合。固定角色顺序
   ingest / summary / project / regression；启用以 API 当前配置为准（ingest 与
   regression 恒为开）。所有查询在独立连接、独立事务中执行，带
   ``statement_timeout`` 和 anyio 外层超时。查询失败时明确返回 unavailable，调用
   方必须输出 unknown/null，绝不补零冒充健康。regression 的队列/租约统计直接来自
   ``feedback_regression_jobs``，最近失败只取冻结白名单错误码。

最近失败只输出固定白名单错误码与记录更新时间；原始异常、消息、路径、文档名、
用户名和实例 UUID 都不会进入接口或指标。
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

import anyio.to_thread
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.db import session as db_session
from app.models import (
    Document,
    DocumentStatus,
    FeedbackRegressionJob,
    ProjectSnapshot,
    ProjectSnapshotStatus,
    SummaryJob,
    SummaryJobStatus,
    WorkerHeartbeat,
)
from app.models.feedback_regression_job import JOB_STATUS_FAILED, JOB_STATUS_QUEUED, JOB_STATUS_RUNNING
from app.services.feedback_regression_jobs import ERROR_CODES

logger = logging.getLogger(__name__)

WORKER_ROLE_INGEST = "ingest"
WORKER_ROLE_SUMMARY = "summary"
WORKER_ROLE_PROJECT = "project"
WORKER_ROLE_REGRESSION = "regression"
# 固定顺序；接口与指标都按此顺序输出，前端可稳定渲染。
WORKER_ROLES: tuple[str, ...] = (
    WORKER_ROLE_INGEST,
    WORKER_ROLE_SUMMARY,
    WORKER_ROLE_PROJECT,
    WORKER_ROLE_REGRESSION,
)

STALE_AFTER_SECONDS = 45
HEARTBEAT_INTERVAL_SECONDS = 10.0
HEARTBEAT_STOP_JOIN_SECONDS = 5.0
_HEARTBEAT_WRITE_TIMEOUT_MS = 5000

# 最近失败只允许这些稳定码；未知值一律收敛为通用安全码。
_GENERIC_FAILURE_CODE = "failed"
_FAILURE_CODE_INGEST = "document-failed"
_SUMMARY_FAILURE_CODES = frozenset({
    "model_error",
    "invalid_output",
    "worker_error",
    "nothing_to_archive",
    "no_claimable_input",
    "epoch_changed",
    "base_version_moved",
    "lease_expired",
    "source_changed",
})
_PROJECT_FAILURE_CODES = frozenset({
    "blob-missing",
    "blob-corrupt",
    "parse-error",
    "root-error",
    "config-changed",
    "sync-error",
    "max-attempts",
    "auto-empty",
    "auto-sync-failed",
    "auto-enqueue-failed",
})
_REGRESSION_FAILURE_CODES = ERROR_CODES


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """数据库返回 naive 时间时按 UTC 解释；绝不把本地时间混进输出。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age_seconds(started_at: datetime | None) -> float | None:
    started = _aware(started_at)
    if started is None:
        return None
    return max(0.0, (_now() - started).total_seconds())


def _safe_warning(message: str, *args) -> None:
    """心跳路径专用安全日志：日志 handler 自身故障也不得外溢到任务/退出。"""
    try:
        logger.warning(message, *args)
    except Exception:  # noqa: BLE001 - 观测不能反过来变成故障源
        pass


def _safe_failure_code(role: str, raw: str | None) -> str:
    if role == WORKER_ROLE_INGEST:
        return _FAILURE_CODE_INGEST
    if role == WORKER_ROLE_SUMMARY:
        allowed = _SUMMARY_FAILURE_CODES
    elif role == WORKER_ROLE_PROJECT:
        allowed = _PROJECT_FAILURE_CODES
    elif role == WORKER_ROLE_REGRESSION:
        allowed = _REGRESSION_FAILURE_CODES
    else:
        return _GENERIC_FAILURE_CODE
    value = str(raw) if raw is not None else ""
    return value if value in allowed else _GENERIC_FAILURE_CODE


class WorkerHeartbeatReporter:
    """一个 worker 实例的心跳线程；构造/启动/停止都不会向调用方抛异常。"""

    def __init__(
        self,
        role: str,
        *,
        session_factory=None,
        interval_seconds: float | None = None,
        join_timeout_seconds: float = HEARTBEAT_STOP_JOIN_SECONDS,
    ) -> None:
        if role not in WORKER_ROLES:
            raise ValueError("unknown worker role")
        self.role = role
        self.instance_id = uuid.uuid4()
        self._session_factory = session_factory or db_session.SessionLocal
        self._interval = max(0.05, float(interval_seconds or HEARTBEAT_INTERVAL_SECONDS))
        self._join_timeout = max(0.1, float(join_timeout_seconds))
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._registered = False

    def start(self) -> None:
        """启动守护线程；第一次续期立即执行，不阻塞 worker 主循环。"""
        if self._thread is not None:
            return
        try:
            thread = threading.Thread(
                target=self._loop,
                name=f"worker-heartbeat-{self.role}",
                daemon=True,
            )
            thread.start()
            self._thread = thread
        except Exception as exc:  # noqa: BLE001 - 观测线程失败不得影响任务
            _safe_warning(
                "worker 心跳线程启动失败 role=%s err=%s",
                self.role,
                type(exc).__name__,
            )

    def stop(self) -> None:
        """置位退出并有限等待；停止标记只在心跳线程内尽力写入。

        - 调用线程只做 ``set()`` + 有界 ``join()``，不在退出路径上获取数据库连接：
          连接池等待/建连不在 ``statement_timeout`` 覆盖范围内，可能把 worker
          退出无限挂住。
        - 线程被唤醒后总会尝试一次最终心跳（含 ``stopped_at``）；若心跳线程在
          ``join`` 时限内仍未退出（例如卡在已建立的慢查询上），进程照常退出，
          不等待不确定的数据库操作。
        - 首次登记采用幂等 upsert，并只在事务真正提交后才视为已登记；因此首次
          INSERT 提交失败后，下一轮会重新 upsert，数据库恢复后仍能出现心跳。
        """
        self._stopped.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self._join_timeout)
            if thread.is_alive():
                _safe_warning("worker 心跳线程未在时限内退出 role=%s", self.role)

    def _loop(self) -> None:
        try:
            while not self._stopped.is_set():
                self._beat_once()
                if self._stopped.wait(self._interval):
                    break
            # 退出前最后一次短事务：更新心跳并尽量写 stopped_at；线程内失败只记录。
            self._beat_once(final=True)
        except Exception as exc:  # noqa: BLE001 - 观测线程绝不影响任务/退出
            _safe_warning(
                "worker 心跳线程异常退出 role=%s err=%s",
                self.role,
                type(exc).__name__,
            )

    def _beat_once(self, *, final: bool = False) -> None:
        try:
            now = _now()
            stopped_at = now if final else None
            with self._session_factory() as db:
                with db.begin():
                    db.execute(text(f"SET LOCAL statement_timeout = {_HEARTBEAT_WRITE_TIMEOUT_MS}"))
                    # 幂等 upsert：首轮登记或提交失败后的重试用同一语句；
                    # 已存在的行只续期（正常心跳）或同时标记停止（最终心跳）。
                    values = {
                        "instance_id": self.instance_id,
                        "role": self.role,
                        "started_at": now,
                        "heartbeat_at": now,
                        "stopped_at": stopped_at,
                    }
                    stmt = pg_insert(WorkerHeartbeat).values(**values)
                    update_values = {"heartbeat_at": now}
                    if final:
                        update_values["stopped_at"] = now
                    db.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["instance_id"],
                            set_=update_values,
                        )
                    )
                # 只有 with db.begin() 正常退出（提交成功）才走到这里。
            self._registered = True
        except Exception as exc:  # noqa: BLE001 - 心跳失败绝不打断任务
            _safe_warning(
                "worker 心跳写入失败 role=%s err=%s",
                self.role,
                type(exc).__name__,
            )


def start_worker_heartbeat(role: str) -> WorkerHeartbeatReporter | None:
    """供 worker.py 使用：任何失败都只返回 None，不影响角色主循环。"""
    try:
        reporter = WorkerHeartbeatReporter(role)
    except Exception as exc:  # noqa: BLE001
        _safe_warning("worker 心跳初始化失败 role=%s err=%s", role, type(exc).__name__)
        return None
    reporter.start()
    return reporter


def stop_worker_heartbeat(reporter: WorkerHeartbeatReporter | None) -> None:
    if reporter is not None:
        try:
            reporter.stop()
        except Exception as exc:  # noqa: BLE001
            _safe_warning("worker 心跳停止失败 err=%s", type(exc).__name__)


# ---------------------------------------------------------------------------
# 只读聚合
# ---------------------------------------------------------------------------

def _enabled_roles() -> dict[str, bool]:
    return {
        WORKER_ROLE_INGEST: True,
        WORKER_ROLE_SUMMARY: bool(settings.CHAT_MEMORY_ENABLED),
        WORKER_ROLE_PROJECT: bool(settings.PROJECT_ALLOWED_ROOTS),
        WORKER_ROLE_REGRESSION: True,
    }


def _state_for(enabled: bool, stats: dict) -> str:
    if not enabled:
        return "disabled"
    if (stats.get("alive_count") or 0) > 0:
        return "running"
    total = int(stats.get("heartbeat_total") or 0)
    if total == 0:
        return "not_started"
    if int(stats.get("unstopped_total") or 0) > 0:
        return "stale"
    return "stopped"


def _parse_heartbeat_rows(rows: list) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for role, alive, last_heartbeat, total, unstopped in rows:
        if role not in WORKER_ROLES:
            continue
        stats[role] = {
            "alive_count": int(alive or 0),
            "last_heartbeat_at": _aware(last_heartbeat),
            "heartbeat_total": int(total or 0),
            "unstopped_total": int(unstopped or 0),
        }
    return stats


def _collect_worker_stats(timeout_seconds: float) -> dict:
    """独立连接/事务读取心跳与四类任务统计；只返回计数和固定安全码。"""
    now = _now()
    heartbeat_deadline = now - timedelta(seconds=STALE_AFTER_SECONDS)
    summary_deadline = now - timedelta(seconds=max(1, int(settings.MEMORY_JOB_LEASE_SECONDS)))
    project_deadline = now - timedelta(seconds=max(1, int(settings.PROJECT_STALE_SECONDS)))
    ingest_deadline = now - timedelta(seconds=max(1, int(settings.INGEST_STALE_SECONDS)))

    with db_session.engine.connect() as conn:
        with conn.begin():
            conn.execute(
                text(f"SET LOCAL statement_timeout = {max(1, int(timeout_seconds * 1000))}")
            )
            heartbeat_rows = conn.execute(
                select(
                    WorkerHeartbeat.role,
                    func.count()
                    .filter(
                        WorkerHeartbeat.stopped_at.is_(None),
                        WorkerHeartbeat.heartbeat_at >= heartbeat_deadline,
                    )
                    .label("alive"),
                    func.max(WorkerHeartbeat.heartbeat_at).label("last_heartbeat"),
                    func.count().label("total"),
                    func.count()
                    .filter(WorkerHeartbeat.stopped_at.is_(None))
                    .label("unstopped"),
                ).group_by(WorkerHeartbeat.role)
            ).all()

            ingest_pending, ingest_processing, ingest_stale, ingest_oldest = conn.execute(
                select(
                    func.count().filter(Document.status == DocumentStatus.PENDING.value),
                    func.count().filter(Document.status == DocumentStatus.PROCESSING.value),
                    # 与 worker.recover_stale 的真实谓词保持一致：心跳缺失/超时，
                    # **或**没有租约（processing 但 lease_id IS NULL 同样会被恢复）。
                    func.count().filter(
                        Document.status == DocumentStatus.PROCESSING.value,
                        (Document.lease_id.is_(None))
                        | (Document.heartbeat_at.is_(None))
                        | (Document.heartbeat_at < ingest_deadline),
                    ),
                    func.min(Document.created_at).filter(
                        Document.status == DocumentStatus.PENDING.value
                    ),
                ).select_from(Document)
            ).one()
            ingest_failed_at = conn.scalar(
                select(Document.updated_at)
                .where(Document.status == DocumentStatus.FAILED.value)
                .order_by(Document.updated_at.desc())
                .limit(1)
            )

            summary_pending, summary_processing, summary_stale, summary_oldest = conn.execute(
                select(
                    func.count().filter(SummaryJob.status == SummaryJobStatus.PENDING.value),
                    func.count().filter(SummaryJob.status == SummaryJobStatus.PROCESSING.value),
                    func.count().filter(
                        SummaryJob.status == SummaryJobStatus.PROCESSING.value,
                        (SummaryJob.heartbeat_at.is_(None))
                        | (SummaryJob.heartbeat_at < summary_deadline),
                    ),
                    func.min(SummaryJob.created_at).filter(
                        SummaryJob.status == SummaryJobStatus.PENDING.value
                    ),
                ).select_from(SummaryJob)
            ).one()
            summary_failure = conn.execute(
                select(SummaryJob.error_code, SummaryJob.updated_at)
                .where(SummaryJob.status == SummaryJobStatus.FAILED.value)
                .order_by(SummaryJob.updated_at.desc())
                .limit(1)
            ).first()

            project_pending, project_processing, project_stale, project_oldest = conn.execute(
                select(
                    func.count().filter(
                        ProjectSnapshot.status == ProjectSnapshotStatus.PENDING.value
                    ),
                    func.count().filter(
                        ProjectSnapshot.status == ProjectSnapshotStatus.PROCESSING.value
                    ),
                    func.count().filter(
                        ProjectSnapshot.status == ProjectSnapshotStatus.PROCESSING.value,
                        (ProjectSnapshot.heartbeat_at.is_(None))
                        | (ProjectSnapshot.heartbeat_at < project_deadline),
                    ),
                    func.min(ProjectSnapshot.created_at).filter(
                        ProjectSnapshot.status == ProjectSnapshotStatus.PENDING.value
                    ),
                ).select_from(ProjectSnapshot)
            ).one()
            project_failure = conn.execute(
                select(ProjectSnapshot.error_code, ProjectSnapshot.updated_at)
                .where(ProjectSnapshot.status == ProjectSnapshotStatus.FAILED.value)
                .order_by(ProjectSnapshot.updated_at.desc())
                .limit(1)
            ).first()

            regression_pending, regression_processing, regression_stale, regression_oldest = conn.execute(
                select(
                    func.count().filter(FeedbackRegressionJob.status == JOB_STATUS_QUEUED),
                    func.count().filter(FeedbackRegressionJob.status == JOB_STATUS_RUNNING),
                    func.count().filter(
                        FeedbackRegressionJob.status == JOB_STATUS_RUNNING,
                        (FeedbackRegressionJob.lease_expires_at.is_(None))
                        | (FeedbackRegressionJob.lease_expires_at <= now),
                    ),
                    func.min(FeedbackRegressionJob.created_at).filter(
                        FeedbackRegressionJob.status == JOB_STATUS_QUEUED
                    ),
                ).select_from(FeedbackRegressionJob)
            ).one()
            regression_failure = conn.execute(
                select(FeedbackRegressionJob.error_code, FeedbackRegressionJob.updated_at)
                .where(FeedbackRegressionJob.status == JOB_STATUS_FAILED)
                .order_by(FeedbackRegressionJob.updated_at.desc())
                .limit(1)
            ).first()

    heartbeats = _parse_heartbeat_rows(list(heartbeat_rows))
    failures = {
        WORKER_ROLE_INGEST: (
            {"updated_at": _aware(ingest_failed_at), "code": _FAILURE_CODE_INGEST}
            if ingest_failed_at is not None
            else None
        ),
        WORKER_ROLE_SUMMARY: (
            {
                "updated_at": _aware(summary_failure.updated_at),
                "code": _safe_failure_code(WORKER_ROLE_SUMMARY, summary_failure.error_code),
            }
            if summary_failure is not None
            else None
        ),
        WORKER_ROLE_PROJECT: (
            {
                "updated_at": _aware(project_failure.updated_at),
                "code": _safe_failure_code(WORKER_ROLE_PROJECT, project_failure.error_code),
            }
            if project_failure is not None
            else None
        ),
        WORKER_ROLE_REGRESSION: (
            {
                "updated_at": _aware(regression_failure.updated_at),
                "code": _safe_failure_code(WORKER_ROLE_REGRESSION, regression_failure.error_code),
            }
            if regression_failure is not None
            else None
        ),
    }
    task_stats = {
        WORKER_ROLE_INGEST: {
            "pending": int(ingest_pending or 0),
            "processing": int(ingest_processing or 0),
            "stale_tasks": int(ingest_stale or 0),
            "oldest_pending_age_seconds": _age_seconds(ingest_oldest),
        },
        WORKER_ROLE_SUMMARY: {
            "pending": int(summary_pending or 0),
            "processing": int(summary_processing or 0),
            "stale_tasks": int(summary_stale or 0),
            "oldest_pending_age_seconds": _age_seconds(summary_oldest),
        },
        WORKER_ROLE_PROJECT: {
            "pending": int(project_pending or 0),
            "processing": int(project_processing or 0),
            "stale_tasks": int(project_stale or 0),
            "oldest_pending_age_seconds": _age_seconds(project_oldest),
        },
        WORKER_ROLE_REGRESSION: {
            "pending": int(regression_pending or 0),
            "processing": int(regression_processing or 0),
            "stale_tasks": int(regression_stale or 0),
            "oldest_pending_age_seconds": _age_seconds(regression_oldest),
        },
    }
    roles = {}
    for role in WORKER_ROLES:
        role_heartbeat = heartbeats.get(role, {
            "alive_count": 0,
            "last_heartbeat_at": None,
            "heartbeat_total": 0,
            "unstopped_total": 0,
        })
        roles[role] = {**role_heartbeat, **task_stats[role], "last_failure": failures[role]}
    return {"status": "ok", "roles": roles}


def _unavailable_role(enabled: bool) -> dict:
    return {
        "enabled": enabled,
        "state": "unknown" if enabled else "disabled",
        "alive_count": None,
        "last_heartbeat_at": None,
        "pending": None,
        "processing": None,
        "stale_tasks": None,
        "oldest_pending_age_seconds": None,
        "last_failure": None,
    }


def build_worker_status(stats: dict) -> dict:
    """把查询结果整理为冻结契约结构；不可用时计数为 null 而不是 0。"""
    enabled = _enabled_roles()
    degraded = stats.get("status") != "ok"
    roles = []
    for role in WORKER_ROLES:
        is_enabled = enabled[role]
        if degraded:
            item = _unavailable_role(is_enabled)
        else:
            source = (stats.get("roles") or {}).get(role) or {
                "alive_count": 0,
                "last_heartbeat_at": None,
                "pending": 0,
                "processing": 0,
                "stale_tasks": 0,
                "oldest_pending_age_seconds": None,
                "last_failure": None,
            }
            item = {
                "enabled": is_enabled,
                "state": _state_for(is_enabled, source),
                "alive_count": int(source["alive_count"]),
                "last_heartbeat_at": source["last_heartbeat_at"],
                "pending": int(source["pending"]),
                "processing": int(source["processing"]),
                "stale_tasks": int(source["stale_tasks"]),
                "oldest_pending_age_seconds": source["oldest_pending_age_seconds"],
                "last_failure": source["last_failure"],
            }
        item["role"] = role
        roles.append(item)
    return {
        "generated_at": _now(),
        "stale_after_seconds": STALE_AFTER_SECONDS,
        "degraded": degraded,
        "roles": roles,
    }


async def collect_worker_status() -> dict:
    """带 anyio 超时的聚合入口；异常只返回 unavailable，不向调用方抛出。"""
    timeout = float(settings.OPERATIONS_DB_TIMEOUT_SECONDS)
    try:
        with anyio.fail_after(timeout):
            return await anyio.to_thread.run_sync(
                _collect_worker_stats, timeout, abandon_on_cancel=True
            )
    except TimeoutError:
        return {"status": "unavailable", "error": "timeout"}
    except Exception as exc:  # noqa: BLE001 - 只回错误类别，不回连接串/凭据
        logger.warning("worker 状态读取数据库失败: %s", type(exc).__name__)
        return {"status": "unavailable", "error": "query_failed"}
