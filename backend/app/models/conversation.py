"""会话与消息。"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

# 会话模式：查知识库 / 普通对话。
# 放在模型层而不是 schemas 里，是为了让迁移脚本与接口共用同一份取值。
CONVERSATION_MODE_RAG = "rag"
CONVERSATION_MODE_CHAT = "chat"
CONVERSATION_MODES = (CONVERSATION_MODE_RAG, CONVERSATION_MODE_CHAT)


class Conversation(Base, TimestampMixin):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    # 普通对话（mode=chat）不挂知识库，所以可空；知识库被删时 RAG 会话跟着级联删除
    kb_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    # 按会话固定：rag（检索知识库） / chat（普通对话）。
    # 老会话迁移后一律为 rag，行为与升级前完全一致。
    mode: Mapped[str] = mapped_column(
        String(16), default=CONVERSATION_MODE_RAG, nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), default="新会话", nullable=False)
    # 数据库租约令牌（不加循环外键）；所有生成写回必须再次匹配令牌。
    active_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="conversations")
    knowledge_base: Mapped["KnowledgeBase | None"] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )


class ChatTurn(Base, TimestampMixin):
    """一次持久化问答请求；同一用户的 request_id 永远只认领一次。"""
    __tablename__ = "chat_turns"
    __table_args__ = (UniqueConstraint("user_id", "request_id", name="uq_chat_turns_user_request"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    user_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="processing", nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (Index("uq_messages_turn_role", "turn_id", "role", unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_turns.id", ondelete="CASCADE"), nullable=True)
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="completed", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 引用的检索片段: [{document_id, filename, chunk_index, score, snippet}]
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
