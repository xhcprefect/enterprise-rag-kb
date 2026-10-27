import json
import logging
import time
import uuid
from collections.abc import Callable, Iterator

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import admit_chat, get_current_user, write_audit
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.models import CONVERSATION_MODE_CHAT, CONVERSATION_MODE_RAG, ChatTurn, Conversation, KnowledgeBase, Message, User
from app.schemas.chat import (
    Citation, ChatMode, ChatRequest, ConversationDetail, ConversationRead,
    DocumentScopeState, DocumentScopeUpdate,
)
from app.schemas.common import ApiResponse
from app.services import chat_turns, document_scopes, followup, rag, usage_tracking
from app.services.cloud_models import CloudModelError
from app.services.context_window import ContextBudgetError
from app.services.vector_store import load_ready_citations

router = APIRouter(prefix="/chat", tags=["问答"])
logger = logging.getLogger(__name__)


def _get_conversation(db: DBSession, conversation_id: uuid.UUID, user: User) -> Conversation:
    conv = db.get(Conversation, conversation_id, populate_existing=True)
    if conv is None or (conv.user_id != user.id and not user.is_admin):
        raise HTTPException(status_code=404, detail="会话不存在")
    return conv


def _get_kb(db: DBSession, kb_id: uuid.UUID, user: User) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if not user.is_admin and not kb.is_public and kb.owner_id != user.id:
        raise HTTPException(status_code=403, detail="无权访问该知识库")
    return kb


@router.get("/conversations", response_model=ApiResponse[list[ConversationRead]], summary="我的会话列表")
def list_conversations(kb_id: uuid.UUID | None = None, mode: ChatMode | None = None,
                       db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if kb_id:
        stmt = stmt.where(Conversation.kb_id == kb_id)
    if mode:
        stmt = stmt.where(Conversation.mode == mode)
    rows = db.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(100)).all()
    return ApiResponse(data=[ConversationRead.model_validate(r) for r in rows])


