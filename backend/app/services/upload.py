"""安全分片续传：内容摘要识别、行锁串行写入、事务内完成认领。"""
import hashlib
import logging
import math
import os
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    KNOWLEDGE_BASE_KIND_PROJECT,
    Document,
    KnowledgeBase,
    UploadSession,
    UploadStatus,
)

logger = logging.getLogger(__name__)
COPY_BUF = 4 * 1024 * 1024


class UploadConflict(ValueError):
    """请求与已有上传状态/内容冲突。"""


def _guard_project_kb(db, kb_id) -> None:
    """服务层兜底：项目源码库拒绝普通分片写入。

    项目库只能通过项目 API 登记维护；API 入口已先按 400 拒绝，这里仅防
    直接绕过 API 调用本服务的路径，若被触达同样不允许落盘/合并/发布。
    """
    kb = db.get(KnowledgeBase, kb_id)
    if kb is not None and kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        raise UploadConflict("项目源码库不支持此操作")


def upload_root() -> Path:
    return Path(settings.UPLOAD_DIR)


def tmp_dir(session_id: uuid.UUID) -> Path:
    return upload_root() / "tmp" / str(session_id)


def final_path(kb_id: uuid.UUID, doc_id: uuid.UUID, ext: str) -> Path:
    return upload_root() / str(kb_id) / f"{doc_id}{ext}"


def make_fingerprint(
    kb_id: uuid.UUID, filename: str, file_size: int,
    content_sha256: str | None = None, user_id: uuid.UUID | None = None,
) -> str:
    # 无内容摘要的老客户端每次新建，绝不按同名同大小猜测“同一次上传”。
    identity = content_sha256.lower() if content_sha256 else uuid.uuid4().hex
    raw = f"v2:{kb_id}:{user_id}:{filename}:{file_size}:{identity}".encode()
    return hashlib.sha256(raw).hexdigest()


def chunk_length(session: UploadSession, index: int) -> int:
    if index < 0 or index >= session.total_chunks:
        raise UploadConflict("分片下标越界")
    return min(session.chunk_size, session.file_size - index * session.chunk_size)


def received_indices(session: UploadSession) -> list[int]:
    folder = tmp_dir(session.id)
    if not folder.is_dir():
        return []
    done = []
    for index in range(session.total_chunks):
        try:
            if (folder / f"{index}.part").stat().st_size == chunk_length(session, index):
                done.append(index)
        except OSError:
            continue
    return done


def get_session(
    db: Session, kb_id: uuid.UUID, session_id: uuid.UUID, *,
    user_id: uuid.UUID | None = None, lock: bool = False,
) -> UploadSession | None:
    statement = select(UploadSession).where(UploadSession.id == session_id, UploadSession.kb_id == kb_id)
    if user_id is not None:
        statement = statement.where(UploadSession.created_by == user_id)
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return db.scalar(statement)


