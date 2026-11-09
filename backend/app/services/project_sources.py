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
from datetime import datetime, timedelta, timezone
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
from app.schemas.project import (
    AUTO_SYNC_MAX_INTERVAL_SECONDS,
    AUTO_SYNC_MIN_INTERVAL_SECONDS,
    ProjectSourceRead,
)
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


class AutoSyncDisabled(Exception):
    """内部自动调度在源行锁内发现“自动同步已被关闭”。

    领取到期源与真正入队之间存在窗口；管理员可能在窗口内关闭自动同步。入队
    必须在**同一源行锁事务**内重新核验开关，关闭时抛出本异常由调度器静默跳过，
    绝不新建 auto 快照、不误报入队失败。
    """


# 关闭自动同步时取消其活动任务的稳定错误码与固定安全消息。
AUTO_SYNC_CANCELLED_CODE = "cancelled-auto-disabled"
AUTO_SYNC_CANCELLED_MESSAGE = "自动同步已关闭，任务取消"


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
        auto_sync_enabled=source.auto_sync_enabled,
        auto_sync_interval_seconds=source.auto_sync_interval_seconds,
        auto_sync_next_check_at=source.auto_sync_next_check_at,
        auto_sync_last_checked_at=source.auto_sync_last_checked_at,
        auto_sync_last_error_code=source.auto_sync_last_error_code,
        auto_sync_last_error_message=source.auto_sync_last_error_message,
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


