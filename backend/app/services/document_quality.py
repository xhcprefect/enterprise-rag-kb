"""入库质量预览：分块汇总、问题清单与分页正文预览。

质量汇总只输出计数和阈值，不携带任何分块正文；分页预览的正文每块最多
``PREVIEW_CHARS`` 个字符。所有读取都是只读查询，不修改文档状态。
"""
import unicodedata
from typing import Any

from sqlalchemy import func, select

from app.core.config import settings
from app.models import DocumentChunk
from app.services import document_identity
from app.services.source_documents import public_location
from app.services.upload import upload_root

SCAN_LIMIT = 2000
PREVIEW_CHARS = 1200
SHORT_CHUNK_CHARS = 50

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

# severity 内部仍按固定 code 顺序输出，前端不需要自己再排序。
ISSUE_CODE_ORDER = (
    "file_missing",
    "document_failed",
    "document_not_ready",
    "parse_incomplete",
    "no_chunks",
    "vector_incomplete",
    "empty_chunks",
    "short_chunks",
    "duplicate_chunks",
    "replacement_chars",
    "control_chars",
    "oversized_chunks",
    "missing_page_metadata",
    "chunk_index_gaps",
    "sample_truncated",
)

_SOURCE_KEYS = {
    "page_chunks": "page_start",
    "section_chunks": "section",
    "line_chunks": "line_start",
    "sheet_chunks": "sheet",
    "table_chunks": "table_index",
}


def _value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def _content(row: Any) -> str:
    value = _value(row, "content", "")
    return value if isinstance(value, str) else ""


def _metadata(row: Any) -> dict:
    value = _value(row, "source_metadata", None)
    return value if isinstance(value, dict) else {}


def _file_ext(document: Any) -> str:
    value = _value(document, "file_ext", "") or ""
    return str(value).lstrip(".").lower()


def _int_attr(document: Any, name: str, default: int = 0) -> int:
    value = _value(document, name, default)
    return value if type(value) is int else default


def oversized_threshold() -> int:
    return max(settings.CHUNK_SIZE + settings.CHUNK_OVERLAP, settings.CHUNK_SIZE * 2)


def _normalize_duplicate_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def has_control_chars(text: str) -> bool:
    return any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in text)


def compute_stats(rows: list) -> dict[str, int]:
    """基于扫描样本计算汇总；不保留任何正文。"""
    contents = [_content(row) for row in rows]
    stripped = [_normalize_duplicate_text(text.strip()) for text in contents]
    lengths = [len(text) for text in contents]
    duplicate_groups: dict[str, int] = {}
    for text in stripped:
        duplicate_groups[text] = duplicate_groups.get(text, 0) + 1
    threshold = oversized_threshold()
    stats = {
        "empty_chunks": sum(1 for text in stripped if not text),
        "short_chunks": sum(1 for text in stripped if 0 < len(text) < SHORT_CHUNK_CHARS),
        "duplicate_chunks": sum(count - 1 for count in duplicate_groups.values()),
        "replacement_chunks": sum(1 for text in contents if "�" in text),
        "control_chunks": sum(1 for text in contents if has_control_chars(text)),
        "oversized_chunks": sum(1 for text in contents if len(text) > threshold),
        "min_chars": min(lengths) if lengths else 0,
        "max_chars": max(lengths) if lengths else 0,
        "avg_chars": round(sum(lengths) / len(lengths)) if lengths else 0,
    }
    for key, metadata_key in _SOURCE_KEYS.items():
        stats[key] = sum(1 for row in rows if metadata_key in _metadata(row))
    return stats


def chunk_index_gaps(indexes) -> int:
    """扫描样本内 ``max_index + 1 - 唯一索引数``；负数/非整数索引不参与。"""
    valid = {value for value in indexes if type(value) is int}
    if not valid:
        return 0
    return max(0, max(valid) + 1 - len(valid))


def _issue(code: str, severity: str, count: int = 1, details: dict | None = None) -> dict:
    return {"code": code, "severity": severity, "count": count, "details": details}


