"""项目源的**可选**定时自动同步调度（由 project worker 周期调用）。

职责边界（冻结契约）：
- 只调度“已显式开启自动同步、未撤销、到期”的源；API 请求路径绝不扫描目录。
- 在短事务内用 ``FOR UPDATE SKIP LOCKED`` 原子领取到期源，并在领取时推进
  ``next_check_at = now + interval``、``last_checked_at = now``；提交后才做入队，
  绝不长时间持有行锁。
- 入队复用 ``project_index.enqueue_project_sync`` 的源行锁幂等语义：同源已有
  pending/processing 时直接复用，不重复建活动任务；手动任务不会被改标为 auto。
- 单个源入队异常只记录**稳定安全码**与固定文案（绝不写绝对路径/文件正文），
  不中断其它源，也绝不杀死 worker supervisor。

安全：本模块不打开任何目录/文件；真正的扫描、快照与 CAS 发布仍完全复用
``project_index`` 中经 ``open_source_anchor``/``ProjectScanError`` 保护的路径。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.db.session import SessionLocal
from app.models import ProjectSource
from app.schemas.project import (
    AUTO_SYNC_DEFAULT_INTERVAL_SECONDS,
    AUTO_SYNC_MAX_INTERVAL_SECONDS,
    AUTO_SYNC_MIN_INTERVAL_SECONDS,
)
from app.services import project_index, project_sources

logger = logging.getLogger(__name__)

# 入队失败的稳定安全码与固定文案（不包含路径/正文/异常详情）。
ENQUEUE_ERROR_CODE = "auto-enqueue-failed"
ENQUEUE_ERROR_MESSAGE = "自动同步入队失败"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _clamp_interval(value) -> int:
    """把库内间隔收敛到合法硬界；脏数据不触发越界调度。"""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return AUTO_SYNC_DEFAULT_INTERVAL_SECONDS
    return max(
        AUTO_SYNC_MIN_INTERVAL_SECONDS,
        min(AUTO_SYNC_MAX_INTERVAL_SECONDS, seconds),
    )


def _claim_due_source_ids(limit: int) -> list[uuid.UUID]:
    """短事务领取到期源，提交后返回纯 id 列表（锁已释放）。"""
    with SessionLocal() as db:
        due = db.scalars(
            select(ProjectSource)
            .where(
                ProjectSource.revoked_at.is_(None),
                ProjectSource.auto_sync_enabled.is_(True),
                ProjectSource.auto_sync_next_check_at.is_not(None),
                ProjectSource.auto_sync_next_check_at <= _now(),
            )
            .order_by(ProjectSource.auto_sync_next_check_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).all()
        now = _now()
        source_ids: list[uuid.UUID] = []
        for source in due:
            # 领取即推进下次检查；即使后续入队失败也不会在同轮/紧接着反复重试。
            source.auto_sync_next_check_at = now + timedelta(
                seconds=_clamp_interval(source.auto_sync_interval_seconds)
            )
            source.auto_sync_last_checked_at = now
            source_ids.append(source.id)
        db.commit()
        return source_ids


def _record_enqueue_error(source_id: uuid.UUID) -> None:
    """记录安全的入队失败状态；回写失败只记日志，绝不上抛。"""
    try:
        with SessionLocal() as db:
            db.execute(
                update(ProjectSource)
                .where(
                    ProjectSource.id == source_id,
                    ProjectSource.revoked_at.is_(None),
                )
                .values(
                    auto_sync_last_error_code=ENQUEUE_ERROR_CODE,
                    auto_sync_last_error_message=ENQUEUE_ERROR_MESSAGE,
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001 - 不得因状态回写失败中断调度
        logger.exception("记录自动同步入队失败状态失败 sid=%s", source_id)


def _enqueue_one(source_id: uuid.UUID) -> bool:
    """入队一个源；成功（含复用活动任务）返回 True。

    入队成功**不清空**上次错误：任务只是刚入队，尚未 ready/unchanged；若随后
    被取消/撤销，上一次失败信息不能凭空消失。错误只由
    ``project_index._record_auto_source_result`` 在观察到 ready/unchanged 终态时
    清空（禁用时按契约清空）；入队失败仍记录 ``auto-enqueue-failed``。

    源在领取与入队之间被撤销/删除/关闭自动同步时静默跳过（不写错误、不入队）；
    其它异常记录稳定安全码与固定文案，绝不上抛给调用方。
    """
    try:
        with SessionLocal() as db:
            project_index.enqueue_project_sync(
                db,
                source_id,
                None,
                trigger_source="auto",
                # 与入队同一源行锁内复核开关，堵住“领取后、入队前被关闭”的窗口。
                require_auto_sync_enabled=True,
            )
            db.commit()
        return True
    except (
        project_sources.ProjectSourceNotFound,
        project_sources.ProjectSourceRevoked,
        project_sources.AutoSyncDisabled,
    ):
        # 领取后源被删除/撤销/关闭自动同步：无可入队，不算入队失败。
        return False
    except Exception:  # noqa: BLE001 - 单个源异常绝不中断其它源/进程
        logger.warning("自动同步入队异常 sid=%s", source_id, exc_info=True)
        _record_enqueue_error(source_id)
        return False


def enqueue_due_sources(limit: int = 1) -> int:
    """领取并入队至多 ``limit`` 个到期源，返回**成功入队/复用**的源数。

    - 禁用、已撤销、未到期的源不会被领取；
    - ``SKIP LOCKED`` 保证多个 worker 并发/重启时同源不会被重复领取；
    - 同源已有 pending/processing 时由 enqueue 幂等复用，不重复建活动任务。
    """
    if limit <= 0:
        return 0
    source_ids = _claim_due_source_ids(int(limit))
    enqueued = 0
    for source_id in source_ids:
        if _enqueue_one(source_id):
            enqueued += 1
    return enqueued
