"""引用原文与位置的受限读取；所有读取都以当前就绪索引代次为边界。"""
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentStatus

LOCATION_FIELDS = ("page_start", "page_end", "section", "sheet", "row_start", "row_end", "line_start", "line_end")
MAX_PREVIEW_CHARS = 20000


def public_location(metadata: dict | None) -> dict:
    result = {}
    for key in LOCATION_FIELDS:
        value = (metadata or {}).get(key)
        if key in {"section", "sheet"}:
            if isinstance(value, str) and value.strip():
                result[key] = value[:512]
        elif isinstance(value, int) and not isinstance(value, bool) and value >= 1:
            result[key] = value
    return result


def merge_locations(metadata: list[dict]) -> dict:
    locations = [public_location(item) for item in metadata]
    result = {}
    for prefix in ("page", "row", "line"):
        starts = [item[f"{prefix}_start"] for item in locations if f"{prefix}_start" in item]
        ends = [item[f"{prefix}_end"] for item in locations if f"{prefix}_end" in item]
        if len(starts) == len(locations) and starts:
            result[f"{prefix}_start"] = min(starts)
        if len(ends) == len(locations) and ends:
            result[f"{prefix}_end"] = max(ends)
    for key in ("section", "sheet"):
        values = {item.get(key) for item in locations}
        if len(values) == 1 and None not in values:
            result[key] = values.pop()
    return result


def ready_document(db: Session, kb_id: uuid.UUID, document_id: uuid.UUID, index_version: int) -> Document:
    doc = db.scalar(select(Document).where(Document.id == document_id, Document.kb_id == kb_id)
                    .execution_options(populate_existing=True))
    if doc is None:
        raise HTTPException(404, "文档不存在")
    if doc.status != DocumentStatus.READY.value or doc.cancel_requested or doc.index_version != index_version:
        raise HTTPException(409, "引用对应的文档已更新或不可用，请重新提问以获取当前来源")
    return doc


def read_source(db: Session, kb_id: uuid.UUID, document_id: uuid.UUID, chunk_index: int,
                index_version: int, chunk_start: int | None = None, chunk_end: int | None = None) -> dict:
    doc = ready_document(db, kb_id, document_id, index_version)
    start = chunk_index if chunk_start is None else chunk_start
    end = chunk_index if chunk_end is None else chunk_end
    if start < 0 or not start <= chunk_index <= end or end - start > 4:
        raise HTTPException(422, "原文片段范围不合法，最多读取相邻的五个片段")
    rows = db.scalars(select(DocumentChunk).join(Document, Document.id == DocumentChunk.document_id).where(
        DocumentChunk.document_id == document_id, DocumentChunk.chunk_index.between(start, end),
        DocumentChunk.embedded.is_(True), Document.kb_id == kb_id,
        Document.status == DocumentStatus.READY.value, Document.cancel_requested.is_(False),
        Document.index_version == index_version,
    ).order_by(DocumentChunk.chunk_index).limit(5)).all()
    if len(rows) != end - start + 1:
        raise HTTPException(409, "引用原文已不可用，请重新检索当前文档")
    chunks = []
    remaining = MAX_PREVIEW_CHARS
    truncated = False
    for row in rows:
        content = row.content[:remaining]
        truncated = truncated or len(content) < len(row.content)
        remaining -= len(content)
        chunks.append({"chunk_index": row.chunk_index, "content": content,
                       "location": public_location(row.source_metadata)})
    return {"document_id": str(document_id), "filename": doc.filename, "file_ext": doc.file_ext,
            "index_version": index_version, "chunk_index": chunk_index, "chunk_start": start, "chunk_end": end,
            "location": merge_locations([row.source_metadata for row in rows]), "chunks": chunks, "truncated": truncated}
