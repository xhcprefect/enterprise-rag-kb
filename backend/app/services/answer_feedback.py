"""答案反馈服务：结果指纹、提交幂等、管理员核实与评测 case 导出。

本模块只做业务编排；HTTP 错误直接以 ``HTTPException`` 抛出（与项目内其它
API-facing 服务一致）。不写任何服务器数据集文件，也不触发评测运行。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.models import (
    ChatTurn,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    ProjectFileVersion,
    ProjectSnapshotFile,
    User,
)
from app.models.feedback import (
    FEEDBACK_STATUS_PENDING,
    FEEDBACK_STATUS_VERIFIED,
    AnswerFeedback,
)
from app.schemas.feedback import FeedbackCreate, FeedbackReviewRequest
from app.services import document_lifecycle, project_chat_scope
from app.services.evaluation import (
    EvaluationError,
    dataset_identity,
    review_state,
    validate_cases,
)

_CITATION_MAX = 50
_EXPORT_MAX = 500
_VERIFIED_BY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_EXPORT_TURNS_LIMIT = 10
_EXPORT_TURNS_OVERFLOW = "多轮上下文超过导出上限，请人工整理"
_SOURCE_INVALID = "期望来源已失效或无权访问"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_str(value) -> str | None:
    return None if value is None else str(value)


def question_for_message(db: DBSession, message: Message) -> str | None:
    """从 ``reply_to_id`` 指向的原消息读取提问；无法解析时返回 None。"""
    if message.reply_to_id is None:
        return None
    question = db.get(Message, message.reply_to_id)
    if question is None or question.role != "user":
        return None
    if question.conversation_id != message.conversation_id:
        return None
    return question.content


def _canonical_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def result_fingerprint(db: DBSession, message: Message) -> str:
    """冻结的结果指纹：覆盖回答归属、正文、引用与提问/会话身份。"""
    conversation = db.get(Conversation, message.conversation_id)
    payload = {
        "message_id": _uuid_str(message.id),
        "conversation_id": _uuid_str(message.conversation_id),
        "turn_id": _uuid_str(message.turn_id),
        "reply_to_id": _uuid_str(message.reply_to_id),
        "role": message.role,
        "status": message.status,
        "content": message.content,
        "citations": message.citations or [],
        "question": question_for_message(db, message),
        "mode": conversation.mode if conversation is not None else None,
        "kb_id": _uuid_str(conversation.kb_id) if conversation is not None else None,
        "project_snapshot_id": (
            _uuid_str(conversation.project_snapshot_id) if conversation is not None else None
        ),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 提交
# ---------------------------------------------------------------------------


def _validate_feedback_target(db: DBSession, user: User, message_id: uuid.UUID) -> tuple[Message, Conversation]:
    message = db.get(Message, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="回答不存在")
    if message.role != "assistant":
        raise HTTPException(status_code=400, detail="只能对助手回答提交反馈")
    if message.status != "completed" or not message.content.strip():
        raise HTTPException(status_code=409, detail="仅已完成的回答可反馈")
    if message.reply_to_id is None:
        raise HTTPException(status_code=409, detail="无法定位该回答对应的提问")
    question = db.get(Message, message.reply_to_id)
    if (
        question is None
        or question.role != "user"
        or question.conversation_id != message.conversation_id
    ):
        raise HTTPException(status_code=409, detail="无法定位该回答对应的提问")
    if message.turn_id is not None:
        turn = db.get(ChatTurn, message.turn_id)
        if (
            turn is None
            or turn.user_message_id != message.reply_to_id
            or turn.conversation_id != message.conversation_id
        ):
            raise HTTPException(status_code=409, detail="回答与提问的轮次信息不一致")
    conversation = db.get(Conversation, message.conversation_id)
    # 只有会话所有者能提交；管理员也不能替他人提交。
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status_code=404, detail="回答不存在")
    kb = None
    if conversation.kb_id is not None:
        kb = db.get(KnowledgeBase, conversation.kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
        if not user.is_admin and not kb.is_public and kb.owner_id != user.id:
            raise HTTPException(status_code=403, detail="无权访问该知识库")
    if conversation.mode == "rag" and kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb is not None and kb.kind == "project" and conversation.project_snapshot_id is None:
        raise HTTPException(status_code=409, detail="项目快照已不可用")
    if conversation.project_snapshot_id is not None:
        # 原样传播 401/404/409/410。
        project_chat_scope.replay_project_check(db, user.id, conversation.project_snapshot_id)
    return message, conversation


def create_feedback(db: DBSession, user: User, payload: FeedbackCreate) -> AnswerFeedback:
    message, conversation = _validate_feedback_target(db, user, payload.message_id)
    user_id, message_id, conversation_id = user.id, message.id, conversation.id
    existing = db.scalar(
        select(AnswerFeedback).where(
            AnswerFeedback.user_id == user_id,
            AnswerFeedback.message_id == message_id,
        )
    )
    if existing is not None:
        return existing
    feedback = AnswerFeedback(
        user_id=user_id,
        message_id=message_id,
        conversation_id=conversation_id,
        category=payload.category,
        note=payload.note,
        status=FEEDBACK_STATUS_PENDING,
        revision=1,
        result_sha256=result_fingerprint(db, message),
    )
    db.add(feedback)
    try:
        db.commit()
    except IntegrityError:
        # 并发提交同一 (user_id, message_id)：回滚后按唯一键重查，返回既有记录。
        db.rollback()
        existing = db.scalar(
            select(AnswerFeedback).where(
                AnswerFeedback.user_id == user_id,
                AnswerFeedback.message_id == message_id,
            )
        )
        if existing is None:
            raise
        return existing
    db.refresh(feedback)
    return feedback


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------


def _page(db: DBSession, stmt, page: int, page_size: int) -> tuple[int, list[AnswerFeedback]]:
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AnswerFeedback.created_at.desc(), AnswerFeedback.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return int(total), list(rows)


def list_mine(
    db: DBSession,
    user: User,
    *,
    page: int,
    page_size: int,
    status: str | None = None,
) -> tuple[int, list[AnswerFeedback]]:
    stmt = select(AnswerFeedback).where(AnswerFeedback.user_id == user.id)
    if status is not None:
        stmt = stmt.where(AnswerFeedback.status == status)
    return _page(db, stmt, page, page_size)


def list_admin(
    db: DBSession,
    *,
    page: int,
    page_size: int,
    status: str | None = None,
    category: str | None = None,
) -> tuple[int, list[AnswerFeedback]]:
    stmt = select(AnswerFeedback)
    if status is not None:
        stmt = stmt.where(AnswerFeedback.status == status)
    if category is not None:
        stmt = stmt.where(AnswerFeedback.category == category)
    return _page(db, stmt, page, page_size)


def admin_item(feedback: AnswerFeedback) -> dict:
    return {
        "id": feedback.id,
        "category": feedback.category,
        "status": feedback.status,
        "revision": feedback.revision,
        "created_at": feedback.created_at,
        "reviewed_at": feedback.reviewed_at,
        "reviewer_name": feedback.reviewer_name,
        "note_present": bool((feedback.note or "").strip()),
    }


# ---------------------------------------------------------------------------
# 详情
# ---------------------------------------------------------------------------


def sanitize_citations(raw) -> list[dict]:
    """只保留冻结白名单键，最多 50 条；未知键/非法值一律丢弃。"""
    if not isinstance(raw, list):
        return []
    clean: list[dict] = []
    for item in raw[:_CITATION_MAX]:
        if not isinstance(item, dict):
            continue
        value: dict = {}
        document_id = item.get("document_id")
        if document_id is not None:
            value["document_id"] = str(document_id)
        filename = item.get("filename")
        if isinstance(filename, str):
            value["filename"] = filename
        chunk_index = item.get("chunk_index")
        if type(chunk_index) is int and chunk_index >= 0:
            value["chunk_index"] = chunk_index
        index_version = item.get("index_version")
        if type(index_version) is int and index_version >= 0:
            value["index_version"] = index_version
        snippet = item.get("snippet")
        if isinstance(snippet, str):
            value["snippet"] = snippet
        if value:
            clean.append(value)
    return clean


def _invalid_source_ids(
    db: DBSession,
    citations: list[dict],
    *,
    kb: KnowledgeBase | None,
    snapshot_id: uuid.UUID | None,
) -> list[str]:
    """引用里的 document_id 是否仍能对应到有效来源。

    项目库的 ``document_id`` 是不可变 file_version_id，只能按
    ``(snapshot_id, file_version_id)`` 的 ProjectSnapshotFile 映射验证；
    上传库继续按 Document + is_retrievable 判定。
    """
    project = kb is not None and kb.kind == "project"
    invalid: list[str] = []
    for citation in citations:
        raw = citation.get("document_id")
        if raw is None:
            continue
        try:
            document_id = uuid.UUID(str(raw))
        except (ValueError, TypeError, AttributeError):
            invalid.append(str(raw))
            continue
        if project:
            if snapshot_id is None:
                invalid.append(str(document_id))
                continue
            mapped = db.scalar(
                select(ProjectSnapshotFile.snapshot_id)
                .where(
                    ProjectSnapshotFile.snapshot_id == snapshot_id,
                    ProjectSnapshotFile.file_version_id == document_id,
                )
                .limit(1)
            )
            if mapped is None:
                invalid.append(str(document_id))
        else:
            document = db.get(Document, document_id)
            if document is None or not document_lifecycle.is_retrievable(document):
                invalid.append(str(document_id))
    # 去重且保持首次出现顺序。
    return list(dict.fromkeys(invalid))


def feedback_stale(db: DBSession, feedback: AnswerFeedback, message: Message) -> bool:
    """问题不可解析或结果指纹已变化即视为过期。"""
    if question_for_message(db, message) is None:
        return True
    return result_fingerprint(db, message) != feedback.result_sha256


def get_detail(db: DBSession, admin: User, feedback_id: uuid.UUID) -> dict:
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="反馈不存在")
    message = db.get(Message, feedback.message_id)
    conversation = db.get(Conversation, feedback.conversation_id)
    if message is None or conversation is None:
        raise HTTPException(status_code=404, detail="原回答已不存在")
    kb = None
    if conversation.kb_id is not None:
        kb = db.get(KnowledgeBase, conversation.kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
    if conversation.mode == "rag" and kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb is not None and kb.kind == "project" and conversation.project_snapshot_id is None:
        raise HTTPException(status_code=409, detail="项目快照已不可用")
    if conversation.project_snapshot_id is not None:
        project_chat_scope.replay_project_check(db, admin.id, conversation.project_snapshot_id)
    question = question_for_message(db, message)
    citations = sanitize_citations(message.citations)
    return {
        "id": feedback.id,
        "category": feedback.category,
        "status": feedback.status,
        "mode": conversation.mode,
        "revision": feedback.revision,
        "created_at": feedback.created_at,
        "reviewed_at": feedback.reviewed_at,
        "reviewer_name": feedback.reviewer_name,
        "message_id": feedback.message_id,
        "conversation_id": feedback.conversation_id,
        "question": question,
        "answer": message.content,
        "note": feedback.note,
        "citations": citations,
        "invalid_source_ids": _invalid_source_ids(
            db, citations, kb=kb, snapshot_id=conversation.project_snapshot_id
        ),
        "result_stale": question is None or result_fingerprint(db, message) != feedback.result_sha256,
        "corrected_outcome": feedback.corrected_outcome,
        "expected_sources": list(feedback.expected_sources or []),
        "required_phrases": list(feedback.required_phrases or []),
        "forbidden_phrases": list(feedback.forbidden_phrases or []),
        "unknowns": list(feedback.unknowns or []),
        "evidence_note": feedback.evidence_note,
        "reviewer_note": feedback.reviewer_note,
    }


# ---------------------------------------------------------------------------
# 核实 / 驳回
# ---------------------------------------------------------------------------


def _normalize_phrases(values: list[str], label: str) -> list[str]:
    if len(values) > 20:
        raise HTTPException(status_code=400, detail=f"{label}最多 20 条")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = item.strip()
        if not 1 <= len(text) <= 200:
            raise HTTPException(status_code=400, detail=f"{label}每条需为 1–200 个字符")
        if text not in seen:
            seen.add(text)
            normalized.append(text)
    return normalized


def _normalize_unknowns(values: list[str]) -> list[str]:
    if len(values) > 20:
        raise HTTPException(status_code=400, detail="未解决问题最多 20 条")
    normalized: list[str] = []
    for item in values:
        text = item.strip()
        if len(text) > 200:
            raise HTTPException(status_code=400, detail="未解决问题每条最多 200 个字符")
        normalized.append(text)
    return normalized


def _normalize_sources(sources) -> list[dict]:
    if len(sources) > 20:
        raise HTTPException(status_code=400, detail="期望来源最多 20 条")
    normalized: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        item: dict = {}
        if source.document_id is not None:
            item["document_id"] = str(source.document_id)
        if source.filename:
            item["filename"] = source.filename
        if source.chunk_index is not None:
            item["chunk_index"] = source.chunk_index
        if source.index_version is not None:
            item["index_version"] = source.index_version
        key = _canonical_json(item)
        if key not in seen:
            seen.add(key)
            normalized.append(item)
    return normalized


def review_feedback(
    db: DBSession, admin: User, feedback_id: uuid.UUID, payload: FeedbackReviewRequest
) -> AnswerFeedback:
    # 行锁读取，保证并发核实只有一个请求成功。
    feedback = db.scalar(
        select(AnswerFeedback)
        .where(AnswerFeedback.id == feedback_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if feedback is None:
        raise HTTPException(status_code=404, detail="反馈不存在")
    if feedback.revision != payload.expected_revision:
        raise HTTPException(status_code=409, detail="反馈已被他人处理，请刷新")
    if feedback.status != FEEDBACK_STATUS_PENDING:
        raise HTTPException(status_code=409, detail="反馈已被他人处理，请刷新")
    message = db.get(Message, feedback.message_id)
    conversation = db.get(Conversation, feedback.conversation_id)
    if message is None or conversation is None:
        raise HTTPException(status_code=409, detail="原回答已不存在，无法核实")
    if feedback_stale(db, feedback, message):
        raise HTTPException(status_code=409, detail="原回答已被修改，反馈结果已过期")

    sources = _normalize_sources(payload.expected_sources)
    required = _normalize_phrases(payload.required_phrases, "必须出现的短语")
    forbidden = _normalize_phrases(payload.forbidden_phrases, "禁止出现的短语")
    unknowns = _normalize_unknowns(payload.unknowns)

    if payload.decision == FEEDBACK_STATUS_VERIFIED:
        if payload.corrected_outcome is None:
            raise HTTPException(status_code=400, detail="核实必须填写纠正后的结果")
        if not payload.evidence_note.strip():
            raise HTTPException(status_code=400, detail="核实必须填写证据说明")
        if unknowns:
            raise HTTPException(status_code=400, detail="核实时不能保留未解决的问题")
        if conversation.mode == "rag" and payload.corrected_outcome == "answer" and not sources:
            raise HTTPException(status_code=400, detail="知识库回答的核实至少需要一条期望来源")
        if conversation.mode != "rag" and sources:
            raise HTTPException(status_code=400, detail="普通对话的核实不能附带期望来源")
        feedback.corrected_outcome = payload.corrected_outcome
        feedback.expected_sources = sources
        feedback.required_phrases = required
        feedback.forbidden_phrases = forbidden
        feedback.unknowns = unknowns
    else:
        if not payload.reviewer_note.strip():
            raise HTTPException(status_code=400, detail="驳回必须填写核验说明")
        # 驳回不保留任何纠正标注，只保留说明文本。
        feedback.corrected_outcome = None
        feedback.expected_sources = []
        feedback.required_phrases = []
        feedback.forbidden_phrases = []
        feedback.unknowns = []

    feedback.evidence_note = payload.evidence_note
    feedback.reviewer_note = payload.reviewer_note
    feedback.status = payload.decision
    feedback.revision += 1
    feedback.reviewer_id = admin.id
    feedback.reviewer_name = admin.username
    feedback.reviewed_at = utc_now()
    db.commit()
    db.refresh(feedback)
    return feedback


# ---------------------------------------------------------------------------
# 显式导出
# ---------------------------------------------------------------------------


def _stored_sources(values) -> list[dict]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
    sources: list[dict] = []
    for item in values:
        if not isinstance(item, dict):
            raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
        source: dict = {}
        if item.get("document_id") is not None:
            source["document_id"] = str(item["document_id"])
        if item.get("filename") is not None:
            if not isinstance(item["filename"], str):
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            source["filename"] = item["filename"]
        if item.get("chunk_index") is not None:
            value = item["chunk_index"]
            if type(value) is not int or value < 0:
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            source["chunk_index"] = value
        if item.get("index_version") is not None:
            value = item["index_version"]
            if type(value) is not int or value < 0:
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            source["index_version"] = value
        if not source.get("document_id") and not source.get("filename"):
            raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
        sources.append(source)
    return sources


def stored_sources(values) -> list[dict]:
    """公开薄入口：供回归适配层复用同一份来源标注规范化规则。"""
    return _stored_sources(values)


def _verify_expected_sources(
    db: DBSession,
    sources: list[dict],
    kb: KnowledgeBase | None,
    snapshot_id: uuid.UUID | None,
) -> None:
    """导出前验证标注来源仍有效；项目库按快照映射，上传库按 Document 判定。"""
    project = kb is not None and kb.kind == "project"
    for source in sources:
        document_id = source.get("document_id")
        filename = source.get("filename")
        if project:
            if snapshot_id is None:
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            if document_id:
                try:
                    identity = uuid.UUID(str(document_id))
                except (ValueError, TypeError, AttributeError):
                    raise HTTPException(status_code=409, detail=_SOURCE_INVALID) from None
                mapped = db.scalar(
                    select(ProjectSnapshotFile.snapshot_id)
                    .where(
                        ProjectSnapshotFile.snapshot_id == snapshot_id,
                        ProjectSnapshotFile.file_version_id == identity,
                    )
                    .limit(1)
                )
                if mapped is None:
                    raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            elif filename:
                mapped = db.scalar(
                    select(ProjectSnapshotFile.snapshot_id)
                    .join(
                        ProjectFileVersion,
                        ProjectFileVersion.id == ProjectSnapshotFile.file_version_id,
                    )
                    .where(
                        ProjectSnapshotFile.snapshot_id == snapshot_id,
                        ProjectFileVersion.relative_path == filename,
                    )
                    .limit(1)
                )
                if mapped is None:
                    raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            else:
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            continue
        if document_id:
            try:
                identity = uuid.UUID(str(document_id))
            except (ValueError, TypeError, AttributeError):
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID) from None
            document = db.get(Document, identity)
            if (
                document is None
                or kb is None
                or document.kb_id != kb.id
                or not document_lifecycle.is_retrievable(document)
            ):
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
        elif filename:
            if kb is None:
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
            documents = db.scalars(
                select(Document).where(Document.kb_id == kb.id, Document.filename == filename)
            ).all()
            if not any(document_lifecycle.is_retrievable(document) for document in documents):
                raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
        else:
            raise HTTPException(status_code=409, detail=_SOURCE_INVALID)


def verify_expected_sources(
    db: DBSession,
    sources: list[dict],
    kb: KnowledgeBase | None,
    snapshot_id: uuid.UUID | None,
) -> None:
    """公开薄入口：回归纳入/重跑前复用原有来源有效性校验。"""
    _verify_expected_sources(db, sources, kb, snapshot_id)


def _export_turns(db: DBSession, message: Message) -> list[str]:
    """按 ChatTurn 顺序取用户消息内容，截止到当前轮（当前问题必须在最后）。"""
    turns = db.scalars(
        select(ChatTurn)
        .where(ChatTurn.conversation_id == message.conversation_id)
        .order_by(
            ChatTurn.turn_seq.asc().nulls_last(),
            ChatTurn.created_at.asc(),
            ChatTurn.id.asc(),
        )
    ).all()
    if not turns:
        raise HTTPException(status_code=409, detail=_EXPORT_TURNS_OVERFLOW)
    user_message_ids = [turn.user_message_id for turn in turns]
    messages = {
        row.id: row
        for row in db.scalars(select(Message).where(Message.id.in_(user_message_ids))).all()
    }
    contents: list[str] = []
    located = False
    for turn in turns:
        if message.turn_id is not None:
            is_current = turn.id == message.turn_id
        else:
            is_current = turn.user_message_id == message.reply_to_id
        user_message = messages.get(turn.user_message_id)
        content = (
            user_message.content
            if user_message is not None and user_message.role == "user"
            else None
        )
        if is_current:
            if content is None or not content.strip():
                raise HTTPException(status_code=409, detail=_EXPORT_TURNS_OVERFLOW)
            contents.append(content)
            located = True
            break
        if content is not None and content.strip():
            contents.append(content)
    if not located:
        raise HTTPException(status_code=409, detail=_EXPORT_TURNS_OVERFLOW)
    if len(contents) > _EXPORT_TURNS_LIMIT:
        raise HTTPException(status_code=409, detail=_EXPORT_TURNS_OVERFLOW)
    return contents


def export_turns(db: DBSession, message: Message) -> list[str]:
    """公开薄入口：回归适配层复用原有最多 10 轮的提问序列导出。"""
    return _export_turns(db, message)


def _verified_by(feedback: AnswerFeedback) -> str:
    name = feedback.reviewer_name or ""
    if _VERIFIED_BY_RE.fullmatch(name):
        return name
    if feedback.reviewer_id is not None:
        return f"verifier-{feedback.reviewer_id.hex[:8]}"
    return f"verifier-{feedback.id.hex[:8]}"


def _case_for_feedback(db: DBSession, admin: User, feedback: AnswerFeedback) -> dict:
    message = db.get(Message, feedback.message_id)
    conversation = db.get(Conversation, feedback.conversation_id)
    if message is None or conversation is None:
        raise HTTPException(status_code=409, detail="原回答已不存在，无法导出")
    if (
        feedback.corrected_outcome is None
        or feedback.reviewed_at is None
        or not (feedback.evidence_note or "").strip()
        or feedback.unknowns
    ):
        raise HTTPException(status_code=409, detail="反馈核验信息不完整，无法导出")
    if feedback_stale(db, feedback, message):
        raise HTTPException(status_code=409, detail="原回答已被修改，反馈结果已过期")
    if conversation.project_snapshot_id is not None:
        project_chat_scope.replay_project_check(db, admin.id, conversation.project_snapshot_id)
    kb: KnowledgeBase | None = None
    if conversation.kb_id is not None:
        kb = db.get(KnowledgeBase, conversation.kb_id)
        if kb is None:
            raise HTTPException(status_code=404, detail="知识库不存在")
        if not admin.is_admin and not kb.is_public and kb.owner_id != admin.id:
            raise HTTPException(status_code=403, detail="无权访问该知识库")
    if conversation.mode == "rag" and kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if kb is not None and kb.kind == "project" and conversation.project_snapshot_id is None:
        raise HTTPException(status_code=409, detail="项目快照已不可用")
    sources = _stored_sources(feedback.expected_sources)
    if conversation.mode == "chat":
        if sources:
            raise HTTPException(status_code=409, detail=_SOURCE_INVALID)
    else:
        _verify_expected_sources(db, sources, kb, conversation.project_snapshot_id)
    turns = _export_turns(db, message)
    owner = db.get(User, feedback.user_id)
    if owner is None:
        raise HTTPException(status_code=409, detail="反馈用户已不存在，无法导出")
    reviewed_at = feedback.reviewed_at
    if reviewed_at.tzinfo is None:
        reviewed_at = reviewed_at.replace(tzinfo=timezone.utc)
    case = {
        "id": f"feedback-{feedback.id}",
        "turns": turns,
        "question": turns[-1],
        "mode": conversation.mode,
        "expected_outcome": feedback.corrected_outcome,
        "expected_sources": sources,
        "required_phrases": list(feedback.required_phrases or []),
        "forbidden_phrases": list(feedback.forbidden_phrases or []),
        "actor": "admin" if owner.role == "admin" else "member",
        "verified_by": _verified_by(feedback),
        "verified_at": reviewed_at.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        "reviewed": True,
        "unknowns": [],
    }
    if conversation.mode == "rag" and conversation.kb_id is not None:
        case["kb_id"] = str(conversation.kb_id)
    return case


def case_for_feedback(db: DBSession, admin: User, feedback: AnswerFeedback) -> dict:
    """公开薄入口：回归适配层复用原有严格核验 case 构造。

    返回值仍是不含回归协议标记的基准 case；旧导出路径与协议保持不变。
    """
    return _case_for_feedback(db, admin, feedback)


def export_feedback(
    db: DBSession, admin: User, *, feedback_id: uuid.UUID | None = None
) -> dict:
    if feedback_id is not None:
        feedback = db.get(AnswerFeedback, feedback_id)
        if feedback is None:
            raise HTTPException(status_code=404, detail="反馈不存在")
        if feedback.status != FEEDBACK_STATUS_VERIFIED:
            raise HTTPException(status_code=409, detail="仅已核实的反馈可以导出")
        rows = [feedback]
    else:
        rows = list(
            db.scalars(
                select(AnswerFeedback)
                .where(AnswerFeedback.status == FEEDBACK_STATUS_VERIFIED)
                .order_by(AnswerFeedback.created_at.asc(), AnswerFeedback.id.asc())
                .limit(_EXPORT_MAX + 1)
            ).all()
        )
        if not rows:
            raise HTTPException(status_code=400, detail="没有可导出的已核实反馈")
        if len(rows) > _EXPORT_MAX:
            raise HTTPException(
                status_code=409,
                detail=f"已核实反馈超过 {_EXPORT_MAX} 条，请分批导出",
            )
    cases = [_case_for_feedback(db, admin, feedback) for feedback in rows]
    try:
        validate_cases(cases)
    except EvaluationError:
        raise HTTPException(status_code=409, detail="评测用例校验失败，无法导出") from None
    for case in cases:
        if review_state(case) != "complete" or case.get("reviewed") is False:
            raise HTTPException(status_code=409, detail="评测用例核验信息不完整，无法导出")
    identity = dataset_identity(cases)
    jsonl = "".join(
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for case in cases
    )
    return {
        "filename": f"answer-feedback-{utc_now().strftime('%Y%m%dT%H%M%SZ')}.jsonl",
        "case_count": len(cases),
        "dataset_sha256": identity["sha256"],
        "jsonl": jsonl,
    }
