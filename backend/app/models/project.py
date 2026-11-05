"""本地项目文档/源码索引：源、快照（同步任务）、文件版本、快照映射、分块。

F 基础实施契约定稿：

- 一个 ``ProjectSource`` 绑定一个专用私有 ``KnowledgeBase``（kind='project'）。
  服务端只接受 frontend 的 ``root_key`` + ``relative_dir``，不接收任意绝对路径；
  ``root_fingerprint`` 为授权根目录身份指纹，配置换树必须拒绝。
- ``ProjectSnapshot`` 同时承载同步任务状态/租约/检查点；每源至多一个
  pending/processing 活动同步（部分唯一索引）。失败/取消保持上一可用版本。
- ``ProjectFileVersion`` 以 (source_id, relative_path, content_sha256, config_fingerprint)
  唯一标识不可变文件版本；blob 在受控目录，hash/解析均以 blob 字节为准。
- ``ProjectSnapshotFile`` 是 snapshot -> file_version 的映射（复合主键），
  保留旧快照用于已存在会话与引用。删除父级保留旧快照，不实现 GC。
- ``ProjectChunk`` 只绑定不可变 file_version（不写某一次 snapshot）；向量 key 含
  embedding 输入与模型/解析配置指纹，跨配置不误复用。向量沿用 LangChain
  PGVector collection/persist_vectors，不需要新 Vector 列。
"""
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
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class ProjectSnapshotStatus(str, enum.Enum):
    PENDING = "pending"        # 可被 project worker 领取
    PROCESSING = "processing"  # 已领取，持有租约/心跳
    READY = "ready"            # 校验通过，可切为 active
    FAILED = "failed"          # 失败（error_code），保持上一可用版本
    CANCELLED = "cancelled"    # 明确取消

    __str__ = enum.Enum.__str__


class ProjectFileParseStatus(str, enum.Enum):
    PENDING = "pending"    # 尚未解析
    READY = "ready"        # 解析出结构化分块
    FAILED = "failed"      # 解析失败
    FALLBACK = "fallback"  # 降级为带行号纯文本（不伪造符号）

    __str__ = enum.Enum.__str__


