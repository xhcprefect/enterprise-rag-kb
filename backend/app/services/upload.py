"""分片上传：会话管理、分片落盘、合并为正式文件。

设计要点：
- 分片是否收到以磁盘 ``<index>.part`` 的大小为准，数据库里的 ``received`` 只是缓存。
- 写入先落 ``.tmp`` 再 ``os.replace``（同卷原子），因此"文件存在且大小正确"即可判定完成，
  重复上传同一片是幂等的。
- ``complete`` 先把 ``document_id`` 提交为认领标记，再拼接文件；重试复用同一路径，
  不会产生两份文件。
"""
import hashlib
import logging
import math
import os
import shutil
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import UploadSession, UploadStatus

logger = logging.getLogger(__name__)

# 拼接时每次读取的字节数
COPY_BUF = 4 * 1024 * 1024


def upload_root() -> Path:
    return Path(settings.UPLOAD_DIR)


def tmp_dir(session_id: uuid.UUID) -> Path:
    return upload_root() / "tmp" / str(session_id)


def final_path(kb_id: uuid.UUID, doc_id: uuid.UUID, ext: str) -> Path:
    return upload_root() / str(kb_id) / f"{doc_id}{ext}"


def make_fingerprint(kb_id: uuid.UUID, filename: str, file_size: int) -> str:
    raw = f"{kb_id}:{filename}:{file_size}".encode()
    return hashlib.sha256(raw).hexdigest()


def chunk_length(session: UploadSession, index: int) -> int:
    """第 index 片的字节数（末片可能更短）。"""
    start = index * session.chunk_size
    return min(session.chunk_size, session.file_size - start)


def received_indices(session: UploadSession) -> list[int]:
    """按磁盘实际内容重建已完成分片下标（不信数据库缓存）。"""
    folder = tmp_dir(session.id)
    if not folder.is_dir():
        return []
    done: list[int] = []
    for index in range(session.total_chunks):
        part = folder / f"{index}.part"
        try:
            if part.stat().st_size == chunk_length(session, index):
                done.append(index)
        except OSError:
            continue
    return done


def sync_received(session: UploadSession) -> list[int]:
    done = received_indices(session)
    if session.received != done:
        session.received = done
    return done


def get_session(db: Session, kb_id: uuid.UUID, session_id: uuid.UUID) -> UploadSession | None:
    session = db.get(UploadSession, session_id)
    if session is None or session.kb_id != kb_id:
        return None
    return session


def create_or_resume(
    db: Session, kb_id: uuid.UUID, filename: str, file_size: int, ext: str, user_id: uuid.UUID
) -> UploadSession:
    """同库同名同大小的未完成会话直接复用（断点续传的入口）。"""
    fingerprint = make_fingerprint(kb_id, filename, file_size)

    existing = db.scalar(
        select(UploadSession).where(
            UploadSession.fingerprint == fingerprint,
            UploadSession.status == UploadStatus.UPLOADING.value,
        )
    )
    if existing is not None:
        sync_received(existing)
        db.commit()
        return existing

    chunk_size = max(1, settings.UPLOAD_CHUNK_SIZE)
    session = UploadSession(
        kb_id=kb_id,
        fingerprint=fingerprint,
        filename=filename,
        file_ext=ext,
        file_size=file_size,
        chunk_size=chunk_size,
        total_chunks=max(1, math.ceil(file_size / chunk_size)),
        received=[],
        status=UploadStatus.UPLOADING.value,
        created_by=user_id,
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:
        # 并发建会话：唯一索引兜底，退回复用对方创建的那一个
        db.rollback()
        again = db.scalar(
            select(UploadSession).where(
                UploadSession.fingerprint == fingerprint,
                UploadSession.status == UploadStatus.UPLOADING.value,
            )
        )
        if again is None:
            raise
        return again

    db.refresh(session)
    tmp_dir(session.id).mkdir(parents=True, exist_ok=True)
    return session


def save_chunk(session: UploadSession, index: int, data: bytes) -> bool:
    """写入一片；返回是否为本次真正新写入（重复上传视为已完成）。"""
    expected = chunk_length(session, index)
    if len(data) != expected:
        raise ValueError(f"分片大小不符：期望 {expected} 字节，实际 {len(data)} 字节")

    folder = tmp_dir(session.id)
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f"{index}.part"

    if part.exists() and part.stat().st_size == expected:
        return False  # 幂等：已收到过

    tmp = folder / f"{index}.part.tmp"
    with tmp.open("wb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    os.replace(tmp, part)  # 同卷原子替换，避免半片被当作完整分片
    return True


def abort_session(db: Session, session: UploadSession) -> None:
    session.status = UploadStatus.ABORTED.value
    session.received = []
    db.commit()
    shutil.rmtree(tmp_dir(session.id), ignore_errors=True)


def assemble(db: Session, session: UploadSession) -> uuid.UUID:
    """把所有分片拼成正式文件，返回文档 ID。

    幂等：会话已有 document_id 时直接复用（哪怕上次拼到一半崩了，路径也一致）。
    """
    if session.document_id is None:
        session.document_id = uuid.uuid4()
        db.commit()  # 先认领，避免重复合并

    doc_id = session.document_id
    target = final_path(session.kb_id, doc_id, session.file_ext)

    if not target.exists() or target.stat().st_size != session.file_size:
        missing = [
            i for i in range(session.total_chunks)
            if not (tmp_dir(session.id) / f"{i}.part").exists()
        ]
        if missing:
            raise FileNotFoundError(f"缺少分片：{missing[:20]}")

        target.parent.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(target.parent).free
        if free < session.file_size:
            raise OSError(
                f"磁盘空间不足：需要 {session.file_size} 字节，剩余 {free} 字节"
            )

        assembling = target.with_suffix(target.suffix + ".assembling")
        with assembling.open("wb") as out:
            for index in range(session.total_chunks):
                part = tmp_dir(session.id) / f"{index}.part"
                with part.open("rb") as src:
                    shutil.copyfileobj(src, out, COPY_BUF)
            out.flush()
            os.fsync(out.fileno())
        os.replace(assembling, target)

    session.status = UploadStatus.COMPLETED.value
    session.received = list(range(session.total_chunks))
    db.commit()
    shutil.rmtree(tmp_dir(session.id), ignore_errors=True)
    logger.info("分片合并完成: %s (%d 片, %d 字节)", session.filename, session.total_chunks, session.file_size)
    return doc_id


def cleanup_expired(db: Session) -> int:
    """清理超时未完成的上传会话与孤儿临时目录。"""
    from datetime import datetime, timedelta, timezone

    deadline = datetime.now(timezone.utc) - timedelta(hours=settings.UPLOAD_SESSION_TTL_HOURS)
    stale = db.scalars(
        select(UploadSession).where(
            UploadSession.status == UploadStatus.UPLOADING.value,
            UploadSession.updated_at < deadline,
        )
    ).all()
    for session in stale:
        session.status = UploadStatus.ABORTED.value
        session.received = []
        shutil.rmtree(tmp_dir(session.id), ignore_errors=True)
    if stale:
        db.commit()
        logger.info("清理过期上传会话: %d 个", len(stale))

    # 库中已无对应 uploading 会话的临时目录
    known = {
        str(sid)
        for sid in db.scalars(
            select(UploadSession.id).where(UploadSession.status == UploadStatus.UPLOADING.value)
        ).all()
    }
    root = upload_root() / "tmp"
    if root.is_dir():
        for entry in root.iterdir():
            if entry.is_dir() and entry.name not in known:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info("清理孤儿分片目录: %s", entry.name)
    return len(stale)
