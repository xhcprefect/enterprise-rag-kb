"""可续传的入库任务：持续心跳、租约隔离、版本化向量和原子发布。

停机（SIGTERM/SIGINT）是与“用户取消”不同的协作分支：
- worker 传入 ``stop_event`` 后，任务只在安全点停下，已完整的解析/向量检查点保留；
- 解析中途停止时 ``parse_done`` 必须仍为 False，下次会丢弃不完整分块重新切分；
- 已成功返回的当前 embedding 批次照常提交，之后才把文档退回 pending 并清空租约；
- 取消与租约丢失的判定永远先于停止回交，旧 worker 不能借停机复活任务。
"""
import logging
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.documents import Document as LCDocument
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import Document, DocumentChunk, DocumentStatus, KnowledgeBase
from app.services.cloud_models import EmbeddingStopped, embedding_stop_scope
from app.services.structured_documents import iter_structured_chunks
from app.services.hybrid_search import build_search_text
from app.services.usage_tracking import ModelCall, UsageContext, usage_scope
from app.services.vector_store import delete_document_vectors, get_vector_store, persist_vectors

logger = logging.getLogger(__name__)
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", ". ", "! ", "? ", "; ", "，", ", ", " ", ""]
PARSE_FROM, PARSE_TO = 5, 10
EMBED_FROM, EMBED_TO = 10, 95


class IngestCancelled(Exception):
    """用户取消任务。"""


class IngestLeaseLost(Exception):
    """文档删除、索引代次改变或任务已被其他 worker 领取。"""


class IngestStopped(Exception):
    """进程停机：任务在安全点协作退出，已完成的检查点保留。"""


def _stop_requested(stop_event) -> bool:
    return stop_event is not None and stop_event.is_set()


# 专用解析错误的用户提示长度上限：即便未来新增文案也不能无限膨胀。
MAX_PUBLIC_ERROR_CHARS = 300


def _safe_parse_message(exc: BaseException) -> str | None:
    """只接受专用 PDF 解析错误的固定/有界提示；其它异常返回 None 走通用文案。

    文案由 ``pdf_documents`` 用固定字符串与本地页号整数拼出，不含路径、原文或
    上游异常正文；这里再做一次类型与长度兜底。
    """
    from app.services.pdf_documents import PdfParseError

    if not isinstance(exc, PdfParseError):
        return None
    message = exc.public_message if isinstance(exc.public_message, str) else ""
    message = message.strip()
    if not message or len(message) > MAX_PUBLIC_ERROR_CHARS:
        return None
    return message


@dataclass(frozen=True)
class IngestJob:
    document_id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    stored_path: str
    version: int
    lease_id: uuid.UUID
    user_id: uuid.UUID | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _splitter() -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=settings.CHUNK_SIZE, chunk_overlap=settings.CHUNK_OVERLAP,
        separators=SEPARATORS, length_function=len,
    )


def iter_chunks(blocks: Iterable[tuple[str, float]]) -> Iterator[str]:
    """有界缓冲切分；最后一块保留至下个窗口，避免丢失边界上下文。"""
    splitter = _splitter()
    window = max(settings.CHUNK_SIZE * 4, 4096)
    buffer = ""
    for block, _fraction in blocks:
        buffer += block
        while len(buffer) > window:
            pieces = splitter.split_text(buffer[:window])
            if len(pieces) <= 1:
                break
            yield from pieces[:-1]
            buffer = pieces[-1] + buffer[window:]
    if buffer.strip():
        yield from splitter.split_text(buffer)


def _locked_doc(db: Session, job: IngestJob, *, allow_cancel: bool = False) -> Document:
    doc = db.scalar(select(Document).where(Document.id == job.document_id)
                    .with_for_update().execution_options(populate_existing=True))
    if (doc is None or doc.index_version != job.version or doc.lease_id != job.lease_id
            or doc.status != DocumentStatus.PROCESSING.value):
        raise IngestLeaseLost
    if doc.cancel_requested and not allow_cancel:
        raise IngestCancelled
    return doc


