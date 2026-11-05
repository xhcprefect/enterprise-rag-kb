"""独立 worker：按角色消费数据库任务。

角色（--role，默认 ingest 与旧行为完全一致）：
- ingest    ：上传文档入库（原 worker 行为，KEEP 原有 graceful/stale/checkpoint）。
- summary   ：普通对话摘要压缩（独立启动，避免与长文档入库互饿）。
- project   ：本地项目文档/源码索引与手动增量同步（独立启动）。

summary/project 角色启动后按需动态 import 各自服务 worker 并调用
``run(stop_event)``——只有被选中的角色才被 import，因此任一角色的服务模块缺依赖
（例如暂未装 tree-sitter）都不会影响其他角色。Docker Compose 通过 summary/project
可选 profile 启动对应服务；本地开发也需为每个启用的角色单独启动进程。

为什么入库不在 API 进程里（旧的 BackgroundTasks 方案）：
- uvicorn 重启 / reload 会直接杀掉线程，文档永远停在 processing；
- 进程内任务无法限流，多个请求会一起压垮模型服务（本地 Ollama 或云端配额）。

领取方式：``SELECT ... FOR UPDATE SKIP LOCKED``，天然支持多开 worker 并发消费。
启动与空闲时都会做一次恢复扫描，把"心跳超时的 processing"退回 pending 续传。

停机（SIGTERM/SIGINT）：
- signal handler 只设置 ``stop_event``，不做数据库、日志等可能阻塞的重活；
- 空闲等待用 ``stop_event.wait``，信号可以立即唤醒并退出；
- 当前任务通过 ``stop_event`` 在下一个安全点协作停下，不等待整份大文档；
- 已经领取的租约会在任务内交回 pending，随后进程才退出。
"""
import argparse
import importlib
import logging
import signal
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.models import Document, DocumentStatus
from app.services.ingest import ingest_document_task
from app.services.upload import cleanup_expired

logger = logging.getLogger("worker")

IDLE_POLL_SECONDS = 2
RECOVER_INTERVAL_SECONDS = 30   # stale processing 恢复扫描；与上传清理互相独立
CLEANUP_INTERVAL_SECONDS = 600  # 过期上传会话清理间隔

# 停机信号只置位该事件；任务与主循环都通过它协作退出。
stop_event = threading.Event()

# 兼容旧导入（``worker._running``）的只读视图，不再是唯一退出条件。
_running = True

# 角色常量与 supervisor 分发。summary/project 角色只有在被选中时才动态 import
# 各自的服务 worker，从而与旧 ingest 行为、单角色独立启动完全解耦。
ROLE_INGEST = "ingest"
ROLE_SUMMARY = "summary"
ROLE_PROJECT = "project"
ROLES = (ROLE_INGEST, ROLE_SUMMARY, ROLE_PROJECT)
# role -> (package, module)，每个独立启动的 supervisor 调用其 ``run(stop_event)``。
_ROLE_WORKERS = {
    ROLE_SUMMARY: ("app.services", "memory_worker"),
    ROLE_PROJECT: ("app.services", "project_worker"),
}


def parse_role(argv: list[str] | None = None) -> str:
    """解析 --role；缺省为 ingest，旧启动方式（``python worker.py``）完全兼容。"""
    parser = argparse.ArgumentParser(prog="worker", add_help=True)
    parser.add_argument(
        "--role",
        choices=ROLES,
        default=ROLE_INGEST,
        help="worker 角色：ingest(默认) / summary / project",
    )
    args = parser.parse_args(argv)
    return args.role


def _import_role_worker(role: str):  # noqa: ANN202 - 返回被 import 的模块对象
    """只在被选中的角色处触发 import，避免无关依赖缺包影响其他角色。"""
    package, module = _ROLE_WORKERS[role]
    return importlib.import_module(f"{package}.{module}")


def _run_role(role: str) -> int:
    """summary/project 角色：独立 supervisor，转发 stop_event 并调用服务的 run()。"""
    setup_logging(f"worker-{role}")
    # 不建管理员：那是 API 的职责。worker 只保证表结构就绪。
    init_db(create_admin=False)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    logger.info("%s worker 启动", role)
    module = _import_role_worker(role)
    module.run(stop_event)
    logger.info("%s worker 已退出", role)
    return 0


def _stop(signum, frame):  # noqa: ANN001, ARG001 - signal handler 签名固定
    # signal handler 内不做日志/数据库等可能阻塞的操作（logging 也可能持锁）。
    global _running
    _running = False
    stop_event.set()


