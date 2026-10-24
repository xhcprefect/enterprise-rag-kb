import json
import logging
import time
import uuid
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, write_audit
from app.db.session import SessionLocal, get_db
from app.models import Conversation, KnowledgeBase, Message, User
from app.schemas.chat import ChatRequest, ConversationDetail, ConversationRead
from app.schemas.common import ApiResponse
from app.services import rag

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
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = select(Conversation).where(Conversation.user_id == user.id)
    if kb_id:
        stmt = stmt.where(Conversation.kb_id == kb_id)
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
    return ApiResponse(data=_get_conversation(db, conversation_id, user))


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


@router.post("/stream", summary="流式问答（SSE，返回 text/event-stream，不套统一结构）")
def chat_stream(
    payload: ChatRequest,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    kb = _get_kb(db, payload.kb_id, user)

    # 准备/创建会话
    if payload.conversation_id:
        conv = _get_conversation(db, payload.conversation_id, user)
    else:
        title = payload.question.strip()[:30] or "新会话"
        conv = Conversation(user_id=user.id, kb_id=kb.id, title=title)
        db.add(conv)
        db.commit()
        db.refresh(conv)

    history_rows = db.scalars(
        select(Message)
        .where(Message.conversation_id == conv.id)
        .order_by(Message.created_at.asc())
    ).all()
    history = [{"role": m.role, "content": m.content} for m in history_rows]

    # 先落库用户消息
    user_msg = Message(conversation_id=conv.id, role="user", content=payload.question)
    db.add(user_msg)
    db.commit()

    conv_id = str(conv.id)
    collection = kb.collection_name
    question = payload.question
    top_k = payload.top_k
    user_id = user.id

    def event_stream() -> Iterator[str]:
        started = time.perf_counter()
        answer_parts: list[str] = []
        citations: list[dict] = []
        retrieve_ms = 0.0
        llm_ms = 0.0

        try:
            t_retrieve = time.perf_counter()
            docs = rag.retrieve(collection, question, top_k)
            retrieve_ms = (time.perf_counter() - t_retrieve) * 1000
            citations = rag.to_citations(docs)
            logger.info(
                "问答检索: kb=%s user=%s top_k=%d 命中 %d 条 (%.0fms) 问题=%r",
                collection, user.username, top_k, len(docs), retrieve_ms, question[:60],
            )
            if not docs:
                logger.warning("问答未检索到任何资料: kb=%s 问题=%r", collection, question[:60])

            yield _sse(
                "meta",
                {
                    "conversation_id": conv_id,
                    "citations": citations,
                },
            )

            t_llm = time.perf_counter()
            for token in rag.answer_stream(question, docs, history):
                answer_parts.append(token)
                yield _sse("delta", {"content": token})
            llm_ms = (time.perf_counter() - t_llm) * 1000

            answer = "".join(answer_parts).strip() or "抱歉，未能生成回答。"
            latency = int((time.perf_counter() - started) * 1000)
            logger.info(
                "问答完成: kb=%s user=%s conv=%s 检索 %.0fms + 生成 %.0fms = %.1fs "
                "引用 %d 条 答案 %d 字",
                collection, user.username, conv_id, retrieve_ms, llm_ms,
                latency / 1000, len(citations), len(answer),
            )
            if not answer_parts:
                logger.warning(
                    "模型未返回任何内容: kb=%s 问题=%r 检索条数=%d",
                    collection, question[:60], len(docs),
                )

            # 用独立会话落库，避免流式期间请求会话已关闭
            session: DBSession = SessionLocal()
            try:
                db_user = session.get(User, user_id)
                session.add(
                    Message(
                        conversation_id=uuid.UUID(conv_id),
                        role="assistant",
                        content=answer,
                        citations=citations,
                        latency_ms=latency,
                    )
                )
                session.commit()
                write_audit(
                    session,
                    request,
                    db_user,
                    "chat_query",
                    kb.name,
                    {"kb_id": str(kb.id), "conversation_id": conv_id},
                )
            finally:
                session.close()

            yield _sse("done", {"latency_ms": latency})
        except Exception as exc:  # noqa: BLE001 - 流内异常需回传给前端
            logger.exception(
                "问答失败: kb=%s user=%s 问题=%r 已生成 %d 字",
                collection, user.username, question[:60], sum(len(p) for p in answer_parts),
            )
            yield _sse("error", {"message": str(exc)[:500]})

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
