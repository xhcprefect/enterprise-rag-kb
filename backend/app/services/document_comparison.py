"""资料更新差异预览（只读）与 supersede 的预览指纹校验。

冻结契约要点：

- 比较对象是 ``DocumentChunk`` 中**已入库**的文本；不解析原文件、不调用模型、
  不新增表/列。预览本身不写审计、不写会话、不生成答案、不记录正文。
- 读取顺序固定：先按 UUID 升序对两份 ``Document`` 加共享行锁
  （PostgreSQL ``FOR SHARE``，``populate_existing``），全部锁等待结束后才取
  UTC；随后按 ``chunk_index`` 读取两侧分块。绝不在无锁时先读文档再读分块。
- 任何不可比较情形都返回 ``comparable=false``、``reason``、空 ``rows``、
  ``summary=null``、``fingerprint=null``，不返回部分 diff。
- 拆分：每个 chunk 的 ``content`` 使用 ``str.splitlines()``（保留内部空行，
  统一 CRLF/CR），每行一个比较单元；不跨 chunk 合并、不按前后缀启发式去重、
  不删除重复行。
- 对齐：``difflib.SequenceMatcher(None, left_texts, right_texts, autojunk=False)``。
- 指纹：``ingested-chunks-v1``；对规范 JSON 做 SHA-256，只覆盖稳定身份、
  治理字段与按 ``chunk_index`` 排序的分块内容摘要；不包含正文/路径/用户输入。
  它只用于“预览之后资料是否变化”的陈旧检测，**不是**授权凭证。
"""
from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.models import Document, DocumentChunk, DocumentStatus
from app.services import document_lifecycle
from app.services.source_documents import public_location

COMPARISON_VERSION = 1
COMPARISON_BASIS = "ingested_chunks"
FINGERPRINT_ALGORITHM = "ingested-chunks-v1"

MAX_CHUNKS_PER_DOCUMENT = 1000
MAX_CHARS_PER_DOCUMENT = 200_000
MAX_UNITS_PER_DOCUMENT = 2000

REASON_OK = "ok"
REASON_NOT_READY = "not_ready"
REASON_NOT_ELIGIBLE = "not_eligible"
REASON_INCOMPLETE = "incomplete"
REASON_EMPTY_DOCUMENT = "empty_document"
REASON_TOO_MANY_CHUNKS = "too_many_chunks"
REASON_TOO_MANY_CHARS = "too_many_chars"
REASON_TOO_MANY_UNITS = "too_many_units"

STALE_FINGERPRINT_DETAIL = "资料版本已变化，请重新预览差异后再确认"

_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
_KIND_UNCHANGED = "unchanged"
_KIND_ADDED = "added"
_KIND_REMOVED = "removed"
_KIND_MODIFIED = "modified"


# ---------- 通用取值（同时支持 ORM 行与测试替身） ----------

def _value(target: Any, name: str, default: Any = None) -> Any:
    if isinstance(target, dict):
        return target.get(name, default)
    return getattr(target, name, default)


def _content(chunk: Any) -> str:
    value = _value(chunk, "content", "")
    return value if isinstance(value, str) else ""


def _metadata(chunk: Any) -> dict:
    value = _value(chunk, "source_metadata", None)
    return value if isinstance(value, dict) else {}


# ---------- 资格判定（纯函数） ----------

def readiness_reason(document: Any) -> str | None:
    """入库就绪：``status='ready'``、未取消、``index_version`` 非负整数。"""
    if _value(document, "status") != DocumentStatus.READY.value:
        return REASON_NOT_READY
    if _value(document, "cancel_requested") is not False:
        return REASON_NOT_READY
    index_version = _value(document, "index_version")
    if isinstance(index_version, bool) or not isinstance(index_version, int):
        return REASON_NOT_READY
    if index_version < 0:
        return REASON_NOT_READY
    return None


