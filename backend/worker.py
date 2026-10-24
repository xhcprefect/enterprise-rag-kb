"""独立入库 worker：从数据库领取 pending 文档并处理。

为什么不放在 API 进程里（旧的 BackgroundTasks 方案）：
- uvicorn 重启 / reload 会直接杀掉线程，文档永远停在 processing；
- 进程内任务无法限流，多个请求会一起压垮模型服务（本地 Ollama 或云端配额）。

领取方式：``SELECT ... FOR UPDATE SKIP LOCKED``，天然支持多开 worker 并发消费。
启动与空闲时都会做一次恢复扫描，把"心跳超时的 processing"退回 pending 续传。
"""
import logging
import signal
import sys
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

setup_logging("worker")
logger = logging.getLogger("worker")

IDLE_POLL_SECONDS = 2
CLEANUP_INTERVAL_SECONDS = 600  # 过期上传会话清理间隔

_running = True


def _stop(signum, frame):  # noqa: ANN001, ARG001 - signal handler 签名固定
    global _running
    _running = False
    logger.info("收到退出信号 %s，当前任务结束后停止", signum)


def recover_stale(db: Session) -> int:
    """把心跳超时的 processing 文档退回 pending，续传时保留已入库的分块。"""
    deadline = datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS)
    result = db.execute(
        update(Document)
        .where(
            Document.status == DocumentStatus.PROCESSING.value,
            (Document.heartbeat_at.is_(None)) | (Document.heartbeat_at < deadline),
        )
        .values(status=DocumentStatus.PENDING.value, heartbeat_at=None)
    )
    db.commit()
    if result.rowcount:
        logger.warning("恢复 %d 个中断的入库任务，回到队列续传", result.rowcount)
    return result.rowcount or 0


def claim_one(db: Session) -> uuid.UUID | None:
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

    result = db.execute(
        update(Document)
        .where(Document.id == doc_id)
        .values(
            status=DocumentStatus.PROCESSING.value,
            heartbeat_at=datetime.now(timezone.utc),
        )
    )
    db.commit()
    return doc_id if result.rowcount else None


def main() -> int:
    # 不建管理员：那是 API 的职责。worker 只保证表结构就绪，否则两个容器并发启动时
    # worker 可能抢先用默认密码建出 admin，让 .env 里配置的密码永远不生效。
    init_db(create_admin=False)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "worker 启动：batch=%d, stale=%ds, chunk=%d",
        settings.INGEST_BATCH_SIZE, settings.INGEST_STALE_SECONDS, settings.CHUNK_SIZE,
    )

    last_cleanup = 0.0
    with SessionLocal() as db:
        recover_stale(db)

    while _running:
        now = time.monotonic()
        if now - last_cleanup > CLEANUP_INTERVAL_SECONDS:
            last_cleanup = now
            try:
                with SessionLocal() as db:
                    recover_stale(db)
                    cleanup_expired(db)
            except Exception:  # noqa: BLE001 - 清理失败不应影响主循环
                logger.exception("周期清理失败")

        with SessionLocal() as db:
            doc_id = claim_one(db)

        if doc_id is None:
            time.sleep(IDLE_POLL_SECONDS)
            continue

        logger.info("领取任务: %s", doc_id)
        try:
            ingest_document_task(str(doc_id))
        except Exception:  # noqa: BLE001 - ingest 内部已兜底，这里只保 worker 存活
            logger.exception("任务异常退出: %s", doc_id)

    logger.info("worker 已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
