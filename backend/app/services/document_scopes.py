"""会话文档范围：以当前知识库的文档 ID 和索引代次限定检索。"""
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, false, or_, select

from app.models import Document, DocumentStatus

MAX_SCOPE_DOCUMENTS = 5


def scope_ids(values) -> list[uuid.UUID]:
    if not isinstance(values, (list, tuple)):
        raise HTTPException(422, "文档范围必须是文档 ID 列表")
    try:
        identifiers = list(dict.fromkeys(uuid.UUID(str(value)) for value in values))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(422, "文档范围包含不合法的文档 ID") from exc
    if len(identifiers) > MAX_SCOPE_DOCUMENTS:
        raise HTTPException(422, f"最多选择 {MAX_SCOPE_DOCUMENTS} 份文档")
    return identifiers


def resolve_scope(db, kb_id, identifiers, *, expected_versions: dict[str, int] | None = None,
                  lock: bool = False) -> list[dict]:
    requested = scope_ids(identifiers)
    if not requested:
        return []
    if kb_id is None:
        raise HTTPException(400, "普通对话不支持文档范围")
    statement = select(Document).where(Document.kb_id == kb_id, Document.id.in_(requested)).order_by(Document.id)
    if lock:
        statement = statement.with_for_update(read=True)
    rows = db.scalars(statement.execution_options(populate_existing=True)).all()
    found = {str(row.id): row for row in rows}
    if any(str(identifier) not in found for identifier in requested):
        raise HTTPException(404, "范围内文档不存在或不属于当前知识库，请重新选择或取消范围")
    result = []
    for identifier in requested:
        row = found[str(identifier)]
        if row.status != DocumentStatus.READY.value or row.cancel_requested:
            raise HTTPException(409, "范围内文档尚未就绪或已取消，请重新选择或取消范围")
        if expected_versions is not None and expected_versions.get(str(identifier)) != row.index_version:
            raise HTTPException(409, "范围内文档已重新索引，请重新选择以使用最新版本，或取消范围")
        result.append({"document_id": str(row.id), "filename": row.filename, "index_version": row.index_version})
    return result


def scope_pairs(scope: list[dict]) -> list[tuple[uuid.UUID, int]]:
    if not isinstance(scope, list) or len(scope) > MAX_SCOPE_DOCUMENTS:
        raise ValueError("Invalid document scope")
    result, seen = [], set()
    for item in scope:
        if not isinstance(item, dict):
            raise ValueError("Invalid document scope identity")
        identifier = uuid.UUID(str(item.get("document_id")))
        version = item.get("index_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError("Invalid document scope version")
        if identifier in seen:
            raise ValueError("Duplicate document scope identity")
        result.append((identifier, version))
        seen.add(identifier)
    return result


def validate_scope(db, kb_id, scope: list[dict], *, lock: bool = False) -> list[dict]:
    try:
        pairs = scope_pairs(scope)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "文档范围已失效，请重新选择或取消范围") from exc
    return resolve_scope(db, kb_id, [identifier for identifier, _ in pairs], lock=lock,
                         expected_versions={str(identifier): version for identifier, version in pairs})


def scope_filter(scope: list[dict]):
    """None 由调用者表示不限制；显式空范围在 SQL 层返回空结果。"""
    pairs = scope_pairs(scope)
    return or_(*(and_(Document.id == identifier, Document.index_version == version)
                 for identifier, version in pairs)) if pairs else false()


def from_verified_pairs(pairs) -> list[dict]:
    result, seen = [], set()
    for doc, _ in pairs:
        metadata = doc.metadata or {}
        identifier = str(uuid.UUID(str(metadata.get("document_id"))))
        if identifier in seen:
            continue
        filename = metadata.get("filename")
        if not isinstance(filename, str) or not filename:
            raise ValueError("Verified source is missing a filename")
        result.append({"document_id": identifier, "filename": filename,
                       "index_version": metadata.get("index_version", 0)})
        seen.add(identifier)
    scope_pairs(result)
    return result


def contains(scope: list[dict], metadata: dict) -> bool:
    try:
        version = metadata.get("index_version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            return False
        return (uuid.UUID(str(metadata.get("document_id"))), version) in scope_pairs(scope)
    except (ValueError, TypeError, AttributeError):
        return False