def lifecycle_state_or_none(document: Any, *, now: datetime) -> str | None:
    """派生生命周期；非法治理字段返回 ``None``（fail closed，不伪造 active）。"""
    try:
        return document_lifecycle.lifecycle_state(document, now=now)
    except (AttributeError, TypeError, ValueError):
        return None


def precheck_reason(original: Any, replacement: Any, *, now: datetime) -> str | None:
    """两边入库就绪 + 替代目标当前 active。

    原文档可以是 superseded（管理员只读比较历史版本），因此不检查其生命周期。
    """
    if readiness_reason(original) is not None or readiness_reason(replacement) is not None:
        return REASON_NOT_READY
    if lifecycle_state_or_none(replacement, now=now) != document_lifecycle.LIFECYCLE_ACTIVE:
        return REASON_NOT_ELIGIBLE
    return None


def chunk_layout_reason(
    document: Any,
    chunk_indexes: Sequence[int],
    embedded_flags: Sequence[bool],
) -> str | None:
    """实际分块布局：``chunk_count>0``、从 0 连续、条数一致且全部 embedded。"""
    chunk_count = _value(document, "chunk_count")
    if isinstance(chunk_count, bool) or not isinstance(chunk_count, int) or chunk_count <= 0:
        return REASON_EMPTY_DOCUMENT
    indexes = list(chunk_indexes)
    flags = list(embedded_flags)
    if not indexes:
        return REASON_EMPTY_DOCUMENT
    if len(indexes) != chunk_count or len(flags) != len(indexes):
        return REASON_INCOMPLETE
    for expected, actual in enumerate(indexes):
        if isinstance(actual, bool) or not isinstance(actual, int) or actual != expected:
            return REASON_INCOMPLETE
    if any(flag is not True for flag in flags):
        return REASON_INCOMPLETE
    return None


def chunk_limit_reason(chunk_count: int, char_count: int) -> str | None:
    """每边分块数 / 字符数上限；先判块数，避免超限时加载正文。"""
    if chunk_count > MAX_CHUNKS_PER_DOCUMENT:
        return REASON_TOO_MANY_CHUNKS
    if char_count > MAX_CHARS_PER_DOCUMENT:
        return REASON_TOO_MANY_CHARS
    return None


def units_limit_exceeded(unit_count: int) -> bool:
    return unit_count > MAX_UNITS_PER_DOCUMENT


# ---------- 拆分与对齐（纯函数） ----------

def split_units(chunks: Iterable[Any]) -> list[dict]:
    """把分块正文按行拆成比较单元；不合并、不去重、不删除空行。"""
    units: list[dict] = []
    for chunk in chunks:
        chunk_index = _value(chunk, "chunk_index")
        location = public_location(_metadata(chunk))
        for line_index, text in enumerate(_content(chunk).splitlines()):
            units.append({
                "chunk_index": chunk_index,
                "line_index": line_index,
                "text": text,
                "location": dict(location),
            })
    return units


def count_units(chunks: Iterable[Any]) -> int:
    """仅统计行数，不物化比较单元（用于上限判断）。"""
    return sum(len(_content(chunk).splitlines()) for chunk in chunks)


def _row(kind: str, original: dict | None, replacement: dict | None) -> dict:
    return {"kind": kind, "original": original, "replacement": replacement}


