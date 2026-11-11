"""反馈回归执行上下文：批量根规划、统一加锁门禁与可脱会话的有限快照。

本模块为后续持久作业（R1b）与独立执行 worker（R2）提供唯一的“根”基础设施：

* ``plan_roots``：在**不加锁**的情况下先收集一批反馈（1–20 条）的全部根身份，
  同类型按 UUID 稳定排序。
* ``lock_roots``：按冻结顺序
  ``User -> KnowledgeBase -> ProjectSource -> ProjectSnapshot -> Conversation
  -> ChatTurn -> Message -> Document -> Feedback`` 取行写锁，等锁后重读规划身份，
  任一漂移都失败关闭。管理员认证期的原始 token 版本、当前 role/active，以及原反馈
  所有者的当前 active / KB 成员 ACL 都在锁后复核；绝不以管理员权限替代原用户。
* ``build_contexts``：在锁内构建**有限、可序列化、脱离 Session 后仍可读取**的执行
  上下文：ID、版本、指纹、必要临时 case 与问题序列。不复制回答/引用正文。

上传执行清单（manifest）有两种来源：

* ``fixed_manifests`` 未给出该反馈 -> 首次冻结：原会话限定范围优先，否则当前全库
  可检索 ``document_id + index_version``。
* 显式给出 ``[]`` -> 本次固定“无文档”，绝不再按全库重算。
* 显式给出非空清单 -> 只做“原定身份仍可检索/代次未变”的复核；排队后新增的无关
  文档不会进入清单，也不会因此把合法上下文判成变化。

清单上限 ``MAX_EXECUTION_MANIFEST = 256``，超限明确拒绝、绝不截断。清单不写入旧的
manual 评测 case / dataset / binding 摘要；只有限定范围时的显式 ``document_scope``
才进入 case。项目来源固定原会话快照（不随 ``active_snapshot_id`` 漂移），并校验
真实 ProjectSource -> ProjectSnapshot -> ProjectFileVersion -> ProjectChunk 映射。

服务自身不 commit、不 rollback、不持锁调用模型/网络、不创建 Conversation /
ChatTurn / Message。拿到的 ORM 行只在 ``LockedRoots`` 内部使用；``ExecutionContext``
只含基元、列表/字典与字符串指纹。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import func, select

from app.models import (
    KNOWLEDGE_BASE_KIND_PROJECT,
    KNOWLEDGE_BASE_KIND_UPLOAD,
    AnswerFeedback,
    ChatTurn,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    ProjectChunk,
    ProjectFileParseStatus,
    ProjectFileVersion,
    ProjectSnapshot,
    ProjectSnapshotFile,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
)
from app.services import answer_sources, document_lifecycle, document_scopes, kb_access
from app.services.feedback_regression_evaluation import (
    RegressionUnavailable,
    build_base_case,
)

MAX_BATCH_FEEDBACKS = 20
MAX_EXECUTION_MANIFEST = 256

_EXECUTION_UPLOAD = "upload"
_EXECUTION_PROJECT = "project"
_EXECUTION_CHAT = "chat"

_PROJECT_READY_STATUSES = (
    ProjectFileParseStatus.READY.value,
    ProjectFileParseStatus.FALLBACK.value,
)
_PROJECT_IDENTITY_KEYS = (
    "snapshot_id",
    "source_id",
    "manifest_fingerprint",
    "config_fingerprint",
)


class RegressionContextError(Exception):
    """根规划/加锁/上下文构建的稳定失败；``status_code`` 供 API 层映射。"""

    def __init__(self, reason: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status_code = status_code


def _fail(status_code: int, reason: str, message: str):
    raise RegressionContextError(reason, message, status_code)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_uuid(value: Any) -> uuid.UUID:
    if isinstance(value, bool) or not isinstance(value, (str, uuid.UUID)):
        raise ValueError("not uuid")
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("not uuid") from exc


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    return None if value is None else _strict_uuid(value)


def _normalize_scope(scope: Any) -> tuple[dict, ...]:
    """把会话 document_scope 规范成稳定、可比较的 ``(document_id, index_version)``。

    非法结构在加锁 / 上下文构建路径必须 ``scope_invalid``，不能静默退化成空范围
    （空范围代表全库）。
    """
    if not isinstance(scope, list):
        return ()
    try:
        pairs = document_scopes.scope_pairs(scope)
    except (ValueError, TypeError, AttributeError):
        return ()
    return tuple({"document_id": str(identifier), "index_version": version}
                 for identifier, version in pairs)


def _scope_entries(scope: Any) -> tuple[dict, ...]:
    """严格读取会话限定范围：显式空列表才是全库，坏结构失败关闭。"""
    if scope is None:
        return ()
    if not isinstance(scope, list):
        _fail(409, "scope_invalid", "会话限定文档范围已失效")
    if not scope:
        return ()
    entries = _normalize_scope(scope)
    if not entries:
        _fail(409, "scope_invalid", "会话限定文档范围已失效")
    return entries


def _annotation_fingerprint(feedback: AnswerFeedback) -> str:
    """反馈标注身份的规范摘要；用于等锁后判断根规划是否漂移。"""
    reviewed_at = feedback.reviewed_at
    return _sha256({
        "id": str(feedback.id),
        "revision": feedback.revision,
        "user_id": str(feedback.user_id),
        "message_id": str(feedback.message_id),
        "conversation_id": str(feedback.conversation_id),
        "status": feedback.status,
        "result_sha256": feedback.result_sha256,
        "reviewed_at": None if reviewed_at is None else reviewed_at.astimezone(timezone.utc).isoformat(),
        "corrected_outcome": feedback.corrected_outcome,
        "expected_sources": feedback.expected_sources,
        "required_phrases": feedback.required_phrases,
        "forbidden_phrases": feedback.forbidden_phrases,
        "unknowns": feedback.unknowns,
        "evidence_note": feedback.evidence_note,
    })


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RootPlanItem:
    feedback_id: uuid.UUID
    owner_id: uuid.UUID
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    question_message_id: uuid.UUID | None
    kb_id: uuid.UUID | None
    mode: str
    project_snapshot_id: uuid.UUID | None
    project_source_id: uuid.UUID | None
    scope_revision: int
    document_scope: tuple[dict, ...]
    expected_document_ids: tuple[uuid.UUID, ...]
    annotation_fingerprint: str


@dataclass(frozen=True)
class BatchRootPlan:
    items: tuple[RootPlanItem, ...]

    @property
    def feedback_ids(self) -> tuple[uuid.UUID, ...]:
        return tuple(item.feedback_id for item in self.items)


def _feedback_ids(values: Iterable[Any]) -> list[uuid.UUID]:
    try:
        raw = list(values)
    except TypeError:
        _fail(422, "invalid_batch", "反馈标识必须是列表")
    if not 1 <= len(raw) <= MAX_BATCH_FEEDBACKS:
        _fail(422, "invalid_batch", f"批量回归必须包含 1–{MAX_BATCH_FEEDBACKS} 条反馈")
    result: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for value in raw:
        try:
            identifier = _strict_uuid(value)
        except ValueError:
            _fail(422, "invalid_batch", "反馈标识不合法")
        if identifier in seen:
            _fail(422, "invalid_batch", "批量回归中的反馈标识不能重复")
        seen.add(identifier)
        result.append(identifier)
    return result


def _expected_document_ids(values: Any) -> tuple[uuid.UUID, ...]:
    """规划阶段只从反馈标注里提取可锁定的上传文档 ID；项目身份另行锁快照。"""
    if not isinstance(values, list):
        return ()
    found: set[uuid.UUID] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        try:
            if item.get("document_id") is not None:
                found.add(_strict_uuid(item["document_id"]))
        except ValueError:
            continue
    return tuple(sorted(found))


def plan_roots(db, feedback_ids: Iterable[Any]) -> BatchRootPlan:
    """先收集全部根（无锁）。同类型 UUID 顺序在加锁阶段使用；结果保持输入顺序。"""
    identifiers = _feedback_ids(feedback_ids)
    with db.no_autoflush:
        feedbacks = {
            row.id: row
            for row in db.scalars(
                select(AnswerFeedback).where(AnswerFeedback.id.in_(identifiers))
            ).all()
        }
        missing = [identifier for identifier in identifiers if identifier not in feedbacks]
        if missing:
            _fail(404, "feedback_not_found", "反馈不存在")
        conversations = {
            row.id: row
            for row in db.scalars(
                select(Conversation).where(
                    Conversation.id.in_({feedbacks[identifier].conversation_id for identifier in identifiers})
                )
            ).all()
        }
        snapshot_ids = {
            row.project_snapshot_id
            for row in conversations.values()
            if row.project_snapshot_id is not None
        }
        snapshot_sources: dict[uuid.UUID, uuid.UUID] = {}
        if snapshot_ids:
            snapshot_sources = dict(
                db.execute(
                    select(ProjectSnapshot.id, ProjectSnapshot.source_id)
                    .where(ProjectSnapshot.id.in_(snapshot_ids))
                ).all()
            )
        message_ids = {feedbacks[identifier].message_id for identifier in identifiers}
        reply_to: dict[uuid.UUID, uuid.UUID | None] = dict(
            db.execute(select(Message.id, Message.reply_to_id).where(Message.id.in_(message_ids))).all()
        )

    items: list[RootPlanItem] = []
    for identifier in identifiers:
        feedback = feedbacks[identifier]
        conversation = conversations.get(feedback.conversation_id)
        if conversation is None:
            _fail(409, "feedback_stale", "原回答与会话信息不一致，反馈结果已过期")
        snapshot_id = conversation.project_snapshot_id
        source_id = None
        if snapshot_id is not None:
            source_id = snapshot_sources.get(snapshot_id)
            if source_id is None:
                _fail(409, "project_snapshot_invalid", "项目快照已不可用")
        items.append(
            RootPlanItem(
                feedback_id=feedback.id,
                owner_id=feedback.user_id,
                conversation_id=feedback.conversation_id,
                message_id=feedback.message_id,
                question_message_id=reply_to.get(feedback.message_id),
                kb_id=conversation.kb_id,
                mode=conversation.mode,
                project_snapshot_id=snapshot_id,
                project_source_id=source_id,
                scope_revision=int(conversation.scope_revision or 0),
                document_scope=_scope_entries(conversation.document_scope),
                expected_document_ids=_expected_document_ids(feedback.expected_sources),
                annotation_fingerprint=_annotation_fingerprint(feedback),
            )
        )
    return BatchRootPlan(items=tuple(items))


# ---------------------------------------------------------------------------
# 加锁
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LockedRoots:
    """已按全局顺序锁定的根；只在当前 Session/事务内有效，不脱离会话使用。"""

    plan: BatchRootPlan
    admin: User
    users: dict[uuid.UUID, User]
    knowledge_bases: dict[uuid.UUID, KnowledgeBase]
    conversations: dict[uuid.UUID, Conversation]
    turns_by_conversation: dict[uuid.UUID, tuple[ChatTurn, ...]]
    messages: dict[uuid.UUID, Message]
    documents: dict[uuid.UUID, Document]
    feedbacks: dict[uuid.UUID, AnswerFeedback]
    project_snapshots: dict[uuid.UUID, ProjectSnapshot]
    project_sources: dict[uuid.UUID, ProjectSource]
    upload_manifests: dict[uuid.UUID, tuple[dict, ...]]
    limited_scopes: dict[uuid.UUID, tuple[dict, ...]]
    freeze_manifests: bool = True

    def project_binding(self) -> answer_sources.ProjectBinding:
        return answer_sources.ProjectBinding(
            snapshots=dict(self.project_snapshots),
            sources=dict(self.project_sources),
        )


def _lock_users(db, admin_id: uuid.UUID, auth_token_version: int, owner_ids: set[uuid.UUID]) -> dict[uuid.UUID, User]:
    identifiers = sorted({admin_id, *owner_ids})
    with db.no_autoflush:
        rows = db.scalars(
            select(User)
            .where(User.id.in_(identifiers))
            .order_by(User.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    users = {row.id: row for row in rows}
    admin = users.get(admin_id)
    if admin is None or not admin.is_active or type(auth_token_version) is not int \
            or auth_token_version < 0 or admin.token_version != auth_token_version:
        _fail(401, "auth_invalid", "登录状态已失效，请重新登录")
    if admin.role != "admin":
        _fail(403, "admin_required", "需要管理员权限")
    for owner_id in owner_ids:
        owner = users.get(owner_id)
        if owner is None or not owner.is_active:
            _fail(409, "owner_inactive", "反馈所有者已禁用，不能按原权限重跑")
    return users


def _lock_rows(db, model, identifiers: Iterable[uuid.UUID]) -> dict[uuid.UUID, Any]:
    ordered = sorted(set(identifiers))
    if not ordered:
        return {}
    with db.no_autoflush:
        rows = db.scalars(
            select(model)
            .where(model.id.in_(ordered))
            .order_by(model.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    return {row.id: row for row in rows}


def _lock_project_roots(db, plan: BatchRootPlan) -> tuple[dict[uuid.UUID, ProjectSnapshot], dict[uuid.UUID, ProjectSource]]:
    """批量项目根：先全部 ProjectSource（UUID 序），再全部 ProjectSnapshot（UUID 序）。"""
    planned = {item.project_snapshot_id: item for item in plan.items if item.project_snapshot_id is not None}
    if not planned:
        return {}, {}
    snapshot_ids = sorted(planned)
    with db.no_autoflush:
        pairs = dict(
            db.execute(
                select(ProjectSnapshot.id, ProjectSnapshot.source_id)
                .where(ProjectSnapshot.id.in_(snapshot_ids))
            ).all()
        )
    if set(pairs) != set(snapshot_ids):
        _fail(409, "project_snapshot_invalid", "项目快照已不可用")
    sources = _lock_rows(db, ProjectSource, pairs.values())
    if set(sources) != set(pairs.values()):
        _fail(409, "project_snapshot_invalid", "项目快照缺少项目源")
    snapshots = _lock_rows(db, ProjectSnapshot, snapshot_ids)
    if set(snapshots) != set(snapshot_ids):
        _fail(409, "project_snapshot_invalid", "项目快照已不可用")
    for snapshot_id, source_id in pairs.items():
        if snapshots[snapshot_id].source_id != source_id:
            _fail(409, "root_plan_changed", "项目快照绑定在等锁期间发生变化")
        item = planned[snapshot_id]
        if item.project_source_id != source_id:
            _fail(409, "root_plan_changed", "项目快照身份在等锁期间发生变化")
        source = sources[source_id]
        if item.kb_id is None or source.kb_id != item.kb_id:
            _fail(409, "project_snapshot_invalid", "项目快照不属于当前知识库")
        if source.revoked_at is not None:
            _fail(409, "project_snapshot_invalid", "项目源已撤销")
        snapshot = snapshots[snapshot_id]
        if snapshot.status != ProjectSnapshotStatus.READY.value:
            _fail(409, "project_snapshot_invalid", "项目快照未就绪")
        if not snapshot.manifest_fingerprint or not snapshot.config_fingerprint:
            _fail(409, "project_snapshot_invalid", "项目快照缺少 manifest/config 指纹")
    return snapshots, sources


def _normalize_manifest(value: Any) -> tuple[dict, ...]:
    if not isinstance(value, (list, tuple)):
        _fail(409, "manifest_invalid", "执行清单必须是列表")
    if len(value) > MAX_EXECUTION_MANIFEST:
        _fail(409, "manifest_overflow", f"执行清单超过 {MAX_EXECUTION_MANIFEST} 项")
    entries: list[dict] = []
    seen: set[uuid.UUID] = set()
    for item in value:
        if not isinstance(item, Mapping):
            _fail(409, "manifest_invalid", "执行清单条目结构不合法")
        try:
            identifier = _strict_uuid(item.get("document_id"))
        except ValueError:
            _fail(409, "manifest_invalid", "执行清单文档标识不合法")
        version = item.get("index_version")
        if type(version) is not int or version < 0:
            _fail(409, "manifest_invalid", "执行清单索引代次不合法")
        if identifier in seen:
            _fail(409, "manifest_invalid", "执行清单文档标识重复")
        seen.add(identifier)
        entries.append({"document_id": str(identifier), "index_version": version})
    entries.sort(key=lambda entry: entry["document_id"])
    return tuple(entries)


def _retrievable_documents(db, kb_id: uuid.UUID) -> list[tuple[uuid.UUID, int]]:
    with db.no_autoflush:
        count = db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.kb_id == kb_id, *document_lifecycle.retrievable_conditions())
        ) or 0
        if count > MAX_EXECUTION_MANIFEST:
            _fail(409, "manifest_overflow", f"全库执行清单超过 {MAX_EXECUTION_MANIFEST} 项")
        rows = db.execute(
            select(Document.id, Document.index_version)
            .where(Document.kb_id == kb_id, *document_lifecycle.retrievable_conditions())
            .order_by(Document.id)
        ).all()
    if len(rows) > MAX_EXECUTION_MANIFEST:
        _fail(409, "manifest_overflow", f"全库执行清单超过 {MAX_EXECUTION_MANIFEST} 项")
    return [(row[0], int(row[1])) for row in rows]


def _freeze_upload_manifest(db, kb: KnowledgeBase, conversation: Conversation,
                            fixed: Any = None, *, has_fixed: bool = False) -> tuple[dict, ...]:
    limited = _scope_entries(conversation.document_scope)
    if has_fixed:
        manifest = _normalize_manifest(fixed)
        if limited:
            allowed = {(entry["document_id"], entry["index_version"]) for entry in limited}
            if any((entry["document_id"], entry["index_version"]) not in allowed for entry in manifest):
                _fail(409, "scope_invalid", "固定执行清单越过原限定文档范围")
        return manifest
    if limited:
        return tuple({"document_id": entry["document_id"], "index_version": entry["index_version"]}
                     for entry in limited)
    return tuple({"document_id": str(identifier), "index_version": version}
                 for identifier, version in _retrievable_documents(db, kb.id))


def _lock_documents(db, identifiers: Iterable[uuid.UUID]) -> dict[uuid.UUID, Document]:
    return _lock_rows(db, Document, identifiers)


def _verify_upload_manifests(
    db,
    plan: BatchRootPlan,
    manifests: dict[uuid.UUID, tuple[dict, ...]],
    knowledge_bases: dict[uuid.UUID, KnowledgeBase],
    limited_scopes: dict[uuid.UUID, tuple[dict, ...]],
    documents: dict[uuid.UUID, Document],
) -> None:
    """等 Document 行锁后复核：原定文档仍可检索，代次未变；不因新增文档判失败。"""
    now = datetime.now(timezone.utc)
    for item in plan.items:
        manifest = manifests.get(item.feedback_id)
        if manifest is None:
            continue
        kb = knowledge_bases.get(item.kb_id)
        if kb is None or kb.kind != KNOWLEDGE_BASE_KIND_UPLOAD:
            continue
        scope = limited_scopes.get(item.feedback_id)
        allowed = None if scope is None else {(entry["document_id"], entry["index_version"]) for entry in scope}
        for entry in manifest:
            try:
                identifier = uuid.UUID(entry["document_id"])
            except (ValueError, TypeError, AttributeError):
                _fail(409, "manifest_invalid", "执行清单文档标识不合法")
            document = documents.get(identifier)
            if document is None or document.kb_id != kb.id:
                _fail(409, "source_invalid", "执行清单中的文档不存在或不属于当前知识库")
            if int(document.index_version) != entry["index_version"]:
                _fail(409, "version_mismatch", "执行清单中的文档索引代次已变化")
            if not document_lifecycle.is_retrievable(document, now=now):
                _fail(409, "source_invalid", "执行清单中的文档当前不可检索")
            if allowed is not None and (entry["document_id"], entry["index_version"]) not in allowed:
                _fail(409, "scope_invalid", "执行清单越过原限定文档范围")


_MISSING = object()


def lock_roots(db, plan: BatchRootPlan, *, admin_id: uuid.UUID, auth_token_version: int,
               fixed_manifests: Mapping[uuid.UUID | str, Sequence | None] | None = None,
               freeze_manifests: bool = True) -> LockedRoots:
    """按全局类型/ UUID 顺序锁定一批根；等锁后重读并失败关闭。

    ``freeze_manifests=True`` 是在线执行默认：为每个上传反馈冻结本次执行清单
    （原限定范围优先，否则当前全库可检索文档），并硬限 256 项。旧 manual 回归传
    ``False``：只锁/校验标注来源与非空限定范围所需根，**不枚举整库**、不套用在线
    清单上限，也不把空清单当成全库。

    ``fixed_manifests`` 的键是反馈 ID：缺键 / ``None`` 表示首次冻结；显式 ``[]``
    表示本次固定无文档。清单相关的 Document 行同样在本函数的 Document 阶段加锁，
    保证不会在 Feedback 之后回头取下层锁。
    """
    if not freeze_manifests and fixed_manifests is not None:
        _fail(422, "invalid_batch", "manual 回归不能同时提供在线固定执行清单")
    fixed_by_feedback: dict[uuid.UUID, Any] = {}
    if fixed_manifests is not None:
        planned_ids = set(plan.feedback_ids)
        for key, value in fixed_manifests.items():
            try:
                identifier = _strict_uuid(key)
            except ValueError:
                _fail(422, "invalid_batch", "固定执行清单的反馈标识不合法")
            if identifier not in planned_ids:
                _fail(422, "invalid_batch", "固定执行清单包含本批次之外的反馈")
            # None 明确等同于未提供：首次冻结当前范围；[] 才固定为空清单。
            if value is not None:
                fixed_by_feedback[identifier] = value
    owner_ids = {item.owner_id for item in plan.items}
    users = _lock_users(db, admin_id, auth_token_version, owner_ids)

    kb_ids = {item.kb_id for item in plan.items if item.kb_id is not None}
    knowledge_bases = _lock_rows(db, KnowledgeBase, kb_ids)
    if set(knowledge_bases) != set(kb_ids):
        _fail(409, "source_invalid", "知识库不存在")

    project_snapshots, project_sources = _lock_project_roots(db, plan)

    conversations = _lock_rows(db, Conversation, {item.conversation_id for item in plan.items})
    if set(conversations) != {item.conversation_id for item in plan.items}:
        _fail(409, "feedback_stale", "原回答与会话信息不一致，反馈结果已过期")

    for item in plan.items:
        conversation = conversations[item.conversation_id]
        owner = users[item.owner_id]
        kb = knowledge_bases.get(item.kb_id)
        if conversation.user_id != item.owner_id \
                or conversation.kb_id != item.kb_id \
                or conversation.mode != item.mode \
                or conversation.project_snapshot_id != item.project_snapshot_id \
                or int(conversation.scope_revision or 0) != item.scope_revision \
                or _scope_entries(conversation.document_scope) != item.document_scope:
            _fail(409, "root_plan_changed", "会话根身份在等锁期间发生变化")
        if kb is not None and not kb_access.can_read_kb(db, kb, owner):
            _fail(409, "owner_access_revoked", "反馈所有者已无权访问该知识库")

    conversation_ids = {item.conversation_id for item in plan.items}
    with db.no_autoflush:
        turn_rows = db.scalars(
            select(ChatTurn)
            .where(ChatTurn.conversation_id.in_(conversation_ids))
            .order_by(ChatTurn.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
    turns_by_conversation: dict[uuid.UUID, list[ChatTurn]] = {identifier: [] for identifier in conversation_ids}
    for turn in turn_rows:
        turns_by_conversation.setdefault(turn.conversation_id, []).append(turn)

    message_ids = {item.message_id for item in plan.items}
    message_ids.update(item.question_message_id for item in plan.items if item.question_message_id is not None)
    message_ids.update(turn.user_message_id for turn in turn_rows)
    messages = _lock_rows(db, Message, message_ids)
    for item in plan.items:
        message = messages.get(item.message_id)
        if message is None or message.conversation_id != item.conversation_id:
            _fail(409, "feedback_stale", "原回答已不存在，反馈结果已过期")

    # Document 锁必须晚于 Message/ ChatTurn：先算出每个上传反馈的清单与范围。
    upload_manifests: dict[uuid.UUID, tuple[dict, ...]] = {}
    limited_scopes: dict[uuid.UUID, tuple[dict, ...]] = {}
    for item in plan.items:
        if item.kb_id is None:
            continue
        kb = knowledge_bases[item.kb_id]
        if kb.kind != KNOWLEDGE_BASE_KIND_UPLOAD:
            continue
        conversation = conversations[item.conversation_id]
        scope = _scope_entries(conversation.document_scope)
        if scope:
            try:
                canonical = document_scopes.validate_scope(db, kb.id, conversation.document_scope)
            except Exception:  # noqa: BLE001 - HTTPException 也收敛为稳定门禁码
                _fail(409, "scope_invalid", "会话限定文档范围已失效")
            limited_scopes[item.feedback_id] = tuple(
                {"document_id": entry["document_id"], "index_version": entry["index_version"]}
                for entry in canonical
            )
        if not freeze_manifests:
            # 旧 manual 回归：只保留限定范围，不枚举/冻结整库，也不套用在线 256 上限。
            continue
        fixed = fixed_by_feedback.get(item.feedback_id, _MISSING)
        upload_manifests[item.feedback_id] = _freeze_upload_manifest(
            db, kb, conversation, None if fixed is _MISSING else fixed,
            has_fixed=fixed is not _MISSING,
        )

    document_ids: set[uuid.UUID] = set()
    for item in plan.items:
        kb = knowledge_bases.get(item.kb_id) if item.kb_id is not None else None
        if kb is not None and kb.kind == KNOWLEDGE_BASE_KIND_UPLOAD:
            document_ids.update(item.expected_document_ids)
        manifest = upload_manifests.get(item.feedback_id)
        if manifest:
            document_ids.update(uuid.UUID(entry["document_id"]) for entry in manifest)
        scope = limited_scopes.get(item.feedback_id)
        if scope:
            document_ids.update(uuid.UUID(entry["document_id"]) for entry in scope)
    documents = _lock_documents(db, document_ids)
    _verify_upload_manifests(db, plan, upload_manifests, knowledge_bases, limited_scopes, documents)

    feedbacks = _lock_rows(db, AnswerFeedback, plan.feedback_ids)
    if set(feedbacks) != set(plan.feedback_ids):
        _fail(404, "feedback_not_found", "反馈不存在")
    for item in plan.items:
        feedback = feedbacks[item.feedback_id]
        if (
            feedback.user_id != item.owner_id
            or feedback.conversation_id != item.conversation_id
            or feedback.message_id != item.message_id
            or _annotation_fingerprint(feedback) != item.annotation_fingerprint
        ):
            _fail(409, "root_plan_changed", "反馈根身份在等锁期间发生变化")

    return LockedRoots(
        plan=plan,
        admin=users[admin_id],
        users=users,
        knowledge_bases=knowledge_bases,
        conversations=conversations,
        turns_by_conversation={key: tuple(value) for key, value in turns_by_conversation.items()},
        messages=messages,
        documents=documents,
        feedbacks=feedbacks,
        project_snapshots=project_snapshots,
        project_sources=project_sources,
        upload_manifests=upload_manifests,
        limited_scopes=limited_scopes,
        freeze_manifests=freeze_manifests,
    )


# ---------------------------------------------------------------------------
# 脱离 Session 的执行上下文
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionContext:
    feedback_id: str
    feedback_revision: int
    feedback_result_sha256: str
    feedback_status: str
    owner_id: str
    owner_role: str
    admin_id: str
    conversation_id: str
    message_id: str
    turn_id: str | None
    kb_id: str | None
    mode: str
    execution_kind: str
    case: dict
    turns: tuple[str, ...]
    resolved_sources: tuple[dict, ...]
    upload_manifest: tuple[dict, ...] | None
    limited_scope: tuple[dict, ...] | None
    project: dict | None
    root_sha256: str
    constructed_at: str

    @property
    def has_fixed_execution(self) -> bool:
        """旧 live CLI 无法承载的固定范围/项目组合。"""
        return self.execution_kind == _EXECUTION_PROJECT or bool(self.limited_scope)

    def to_dict(self) -> dict:
        """完整临时执行上下文（含原问题序列 case/turns），**仅限当前执行使用**。

        该字典包含原问题与临时 case，不是持久作业载荷：不得整体写入作业表/摘要。
        未来持久作业只保存 ``summary()`` 的 ID/版本/指纹与配置白名单。
        """
        return {
            "feedback_id": self.feedback_id,
            "feedback_revision": self.feedback_revision,
            "feedback_result_sha256": self.feedback_result_sha256,
            "feedback_status": self.feedback_status,
            "owner_id": self.owner_id,
            "owner_role": self.owner_role,
            "admin_id": self.admin_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "kb_id": self.kb_id,
            "mode": self.mode,
            "execution_kind": self.execution_kind,
            "case": self.case,
            "turns": list(self.turns),
            "resolved_sources": [dict(item) for item in self.resolved_sources],
            "upload_manifest": None if self.upload_manifest is None else [dict(item) for item in self.upload_manifest],
            "limited_scope": None if self.limited_scope is None else [dict(item) for item in self.limited_scope],
            "project": None if self.project is None else dict(self.project),
            "root_sha256": self.root_sha256,
            "constructed_at": self.constructed_at,
        }

    def summary(self) -> dict:
        """持久作业可保存的白名单摘要：不含原问题、临时 case、正文或引用片段。

        只含 ID、版本、执行类型、固定范围/项目身份、来源身份与摘要。来源身份与
        ``resolved_sources`` 相同字段集，但不含任何正文/snippet。
        """
        return {
            "feedback_id": self.feedback_id,
            "feedback_revision": self.feedback_revision,
            "feedback_result_sha256": self.feedback_result_sha256,
            "owner_id": self.owner_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
            "turn_id": self.turn_id,
            "kb_id": self.kb_id,
            "mode": self.mode,
            "execution_kind": self.execution_kind,
            "upload_manifest": None if self.upload_manifest is None else [dict(item) for item in self.upload_manifest],
            "limited_scope": None if self.limited_scope is None else [dict(item) for item in self.limited_scope],
            "project": None if self.project is None else {
                key: self.project.get(key) for key in _PROJECT_IDENTITY_KEYS
            },
            "resolved_sources": [
                {key: value for key, value in item.items()
                 if key not in ("snippet", "content", "score")}
                for item in self.resolved_sources
            ],
            "root_sha256": self.root_sha256,
            "constructed_at": self.constructed_at,
        }


@dataclass(frozen=True)
class BatchExecutionContext:
    contexts: tuple[ExecutionContext, ...]

    def by_feedback(self) -> dict[uuid.UUID, ExecutionContext]:
        return {uuid.UUID(context.feedback_id): context for context in self.contexts}


def _project_identity(snapshot: ProjectSnapshot, source: ProjectSource) -> dict:
    return {
        "snapshot_id": str(snapshot.id),
        "source_id": str(snapshot.source_id),
        "manifest_fingerprint": snapshot.manifest_fingerprint,
        "config_fingerprint": snapshot.config_fingerprint,
    }


def _validate_project_expected(db, kb: KnowledgeBase, snapshot: ProjectSnapshot,
                               sources: Sequence[Mapping]) -> None:
    """真实校验项目期望来源：快照映射、源、文件版本、配置/清单与具体 chunk。"""
    if not sources:
        return
    snapshot_id = uuid.UUID(str(snapshot.id))
    validation_sources: list[dict] = []
    for source in sources:
        if source.get("source_kind") != answer_sources.SOURCE_KIND_PROJECT:
            _fail(409, "source_invalid", "项目会话的期望来源必须是项目来源")
        file_version_id = uuid.UUID(str(source.get("file_version_id") or source.get("document_id")))
        if str(source.get("snapshot_id")) != str(snapshot_id):
            _fail(409, "source_invalid", "期望来源不属于会话固定的项目快照")
        if str(source.get("source_id")) != str(snapshot.source_id):
            _fail(409, "source_invalid", "期望来源与项目快照不同源")
        if source.get("manifest_fingerprint") != snapshot.manifest_fingerprint:
            _fail(409, "source_invalid", "期望来源 manifest 指纹不一致")
        if source.get("config_fingerprint") != snapshot.config_fingerprint:
            _fail(409, "source_invalid", "期望来源 config 指纹不一致")
        chunk_index = source.get("chunk_index")
        if chunk_index is None:
            with db.no_autoflush:
                chunk_index = db.scalar(
                    select(ProjectChunk.chunk_index)
                    .where(ProjectChunk.file_version_id == file_version_id,
                           ProjectChunk.embedded.is_(True))
                    .order_by(ProjectChunk.chunk_index)
                    .limit(1)
                )
            if chunk_index is None:
                _fail(409, "chunk_missing", "期望来源没有可用的实际分块")
        if type(chunk_index) is not int or chunk_index < 0:
            _fail(409, "source_invalid", "期望来源片段编号不合法")
        validation_sources.append({
            "source_kind": answer_sources.SOURCE_KIND_PROJECT,
            "document_id": str(file_version_id),
            "file_version_id": str(file_version_id),
            "snapshot_id": str(snapshot_id),
            "source_id": str(snapshot.source_id),
            "relative_path": source.get("relative_path"),
            "manifest_fingerprint": source.get("manifest_fingerprint"),
            "config_fingerprint": source.get("config_fingerprint"),
            "chunk_index": chunk_index,
            "chunk_start": chunk_index,
            "chunk_end": chunk_index,
        })
    try:
        parsed = answer_sources.parse_sources(validation_sources)
    except answer_sources.SourceSnapshotError as exc:
        _fail(409, "source_invalid", str(exc))
    # parse 只验证规范结构；真实映射/分块在此逐项校验（不读上传 Document）。
    with db.no_autoflush:
        for source in parsed:
            row = db.execute(
                select(ProjectSnapshotFile.snapshot_id,
                       ProjectSnapshotFile.relative_path,
                       ProjectFileVersion.source_id,
                       ProjectFileVersion.relative_path,
                       ProjectFileVersion.parse_status)
                .join(ProjectFileVersion,
                      ProjectFileVersion.id == ProjectSnapshotFile.file_version_id)
                .where(ProjectSnapshotFile.snapshot_id == snapshot_id,
                       ProjectSnapshotFile.file_version_id == uuid.UUID(source["file_version_id"]))
            ).first()
            if row is None:
                _fail(409, "source_invalid", "期望来源文件版本不在项目快照映射中")
            _, mapped_path, source_id, relative_path, parse_status = row
            if source_id != snapshot.source_id:
                _fail(409, "source_invalid", "期望来源文件版本与快照不同源")
            if mapped_path != relative_path or relative_path != source["relative_path"]:
                _fail(409, "source_invalid", "期望来源路径与快照映射不一致")
            if parse_status not in _PROJECT_READY_STATUSES:
                _fail(409, "source_invalid", "期望来源文件尚未解析完成")
            exists = db.scalar(
                select(ProjectChunk.id)
                .where(ProjectChunk.file_version_id == uuid.UUID(source["file_version_id"]),
                       ProjectChunk.chunk_index == source["chunk_index"],
                       ProjectChunk.embedded.is_(True))
                .limit(1)
            )
            if exists is None:
                _fail(409, "chunk_missing", "期望来源片段已不可用")


def _verify_fixed_project(fixed: Mapping, snapshot: ProjectSnapshot) -> None:
    actual_values = {
        "snapshot_id": snapshot.id,
        "source_id": snapshot.source_id,
        "manifest_fingerprint": snapshot.manifest_fingerprint,
        "config_fingerprint": snapshot.config_fingerprint,
    }
    for key in _PROJECT_IDENTITY_KEYS:
        expected = fixed.get(key)
        actual = actual_values[key]
        if expected is None or str(expected) != str(actual):
            _fail(409, "project_snapshot_changed", "项目快照身份与入队时冻结值不一致")


def _context_from_locked(
    db,
    locked: LockedRoots,
    item: RootPlanItem,
    *,
    fixed_projects: Mapping[uuid.UUID, Mapping] | None,
) -> ExecutionContext:
    feedback = locked.feedbacks[item.feedback_id]
    conversation = locked.conversations[item.conversation_id]
    message = locked.messages[item.message_id]
    owner = locked.users[item.owner_id]
    kb = locked.knowledge_bases.get(item.kb_id)
    project_snapshot = None
    project_source = None
    execution_kind = _EXECUTION_CHAT
    limited_scope = None
    upload_manifest = None

    if kb is not None and kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        execution_kind = _EXECUTION_PROJECT
        project_snapshot = locked.project_snapshots.get(item.project_snapshot_id)
        if project_snapshot is None:
            _fail(409, "project_snapshot_invalid", "项目快照已不可用")
        project_source = locked.project_sources.get(project_snapshot.source_id)
        if project_source is None:
            _fail(409, "project_snapshot_invalid", "项目快照缺少项目源")
        if fixed_projects and item.feedback_id in fixed_projects:
            _verify_fixed_project(fixed_projects[item.feedback_id], project_snapshot)
    elif kb is not None and kb.kind == KNOWLEDGE_BASE_KIND_UPLOAD:
        execution_kind = _EXECUTION_UPLOAD
        limited_scope = locked.limited_scopes.get(item.feedback_id)
        if locked.freeze_manifests:
            upload_manifest = locked.upload_manifests.get(item.feedback_id, ())
        else:
            # 旧 manual 回归没有在线执行清单：来源有效性由标注解析/锁定行直接校验。
            upload_manifest = None

    try:
        case = build_base_case(
            db, locked.admin, feedback, message, conversation, cause="", owner=owner
        )
    except RegressionUnavailable as exc:
        _fail(409, str(exc.reason), str(exc.message))

    resolved_sources = tuple(dict(source) for source in case.get("expected_sources") or [])
    if execution_kind == _EXECUTION_PROJECT:
        if project_snapshot is None or project_source is None:
            _fail(409, "project_snapshot_invalid", "项目快照已不可用")
        _validate_project_expected(db, kb, project_snapshot, resolved_sources)
        project = _project_identity(project_snapshot, project_source)
        project["expected_sources"] = [dict(source) for source in resolved_sources]
    else:
        project = None
        if execution_kind == _EXECUTION_UPLOAD and upload_manifest is not None:
            allowed = {(entry["document_id"], entry["index_version"]) for entry in upload_manifest}
            for source in resolved_sources:
                if (str(source.get("document_id")), source.get("index_version")) not in allowed:
                    _fail(409, "source_invalid", "期望来源不在本次固定执行清单内")

    turns = tuple(case.get("turns") or ([case["question"]] if case.get("question") else []))
    root_payload = {
        "feedback_id": str(feedback.id),
        "feedback_revision": feedback.revision,
        "feedback_result_sha256": feedback.result_sha256,
        "owner_id": str(feedback.user_id),
        "conversation_id": str(conversation.id),
        "message_id": str(message.id),
        "kb_id": None if kb is None else str(kb.id),
        "mode": conversation.mode,
        "execution_kind": execution_kind,
        "upload_manifest": None if upload_manifest is None else list(upload_manifest),
        "limited_scope": None if limited_scope is None else list(limited_scope),
        "project": None if project is None else {key: project[key] for key in _PROJECT_IDENTITY_KEYS},
        "case_sha256": _sha256(case),
    }
    return ExecutionContext(
        feedback_id=str(feedback.id),
        feedback_revision=int(feedback.revision),
        feedback_result_sha256=feedback.result_sha256,
        feedback_status=feedback.status,
        owner_id=str(feedback.user_id),
        owner_role=owner.role,
        admin_id=str(locked.admin.id),
        conversation_id=str(conversation.id),
        message_id=str(message.id),
        turn_id=str(message.turn_id) if message.turn_id is not None else None,
        kb_id=None if kb is None else str(kb.id),
        mode=conversation.mode,
        execution_kind=execution_kind,
        case=case,
        turns=turns,
        resolved_sources=resolved_sources,
        upload_manifest=upload_manifest,
        limited_scope=limited_scope,
        project=project,
        root_sha256=_sha256(root_payload),
        constructed_at=datetime.now(timezone.utc).isoformat(),
    )


def build_contexts(
    db,
    locked: LockedRoots,
    *,
    fixed_projects: Mapping[uuid.UUID | str, Mapping] | None = None,
) -> BatchExecutionContext:
    """在已锁定的根上构建脱离 Session 的执行上下文。

    ``fixed_projects`` 用于作业重放时复核入队时冻结的快照/源/manifest/config。
    上传固定清单由 ``lock_roots(..., fixed_manifests=...)`` 在 Document 阶段处理，
    因此这里绝不再获取下层锁。
    """
    projects = None
    if fixed_projects is not None:
        projects = {}
        planned_ids = set(locked.plan.feedback_ids)
        for key, value in fixed_projects.items():
            try:
                identifier = _strict_uuid(key)
            except ValueError:
                _fail(422, "invalid_batch", "固定项目绑定的反馈标识不合法")
            if identifier not in planned_ids or not isinstance(value, Mapping):
                _fail(422, "invalid_batch", "固定项目绑定必须匹配本批次反馈并提供对象")
            projects[identifier] = value
    return BatchExecutionContext(
        contexts=tuple(
            _context_from_locked(db, locked, item, fixed_projects=projects)
            for item in locked.plan.items
        )
    )


def build_batch_contexts(
    db,
    *,
    admin_id: uuid.UUID,
    auth_token_version: int,
    feedback_ids: Iterable[Any],
    fixed_manifests: Mapping[uuid.UUID | str, Sequence | None] | None = None,
    fixed_projects: Mapping[uuid.UUID | str, Mapping] | None = None,
) -> BatchExecutionContext:
    """规划 -> 加锁 -> 构建上下文的一站式入口（供 R1b/R2 在线执行复用）。

    默认 ``freeze_manifests=True``：冻结本次在线执行清单并硬限 256 项。旧 manual
    回归服务不调用本函数，而是用 ``lock_roots(..., freeze_manifests=False)`` 只锁
    标注来源与非空限定范围，避免整库规模影响兼容路径。
    """
    plan = plan_roots(db, feedback_ids)
    locked = lock_roots(
        db, plan, admin_id=admin_id, auth_token_version=auth_token_version,
        fixed_manifests=fixed_manifests,
    )
    return build_contexts(db, locked, fixed_projects=fixed_projects)


# ---------------------------------------------------------------------------
# 只读可用性探测（GET/历史展示用；不加根锁）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _InspectRows:
    feedback: AnswerFeedback
    conversation: Conversation
    message: Message
    owner: User
    kb: KnowledgeBase | None
    snapshot: ProjectSnapshot | None
    source: ProjectSource | None


def _inspect_rows(db, feedback_id: uuid.UUID) -> _InspectRows:
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    conversation = db.get(Conversation, feedback.conversation_id)
    message = db.get(Message, feedback.message_id)
    if conversation is None or message is None or message.conversation_id != conversation.id:
        _fail(409, "feedback_stale", "原回答与会话信息不一致，反馈结果已过期")
    owner = db.get(User, feedback.user_id)
    if owner is None or not owner.is_active:
        _fail(409, "owner_inactive", "反馈所有者已禁用，不能按原权限重跑")
    kb = db.get(KnowledgeBase, conversation.kb_id) if conversation.kb_id is not None else None
    if conversation.kb_id is not None and kb is None:
        _fail(409, "source_invalid", "知识库不存在")
    if kb is not None and not kb_access.can_read_kb(db, kb, owner):
        _fail(409, "owner_access_revoked", "反馈所有者已无权访问该知识库")
    snapshot = None
    source = None
    if conversation.project_snapshot_id is not None:
        snapshot = db.get(ProjectSnapshot, conversation.project_snapshot_id)
        if snapshot is None:
            _fail(409, "project_snapshot_invalid", "项目快照已不可用")
        source = db.get(ProjectSource, snapshot.source_id)
        if source is None or source.revoked_at is not None or source.kb_id != conversation.kb_id:
            _fail(409, "project_snapshot_invalid", "项目源已撤销或不属于当前知识库")
        if snapshot.status != ProjectSnapshotStatus.READY.value:
            _fail(409, "project_snapshot_invalid", "项目快照未就绪")
        if not snapshot.manifest_fingerprint or not snapshot.config_fingerprint:
            _fail(409, "project_snapshot_invalid", "项目快照缺少 manifest/config 指纹")
    return _InspectRows(feedback, conversation, message, owner, kb, snapshot, source)


def inspect_context(db, feedback_id: uuid.UUID) -> ExecutionContext:
    """只读构建 manual 回归可用性上下文（不加锁、不复核管理员 token）。

    与旧 manual prepare/export/import 一致：不枚举/冻结整库在线执行清单，也不套用
    256 项上限；上传来源有效性仍由标注解析与锁定行逐一校验。GET/历史展示使用。
    """
    identifier = _strict_uuid(feedback_id)
    rows = _inspect_rows(db, identifier)
    conversation = rows.conversation
    kb = rows.kb
    execution_kind = _EXECUTION_CHAT
    upload_manifest = None
    limited_scope = None
    project = None
    if kb is not None and kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        execution_kind = _EXECUTION_PROJECT
    elif kb is not None and kb.kind == KNOWLEDGE_BASE_KIND_UPLOAD:
        execution_kind = _EXECUTION_UPLOAD
        scope = _scope_entries(conversation.document_scope)
        if scope:
            try:
                canonical = document_scopes.validate_scope(db, kb.id, conversation.document_scope)
            except Exception:  # noqa: BLE001
                _fail(409, "scope_invalid", "会话限定文档范围已失效")
            limited_scope = tuple(
                {"document_id": entry["document_id"], "index_version": entry["index_version"]}
                for entry in canonical
            )
    try:
        case = build_base_case(
            db, None, rows.feedback, rows.message, conversation, cause="", owner=rows.owner
        )
    except RegressionUnavailable as exc:
        _fail(409, str(exc.reason), str(exc.message))
    resolved_sources = tuple(dict(source) for source in case.get("expected_sources") or [])
    if execution_kind == _EXECUTION_PROJECT:
        _validate_project_expected(db, kb, rows.snapshot, resolved_sources)
        project = _project_identity(rows.snapshot, rows.source)
        project["expected_sources"] = [dict(source) for source in resolved_sources]
    turns = tuple(case.get("turns") or ([case["question"]] if case.get("question") else []))
    feedback = rows.feedback
    return ExecutionContext(
        feedback_id=str(feedback.id),
        feedback_revision=int(feedback.revision),
        feedback_result_sha256=feedback.result_sha256,
        feedback_status=feedback.status,
        owner_id=str(feedback.user_id),
        owner_role=rows.owner.role,
        admin_id="",
        conversation_id=str(conversation.id),
        message_id=str(rows.message.id),
        turn_id=str(rows.message.turn_id) if rows.message.turn_id is not None else None,
        kb_id=None if kb is None else str(kb.id),
        mode=conversation.mode,
        execution_kind=execution_kind,
        case=case,
        turns=turns,
        resolved_sources=resolved_sources,
        upload_manifest=upload_manifest,
        limited_scope=limited_scope,
        project=project,
        root_sha256="",
        constructed_at=datetime.now(timezone.utc).isoformat(),
    )


__all__ = [
    "MAX_BATCH_FEEDBACKS",
    "MAX_EXECUTION_MANIFEST",
    "BatchExecutionContext",
    "BatchRootPlan",
    "ExecutionContext",
    "LockedRoots",
    "RegressionContextError",
    "RootPlanItem",
    "build_batch_contexts",
    "build_contexts",
    "inspect_context",
    "lock_roots",
    "plan_roots",
]