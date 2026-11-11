"""知识库成员的请求/响应 schema（A1 冻结 HTTP 契约）。

- 列表：``{revision, total, items:[{user_id, username, display_name, is_active, role}]}``
- 变更：``{revision, member}``；DELETE 的 ``member`` 恒为 null。
- 请求体拒绝额外字段；``role`` 只接受 reader/maintainer；
  ``expected_revision`` 是严格非负整数（拒绝 bool/字符串/小数/负数）。
"""
from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

KBMemberRole = Literal["reader", "maintainer"]

# strict=True 拒绝 bool（bool 是 int 子类但语义上不是修订号）、字符串与浮点。
ExpectedRevision = Annotated[int, Field(strict=True, ge=0)]


class KnowledgeBaseMemberRead(BaseModel):
    """列表元素 / PUT 回执共用的成员视图；只含稳定 ID 与展示字段。"""

    user_id: uuid.UUID
    username: str
    display_name: str
    is_active: bool
    role: KBMemberRole


class KnowledgeBaseMemberList(BaseModel):
    revision: int = Field(ge=0)
    total: int = Field(ge=0)
    items: list[KnowledgeBaseMemberRead]


class KnowledgeBaseMemberUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: KBMemberRole
    expected_revision: ExpectedRevision


class KnowledgeBaseMemberMutation(BaseModel):
    revision: int = Field(ge=0)
    # PUT 返回与列表元素同形的 member；DELETE 返回 null。
    member: KnowledgeBaseMemberRead | None = None
