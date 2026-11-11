"""知识库读取/维护权限的唯一判定入口（A1 公共接口冻结）。

接口冻结（A2/A3 之后按真实产物接入，不得擅自改签名）：

- ``visible_kb_predicate(user, kb_model=KnowledgeBase)``：返回 SQL 谓词。用于在
  SQL 分页/排名/``LIMIT`` **之前**过滤可见知识库；相关 ``EXISTS``，绝不先取一页
  再在 Python 里过滤。不活跃用户恒为 false。
- ``can_read_kb(db, kb, user)``：当前数据库内的读取判定；兼容旧
  admin/public/owner 语义，并接受显式成员 reader/maintainer。必须显式传 Session，
  不通过隐藏的 ``object_session`` 拿连接，也不在没有数据库时放行。
- ``can_manage_documents(db, kb, user)``：维护**上传库文档**的判定。仅
  ``kind='upload'``，且为管理员或显式 maintainer；项目库恒 false（管理员也不能
  借此获得项目源配置管理）。owner/public 只保留读取兼容，不自动获得维护权。
- ``lock_current_user(db, user_id, auth_token_version)``：在 ``no_autoflush`` 内
  取 ``User FOR UPDATE`` 并强制重读，核对 active 与认证期原始 ``token_version``
  快照（严格 int、非负、精确匹配；bool/None 拒绝）。**不重新 decode JWT**。
  失败一律 401。调用方随后按 ``User -> KB -> Member`` 顺序取资源锁并复核具体
  权利；本 helper 不对外承诺调用者是管理员。

其他约定：

- 所有判定都拒绝 ``is_active`` 为假（含被禁用的管理员）。
- 不跨请求缓存成员角色；每次判定都查询当前数据库状态。
- 显式成员只在**该知识库**上生效；不会把成员关系扩展成他人会话或全站管理权。
"""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import exists, false, or_, select, true
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.sql.elements import ColumnElement

from app.models import (
    KB_MEMBER_ROLE_MAINTAINER,
    KB_MEMBER_ROLES,
    KNOWLEDGE_BASE_KIND_UPLOAD,
    KnowledgeBase,
    KnowledgeBaseMember,
    User,
)

# 统一 401 文案：不暴露“用户不存在/已禁用/版本不符”之间的差异。
_AUTH_INVALID = "登录状态已失效，请重新登录"


def _is_active(user: User | None) -> bool:
    return user is not None and bool(getattr(user, "is_active", False))


def _is_admin(user: User | None) -> bool:
    return _is_active(user) and getattr(user, "role", None) == "admin"


def _member_exists(user_id, kb_model):
    """相关 EXISTS：当前用户在该 KB 上是否有 reader/maintainer 成员行。

    角色条件不是“额外收紧”：DB CHECK 只允许这两种角色，这里显式列出是为了让
    未来若扩展角色时，新角色不会自动获得读取权。
    """
    return exists(
        select(KnowledgeBaseMember.kb_id).where(
            KnowledgeBaseMember.kb_id == kb_model.id,
            KnowledgeBaseMember.user_id == user_id,
            KnowledgeBaseMember.role.in_(KB_MEMBER_ROLES),
        )
    )


def visible_kb_predicate(user: User, kb_model=KnowledgeBase) -> ColumnElement[bool]:
    """SQL 谓词：当前用户可见的知识库（分页/排名/LIMIT 之前使用）。

    - 不活跃（含被禁用管理员）-> ``false()``；
    - 活跃管理员 -> ``true()``；
    - 其余：public OR owner OR 相关 EXISTS 显式成员（reader/maintainer）。

    谓词只依赖 KB 行与成员行，不读取请求级缓存；调用方负责把返回表达式放进
    自己的 ``WHERE``。``kb_model`` 必须是同一映射里带 ``id/is_public/owner_id``
    列的 KnowledgeBase 模型（默认即真实模型；测试可传别名模型）。
    """
    if not _is_active(user):
        return false()
    if _is_admin(user):
        return true()
    return or_(
        kb_model.is_public.is_(True),
        kb_model.owner_id == user.id,
        _member_exists(user.id, kb_model),
    )


def can_read_kb(db: DBSession, kb: KnowledgeBase, user: User) -> bool:
    """当前数据库状态下的读取判定（无跨请求缓存）。

    旧语义（admin/public/owner）保持兼容；显式成员补充私有库读取权。
    显式传入 Session 是接口的一部分：调用方不能靠隐藏的 ``object_session``
    或“没有 DB 就放行”的兜底。

    查询包在 ``no_autoflush`` 内（与 ``lock_current_user`` 一致）：权限判定
    不应把调用方会话里未提交的脏对象/半成品写入顺带 flush 到数据库，也不能
    因为权限检查而触发意外的约束错误或提前写库。
    """
    if db is None or not _is_active(user) or kb is None:
        # 没有显式 Session 一律拒绝：包括 admin/public/owner 分支也不能绕过——
        # 契约不允许“无 DB 时放行”，也不用隐藏的 object_session 兜底。
        return False
    if getattr(user, "role", None) == "admin":
        return True
    if bool(getattr(kb, "is_public", False)) or getattr(kb, "owner_id", None) == user.id:
        return True
    with db.no_autoflush:
        member = db.scalar(
            select(KnowledgeBaseMember.user_id)
            .where(
                KnowledgeBaseMember.kb_id == kb.id,
                KnowledgeBaseMember.user_id == user.id,
                KnowledgeBaseMember.role.in_(KB_MEMBER_ROLES),
            )
            .limit(1)
        )
    return member is not None


def can_manage_documents(db: DBSession, kb: KnowledgeBase, user: User) -> bool:
    """上传库文档维护权：仅 upload 库 + 活跃管理员或显式 maintainer。

    项目库（以及任何未知 kind）恒 false；owner/public/reader 都不获得维护权。
    不把成员关系当成创建/删除知识库、成员管理或全站审计的授权。
    成员查询同样在 ``no_autoflush`` 内执行（只读判定，不借机写库）。
    """
    if db is None or not _is_active(user) or kb is None:
        # 缺失 Session（含管理员分支）明确拒绝，不做“无 DB 放行”。
        return False
    if getattr(kb, "kind", None) != KNOWLEDGE_BASE_KIND_UPLOAD:
        return False
    if getattr(user, "role", None) == "admin":
        return True
    with db.no_autoflush:
        member = db.scalar(
            select(KnowledgeBaseMember.user_id)
            .where(
                KnowledgeBaseMember.kb_id == kb.id,
                KnowledgeBaseMember.user_id == user.id,
                KnowledgeBaseMember.role == KB_MEMBER_ROLE_MAINTAINER,
            )
            .limit(1)
        )
    return member is not None


def lock_current_user(db: DBSession, user_id: uuid.UUID, auth_token_version) -> User:
    """锁当前用户并复核认证期原始版本快照；失败 401。

    - 版本快照必须是严格 int 且非负；``True``/``None``/字符串一律拒绝；
    - ``no_autoflush``：避免为取锁而把会话里未提交的脏对象先 flush 到数据库；
    - ``FOR UPDATE`` + ``populate_existing``：等锁后读到的是已提交的新行；
    - 绝不重新解码 JWT，也不读取可能被刷新过的 ORM 实例来推导版本。
    """
    if type(auth_token_version) is not int or auth_token_version < 0:
        raise HTTPException(status_code=401, detail=_AUTH_INVALID)
    with db.no_autoflush:
        user = db.scalar(
            select(User)
            .where(User.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if user is None or not user.is_active or user.token_version != auth_token_version:
        raise HTTPException(status_code=401, detail=_AUTH_INVALID)
    return user
