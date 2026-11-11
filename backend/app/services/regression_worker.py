"""独立反馈回归 worker（--role regression）。

一个独立进程/线程只消费管理员**显式提交**的在线回归作业：

* 有界轮询 ``FEEDBACK_REGRESSION_JOB_POLL_SECONDS``，空闲不建任何模型调用；
* 周期性 ``recover`` 回收过期租约/超时排队作业（有界批大小）；
* 每次只领取一个作业，模型等待全部在短事务之外完成；
* 停止信号到达时协作结束当前生成、关闭上游流，并对仍有效的租约调用
  ``release`` 交回队列；租约已失效时绝不再写失败/成功。

日志边界：只输出稳定作业 ID、尝试次数、状态与冻结白名单错误码；绝不输出原
问题、回答、引用、模型原始异常或包含正文的 traceback。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import suppress

from app.core.config import settings
from app.db.session import SessionLocal
from app.services import async_models
from app.services import feedback_regression_executor as executor
from app.services import feedback_regression_jobs as jobs

logger = logging.getLogger(__name__)

# 回收过期租约/超时作业的周期（秒）；测试可缩短该模块常量。
RECOVER_INTERVAL_SECONDS = 30.0


def _poll_seconds() -> float:
    return max(0.05, float(settings.FEEDBACK_REGRESSION_JOB_POLL_SECONDS))


def _recover_once() -> int:
    """有界回收一批；返回实际推进的作业数。异常由调用方记录类别。"""
    with SessionLocal() as db:
        recovered = jobs.recover(db)
    if recovered:
        logger.info(
            "回收回归作业 count=%d",
            len(recovered),
        )
    return len(recovered)


def _claim_one() -> jobs.JobClaim | None:
    with SessionLocal() as db:
        return jobs.claim(db)


def _load(claim: jobs.JobClaim):
    with SessionLocal() as db:
        return jobs.load_execution(db, job_id=claim.job_id, lease_id=claim.lease_id)


def _complete(claim: jobs.JobClaim, outcome: executor.ExecutionOutcome):
    with SessionLocal() as db:
        return jobs.complete(
            db,
            job_id=claim.job_id,
            lease_id=claim.lease_id,
            answer=outcome.answer,
            citations=list(outcome.citations),
            trace=outcome.trace,
            supplied_sources=list(outcome.supplied_sources),
            history_sources=list(outcome.history_sources),
        )


def _fail(claim: jobs.JobClaim, error_code: str, retryable: bool):
    with SessionLocal() as db:
        return jobs.fail(
            db,
            job_id=claim.job_id,
            lease_id=claim.lease_id,
            error_code=error_code,
            retryable=retryable,
        )


def _release(claim: jobs.JobClaim):
    with SessionLocal() as db:
        return jobs.release(db, job_id=claim.job_id, lease_id=claim.lease_id)


def _safe_fail(claim: jobs.JobClaim, code: str, retryable: bool) -> None:
    try:
        transition = _fail(claim, code, retryable)
    except Exception as exc:  # noqa: BLE001 - 失败写入本身失败只记录类别
        logger.warning(
            "回归作业失败写入异常 job=%s err=%s", claim.job_id, type(exc).__name__
        )
        return
    logger.info(
        "回归作业失败 job=%s status=%s code=%s retryable=%s",
        claim.job_id,
        transition.status,
        code,
        bool(retryable),
    )


def _safe_release(claim: jobs.JobClaim) -> None:
    try:
        transition = _release(claim)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "回归作业交回租约异常 job=%s err=%s", claim.job_id, type(exc).__name__
        )
        return
    logger.info(
        "回归作业已交回 job=%s status=%s applied=%s",
        claim.job_id,
        transition.status,
        transition.applied,
    )


def _execute(loaded, stop_event: threading.Event) -> executor.ExecutionOutcome:
    """在独立事件循环中执行；结束后关闭本循环的异步模型连接池。"""

    async def runner():
        try:
            return await executor.execute(loaded, stop_event=stop_event)
        finally:
            with suppress(Exception):
                await async_models.close_async_clients()

    return asyncio.run(runner())


def _handle(claim: jobs.JobClaim, stop_event: threading.Event) -> None:
    logger.info(
        "领取回归作业 job=%s attempt=%s", claim.job_id, claim.attempt_count
    )
    try:
        loaded = _load(claim)
    except Exception as exc:  # noqa: BLE001 - 授权/绑定失败映射为安全白名单码
        code, retryable = executor.classify_failure(exc)
        if code is not None:
            _safe_fail(claim, code, retryable)
        else:
            logger.info("回归作业加载时租约已失效 job=%s", claim.job_id)
        return

    try:
        outcome = _execute(loaded, stop_event)
    except executor.ExecutionStopped:
        _safe_release(claim)
        return
    except executor.ExecutionLeaseLost as exc:
        # 取消/过期/期限：租约已不可用，绝不写 fail/complete；由 recover 或取消方推进。
        logger.info(
            "回归作业租约失效 job=%s reason=%s", claim.job_id, exc.reason
        )
        return
    except Exception as exc:  # noqa: BLE001 - 只记录类别与白名单码
        code, retryable = executor.classify_failure(exc)
        if code is not None:
            _safe_fail(claim, code, retryable)
        return

    try:
        completion = _complete(claim, outcome)
    except jobs.JobError as exc:
        code, retryable = executor.classify_failure(exc)
        if code is not None:
            _safe_fail(claim, code, retryable)
        else:
            logger.info("回归作业发布时租约已失效 job=%s", claim.job_id)
        return
    except Exception as exc:  # noqa: BLE001
        code, retryable = executor.classify_failure(exc)
        if code is not None:
            _safe_fail(claim, code, retryable)
        return
    logger.info(
        "回归作业发布完成 job=%s passed=%s citations=%d",
        completion.job_id,
        completion.passed,
        completion.citation_count,
    )


def run(stop_event: threading.Event) -> int:
    """角色入口：固定契约 ``run(stop_event)``；正常停止返回 0。"""
    logger.info("regression worker 启动")
    last_recover = 0.0
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_recover >= RECOVER_INTERVAL_SECONDS:
                last_recover = now
                try:
                    _recover_once()
                except Exception as exc:  # noqa: BLE001 - 单次回收失败不退出角色
                    logger.warning("回收回归作业失败 err=%s", type(exc).__name__)
            if stop_event.is_set():
                break
            try:
                claim = _claim_one()
            except Exception as exc:  # noqa: BLE001 - 数据库抖动按轮询退避重试
                logger.warning("领取回归作业失败 err=%s", type(exc).__name__)
                claim = None
                if stop_event.wait(_poll_seconds()):
                    break
                continue
            if claim is None:
                # 可被信号立即唤醒，不必等满轮询间隔。
                if stop_event.wait(_poll_seconds()):
                    break
                continue
            if stop_event.is_set():
                # 领取与停机竞态：还没开始执行，直接交回。
                _safe_release(claim)
                break
            _handle(claim, stop_event)
        return 0
    finally:
        logger.info("regression worker 已退出")


__all__ = ["RECOVER_INTERVAL_SECONDS", "run"]
