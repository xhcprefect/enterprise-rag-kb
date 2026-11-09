"""文档治理写路径：审核元数据、显式替代与显式重新启用。

冻结契约要点：

- 所有治理写入仅限管理员、仅限普通上传库（``kind='upload'``）；项目库与任何未知
  ``kind`` 一律拒绝，绝不留下旁路。
- CAS：``expected_revision`` 必须精确等于当前 ``governance_revision``，不一致 409。
- 时间输入由 schema 层严格校验并规范为 aware UTC datetime；本服务只消费该结果，
  不再做任何宽松字符串/时间戳解析（naive 值同样拒绝）。
- merged 窗口必须满足 ``valid_from < valid_until``；先校验再改 ORM，避免异常后
  会话自动 flush 留下半更新行。
- ``enabled=true`` 无法绕过替代；替代未显式清除前不允许再次替代（防止链条被覆盖）。
- 替代绝不删除原文件/chunks/向量/入库状态/``index_version``；
  删除替代文档时自引用外键 SET NULL，原文档保持 ``superseded_at`` 继续不可检索。
- ``reenable`` 只清替代标记并置回 ``enabled=true``，要求当前窗口处于 effective
  （scheduled/expired 重新启用被拒绝，绝不静默抹除期限）；无索引/文件副作用。
- 审计只写稳定 ID、revision 与变更标志，不写正文、不写负责人自由文本。

确定性锁顺序（与冻结契约一致，无锁跨 HTTP/模型等待）：

  ``User`` -> ``KnowledgeBase`` -> 会话 -> 上传 ``Document`` 按 UUID 排序。

本服务要求 **调用方已持有** 目标 ``KnowledgeBase`` 行写锁（见
``require_governance_kb``）；服务内部只按 UUID 升序补文档行锁，且 ``now``
一律在所有锁等待结束之后才读取。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.models import (
    KNOWLEDGE_BASE_KIND_PROJECT,
    KNOWLEDGE_BASE_KIND_UPLOAD,
    Document,
    DocumentStatus,
    KnowledgeBase,
)

logger = logging.getLogger(__name__)

MAINTAINER_MAX_LENGTH = 200


# ---------- 时间/字段值（schema 已规范化，这里只做安全复核） ----------

def _require_aware_datetime(value, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise HTTPException(status_code=422, detail=f"{field} 必须是带时区的 ISO 时间")
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=422, detail=f"{field} 必须显式包含时区")
    return value.astimezone(timezone.utc)


def _validate_maintainer(value) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=422, detail="maintainer 必须是字符串")
    if len(value) > MAINTAINER_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"maintainer 最长 {MAINTAINER_MAX_LENGTH} 字符")
    return value


def _ensure_effective_window(valid_from: datetime | None, valid_until: datetime | None) -> None:
    if valid_from is not None and valid_until is not None and valid_from >= valid_until:
        raise HTTPException(status_code=422, detail="valid_from 必须早于 valid_until")


# ---------- 共享入口校验 ----------

def require_governance_kb(db: DBSession, kb_id: uuid.UUID, *, lock: bool = True) -> KnowledgeBase:
    """治理 API 的知识库入口：必须存在且 ``kind`` 精确为 ``upload``。

    项目库与任何未知 ``kind`` 都 400：治理写接口没有项目/自定义类型旁路。
    ``lock=True`` 时返回持有行写锁的 KB 行，调用方必须在调用本模块写函数前
    已经拿到该锁（``User`` 锁应更早，见 ``knowledge.py``）。
    """
    stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    kb = db.scalar(stmt)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        raise HTTPException(status_code=400, detail="项目源码库不支持治理操作")
    if kb.kind != KNOWLEDGE_BASE_KIND_UPLOAD:
        raise HTTPException(status_code=400, detail="该知识库类型不支持治理操作")
    return kb


def _require_revision(document: Document, expected_revision) -> None:
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
        raise HTTPException(status_code=422, detail="expected_revision 必须是非负整数")
    if document.governance_revision != expected_revision:
        raise HTTPException(status_code=409, detail="治理修订号已变化，请刷新后重试")


def _check_effective(document: Document, *, now: datetime) -> None:
    """当前时刻必须落在 effective 窗口内（替代目标校验）。"""
    from app.services.document_lifecycle import lifecycle_state

    state = lifecycle_state(document, now=now)
    if state != "active":
        if state == "superseded":
            raise HTTPException(status_code=409, detail="文档已被替代，请先显式重新启用")
        raise HTTPException(status_code=409, detail=f"文档当前生命周期状态为 {state}，不在生效窗口内")


# ---------- PATCH /governance ----------

def update_governance(
    db: DBSession,
    kb: KnowledgeBase,
    doc_id: uuid.UUID,
    payload,
    *,
    now: datetime | None = None,
) -> Document:
    """合并式治理元数据更新。Omitted 不变、显式 null 清除；替代关系不可通过此接口清除。

    ``payload`` 为 ``app.schemas.knowledge.DocumentGovernanceUpdate``（extra 已禁止，
    日期已规范为 aware UTC）。调用方必须已持有 ``kb`` 的行写锁。
    """
    doc = _lock_document(db, kb.id, doc_id)
    moment = now or datetime.now(timezone.utc)
    _require_revision(doc, payload.expected_revision)

    provided = set(payload.model_fields_set) - {"expected_revision"}
    if not provided:
        raise HTTPException(status_code=422, detail="空补丁：至少提供一个可变字段")

    # 先算出合并后的目标值并校验，全部通过后才写 ORM：任何 422/409 都不会因为
    # 前置赋值在后续 flush 中留下半更新行。
    changes: list[str] = []
    new_maintainer = None
    if "maintainer" in provided:
        if payload.maintainer is None:
            raise HTTPException(status_code=422, detail="maintainer 不能为 null")
        new_maintainer = _validate_maintainer(payload.maintainer)
        changes.append("maintainer")

    new_enabled = None
    if "enabled" in provided:
        if payload.enabled is None:
            raise HTTPException(status_code=422, detail="enabled 不能为 null")
        new_enabled = bool(payload.enabled)
        changes.append("enabled")

    new_reviewed = None
    if "last_reviewed_at" in provided:
        new_reviewed = (
            None if payload.last_reviewed_at is None
            else _require_aware_datetime(payload.last_reviewed_at, "last_reviewed_at")
        )
        changes.append("last_reviewed_at")

    merged_from = doc.valid_from
    if "valid_from" in provided:
        merged_from = (
            None if payload.valid_from is None
            else _require_aware_datetime(payload.valid_from, "valid_from")
        )
        changes.append("valid_from")
    merged_until = doc.valid_until
    if "valid_until" in provided:
        merged_until = (
            None if payload.valid_until is None
            else _require_aware_datetime(payload.valid_until, "valid_until")
        )
        changes.append("valid_until")
    _ensure_effective_window(merged_from, merged_until)

    if "maintainer" in provided:
        doc.maintainer = new_maintainer
    if "enabled" in provided:
        doc.enabled = new_enabled
    if "last_reviewed_at" in provided:
        doc.last_reviewed_at = new_reviewed
    if "valid_from" in provided:
        doc.valid_from = merged_from
    if "valid_until" in provided:
        doc.valid_until = merged_until

    doc.governance_revision += 1
    revision = doc.governance_revision
    db.commit()
    from app.services.document_lifecycle import is_retrievable

    logger.info(
        "document governance updated: doc=%s revision=%s fields=%s retrievable=%s",
        doc.id, revision, sorted(changes), is_retrievable(doc, now=moment),
    )
    return doc


# ---------- POST /supersede ----------

def supersede_document(
    db: DBSession,
    kb: KnowledgeBase,
    doc_id: uuid.UUID,
    replacement_document_id: uuid.UUID,
    expected_revision,
    *,
    now: datetime | None = None,
    comparison_fingerprint: str | None = None,
) -> Document:
    """把当前文档标记为被 ``replacement_document_id`` 替代。

    要求：同一上传库、非自身、替代目标 fresh ready/未取消/effective、
    替代链不成环；原文档若已替代须先显式 reenable。
    原文件/chunks/向量/入库状态/``index_version`` 全部保留。
    两张文档行按 UUID 升序加锁（不按 original/replacement 的语义顺序），
    ``now`` 在全部锁等待结束后才读取。调用方必须已持有 ``kb`` 的行写锁。

    ``comparison_fingerprint`` 可选：传入时在文档锁内、任何 ORM 变更之前重新
    校验两边资格与分块布局并重算指纹；不一致返回 409，零修改。不传时保持
    原有行为（兼容旧客户端）。
    """
    if not isinstance(replacement_document_id, uuid.UUID):
        raise HTTPException(status_code=422, detail="replacement_document_id 必须是 UUID")
    if replacement_document_id == doc_id:
        raise HTTPException(status_code=400, detail="替代文档不能是自身")

    # 确定性锁顺序：两份文档一律按 UUID 升序加锁，避免相反方向死锁。
    locked: dict[uuid.UUID, Document] = {}
    for locked_id in sorted({doc_id, replacement_document_id}, key=str):
        locked[locked_id] = _lock_document(db, kb.id, locked_id)
    original = locked[doc_id]
    replacement = locked[replacement_document_id]

    moment = now or datetime.now(timezone.utc)
    _require_revision(original, expected_revision)
    if original.superseded_at is not None or original.superseded_by_id is not None:
        raise HTTPException(status_code=409, detail="文档已被替代，请先显式重新启用")
    if replacement.status != DocumentStatus.READY.value or replacement.cancel_requested:
        raise HTTPException(status_code=409, detail="替代文档必须处于已就绪且未取消的状态")
    _check_effective(replacement, now=moment)

    # 防环：沿 superseded_by 链向上追溯，出现 original 即闭合环。
    # KB 写锁已串行化本库的治理图变更，链读取不需要额外文档锁。
    seen: set[uuid.UUID] = {original.id, replacement.id}
    cursor = replacement
    while cursor.superseded_by_id is not None:
        if cursor.superseded_by_id in seen:
            raise HTTPException(status_code=409, detail="替代关系成环，已拒绝")
        seen.add(cursor.superseded_by_id)
        cursor = db.get(Document, cursor.superseded_by_id)
        if cursor is None:
            break

    # 预览指纹复核：必须在文档锁内、任何 ORM 变更之前完成。失败 409 且零修改，
    # 绝不自动重试；不传指纹的旧调用完全跳过本段。
    if comparison_fingerprint is not None:
        from app.services import document_comparison

        document_comparison.ensure_current_fingerprint(
            db, original, replacement, comparison_fingerprint, now=moment
        )

    original.superseded_at = moment
    original.superseded_by_id = replacement.id
    original.governance_revision += 1
    revision = original.governance_revision
    db.commit()
    logger.info(
        "document superseded: doc=%s by=%s revision=%s",
        original.id, replacement.id, revision,
    )
    return original


# ---------- POST /reenable ----------

def reenable_document(
    db: DBSession,
    kb: KnowledgeBase,
    doc_id: uuid.UUID,
    expected_revision,
    *,
    now: datetime | None = None,
) -> Document:
    """显式清除替代标记并置回 ``enabled=true``。

    要求当前窗口 effective：scheduled/expired 状态重新启用一律拒绝，绝不静默抹除
    期限日期。无任何索引、文件、chunks 或向量副作用。调用方必须已持有 KB 行锁。
    """
    doc = _lock_document(db, kb.id, doc_id)
    moment = now or datetime.now(timezone.utc)
    _require_revision(doc, expected_revision)
    if doc.superseded_at is None and doc.superseded_by_id is None and doc.enabled:
        raise HTTPException(status_code=400, detail="文档未被替代且已启用，无需重新启用")
    # 重新启用只允许在当前时刻生效窗口内：清掉替代标记后的状态若为 scheduled/expired
    # 必须 409，绝不静默抹除期限日期；用户需先通过 governance 显式调整窗口。
    from app.services.document_lifecycle import lifecycle_state

    state = lifecycle_state(_without_supersession(doc), now=moment)
    if state in {"scheduled", "expired"}:
        raise HTTPException(status_code=409, detail=f"文档生效窗口当前为 {state}，请先调整生效日期")

    doc.superseded_at = None
    doc.superseded_by_id = None
    doc.enabled = True
    doc.governance_revision += 1
    revision = doc.governance_revision
    db.commit()
    logger.info("document reenabled: doc=%s revision=%s", doc.id, revision)
    return doc


# ---------- 去重/重放：治理性不可用文档必须显式 409 ----------

def ensure_active_governance_document(canonical: Document | None) -> None:
    """命中治理性不可用行时显式 409：绝不复活，也不假装可检索。

    ``inactive`` 指治理层面的不可检索状态（superseded/disabled/scheduled/expired）；
    入库状态（pending/processing/failed/cancelled）不在本函数判定范围，沿用现有
    去重/重放语义。``canonical is None`` 视为无命中，直接返回。
    """
    if canonical is None:
        return
    from app.services.document_lifecycle import lifecycle_state

    state = lifecycle_state(canonical)
    if state == "active":
        return
    raise HTTPException(
        status_code=409,
        detail=f"知识库中已存在相同内容但该文档当前不可检索（{state}），"
               "请先由管理员恢复或更换内容",
    )


# 兼容既有调用名：去重 canonical 校验与通用治理可用性校验是同一判定。
ensure_active_dedup_canonical = ensure_active_governance_document


class _WithoutSupersession:
    """轻量视图：模拟清掉替代标记后的行，仅用于生效窗口校验。"""

    def __init__(self, doc):
        self.enabled = True
        self.superseded_at = None
        self.superseded_by_id = None
        self.valid_from = doc.valid_from
        self.valid_until = doc.valid_until


def _without_supersession(doc) -> _WithoutSupersession:
    return _WithoutSupersession(doc)


def _lock_document(db: DBSession, kb_id: uuid.UUID, doc_id: uuid.UUID) -> Document:
    """单文档行锁（populate_existing 强制重读）；KB 行锁由调用方先行持有。"""
    doc = db.scalar(
        select(Document)
        .where(Document.id == doc_id, Document.kb_id == kb_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


def audit_detail(document: Document, changes: list[str]) -> dict:
    """审计 detail 只放稳定 ID/revision/变更字段名，绝不写正文或负责人文本。"""
    return {
        "document_id": str(document.id),
        "governance_revision": int(document.governance_revision),
        "fields": sorted(changes),
    }
