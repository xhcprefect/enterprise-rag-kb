"""关键词候选和相邻结构片段；沿用当前知识库与就绪文档边界。"""
import uuid

from langchain_core.documents import Document as LCDocument
from sqlalchemy import and_, func, literal_column, or_, select

from app.db.session import SessionLocal
from app.models import Document, DocumentChunk, KnowledgeBase
from app.services.document_lifecycle import retrievable_conditions
from app.services.source_documents import LOCATION_FIELDS, merge_locations, public_location

# 候选自带的这些键可能来自上一次合并/旧索引，不能当成本次权威位置或正文。
_UNVERIFIED_KEYS = ("chunk_start", "chunk_end", "_anchor_content", "_anchor_location")


def keyword_candidates(collection_name: str, question: str, limit: int = 60, *,
                       document_scope: list[dict] | None = None):
    from app.services.hybrid_search import keyword_terms

    terms = keyword_terms(question)
    if not terms:
        return []
    expression = " | ".join("'" + term.replace("\\", "\\\\").replace("'", "''") + "'" for term in terms)
    language = literal_column("'simple'")
    vector = func.to_tsvector(language, DocumentChunk.search_text)
    query = func.to_tsquery(language, expression)
    rank = func.ts_rank_cd(vector, query).label("keyword_rank")
    statement = (select(DocumentChunk.content, DocumentChunk.source_metadata, DocumentChunk.chunk_index,
                        Document.id, Document.filename, Document.index_version, rank)
                 .join(Document, Document.id == DocumentChunk.document_id)
                 .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                 .where(KnowledgeBase.collection_name == collection_name, *retrievable_conditions(),
                        DocumentChunk.embedded.is_(True), DocumentChunk.search_text != "", vector.op("@@")(query))
                 .order_by(rank.desc(), Document.id, DocumentChunk.chunk_index).limit(max(1, min(limit, 200))))
    if document_scope is not None:
        from app.services.document_scopes import scope_filter
        statement = statement.where(scope_filter(document_scope))
    with SessionLocal() as db:
        rows = db.execute(statement).all()
    return [(LCDocument(page_content=content, metadata={
        **(metadata or {}), "document_id": str(document_id), "filename": filename,
        "chunk_index": chunk_index, "index_version": version, "citation_score": None,
        "keyword_rank": float(score), "retrieval_method": "keyword",
    }), 0.0) for content, metadata, chunk_index, document_id, filename, version, score in rows]


def _same_structure(left: dict, right: dict) -> bool:
    for key in ("section_key", "section", "sheet", "table_index"):
        if left.get(key) != right.get(key):
            return False
    # 没有章节信息的 PDF 只补同页，不把下一页的新主题混入。
    if not (left.get("section_key") or left.get("section")) and left.get("page_start") != right.get("page_start"):
        return False
    return True


def _strict_index(value):
    """严格非负整数；bool、float、字符串和负数都是坏输入，返回 None。"""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _pair_identity(meta: dict):
    """解析候选的文档身份；缺失或类型非法一律返回 None（fail closed）。

    缺失/None 的 ``index_version`` 不补 0：那会让旧代次候选冒充当前代次。
    显式给出且不是 ``document`` 的来源类型（如 project）绝不当上传文档核对。
    """
    if "source_kind" in meta and meta["source_kind"] != "document":
        return None
    if "index_version" not in meta:
        return None
    try:
        identifier = uuid.UUID(str(meta["document_id"]))
    except (ValueError, KeyError, TypeError):
        return None
    index = _strict_index(meta.get("chunk_index"))
    version = _strict_index(meta.get("index_version"))
    if index is None or version is None:
        return None
    return str(identifier), index, version


def _authoritative_metadata(meta: dict, rows: list, anchor, version: int) -> dict:
    """用本次查到的权威 base 行重写位置/anchor；不保留候选自带的旧范围。

    只保留标量标签（filename、排名、检索方式等）。``document_id``、
    ``index_version``、``chunk_index`` 也从权威行重写，候选自带的身份不参与输出。
    """
    base = {key: value for key, value in meta.items()
            if key not in LOCATION_FIELDS and key not in _UNVERIFIED_KEYS
            and key not in ("document_id", "index_version", "chunk_index")}
    identity = {"document_id": str(anchor.document_id), "index_version": version,
                "chunk_index": anchor.chunk_index}
    indexes = [row.chunk_index for row in rows]
    return {
        **base,
        **identity,
        **merge_locations([row.source_metadata or {} for row in rows]),
        "chunk_start": min(indexes), "chunk_end": max(indexes),
        "_anchor_content": anchor.content,
        "_anchor_location": public_location(anchor.source_metadata),
    }


def expand_neighbors(collection_name: str, pairs, radius: int = 1):
    """只合并同一结构的连续片段；返回数量不会增加，最终仍由提示预算裁剪。

    radius 为 0、片段缺少结构信息或没有可解析身份时也不能直接返回候选：
    所有片段都要先回查「同知识库 + 当前代次 + embedded + 统一治理可检索」的
    base 行，查不到就丢弃，绝不把无权威原正文的候选交给模型。
    """
    if not pairs:
        return []
    radius = max(0, min(radius, 2))
    requests = []
    for doc, _ in pairs[:20]:
        identity = _pair_identity(doc.metadata or {})
        if identity is not None:
            requests.append(identity)
    if not requests:
        return []
    statement = (select(DocumentChunk, Document.index_version)
                 .join(Document, Document.id == DocumentChunk.document_id)
                 .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                 .where(KnowledgeBase.collection_name == collection_name, *retrievable_conditions(),
                        DocumentChunk.embedded.is_(True),
                        or_(*(and_(Document.id == uuid.UUID(identifier), Document.index_version == version,
                                  DocumentChunk.chunk_index.between(max(0, index - radius), index + radius))
                              for identifier, index, version in requests)))
                 .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index).limit(100))
    with SessionLocal() as db:
        rows = db.execute(statement).all()
    found = {(str(row.document_id), version, row.chunk_index): row for row, version in rows}
    result, consumed = [], set()
    for doc, distance in pairs:
        meta = doc.metadata or {}
        identity = _pair_identity(meta)
        if identity is None:
            continue
        identifier, index, version = identity
        key = (identifier, version, index)
        if key in consumed:
            continue
        base = found.get(key)
        if base is None:
            continue  # 没有权威原文：直接丢弃
        selected = {index: base}
        for direction in (-1, 1):
            for offset in range(1, radius + 1):
                other_index = index + direction * offset
                other_key = (identifier, version, other_index)
                row = found.get(other_key)
                if row is None or other_key in consumed or not _same_structure(base.source_metadata or {}, row.source_metadata or {}):
                    break
                selected[other_index] = row
        ordered = [selected[key] for key in sorted(selected)]
        consumed.update((identifier, version, row.chunk_index) for row in ordered)
        result.append((LCDocument(
            page_content="\n\n".join(row.content for row in ordered),
            metadata=_authoritative_metadata(meta, ordered, base, version),
        ), distance))
    return result