class ProjectSource(Base, TimestampMixin):
    """一个项目源：绑定一个专用私有知识库 + 服务端授权根目录下的一棵子树。"""

    __tablename__ = "project_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # 一一对应一个专用知识库（kind='project'）；删除 KB 时源随之级联删除。
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 服务端 PROJECT_ALLOWED_ROOTS 映射里的键；绝不直接存任意绝对路径。
    root_key: Mapped[str] = mapped_column(String(200), nullable=False)
    # 授权根目录下的相对子目录（可空 = 根目录本身）。
    relative_dir: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    # 授权根目录身份指纹（如 inode/设备/字节抽样），配置换树必须拒绝而非静默换源。
    root_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # 当前生效的快照；CAS 异常时保持上一可用版本，不让旧引用漂移到新代码。
    active_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "project_snapshots.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_project_sources_active_snapshot",
        ),
        nullable=True,
    )
    # 删除源先事务内 revoked，使后续引用读取返回 410，再标记可回收（GC 后移）。
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    knowledge_base: Mapped["KnowledgeBase"] = relationship(back_populates="project_source")
    creator: Mapped["User | None"] = relationship()
    active_snapshot: Mapped["ProjectSnapshot | None"] = relationship(
        foreign_keys=[active_snapshot_id], post_update=True
    )
    snapshots: Mapped[list["ProjectSnapshot"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        foreign_keys="ProjectSnapshot.source_id",
        order_by="ProjectSnapshot.created_at",
    )
    file_versions: Mapped[list["ProjectFileVersion"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class ProjectSnapshot(Base, TimestampMixin):
    """一次项目同步任务与产物快照：状态/租约/检查点与快照清单一起落库。"""

    __tablename__ = "project_snapshots"
    __table_args__ = (
        # 每源至多一个活动同步（pending 或 processing）。
        Index(
            "uq_project_snapshots_one_active",
            "source_id",
            unique=True,
            postgresql_where=text("status IN ('pending','processing')"),
        ),
        Index("ix_project_snapshots_claimable", "status", "available_at"),
        Index("ix_project_snapshots_source_status", "source_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_sources.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), default=ProjectSnapshotStatus.PENDING.value, index=True, nullable=False
    )
    # 本次同步基于的快照（CAS 依据）：增量只对比上一步，避免重扫全部字节。
    base_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "project_snapshots.id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_project_snapshots_base",
        ),
        nullable=True,
    )
    lease_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # manifest/config 指纹：换配置/换树必须拒绝而非静默复用。
    manifest_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 进度/计数/字节：入队、领取、批处理中刷新，前端轮询读取。
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    total_bytes: Mapped[int] = mapped_column(
        BigInteger, default=0, server_default="0", nullable=False
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source: Mapped["ProjectSource"] = relationship(back_populates="snapshots",
                                                   foreign_keys=[source_id])
    base_snapshot: Mapped["ProjectSnapshot | None"] = relationship(
        foreign_keys=[base_snapshot_id], remote_side="ProjectSnapshot.id", post_update=True
    )
    files: Mapped[list["ProjectSnapshotFile"]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan"
    )


class ProjectFileVersion(Base, TimestampMixin):
    """一个不可变文件版本；blob 在受控目录，hash/解析均以 blob 字节为准。"""

    __tablename__ = "project_file_versions"
    __table_args__ = (
        # 文件身份：项目 + 相对路径 + 内容 + 解析配置。同字节不同路径保留两个身份。
        UniqueConstraint(
            "source_id",
            "relative_path",
            "content_sha256",
            "config_fingerprint",
            name="uq_project_file_versions_identity",
        ),
        Index("ix_project_file_versions_source_path", "source_id", "relative_path"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_sources.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(String(2048), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 受控 blob 引用（系统拥有的不可变副本），不以用户传入的服务端路径打开文件。
    blob_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0", nullable=False)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 生成该版本的同步/解析配置指纹（文件身份的一部分）；配置换树必须拒绝而非静默复用。
    config_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # parser/chunker/embedding 指纹：跨配置不得误复用向量/解析结果。
    parser_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunker_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parse_done: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    parse_status: Mapped[str] = mapped_column(
        String(16), default=ProjectFileParseStatus.PENDING.value, nullable=False
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped["ProjectSource"] = relationship(back_populates="file_versions")
    chunks: Mapped[list["ProjectChunk"]] = relationship(
        back_populates="file_version", cascade="all, delete-orphan"
    )


class ProjectSnapshotFile(Base, TimestampMixin):
    """snapshot -> file_version 映射：快照通过它复用未变化文件版本并保留引用。"""

    __tablename__ = "project_snapshot_files"
    __table_args__ = (
        PrimaryKeyConstraint("snapshot_id", "relative_path", name="pk_project_snapshot_files"),
        Index("ix_project_snapshot_files_file_version", "file_version_id"),
    )

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
    )
    relative_path: Mapped[str] = mapped_column(String(2048), primary_key=True, nullable=False)
    # 不可变文件版本；被任何快照引用时不得删除（RESTRICT），保证旧引用不漂移。
    file_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_file_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )

    snapshot: Mapped["ProjectSnapshot"] = relationship(back_populates="files")
    file_version: Mapped["ProjectFileVersion"] = relationship()


class ProjectChunk(Base, TimestampMixin):
    """项目文件分块：只绑定不可变 file_version，不写某一次 snapshot。"""

    __tablename__ = "project_chunks"
    __table_args__ = (
        Index(
            "uq_project_chunks_version_index",
            "file_version_id",
            "chunk_index",
            unique=True,
        ),
        Index("ix_project_chunks_pending", "file_version_id", "embedded"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    file_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("project_file_versions.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    source_metadata: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    # 精确行号；未知保留 NULL，绝不伪造。
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    symbol_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedded: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    file_version: Mapped["ProjectFileVersion"] = relationship(back_populates="chunks")
