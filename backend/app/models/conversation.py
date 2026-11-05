"""会话与消息。"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
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
    # 置顶时间；NULL 表示未置顶。列表按 pinned_at DESC NULLS LAST, updated_at DESC, id DESC 稳定排序。
    pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 空列表表示全知识库；非空范围绑定文档身份和索引代次，不能凭文件名复活。
    document_scope: Mapped[list[dict]] = mapped_column(JSONB, default=list, server_default="[]", nullable=False)
    scope_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 数据库租约令牌（不加循环外键）；所有生成写回必须再次匹配令牌。
    active_turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # 会话内单调轮次计数：本列恒等于“该会话已分配的最大 turn_seq”（last-used）。
    # 新请求在现有会话行锁下取 next_turn_seq + 1 并回写，request_id 重放不能重复分配；
    # 只递增、不重排已分配序号；失败/取消的终态轮可留空号并记录跳过范围。
    # legacy 补序（app.db.migrations.backfill_turn_seq）把它提升到已分配最大 seq，
    # 空会话保持 0，保证下一新请求不与旧 Message 部分唯一索引撞号。
    next_turn_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 记忆代次：摘要重置/清空时 +1，旧任务不得越过新代次复活。
    memory_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # 成功完整轮次的增量计数（有界统计，供触发判定；不做 LLM/chat 正文扫描）。
    # chars/tokens 用 BigInteger，长期会话累计不会溢出。
    memory_completed_turns: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    memory_completed_chars: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    memory_completed_tokens: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    # 旧史一次统计的显式初始化标记（替代用 memory_completed_turns==0 判断"是否已扫过"）：
    #   memory_stats_initialized 表示"旧史统计是否已尝试运行过"（幂等门槛，避免每 claim
    #     重扫）；memory_stats_complete 表示"初始化是否完整可信"。
    # 当 legacy 全量统计遇到读截断/不确定（无法安全确定全部旧完整轮）时置 complete=false，
    # 状态端把计数视为 unknown（None），绝不拿 partial 冒全。
    memory_stats_initialized: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    memory_stats_complete: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    # 当前 RAG 项目源绑定的快照 id（会话固定，结果回放不借用后来快照）。
    project_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    user: Mapped["User"] = relationship(back_populates="conversations")
    knowledge_base: Mapped["KnowledgeBase | None"] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    memory: Mapped["ConversationMemory | None"] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        # conversation_id NOT NULL FK ON DELETE CASCADE：让 DB 级联，避免 ORM 先置 NULL。
        passive_deletes=True,
        uselist=False,
    )


class ChatTurn(Base, TimestampMixin):
    """一次持久化问答请求；同一用户的 request_id 永远只认领一次。"""
    __tablename__ = "chat_turns"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_chat_turns_user_request"),
        # 会话内稳定轮次序号：已分配非空 turn_seq 的 ChatTurn 不重复（部分唯一索引）。
        # 旧 chat 完整轮按 (created_at,id) 幂等补序；不重排已分配序号。
        Index(
            "uq_chat_turns_conversation_seq",
            "conversation_id",
            "turn_seq",
            unique=True,
            postgresql_where=text("turn_seq IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True)
    # 会话内稳定轮次序号：新请求分配，旧数据/重放前为 NULL；不能对 user/assistant 直接 row_number 当水位。
    turn_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 当次请求绑定并回放用的项目快照 id（不可借用会话后来的快照）。
    project_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="processing", nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trace: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class Message(Base, TimestampMixin):
    __tablename__ = "messages"
    __table_args__ = (
        Index("uq_messages_turn_role", "turn_id", "role", unique=True),
        # 会话内 (conversation, turn_seq, role) 非空唯一：同一轮同一角色的消息不重复。
        # 旧数据 turn_seq 为 NULL 不受约束；补序时按 user created_at/id 幂等，reply_to 优先。
        Index(
            "uq_messages_conversation_seq_role",
            "conversation_id",
            "turn_seq",
            "role",
            unique=True,
            postgresql_where=text("turn_seq IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user / assistant
    # 会话内稳定轮次序号；旧数据/未补序为 NULL。
    turn_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("chat_turns.id", ondelete="CASCADE"), nullable=True)
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="completed", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 引用的检索片段: [{document_id, filename, chunk_index, score, snippet}]
    citations: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    conversation: Mapped["Conversation"] = relationship(back_populates="messages")