def recover_stale(db: Session) -> int:
    """把心跳超时的 processing 文档退回 pending，续传时保留已入库的分块。"""
    deadline = datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS)
    result = db.execute(
        update(Document)
        .where(
            Document.status == DocumentStatus.PROCESSING.value,
            (Document.lease_id.is_(None)) | (Document.heartbeat_at.is_(None)) | (Document.heartbeat_at < deadline),
        )
        .values(status=DocumentStatus.PENDING.value, heartbeat_at=None, lease_id=None)
    )
    db.commit()
    if result.rowcount:
        logger.warning("恢复 %d 个中断的入库任务，回到队列续传", result.rowcount)
    return result.rowcount or 0


def claim_one(db: Session) -> tuple[uuid.UUID, uuid.UUID] | None:
    """原子领取一个待处理文档。"""
    doc_id = db.scalar(
        select(Document.id)
        .where(Document.status == DocumentStatus.PENDING.value)
        .order_by(Document.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if doc_id is None:
        db.rollback()
        return None

    lease_id = uuid.uuid4()
    result = db.execute(
        update(Document)
        .where(Document.id == doc_id)
        .values(
            status=DocumentStatus.PROCESSING.value,
            lease_id=lease_id,
            heartbeat_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return (doc_id, lease_id) if result.rowcount else None


def main(argv: list[str] | None = None) -> int:
    """角色入口。默认 ingest；summary/project 显式 --role 独立启动。"""
    role = parse_role(argv)
    if role == ROLE_INGEST:
        return _ingest_main()
    return _run_role(role)


def _ingest_main() -> int:
    setup_logging("worker")
    # 不建管理员：那是 API 的职责。worker 只保证表结构就绪，否则两个容器并发启动时
    # worker 可能抢先用默认密码建出 admin，让 .env 里配置的密码永远不生效。
    init_db(create_admin=False)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "worker 启动：batch=%d, stale=%ds, chunk=%d",
        settings.INGEST_BATCH_SIZE, settings.INGEST_STALE_SECONDS, settings.CHUNK_SIZE,
    )

    last_recover = last_cleanup = 0.0
    with SessionLocal() as db:
        recover_stale(db)

    while not stop_event.is_set():
        now = time.monotonic()
        # 恢复扫描与上传清理拆开：stale 判定要更频繁，上传会话保留 24h，无需同步跟跑。
        if now - last_recover > RECOVER_INTERVAL_SECONDS:
            last_recover = now
            try:
                with SessionLocal() as db:
                    recover_stale(db)
            except Exception:  # noqa: BLE001 - 单次清理失败不应影响主循环
                logger.exception("周期恢复扫描失败")
        if now - last_cleanup > CLEANUP_INTERVAL_SECONDS:
            last_cleanup = now
            try:
                with SessionLocal() as db:
                    cleanup_expired(db)
            except Exception:  # noqa: BLE001
                logger.exception("周期上传清理失败")

        with SessionLocal() as db:
            claimed = claim_one(db)

        if claimed is None:
            # 可被信号立即唤醒，不必等满 IDLE_POLL_SECONDS。
            if stop_event.wait(IDLE_POLL_SECONDS):
                break
            continue

        doc_id, lease_id = claimed
        if stop_event.is_set():
            # 领取与停机竞态：刚领到但还没开始处理，直接交回 pending，避免留下
            # 一个只有本进程知道租约的 processing 文档。
            with SessionLocal() as db:
                _release(db, doc_id, lease_id)
            break
        logger.info("领取任务: %s", doc_id)
        try:
            ingest_document_task(str(doc_id), lease_id, stop_event=stop_event)
        except Exception:  # noqa: BLE001 - ingest 内部已兜底，这里只保 worker 存活
            logger.exception("任务异常退出: %s", doc_id)

    logger.info("worker 已退出")
    return 0


def _release(db: Session, doc_id: uuid.UUID, lease_id: uuid.UUID) -> None:
    """把刚领取、尚未处理的任务按租约条件退回 pending。"""
    try:
        result = db.execute(
            update(Document)
            .where(Document.id == doc_id, Document.lease_id == lease_id,
                   Document.status == DocumentStatus.PROCESSING.value)
            .values(status=DocumentStatus.PENDING.value, lease_id=None, heartbeat_at=None)
        )
        db.commit()
        if result.rowcount:
            logger.info("停机竞态：任务已交回 pending: %s", doc_id)
    except Exception:  # noqa: BLE001 - 交回失败时留给 stale 扫描兜底
        db.rollback()
        logger.warning("停机时交回任务失败: %s", doc_id, exc_info=True)


if __name__ == "__main__":
    sys.exit(main())
