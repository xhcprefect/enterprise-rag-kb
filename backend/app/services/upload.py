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
from app.models import UploadSession, UploadStatus

logger = logging.getLogger(__name__)
COPY_BUF = 4 * 1024 * 1024


class UploadConflict(ValueError):
    """请求与已有上传状态/内容冲突。"""


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


def abort_session(db: Session, session: UploadSession) -> None:
    session = get_session(db, session.kb_id, session.id, lock=True)
    if session is None or session.status != UploadStatus.UPLOADING.value:
        return
    session.status, session.received = UploadStatus.ABORTED.value, []
    db.commit()
    shutil.rmtree(tmp_dir(session.id), ignore_errors=True)


def _valid_final(session: UploadSession, target: Path) -> bool:
    if not target.exists() or target.stat().st_size != session.file_size:
        return False
    if session.content_sha256:
        with target.open("rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest() == session.content_sha256
    return True


def assemble(db: Session, session: UploadSession) -> uuid.UUID:
    """持有会话行锁直到文档行一起提交；磁盘清理由调用方在提交后执行。"""
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
    session.status = UploadStatus.COMPLETED.value
    session.received = list(range(session.total_chunks))
    db.flush()
    return doc_id


def cleanup_expired(db: Session) -> int:
    deadline = datetime.now(timezone.utc) - timedelta(hours=settings.UPLOAD_SESSION_TTL_HOURS)
    stale = db.scalars(select(UploadSession).where(
        UploadSession.status == UploadStatus.UPLOADING.value, UploadSession.updated_at < deadline,
    ).with_for_update(skip_locked=True)).all()
    for session in stale:
        session.status, session.received = UploadStatus.ABORTED.value, []
        shutil.rmtree(tmp_dir(session.id), ignore_errors=True)
    db.commit()
    known = {str(sid) for sid in db.scalars(select(UploadSession.id).where(
        UploadSession.status == UploadStatus.UPLOADING.value)).all()}
    root = upload_root() / "tmp"
    if root.is_dir():
        for entry in root.iterdir():
            # 不删除刚创建但尚未出现在查询快照里的上传目录。
            if entry.is_dir() and entry.name not in known and entry.stat().st_mtime < deadline.timestamp():
                shutil.rmtree(entry, ignore_errors=True)
    return len(stale)