@router.get("/conversations/{conversation_id}", response_model=ApiResponse[ConversationDetail], summary="会话详情")
def get_conversation(conversation_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    detail = ConversationDetail.model_validate(_get_conversation(db, conversation_id, user))
    # 只规范化响应，不覆盖旧消息；未完成消息不成为历史证据。
    for message in detail.messages:
        candidates = [citation.model_dump() for citation in message.citations or []]
        citations = (rag.finalize_citations(message.content, candidates)
                     if detail.mode == CONVERSATION_MODE_RAG and message.role == "assistant" and message.status == "completed" else [])
        message.citations = [Citation.model_validate(citation) for citation in citations] or None
    return ApiResponse(data=detail)


@router.patch("/conversations/{conversation_id}/scope", response_model=ApiResponse[DocumentScopeState], summary="调整会话文档范围")
def set_document_scope(conversation_id: uuid.UUID, payload: DocumentScopeUpdate,
                       db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    conv = _get_conversation(db, conversation_id, user)
    if conv.mode != CONVERSATION_MODE_RAG or conv.kb_id is None:
        raise HTTPException(400, "普通对话不支持文档范围")
    _get_kb(db, conv.kb_id, user)
    selected, revision = chat_turns.set_document_scope(db, conversation_id, payload.document_ids,
                                                     payload.expected_revision)
    return ApiResponse(data=DocumentScopeState(document_scope=selected, scope_revision=revision))


@router.delete("/conversations/{conversation_id}", response_model=ApiResponse[None], summary="删除会话")
def delete_conversation(conversation_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    _get_conversation(db, conversation_id, user)
    # 与完成写回使用同一行锁：删除后保留去重身份，但清掉含问题/来源的诊断。
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
    return ApiResponse(data=None, message="请求已结束")


class ManagedChatResponse(StreamingResponse):
    """即使流尚未启动或在等待模型时断连，也清理持久化租约。"""
    def __init__(self, content, cleanup: Callable[[], None]):
        super().__init__(content, media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })
        self.cleanup = cleanup

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self.cleanup)


def _source_identities(pairs):
    return [{key: value for key, value in citation.items() if key not in {"snippet", "score"}}
            for citation in rag.to_citations(pairs)]


def _retrieve_plan(collection, plan, top_k, active_scope, *, reset_scope=False):
    """历史来源只提供已核验锚点；新增证据始终从当前文档范围重新检索。"""
    selected = None if reset_scope else active_scope or None
    scope_update = [] if reset_scope else None
    if plan.status in {"reference", "document_followup", "elaboration"}:
        requested = plan.citations
        if selected and plan.status == "reference" and any(not document_scopes.contains(selected, item) for item in requested):
            return [], followup.RetrievalPlan(plan.query, "clarify", "该历史来源不在当前文档范围内，请先切换或取消范围后再核验。", topic=plan.topic), None
        if selected and plan.status == "elaboration":
            requested = [item for item in requested if document_scopes.contains(selected, item)]
            names = "、".join("《" + item["filename"] + "》" for item in selected)
            plan = followup.RetrievalPlan(
                f"围绕原问题和后续要求：\n{plan.topic}\n请仅在当前限定文件中继续展开：{names}。",
                plan.status, citations=requested, topic=plan.topic,
            )
        anchors = load_ready_citations(collection, requested) if requested else []
        if len(anchors) != len(requested):
            return [], followup.RetrievalPlan(plan.query, "clarify", "您指定的历史来源已不可用、被更新或无法核验，请重新选择文档或确认来源。", topic=plan.topic), None
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


@router.post("/stream", dependencies=[Depends(admit_chat)], summary="流式问答（SSE）")
def chat_stream(payload: ChatRequest, request: Request, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    if payload.include_trace and not user.is_admin:
        raise HTTPException(403, "检索评测明细仅管理员可读取")
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
        mode=mode, question=payload.question, top_k=payload.top_k, request_id=payload.request_id,
        document_ids=payload.document_ids, scope_revision=payload.scope_revision,
        reset_scope=mode == CONVERSATION_MODE_RAG and followup.is_topic_switch(payload.question),
    )
    conv_id = str(reservation.conversation_id)
    scope_state = {"document_scope": reservation.document_scope, "scope_revision": reservation.scope_revision}
    meta = {"conversation_id": conv_id, "request_id": str(reservation.request_id),
            "user_message_id": str(reservation.user_message_id), "citations": [], "mode": mode, **scope_state}
    if reservation.replay_answer is not None:
        # 新建请求的重放也要重新检查当前会话权限，不能借历史管理员身份读取他人会话。
        replay_conv = _get_conversation(db, reservation.conversation_id, user)
        if replay_conv.mode != mode or replay_conv.kb_id != (kb.id if kb else None):
            raise HTTPException(409, "原会话配置已改变，请刷新后重试")
        scope_state = {"document_scope": getattr(replay_conv, "document_scope", []) or [],
                       "scope_revision": getattr(replay_conv, "scope_revision", 0) or 0}
        meta.update(scope_state)
        db.commit()
        def replay():
            citations = rag.finalize_citations(reservation.replay_answer, reservation.replay_citations) if mode == "rag" else []
            yield _sse("meta", {**meta, "replayed": True})
            yield _sse("delta", {"content": reservation.replay_answer})
            done = {"latency_ms": reservation.replay_latency_ms, "citations": citations,
                    "message_id": str(reservation.assistant_message_id), "replayed": True, **scope_state}
            if payload.include_trace:
                done["trace"] = reservation.replay_trace
            yield _sse("done", done)
        return ManagedChatResponse(replay(), lambda: None)

    cleaned = False
    def cleanup(status="cancelled"):
        nonlocal cleaned
        if not cleaned:
            chat_turns.end_turn(reservation, status)
            cleaned = True

    try:
        history_rows = db.scalars(select(Message).where(
            Message.conversation_id == reservation.conversation_id, Message.status == "completed",
        ).order_by(Message.created_at.desc(), Message.id.desc()).limit(settings.HISTORY_MAX_TURNS * 2)).all()
        # 主题随已完成请求保存，刷新页面、跨进程或历史窗口缩短后也能继续澄清。
        # 只读取当前会话的内部主题字段，不把诊断明细或旧答案变成本轮证据。
        turn_ids = {m.turn_id for m in history_rows if isinstance(getattr(m, "turn_id", None), uuid.UUID)}
        topics = {}
        if mode == CONVERSATION_MODE_RAG and turn_ids:
            traces = db.execute(select(ChatTurn.id, ChatTurn.trace).where(
                ChatTurn.id.in_(turn_ids), ChatTurn.conversation_id == reservation.conversation_id,
                ChatTurn.status == "completed",
            )).all()
            topics = {identifier: trace["followup_topic"] for identifier, trace in traces
                      if isinstance(trace, dict) and isinstance(trace.get("followup_topic"), str)
                      and 0 < len(trace["followup_topic"]) <= settings.HISTORY_MAX_CHARS}
        history = [{"id": str(m.id), "role": m.role, "content": m.content,
                    "reply_to_id": str(m.reply_to_id) if getattr(m, "reply_to_id", None) else None,
                    "turn_id": str(m.turn_id) if getattr(m, "turn_id", None) else None, "status": getattr(m, "status", "completed"),
                    "followup_topic": topics.get(getattr(m, "turn_id", None)) if m.role == "assistant" else None,
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

    def event_stream() -> Iterator[str]:
        nonlocal cleaned
        started = time.perf_counter()
        answer_parts, docs = [], []
        rewrite_ms = retrieve_ms = 0.0
        plan = followup.RetrievalPlan(question)
        prepared = None
        usage = {}
        scope_update = None
        try:
            with chat_turns.keepalive(reservation) as ensure_active:
                yield _sse("meta", meta)
                if mode == CONVERSATION_MODE_RAG:
                    t = time.perf_counter()
                    plan_options = ({"document_scope": reservation.document_scope}
                                    if reservation.document_scope and not followup.is_topic_switch(question) else {})
                    with usage_tracking.usage_scope(usage_context.for_operation("rewrite")):
                        plan = followup.plan_retrieval(question, history, **plan_options)
                    rewrite_ms = (time.perf_counter() - t) * 1000
                    ensure_active()
                    if not plan.clarification:
                        t = time.perf_counter()
                        with usage_tracking.usage_scope(usage_context.for_operation("query_embedding")):
                            docs, plan, scope_update = _retrieve_plan(
                                collection, plan, payload.top_k, reservation.document_scope,
                                reset_scope=followup.is_topic_switch(question),
                            )
                        retrieve_ms = (time.perf_counter() - t) * 1000
                candidate_sources = _source_identities(docs)
                if not plan.clarification and (mode == CONVERSATION_MODE_CHAT or docs):
                    prepared = rag.prepare_answer(question, docs, history, mode=mode, retrieval_query=plan.query)
                    docs = prepared.pairs
                candidates = rag.to_citations(docs)
                t_llm = time.perf_counter()
                tokens = iter([plan.clarification]) if plan.clarification else rag.answer_stream(
                    question, docs, history, mode=mode, prepared=prepared, metrics=usage, usage_context=usage_context)
                try:
                    for token in tokens:
                        ensure_active()
                        answer_parts.append(token)
                        yield _sse("delta", {"content": token})
                finally:
                    close = getattr(tokens, "close", None)
                    if close:
                        close()
                answer = "".join(answer_parts).strip()
                if not answer:
                    raise CloudModelError("模型未返回有效内容，请重试")
                citations = rag.finalize_citations(answer, candidates)
                generation_ms = (time.perf_counter() - t_llm) * 1000
                latency = int((time.perf_counter() - started) * 1000)
                trace = {"retrieval_query": plan.query, "status": plan.status, "followup_topic": plan.topic,
                         "candidate_sources": candidate_sources,
                         "supplied_sources": _source_identities(docs), "rewrite_ms": round(rewrite_ms, 2),
                         "retrieve_ms": round(retrieve_ms, 2), "generation_ms": round(generation_ms, 2),
                         "input_tokens_estimate": prepared.input_tokens if prepared else 0,
                         "input_budget": prepared.input_budget if prepared else 0, "usage": usage or None,
                         "usage_scope": "generation_only"}
                with SessionLocal() as session:
                    completion_options = {"scope_update": scope_update} if scope_update is not None else {}
                    message_id = chat_turns.complete_turn(session, reservation, answer, citations, latency, trace,
                                                          **completion_options)
                    cleaned = True
                    detail = {"mode": mode, "conversation_id": conv_id, "request_id": str(reservation.request_id),
                              "retrieval_status": plan.status, "context_tokens_estimate": trace["input_tokens_estimate"]}
                    if kb:
                        detail["kb_id"] = str(kb.id)
                    try:
                        write_audit(session, request, session.get(User, user_id), "chat_query", kb_name, detail)
                    except Exception:
                        session.rollback()
                        logger.warning("问答已完成，审计日志写入失败: request=%s", reservation.request_id)
                logger.info("问答完成: %s user=%s request=%s 补全=%.0fms 检索=%.0fms 生成=%.0fms 总计=%dms 引用=%d 上下文=%d/%d",
                            scope, username, reservation.request_id, rewrite_ms, retrieve_ms, generation_ms, latency,
                            len(citations), trace["input_tokens_estimate"], trace["input_budget"])
                final_scope = reservation.document_scope if scope_update is None else scope_update
                revision = reservation.scope_revision + int(final_scope != reservation.document_scope)
                done = {"latency_ms": latency, "citations": citations, "message_id": str(message_id),
                        "document_scope": final_scope, "scope_revision": revision}
                if payload.include_trace:
                    done["trace"] = trace
                yield _sse("done", done)
        except Exception as exc:
            cleanup("failed")
            if isinstance(exc, chat_turns.ChatLeaseLost):
                # 用户停止或另一请求接管后，模型才返回：这是预期的取消路径，不是故障，
                # 记 INFO 即可，否则每次点「停止」都会留下一条看似严重的 traceback。
                logger.info("问答已结束（取消或被新请求替代）: %s user=%s request=%s",
                            scope, username, reservation.request_id)
            else:
                logger.exception("问答失败: %s user=%s request=%s", scope, username, reservation.request_id)
            message = (str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
                       if isinstance(exc, (CloudModelError, ContextBudgetError, chat_turns.ChatLeaseLost))
                       else "问答处理失败，请稍后重试")
            for key in (settings.LLM_API_KEY, settings.EMBEDDING_API_KEY, settings.RERANK_API_KEY):
                if key:
                    message = message.replace(key, "[已隐藏]")
            yield _sse("error", {"message": message[:500]})
        finally:
            cleanup()

    return ManagedChatResponse(event_stream(), cleanup)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
