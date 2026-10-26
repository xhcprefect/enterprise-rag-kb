import json
import logging
import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DBSession

from app.api.deps import admit_chat, get_current_user, write_audit
from app.db.session import SessionLocal, get_db
from app.models import (
    CONVERSATION_MODE_CHAT,
    CONVERSATION_MODE_RAG,
    Conversation,
    KnowledgeBase,
    Message,
    User,
)
from app.schemas.chat import Citation, ChatMode, ChatRequest, ConversationDetail, ConversationRead
from app.schemas.common import ApiResponse
from app.services import rag
from app.core.config import settings
from app.services.cloud_models import CloudModelError

router = APIRouter(prefix="/chat", tags=["问答"])
logger = logging.getLogger(__name__)


def _get_conversation(
    db: DBSession, conversation_id: uuid.UUID, user: User
) -> Conversation:
    conv = db.get(Conversation, conversation_id)
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


@router.get(
    "/conversations",
    response_model=ApiResponse[list[ConversationRead]],
    summary="我的会话列表",
)
def list_conversations(
    kb_id: uuid.UUID | None = None,
    mode: ChatMode | None = None,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if kb_id:
        stmt = stmt.where(Conversation.kb_id == kb_id)
    if mode:
        stmt = stmt.where(Conversation.mode == mode)
    rows = db.scalars(stmt.order_by(Conversation.updated_at.desc()).limit(100)).all()
    return ApiResponse(data=[ConversationRead.model_validate(r) for r in rows])


@router.get(
    "/conversations/{conversation_id}",
    response_model=ApiResponse[ConversationDetail],
    summary="会话详情",
)
def get_conversation(
    conversation_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    detail = ConversationDetail.model_validate(_get_conversation(db, conversation_id, user))
    # 旧版保存的是所有检索候选。只规范化响应，不覆盖原消息或要求数据库迁移。
    for message in detail.messages:
        candidates = [citation.model_dump() for citation in message.citations or []]
        citations = (
            rag.finalize_citations(message.content, candidates)
            if detail.mode == CONVERSATION_MODE_RAG and message.role == "assistant"
            else []
        )
        message.citations = [Citation.model_validate(citation) for citation in citations] or None
    return ApiResponse(data=detail)


@router.delete(
    "/conversations/{conversation_id}",
    response_model=ApiResponse[None],
    summary="删除会话",
)
def delete_conversation(
    conversation_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    conv = _get_conversation(db, conversation_id, user)
    db.delete(conv)
    db.commit()
    return ApiResponse(data=None, message="已删除")


@router.post("/stream", dependencies=[Depends(admit_chat)], summary="流式问答（SSE，返回 text/event-stream，不套统一结构）")
def chat_stream(
    payload: ChatRequest,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 模式与会话绑定：续聊一律以会话已存的模式为准，请求体的 mode 只在新建时生效。
    # 否则把 RAG 会话当普通对话续写（或反过来），历史会与生成方式错配。
    if payload.conversation_id:
        conv = _get_conversation(db, payload.conversation_id, user)
        mode = conv.mode
        if mode == CONVERSATION_MODE_RAG:
            if conv.kb_id is None:
                raise HTTPException(status_code=409, detail="该会话缺少知识库，无法继续")
            # 以会话上的知识库为准，顺带堵住"用 A 库会话检索 B 库"的口子
            if payload.kb_id and payload.kb_id != conv.kb_id:
                raise HTTPException(status_code=400, detail="会话与知识库不匹配")
            kb = _get_kb(db, conv.kb_id, user)
        else:
            kb = None
            if payload.kb_id:
                logger.warning("普通对话会话收到 kb_id，已忽略: conv=%s", conv.id)
    else:
        mode = payload.mode
        if mode == CONVERSATION_MODE_RAG:
            if payload.kb_id is None:
                raise HTTPException(status_code=400, detail="知识库问答需要指定知识库")
            kb = _get_kb(db, payload.kb_id, user)
        else:
            # 前端切模式后可能仍带着旧 kbId，静默忽略而不是报错打断用户
            kb = None
            if payload.kb_id:
                logger.warning("新建普通对话收到 kb_id，已忽略")

        title = payload.question.strip()[:30] or "新会话"
        conv = Conversation(
            user_id=user.id,
            kb_id=kb.id if kb else None,
            mode=mode,
            title=title,
        )
        db.add(conv)
        db.commit()
        db.refresh(conv)

    history_rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(max(0, settings.HISTORY_MAX_TURNS) * 2)
    ).all()
    history = [{"role": m.role, "content": m.content} for m in reversed(history_rows)]

    # 先落库用户消息
    user_msg = Message(conversation_id=conv.id, role="user", content=payload.question)
    db.add(user_msg)
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()

    conv_id = str(conv.id)
    collection = kb.collection_name if kb else None
    kb_name = kb.name if kb else "普通对话"
    kb_id_str = str(kb.id) if kb else None
    question = payload.question
    top_k = payload.top_k
    user_id = user.id

    scope = f"kb={collection}" if collection else f"mode={mode}"

    def event_stream() -> Iterator[str]:
        started = time.perf_counter()
        answer_parts: list[str] = []
        candidates: list[dict] = []
        docs: list = []
        retrieve_ms = 0.0
        llm_ms = 0.0

        try:
            # 普通对话不做检索：省掉一次向量化与一次 pgvector 查询
            if mode == CONVERSATION_MODE_RAG:
                t_retrieve = time.perf_counter()
                docs = rag.retrieve(collection, question, top_k)
                retrieve_ms = (time.perf_counter() - t_retrieve) * 1000
                candidates = rag.to_citations(docs)
                logger.info(
                    "问答检索: %s user=%s top_k=%d 命中 %d 条 (%.0fms) 问题=%r",
                    scope, user.username, top_k, len(docs), retrieve_ms, question[:60],
                )
                if not docs:
                    logger.warning("问答未检索到任何资料: %s 问题=%r", scope, question[:60])

            yield _sse(
                "meta",
                {
                    "conversation_id": conv_id,
                    # 候选资料不是引用；生成结束后由 done 下发最终采用的来源。
                    "citations": [],
                    "mode": mode,
                },
            )

            t_llm = time.perf_counter()
            for token in rag.answer_stream(question, docs, history, mode=mode):
                answer_parts.append(token)
                yield _sse("delta", {"content": token})
            llm_ms = (time.perf_counter() - t_llm) * 1000

            answer = "".join(answer_parts).strip()
            if not answer:
                raise CloudModelError("模型未返回有效内容，请重试")
            citations = rag.finalize_citations(answer, candidates)
            latency = int((time.perf_counter() - started) * 1000)
            logger.info(
                "问答完成: %s user=%s conv=%s 检索 %.0fms + 生成 %.0fms = %.1fs "
                "引用 %d 条 答案 %d 字",
                scope, user.username, conv_id, retrieve_ms, llm_ms,
                latency / 1000, len(citations), len(answer),
            )
            if not answer_parts:
                logger.warning(
                    "模型未返回任何内容: %s 问题=%r 检索条数=%d",
                    scope, question[:60], len(docs),
                )

            # 用独立会话落库，避免流式期间请求会话已关闭
            session: DBSession = SessionLocal()
            try:
                db_user = session.get(User, user_id)
                detail = {"mode": mode, "conversation_id": conv_id}
                if kb_id_str:
                    detail["kb_id"] = kb_id_str
                session.add(
                    Message(
                        conversation_id=uuid.UUID(conv_id),
                        role="assistant",
                        content=answer,
                        # 拒答或未实际引用时存 NULL，和最终流式事件保持一致。
                        citations=citations or None,
                        latency_ms=latency,
                    )
                )
                session.execute(update(Conversation).where(Conversation.id == uuid.UUID(conv_id)).values(updated_at=datetime.now(timezone.utc)))
                session.commit()
                write_audit(session, request, db_user, "chat_query", kb_name, detail)
            finally:
                session.close()

            yield _sse("done", {"latency_ms": latency, "citations": citations})
        except Exception as exc:  # noqa: BLE001 - 流内异常需回传给前端
            logger.exception(
                "问答失败: %s user=%s 问题=%r 已生成 %d 字",
                scope, user.username, question[:60], sum(len(p) for p in answer_parts),
            )
            message = str(exc) if isinstance(exc, CloudModelError) else "问答处理失败，请稍后重试"
            for key in (settings.LLM_API_KEY, settings.EMBEDDING_API_KEY):
                if key:
                    message = message.replace(key, "[已隐藏]")
            yield _sse("error", {"message": message[:500]})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
