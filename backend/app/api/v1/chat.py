import asyncio
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import exists, false, func, or_, select, update
from sqlalchemy.orm import Session as DBSession
from starlette.requests import ClientDisconnect

from app.api.deps import admit_chat, get_current_user, write_audit
from app.core.config import settings
from app.core.observability import start_chat
from app.db.session import SessionLocal, get_db
from app.models import CONVERSATION_MODE_CHAT, CONVERSATION_MODE_RAG, ChatTurn, Conversation, KnowledgeBase, Message, User
from app.schemas.chat import (
    Citation, ChatMode, ChatRequest, ChatRequestState, ConversationDetail, ConversationMetadataUpdate,
    ConversationRead, DocumentScopeState, DocumentScopeUpdate, MemoryResetRequest, MemoryResetResult,
    MemoryStatus, MessageRead, ProjectSnapshotUpdate, RetrievalConfig,
)
from app.schemas.common import ApiResponse, PageResult
from app.services import (
    answer_history, answer_sources, chat_turns, document_scopes, followup, kb_access, memory_service,
    project_chat_scope, project_retrieval, project_sources, rag, usage_tracking,
)
from app.services.cloud_models import CloudModelError
from app.services.context_window import ContextBudgetError
from app.services.vector_store import load_ready_citations
from app.services.stream_control import (
    StreamEvent, StreamFault, StreamState, active_streams, encode_event, public_limits, stream_events,
)

router = APIRouter(prefix="/chat", tags=["问答"])
logger = logging.getLogger(__name__)


def _get_conversation_row(db: DBSession, conversation_id: uuid.UUID, user: User) -> Conversation:
    """会话行归属（本人或管理员）；内容读取另由 ``_require_conversation_kb_read`` 把关。"""
    conv = db.get(Conversation, conversation_id, populate_existing=True)
    if conv is None or (conv.user_id != user.id and not getattr(user, "is_admin", False)):
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv


