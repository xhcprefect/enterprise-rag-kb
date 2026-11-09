"""项目同步 worker（Q 独占，角色 project）。

由 ``worker.py`` 在选中 ``--role project`` 时动态 import 并调用 ``run(stop_event)``
（模块级必须有 ``run(stop_event)`` 签名）。supervisor 循环只调用 aa0f 在
``project_index`` 落地的真实函数，不自造接口：

- ``recover_stale_jobs()``              启动/周期恢复超时 processing -> pending（保检查点）
- ``claim_next_project_snapshot()``     领取一个 pending 快照（源行锁→快照行锁，租约+心跳）
- ``run_project_sync(claim, stop)``     处理一个已领取的 Claim；内部协作停机回 pending、用户取消区分

单任务处理异常绝不杀死 supervisor：记录后继续，交由 ``recover_stale_jobs`` 在租约
超时后回收，避免把整进程拖垮或被一个坏任务卡住。停机由 ``stop_event`` 协作，空闲用
``stop_event.wait`` 可立即唤醒。
"""
from __future__ import annotations

import logging
import threading
import time

from app.core.config import settings

logger = logging.getLogger("project_worker")

RECOVER_INTERVAL_SECONDS = 30
# 到期自动同步源的最多轮询频率（秒）：每轮最多入队 1 个源；扫描/解析仍完全在
# project worker 既有同步路径内完成，绝不在 API 请求路径触发目录扫描。
AUTO_SYNC_POLL_SECONDS = 5


def _poll_seconds() -> float:
    try:
        return max(0.5, float(settings.PROJECT_POLL_SECONDS))
    except Exception:  # noqa: BLE001 - 配置异常时用保守默认
        return 2.0


def run(stop_event: threading.Event) -> None:
    """project 角色 supervisor 循环：恢复 -> 自动调度 -> 领取 -> 处理，协作停机。"""
    # 动态 import 放在函数内：只在选中本项目角色时才 import project_index，
    # worker.py 的启动路径保证本模块只在 project 角色被加载，缺依赖不影响其他角色。
    from app.services import project_auto_sync, project_index

    logger.info("project worker 启动")
    last_recover = 0.0
    last_auto_sync = float("-inf")
    poll = _poll_seconds()
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_recover > RECOVER_INTERVAL_SECONDS:
            last_recover = now
            try:
                project_index.recover_stale_jobs()
            except Exception:  # noqa: BLE001
                logger.exception("项目任务恢复扫描失败")

        # 到期自动同步入队：每轮最多一次（≤ 每 5 秒），异常只记录不杀 supervisor。
        if now - last_auto_sync >= AUTO_SYNC_POLL_SECONDS:
            last_auto_sync = now
            try:
                project_auto_sync.enqueue_due_sources(limit=1)
            except Exception:  # noqa: BLE001 - 调度异常不得中断 worker
                logger.exception("自动同步调度异常")

        snap = None
        try:
            snap = project_index.claim_next_project_snapshot()
        except Exception:  # noqa: BLE001
            logger.exception("项目领取任务异常")
            if stop_event.wait(poll):
                break
            continue
        if snap is None:
            if stop_event.wait(poll):
                break
            continue

        # 已领取。run_project_sync 内部在 stop_event 置位时协作回退 pending（保检查点），
        # 与用户取消（cancel_requested）区分；因此即使此刻已停机也照常调用。
        # claim_next_project_snapshot 返回不可变 Claim(snapshot_id, source_id, lease_id)；
        # run_project_sync 只接受 Claim（含原始 lease_id，防旧租约被新租约顶替）。
        try:
            project_index.run_project_sync(snap, stop_event)
        except Exception:  # noqa: BLE001 - 单任务异常绝不杀死 supervisor
            logger.exception("项目任务处理异常 sid=%s", snap.snapshot_id)
            # 交给 recover_stale_jobs 在租约超时后回收；不在此私自改状态。
    logger.info("project worker 已退出")