def align_units(
    original_units: Sequence[dict],
    replacement_units: Sequence[dict],
) -> tuple[list[dict], dict[str, int]]:
    """按行文本做 ``SequenceMatcher`` 对齐，返回 rows 与 summary。"""
    left = list(original_units)
    right = list(replacement_units)
    matcher = difflib.SequenceMatcher(
        None,
        [unit["text"] for unit in left],
        [unit["text"] for unit in right],
        autojunk=False,
    )
    rows: list[dict] = []
    summary = {_KIND_ADDED: 0, _KIND_REMOVED: 0, _KIND_MODIFIED: 0, _KIND_UNCHANGED: 0}
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append(_row(_KIND_UNCHANGED, left[i1 + offset], right[j1 + offset]))
                summary[_KIND_UNCHANGED] += 1
        elif tag == "delete":
            for offset in range(i1, i2):
                rows.append(_row(_KIND_REMOVED, left[offset], None))
                summary[_KIND_REMOVED] += 1
        elif tag == "insert":
            for offset in range(j1, j2):
                rows.append(_row(_KIND_ADDED, None, right[offset]))
                summary[_KIND_ADDED] += 1
        else:  # replace：前 min(len) 对为 modified，多出的分别为 removed/added
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                rows.append(_row(_KIND_MODIFIED, left[i1 + offset], right[j1 + offset]))
                summary[_KIND_MODIFIED] += 1
            for offset in range(i1 + paired, i2):
                rows.append(_row(_KIND_REMOVED, left[offset], None))
                summary[_KIND_REMOVED] += 1
            for offset in range(j1 + paired, j2):
                rows.append(_row(_KIND_ADDED, None, right[offset]))
                summary[_KIND_ADDED] += 1
    return rows, summary


# ---------- 指纹 ----------