@contextmanager
def _heartbeats(job: IngestJob):
    """网络请求/解析期间独立续约；更新也带租约条件，旧任务无法续新任务的租约。

    退出时先让线程醒来再 join。即便 join 超时，线程的下一次 UPDATE 仍带
    ``lease_id``/``index_version``/``processing`` 条件，无法刷新已经交回或
    被其他 worker 接管的租约。
    """
    stopped = threading.Event()
    interval = max(1.0, min(30.0, settings.INGEST_STALE_SECONDS / 3))

    def renew():
        while not stopped.wait(interval):
            try:
                with SessionLocal() as db:
                    result = db.execute(update(Document).where(
                        Document.id == job.document_id,
                        Document.lease_id == job.lease_id,
                        Document.index_version == job.version,
                        Document.status == DocumentStatus.PROCESSING.value,
                    ).values(heartbeat_at=_now()))
                    db.commit()
                    if not result.rowcount:
                        return
            except Exception:
                logger.warning("入库心跳刷新失败: %s", job.document_id, exc_info=True)

    thread = threading.Thread(target=renew, name=f"ingest-heartbeat-{job.document_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=2)


def _claim(db: Session, document_id: uuid.UUID, lease_id: uuid.UUID | None) -> IngestJob:
    doc = db.scalar(select(Document).where(Document.id == document_id)
                    .with_for_update().execution_options(populate_existing=True))
    if doc is None:
        raise IngestLeaseLost
    if lease_id is None:
        if doc.status != DocumentStatus.PENDING.value:
            raise IngestLeaseLost
        doc.lease_id = uuid.uuid4()
        doc.status = DocumentStatus.PROCESSING.value
    elif doc.status != DocumentStatus.PROCESSING.value or doc.lease_id != lease_id:
        raise IngestLeaseLost
    doc.error_message = None
    doc.heartbeat_at = _now()
    job = IngestJob(doc.id, doc.kb_id, doc.filename, doc.stored_path, doc.index_version, doc.lease_id, getattr(doc, "uploaded_by", None))
    db.commit()
    return job


def _persist_chunks(db: Session, job: IngestJob, stop_event=None) -> int:
    """切分并分批落库；停机时在安全点退出，``parse_done`` 保持 False。

    中途停止前已落库的分块属于“不完整解析”，回交时会被清掉；下一次任务从
    文件头重新切分，不会把部分结果当作可续传检查点。
    """
    pending: list[DocumentChunk] = []
    next_index = 0
    progress = 0.0

    def flush():
        if not pending:
            return
        doc = _locked_doc(db, job)
        db.add_all(pending)
        doc.progress = PARSE_FROM + int(progress * (PARSE_TO - PARSE_FROM))
        doc.heartbeat_at = _now()
        db.commit()
        pending.clear()

    for chunk in iter_structured_chunks(Path(job.stored_path), settings.CHUNK_SIZE, settings.CHUNK_OVERLAP):
        if _stop_requested(stop_event):
            raise IngestStopped
        content, progress = chunk.content, chunk.progress
        pending.append(DocumentChunk(document_id=job.document_id, chunk_index=next_index, content=content,
                                     source_metadata=chunk.metadata,
                                     search_text=build_search_text(content, {**chunk.metadata, "filename": job.filename})))
        next_index += 1
        if len(pending) >= max(1, settings.CHUNK_PERSIST_BATCH):
            flush()
    flush()
    return next_index


def _embed_pending(db: Session, job: IngestJob, store, total: int, done: int, stop_event=None) -> None:
    """逐批向量化。停止优先级：

    1. 每轮先核验租约/取消（``_locked_doc``）——删除、重建代次、取消永远最先；
    2. 没有待处理分块时正常返回，让最终发布完成（无批次就不会有半批状态）；
    3. 有批次且已收到停止信号时，不再发起新请求，直接交回；
    4. 网络调用成功后本批照常原子落库，下一轮再停——成功响应不丢弃。
    """
    while True:
        _locked_doc(db, job)
        rows = db.scalars(select(DocumentChunk).where(
            DocumentChunk.document_id == job.document_id, DocumentChunk.embedded.is_(False),
        ).order_by(DocumentChunk.chunk_index).limit(max(1, settings.INGEST_BATCH_SIZE))).all()
        if not rows:
            db.commit()
            return
        if _stop_requested(stop_event):
            db.rollback()
            raise IngestStopped
        contents = [row.content for row in rows]
        row_ids = [row.id for row in rows]
        indexes = [row.chunk_index for row in rows]
        locations = [dict(row.source_metadata or {}) for row in rows]
        for row in rows:
            if not row.search_text:
                row.search_text = build_search_text(row.content, {**(row.source_metadata or {}), "filename": job.filename})
        db.commit()  # 网络期间不持有文档行锁，也不占用数据库连接。

        context = UsageContext(user_id=job.user_id, kb_id=job.kb_id, document_id=job.document_id,
                               operation="document_embedding")
        with usage_scope(context), embedding_stop_scope(stop_event):
            local_call = ModelCall("embedding", "ollama", settings.EMBEDDING_MODEL) if settings.EMBEDDING_PROVIDER != "cloud" else None
            try:
                vectors = store.embeddings.embed_documents(contents)
            except BaseException:
                if local_call:
                    local_call.finish("failed")
                raise
            else:
                if local_call:
                    local_call.finish()
        payload = [LCDocument(page_content=content, metadata={
            **location,
            "document_id": str(job.document_id), "kb_id": str(job.kb_id),
            "filename": job.filename, "chunk_index": index, "index_version": job.version,
        }) for index, content, location in zip(indexes, contents, locations)]
        identifiers = [f"{job.document_id}:{index}" if job.version == 0 else
                       f"{job.document_id}:{job.version}:{index}" for index in indexes]

        # 删除/取消/重新领取可能发生在网络调用期间；写回前必须重新加锁并核验租约。
        # 注意：这里不检查停止信号——本轮请求已经成功，应先落库再在下一轮退出。
        doc = _locked_doc(db, job)
        persist_vectors(db, store, payload, vectors, identifiers)
        db.execute(update(DocumentChunk).where(DocumentChunk.id.in_(row_ids)).values(embedded=True))
        done += len(rows)
        doc.progress = min(EMBED_TO, EMBED_FROM + int(done / max(1, total) * (EMBED_TO - EMBED_FROM)))
        doc.heartbeat_at = _now()
        db.commit()  # 向量与 embedded 标记同一事务提交。


def _cancel_job(db: Session, job: IngestJob) -> None:
    """用户取消的收尾：删除向量与分块，标记 CANCELLED。租约变化时静默放弃。"""
    try:
        doc = _locked_doc(db, job, allow_cancel=True)
        kb = db.get(KnowledgeBase, job.kb_id)
        if kb is not None:
            delete_document_vectors(kb.collection_name, str(job.document_id), db=db)
        db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == job.document_id))
        doc.status, doc.error_message = DocumentStatus.CANCELLED.value, "已取消入库"
        doc.progress = doc.chunk_count = doc.char_count = 0
        doc.parse_done, doc.lease_id = False, None
        db.commit()
    except IngestLeaseLost:
        db.rollback()