def _lock_kb_row(db: DBSession, kb_id: uuid.UUID) -> KnowledgeBase | None:
    """锁锁序里的第一个资源根：KnowledgeBase（``FOR UPDATE`` + 强制重读）。"""
    return db.execute(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == kb_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _lock_source_row(db: DBSession, source_id: uuid.UUID) -> ProjectSource | None:
    """按全库统一锁序 ``KnowledgeBase -> ProjectSource`` 取得源的**新鲜**行。

    调用方传来的 ``ProjectSource`` 可能来自事务外/identify map，绝不能用它做判断
    或写回。这里先只读取出 ``kb_id``（仅用于确定先锁哪一行），锁住 KB 后再
    ``FOR UPDATE + populate_existing`` 重读源行并复核 kb_id 未变；任何一步缺失都
    返回 None（由调用方按各自语义处理，绝不冒用旧 ORM 数据）。
    """
    kb_id = db.scalar(select(ProjectSource.kb_id).where(ProjectSource.id == source_id))
    if kb_id is None:
        return None
    if _lock_kb_row(db, kb_id) is None:
        return None
    source = db.execute(
        select(ProjectSource)
        .where(ProjectSource.id == source_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if source is None or source.kb_id != kb_id:
        return None
    return source


def _cancel_active_job(
    db: DBSession,
    source_id: uuid.UUID,
    *,
    trigger_source: str | None = None,
    error_code: str = "cancelled-revoked",
    error_message: str = "源已删除/撤销，任务取消",
) -> None:
    """取消源上所有 pending/processing 快照（释放租约，交由 worker 回收）。

    必须在 Source 行锁已持后调用；快照按 UUID 排序加锁（与全库统一类内顺序一致），
    ``now`` 只等锁之后取。``trigger_source`` 非空时只取消该来源的活动任务（用于
    关闭自动同步：只取消 auto 任务，绝不误取消手动任务）。
    """
    stmt = select(ProjectSnapshot).where(
        ProjectSnapshot.source_id == source_id,
        ProjectSnapshot.status.in_(
            [
                ProjectSnapshotStatus.PENDING.value,
                ProjectSnapshotStatus.PROCESSING.value,
            ]
        ),
    )
    if trigger_source is not None:
        stmt = stmt.where(ProjectSnapshot.trigger_source == trigger_source)
    active = db.scalars(
        stmt.order_by(ProjectSnapshot.id).with_for_update()
    ).all()
    now = datetime.now(timezone.utc)
    for snap in active:
        snap.cancel_requested = True
        snap.status = ProjectSnapshotStatus.CANCELLED.value
        snap.lease_id = None
        snap.heartbeat_at = None
        snap.finished_at = now
        snap.error_code = error_code
        snap.error_message = error_message
        snap.progress = snap.progress
        db.flush()


def revoke_source(db: DBSession, source: ProjectSource) -> ProjectSource | None:
    """软删除源：先 revoke + 取消活跃任务，再让授权读取返回 410。

    不删除原目录、不自动 GC、不硬删快照（旧会话/引用仍有效）。

    调用方传入的 ``source`` 可能来自无锁 ``db.get``（API 鉴权路径），因此这里**不
    使用它的字段做决策**：按全库统一锁序 ``KnowledgeBase -> ProjectSource ->
    ProjectSnapshot`` 重新加锁并强制重读，任何写入都基于锁后行的最新值。并发删除
    后源已不存在时返回 None（调用方语义仍视为已撤销）。
    """
    locked = _lock_source_row(db, source.id)
    if locked is None:
        return None
    if locked.revoked_at is None:
        # 等锁后才取时钟：排队期间的时间不算进撤销时刻。
        locked.revoked_at = datetime.now(timezone.utc)
    _cancel_active_job(db, locked.id)
    db.flush()
    return locked


def revoke_source_by_kb(db: DBSession, kb_id: uuid.UUID) -> None:
    """知识库删除的一致性路径：软撤销对应源（若存在），不级联硬删快照。

    锁序同样从 KB 开始（调用方 delete_kb 已持该 KB 锁，重复加锁在同一事务内是
    无操作），再锁源、再按 UUID 顺序锁活跃快照。
    """
    if _lock_kb_row(db, kb_id) is None:
        return
    source_id = db.scalar(select(ProjectSource.id).where(ProjectSource.kb_id == kb_id))
    if source_id is None:
        return
    locked = _lock_source_row(db, source_id)
    if locked is None or locked.revoked_at is not None:
        return
    locked.revoked_at = datetime.now(timezone.utc)
    _cancel_active_job(db, locked.id)
    db.flush()


def cancel_active_snapshot(db: DBSession, source: ProjectSource) -> ProjectSnapshot | None:
    """用户明确取消源上的活动同步（与 revoke 的强制取消语义一致）。

    - pending（尚未被 worker 领取）：直接置 cancelled，清租约。
    - processing（持租约）：只置 cancel_requested=True，由 worker 心跳观察到
      cancel_requested 后合作转为 cancelled（保留 lease 完整性，不混停机回退）。
    返回被标记的快照（无活动任务时返回 None）。源须已由调用方鉴权。

    与 revoke 相同：调用方传入的源对象不作为写入依据，入口先按统一锁序
    ``KnowledgeBase -> ProjectSource -> ProjectSnapshot(UUID 序)`` 取得新鲜行；
    源已消失/已撤销时返回 None（API 层按"没有活动任务"处理）。
    """
    locked = _lock_source_row(db, source.id)
    if locked is None or locked.revoked_at is not None:
        return None
    active = db.scalars(
        select(ProjectSnapshot)
        .where(
            ProjectSnapshot.source_id == locked.id,
            ProjectSnapshot.status.in_(
                [ProjectSnapshotStatus.PENDING.value, ProjectSnapshotStatus.PROCESSING.value]
            ),
        )
        .order_by(ProjectSnapshot.id)
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


def update_auto_sync(
    db: DBSession,
    source_id: uuid.UUID,
    *,
    enabled: bool,
    interval_seconds: int,
) -> ProjectSource:
    """管理员开关/调整定时自动同步（复用源行锁取得新鲜行）。

    语义（冻结契约）：
    - 启用时 ``next_check_at = now``（立即纳入下一轮调度）；
    - 已启用且间隔变化时 ``next_check_at = now + interval``；
    - 禁用时清空 ``next_check_at`` 与最近错误，并**只取消 trigger_source='auto'**
      的 pending/processing 快照（已 ready/unchanged/failed 的终态绝不动，手动
      任务也绝不误取消）。
    源不存在 -> ``ProjectSourceNotFound``；已撤销 -> ``ProjectSourceRevoked``。
    """
    interval = int(interval_seconds)
    if not AUTO_SYNC_MIN_INTERVAL_SECONDS <= interval <= AUTO_SYNC_MAX_INTERVAL_SECONDS:
        raise ValueError(
            f"自动同步间隔必须在 {AUTO_SYNC_MIN_INTERVAL_SECONDS}~"
            f"{AUTO_SYNC_MAX_INTERVAL_SECONDS} 秒之间"
        )
    locked = _lock_source_row(db, source_id)
    if locked is None:
        raise ProjectSourceNotFound()
    if locked.revoked_at is not None:
        raise ProjectSourceRevoked()

    was_enabled = bool(locked.auto_sync_enabled)
    previous_interval = int(locked.auto_sync_interval_seconds or 0)
    now = datetime.now(timezone.utc)
    locked.auto_sync_interval_seconds = interval
    if enabled:
        locked.auto_sync_enabled = True
        if not was_enabled:
            locked.auto_sync_next_check_at = now
        elif interval != previous_interval or locked.auto_sync_next_check_at is None:
            locked.auto_sync_next_check_at = now + timedelta(seconds=interval)
    else:
        locked.auto_sync_enabled = False
        locked.auto_sync_next_check_at = None
        locked.auto_sync_last_error_code = None
        locked.auto_sync_last_error_message = None
        # 只取消自动触发的活动任务；manual 任务保持原状。
        _cancel_active_job(
            db,
            locked.id,
            trigger_source="auto",
            error_code=AUTO_SYNC_CANCELLED_CODE,
            error_message=AUTO_SYNC_CANCELLED_MESSAGE,
        )
    db.flush()
    return locked
