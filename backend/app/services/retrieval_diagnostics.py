"""管理员检索诊断：复用真实检索流程，不生成答案、不写会话/轮次/审计。

只把 ``rag.retrieve`` 已采集的阶段信息整理成冻结响应；异常只回传异常类名，
绝不回传异常消息、堆栈、原始 metadata 或路径。模型用量由 ``usage_scope``
按管理员 + 知识库记录（embedding/rerank），不创建会话与轮次。
"""
import logging
import math
import uuid

from sqlalchemy import func, select

from app.core.config import settings
from app.models import Document
from app.services import document_lifecycle, rag
from app.services.document_scopes import resolve_scope
from app.services.source_documents import public_location
from app.services.usage_tracking import UsageContext, usage_scope

logger = logging.getLogger(__name__)

STAGE_NAMES = ("vector", "keyword", "fusion", "rerank", "select", "neighbors")
STAGE_STATUSES = {"ok", "error", "skipped", "fallback"}
STAGE_REASONS = {"disabled", "not_configured"}
SCORE_KEYS = ("fusion_score", "rerank_score", "citation_score")
RETRIEVAL_METHODS = {"vector", "keyword", "hybrid"}
EXCERPT_CHARS = 600


def _non_negative_int(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def _strict_int(value):
    """只接受非 bool 的整数；bool 是 int 子类，必须显式排除。"""
    return value if type(value) is int else None


def _index_version(value):
    return value if type(value) is int and value >= 0 else None


def _finite_number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _document_id(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _error_type(value):
    """只保留合法的 Python 标识符（异常类名形态），拒绝任何消息文本。"""
    return value if isinstance(value, str) and value.isidentifier() else None


def _candidate(rank: int, doc, distance) -> tuple[dict, uuid.UUID | None]:
    metadata = getattr(doc, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    content = getattr(doc, "page_content", "")
    content = content if isinstance(content, str) else ""
    scores: dict[str, float] = {}
    distance_value = _finite_number(distance)
    if distance_value is not None:
        scores["distance"] = distance_value
    for key in SCORE_KEYS:
        value = _finite_number(metadata.get(key))
        if value is not None:
            scores[key] = value
    filename = metadata.get("filename")
    method = metadata.get("retrieval_method")
    identifier = _document_id(metadata.get("document_id"))
    item = {
        "rank": rank,
        "document_id": identifier,
        "filename": filename if isinstance(filename, str) and filename else None,
        "chunk_index": _strict_int(metadata.get("chunk_index")),
        "chunk_start": _strict_int(metadata.get("chunk_start")),
        "chunk_end": _strict_int(metadata.get("chunk_end")),
        "index_version": _index_version(metadata.get("index_version")),
        "location": public_location(metadata),
        "excerpt": content[:EXCERPT_CHARS],
        "excerpt_chars": len(content),
        "excerpt_truncated": len(content) > EXCERPT_CHARS,
        "scores": scores,
        "retrieval_method": method if method in RETRIEVAL_METHODS else None,
        "lifecycle_state": None,
        "retrievable": None,
    }
    return item, identifier


def _attach_document_health(db, candidates: list[tuple[dict, uuid.UUID | None]]) -> None:
    identifiers = {identifier for _, identifier in candidates if identifier is not None}
    rows = {}
    if identifiers:
        statement = select(Document).where(Document.id.in_(sorted(identifiers)))
        for row in db.scalars(statement).all():
            rows[row.id] = row
    for item, identifier in candidates:
        row = rows.get(identifier) if identifier is not None else None
        if row is None:
            continue
        try:
            item["lifecycle_state"] = document_lifecycle.lifecycle_state(row)
        except (ValueError, TypeError, AttributeError):
            item["lifecycle_state"] = None
        try:
            item["retrievable"] = document_lifecycle.is_retrievable(row)
        except (ValueError, TypeError, AttributeError):
            item["retrievable"] = None


def _kb_counts(db, kb_id) -> dict[str, int]:
    base = select(func.count()).select_from(Document).where(Document.kb_id == kb_id)
    total = db.scalar(base) or 0
    ready = db.scalar(select(func.count()).select_from(Document).where(
        Document.kb_id == kb_id, Document.status == "ready")) or 0
    retrievable = db.scalar(select(func.count()).select_from(Document).where(
        Document.kb_id == kb_id, *document_lifecycle.retrievable_conditions())) or 0
    return {
        "total_documents": _non_negative_int(total),
        "ready_documents": _non_negative_int(ready),
        "retrievable_documents": _non_negative_int(retrievable),
    }


def _config_snapshot() -> dict:
    return {
        "top_k": settings.RETRIEVAL_TOP_K,
        "min_similarity": settings.RETRIEVAL_MIN_SIMILARITY,
        "max_per_doc": settings.RETRIEVAL_MAX_PER_DOC,
        "hybrid_retrieval_enabled": settings.HYBRID_RETRIEVAL_ENABLED,
        "keyword_candidates": settings.KEYWORD_CANDIDATES,
        "rerank_enabled": settings.RERANK_ENABLED,
        "neighbor_chunks": settings.RETRIEVAL_NEIGHBOR_CHUNKS,
    }


def _stage(name: str, trace: dict) -> dict:
    raw = trace.get(name)
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get("status")
    if status not in STAGE_STATUSES:
        status = "skipped"
    reason = raw.get("reason")
    applied = raw.get("applied")
    return {
        "name": name,
        "status": status,
        "ms": _non_negative_int(raw.get("ms")),
        "candidates": _non_negative_int(raw.get("candidates")),
        "error_type": _error_type(raw.get("error_type")),
        "applied": applied if type(applied) is bool else None,
        "reason": reason if reason in STAGE_REASONS else None,
    }


def run(db, kb, question: str, document_ids, *, user) -> dict:
    """执行一次与真实问答相同的检索；不生成答案、不写会话/轮次。"""
    scope = resolve_scope(db, kb.id, document_ids) if document_ids else []
    counts = _kb_counts(db, kb.id)

    trace: dict = {}
    status, error_type = "ok", None
    pairs = []
    try:
        with usage_scope(UsageContext(user_id=getattr(user, "id", None), kb_id=kb.id,
                                      operation="retrieval_diagnostics")):
            pairs = rag.retrieve(
                kb.collection_name, question, settings.RETRIEVAL_TOP_K,
                document_scope=scope or None, trace=trace,
            )
    except Exception as exc:  # noqa: BLE001 - 诊断接口必须 200 返回错误类型
        status, error_type = "error", type(exc).__name__
        logger.warning("检索诊断失败: %s", error_type)
        pairs = []

    raw_candidates = [_candidate(rank, doc, distance)
                      for rank, (doc, distance) in enumerate(pairs, start=1)]
    _attach_document_health(db, raw_candidates)

    return {
        "status": status,
        "error_type": error_type,
        "question": question,
        "kb_id": kb.id,
        "collection_name": kb.collection_name,
        "scope": scope,
        "kb": counts,
        "config": _config_snapshot(),
        "stages": [_stage(name, trace) for name in STAGE_NAMES],
        "candidates": [item for item, _ in raw_candidates],
        "total_ms": _non_negative_int(trace.get("total_ms")),
        "answer_generated": False,
    }
