"""文档内容身份：服务端真实 SHA-256、跨入口去重与并发收敛。

客户端声明的摘要只用于续传身份与完整性校验，绝不直接落库：真正的
``Document.content_sha256`` 一律由服务端对落盘文件重新计算。唯一性由
``uq_documents_kb_content_sha256`` 部分唯一索引兜底，两个入口同时上传同一内容时
只会有一个插入成功，另一个回滚后重新查出 canonical 并复用它。
"""
import hashlib
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document

COPY_BUF = 4 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def normalize_declared_sha256(value: str | None) -> str | None:
    """客户端声明值：只接受 64 位十六进制，其他一律视为未声明。"""
    if not value:
        return None
    candidate = value.strip().lower()
    return candidate if _SHA256_PATTERN.fullmatch(candidate) else None


def file_sha256(path: Path) -> str:
    """流式计算磁盘文件的真实摘要，内存占用与文件大小无关。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(COPY_BUF):
            digest.update(block)
    return digest.hexdigest()


def canonical_document(db: Session, kb_id, content_sha256: str) -> Document | None:
    """同一知识库内按内容摘要找已有 canonical；绝不跨库复用。"""
    return db.scalar(
        select(Document).where(
            Document.kb_id == kb_id,
            Document.content_sha256 == content_sha256,
        )
    )


def is_trusted_upload_file(stored_path: str, root: Path) -> bool:
    """``stored_path`` 必须位于可信上传根内，且是普通文件（不跟随越界符号链接）。"""
    try:
        trusted_root = root.resolve()
        candidate = Path(stored_path).resolve()
        candidate.relative_to(trusted_root)
    except (OSError, ValueError):
        return False
    return candidate.is_file()
