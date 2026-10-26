"""关键词候选和相邻结构片段；沿用当前知识库与就绪文档边界。"""
import uuid

from langchain_core.documents import Document as LCDocument
from sqlalchemy import and_, func, literal_column, or_, select

from app.db.session import SessionLocal
from app.models import Document, DocumentChunk, DocumentStatus, KnowledgeBase
from app.services.source_documents import merge_locations, public_location


def keyword_candidates(collection_name: str, question: str, limit: int = 60):
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
                 .where(KnowledgeBase.collection_name == collection_name,
                        Document.status == DocumentStatus.READY.value, Document.cancel_requested.is_(False),
                        DocumentChunk.embedded.is_(True), DocumentChunk.search_text != "", vector.op("@@")(query))
                 .order_by(rank.desc(), Document.id, DocumentChunk.chunk_index).limit(max(1, min(limit, 200))))
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


def expand_neighbors(collection_name: str, pairs, radius: int = 1):
    """只合并同一结构的连续片段；返回数量不会增加，最终仍由提示预算裁剪。"""
    radius = max(0, min(radius, 2))
    if not radius or not pairs:
        return pairs
    requests = []
    for doc, _ in pairs[:20]:
        meta = doc.metadata
        if not any(meta.get(key) is not None for key in ("section_key", "section", "sheet", "page_start", "line_start", "row_start")):
            continue  # 旧索引仍按原片段工作，结构信息由重新索引补齐。
        try:
            identifier = uuid.UUID(str(meta["document_id"]))
            index, version = int(meta["chunk_index"]), int(meta.get("index_version") or 0)
        except (ValueError, KeyError, TypeError):
            continue
        requests.append((identifier, index, version))
    if not requests:
        return pairs
    statement = (select(DocumentChunk, Document.index_version)
                 .join(Document, Document.id == DocumentChunk.document_id)
                 .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
                 .where(KnowledgeBase.collection_name == collection_name,
                        Document.status == DocumentStatus.READY.value, Document.cancel_requested.is_(False),
                        DocumentChunk.embedded.is_(True),
                        or_(*(and_(Document.id == identifier, Document.index_version == version,
                                  DocumentChunk.chunk_index.between(max(0, index - radius), index + radius))
                              for identifier, index, version in requests)))
                 .order_by(DocumentChunk.document_id, DocumentChunk.chunk_index).limit(100))
    with SessionLocal() as db:
        rows = db.execute(statement).all()
    found = {(str(row.document_id), version, row.chunk_index): row for row, version in rows}
    result, consumed = [], set()
    for doc, distance in pairs:
        meta = doc.metadata
        identifier, version, index = str(meta.get("document_id")), meta.get("index_version") or 0, meta.get("chunk_index")
        key = (identifier, version, index)
        if key in consumed:
            continue
        base = found.get(key)
        if base is None:
            result.append((doc, distance))
            continue
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
        location = merge_locations([row.source_metadata or {} for row in ordered])
        combined = LCDocument(page_content="\n\n".join(row.content for row in ordered), metadata={
            **meta, **location, "chunk_start": min(selected), "chunk_end": max(selected),
            "_anchor_content": base.content, "_anchor_location": public_location(base.source_metadata),
        })
        result.append((combined, distance))
    return result
