"""项目源的持久化 / 授权根绑定 / 读写守卫（Q 独占，纯服务 + 事务）。

写接口仅管理员；读接口沿用知识库 ACL（私有 project KB，root_key 映射不暴露
绝对根）。API 只接受 ``root_key`` + ``relative_dir``，绝不接受任意绝对路径。
root_fingerprint 使用 P 的 ``RootAnchor.root_fingerprint``（pin 目录身份指纹），换树必须拒绝。

删除源/知识库先事务内 ``revoked``，使后续授权读取返回 410，再取消活跃任务；
绝不删除原目录、不自动 GC、保留历史快照（旧会话仍引用）。
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.models import (
    KNOWLEDGE_BASE_KIND_PROJECT,
    KnowledgeBase,
    ProjectSnapshot,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
)
from app.schemas.project import ProjectSourceRead
from app.services.project_paths import (
    ProjectPathError,
    RootAnchor,
    make_root_binding,
    secure_source_root,
    split_relative_path,
)


class ProjectSourceNotFound(Exception):
    pass


class ProjectSourceRevoked(Exception):
    """授权读取命中已 revoke 的源：按任务返回 410，不暴露原路径。"""


class ProjectRootError(ProjectPathError):
    """授权根绑定/子树校验失败（缺映射、换树、含链接组件等）。"""


def _source_root_path(root_key: str, relative_dir: str) -> Path:
    """由授权根映射 + 安全相对子目录解析出源根（绝不接收绝对路径）。"""
    mapping = settings.PROJECT_ALLOWED_ROOTS
    root_value = mapping.get(root_key) if isinstance(mapping, dict) else None
    if not root_value:
        raise ProjectRootError("授权根未配置或 root_key 无效")
    root = Path(root_value)
    if not relative_dir:
        return root
    components = split_relative_path(relative_dir)
    return root.joinpath(*components)


def _reject_link_leading(source_root: Path, authorized_root: Path) -> None:
    """拒绝授权根到源根之间任何组件为符号链接 / reparse（防换指逃逸）。

    只做轻量守卫，不重写 P 的打开层；P 的 open_secure_regular 会在打开时再次
    拒绝链接/reparse，双保险。
    """
    try:
        rel = source_root.relative_to(authorized_root)
    except ValueError as exc:
        raise ProjectRootError("源根不在授权根之下") from exc
    cur = authorized_root
    for part in rel.parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except OSError as err:
            raise ProjectRootError(f"子目录不可用: {cur} ({err.strerror})") from err
        if getattr(st, "st_mode", 0) & 0xF000 == 0xA000:  # S_IFLNK
            raise ProjectRootError(f"子目录组件是符号链接: {part!r}")


def _authorized_binding(root_key: str):
    """解析并核验授权根（服务端配置映射），返回 P 的安全 RootBinding。

    未知 root_key → 受控 ProjectRootError（绝不 KeyError→500）；根须是真实目录、
    非 UNC、非符号链接/reparse（P 的 make_root_binding 全部核验）。
    """
    mapping = settings.PROJECT_ALLOWED_ROOTS
    root_value = mapping.get(root_key) if isinstance(mapping, dict) else None
    if not root_value:
        raise ProjectRootError("授权根未配置或 root_key 无效")
    try:
        return make_root_binding(root_key, Path(root_value))
    except ProjectPathError as exc:
        raise ProjectRootError(str(exc)) from exc


def resolve_source_root(root_key: str, relative_dir: str) -> Path:
    """解析源根并做 P 核验的根绑定 + 链接前置守卫。

    以 P 的 RootBinding 为安全判据（根非链接/reparse/UNC、真实指纹），补一层
    子目录组件的轻量链接守卫；真实创建/同步的指纹一致性由 compute_root_fingerprint
    与 P 的安全打开在新工作流中再次复核，不以 Path.is_dir 当作安全判据。
    """
    binding = _authorized_binding(root_key)
    source = _source_root_path(root_key, relative_dir)
    _reject_link_leading(source, binding.root)
    if not source.is_dir():
        raise ProjectRootError(f"源根目录不存在或不是目录: {source}")
    return source


def open_source_anchor(root_key: str, relative_dir: str) -> RootAnchor:
    """P 修订契约：把“授权根 + 相对子目录”解析为**被 pin 的 RootAnchor**。

    ``with secure_source_root(Path(config[root_key]), split_relative_path(relative_dir))
    as anchor``——持有期间 ``anchor.root_fingerprint()`` 是 **pin 身份**（换树必变），
    同步的创建时/同步时指纹都在同一 pin 内比较。调用方必须 ```with`` 持有整个
    scan/创建，绝不 verify 后返回 Path 再重新打开树（那会把 pin 与后续 open 之间
    留下换树窗口）。``relative_dir`` 为空时返回授权根本身的锚定。
    """
    mapping = settings.PROJECT_ALLOWED_ROOTS
    root_value = mapping.get(root_key) if isinstance(mapping, dict) else None
    if not root_value:
        raise ProjectRootError("授权根未配置或 root_key 无效")
    root = Path(root_value).expanduser()
    rel = split_relative_path(relative_dir) if relative_dir else ()
    try:
        return secure_source_root(root, rel)
    except ProjectPathError as exc:
        raise ProjectRootError(str(exc)) from exc


def compute_root_fingerprint(root_key: str, relative_dir: str) -> str:
    """源根身份的 **pin 指纹**（RootAnchor.root_fingerprint），换树时同步必须拒绝。

    在同一个被 pin 的 anchor 内求值（创建/同步一致）：绝不逐个 verify 后返回
    Path 再重开树。换树/重绑隐式改变 pin 身份，从而让同步的指纹比较失败。
    """
    with open_source_anchor(root_key, relative_dir) as anchor:
        return anchor.root_fingerprint()


def create_project_source(
    db: DBSession,
    admin: User,
    *,
    name: str,
    root_key: str,
    relative_dir: str,
) -> tuple[ProjectSource, KnowledgeBase]:
    """创建源（管理员）：先建 kind='project' 专用私有 KB，再建 ProjectSource。

    返回 (source, kb)。事务由 API 层负责提交/回滚。
    """
    name = (name or "").strip()
    if not name:
        raise ProjectRootError("源名称不能为空")
    if db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == name)):
        raise ProjectRootError("知识库名称已存在")
    # 解析并验证授权根绑定（缺映射/链接组件/换树在此失败，不落库）。
    fingerprint = compute_root_fingerprint(root_key, relative_dir)
    kb = KnowledgeBase(
        name=name,
        description="",
        collection_name=f"{settings.PROJECT_VECTOR_PREFIX}{uuid.uuid4().hex}",
        owner_id=admin.id,
        is_public=False,
        kind=KNOWLEDGE_BASE_KIND_PROJECT,
    )
    db.add(kb)
    db.flush()
    source = ProjectSource(
        kb_id=kb.id,
        name=name,
        root_key=root_key,
        relative_dir=relative_dir,
        root_fingerprint=fingerprint,
        created_by=admin.id,
    )
    db.add(source)
    db.flush()
    return source, kb


def source_to_read(source: ProjectSource) -> ProjectSourceRead:
    return ProjectSourceRead(
        id=source.id,
        kb_id=source.kb_id,
        name=source.name,
        root_key=source.root_key,
        relative_dir=source.relative_dir,
        active_snapshot_id=source.active_snapshot_id,
        revoked_at=source.revoked_at,
        created_at=source.created_at,
    )


def _kb_visible(kb: KnowledgeBase, user: User) -> bool:
    if user.is_admin:
        return True
    return bool(kb.is_public) or kb.owner_id == user.id


def _source_or_gone(source: ProjectSource | None, user: User) -> ProjectSource:
    if source is None:
        raise ProjectSourceNotFound()
    kb = source.knowledge_base
    if kb is None or not _kb_visible(kb, user):
        raise ProjectSourceNotFound()
    if source.revoked_at is not None:
        raise ProjectSourceRevoked()
    return source


def get_project_source(
    db: DBSession, source_id: uuid.UUID, user: User
) -> ProjectSource:
    source = db.get(ProjectSource, source_id)
    return _source_or_gone(source, user)


def get_project_source_by_kb(
    db: DBSession, kb_id: uuid.UUID, user: User
) -> ProjectSource:
    source = db.scalar(select(ProjectSource).where(ProjectSource.kb_id == kb_id))
    return _source_or_gone(source, user)


def list_visible_sources(db: DBSession, user: User) -> list[ProjectSource]:
    """按 KB ACL 返回可见且未 revoke 的源（管理员全部可见）。"""
    stmt = select(ProjectSource)
    if not user.is_admin:
        stmt = stmt.join(KnowledgeBase).where(
            KnowledgeBase.is_public.is_(True)
            | (KnowledgeBase.owner_id == user.id)
        )
    stmt = stmt.where(ProjectSource.revoked_at.is_(None)).order_by(
        ProjectSource.created_at.desc()
    )
    return list(db.scalars(stmt).all())


def _cancel_active_job(db: DBSession, source_id: uuid.UUID) -> None:
    """取消源上所有 pending/processing 快照（释放租约，交由 worker 回收）。"""
    active = db.scalars(
        select(ProjectSnapshot)
        .where(
            ProjectSnapshot.source_id == source_id,
            ProjectSnapshot.status.in_(
                [
                    ProjectSnapshotStatus.PENDING.value,
                    ProjectSnapshotStatus.PROCESSING.value,
                ]
            ),
        )
        .with_for_update()
    ).all()
    now = datetime.now(timezone.utc)
    for snap in active:
        snap.cancel_requested = True
        snap.status = ProjectSnapshotStatus.CANCELLED.value
        snap.lease_id = None
        snap.heartbeat_at = None
        snap.finished_at = now
        snap.error_code = "cancelled-revoked"
        snap.error_message = "源已删除/撤销，任务取消"
        snap.progress = snap.progress
        db.flush()


def revoke_source(db: DBSession, source: ProjectSource) -> None:
    """软删除源：先 revoke + 取消活跃任务，再让授权读取返回 410。

    不删除原目录、不自动 GC、不硬删快照（旧会话/引用仍有效）。
    """
    source.revoked_at = datetime.now(timezone.utc)
    _cancel_active_job(db, source.id)
    db.flush()


def revoke_source_by_kb(db: DBSession, kb_id: uuid.UUID) -> None:
    """知识库删除的一致性路径：软撤销对应源（若存在），不级联硬删快照。"""
    source = db.scalar(select(ProjectSource).where(ProjectSource.kb_id == kb_id))
    if source is not None and source.revoked_at is None:
        revoke_source(db, source)
        db.flush()


def cancel_active_snapshot(db: DBSession, source: ProjectSource) -> ProjectSnapshot | None:
    """用户明确取消源上的活动同步（与 revoke 的强制取消语义一致）。

    - pending（尚未被 worker 领取）：直接置 cancelled，清租约。
    - processing（持租约）：只置 cancel_requested=True，由 worker 心跳观察到
      cancel_requested 后合作转为 cancelled（保留 lease 完整性，不混停机回退）。
    返回被标记的快照（无活动任务时返回 None）。源须已由调用方鉴权。
    """
    active = db.scalars(
        select(ProjectSnapshot)
        .where(
            ProjectSnapshot.source_id == source.id,
            ProjectSnapshot.status.in_(
                [ProjectSnapshotStatus.PENDING.value, ProjectSnapshotStatus.PROCESSING.value]
            ),
        )
        .with_for_update()
    ).all()
    now = datetime.now(timezone.utc)
    target: ProjectSnapshot | None = None
    for snap in active:
        target = snap
        snap.cancel_requested = True
        if snap.status == ProjectSnapshotStatus.PENDING.value:
            snap.status = ProjectSnapshotStatus.CANCELLED.value
            snap.lease_id = None
            snap.heartbeat_at = None
            snap.finished_at = now
            snap.error_code = "cancelled-user"
            snap.error_message = "已取消"
    db.flush()
    return target
