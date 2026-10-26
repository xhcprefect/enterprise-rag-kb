"""引用原文与位置的受限读取；所有读取都以当前就绪索引代次为边界。"""
import logging
import re
import unicodedata
import uuid
from bisect import bisect_right
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, DocumentChunk, DocumentStatus

LOCATION_FIELDS = ("page_start", "page_end", "section", "sheet", "row_start", "row_end", "line_start", "line_end")
MAX_PREVIEW_CHARS = 20000
MAX_PAGE_LOOKUP_PAGES = 2000
MAX_PAGE_LOOKUP_CHARS = 4_000_000
_LEGACY_PAGE = re.compile(r"<!--[ \t]*page[ \t]+([1-9][0-9]{0,5})[ \t]*-->", re.IGNORECASE)
logger = logging.getLogger(__name__)


def pdf_file_path(doc: Document) -> Path:
    """原文件与旧页码恢复共享路径边界，不能读取上传根目录外的文件。"""
    from app.services.upload import upload_root

    if doc.file_ext.lower() != ".pdf":
        raise HTTPException(400, "该文档不是 PDF，请使用文本原文预览")
    path = Path(doc.stored_path).resolve()
    if not path.is_relative_to(upload_root().resolve()) or not path.is_file():
        raise HTTPException(404, "原文件不存在")
    return path


def _normalized_pdf_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\xad", "").replace("\u200b", "")
    return "".join(text.split())


def _legacy_content(text: str) -> tuple[str, list[tuple[int, int]]]:
    """剔除旧解析器添加的页标记，同时保留标记在去空白正文中的偏移。"""
    parts, hints, cursor, offset = [], [], 0, 0
    for marker in _LEGACY_PAGE.finditer(text):
        part = _normalized_pdf_text(text[cursor:marker.start()])
        parts.append(part)
        offset += len(part)
        hints.append((int(marker.group(1)), offset))
        cursor = marker.end()
    parts.append(_normalized_pdf_text(text[cursor:]))
    return "".join(parts), hints


def _recover_pdf_locations(doc: Document, contents: list[str]) -> list[dict]:
    """旧分块只在正文与当前 PDF 对得上时恢复物理页码，不用分块序号猜测。

    旧 <!-- page N --> 必须与实际页面起点一致；没有标记时只接受全文唯一匹配。
    读取有上限；无法核验、原文件缺失或格式损坏均保留文本预览，不伪造页码。
    """
    empty = [{} for _ in contents]
    if sum(map(len, contents)) > MAX_PAGE_LOOKUP_CHARS:
        return empty
    try:
        import pymupdf

        path = pdf_file_path(doc)
        parts, bounds, ends, pages, size = [], {}, [], [], 0
        with pymupdf.open(path) as pdf:
            if pdf.page_count > MAX_PAGE_LOOKUP_PAGES:
                return empty
            for number, page in enumerate(pdf, 1):
                text = _normalized_pdf_text(page.get_text("text"))
                bounds[number] = (size, size + len(text))
                parts.append(text)
                size += len(text)
                if size > MAX_PAGE_LOOKUP_CHARS:
                    return empty
                if text:
                    ends.append(size)
                    pages.append(number)
        original = "".join(parts)
        recovered = []
        for content in contents:
            text, hints = _legacy_content(content)
            if not text:
                recovered.append({})
                continue
            if hints:
                first_page, first_offset = hints[0]
                start = bounds.get(first_page, (-1, -1))[0] - first_offset
                verified = start >= 0 and original.startswith(text, start) and all(
                    page in bounds and bounds[page][0] == start + offset for page, offset in hints)
            else:
                start = original.find(text)
                verified = start >= 0 and original.find(text, start + 1) == -1
            if not verified:
                recovered.append({})
                continue
            first = bisect_right(ends, start)
            last = bisect_right(ends, start + len(text) - 1)
            recovered.append({"page_start": pages[first], "page_end": pages[last]})
        return recovered
    except Exception as exc:
        # 失败不能把路径、原文或上游异常正文写进日志，也不阻断已有分块预览。
        logger.debug("旧 PDF 页码未能核验: %s", type(exc).__name__)
        return empty


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
    locations = [public_location(row.source_metadata) for row in rows]
    missing_pages = [index for index, location in enumerate(locations) if "page_start" not in location]
    if doc.file_ext.lower() == ".pdf" and missing_pages:
        recovered = _recover_pdf_locations(doc, [rows[index].content for index in missing_pages])
        for index, location in zip(missing_pages, recovered):
            locations[index].update(location)
    chunks = []
    remaining = MAX_PREVIEW_CHARS
    truncated = False
    for row, location in zip(rows, locations):
        content = row.content[:remaining]
        truncated = truncated or len(content) < len(row.content)
        remaining -= len(content)
        chunks.append({"chunk_index": row.chunk_index, "content": content,
                       "location": location})
    return {"document_id": str(document_id), "filename": doc.filename, "file_ext": doc.file_ext,
            "index_version": index_version, "chunk_index": chunk_index, "chunk_start": start, "chunk_end": end,
            "location": merge_locations(locations), "chunks": chunks, "truncated": truncated}
