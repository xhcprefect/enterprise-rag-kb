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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    # 资源类型：'upload'（上传文档库，默认） / 'project'（项目源码库）。
    # 项目库禁止普通上传/分片 complete/reindex/cancel 等旁路；普通 KB PATCH 改不了 kind。
    kind: Mapped[str] = mapped_column(String(16), default="upload", server_default="'upload'", nullable=False)
    # 显式成员列表的 CAS 版本：任何成员变更（含用户删除导致的 FK 级联）都 +1。
    # 老库由 migrations.ensure_kb_membership_schema 幂等补列，DEFAULT 0 回填既有行。
    membership_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    owner: Mapped["User | None"] = relationship()
    documents: Mapped[list["Document"]] = relationship(
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )
    project_source: Mapped["ProjectSource | None"] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        # ProjectSource.kb_id NOT NULL FK ON DELETE CASCADE：让 DB 级联，避免 ORM 先置 NULL。
        passive_deletes=True,
        uselist=False,
    )
    # 显式成员（reader/maintainer）；删除 KB 时由 FK CASCADE 清理，不在 ORM 里逐行删。
    members: Mapped[list["KnowledgeBaseMember"]] = relationship(
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Document(Base, TimestampMixin):
    __tablename__ = "documents"
    __table_args__ = (
        # 同一知识库内相同内容只保留一份 canonical：仅对已算出摘要的行生效，
        # 老数据（NULL）与 backfill 未覆盖的行不受约束，也不会被删除或合并。
        Index(
            "uq_documents_kb_content_sha256",
            "kb_id",
            "content_sha256",
            unique=True,
            postgresql_where=text("content_sha256 IS NOT NULL"),
        ),
    )

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
    # 服务端实际计算的完整内容摘要；上传时声明值只用于续传/完整性校验，从不直接写入。
    # 老数据与 backfill 未覆盖的行为 NULL，唯一索引是部分索引，因此可以共存。
    content_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    # 索引代次隔离重建前后的向量；lease_id 阻止超时旧任务继续写入。
    index_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # ---- 治理字段（T3 冻结契约）----
    # 负责人说明文本（最多 200 字符），仅描述用途，绝不承载任何访问控制语义。
    # 真实 server_default：绕过 ORM 的 INSERT（迁移脚本/psql/backfill）也得到确定值。
    maintainer: Mapped[str] = mapped_column(
        String(200), default="", server_default="", nullable=False
    )
    # 最近一次人工审核时间；缺失只告警，不参与可检索判定。
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 显式生效窗口 [valid_from, valid_until)：NULL 表示该侧不限。
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(
        default=True, server_default=text("true"), nullable=False
    )
    # 替代关系：superseded_at/superseded_by_id 任一存在即视为已被替代。
    # 自引用外键 ON DELETE SET NULL：删除替代文档不删除原文档，也不复活它。
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "documents.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_documents_superseded_by_id_documents",
        ),
        nullable=True,
    )
    governance_revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    superseded_by: Mapped["Document | None"] = relationship(
        foreign_keys=[superseded_by_id], remote_side=[id]
    )

    @property
    def lifecycle_state(self) -> str:
        """派生生命周期状态；与 SQL 谓词共享同一模块，保证两边一致。"""
        from app.services.document_lifecycle import lifecycle_state as derive

        return derive(self)

    @property
    def retrievable(self) -> bool:
        """当前是否可被检索：入库状态 + 治理谓词的实时派生值。"""
        from app.services.document_lifecycle import is_retrievable

        return is_retrievable(self)

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
    source_metadata: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    embedded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    document: Mapped["Document"] = relationship(back_populates="chunks")