def _require_conversation_kb_read(db: DBSession, conv: Conversation, user: User) -> None:
    """RAG 会话必须仍能读取其知识库；撤权后不返回历史标题/正文/引用。

    调用前必须已确认会话归属（本人或管理员）：陌生会话仍是 404。属主会话在
    KB 读取权被撤销时保留既有 403 契约（``_get_kb`` 的语义），绝不因会话属于
    本人就绕过知识库成员授权。真实 ``Conversation.kb_id`` 是 ``ON DELETE
    CASCADE``，因此 RAG 却没有知识库（含 mock 中只为重放配置的 rag/None）
    一律按不可读拒绝，不设“无 KB 的 RAG 放行”旁路。
    """
    if conv.mode != CONVERSATION_MODE_RAG:
        return
    kb = db.get(KnowledgeBase, conv.kb_id) if conv.kb_id is not None else None
    if kb is None or not kb_access.can_read_kb(db, kb, user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")


def _get_conversation(db: DBSession, conversation_id: uuid.UUID, user: User) -> Conversation:
    conv = _get_conversation_row(db, conversation_id, user)
    _require_conversation_kb_read(db, conv, user)
    return conv


def _get_owned_conversation(db: DBSession, conversation_id: uuid.UUID, user: User) -> Conversation:
    """元数据写操作只认会话本人；管理员读权限不能扩展到改别人的会话。"""
    conv = _get_conversation_row(db, conversation_id, user)
    if conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv


def _conversation_filters(kb_id: uuid.UUID | None, mode: ChatMode | None, q: str | None):
    """列表筛选：始终按当前用户收敛，q 为标题字面匹配。"""
    clauses = []
    if kb_id:
        clauses.append(Conversation.kb_id == kb_id)
    if mode:
        clauses.append(Conversation.mode == mode)
    if q:
        clauses.append(Conversation.title.icontains(q, autoescape=True))
    return clauses


def _conversation_order():
    # 稳定排序：置顶优先（NULL 最后），再按更新时间与 ID 兜底。
    return (Conversation.pinned_at.desc().nulls_last(),
            Conversation.updated_at.desc(), Conversation.id.desc())


def _conversation_visibility(user: User):
    """列表 SQL 谓词：本人会话中，普通对话恒可见、RAG 会话还需当前知识库可见。

    成员 EXISTS 在 ``total``、分页与 ``LIMIT`` 之前进入 SQL，绝不先取一页再在
    Python 里过滤（否则总量与页码都会泄漏已撤权的私有标题）。会话本人隔离不变。
    """
    if not getattr(user, "is_active", False):
        return false()
    visible_kb = exists(select(KnowledgeBase.id).where(
        KnowledgeBase.id == Conversation.kb_id,
        kb_access.visible_kb_predicate(user),
    ))
    return or_(Conversation.mode == CONVERSATION_MODE_CHAT, visible_kb)


def _validate_query(q: str | None) -> str | None:
    if q is None:
        return None
    value = q.strip()
    if len(value) > 200:
        raise HTTPException(status_code=422, detail="搜索关键字不能超过 200 个字符")
    return value or None


def _get_kb(db: DBSession, kb_id: uuid.UUID, user: User) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not kb_access.can_read_kb(db, kb, user):
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    return kb


@router.get("/retrieval-config", response_model=ApiResponse[RetrievalConfig], summary="检索参数默认值")
def get_retrieval_config(user: User = Depends(get_current_user)):
    # 只暴露前端需要的那一个值：滑块的初值必须和服务端的回退值一致，
    # 否则界面显示的条数与调用方省略 top_k 时的实际检索条数会对不上。
    return ApiResponse(data=RetrievalConfig(top_k=settings.RETRIEVAL_TOP_K))


@router.get("/conversations", response_model=ApiResponse[list[ConversationRead]], summary="我的会话列表")
def list_conversations(kb_id: uuid.UUID | None = None, mode: ChatMode | None = None,
                       db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(Conversation).where(Conversation.user_id == user.id,
                                      _conversation_visibility(user))
    if kb_id:
        stmt = stmt.where(Conversation.kb_id == kb_id)
    if mode:
        stmt = stmt.where(Conversation.mode == mode)
    rows = db.scalars(stmt.order_by(*_conversation_order()).limit(100)).all()
    return ApiResponse(data=[ConversationRead.model_validate(r) for r in rows])


@router.get("/conversations/page", response_model=ApiResponse[PageResult[ConversationRead]], summary="我的会话分页列表")
def page_conversations(q: str | None = Query(None), page: int = Query(1, ge=1),
                       page_size: int = Query(20, ge=1, le=100), kb_id: uuid.UUID | None = None,
                       mode: ChatMode | None = None, db: DBSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    # 固定路由必须放在 /conversations/{conversation_id} 之前，否则 "page" 会被当成会话 ID。
    keyword = _validate_query(q)
    clauses = _conversation_filters(kb_id, mode, keyword)
    base = select(Conversation).where(Conversation.user_id == user.id,
                                     _conversation_visibility(user), *clauses)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(*_conversation_order()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return ApiResponse(data=PageResult(
        total=total, items=[ConversationRead.model_validate(row) for row in rows]
    ))


@router.patch("/conversations/{conversation_id}", response_model=ApiResponse[ConversationRead], summary="重命名或置顶会话")
def update_conversation_metadata(conversation_id: uuid.UUID, payload: ConversationMetadataUpdate, request: Request,
                                 db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    # 仅会话本人可改；管理员对他人的读权限不延伸到这里。
    conv = _get_owned_conversation(db, conversation_id, user)
    _require_conversation_kb_read(db, conv, user)
    # RAG 会话沿用知识库访问校验：权限被撤销后不能借元数据接口继续操作。
    fields = payload.model_fields_set
    values = {}
    if "title" in fields:
        values["title"] = payload.title
    if "pinned" in fields:
        values["pinned_at"] = datetime.now(timezone.utc) if payload.pinned else None
    if "pinned" in fields and payload.pinned and conv.pinned_at is not None:
        # 已经置顶时不刷新置顶时间，避免一次无意义的改名/重复置顶打乱排序。
        values["pinned_at"] = conv.pinned_at
    # 显式带上原 updated_at：TimestampMixin 的 onupdate 只在未提供该列时生效，
    # 这样元数据变更不会把会话顶到"最近更新"的最前面，也不影响正在生成的租约。
    db.execute(
        update(Conversation)
        .where(Conversation.id == conv.id, Conversation.user_id == user.id)
        .values(**values, updated_at=Conversation.updated_at)
    )
    db.commit()
    db.refresh(conv)
    try:
        detail = {"fields": sorted(fields), "title": conv.title, "pinned": conv.pinned_at is not None}
        write_audit(db, request, user, "conversation_metadata", conv.title, detail)
    except Exception:
        # 审计失败不能撤销已经成功的元数据。
        logger.warning("会话元数据已更新，审计写入失败: conversation=%s", conversation_id)
    return ApiResponse(data=ConversationRead.model_validate(conv))


@router.get("/conversations/{conversation_id}", response_model=ApiResponse[ConversationDetail], summary="会话详情")
def get_conversation(conversation_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    conv = _get_conversation(db, conversation_id, user)
    if getattr(conv, "project_snapshot_id", None) is not None:
        project_chat_scope.replay_project_check(db, user.id, conv.project_snapshot_id)
    detail = ConversationDetail.model_validate(conv)
    completed_turns = {message.turn_id for message in detail.messages if message.turn_id and message.role == "assistant"}
    traces = dict(db.execute(select(ChatTurn.id, ChatTurn.trace).where(
        ChatTurn.id.in_(completed_turns), ChatTurn.conversation_id == conversation_id,
        ChatTurn.status == "completed",
    )).all()) if completed_turns else {}
    # 只规范化响应，不覆盖旧消息；未完成消息不成为历史证据。
    for message in detail.messages:
        message.finish_reason, message.truncated = _completion_flags(traces.get(message.turn_id))
        candidates = [citation.model_dump() for citation in message.citations or []]
        citations = (rag.finalize_citations(message.content, candidates)
                     if detail.mode == CONVERSATION_MODE_RAG and message.role == "assistant" and message.status == "completed" else [])
        message.citations = [Citation.model_validate(citation) for citation in citations] or None
    return ApiResponse(data=detail)


@router.patch("/conversations/{conversation_id}/scope", response_model=ApiResponse[DocumentScopeState], summary="调整会话文档范围")
def set_document_scope(conversation_id: uuid.UUID, payload: DocumentScopeUpdate,
                       db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    conv = _get_owned_conversation(db, conversation_id, user)
    _require_conversation_kb_read(db, conv, user)
    if conv.mode != CONVERSATION_MODE_RAG or conv.kb_id is None:
        raise HTTPException(400, "普通对话不支持文档范围")
    kb = _get_kb(db, conv.kb_id, user)
    if kb.kind == "project":
        raise HTTPException(400, "项目知识库请通过项目快照调整范围")
    selected, revision = chat_turns.set_document_scope(db, conversation_id, payload.document_ids,
                                                     payload.expected_revision)
    return ApiResponse(data=DocumentScopeState(document_scope=selected, scope_revision=revision))


@router.patch("/conversations/{conversation_id}/project-snapshot", response_model=ApiResponse[DocumentScopeState],
              summary="切换会话项目快照")
def set_project_snapshot(conversation_id: uuid.UUID, payload: ProjectSnapshotUpdate,
                         db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    conv = _get_owned_conversation(db, conversation_id, user)
    _require_conversation_kb_read(db, conv, user)
    if conv.mode != CONVERSATION_MODE_RAG or conv.kb_id is None:
        raise HTTPException(400, "仅项目知识库会话支持项目快照")
    _get_kb(db, conv.kb_id, user)
    snapshot_id, revision = chat_turns.set_project_snapshot(
        db, conversation_id, user, payload.snapshot_id, payload.expected_revision,
    )
    return ApiResponse(data=DocumentScopeState(
        document_scope=[], scope_revision=revision, project_snapshot_id=snapshot_id,
    ))


@router.get("/conversations/{conversation_id}/memory", response_model=ApiResponse[MemoryStatus], summary="读取会话摘要状态")
def get_conversation_memory(conversation_id: uuid.UUID, db: DBSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    # 只读本人会话的摘要状态；管理员对他人的读权限不延伸到这里的记忆。
    conv = _get_owned_conversation(db, conversation_id, user)
    _require_conversation_kb_read(db, conv, user)
    return ApiResponse(data=memory_service.read_memory_status(db, conv))


@router.post("/conversations/{conversation_id}/memory/reset", response_model=ApiResponse[MemoryResetResult],
             summary="重置会话记忆")
def reset_conversation_memory(conversation_id: uuid.UUID, payload: MemoryResetRequest,
                              db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    # 仅会话本人可重置（管理员不能重置他人会话）。
    conv = _get_owned_conversation(db, conversation_id, user)
    _require_conversation_kb_read(db, conv, user)
    if not memory_service.memory_enabled(conv):
        raise HTTPException(400, "该会话未启用记忆摘要")
    # 会话行锁内判定：正在生成答案（有效 active 问答租约）阻重置（409）；
    # 后台摘要任务 pending/processing 不阻塞——reset 会原子作废其 job 与 lease。
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        raise HTTPException(404, "会话不存在")
    if conv.memory_epoch is None:
        conv.memory_epoch = 0
    now = datetime.now(timezone.utc)
    if conv.active_turn_id is not None and conv.lease_expires_at and conv.lease_expires_at > now:
        raise HTTPException(409, "回答正在生成，无法重置记忆，请等待完成或停止后重试")
    if conv.active_turn_id is not None:
        # 租约已过期：回收残留的活跃问答，避免重置与它并发。
        chat_turns._expire_active(db, conv)
    try:
        result = memory_service.reset_memory(db, conv, payload.expected_version)
    except ValueError as exc:
        db.rollback()
        if str(exc) == "memory_version_mismatch":
            raise HTTPException(409, "记忆版本已改变，请刷新后重试") from exc
        raise
    db.commit()
    return ApiResponse(data=result)


@router.delete("/conversations/{conversation_id}", response_model=ApiResponse[None], summary="删除会话")
def delete_conversation(conversation_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    conv = _get_conversation_row(db, conversation_id, user)
    # 撤权后不能借删除接口读取/清理历史；本人取消在途任务仍走 cancel_request，
    # 不会因知识库撤权被扩大成读取他人内容。
    _require_conversation_kb_read(db, conv, user)
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        raise HTTPException(404, "会话不存在")
    chat_turns.clear_conversation_requests(db, [conversation_id])
    db.delete(conv)
    db.commit()
    return ApiResponse(data=None, message="已删除")


@router.post("/requests/{request_id}/cancel", response_model=ApiResponse[None], summary="停止自己的问答请求")
def cancel_request(request_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    chat_turns.cancel_request(db, request_id, user.id)
    active_streams.cancel(user.id, request_id)
    return ApiResponse(data=None, message="请求已结束")


def _completion_flags(trace) -> tuple[str | None, bool]:
    reason = trace.get("finish_reason") if isinstance(trace, dict) else None
    reason = reason if isinstance(reason, str) and len(reason) <= 64 else None
    return reason, reason == "length"


@router.get("/requests/{request_id}", response_model=ApiResponse[ChatRequestState], summary="读取自己的问答请求结果")
def get_request_state(request_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    # 管理员也只能按自己的 request_id 恢复，不能借状态接口读取其他用户的请求。
    turn = db.scalar(select(ChatTurn).where(ChatTurn.request_id == request_id, ChatTurn.user_id == user.id))
    if turn is None:
        raise HTTPException(404, "请求不存在")
    if turn.conversation_id is None:
        raise HTTPException(410, "原会话已删除，请求结果不可用")
    conv = _get_conversation_row(db, turn.conversation_id, user)
    if conv.mode == CONVERSATION_MODE_RAG:
        if conv.kb_id is None:
            raise HTTPException(410, "原知识库已删除，请求结果不可用")
        _get_kb(db, conv.kb_id, user)  # 权限撤销后不能靠旧请求读取结果（含成员撤权）。
        if getattr(turn, "project_snapshot_id", None) is not None:
            project_chat_scope.replay_project_check(db, user.id, turn.project_snapshot_id)
    chat_turns.expire_request(db, turn)
    finish_reason, truncated = _completion_flags(turn.trace)
    message = None
    if turn.status == "completed":
        answer = db.scalar(select(Message).where(Message.turn_id == turn.id,
                           Message.conversation_id == conv.id, Message.role == "assistant", Message.status == "completed"))
        if answer is None:
            raise HTTPException(410, "原请求结果已不可用")
        message = MessageRead.model_validate(answer)
        message.finish_reason, message.truncated = finish_reason, truncated
        citations = rag.finalize_citations(message.content, answer.citations or []) if conv.mode == "rag" else []
        message.citations = [Citation.model_validate(item) for item in citations] or None
    trace = turn.trace if isinstance(turn.trace, dict) else {}
    return ApiResponse(data=ChatRequestState(
        request_id=request_id, conversation_id=conv.id, user_message_id=turn.user_message_id,
        status=turn.status, message=message, finish_reason=finish_reason, truncated=truncated,
        document_scope=getattr(conv, "document_scope", None) or [], scope_revision=getattr(conv, "scope_revision", 0) or 0,
        project_snapshot_id=getattr(turn, "project_snapshot_id", None),
        error_code=trace.get("error_code") if turn.status in {"failed", "cancelled"} else None,
        error_message=trace.get("error_message") if turn.status in {"failed", "cancelled"} else None,
    ))


class ManagedChatResponse(StreamingResponse):
    """即使流尚未启动或在等待模型时断连，也清理持久化租约。"""
    def __init__(self, content, cleanup: Callable[[], None]):
        super().__init__(content, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        async def bounded_send(message):
            try:
                with anyio.fail_after(settings.CHAT_STREAM_SEND_TIMEOUT_SECONDS):
                    await send(message)
            except TimeoutError as exc:
                # 慢客户端也必须释放模型连接与名额；不为它继续生成或积压队列。
                raise ClientDisconnect("流式客户端读取超时") from exc
        try:
            await super().__call__(scope, receive, bounded_send)
        finally:
            with anyio.CancelScope(shield=True):
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    with suppress(asyncio.CancelledError, Exception):
                        await close()
                await anyio.to_thread.run_sync(self.cleanup)


def _source_identities(pairs):
    return [dict(source) for source in answer_sources.sources_from_pairs(pairs)]


def _retrieve_plan(collection, plan, top_k, active_scope, *, reset_scope=False):
    """历史来源只提供已核验锚点；新增证据始终从当前文档范围重新检索。"""
    selected = None if reset_scope else active_scope or None
    scope_update = [] if reset_scope else None
    if plan.status in {"reference", "document_followup", "elaboration"}:
        requested = plan.citations
        if selected and plan.status == "reference" and any(not document_scopes.contains(selected, item) for item in requested):
            # 重建只改状态文案；规划依赖保留计划的完整既有集合，不因 scope 缩窄丢失。
            return [], plan.with_fields(status="clarify",
                clarification="该历史来源不在当前文档范围内，请先切换或取消范围后再核验。"), None
        if selected and plan.status == "elaboration":
            requested = [item for item in requested if document_scopes.contains(selected, item)]
            names = "、".join("《" + item["filename"] + "》" for item in selected)
            plan = plan.with_fields(citations=requested,
                query=f"围绕原问题和后续要求：\n{plan.topic}\n请仅在当前限定文件中继续展开：{names}。")
        anchors = load_ready_citations(collection, requested) if requested else []
        if len(anchors) != len(requested):
            return [], plan.with_fields(status="clarify",
                clarification="您指定的历史来源已不可用、被更新或无法核验，请重新选择文档或确认来源。"), None
        if plan.status == "reference":
            return anchors, plan, scope_update
        if plan.status == "document_followup":
            selected = scope_update = document_scopes.from_verified_pairs(anchors)
        elif selected is None:
            selected = document_scopes.from_verified_pairs(anchors)
        docs = rag.supplement_sources(collection, plan.topic or plan.query, anchors, top_k, selected)
    else:
        options = {"document_scope": selected} if selected is not None else {}
        docs = rag.retrieve(collection, plan.query, top_k, **options)
    if selected is not None:
        docs = [pair for pair in docs if document_scopes.contains(selected, pair[0].metadata)]
    return docs, plan, scope_update


def _retrieve_project_plan(snapshot_id, user_id, plan, top_k):
    """项目检索始终按本轮固定快照重读原文，历史回答只用于定位来源。"""
    with SessionLocal() as session:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            raise chat_turns.ChatLeaseLost("用户已不可用，请重新登录")
        try:
            anchors, file_ids = [], None
            if plan.status in {"reference", "document_followup", "elaboration"}:
                requested = plan.citations
                if any(item.get("source_kind") != "project" or
                       item.get("snapshot_id") != str(snapshot_id) for item in requested):
                    return [], plan.with_fields(
                        status="clarify",
                        clarification="该历史来源不属于当前项目快照，请先切换到原快照或重新提问。",
                    ), None
                anchors = project_retrieval.load_project_citations(session, snapshot_id, requested, user)
                if len(anchors) != len(requested):
                    return [], plan.with_fields(
                        status="clarify",
                        clarification="您指定的项目来源已不可用或无法核验，请重新选择文件或快照。",
                    ), None
                for (doc, _score), citation in zip(anchors, requested):
                    doc.metadata["historical_source_index"] = citation.get("source_index")
                if plan.status == "reference":
                    return anchors, plan, None
                file_ids = list(dict.fromkeys(doc.metadata["file_version_id"] for doc, _score in anchors))
            fresh = project_retrieval.retrieve_project(
                session, snapshot_id, user, plan.topic or plan.query, top_k, file_version_ids=file_ids,
            )
            # 项目入口返回融合后的文档，不暴露余弦距离；缺少真实分数时保持未知。
            pairs = [(doc, 0.0) for doc in fresh]
            for doc, _score in pairs:
                doc.metadata.setdefault("citation_score", None)
            anchor_limit = min(5, max(1, top_k // 2))
            result, covered = [], set()
            for doc, score in [*anchors[:anchor_limit], *pairs, *anchors[anchor_limit:]]:
                meta = doc.metadata
                first = meta.get("chunk_start", meta["chunk_index"])
                last = meta.get("chunk_end", meta["chunk_index"])
                keys = {(meta["file_version_id"], index) for index in range(first, last + 1)}
                if keys <= covered:
                    continue
                result.append((doc, score))
                covered.update(keys)
                if len(result) >= top_k:
                    break
            return result, plan, None
        except (project_retrieval.ProjectSnapshotNotReady, project_sources.ProjectSourceNotFound,
                project_sources.ProjectSourceRevoked) as exc:
            raise chat_turns.ChatLeaseLost("项目快照不再可用或访问权限已改变，请刷新会话") from exc


@router.post("/stream", dependencies=[Depends(admit_chat)], summary="流式问答（SSE）")
def chat_stream(payload: ChatRequest, request: Request, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    if payload.include_trace and not user.is_admin:
        raise HTTPException(403, "检索评测明细仅管理员可读取")
    # 调用方未指定时回落到服务端默认；否则 RETRIEVAL_TOP_K 只是摆设。
    top_k = settings.RETRIEVAL_TOP_K if payload.top_k is None else payload.top_k
    # 续聊始终以会话已存的模式、知识库为准；新建与用户消息由认领事务原子保存。
    if payload.conversation_id:
        conv = _get_conversation(db, payload.conversation_id, user)
        mode = conv.mode
        if mode == CONVERSATION_MODE_RAG:
            if conv.kb_id is None:
                raise HTTPException(409, "该会话缺少知识库，无法继续")
            if payload.kb_id and payload.kb_id != conv.kb_id:
                raise HTTPException(400, "会话与知识库不匹配")
            kb = _get_kb(db, conv.kb_id, user)
        else:
            kb = None
    else:
        mode = payload.mode
        if mode == CONVERSATION_MODE_RAG and payload.kb_id is None:
            raise HTTPException(400, "知识库问答需要指定知识库")
        kb = _get_kb(db, payload.kb_id, user) if mode == CONVERSATION_MODE_RAG else None

    reservation = chat_turns.claim_turn(
        db, user_id=user.id, conversation_id=payload.conversation_id, kb_id=kb.id if kb else None,
        mode=mode, question=payload.question, top_k=top_k, request_id=payload.request_id,
        document_ids=payload.document_ids, scope_revision=payload.scope_revision,
        project_snapshot_id=payload.project_snapshot_id,
        auth_token_version=getattr(request.state, "auth_token_version", None),
        reset_scope=mode == CONVERSATION_MODE_RAG and followup.is_topic_switch(payload.question),
    )
    conv_id = str(reservation.conversation_id)
    project_sid = reservation.project_snapshot_id
    scope_state = {"document_scope": reservation.document_scope, "scope_revision": reservation.scope_revision,
                   "project_snapshot_id": str(project_sid) if project_sid else None}
    meta = {"conversation_id": conv_id, "request_id": str(reservation.request_id),
            "user_message_id": str(reservation.user_message_id), "citations": [], "mode": mode,
            "stream_limits": public_limits(), **scope_state}
    if reservation.replay_answer is not None:
        # 新建请求的重放也要重新检查当前会话权限，不能借历史管理员身份读取他人会话。
        replay_conv = _get_conversation(db, reservation.conversation_id, user)
        if replay_conv.mode != mode or replay_conv.kb_id != (kb.id if kb else None):
            raise HTTPException(409, "原会话配置已改变，请刷新后重试")
        scope_state = {"document_scope": getattr(replay_conv, "document_scope", []) or [],
                       "scope_revision": getattr(replay_conv, "scope_revision", 0) or 0,
                       "project_snapshot_id": str(project_sid) if project_sid else None}
        meta.update(scope_state)
        db.commit()
        async def replay_source():
            citations = rag.finalize_citations(reservation.replay_answer, reservation.replay_citations) if mode == "rag" else []
            yield StreamEvent("meta", {**meta, "replayed": True})
            if len(reservation.replay_answer) > settings.CHAT_STREAM_MAX_ANSWER_CHARS:
                yield StreamEvent("error", {"code": "stream_limit", "message": "历史回答超过流式长度上限，请读取请求结果"})
                return
            # 单个大 delta 会超过事件上限；重放和新生成共用有界正文片段。
            for offset in range(0, len(reservation.replay_answer), 4096):
                yield StreamEvent("delta", {"content": reservation.replay_answer[offset:offset + 4096]})
            reason, truncated = _completion_flags(reservation.replay_trace)
            done = {"latency_ms": reservation.replay_latency_ms, "citations": citations,
                    "message_id": str(reservation.assistant_message_id), "replayed": True,
                    "finish_reason": reason, "truncated": truncated,
                    "first_token_ms": reservation.replay_trace.get("first_token_ms"), **scope_state}
            if payload.include_trace:
                done["trace"] = reservation.replay_trace
            yield StreamEvent("done", done)

        async def replay():
            controlled = stream_events(replay_source(), state=StreamState(), user_id=user.id,
                                       request_id=reservation.request_id)
            try:
                async for frame in controlled:
                    yield frame
            except StreamFault as exc:
                yield _sse("error", {"code": exc.code, "message": str(exc)})
            finally:
                with anyio.CancelScope(shield=True):
                    await controlled.aclose()
        return ManagedChatResponse(replay(), lambda: None)

    # 只有真正开始一次新生成才观测；历史回答的重放不计入生成指标。
    # 观测对象在持久化与流式主体之前创建，使历史加载失败、客户端未消费响应体、
    # 断连与取消都能收口同一个 gauge，且 finish 幂等只会生效一次。
    observation = start_chat(mode)
    stream_started = time.monotonic()
    metric_lock = threading.Lock()
    metric_state: dict = {"first_token_ms": None, "stages": {}, "done": False}

    def record_stage(name, value):
        if value is None:
            return
        with metric_lock:
            metric_state["stages"][name] = value

    def note_first_token():
        """首个非空白正文到达时记录一次；与 StreamState 的首字语义一致。"""
        with metric_lock:
            if metric_state["first_token_ms"] is not None:
                return
        record_first_token((time.monotonic() - stream_started) * 1000)

    def record_first_token(value):
        if value is None:
            return
        with metric_lock:
            if metric_state["first_token_ms"] is None:
                metric_state["first_token_ms"] = value

    def finalize_metrics(status):
        # 不引用 state / docs 等流式局部变量：cleanup 可能在这些对象建立前运行。
        with metric_lock:
            if metric_state["done"]:
                return
            metric_state["done"] = True
            first_token = metric_state["first_token_ms"]
            stages = dict(metric_state["stages"])
        # total_ms 省略：由观测对象按真实起点补齐，不能用响应头时延冒充生成时长。
        observation.finish(status, first_token_ms=first_token, stages_ms=stages)

    cleaned = False
    def cleanup(status="cancelled", *, error_code=None, error_message=None):
        nonlocal cleaned
        finalize_metrics(status)
        if not cleaned:
            chat_turns.end_turn(reservation, status, error_code=error_code, error_message=error_message)
            cleaned = True

    try:
        memory_meta = None
        # 普通对话且启用记忆时，历史来自"已发布摘要 + 水位后最近完整轮原文"（不含 RAG
        # 主题/引用）；否则回落到原有 HISTORY_MAX_TURNS 窗口裁剪（行为与升级前一致）。
        # conv 在该函数作用域可能未定义（新建会话），从认领事务保存的会话重新读取。
        memory_conv = db.get(Conversation, reservation.conversation_id)
        if mode == CONVERSATION_MODE_CHAT and memory_service.memory_enabled(memory_conv):
            history, mem_lag, mem_truncated = memory_service.build_chat_history(db, memory_conv, payload.question)
            memory_meta = {"mode": "memory", "lag": mem_lag, "truncated": mem_truncated}
        elif mode == CONVERSATION_MODE_RAG:
            history = answer_history.load_rag_history(db, reservation)
            for item in history:
                if item["role"] == "assistant":
                    item["citations"] = rag.finalize_citations(item["content"], item["citations"])
        else:
            history_rows = db.scalars(select(Message).where(
                Message.conversation_id == reservation.conversation_id, Message.status == "completed",
            ).order_by(Message.created_at.desc(), Message.id.desc()).limit(settings.HISTORY_MAX_TURNS * 2)).all()
            history = [{"id": str(m.id), "role": m.role, "content": m.content,
                        "reply_to_id": str(m.reply_to_id) if getattr(m, "reply_to_id", None) else None,
                        "turn_id": str(m.turn_id) if getattr(m, "turn_id", None) else None, "status": getattr(m, "status", "completed"),
                        "followup_topic": None,
                        "citations": rag.finalize_citations(m.content, m.citations or []) if m.role == "assistant" else []}
                       for m in reversed(history_rows)]
        db.commit()  # 流式等待期间不占请求会话的连接。
    except Exception:
        cleanup("failed")
        raise

    collection = kb.collection_name if kb else None
    kb_name = kb.name if kb else "普通对话"
    user_id, username = user.id, user.username
    question = payload.question
    usage_context = usage_tracking.UsageContext(user_id=user_id, kb_id=kb.id if kb else None,
                                               conversation_id=reservation.conversation_id, turn_id=reservation.turn_id)
    scope = f"kb={collection}" if collection else f"mode={mode}"

    state = StreamState()
    saving = False

    async def in_worker(function):
        # 检索/ORM 暂留短工作线程；取消时停止等待，其结果不能再进入后续生成或写回。
        return await anyio.to_thread.run_sync(function, abandon_on_cancel=True)

    async def check_lease(renew):
        if cleaned or saving:
            return True  # 完成事务自己执行行锁和租约校验，释放租约不能误判为取消。
        valid = await in_worker(lambda: chat_turns.check_lease(reservation, renew))
        return valid or cleaned or saving

    async def event_source() -> AsyncIterator[StreamEvent]:
        nonlocal cleaned, saving
        docs, answer_parts = [], []
        answer_chars = 0
        rewrite_ms = retrieve_ms = 0.0
        plan = followup.RetrievalPlan(question)
        prepared, scope_update = None, None
        usage = {}
        yield StreamEvent("meta", meta)
        if mode == CONVERSATION_MODE_RAG:
            yield StreamEvent("status", {"phase": "retrieving"})
            t = time.monotonic()
            plan_options = ({"document_scope": reservation.document_scope}
                            if reservation.document_scope and not followup.is_topic_switch(question) else {})
            def plan_query():
                with usage_tracking.usage_scope(usage_context.for_operation("rewrite")):
                    return followup.plan_retrieval(question, history, **plan_options)
            plan = await in_worker(plan_query)
            rewrite_ms = (time.monotonic() - t) * 1000
            record_stage("rewrite", rewrite_ms)
            if not plan.clarification:
                t = time.monotonic()
                def retrieve():
                    with usage_tracking.usage_scope(usage_context.for_operation("query_embedding")):
                        if project_sid is not None:
                            return _retrieve_project_plan(project_sid, user_id, plan, top_k)
                        return _retrieve_plan(collection, plan, top_k, reservation.document_scope,
                                              reset_scope=followup.is_topic_switch(question))
                docs, plan, scope_update = await in_worker(retrieve)
                retrieve_ms = (time.monotonic() - t) * 1000
                # trace 中的 retrieve_ms 对应固定阶段 retrieval；不能把整份 trace 当阶段表。
                record_stage("retrieval", retrieve_ms)
        candidate_sources = _source_identities(docs)
        # 发布契约的 history_sources 在生成前合并（规划依赖 ∪ 最终有界历史来源）：
        # 超限/坏源在进入模型前整轮拒绝，不会生成后再被发布门禁兜底。
        history_sources = list(plan.planning_sources)
        if not plan.clarification and (mode == CONVERSATION_MODE_CHAT or docs):
            t_prompt = time.monotonic()
            prepared = await in_worker(lambda: rag.prepare_answer(question, docs, history, mode=mode, retrieval_query=plan.query))
            record_stage("prompt", (time.monotonic() - t_prompt) * 1000)
            docs = prepared.pairs
            history_sources = list(answer_sources.parse_sources(
                [*plan.planning_sources, *prepared.history_sources],
                default_kind=answer_sources.SOURCE_KIND_DOCUMENT))
        candidates = rag.to_citations(docs)
        yield StreamEvent("status", {"phase": "generating"})
        t_llm = time.monotonic()
        if plan.clarification:
            answer_parts.append(plan.clarification)
            for offset in range(0, len(plan.clarification), 4096):
                yield StreamEvent("delta", {"content": plan.clarification[offset:offset + 4096]})
        else:
            tokens = rag.answer_astream(question, docs, history, mode=mode, prepared=prepared,
                                        metrics=usage, usage_context=usage_context)
            try:
                async for token in tokens:
                    # 校验发生在累积前，模型/测试适配器不能绕过回答大小与类型限制。
                    if not isinstance(token, str):
                        raise StreamFault("invalid_stream", "模型返回的正文格式异常")
                    if answer_chars + len(token) > settings.CHAT_STREAM_MAX_ANSWER_CHARS:
                        raise StreamFault("stream_limit", "模型回答超过允许的长度，请缩小问题范围")
                    answer_chars += len(token)
                    answer_parts.append(token)
                    if token.strip():
                        # 首字时间只在模型真正输出正文时记录；澄清文案不算生成首字。
                        note_first_token()
                    for offset in range(0, len(token), 4096):
                        yield StreamEvent("delta", {"content": token[offset:offset + 4096]})
            finally:
                with anyio.CancelScope(shield=True):
                    await tokens.aclose()
        answer = "".join(answer_parts).strip()
        if not answer:
            raise CloudModelError("模型未返回有效内容，请重试")
        state.remaining()
        citations = rag.finalize_citations(answer, candidates)
        generation_ms = (time.monotonic() - t_llm) * 1000
        record_stage("generation", generation_ms)
        latency = round((time.monotonic() - state.started_at) * 1000)
        reason = usage.get("finish_reason")
        truncated = reason == "length"
        actual_usage = {key: usage[key] for key in ("input_tokens", "output_tokens") if key in usage}
        trace = {"retrieval_query": plan.query, "status": plan.status, "followup_topic": plan.topic,
                 "candidate_sources": candidate_sources,
                 "source_snapshot_version": answer_sources.SOURCE_SNAPSHOT_VERSION,
                 "supplied_sources": _source_identities(prepared.pairs) if prepared else [],
                 "history_sources": history_sources,
                 "rewrite_ms": round(rewrite_ms, 2), "retrieve_ms": round(retrieve_ms, 2),
                 "generation_ms": round(generation_ms, 2), "first_token_ms": state.first_token_ms,
                 "finish_reason": reason, "truncated": truncated,
                 "input_tokens_estimate": prepared.input_tokens if prepared else 0,
                 "input_budget": prepared.input_budget if prepared else 0,
                 "usage": actual_usage or None, "usage_scope": "generation_only",
                 "memory": memory_meta}
        yield StreamEvent("status", {"phase": "saving"})

        def save_answer():
            nonlocal cleaned
            t_save = time.monotonic()
            with SessionLocal() as session:
                completion_options = {"scope_update": scope_update} if scope_update is not None else {}
                message_id = chat_turns.complete_turn(session, reservation, answer, citations, latency, trace, **completion_options)
                cleaned = True
                record_stage("persistence", (time.monotonic() - t_save) * 1000)
                # 写回成功即视为一次完整生成；之后即使客户端不再读取响应体，
                # finally 里的默认 cancelled 也不会覆盖这个已完成状态（finish 幂等）。
                finalize_metrics("completed")
                detail = {"mode": mode, "conversation_id": conv_id, "request_id": str(reservation.request_id),
                          "retrieval_status": plan.status, "context_tokens_estimate": trace["input_tokens_estimate"]}
                if kb:
                    detail["kb_id"] = str(kb.id)
                try:
                    write_audit(session, request, session.get(User, user_id), "chat_query", kb_name, detail)
                except Exception:
                    session.rollback()
                    logger.warning("问答已完成，审计日志写入失败: request=%s", reservation.request_id)
                return message_id

        saving = True
        message_id = await anyio.to_thread.run_sync(save_answer)
        logger.info("问答完成: %s user=%s request=%s 补全=%.0fms 检索=%.0fms 生成=%.0fms 总计=%dms 首字=%s 引用=%d",
                    scope, username, reservation.request_id, rewrite_ms, retrieve_ms, generation_ms,
                    latency, state.first_token_ms, len(citations))
        final_scope = reservation.document_scope if scope_update is None else scope_update
        revision = reservation.scope_revision + int(final_scope != reservation.document_scope)
        done = {"latency_ms": latency, "citations": citations, "message_id": str(message_id),
                "document_scope": final_scope, "scope_revision": revision, "finish_reason": reason,
                "project_snapshot_id": str(project_sid) if project_sid else None,
                "truncated": truncated, "first_token_ms": state.first_token_ms}
        if payload.include_trace:
            done["trace"] = trace
        yield StreamEvent("done", done)

    async def event_stream() -> AsyncIterator[str]:
        controlled = stream_events(event_source(), state=state, user_id=user_id,
                                   request_id=reservation.request_id, check_lease=check_lease)
        try:
            async for frame in controlled:
                yield frame
        except Exception as exc:
            expected = isinstance(exc, (StreamFault, chat_turns.ChatLeaseLost))
            if expected:
                logger.info("问答流结束: %s user=%s request=%s reason=%s", scope, username,
                            reservation.request_id, getattr(exc, "code", "lease_lost"))
            else:
                logger.exception("问答失败: %s user=%s request=%s", scope, username, reservation.request_id)
            message = (str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                       if isinstance(exc, (StreamFault, CloudModelError, ContextBudgetError, chat_turns.ChatLeaseLost))
                       else "问答处理失败，请稍后重试")
            for key in (settings.LLM_API_KEY, settings.EMBEDDING_API_KEY, settings.RERANK_API_KEY):
                if key:
                    message = message.replace(key, "[已隐藏]")
            code = exc.code if isinstance(exc, StreamFault) else "lease_lost" if isinstance(exc, chat_turns.ChatLeaseLost) else "model_error"
            status = "cancelled" if code in {"cancelled", "lease_lost"} else "failed"
            await anyio.to_thread.run_sync(lambda: cleanup(status, error_code=code, error_message=message[:500]))
            yield _sse("error", {"code": code, "message": message[:500]})
        finally:
            with anyio.CancelScope(shield=True):
                await controlled.aclose()
                await anyio.to_thread.run_sync(cleanup)

    return ManagedChatResponse(event_stream(), cleanup)


def _sse(event: str, data: dict) -> str:
    return encode_event(event, data)
