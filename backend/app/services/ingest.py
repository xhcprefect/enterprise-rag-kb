"""文档入库：解析 -> 切分落库 -> 分批向量化（可中断、可续传）。

与旧实现的区别：
- 切分结果先写入 ``document_chunks``，向量化按 ``embedded`` 标志推进；
- 进程中断后只重跑未完成的分块，**不重新解析源文件**；
- 向量使用确定性 ID ``"{document_id}:{chunk_index}"``，重复写入是 UPSERT，不会产生重复行。
"""
import logging
import time
import uuid
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Document, DocumentChunk, DocumentStatus, KnowledgeBase
from app.services.parser import iter_document_blocks
from app.services.vector_store import delete_document_vectors, get_vector_store

logger = logging.getLogger(__name__)

# 中文优先的分隔符，保证按段落/句子切分而不是硬切字符
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " ", ""]

# 解析进度区间：解析占 5%，向量化占 90%，收尾占 5%
PARSE_FROM, PARSE_TO = 5, 10
EMBED_FROM, EMBED_TO = 10, 95


class IngestCancelled(Exception):
    """用户请求取消入库。"""


def _splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.CHUNK_SIZE,
        chunk_overlap=settings.CHUNK_OVERLAP,
        separators=SEPARATORS,
        length_function=len,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_cancelled(db: Session, document_id: uuid.UUID) -> bool:
    db.expire_all()
    doc = db.get(Document, document_id)
    return doc is not None and doc.cancel_requested


def _heartbeat(db: Session, document_id: uuid.UUID, progress: int | None = None) -> bool:
    """刷新心跳/进度；文档已被删除时返回 False（调用方应立即退出）。"""
    values: dict = {"heartbeat_at": _now()}
    if progress is not None:
        values["progress"] = progress
    result = db.execute(update(Document).where(Document.id == document_id).values(**values))
    db.commit()
    return (result.rowcount or 0) > 0


# ---------------- 切分落库 ----------------

def iter_chunks(blocks: Iterable[tuple[str, float]]) -> Iterator[str]:
    """把流式文本块切成定长分块（与一次性 split_text 的结果一致，内存恒定）。

    缓冲区超过一个窗口就切一刀：窗口内除最后一块外全部输出，最后一块拼回"窗口之后的
    剩余文本"继续参与下一轮——这样跨窗口的上下文不会丢，也不会重复切分。
    """
    splitter = _splitter()
    window = max(settings.CHUNK_SIZE * 4, 4096)
    buffer = ""

    for block, _fraction in blocks:
        buffer += block
        while len(buffer) > window:
            pieces = splitter.split_text(buffer[:window])
            if len(pieces) <= 1:
                break  # 窗口内切不开，等更多文本
            for content in pieces[:-1]:
                yield content
            buffer = pieces[-1] + buffer[window:]

    if buffer.strip():
        yield from splitter.split_text(buffer)


def _persist_chunks(db: Session, document_id: uuid.UUID, path: Path) -> int:
    """流式解析 + 切分，分批写入 document_chunks，返回总分块数。"""
    next_index = 0
    pending: list[DocumentChunk] = []
    progress = [0.0]  # 最近一次解析进度，用于批次间刷新心跳

    def blocks() -> Iterator[tuple[str, float]]:
        for text, fraction in iter_document_blocks(path):
            progress[0] = fraction
            yield text, fraction

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        db.add_all(pending)
        db.commit()
        pending = []
        _heartbeat(db, document_id, PARSE_FROM + int(progress[0] * (PARSE_TO - PARSE_FROM)))

    for content in iter_chunks(blocks()):
        if not content.strip():
            continue
        pending.append(
            DocumentChunk(document_id=document_id, chunk_index=next_index, content=content)
        )
        next_index += 1
        if len(pending) >= settings.CHUNK_PERSIST_BATCH:
            flush()

    flush()
    return next_index


# ---------------- 向量化 ----------------

def _chunk_ids(document_id: uuid.UUID, indexes: list[int]) -> list[str]:
    return [f"{document_id}:{i}" for i in indexes]


def _embed_pending(db: Session, doc: Document, total: int, done: int, started: float) -> int:
    """把 embedded=False 的分块逐批写入 pgvector，返回新增完成数。"""
    kb = db.get(KnowledgeBase, doc.kb_id)
    if kb is None:
        raise ValueError("所属知识库不存在")
    store = get_vector_store(kb.collection_name)

    batch_size = max(1, settings.INGEST_BATCH_SIZE)
    while True:
        if _is_cancelled(db, doc.id):
            raise IngestCancelled

        rows = db.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == doc.id, DocumentChunk.embedded.is_(False))
            .order_by(DocumentChunk.chunk_index)
            .limit(batch_size)
        ).all()
        if not rows:
            return done
        # 文档可能已被用户删除：此时应立刻停手，避免继续写孤儿向量
        if db.get(Document, doc.id) is None:
            logger.info("文档在向量化过程中被删除，停止: %s", doc.id)
            return done

        payload = [
            LCDocument(
                page_content=row.content,
                metadata={
                    "document_id": str(doc.id),
                    "kb_id": str(doc.kb_id),
                    "filename": doc.filename,
                    "chunk_index": row.chunk_index,
                },
            )
            for row in rows
        ]
        ids = _chunk_ids(doc.id, [row.chunk_index for row in rows])
        # 确定性 ID -> ON CONFLICT DO UPDATE，崩溃后重跑同一批不会产生重复向量
        store.add_documents(payload, ids=ids)

        db.execute(
            update(DocumentChunk)
            .where(DocumentChunk.id.in_([row.id for row in rows]))
            .values(embedded=True)
        )
        db.commit()

        done += len(rows)
        progress = EMBED_FROM + int(done / total * (EMBED_TO - EMBED_FROM))
        # 续传时 done 已大于零，进度只会前进不会倒退
        _heartbeat(db, doc.id, min(EMBED_TO, progress))

        if done % 64 < batch_size or done == total:
            elapsed = time.perf_counter() - started
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            logger.info(
                "入库进度 %s: %d/%d (%.2f chunk/s, 预计剩余 %.0fs)",
                doc.filename, done, total, rate, eta,
            )


