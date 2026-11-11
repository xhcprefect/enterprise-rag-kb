"""知识库成员管理（仅管理员）的事务/锁/CAS 实现。

HTTP 契约（冻结）：

- ``GET  /knowledge-bases/{kb_id}/members``：管理员分页列表，data 为
  ``{revision, total, items}``；revision/total/items 在同一短 KB 锁内读取。
- ``PUT  /knowledge-bases/{kb_id}/members/{user_id}``：body ``{role, expected_revision}``，
  data 为 ``{revision, member}``。
- ``DELETE /knowledge-bases/{kb_id}/members/{user_id}?expected_revision=N``：
  data 为 ``{revision, member: null}``。

锁与并发（与 users.delete_user 共用同一顺序）：

1. 先认证（依赖层已完成），从 ``request.state.auth_token_version`` 取原始令牌
   版本快照；缺失/类型不对一律 401（fail closed）。
2. 一次按 UUID 升序锁 ``User``：管理员 + 目标用户。绝不先锁管理员再反向锁目标；
   等锁后只用新鲜行复核 admin/is_active/token_version。
3. 再锁 ``KnowledgeBase``（``FOR UPDATE`` + 强制重读），随后才锁成员行。
4. CAS 使用锁定后的 ``membership_revision``；冲突 409。无变化仍先核对版本，
   但不递增 revision，也不写成员行。

删除用户时由 ``users.delete_user`` 持有 User -> KB 锁并为确有成员行的 KB
递增 revision；本服务每次变更都在 commit 前递增 revision，因此成员列表的
revision 与集合不会出现“旧版本 + 新列表”。

审计细节只允许稳定 KB/User ID、角色与 revision，不写用户名、正文或路径。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models import (
    KB_MEMBER_ROLE_MAINTAINER,
    KB_MEMBER_ROLES,
    KnowledgeBase,
    KnowledgeBaseMember,
    User,
)
from app.schemas.kb_members import (
    KnowledgeBaseMemberList,
    KnowledgeBaseMemberRead,
)

logger = logging.getLogger(__name__)

_AUTH_INVALID = "登录状态已失效，请重新登录"
_ADMIN_REQUIRED = "需要管理员权限"

# 分页硬界与默认值（与 HTTP 契约一致）。
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class MemberMutation:
    """服务层变更结果；changed=False 表示幂等无变化（revision 未递增）。"""

    revision: int
    member: KnowledgeBaseMemberRead | None
    changed: bool
    previous_role: str | None


def _request_token_version(request: Request) -> int | None:
    """只读取认证依赖留下的原始快照；缺失/非严格 int 返回 None（调用方 fail closed）。"""
    try:
        version = getattr(request.state, "auth_token_version", None)
    except Exception:  # noqa: BLE001 - 极简 Request 可能没有 state
        return None
    return version if type(version) is int else None


def _lock_users_sorted(db: DBSession, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    """一次按 UUID 升序锁给定用户（``FOR UPDATE`` + 强制重读）。"""
    if not user_ids:
        return {}
    rows = db.scalars(
        select(User)
        .where(User.id.in_(list(user_ids)))
        .order_by(User.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {row.id: row for row in rows}


def lock_membership_admin(
    db: DBSession,
    request: Request,
    admin: User,
    target_id: uuid.UUID | None = None,
) -> tuple[User, User | None]:
    """锁管理员与（可选）目标用户并按认证快照复核管理员。

    返回 ``(fresh_admin, fresh_target_or_none)``。目标不存在不是错误——由具体
    操作决定 404 或幂等删除；管理员失败统一 401/403。
    """
    version = _request_token_version(request)
    if version is None or version < 0:
        raise HTTPException(status_code=401, detail=_AUTH_INVALID)
    ids = {admin.id}
    if target_id is not None:
        ids.add(target_id)
    locked = _lock_users_sorted(db, ids)
    fresh_admin = locked.get(admin.id)
    if fresh_admin is None:
        raise HTTPException(status_code=401, detail=_AUTH_INVALID)
    if not fresh_admin.is_active or not fresh_admin.is_admin:
        raise HTTPException(status_code=403, detail=_ADMIN_REQUIRED)
    if fresh_admin.token_version != version:
        raise HTTPException(status_code=401, detail=_AUTH_INVALID)
    # 请求日志/审计使用锁定后的新鲜管理员对象。
    request.state.user = fresh_admin
    return fresh_admin, locked.get(target_id) if target_id is not None else None


def _lock_kb(db: DBSession, kb_id: uuid.UUID, *, share: bool = False) -> KnowledgeBase:
    stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    stmt = stmt.with_for_update(read=True) if share else stmt.with_for_update()
    kb = db.scalar(stmt.execution_options(populate_existing=True))
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


def _require_revision(kb: KnowledgeBase, expected_revision) -> None:
    if type(expected_revision) is not int or expected_revision < 0:
        raise HTTPException(status_code=422, detail="expected_revision 必须是非负整数")
    if int(kb.membership_revision) != expected_revision:
        raise HTTPException(status_code=409, detail="成员修订号已变化，请刷新后重试")


def _member_view(user: User, role: str) -> KnowledgeBaseMemberRead:
    return KnowledgeBaseMemberRead(
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        is_active=bool(user.is_active),
        role=role,
    )


def list_members(
    db: DBSession, kb_id: uuid.UUID, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
) -> KnowledgeBaseMemberList:
    """管理员成员分页列表。

    调用方须先完成管理员鉴权（HTTP router 依赖 ``require_admin``）。这里在 KB
    行上取共享锁（``FOR SHARE``）：与成员写/删除用户所需的 ``FOR UPDATE`` 互斥，保证
    revision、total 与 items 来自同一个短快照，不会把旧 revision 搭配新列表。
    """
    if page < 1 or page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise HTTPException(status_code=422, detail="分页参数超出允许范围")
    kb = _lock_kb(db, kb_id, share=True)
    revision = int(kb.membership_revision)
    total = db.scalar(
        select(func.count()).select_from(KnowledgeBaseMember)
        .where(KnowledgeBaseMember.kb_id == kb.id)
    ) or 0
    rows = db.execute(
        select(KnowledgeBaseMember, User)
        .join(User, User.id == KnowledgeBaseMember.user_id)
        .where(KnowledgeBaseMember.kb_id == kb.id)
        .order_by(User.username.asc(), User.id.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    result = KnowledgeBaseMemberList(
        revision=revision,
        total=int(total),
        items=[_member_view(user, member.role) for member, user in rows],
    )
    # 结果已物化为 Pydantic 对象；立即释放共享锁，不把它拖到响应序列化之后。
    db.rollback()
    return result


def upsert_member(
    db: DBSession,
    request: Request,
    admin: User,
    kb_id: uuid.UUID,
    target_id: uuid.UUID,
    role: str,
    expected_revision,
) -> MemberMutation:
    """授予或改变成员角色；CAS 冲突 409，无变化幂等不递增 revision。"""
    if role not in KB_MEMBER_ROLES:
        raise HTTPException(status_code=422, detail="role 只允许 reader 或 maintainer")
    _, target = lock_membership_admin(db, request, admin, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    kb = _lock_kb(db, kb_id)
    _require_revision(kb, expected_revision)

    existing = db.scalar(
        select(KnowledgeBaseMember)
        .where(
            KnowledgeBaseMember.kb_id == kb.id,
            KnowledgeBaseMember.user_id == target.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None and existing.role == role:
        # 完全相同：先把列表元素物化，再回滚以释放锁；revision 不变。
        result = MemberMutation(
            revision=int(kb.membership_revision),
            member=_member_view(target, existing.role),
            changed=False,
            previous_role=existing.role,
        )
        db.rollback()
        return result

    # 新授予或真实角色变化前，目标必须处于启用状态（禁用用户不能获得/改变授权）。
    if not target.is_active:
        raise HTTPException(status_code=400, detail="目标用户已被禁用，不能授予或变更成员角色")

    previous_role = existing.role if existing is not None else None
    if existing is None:
        existing = KnowledgeBaseMember(kb_id=kb.id, user_id=target.id, role=role)
        db.add(existing)
    else:
        existing.role = role
    kb.membership_revision = int(kb.membership_revision) + 1
    revision = int(kb.membership_revision)
    db.commit()
    logger.info(
        "kb membership granted/updated: kb=%s user=%s role=%s revision=%s",
        kb.id, target.id, role, revision,
    )
    return MemberMutation(
        revision=revision,
        member=_member_view(target, role),
        changed=True,
        previous_role=previous_role,
    )


def remove_member(
    db: DBSession,
    request: Request,
    admin: User,
    kb_id: uuid.UUID,
    target_id: uuid.UUID,
    expected_revision,
) -> MemberMutation:
    """移除成员；无成员（含目标用户已不存在）幂等成功且不递增 revision。"""
    _, target = lock_membership_admin(db, request, admin, target_id)
    kb = _lock_kb(db, kb_id)
    _require_revision(kb, expected_revision)

    existing = db.scalar(
        select(KnowledgeBaseMember)
        .where(
            KnowledgeBaseMember.kb_id == kb.id,
            KnowledgeBaseMember.user_id == target_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is None:
        revision = int(kb.membership_revision)
        db.rollback()
        return MemberMutation(revision=revision, member=None, changed=False, previous_role=None)

    previous_role = existing.role
    db.delete(existing)
    kb.membership_revision = int(kb.membership_revision) + 1
    revision = int(kb.membership_revision)
    db.commit()
    logger.info(
        "kb membership removed: kb=%s user=%s previous_role=%s revision=%s",
        kb.id, target_id, previous_role, revision,
    )
    return MemberMutation(
        revision=revision, member=None, changed=True, previous_role=previous_role
    )


def audit_detail(
    kb_id: uuid.UUID, target_id: uuid.UUID, *, role: str | None, revision: int,
    previous_role: str | None = None,
) -> dict:
    """审计 detail：只放稳定 ID、角色与 revision（不写用户名/正文/路径）。"""
    detail: dict = {
        "kb_id": str(kb_id),
        "user_id": str(target_id),
        "role": role,
        "membership_revision": int(revision),
    }
    if previous_role is not None and previous_role != role:
        detail["previous_role"] = previous_role
    return detail


# 供测试/调用方显式引用维护者角色常量，避免再出现自由文本。
MAINTAINER_ROLE = KB_MEMBER_ROLE_MAINTAINER