def _canonical_time(value: Any) -> str | None:
    """aware 时间规范为 UTC ISO；null 保持 null。

    合法的 ORM 行不会出现 naive 时间；万一出现，保留其原始 ISO 形式而不是
    猜测时区，至少保证指纹稳定可复算。
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("文档治理时间字段类型异常，拒绝计算指纹")
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).isoformat()
    return value.isoformat()


def _document_fingerprint_payload(document: Any, chunks: Iterable[Any]) -> dict:
    entries: list[dict] = []
    for chunk in chunks:
        chunk_index = _value(chunk, "chunk_index")
        if isinstance(chunk_index, bool) or not isinstance(chunk_index, int):
            raise ValueError("chunk_index 必须是整数，拒绝计算指纹")
        entries.append({
            "chunk_index": chunk_index,
            "content_sha256": hashlib.sha256(_content(chunk).encode("utf-8")).hexdigest(),
        })
    entries.sort(key=lambda item: item["chunk_index"])
    superseded_by_id = _value(document, "superseded_by_id")
    return {
        "id": str(_value(document, "id")),
        "kb_id": str(_value(document, "kb_id")),
        "index_version": _value(document, "index_version"),
        "governance_revision": _value(document, "governance_revision"),
        "status": _value(document, "status"),
        "enabled": _value(document, "enabled"),
        "valid_from": _canonical_time(_value(document, "valid_from")),
        "valid_until": _canonical_time(_value(document, "valid_until")),
        "superseded_at": _canonical_time(_value(document, "superseded_at")),
        "superseded_by_id": None if superseded_by_id is None else str(superseded_by_id),
        "chunks": entries,
    }


def comparison_fingerprint(
    original: Any,
    replacement: Any,
    original_chunks: Iterable[Any],
    replacement_chunks: Iterable[Any],
) -> str:
    """64 位小写十六进制 SHA-256；只覆盖稳定身份/治理字段与分块内容摘要。"""
    payload = {
        "algorithm": FINGERPRINT_ALGORITHM,
        "original": _document_fingerprint_payload(original, original_chunks),
        "replacement": _document_fingerprint_payload(replacement, replacement_chunks),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_fingerprint(value: Any) -> str:
    """校验外部传入的预览指纹格式（只接受 64 位小写十六进制）。"""
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise HTTPException(status_code=422, detail="comparison_fingerprint 必须是 64 位小写十六进制")
    return value


# ---------- 数据库读取（只读） ----------

def _lock_documents_for_read(
    db: DBSession,
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    replacement_document_id: uuid.UUID,
) -> tuple[Document, Document]:
    """按 UUID 升序对两份文档加 ``FOR SHARE`` 锁并强制重读。

    两份文档必须同属 ``kb_id``；任一不存在（含跨库）一律 404。
    """
    locked: dict[uuid.UUID, Document] = {}
    for identifier in sorted({doc_id, replacement_document_id}, key=str):
        document = db.scalar(
            select(Document)
            .where(Document.id == identifier, Document.kb_id == kb_id)
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise HTTPException(status_code=404, detail="文档不存在")
        locked[identifier] = document
    return locked[doc_id], locked[replacement_document_id]


def _count_chunks(db: DBSession, document_id: uuid.UUID) -> int:
    return int(db.scalar(
        select(func.count()).select_from(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
    ) or 0)


def _sum_chunk_chars(db: DBSession, document_id: uuid.UUID) -> int:
    return int(db.scalar(
        select(func.coalesce(func.sum(func.length(DocumentChunk.content)), 0))
        .where(DocumentChunk.document_id == document_id)
    ) or 0)


def _chunk_layout(db: DBSession, document_id: uuid.UUID):
    return db.execute(
        select(DocumentChunk.chunk_index, DocumentChunk.embedded)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
    ).all()


def _load_chunks(db: DBSession, document_id: uuid.UUID) -> list[DocumentChunk]:
    return list(db.scalars(
        select(DocumentChunk)
        .where(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index)
        .execution_options(populate_existing=True)
    ).all())


def scan_document(db: DBSession, document: Document) -> tuple[str | None, list[DocumentChunk]]:
    """读取并校验一侧分块；超限时绝不加载正文。

    顺序：``chunk_count``/SQL count → SQL 字符总和（超限直接返回，不加载正文）
    → 布局（索引、embedded）→ 加载正文（加载后按实际行复核，避免计数与读取
    之间的漂移）。
    """
    chunk_count = _value(document, "chunk_count")
    if isinstance(chunk_count, bool) or not isinstance(chunk_count, int) or chunk_count <= 0:
        return REASON_EMPTY_DOCUMENT, []
    total = _count_chunks(db, document.id)
    if total == 0:
        return REASON_EMPTY_DOCUMENT, []
    if total > MAX_CHUNKS_PER_DOCUMENT:
        # 块数已超限：连字符总和都不必计算，更不加载正文。
        return REASON_TOO_MANY_CHUNKS, []
    limit_reason = chunk_limit_reason(total, _sum_chunk_chars(db, document.id))
    if limit_reason is not None:
        return limit_reason, []
    layout = _chunk_layout(db, document.id)
    reason = chunk_layout_reason(
        document, [row[0] for row in layout], [row[1] for row in layout]
    )
    if reason is not None:
        return reason, []
    chunks = _load_chunks(db, document.id)
    reason = chunk_layout_reason(
        document, [chunk.chunk_index for chunk in chunks],
        [chunk.embedded for chunk in chunks],
    )
    if reason is not None:
        return reason, []
    limit_reason = chunk_limit_reason(
        len(chunks), sum(len(_content(chunk)) for chunk in chunks)
    )
    if limit_reason is not None:
        return limit_reason, []
    return None, chunks


def document_snapshot(document: Document, *, now: datetime) -> dict:
    """响应中的文档快照；非法治理字段按不可检索（disabled）展示。"""
    try:
        state = document_lifecycle.lifecycle_state(document, now=now)
    except (AttributeError, TypeError, ValueError):
        state = document_lifecycle.LIFECYCLE_DISABLED
    return {
        "id": document.id,
        "filename": document.filename,
        "index_version": _value(document, "index_version"),
        "governance_revision": _value(document, "governance_revision"),
        "chunk_count": _value(document, "chunk_count"),
        "char_count": _value(document, "char_count"),
        "lifecycle_state": state,
        "retrievable": document_lifecycle.is_retrievable(document, now=now),
    }


def _payload(
    *,
    comparable: bool,
    reason: str,
    original: dict,
    replacement: dict,
    rows: list[dict] | None = None,
    summary: dict[str, int] | None = None,
    fingerprint: str | None = None,
) -> dict:
    return {
        "comparison_version": COMPARISON_VERSION,
        "basis": COMPARISON_BASIS,
        "comparable": comparable,
        "reason": reason,
        "original": original,
        "replacement": replacement,
        "summary": summary,
        "rows": rows if rows is not None else [],
        "comparison_fingerprint": fingerprint,
    }


def build_comparison(
    db: DBSession,
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    replacement_document_id: uuid.UUID,
) -> dict:
    """只读构建差异预览；不写任何表，也不记录正文。"""
    if not isinstance(doc_id, uuid.UUID) or not isinstance(replacement_document_id, uuid.UUID):
        raise HTTPException(status_code=422, detail="文档 ID 必须是 UUID")
    if replacement_document_id == doc_id:
        raise HTTPException(status_code=400, detail="对比文档不能是自身")

    original, replacement = _lock_documents_for_read(
        db, kb_id, doc_id, replacement_document_id
    )
    # 契约：所有锁等待结束后才读取当前 UTC。
    now = datetime.now(timezone.utc)
    original_snapshot = document_snapshot(original, now=now)
    replacement_snapshot = document_snapshot(replacement, now=now)

    reason = precheck_reason(original, replacement, now=now)
    if reason is not None:
        return _payload(
            comparable=False, reason=reason,
            original=original_snapshot, replacement=replacement_snapshot,
        )

    reason, original_chunks = scan_document(db, original)
    if reason is not None:
        return _payload(
            comparable=False, reason=reason,
            original=original_snapshot, replacement=replacement_snapshot,
        )
    reason, replacement_chunks = scan_document(db, replacement)
    if reason is not None:
        return _payload(
            comparable=False, reason=reason,
            original=original_snapshot, replacement=replacement_snapshot,
        )

    original_units = split_units(original_chunks)
    replacement_units = split_units(replacement_chunks)
    if units_limit_exceeded(len(original_units)) or units_limit_exceeded(len(replacement_units)):
        return _payload(
            comparable=False, reason=REASON_TOO_MANY_UNITS,
            original=original_snapshot, replacement=replacement_snapshot,
        )

    rows, summary = align_units(original_units, replacement_units)
    fingerprint = comparison_fingerprint(
        original, replacement, original_chunks, replacement_chunks
    )
    return _payload(
        comparable=True,
        reason=REASON_OK,
        original=original_snapshot,
        replacement=replacement_snapshot,
        rows=rows,
        summary=summary,
        fingerprint=fingerprint,
    )


def ensure_current_fingerprint(
    db: DBSession,
    original: Document,
    replacement: Document,
    expected_fingerprint: str,
    *,
    now: datetime,
) -> None:
    """supersede 前在校验锁内复核预览指纹。

    调用方必须已经持有两份文档的行写锁，且尚未做任何 ORM 变更。任何资格、
    分块、上限或指纹不一致都返回 409（不自动重试、不留下半更新行）。
    """
    require_fingerprint(expected_fingerprint)
    if precheck_reason(original, replacement, now=now) is not None:
        raise HTTPException(status_code=409, detail=STALE_FINGERPRINT_DETAIL)

    reason, original_chunks = scan_document(db, original)
    if reason is not None:
        raise HTTPException(status_code=409, detail=STALE_FINGERPRINT_DETAIL)
    reason, replacement_chunks = scan_document(db, replacement)
    if reason is not None:
        raise HTTPException(status_code=409, detail=STALE_FINGERPRINT_DETAIL)

    if (units_limit_exceeded(count_units(original_chunks))
            or units_limit_exceeded(count_units(replacement_chunks))):
        raise HTTPException(status_code=409, detail=STALE_FINGERPRINT_DETAIL)

    current = comparison_fingerprint(
        original, replacement, original_chunks, replacement_chunks
    )
    if not hmac.compare_digest(current, expected_fingerprint):
        raise HTTPException(status_code=409, detail=STALE_FINGERPRINT_DETAIL)
