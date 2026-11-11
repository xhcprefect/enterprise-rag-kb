"""会话文档范围：以当前知识库的文档 ID 和索引代次限定检索。"""
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, false, or_, select

from app.models import Document, DocumentStatus
from app.services.document_lifecycle import is_retrievable, retrievable_conditions, utc_now

# 用户会话显式选择的文档上限；scope_ids/resolve_scope/validate_scope 与
# scope_pairs 的默认值都保持这条严格边界。
MAX_SCOPE_DOCUMENTS = 5
# 内部检索清单（在线回归冻结的执行范围等）有界上限。只允许检索路径显式使用，
# 不能反过来放宽用户会话选择；超限明确拒绝，绝不截断。
MAX_INTERNAL_SCOPE_DOCUMENTS = 256


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
    # 统一按 UUID 排序加锁，避免并发请求以不同顺序取行锁造成死锁。
    # 治理谓词与检索侧共用同一实现：禁用/已替代/未生效/已过期文档不可能进入范围。
    statement = (select(Document)
                 .where(Document.kb_id == kb_id, Document.id.in_(requested), *retrievable_conditions())
                 .order_by(Document.id))
    if lock:
        statement = statement.with_for_update(read=True)
    rows = db.scalars(statement.execution_options(populate_existing=True)).all()
    found = {str(row.id): row for row in rows}
    if any(str(identifier) not in found for identifier in requested):
        raise HTTPException(404, "范围内文档不存在或不属于当前知识库，请重新选择或取消范围")
    # SQL 谓词里的时刻是在等行锁之前取的；锁可能一直等到 valid_until 之后。
    # 所有行锁返回后必须重新取 UTC，逐行用同一 is_retrievable 对象级复核，
    # 否则已过期文档会被陈旧时刻放行。坏类型/缺字段同样 fail closed。
    moment = utc_now()
    result = []
    for identifier in requested:
        row = found[str(identifier)]
        if row.status != DocumentStatus.READY.value or row.cancel_requested:
            raise HTTPException(409, "范围内文档尚未就绪或已取消，请重新选择或取消范围")
        if not is_retrievable(row, now=moment):
            raise HTTPException(409, "范围内文档已失效或不在生效期，请重新选择或取消范围")
        if expected_versions is not None and expected_versions.get(str(identifier)) != row.index_version:
            raise HTTPException(409, "范围内文档已重新索引，请重新选择以使用最新版本，或取消范围")
        result.append({"document_id": str(row.id), "filename": row.filename, "index_version": row.index_version})
    return result


def _scope_limit(max_documents: int) -> int:
    """上限本身也必须是严格非负整数；坏参数失败关闭，不能静默当成 0 或无限。"""
    if not isinstance(max_documents, int) or isinstance(max_documents, bool) or max_documents < 0:
        raise ValueError("Invalid document scope limit")
    return max_documents


def scope_pairs(scope, *, max_documents: int = MAX_SCOPE_DOCUMENTS) -> list[tuple[uuid.UUID, int]]:
    """规范文档身份。

    默认仍是用户会话上限 5；只有检索路径可显式传入
    ``max_documents=MAX_INTERNAL_SCOPE_DOCUMENTS`` 接纳内部冻结清单。
    超过所给上限一律拒绝，绝不截断。内部清单可以由执行上下文以 tuple 提供，
    用户请求仍以 HTTP 列表校验（``scope_ids``）为唯一入口。
    """
    limit = _scope_limit(max_documents)
    if not isinstance(scope, (list, tuple)) or len(scope) > limit:
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
        pairs = scope_pairs(scope, max_documents=MAX_SCOPE_DOCUMENTS)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(409, "文档范围已失效，请重新选择或取消范围") from exc
    return resolve_scope(db, kb_id, [identifier for identifier, _ in pairs], lock=lock,
                         expected_versions={str(identifier): version for identifier, version in pairs})


def scope_filter(scope, *, max_documents: int = MAX_INTERNAL_SCOPE_DOCUMENTS):
    """检索 SQL 的文档范围谓词；None 仍由调用者表示不限制。

    此函数只服务于检索 SQL，默认接纳有界内部清单 256；显式空范围在 SQL 层返回
    空结果。用户会话选择仍必须先经 ``scope_ids`` / ``validate_scope`` 的 5 份限制。
    """
    pairs = scope_pairs(scope, max_documents=max_documents)
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


def contains(scope, metadata: dict, *, max_documents: int = MAX_INTERNAL_SCOPE_DOCUMENTS) -> bool:
    """候选是否属于范围；检索身份判定默认接纳有界内部清单 256。

    ``contains`` 只做成员判定，不扩大选择入口：用户会话范围仍必须先经
    ``scope_ids`` / ``validate_scope`` 的 5 份限制，普通 chat 传入的已是合法范围。
    需要按更小边界复核时可显式传 ``max_documents=MAX_SCOPE_DOCUMENTS``。
    """
    try:
        version = metadata.get("index_version", 0)
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            return False
        return (uuid.UUID(str(metadata.get("document_id"))), version) in scope_pairs(
            scope, max_documents=max_documents)
    except (ValueError, TypeError, AttributeError):
        return False
