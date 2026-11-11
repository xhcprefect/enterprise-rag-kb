"""知识库显式成员（A1 基础）。

- 复合主键 ``(kb_id, user_id)``：同一用户在同一知识库至多一条显式授权；
- 只保存稳定 ID 与角色，不复制用户资料、正文或会话内容；
- ``user_id`` 单独建索引：删除用户时要按用户查"当前实际成员 KB"的有界集合；
- ``kb_id`` 是复合主键前缀，按库分页/判定直接走主键索引；
- 角色只有 reader/maintainer，并由真实数据库 CHECK 约束兜底（ORM/裸 SQL 写入
  都必须遵守，不能靠接口层自由文本）。

删除级联：

- 删除用户 / 删除知识库都由数据库 ``ON DELETE CASCADE`` 清理成员行；
- 删除用户时 API 层会在同一事务里为确有成员行的 KB 递增 ``membership_revision``，
  让成员列表的 CAS 版本不会停留在旧值（users.delete_user 负责，不在本模型里做）。
"""
import enum
import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class KnowledgeBaseMemberRole(str, enum.Enum):
    READER = "reader"          # 可读取/检索/引用该 KB（项目库含固定快照问答）
    MAINTAINER = "maintainer"  # 读取 + 仅 kind='upload' 时可维护文档


KB_MEMBER_ROLE_READER = KnowledgeBaseMemberRole.READER.value
KB_MEMBER_ROLE_MAINTAINER = KnowledgeBaseMemberRole.MAINTAINER.value
KB_MEMBER_ROLES = (KB_MEMBER_ROLE_READER, KB_MEMBER_ROLE_MAINTAINER)


class KnowledgeBaseMember(Base):
    __tablename__ = "knowledge_base_members"
    __table_args__ = (
        # 真实 DB CHECK：绕过接口的写入也不能得到第三种角色。
        CheckConstraint(
            "role IN ('reader', 'maintainer')",
            name="ck_knowledge_base_members_role",
        ),
        # 删除用户按 user_id 找成员 KB；复合主键只覆盖 kb_id 前缀，必须单独建。
        Index("ix_knowledge_base_members_user_id", "user_id"),
    )

    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)

    # 只读关联：成员行的权限归属由显式查询决定，不做跨请求缓存。
    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="members")
    user: Mapped["User"] = relationship()
