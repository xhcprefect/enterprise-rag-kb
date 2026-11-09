"""本地项目文档/源码索引 API（Q 独占）。

统一挂在 ``/api/v1/projects`` 下。写接口仅管理员；读接口沿用知识库 ACL
（私有 project KB 的 root_key/root 映射不暴露绝对根）。绝不接受任意绝对路径。

检查点：
- 源：POST/GET/LIST/DELETE（软撤销 -> 410 读取、取消活动任务，不删原目录）。
- 同步：POST sync（enqueue，幂等）、GET snapshots（状态/历史）、POST sync/cancel。
- 文件：GET files（按快照视图列文件版本）。
- 原文预览：GET files/{file_version_id}/source（只凭 UUID+授权 ready 快照映射）。
- 独立搜索：GET search（不生成回答，沿 KB ACL，可指定快照/路径前缀）。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, require_admin
from app.core.config import settings
from app.db.session import get_db
from app.models import (
    ProjectFileVersion,
    ProjectSnapshot,
    ProjectSnapshotFile,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
)
from app.schemas.common import ApiResponse, PageResult
from app.schemas.project import (
    MAX_SEARCH_QUERY,
    MAX_TOP_K,
    ProjectAutoSyncUpdate,
    ProjectFileRead,
    ProjectSearchRead,
    ProjectSnapshotRead,
    ProjectSourceCreate,
    ProjectSourcePreviewRead,
    ProjectSourceRead,
)
from app.services import project_index, project_retrieval, project_source_documents, project_sources

router = APIRouter(prefix="/projects", tags=["项目源"])

logger = __import__("logging").getLogger(__name__)

# 文件列表分页硬界。
FILE_PAGE_SIZE = 100


def _source_or_http(
    db: DBSession, source_id: uuid.UUID, user: User
) -> ProjectSource:
    try:
        return project_sources.get_project_source(db, source_id, user)
    except project_sources.ProjectSourceNotFound as exc:
        raise HTTPException(status_code=404, detail="项目源不存在") from exc
    except project_sources.ProjectSourceRevoked as exc:
        raise HTTPException(status_code=410, detail="项目源已撤销") from exc


def _snap_out(snap: ProjectSnapshot) -> ProjectSnapshotRead:
    return ProjectSnapshotRead(
        id=snap.id,
        source_id=snap.source_id,
        status=snap.status,
        trigger_source=snap.trigger_source,
        base_snapshot_id=snap.base_snapshot_id,
        progress=snap.progress,
        file_count=snap.file_count,
        total_bytes=snap.total_bytes,
        manifest_fingerprint=snap.manifest_fingerprint,
        config_fingerprint=snap.config_fingerprint,
        error_code=snap.error_code,
        error_message=snap.error_message,
        cancel_requested=snap.cancel_requested,
        started_at=snap.started_at,
        finished_at=snap.finished_at,
        created_at=snap.created_at,
    )


def _view_snapshot(
    db: DBSession, source: ProjectSource, user: User, snapshot_id: uuid.UUID | None
) -> ProjectSnapshot:
    """当前文件列表/搜索所用的视图快照：显式指定，否则 active，再退最近 ready。

    显式 snapshot_id 经 project_retrieval.validate_project_binding 做 KB ACL +
    源未 revoke 校验（404/410）；未达 ready 抛 409（提示先同步）。
    """
    if snapshot_id is not None:
        try:
            snap, _src, _kb = project_retrieval.validate_project_binding(
                db, snapshot_id, user
            )
        except project_sources.ProjectSourceNotFound as exc:
            raise HTTPException(status_code=404, detail="快照不存在或未就绪") from exc
        except project_sources.ProjectSourceRevoked as exc:
            raise HTTPException(status_code=410, detail="项目源已撤销") from exc
        except project_retrieval.ProjectSnapshotNotReady as exc:
            raise HTTPException(status_code=404, detail="快照不存在或未就绪") from exc
        # 显式 sid 必须属于当前路由 source：别的源快照不得在 /projects/A 下被读。
        if snap.source_id != source.id:
            raise HTTPException(status_code=404, detail="快照不存在或未就绪")
        return snap
    sid = source.active_snapshot_id
    if sid is not None:
        snap = db.get(ProjectSnapshot, sid)
        if snap is not None and snap.status == ProjectSnapshotStatus.READY.value:
            return snap
    latest = db.scalar(
        select(ProjectSnapshot)
        .where(
            ProjectSnapshot.source_id == source.id,
            ProjectSnapshot.status == ProjectSnapshotStatus.READY.value,
        )
        .order_by(ProjectSnapshot.created_at.desc())
        .limit(1)
    )
    if latest is None:
        raise HTTPException(status_code=409, detail="项目源尚无就绪快照，请先同步")
    return latest


@router.get("", response_model=ApiResponse[list[ProjectSourceRead]], summary="项目源列表")
def list_sources(
    db: DBSession = Depends(get_db), user: User = Depends(get_current_user)
):
    sources = project_sources.list_visible_sources(db, user)
    return ApiResponse(data=[project_sources.source_to_read(s) for s in sources])


@router.post(
    "", response_model=ApiResponse[ProjectSourceRead], status_code=201, summary="登记项目源（管理员）"
)
def create_source(
    payload: ProjectSourceCreate,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    try:
        source, _kb = project_sources.create_project_source(
            db, admin, name=payload.name, root_key=payload.root_key, relative_dir=payload.relative_dir
        )
        db.commit()
    except project_sources.ProjectRootError as exc:
        # 不向用户回显 str(exc)：其中可能含本机授权根/源根绝对路径。稳定中文泛化错误。
        logger.warning("登记项目源失败（root_key=%r）: %s", payload.root_key, type(exc).__name__)
        db.rollback()
        raise HTTPException(status_code=400, detail="授权根无效或源根不可用，请检查 root_key 与相对子目录") from exc
    db.refresh(source)
    return ApiResponse(data=project_sources.source_to_read(source))


@router.get("/roots", response_model=ApiResponse[list[str]], summary="可用的授权 root_key 列表（仅管理员）")
def list_roots(admin: User = Depends(require_admin)):
    """只回显已配置的 root_key（稳定顺序），绝不返回授权根的绝对路径。"""
    roots = dict(settings.PROJECT_ALLOWED_ROOTS or {})
    return ApiResponse(data=sorted(roots.keys()))


@router.get("/{source_id}", response_model=ApiResponse[ProjectSourceRead], summary="单个项目源")
def get_source(
    source_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = _source_or_http(db, source_id, user)
    return ApiResponse(data=project_sources.source_to_read(source))


@router.delete("/{source_id}", response_model=ApiResponse[None], summary="撤销项目源（管理员，软删除）")
def revoke_source(
    source_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    source = _source_or_http(db, source_id, admin)
    project_sources.revoke_source(db, source)
    db.commit()
    return ApiResponse(data=None, message="项目源已撤销")


@router.patch(
    "/{source_id}/auto-sync",
    response_model=ApiResponse[ProjectSourceRead],
    summary="配置定时自动同步（管理员）",
)
def update_auto_sync(
    source_id: uuid.UUID,
    payload: ProjectAutoSyncUpdate,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """启用/调整/关闭自动同步；关闭时只取消该源 auto 触发的活动任务。"""
    try:
        source = project_sources.update_auto_sync(
            db,
            source_id,
            enabled=payload.enabled,
            interval_seconds=payload.interval_seconds,
        )
        db.commit()
    except project_sources.ProjectSourceNotFound as exc:
        # 不在此 rollback：会过期同一请求会话里的认证 User，请求日志随后读取
        # user.username 会触发 DetachedInstanceError，把 404 变成 500。
        # 依赖注入的 get_db 会在 finally 关闭会话并回滚；close 不会过期已加载属性。
        raise HTTPException(status_code=404, detail="项目源不存在") from exc
    except project_sources.ProjectSourceRevoked as exc:
        # 同上：410 分支同样不得手动 rollback。
        raise HTTPException(status_code=410, detail="项目源已撤销") from exc
    db.refresh(source)
    return ApiResponse(data=project_sources.source_to_read(source))


@router.post("/{source_id}/sync", response_model=ApiResponse[ProjectSnapshotRead], summary="手动同步（管理员，幂等）")
def sync_source(
    source_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    source = _source_or_http(db, source_id, admin)
    try:
        snap = project_index.enqueue_project_sync(db, source_id, admin)
        db.commit()
    except project_sources.ProjectSourceNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail="项目源不存在") from exc
    except project_sources.ProjectSourceRevoked as exc:
        db.rollback()
        raise HTTPException(status_code=410, detail="项目源已撤销") from exc
    db.refresh(snap)
    return ApiResponse(data=_snap_out(snap))


@router.get("/{source_id}/snapshots", response_model=ApiResponse[list[ProjectSnapshotRead]], summary="同步状态/历史")
def list_snapshots(
    source_id: uuid.UUID,
    include_unchanged: bool = Query(default=False),
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """历史默认隐藏 unchanged 检查记录（避免淹没版本历史）；显式 true 时包含。"""
    _source_or_http(db, source_id, user)
    stmt = select(ProjectSnapshot).where(ProjectSnapshot.source_id == source_id)
    if not include_unchanged:
        stmt = stmt.where(
            ProjectSnapshot.status != ProjectSnapshotStatus.UNCHANGED.value
        )
    snaps = db.scalars(
        stmt.order_by(ProjectSnapshot.created_at.desc()).limit(50)
    ).all()
    return ApiResponse(data=[_snap_out(s) for s in snaps])


@router.post("/{source_id}/sync/cancel", response_model=ApiResponse[ProjectSnapshotRead], summary="取消活动同步（管理员）")
def cancel_sync(
    source_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    source = _source_or_http(db, source_id, admin)
    target = project_sources.cancel_active_snapshot(db, source)
    db.commit()
    if target is None:
        raise HTTPException(status_code=409, detail="没有活动中的同步任务")
    db.refresh(target)
    return ApiResponse(data=_snap_out(target))


@router.get("/{source_id}/files", response_model=ApiResponse[PageResult[ProjectFileRead]], summary="文件列表（快照视图）")
def list_files(
    source_id: uuid.UUID,
    snapshot_id: uuid.UUID | None = Query(default=None),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=FILE_PAGE_SIZE),
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = _source_or_http(db, source_id, user)
    snap = _view_snapshot(db, source, user, snapshot_id)
    base = (
        select(ProjectFileVersion)
        .join(
            ProjectSnapshotFile,
            ProjectSnapshotFile.file_version_id == ProjectFileVersion.id,
        )
        .where(ProjectSnapshotFile.snapshot_id == snap.id)
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(ProjectFileVersion.relative_path)
        .offset(offset)
        .limit(limit)
    ).all()
    items = []
    for fv in rows:
        items.append(
            ProjectFileRead(
                file_version_id=fv.id,
                source_id=fv.source_id,
                snapshot_id=snap.id,
                relative_path=fv.relative_path,
                content_sha256=fv.content_sha256,
                size=fv.size,
                language=fv.language,
                parse_status=fv.parse_status,
                chunk_count=fv.chunk_count,
                char_count=fv.char_count,
                created_at=fv.created_at,
            )
        )
    return ApiResponse(data=PageResult(total=total, items=items))


@router.get(
    "/{source_id}/files/{file_version_id}/source",
    response_model=ApiResponse[ProjectSourcePreviewRead],
    summary="读取项目引用原文（只凭 UUID，沿 KB ACL）",
)
def read_source(
    source_id: uuid.UUID,
    file_version_id: uuid.UUID,
    snapshot_id: uuid.UUID | None = Query(default=None),
    chunk_start: int = Query(default=0, ge=0),
    chunk_end: int | None = Query(default=None, ge=0),
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = _source_or_http(db, source_id, user)
    try:
        result = project_source_documents.read_project_source(
            db,
            file_version_id,
            user,
            snapshot_id=snapshot_id,
            expected_source_id=source.id,
            chunk_start=chunk_start,
            chunk_end=chunk_end if chunk_end is not None else project_source_documents.MAX_PREVIEW_CHUNKS - 1,
        )
    except project_source_documents.ProjectSourceNotFound as exc:
        raise HTTPException(status_code=404, detail="项目源或文件不存在") from exc
    except project_source_documents.ProjectSourceRevoked as exc:
        raise HTTPException(status_code=410, detail="项目源已撤销") from exc
    except project_source_documents.ProjectSnapshotNotReady as exc:
        raise HTTPException(status_code=404, detail="快照未就绪或文件中不在该快照") from exc
    return ApiResponse(data=result)


@router.get("/{source_id}/search", response_model=ApiResponse[ProjectSearchRead], summary="项目内容独立搜索（沿 KB ACL）")
def search(
    source_id: uuid.UUID,
    query: str = Query(..., max_length=MAX_SEARCH_QUERY),
    snapshot_id: uuid.UUID | None = Query(default=None),
    top_k: int = Query(default=8, ge=1, le=MAX_TOP_K),
    path_prefix: str | None = Query(default=None, max_length=1024),
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = _source_or_http(db, source_id, user)
    snap = _view_snapshot(db, source, user, snapshot_id)
    try:
        result = project_retrieval.search_project(
            db, snap.id, user, query, top_k=top_k, path_prefix=path_prefix
        )
    except project_retrieval.ProjectSnapshotNotReady as exc:
        raise HTTPException(status_code=409, detail="项目索引未就绪") from exc
    return ApiResponse(data=result)
