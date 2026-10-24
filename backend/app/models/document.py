"""知识库与文档。"""
import enum
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
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.models.user import User


class DocumentStatus(str, enum.Enum):
    PENDING = "pending"        # 已上传，等待解析
    PROCESSING = "processing"  # 解析/向量化中
    READY = "ready"            # 可检索
    FAILED = "failed"          # 处理失败
    CANCELLED = "cancelled"    # 用户主动取消入库


class KnowledgeBase(Base, TimestampMixin):
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # pgvector 集合名（langchain_pg_collection.name），一个知识库一个集合
    collection_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_public: Mapped[bool] = mapped_column(default=True, nullable=False)

    owner: Mapped["User | None"] = relationship()
    documents: Mapped[list["Document"]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )


class Document(Base, TimestampMixin):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(16), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=DocumentStatus.PENDING.value, index=True, nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 入库进度（0-100）与取消标志：worker 按批次更新，前端轮询读取 / 写入取消
    progress: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(default=False, nullable=False)
    # 入库心跳：worker 每批刷新，用于识别"进程已死但状态还是 processing"的文档
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 切分是否已完整落库：为 False 时，残留的 chunk 行不完整，续传必须重新切分
    parse_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="documents")
    uploader: Mapped["User | None"] = relationship()
    chunks: Mapped[list["DocumentChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class DocumentChunk(Base, TimestampMixin):
    """分块落库表：入库断点续传的唯一真相。

    先切分落库，再逐批向量化并置 ``embedded=True``。进程中断后只重跑未完成的分块，
    不必重新解析 GB 级源文件；向量写入使用确定性 ID ``"{document_id}:{chunk_index}"``，
    重复写入是 upsert，不会产生重复行。
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("uq_document_chunks_doc_index", "document_id", "chunk_index", unique=True),
        Index("ix_document_chunks_pending", "document_id", "embedded"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    document: Mapped["Document"] = relationship(back_populates="chunks")
