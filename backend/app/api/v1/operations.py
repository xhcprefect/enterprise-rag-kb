"""运维观测接口：Prometheus 文本采集 + 管理员结构化摘要。

两个接口的鉴权边界必须分开：

- ``/operations/metrics`` 只认独立的 ``METRICS_TOKEN``（给 Prometheus），
  空令牌时采集整体关闭；管理员 JWT 不能替代采集令牌。
- ``/operations/summary`` 只认管理员 JWT，返回统一 JSON envelope。

所有输出都刻意不含模型地址、密钥、文档名、用户名或原始 UUID；指标本身是
进程内低基数计数，多副本不会自动合并（``ragkb_metrics_process_local`` 恒为 1
就是在注记这一点）。
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta, timezone

import anyio.to_thread
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import func, select, text

from app.api.deps import bearer_scheme, require_admin
from app.core.config import settings
from app.core.observability import CONTENT_TYPE, metrics
from app.db import session as db_session
from app.models import Document, DocumentStatus
from app.schemas.common import ApiResponse
from app.schemas.operations import WorkersStatusRead
from app.services import worker_status as worker_status_service

router = APIRouter(prefix="/operations", tags=["运维"])
logger = logging.getLogger(__name__)

_DISABLED_BODY = "metrics collection is disabled\n"


def _check_metrics_token(credentials: HTTPAuthorizationCredentials | None) -> None:
    expected = settings.METRICS_TOKEN.strip()
    if not expected:
        # 未配置即视为关闭：返回 503，而不是落到"无令牌也可读"。
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="指标采集未启用",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少采集令牌",
            headers={"WWW-Authenticate": "Bearer"},
        )
    supplied = credentials.credentials
    # 常数时间比较；管理员 JWT 与采集令牌不是同一凭据，不会相等。
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="采集令牌无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _render_worker_metrics(worker_payload: dict) -> list[str]:
    """固定 role 的 worker 存活/队列指标。

    - ``ragkb_worker_enabled`` 只来自 API 配置，任何数据库状态下都输出；
    - ``ragkb_workers_available`` 标明本次角色观测是否成功；
    - 数据库失败/超时时，存活与队列指标整体省略——未知绝不能伪装成 0，
      否则“采不到”会被告警读成“队列为空/进程已停”；
    - 最近失败只输出白名单安全码与时间戳，不含原始异常/路径/UUID。
    """
    role_lines: list[str] = [
        "# HELP ragkb_worker_enabled Whether this worker role is enabled by the API configuration (1/0).",
        "# TYPE ragkb_worker_enabled gauge",
    ]
    roles = worker_payload.get("roles") or []
    for item in roles:
        role_lines.append(
            f'ragkb_worker_enabled{{role="{item["role"]}"}} {1 if item["enabled"] else 0}'
        )

    lines = role_lines + [
        "# HELP ragkb_workers_available Whether the worker heartbeat/queue observation query succeeded in this scrape.",
        "# TYPE ragkb_workers_available gauge",
    ]
    degraded = bool(worker_payload.get("degraded"))
    lines.append(f"ragkb_workers_available {0 if degraded else 1}")
    if degraded:
        return lines

    lines += [
        "# HELP ragkb_worker_alive Fresh worker instances (heartbeat within 45s).",
        "# TYPE ragkb_worker_alive gauge",
    ]
    for item in roles:
        lines.append(f'ragkb_worker_alive{{role="{item["role"]}"}} {int(item["alive_count"] or 0)}')
    for field, name in (
        ("pending", "ragkb_worker_pending_tasks"),
        ("processing", "ragkb_worker_processing_tasks"),
        ("stale_tasks", "ragkb_worker_stale_tasks"),
    ):
        lines += [
            f"# HELP {name} Worker queue tasks by role ({field.replace('_', ' ')}).",
            f"# TYPE {name} gauge",
        ]
        for item in roles:
            lines.append(f'{name}{{role="{item["role"]}"}} {int(item[field] or 0)}')
    lines += [
        "# HELP ragkb_worker_oldest_pending_age_seconds Age of the oldest pending task by role (omitted when no pending task).",
        "# TYPE ragkb_worker_oldest_pending_age_seconds gauge",
    ]
    for item in roles:
        age = item.get("oldest_pending_age_seconds")
        if age is not None:
            lines.append(
                f'ragkb_worker_oldest_pending_age_seconds{{role="{item["role"]}"}} {float(age):.3f}'
            )
    lines += [
        "# HELP ragkb_worker_last_failure_timestamp_seconds Update time of the latest failed task by role (omitted when none).",
        "# TYPE ragkb_worker_last_failure_timestamp_seconds gauge",
        "# HELP ragkb_worker_last_failure_info Latest failed task code by role, restricted to a fixed safe allowlist.",
        "# TYPE ragkb_worker_last_failure_info gauge",
    ]
    for item in roles:
        failure = item.get("last_failure")
        if not failure:
            continue
        updated_at = failure["updated_at"]
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        code = failure["code"]
        lines.append(
            f'ragkb_worker_last_failure_timestamp_seconds{{role="{item["role"]}"}} '
            f"{updated_at.timestamp():.3f}"
        )
        lines.append(
            f'ragkb_worker_last_failure_info{{role="{item["role"]}",code="{_metric_label(code)}"}} 1'
        )
    return lines


def _metric_label(value: object) -> str:
    """标签只允许白名单稳定码；其余字符一律去除，绝不回显原始文本。"""
    text_value = str(value or "")
    return "".join(ch for ch in text_value if ch.isascii() and (ch.isalnum() or ch in "-_"))[:64] or "failed"


@router.get("/metrics", include_in_schema=False)
async def get_metrics(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
):
    """Prometheus 兼容文本；空 METRICS_TOKEN 时整体不可用。

    鉴权先于任何数据库读取：未授权请求不会触发队列查询，避免采集端故障时
    反过来压垮数据库。
    """
    if not settings.METRICS_TOKEN.strip():
        return PlainTextResponse(_DISABLED_BODY, status_code=503)
    _check_metrics_token(credentials)
    # 复用管理员摘要的有界查询（anyio 超时 + statement_timeout），
    # 让队列/心跳/连接池也能被 Prometheus 直接告警。
    database = await _database_summary()
    # worker 角色观测与队列摘要是**两次独立查询**：任一方失败都不影响另一方，
    # 也绝不能让失败被渲染成 0（见 _render_worker_metrics）。
    try:
        workers = await worker_status_service.collect_worker_status()
    except Exception as exc:  # noqa: BLE001 - worker 观测异常不得破坏现有采集
        logger.warning("worker 指标采集失败: %s", type(exc).__name__)
        workers = {"status": "unavailable", "error": "query_failed"}
    worker_payload = worker_status_service.build_worker_status(workers)
    base = metrics.render()
    extra = _render_database_metrics(database)
    extra += _render_worker_metrics(worker_payload)
    body = base + "".join(f"{line}\n" for line in extra)
    return Response(content=body, media_type=CONTENT_TYPE)


# ---------------------------------------------------------------------------
# 管理员摘要
# ---------------------------------------------------------------------------

_POOL_FIELDS = ("size", "checkedin", "checkedout", "overflow", "total")

# Prometheus 导出的队列/连接池 gauge：只来自固定集合，基数恒定。
_POOL_METRICS = {
    "size": "ragkb_db_pool_size",
    "checkedin": "ragkb_db_pool_checked_in",
    "checkedout": "ragkb_db_pool_checked_out",
    "overflow": "ragkb_db_pool_overflow",
    "total": "ragkb_db_pool_total",
    "capacity": "ragkb_db_pool_capacity",
}


def _render_database_metrics(database: dict) -> list[str]:
    """把有界数据库摘要转成 Prometheus 文本。

    - ``ragkb_db_available`` 恒输出（1/0），数据库不可用时其余队列指标整体省略：
      未知的队列长度绝不能伪装成 0，否则告警会把"采不到"当成"队列为空"。
    - 连接池来自本地 engine，即使查询失败也仍然有意义，因此可独立输出。
    """
    lines = [
        "# HELP ragkb_db_available Whether the operations database summary query succeeded in this scrape.",
        "# TYPE ragkb_db_available gauge",
    ]
    ok = database.get("status") == "ok"
    lines.append(f"ragkb_db_available {1 if ok else 0}")

    if ok:
        statuses = database.get("statuses") or {}
        lines += [
            "# HELP ragkb_ingest_documents Documents by ingest status.",
            "# TYPE ragkb_ingest_documents gauge",
        ]
        for item in DocumentStatus:
            # 五个状态总是输出（含 0），告警规则可以直接写 >0 而不必担心缺失。
            lines.append(
                f'ragkb_ingest_documents{{status="{item.value}"}} {int(statuses.get(item.value, 0))}'
            )
        lines += [
            "# HELP ragkb_ingest_queued_documents Documents queued for ingest (pending + processing).",
            "# TYPE ragkb_ingest_queued_documents gauge",
        ]
        lines.append(f"ragkb_ingest_queued_documents {int(database.get('queued') or 0)}")
        lines += [
            "# HELP ragkb_ingest_stale_processing_documents Processing documents whose heartbeat is missing or expired.",
            "# TYPE ragkb_ingest_stale_processing_documents gauge",
        ]
        lines.append(
            f"ragkb_ingest_stale_processing_documents {int(database.get('stale_processing') or 0)}"
        )
        oldest = database.get("oldest_pending_age_seconds")
        if oldest is not None:
            lines += [
                "# HELP ragkb_ingest_oldest_pending_age_seconds Age of the oldest pending document.",
                "# TYPE ragkb_ingest_oldest_pending_age_seconds gauge",
            ]
            lines.append(f"ragkb_ingest_oldest_pending_age_seconds {float(oldest):.3f}")

    pool = database.get("pool") or {}
    for field, name in _POOL_METRICS.items():
        value = pool.get(field)
        if value is None:
            continue
        # SQLAlchemy 的 overflow 在回收空余连接时可能为负，表示的"超出基础池的
        # 活跃连接"不会小于 0，这里夹到 0 以免告警阈值误判。
        number = max(0, int(value)) if field == "overflow" else int(value)
        lines.append(f"# HELP {name} SQLAlchemy connection pool {field.replace('_', ' ')}.")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {number}")
    return lines


def _pool_stats() -> dict:
    pool = getattr(db_session.engine, "pool", None)
    stats: dict[str, int | None] = {name: None for name in _POOL_FIELDS}
    if pool is None:
        return stats
    for name in _POOL_FIELDS:
        method = getattr(pool, name, None)
        if callable(method):
            try:
                stats[name] = int(method())
            except Exception:  # noqa: BLE001 - 池实现差异不影响摘要其余部分
                stats[name] = None
    max_overflow = getattr(pool, "_max_overflow", None)
    if isinstance(max_overflow, int) and stats["size"] is not None:
        stats["capacity"] = stats["size"] + max_overflow
    else:
        stats["capacity"] = None
    return stats


def _collect_db_stats(timeout_seconds: float) -> dict:
    """在一个独立连接上取队列/心跳统计；语句级超时保证数据库自身也会放弃。"""
    deadline = datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS)
    statuses = {item.value: 0 for item in DocumentStatus}
    with db_session.engine.connect() as conn:
        # SET LOCAL 只在当前事务内生效，连接归还池后自动恢复；值只来自受控整数。
        with conn.begin():
            conn.execute(
                text(f"SET LOCAL statement_timeout = {max(1, int(timeout_seconds * 1000))}")
            )
            rows = conn.execute(
                select(Document.status, func.count()).group_by(Document.status)
            ).all()
            for name, count in rows:
                if name in statuses:
                    statuses[name] = int(count)
            queued = conn.scalar(
                select(func.count()).select_from(Document).where(
                    Document.status.in_(
                        [DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value]
                    )
                )
            ) or 0
            stale = conn.scalar(
                select(func.count()).select_from(Document).where(
                    Document.status == DocumentStatus.PROCESSING.value,
                    (Document.heartbeat_at.is_(None)) | (Document.heartbeat_at < deadline),
                )
            ) or 0
            oldest_pending = conn.scalar(
                select(func.min(Document.created_at)).where(
                    Document.status == DocumentStatus.PENDING.value
                )
            )
    oldest_age = None
    if oldest_pending is not None:
        if oldest_pending.tzinfo is None:
            oldest_pending = oldest_pending.replace(tzinfo=timezone.utc)
        oldest_age = max(0.0, (datetime.now(timezone.utc) - oldest_pending).total_seconds())
    return {
        "status": "ok",
        "statuses": statuses,
        "queued": int(queued),
        "stale_processing": int(stale),
        "oldest_pending_age_seconds": oldest_age,
        "pool": _pool_stats(),
    }


def _queue_summary_from_counts(counts: dict) -> str:
    queued = counts.get(DocumentStatus.PENDING.value, 0) + counts.get(
        DocumentStatus.PROCESSING.value, 0
    )
    if counts.get(DocumentStatus.PROCESSING.value, 0):
        return "busy"
    return "idle" if not queued else "waiting"


async def _database_summary() -> dict:
    timeout = float(settings.OPERATIONS_DB_TIMEOUT_SECONDS)
    try:
        # abandon_on_cancel=True：超时后立即返回，不等待仍卡在数据库调用里的线程；
        # 同时 SET LOCAL statement_timeout 会让数据库自身在超时后放弃该语句。
        with anyio.fail_after(timeout):
            return await anyio.to_thread.run_sync(_collect_db_stats, timeout, abandon_on_cancel=True)
    except TimeoutError:
        # 超时时明确报告不可用；绝不返回全零伪装成"队列为空"。
        return {"status": "unavailable", "error": "timeout", "pool": _pool_stats()}
    except Exception as exc:  # noqa: BLE001 - 只回错误类别，不回连接串/凭据
        logger.warning("运维摘要读取数据库失败: %s", type(exc).__name__)
        return {
            "status": "unavailable",
            "error": "query_failed",
            "error_type": type(exc).__name__,
            "pool": _pool_stats(),
        }


@router.get(
    "/workers",
    response_model=ApiResponse[WorkersStatusRead],
    summary="worker 角色心跳与队列状态（管理员）",
)
async def get_workers(response: Response, _admin=Depends(require_admin)):
    """固定 ingest/summary/project/regression 顺序的实例心跳与任务队列观测。

    鉴权由 ``require_admin`` 先于任何数据库读取执行；``no-store`` 避免中间缓存
    把过期的运维状态当作当前状态返回。查询失败时 ``degraded=true`` 且计数为
    ``null``，绝不补零。
    """
    try:
        stats = await worker_status_service.collect_worker_status()
    except Exception as exc:  # noqa: BLE001 - 只回安全错误类别，不回连接串/凭据
        logger.warning("worker 状态接口查询失败: %s", type(exc).__name__)
        stats = {"status": "unavailable", "error": "query_failed"}
    payload = worker_status_service.build_worker_status(stats)
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=payload, message="ok")


@router.get("/summary", response_model=ApiResponse[dict], summary="管理员运维摘要")
async def get_summary(_admin=Depends(require_admin)):
    """进程内指标 + 队列/心跳 + 数据库连接池；不暴露任何业务明细。"""
    database = await _database_summary()
    snapshot = metrics.snapshot()
    chat, http = snapshot["chat"], snapshot["http"]
    # 审计失败只暴露进程内累计次数（无标签、无业务数据），与 /metrics 同源。
    audit = snapshot["audit"]
    if database.get("status") == "ok":
        queue = {
            "statuses": database["statuses"],
            "queued": database["queued"],
            "stale_processing": database["stale_processing"],
            "oldest_pending_age_seconds": database["oldest_pending_age_seconds"],
            "state": _queue_summary_from_counts(database["statuses"]),
        }
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "process_local": True,
            "chat": chat,
            "http": http,
            "audit": audit,
            "queue": queue,
            "database": {"status": "ok", "pool": database.get("pool")},
            "degraded": False,
        }
    else:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "process_local": True,
            "chat": chat,
            "http": http,
            "audit": audit,
            "queue": None,
            "database": {
                "status": "unavailable",
                "error": database.get("error"),
                "error_type": database.get("error_type"),
                "pool": database.get("pool"),
            },
            "degraded": True,
        }
    return ApiResponse(data=payload, message="ok")