def collect_issues(
    document: Any,
    stats: dict[str, int],
    *,
    scanned_chunks: int,
    chunk_total: int,
    unembedded_count: int,
    file_exists: bool,
    chunk_indexes=(),
) -> list[dict]:
    status = str(_value(document, "status", "") or "")
    file_ext = _file_ext(document)
    issues: list[dict] = []

    if not file_exists:
        issues.append(_issue("file_missing", "error"))
    if status == "failed":
        issues.append(_issue("document_failed", "error"))
    if status in {"pending", "processing", "cancelled"}:
        issues.append(_issue("document_not_ready", "warning"))
    if status in {"pending", "processing"} and _value(document, "parse_done", False) is not True:
        issues.append(_issue("parse_incomplete", "warning"))
    if status == "ready" and scanned_chunks == 0:
        issues.append(_issue("no_chunks", "error"))
    if status == "ready" and unembedded_count > 0:
        issues.append(_issue("vector_incomplete", "error", unembedded_count))

    if stats["empty_chunks"] > 0:
        issues.append(_issue("empty_chunks", "warning", stats["empty_chunks"]))
    if stats["short_chunks"] > 0:
        issues.append(_issue("short_chunks", "info", stats["short_chunks"],
                             {"threshold_chars": SHORT_CHUNK_CHARS}))
    if stats["duplicate_chunks"] > 0:
        issues.append(_issue("duplicate_chunks", "warning", stats["duplicate_chunks"]))
    if stats["replacement_chunks"] > 0:
        issues.append(_issue("replacement_chars", "warning", stats["replacement_chunks"]))
    if stats["control_chunks"] > 0:
        issues.append(_issue("control_chars", "warning", stats["control_chunks"]))
    if stats["oversized_chunks"] > 0:
        issues.append(_issue("oversized_chunks", "warning", stats["oversized_chunks"],
                             {"threshold_chars": oversized_threshold()}))
    if file_ext == "pdf" and status == "ready" and scanned_chunks > 0:
        missing = scanned_chunks - stats["page_chunks"]
        if missing > 0:
            issues.append(_issue("missing_page_metadata", "warning", missing))
    gaps = chunk_index_gaps(chunk_indexes)
    if gaps > 0:
        issues.append(_issue("chunk_index_gaps", "warning", gaps))
    if chunk_total > scanned_chunks:
        issues.append(_issue("sample_truncated", "info"))

    issues.sort(key=lambda item: (SEVERITY_ORDER[item["severity"]], ISSUE_CODE_ORDER.index(item["code"])))
    return issues


def build_payload(
    document: Any,
    rows: list,
    *,
    chunk_total: int,
    embedded_count: int,
    unembedded_count: int,
    file_exists: bool,
) -> dict:
    """构造 A1 冻结响应；不包含任何分块正文。"""
    rows = list(rows)
    scanned = len(rows)
    stats = compute_stats(rows)
    issues = collect_issues(
        document, stats,
        scanned_chunks=scanned,
        chunk_total=chunk_total,
        unembedded_count=unembedded_count,
        file_exists=file_exists,
        chunk_indexes=[_value(row, "chunk_index", None) for row in rows],
    )
    return {
        "document_id": _value(document, "id", None),
        "filename": str(_value(document, "filename", "") or ""),
        "file_ext": _file_ext(document),
        "status": str(_value(document, "status", "") or ""),
        "error_message": _value(document, "error_message", None),
        "index_version": _int_attr(document, "index_version", 0),
        "parse_done": _value(document, "parse_done", False) is True,
        "cancel_requested": _value(document, "cancel_requested", False) is True,
        "chunk_count": _int_attr(document, "chunk_count", 0),
        "char_count": _int_attr(document, "char_count", 0),
        "scanned_chunks": scanned,
        "scan_limit": SCAN_LIMIT,
        "scan_truncated": max(0, chunk_total) > scanned,
        "embedded_count": max(0, embedded_count),
        "unembedded_count": max(0, unembedded_count),
        "file_exists": bool(file_exists),
        "stats": stats,
        "issues": issues,
    }


def build_quality(db, document) -> dict:
    """只读扫描：最多 2000 行质量样本；向量计数与扫描上限无关。"""
    total = db.scalar(select(func.count()).select_from(DocumentChunk)
                      .where(DocumentChunk.document_id == document.id)) or 0
    embedded = db.scalar(select(func.count()).select_from(DocumentChunk).where(
        DocumentChunk.document_id == document.id, DocumentChunk.embedded.is_(True))) or 0
    rows = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document.id)
                      .order_by(DocumentChunk.chunk_index).limit(SCAN_LIMIT)).all()
    file_exists = document_identity.is_trusted_upload_file(document.stored_path, upload_root())
    return build_payload(document, rows, chunk_total=total, embedded_count=embedded,
                         unembedded_count=max(0, total - embedded), file_exists=file_exists)


def chunk_page(db, document, offset: int, limit: int) -> dict:
    """按 chunk_index 升序分页；正文最多 ``PREVIEW_CHARS`` 字符。"""
    total = db.scalar(select(func.count()).select_from(DocumentChunk)
                      .where(DocumentChunk.document_id == document.id)) or 0
    rows = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document.id)
                      .order_by(DocumentChunk.chunk_index).offset(offset).limit(limit)).all()
    items = []
    for row in rows:
        content = _content(row)
        items.append({
            "chunk_index": row.chunk_index,
            "content": content[:PREVIEW_CHARS],
            "content_chars": len(content),
            "truncated": len(content) > PREVIEW_CHARS,
            "embedded": bool(row.embedded),
            "location": public_location(_metadata(row)),
        })
    return {"total": total, "offset": offset, "limit": limit, "items": items}
