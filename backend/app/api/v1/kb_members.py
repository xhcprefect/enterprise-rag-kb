"""知识库成员管理 API（仅管理员，统一 ApiResponse envelope）。

独立 router（``prefix=/knowledge-bases``），与知识库主 router 路径不重叠：
读写均以 ``/{kb_id}/members`` 为根。成员关系只授予该库读取权与（maintainer）
上传库文档维护权；这里不暴露全站管理、他人会话或项目源配置操作。

HTTP 冻结契约：

- ``GET    /{kb_id}/members?page=&page_size=`` -> ``{revision,total,items}``
- ``PUT    /{kb_id}/members/{user_id}`` body ``{role,expected_revision}``
- ``DELETE /{kb_id}/members/{user_id}?expected_revision=`` -> ``{revision,member:null}``

所有写入先认证（依赖层）再按 ``User(admin+target，UUID 升序) -> KB -> Member``
取锁；CAS 冲突 409，无变化幂等且不递增 revision。审计只写稳定 ID/角色/revision。
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DBSession

from app.api.deps import require_admin, write_audit
from app.db.session import get_db
from app.models import User
from app.schemas.common import ApiResponse
from app.schemas.kb_members import (
    KnowledgeBaseMemberList,
    KnowledgeBaseMemberMutation,
    KnowledgeBaseMemberUpsert,
)
from app.services import kb_members

router = APIRouter(prefix="/knowledge-bases", tags=["知识库成员"])


@router.get(
    "/{kb_id}/members",
    response_model=ApiResponse[KnowledgeBaseMemberList],
    summary="成员列表（管理员，分页）",
)
def list_kb_members(
    kb_id: uuid.UUID,
    response: Response,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=kb_members.MAX_PAGE_SIZE)] = kb_members.DEFAULT_PAGE_SIZE,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """分页读取成员；revision/total/items 来自同一 KB 短快照（不搭配旧版本+新列表）。

    纯读路径不取 User 写锁：认证依赖已按数据库当前角色与令牌版本拒绝禁用/降权/
    旧令牌。快照一致性由 ``list_members`` 内的 KB ``FOR SHARE`` 保证。
    """
    data = kb_members.list_members(db, kb_id, page=page, page_size=page_size)
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=data)


@router.put(
    "/{kb_id}/members/{user_id}",
    response_model=ApiResponse[KnowledgeBaseMemberMutation],
    summary="授予或修改成员角色（管理员，CAS）",
)
def upsert_kb_member(
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: KnowledgeBaseMemberUpsert,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = kb_members.upsert_member(
        db, request, admin, kb_id, user_id, payload.role, payload.expected_revision
    )
    if result.changed:
        write_audit(
            db, request, admin, "kb_member_update", str(kb_id),
            kb_members.audit_detail(
                kb_id, user_id, role=payload.role, revision=result.revision,
                previous_role=result.previous_role,
            ),
        )
    return ApiResponse(data=KnowledgeBaseMemberMutation(
        revision=result.revision, member=result.member))


@router.delete(
    "/{kb_id}/members/{user_id}",
    response_model=ApiResponse[KnowledgeBaseMemberMutation],
    summary="移除成员（管理员，CAS/幂等）",
)
def remove_kb_member(
    kb_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    # Query 参数天然是字符串，不能使用 Pydantic strict=True（会把 "0" 也拒绝）；
    # HTTP 层用 int + ge=0 解析，服务层再以 ``type(x) is int`` 拒绝 bool 等旁路。
    expected_revision: Annotated[int, Query(ge=0)],
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    result = kb_members.remove_member(
        db, request, admin, kb_id, user_id, expected_revision
    )
    if result.changed:
        write_audit(
            db, request, admin, "kb_member_remove", str(kb_id),
            kb_members.audit_detail(
                kb_id, user_id, role=None, revision=result.revision,
                previous_role=result.previous_role,
            ),
        )
    return ApiResponse(data=KnowledgeBaseMemberMutation(
        revision=result.revision, member=None))
