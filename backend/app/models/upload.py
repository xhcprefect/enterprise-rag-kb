"""分片上传会话：用于大文件断点续传。"""
import enum
import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class UploadStatus(str, enum.Enum):
    UPLOADING = "uploading"   # 传输中（可续传）
    COMPLETED = "completed"   # 已合并为正式文件
    ABORTED = "aborted"       # 用户放弃 / 过期清理


class UploadSession(Base, TimestampMixin):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        # 同一知识库下同名同大小的未完成会话只能有一个：并发建会话不会互相覆盖
        Index(
            "uq_upload_sessions_active_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status = 'uploading'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kb_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True
    )
    # sha256(kb_id:filename:file_size)，只用于匹配"同一次上传"，不做安全校验
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_ext: Mapped[str] = mapped_column(String(16), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    total_chunks: Mapped[int] = mapped_column(Integer, nullable=False)
    # 已收分片下标的缓存；真实来源是磁盘上的 .part 文件
    received: Mapped[list[int]] = mapped_column(JSONB, default=list, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=UploadStatus.UPLOADING.value, index=True, nullable=False
    )
    # complete 时先写入本字段作为"认领标记"（正式 Document 行晚于它存在，
    # 所以不建外键，仅用于重试时定位同一次合并）
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