def _hand_back_stopped(db: Session, job: IngestJob) -> None:
    """停机交回：完成后才回 pending，让其他 worker/下次启动续传。

    先重新核验租约与取消（``_locked_doc`` 会抛 ``IngestLeaseLost``/``IngestCancelled``），
    所以删除、重建代次或用户取消永远优先于停机。解析未完成的分块会被丢弃，
    ``parse_done`` 保持 False；已完成的解析与向量检查点原样保留。
    """
    doc = _locked_doc(db, job)
    if not doc.parse_done:
        kb = db.get(KnowledgeBase, job.kb_id)
        if kb is not None:
            delete_document_vectors(kb.collection_name, str(job.document_id), db=db)
        db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == job.document_id))
        doc.progress = doc.chunk_count = doc.char_count = 0
    doc.status = DocumentStatus.PENDING.value
    doc.error_message = None
    doc.heartbeat_at, doc.lease_id = None, None
    db.commit()


def ingest_document_task(document_id: str, lease_id: uuid.UUID | None = None, *,
                         stop_event: threading.Event | None = None) -> None:
    """处理一份文档；``stop_event`` 仅在 worker 停机时传入。

    不传 ``stop_event`` 时行为与之前完全一致：正常的续传、取消、失败状态不变。
    """
    started = time.perf_counter()
    job: IngestJob | None = None
    with SessionLocal() as db:
        try:
            # 已停止且没有拿到租约：不领取任何新任务。已经由 worker 领取的
            # 任务仍要走 _claim 才能安全交回，否则会一直停在 processing。
            if _stop_requested(stop_event) and lease_id is None:
                logger.info("已收到停机信号，跳过领取: %s", document_id)
                return
            job = _claim(db, uuid.UUID(document_id), lease_id)
            if _stop_requested(stop_event):
                raise IngestStopped
            kb = db.get(KnowledgeBase, job.kb_id)
            if kb is None:
                raise IngestLeaseLost
            collection = kb.collection_name
            db.commit()
            store = get_vector_store(collection)
            with _heartbeats(job):
                doc = _locked_doc(db, job)
                parse_done = doc.parse_done
                if not parse_done:
                    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == job.document_id))
                    delete_document_vectors(collection, document_id, db=db)
                db.commit()
                if not parse_done:
                    # 正常返回即代表整份文件已切分并落库；中途停机由 _persist_chunks
                    # 抛 IngestStopped，因此这里可以安全地置 parse_done。即便此刻已收到
                    # 停机信号，完整解析也应当作为检查点保留，供下次直接向量化。
                    total = _persist_chunks(db, job, stop_event)
                    if not total:
                        raise ValueError("切分后无有效内容")
                    doc = _locked_doc(db, job)
                    doc.chunk_count = total
                    doc.parse_done = True
                    doc.progress = PARSE_TO
                    db.commit()
                else:
                    total = db.scalar(select(func.count()).select_from(DocumentChunk).where(
                        DocumentChunk.document_id == job.document_id)) or 0
                    db.commit()
                done = db.scalar(select(func.count()).select_from(DocumentChunk).where(
                    DocumentChunk.document_id == job.document_id, DocumentChunk.embedded.is_(True))) or 0
                db.commit()
                _embed_pending(db, job, store, total, done, stop_event)

                # 走到这里说明没有待嵌入分块（或全部完成）：发布只是一次本地事务，
                # 不再发起模型调用，因此即使此刻已收到停机信号也让它提交完成，
                # 避免把一份已经全部向量化的文档无谓地退回队列。
                doc = _locked_doc(db, job)
                count, chars = db.execute(select(
                    func.count(), func.coalesce(func.sum(func.length(DocumentChunk.content)), 0),
                ).where(DocumentChunk.document_id == job.document_id,
                        DocumentChunk.embedded.is_(True))).one()
                if not total or count != total:
                    raise RuntimeError(f"向量化未完成：{count}/{total}")
                # 发布状态和版本同事务可见，检索不会读到半成品或旧代次。
                doc.chunk_count, doc.char_count = total, int(chars)
                doc.progress, doc.status = 100, DocumentStatus.READY.value
                doc.heartbeat_at, doc.lease_id = _now(), None
                db.commit()
            logger.info("文档入库完成: %s (%d chunks, %.1fs)", job.filename, total, time.perf_counter() - started)
        except IngestLeaseLost:
            db.rollback()
            logger.info("文档已删除或任务租约已改变，停止写入: %s", document_id)
        except (IngestStopped, EmbeddingStopped):
            db.rollback()
            if job is not None:
                try:
                    _hand_back_stopped(db, job)
                    logger.info("停机信号：入库任务已交回 pending: %s", document_id)
                except IngestCancelled:
                    db.rollback()
                    _cancel_job(db, job)
                except IngestLeaseLost:
                    db.rollback()
        except IngestCancelled:
            db.rollback()
            if job is not None:
                _cancel_job(db, job)
        except Exception as exc:  # noqa: BLE001 - 保留原有失败兜底
            db.rollback()
            if job is not None and _stop_requested(stop_event):
                # 调用超时/网络异常恰好发生在停机时：这是可续传的中断，不是普通失败。
                # 但取消/删除/代次变化仍必须先核验，不能借停机复活旧任务。
                try:
                    _hand_back_stopped(db, job)
                    logger.warning("停机期间向量调用未完成，任务已交回 pending: %s", document_id)
                    return
                except IngestCancelled:
                    db.rollback()
                    _cancel_job(db, job)
                    return
                except IngestLeaseLost:
                    db.rollback()
                    logger.info("停机期间租约已改变，放弃写回: %s", document_id)
                    return
            logger.exception("文档处理失败: %s", document_id)
            if job is not None:
                try:
                    doc = _locked_doc(db, job, allow_cancel=True)
                    doc.status = DocumentStatus.FAILED.value
                    # 只有专用解析错误可以给出固定/有边的安全提示；其它异常（含
                    # PyMuPDF、数据库或网络的第三方消息）保持原通用文案，绝不回显
                    # str(exc)、路径或原文。
                    doc.error_message = (_safe_parse_message(exc)
                                         or "文档处理失败，请联系管理员检查日志")
                    doc.lease_id = None
                    db.commit()
                except IngestLeaseLost:
                    db.rollback()


def reset_document_for_reindex(db: Session, doc: Document) -> None:
    """切换代次并重新排队；旧向量立即失去可检索资格。"""
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    doc.index_version += 1
    doc.status, doc.error_message = DocumentStatus.PENDING.value, None
    doc.progress = doc.chunk_count = doc.char_count = 0
    doc.cancel_requested, doc.parse_done = False, False
    doc.heartbeat_at = doc.lease_id = None
    db.commit()