def create_or_resume(
    db: Session, kb_id: uuid.UUID, filename: str, file_size: int, ext: str,
    user_id: uuid.UUID, content_sha256: str | None = None,
) -> UploadSession:
    _guard_project_kb(db, kb_id)
    if not 0 < file_size <= settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise UploadConflict(f"文件大小必须在 1 字节至 {settings.MAX_UPLOAD_MB}MB 之间")
    fingerprint = make_fingerprint(kb_id, filename, file_size, content_sha256, user_id)
    statement = select(UploadSession).where(
        UploadSession.fingerprint == fingerprint, UploadSession.status == UploadStatus.UPLOADING.value,
    )
    existing = db.scalar(statement.with_for_update())
    if existing is not None:
        existing.received = received_indices(existing)
        existing.updated_at = datetime.now(timezone.utc)
        db.commit()
        return existing
    chunk_size = max(1, settings.UPLOAD_CHUNK_SIZE)
    session = UploadSession(
        kb_id=kb_id, fingerprint=fingerprint, filename=filename, file_ext=ext,
        file_size=file_size, content_sha256=content_sha256.lower() if content_sha256 else None,
        chunk_size=chunk_size, total_chunks=math.ceil(file_size / chunk_size),
        received=[], status=UploadStatus.UPLOADING.value, created_by=user_id,
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        session = db.scalar(statement)
        if session is None:
            raise
    tmp_dir(session.id).mkdir(parents=True, exist_ok=True)
    return session


def save_chunk(session: UploadSession, index: int, data: bytes) -> bool:
    """调用方持有上传行锁；重复片必须内容完全相同，临时文件路径每次唯一。"""
    if session.status != UploadStatus.UPLOADING.value:
        raise UploadConflict("上传会话已结束")
    expected = chunk_length(session, index)
    if len(data) != expected:
        raise UploadConflict(f"分片大小不符：期望 {expected} 字节")
    folder = tmp_dir(session.id)
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f"{index}.part"
    if part.exists() and part.stat().st_size == expected:
        with part.open("rb") as source:
            if hashlib.file_digest(source, "sha256").digest() != hashlib.sha256(data).digest():
                raise UploadConflict("同一分片的内容发生变化，请放弃该上传后重试")
        return False
    temporary = folder / f"{index}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, part)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def claimed_document_id(session: UploadSession) -> uuid.UUID:
    """本次会话认领的文档 ID：已提交则用它，未提交也能由会话 ID 推导。"""
    if session.document_id is not None:
        return session.document_id
    return uuid.uuid5(uuid.NAMESPACE_URL, f"rag-upload:{session.id}")


def remove_unreferenced_final(db: Session, session: UploadSession) -> bool:
    """清理本会话认领的合并文件；被任何文档引用时一律保留。

    进程在 complete 阶段二提交后、阶段三之前退出时，正式文件已落盘但会话
    仍是 uploading。TTL/主动放弃只清 tmp 会把它留成孤儿，因此两者都调用
    这里；但与同内容并发完成时该路径可能已被另一文档收编，所以先查引用，
    绝不删除 canonical。
    """
    target = final_path(session.kb_id, claimed_document_id(session), session.file_ext)
    referenced = db.scalar(select(Document.id).where(Document.stored_path == str(target)))
    if referenced is not None:
        return False
    try:
        target.unlink(missing_ok=True)
    except OSError:
        logger.warning("清理合并文件失败: %s", target, exc_info=True)
        return False
    return True


def abort_session(db: Session, session: UploadSession) -> None:
    session = get_session(db, session.kb_id, session.id, lock=True)
    if session is None or session.status != UploadStatus.UPLOADING.value:
        return
    session.status, session.received = UploadStatus.ABORTED.value, []
    db.commit()
    shutil.rmtree(tmp_dir(session.id), ignore_errors=True)
    # 阶段二已落盘、阶段三尚未提交时被放弃：正式文件也必须清掉（引用者除外）。
    remove_unreferenced_final(db, session)


def _valid_final(session: UploadSession, target: Path) -> bool:
    if not target.exists() or target.stat().st_size != session.file_size:
        return False
    if session.content_sha256:
        with target.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == session.content_sha256
    return True


class AssemblyResult:
    """慢写阶段的结果：本次认领的文件与它的服务端真实摘要。

    状态切换（completed/deduplicated/document_id）留给调用方在短事务里
    与文档行一起提交，避免把知识库行锁跨在合并/哈希这类磁盘操作上。
    """

    def __init__(self, claimed_doc_id: uuid.UUID, path: Path, digest: str):
        self.claimed_doc_id = claimed_doc_id
        self.path = path
        self.digest = digest


def assemble(db: Session, session: UploadSession) -> AssemblyResult:
    """只持有会话行锁完成合并与校验；提交由调用方负责。

    慢写期间不碰知识库行，也不会提前把会话标记为 completed——若进程在
    文件落盘与文档提交之间退出，重试仍能定位同一认领路径并继续。
    """
    _guard_project_kb(db, session.kb_id)
    session = get_session(db, session.kb_id, session.id, lock=True)
    if session is None or session.status == UploadStatus.ABORTED.value:
        raise UploadConflict("上传会话已终止")
    if session.document_id is None:
        # 进程在文件落盘与提交之间退出时，下次重试仍定位同一文件，不遗留随机路径。
        session.document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"rag-upload:{session.id}")
        db.flush()
    doc_id = session.document_id
    target = final_path(session.kb_id, doc_id, session.file_ext)
    if not _valid_final(session, target):
        if session.status == UploadStatus.COMPLETED.value:
            raise UploadConflict("已完成的上传文件缺失或校验失败，请重新上传")
        received = received_indices(session)
        if len(received) != session.total_chunks:
            raise UploadConflict(f"分片不完整：已收到 {len(received)}/{session.total_chunks}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < session.file_size:
            raise OSError("磁盘剩余空间不足")
        temporary = target.with_name(target.name + f".{uuid.uuid4().hex}.assembling")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as out:
                for index in range(session.total_chunks):
                    with (tmp_dir(session.id) / f"{index}.part").open("rb") as source:
                        while block := source.read(COPY_BUF):
                            size += len(block)
                            digest.update(block)
                            out.write(block)
                out.flush()
                os.fsync(out.fileno())
            if size != session.file_size:
                raise UploadConflict("合并后文件大小不符")
            if session.content_sha256 and digest.hexdigest() != session.content_sha256:
                raise UploadConflict("文件摘要校验失败，请放弃该上传后重新选择原文件")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    # 服务端真实摘要：声明值只参与上面的完整性校验，绝不直接落库。
    with target.open("rb") as source:
        digest_hex = hashlib.file_digest(source, "sha256").hexdigest()
    session.received = list(range(session.total_chunks))
    db.flush()
    return AssemblyResult(doc_id, target, digest_hex)


def cleanup_expired(db: Session) -> int:
    deadline = datetime.now(timezone.utc) - timedelta(hours=settings.UPLOAD_SESSION_TTL_HOURS)
    stale = db.scalars(select(UploadSession).where(
        UploadSession.status == UploadStatus.UPLOADING.value, UploadSession.updated_at < deadline,
    ).with_for_update(skip_locked=True)).all()
    for session in stale:
        session.status, session.received = UploadStatus.ABORTED.value, []
        shutil.rmtree(tmp_dir(session.id), ignore_errors=True)
    db.commit()
    # 阶段二落盘后进程退出：会话已过期，正式文件也没有 worker 会再发布它。
    # 逐个检查引用，避免误删并发完成时已被收编为 canonical 的同路径文件。
    for session in stale:
        remove_unreferenced_final(db, session)
    known = {str(sid) for sid in db.scalars(select(UploadSession.id).where(
        UploadSession.status == UploadStatus.UPLOADING.value)).all()}
    root = upload_root() / "tmp"
    if root.is_dir():
        for entry in root.iterdir():
            # 不删除刚创建但尚未出现在查询快照里的上传目录。
            if entry.is_dir() and entry.name not in known and entry.stat().st_mtime < deadline.timestamp():
                shutil.rmtree(entry, ignore_errors=True)
    return len(stale)
