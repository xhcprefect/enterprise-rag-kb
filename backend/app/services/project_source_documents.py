"""项目源内容的安全范围读取（Q 独占）。

只凭 UUID（file_version_id）+ 授权快照映射定位，永不按任意绝对路径/blob 名读取：
- 先校验该 file_version 属于某个**已授权且 ready** 的快照（源未 revoke、KB 可见）。
- 再以该快照的映射为准读取不可变 chunk（project_chunks），做安全 chunk/line 范围裁剪。
- 旧 ready 快照也可读；指定 snapshot_id 时只认该快照，避免"旧引用却读到新内容"的漂移。
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models import (
    KnowledgeBase,
    ProjectChunk,
    ProjectFileVersion,
    ProjectSnapshot,
    ProjectSnapshotFile,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
)
from app.schemas.project import (
    MAX_PREVIEW_CHARS,
    ProjectSourceChunkRead,
    ProjectSourcePreviewRead,
)
from app.services import project_sources

# 单次预览的硬界：最多 48 个 chunk、起始不越界、总字符有上限。
MAX_PREVIEW_CHUNKS = 48


class ProjectSourceNotFound(project_sources.ProjectSourceNotFound):
    pass


class ProjectSourceRevoked(project_sources.ProjectSourceRevoked):
    pass


class ProjectSnapshotNotReady(Exception):
    """目标快照未 ready / 不含该文件 / 与文件不同源：读预览失败，不得返回空成功。"""


def _get_ready_snapshot(
    db: DBSession, snapshot_id: uuid.UUID
) -> ProjectSnapshot | None:
    snap = db.get(ProjectSnapshot, snapshot_id)
    if snap is None or snap.status != ProjectSnapshotStatus.READY.value:
        return None
    return snap


def _maps_file(
    db: DBSession, snapshot_id: uuid.UUID, file_version_id: uuid.UUID
) -> bool:
    """该文件版本是否确实是此快照的映射（有界单行核验，不枚举全量快照）。"""
    return (
        db.scalar(
            select(ProjectSnapshotFile.file_version_id).where(
                ProjectSnapshotFile.snapshot_id == snapshot_id,
                ProjectSnapshotFile.file_version_id == file_version_id,
            )
        )
        is not None
    )


def _pick_mapped_ready_snapshot(
    db: DBSession, source: ProjectSource, file_version_id: uuid.UUID
) -> ProjectSnapshot | None:
    """只选**实际映射**该文件版本的 ready 快照：优先 active，其次最近的 ready 映射。

    通过 JOIN ProjectSnapshotFile 限定，绝不会把未包含该文件的 active 快照当作用户
    引用的当前快照（防止旧文件版本被冒称"当前"）；查询带 LIMIT 1，不无界枚举。
    """
    active = source.active_snapshot_id
    return db.scalar(
        select(ProjectSnapshot)
        .join(
            ProjectSnapshotFile,
            ProjectSnapshotFile.snapshot_id == ProjectSnapshot.id,
        )
        .where(
            ProjectSnapshot.status == ProjectSnapshotStatus.READY.value,
            ProjectSnapshot.source_id == source.id,
            ProjectSnapshotFile.file_version_id == file_version_id,
        )
        .order_by(
            (ProjectSnapshot.id == active).desc(),
            ProjectSnapshot.created_at.desc(),
        )
        .limit(1)
    )


def _authorized_snapshot_for_file(
    db: DBSession, fv: ProjectFileVersion, user: User, snapshot_id: uuid.UUID | None
) -> ProjectSnapshot:
    """返回授权当前 file_version 的 ready 快照（且该快照确实映射此文件）。

    源未撤销（否则 410）、KB 对本用户可见（否则 404）。显式 snapshot_id 必须是
    ready、与文件同 source、且包含该文件；未指定时只选实际映射的 active 或最近
    ready 快照，绝不凭空返回某快照冒充当前。
    """
    source = db.get(ProjectSource, fv.source_id)
    if source is None:
        raise ProjectSourceNotFound()
    kb = db.get(KnowledgeBase, source.kb_id)
    if kb is None:
        raise ProjectSourceNotFound()
    # ACL 先于 revoke：私有已 revoke 源对无权限 stranger 呈 404，而非 410。
    # 只有授权（管理员/拥有者）才见到 revoke 语义（410），避免对陌生人多泄一次"此处曾存在"。
    if not user.is_admin and not kb.is_public and kb.owner_id != user.id:
        raise ProjectSourceNotFound()
    if source.revoked_at is not None:
        raise ProjectSourceRevoked()

    if snapshot_id is not None:
        snap = _get_ready_snapshot(db, snapshot_id)
        if snap is None:
            raise ProjectSnapshotNotReady()
        if snap.source_id != source.id:
            raise ProjectSnapshotNotReady()  # 必须与文件同 source
        if not _maps_file(db, snapshot_id, fv.id):
            raise ProjectSnapshotNotReady()
        return snap

    snap = _pick_mapped_ready_snapshot(db, source, fv.id)
    if snap is None:
        raise ProjectSnapshotNotReady()
    return snap


def resolve_file_preview_snapshot(
    db: DBSession, file_version_id: uuid.UUID, user: User, snapshot_id: uuid.UUID | None = None
) -> tuple[ProjectFileVersion, ProjectSnapshot]:
    """校验并返回 (file_version, 授权 ready 快照)。供 API 层复用。"""
    fv = db.get(ProjectFileVersion, file_version_id)
    if fv is None:
        raise ProjectSourceNotFound()
    snap = _authorized_snapshot_for_file(db, fv, user, snapshot_id)
    return fv, snap


def read_project_source(
    db: DBSession,
    file_version_id: uuid.UUID,
    user: User,
    *,
    snapshot_id: uuid.UUID | None = None,
    expected_source_id: uuid.UUID | None = None,
    chunk_start: int = 0,
    chunk_end: int = MAX_PREVIEW_CHUNKS - 1,
) -> ProjectSourcePreviewRead:
    """读某文件版本在一个授权快照内的不可变分块范围（安全裁剪）。

    expected_source_id 供路由层绑定：文件版本必须先属于路由 source，否则 404，
    防止"各自可见"的文件被拿到别的源路由下读取（不只单独可见）。
    """
    fv = db.get(ProjectFileVersion, file_version_id)
    if fv is None:
        raise ProjectSourceNotFound()
    if expected_source_id is not None and fv.source_id != expected_source_id:
        raise ProjectSourceNotFound()
    fv, snap = resolve_file_preview_snapshot(db, file_version_id, user, snapshot_id)
    source = db.get(ProjectSource, fv.source_id)

    if not isinstance(chunk_start, int) or isinstance(chunk_start, bool) or chunk_start < 0:
        chunk_start = 0
    if not isinstance(chunk_end, int) or isinstance(chunk_end, bool):
        chunk_end = chunk_start + MAX_PREVIEW_CHUNKS - 1
    if chunk_end < chunk_start:
        chunk_end = chunk_start
    chunk_end = min(chunk_end, chunk_start + MAX_PREVIEW_CHUNKS - 1)

    rows = db.execute(
        select(ProjectChunk.chunk_index, ProjectChunk.content,
               ProjectChunk.line_start, ProjectChunk.line_end)
        .where(
            ProjectChunk.file_version_id == fv.id,
            ProjectChunk.chunk_index >= chunk_start,
            ProjectChunk.chunk_index <= chunk_end,
        )
        .order_by(ProjectChunk.chunk_index)
    ).all()
    chunks: list[ProjectSourceChunkRead] = []
    total_chars = 0
    truncated = False
    for index, content, ls, le in rows:
        content = content or ""
        if chunks and total_chars + len(content) > MAX_PREVIEW_CHARS:
            # 已有内容且再加会超字符上限：明确截断，不再返回伪完整/垃圾 tail。
            truncated = True
            break
        if not chunks and len(content) > MAX_PREVIEW_CHARS:
            # 首个 chunk 本身超上限：只返回前 MAX_PREVIEW_CHARS，并明确截断。
            content = content[:MAX_PREVIEW_CHARS]
            truncated = True
        chunks.append(
            ProjectSourceChunkRead(
                chunk_index=index, content=content, line_start=ls, line_end=le
            )
        )
        total_chars += len(content)
        if total_chars >= MAX_PREVIEW_CHARS:
            truncated = True
            break

    return ProjectSourcePreviewRead(
        source_id=source.id if source else fv.source_id,
        snapshot_id=snap.id,
        file_version_id=fv.id,
        relative_path=fv.relative_path,
        language=fv.language,
        parse_status=fv.parse_status,
        chunk_start=chunk_start,
        chunk_end=chunks[-1].chunk_index if chunks else chunk_end,
        chunks=chunks,
        truncated=truncated,
    )