# ---------------- 入口 ----------------

def ingest_document_task(document_id: str) -> None:
    """单个文档的入库流程（由 worker 调用；独立 DB 会话）。"""
    db: Session = SessionLocal()
    doc_uuid = uuid.UUID(document_id)
    started = time.perf_counter()
    try:
        doc = db.get(Document, doc_uuid)
        if doc is None:
            logger.warning("文档不存在: %s", document_id)
            return

        if doc.cancel_requested:
            raise IngestCancelled

        doc.status = DocumentStatus.PROCESSING.value
        doc.error_message = None
        doc.heartbeat_at = _now()
        db.commit()

        total = db.scalar(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == doc_uuid
            )
        ) or 0

        if not doc.parse_done:
            # 全新入库 / 重建索引 / 上次切分没跑完：清掉半截分块与残留向量，重新流式切分
            if total:
                db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_uuid))
                db.commit()
            kb = db.get(KnowledgeBase, doc.kb_id)
            if kb is None:
                raise ValueError("所属知识库不存在")
            delete_document_vectors(kb.collection_name, str(doc_uuid))
            total = _persist_chunks(db, doc_uuid, Path(doc.stored_path))
            if total == 0:
                raise ValueError("切分后无有效内容")
            doc = db.get(Document, doc_uuid)
            if doc is None:
                # 解析期间被用户删除：直接退出，分块行已随删除清空
                logger.info("文档在解析过程中被删除，停止: %s", doc_uuid)
                return
            doc.chunk_count = total
            doc.parse_done = True
            db.commit()
            logger.info("切分完成: %s -> %d 块", doc.filename, total)
            _heartbeat(db, doc_uuid, PARSE_TO)
        else:
            logger.info("检测到未完成入库，从断点续传: %s（已有 %d 块）", doc.filename, total)

        done = db.scalar(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == doc_uuid, DocumentChunk.embedded.is_(True)
            )
        ) or 0
        if done:
            logger.info("已有 %d/%d 块完成向量化，继续处理剩余部分", done, total)

        doc = db.get(Document, doc_uuid)
        _embed_pending(db, doc, total, done, started)

        # 收尾
        done = db.scalar(
            select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == doc_uuid, DocumentChunk.embedded.is_(True)
            )
        ) or 0
        if done < total:
            raise RuntimeError(f"向量化未完成：{done}/{total}")

        char_count = db.scalar(
            select(func.coalesce(func.sum(func.length(DocumentChunk.content)), 0)).where(
                DocumentChunk.document_id == doc_uuid
            )
        ) or 0

        doc = db.get(Document, doc_uuid)
        if doc is None:
            logger.warning("文档在入库过程中被删除: %s", document_id)
            return
        doc.chunk_count = total
        doc.char_count = int(char_count)
        doc.progress = 100
        doc.status = DocumentStatus.READY.value
        doc.heartbeat_at = _now()
        db.commit()

        logger.info(
            "文档入库完成: %s (%d chunks, %.1fs)",
            doc.filename, total, time.perf_counter() - started,
        )
    except IngestCancelled:
        logger.info("文档入库已取消: %s", document_id)
        db.rollback()
        try:
            doc = db.get(Document, doc_uuid)
            if doc is not None:
                kb = db.get(KnowledgeBase, doc.kb_id)
                if kb is not None:
                    try:
                        deleted = delete_document_vectors(kb.collection_name, str(doc_uuid))
                        logger.info("取消入库已清理部分向量: %s -> %d 条", document_id, deleted)
                    except Exception:  # noqa: BLE001 - 可能还没写入任何向量
                        logger.warning("清理取消文档的部分向量失败: %s", document_id)
                db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_uuid))
                doc.status = DocumentStatus.CANCELLED.value
                doc.progress = 0
                doc.chunk_count = 0
                doc.char_count = 0
                doc.parse_done = False
                doc.error_message = "已取消入库"
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    except Exception as exc:  # noqa: BLE001 - 后台任务需兜底
        logger.exception("文档处理失败: %s", document_id)
        db.rollback()
        try:
            doc = db.get(Document, doc_uuid)
            if doc is not None:
                # 保留 chunk 行与已写向量：重建索引/重试时能接着做
                doc.status = DocumentStatus.FAILED.value
                doc.error_message = str(exc)[:2000]
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    finally:
        db.close()


def reset_document_for_reindex(db: Session, doc: Document) -> None:
    """重建索引：清空分块与向量，让入库走全新路径（文件本身保留）。"""
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    doc.status = DocumentStatus.PENDING.value
    doc.error_message = None
    doc.progress = 0
    doc.chunk_count = 0
    doc.char_count = 0
    doc.cancel_requested = False
    doc.heartbeat_at = None
    doc.parse_done = False
    db.commit()
