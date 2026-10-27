"""持久化问答认领、幂等重放和租约隔离；网络期间不持有数据库行锁。"""
import hashlib
import json
import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import ChatTurn, Conversation, KnowledgeBase, Message, User
from app.services import document_scopes

logger = logging.getLogger(__name__)


class ChatLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    turn_id: uuid.UUID
    request_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    user_id: uuid.UUID
    replay_answer: str | None = None
    replay_citations: list[dict] = field(default_factory=list)
    replay_latency_ms: int = 0
    replay_trace: dict = field(default_factory=dict)
    assistant_message_id: uuid.UUID | None = None
    document_scope: list[dict] = field(default_factory=list)
    scope_revision: int = 0


def _now():
    return datetime.now(timezone.utc)


def payload_hash(question: str, mode: str, kb_id, top_k: int, *, document_ids=None,
                 scope_revision: int | None = None) -> str:
    values = [question, mode, str(kb_id) if kb_id else None, top_k]
    # 旧调用保留原摘要；只有显式提供的范围参数参与幂等身份。
    scope_request = {}
    if document_ids is not None:
        scope_request["document_ids"] = sorted(str(value) for value in document_scopes.scope_ids(document_ids))
    if scope_revision is not None:
        scope_request["scope_revision"] = scope_revision
    if scope_request:
        values.append(scope_request)
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _request_lock(user_id: uuid.UUID, request_id: uuid.UUID) -> int:
    return int.from_bytes(hashlib.sha256(f"chat:{user_id}:{request_id}".encode()).digest()[:8], "big", signed=True)


def _expire_active(db: Session, conv: Conversation) -> None:
    if conv.active_turn_id is not None:
        db.execute(update(ChatTurn).where(ChatTurn.id == conv.active_turn_id, ChatTurn.status == "processing").values(status="failed"))
        db.execute(update(Message).where(Message.turn_id == conv.active_turn_id, Message.status == "processing").values(status="failed"))
    conv.active_turn_id, conv.lease_expires_at = None, None


def reclaim_expired(db: Session) -> int:
    """回收租约已过期的在途问答；返回处理的会话数。

    正常路径由 ``_expire_active`` 在**同一会话**再次认领时清理。但若进程在生成中途
    被杀（部署、OOM、竞价实例回收），那轮会永远停在 processing：没有并发续期线程，
    也就不会有人再碰这个会话，``/chat/conversations`` 会一直显示「回答中」。
    这里在启动时按租约全局兜底一次，与 ``_expire_active`` 的判定口径保持一致。
    """
    now = _now()
    conv_ids = db.scalars(select(Conversation.id).where(
        Conversation.active_turn_id.is_not(None),
        or_(Conversation.lease_expires_at.is_(None), Conversation.lease_expires_at <= now),
    )).all()
    if not conv_ids:
        return 0
    # 先记下要回收的轮次，后面只动这些行：不能顺带改到该会话里更早失败的历史记录。
    turn_ids = db.scalars(select(ChatTurn.id).where(
        ChatTurn.conversation_id.in_(conv_ids), ChatTurn.status == "processing",
    )).all()
    messages = db.execute(update(Message).where(
        Message.turn_id.in_(turn_ids), Message.status == "processing",
    ).values(status="failed")).rowcount if turn_ids else 0
    turns = db.execute(update(ChatTurn).where(
        ChatTurn.id.in_(turn_ids), ChatTurn.status == "processing",
    ).values(status="failed")).rowcount if turn_ids else 0
    # 整行批量更新，避免身份映射里留着旧的 active_turn_id。
    db.execute(update(Conversation).where(Conversation.id.in_(conv_ids))
               .values(active_turn_id=None, lease_expires_at=None))
    db.commit()
    logger.warning("回收了 %d 个租约过期的在途会话（问答 %d 条、消息 %d 条标记为失败）",
                   len(conv_ids), turns, messages)
    return len(conv_ids)


def claim_turn(db: Session, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None,
               kb_id: uuid.UUID | None, mode: str, question: str, top_k: int,
               request_id: uuid.UUID | None = None, document_ids=None,
               scope_revision: int | None = None, reset_scope: bool = False) -> Reservation:
    """只在短事务内认领；同键重放不新建会话/消息，也不再次调用模型。"""
    if mode != "rag" and (document_ids is not None or scope_revision is not None):
        raise HTTPException(400, "普通对话不支持文档范围")
    key = request_id or uuid.uuid4()
    digest = payload_hash(question, mode, kb_id, top_k, document_ids=document_ids, scope_revision=scope_revision)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _request_lock(user_id, key)})
    previous = db.scalar(select(ChatTurn).where(ChatTurn.user_id == user_id, ChatTurn.request_id == key))
    if previous is not None:
        if previous.conversation_id is None:
            # 删除会话后保留去重记录：迟到的重试不能重新建会话或再次调用模型。
            raise HTTPException(410, "原会话已删除，该 request_id 不可复用，请使用新的 request_id")
        if previous.payload_hash != digest or (conversation_id and previous.conversation_id != conversation_id):
            raise HTTPException(409, "同一 request_id 不能用于不同的问题、参数或会话")
        if previous.status != "completed":
            message = "该请求仍在处理中，请等待或刷新会话" if previous.status == "processing" else "该请求已结束且未完成，请使用新的 request_id 重试"
            raise HTTPException(409, message)
        answer = db.scalar(select(Message).where(Message.turn_id == previous.id, Message.role == "assistant", Message.status == "completed"))
        if answer is None:
            raise HTTPException(409, "原请求结果已不可用，请使用新的 request_id")
        result = Reservation(previous.id, key, previous.conversation_id, previous.user_message_id, user_id,
                             answer.content, answer.citations or [], answer.latency_ms, previous.trace or {}, answer.id)
        db.commit()
        return result

    if conversation_id is None:
        conv = Conversation(id=uuid.uuid4(), user_id=user_id, kb_id=kb_id, mode=mode,
                            title=question.strip()[:30] or "新会话", document_scope=[], scope_revision=0)
        db.add(conv)
        db.flush()
    else:
        conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                         .with_for_update().execution_options(populate_existing=True))
        if conv is None:
            raise HTTPException(404, "会话不存在")
        if conv.mode != mode or conv.kb_id != kb_id:
            raise HTTPException(409, "会话配置已改变，请刷新后重试")
    now = _now()
    if conv.active_turn_id is not None:
        if conv.lease_expires_at and conv.lease_expires_at > now:
            raise HTTPException(409, "该会话已有回答正在生成，请等待完成或停止后重试")
        _expire_active(db, conv)

    current_scope = getattr(conv, "document_scope", [])
    current_revision = getattr(conv, "scope_revision", None) or 0
    if scope_revision is not None and scope_revision != current_revision:
        raise HTTPException(409, "文档范围已在其他页面改变，请刷新会话后重试")
    if document_ids is not None:
        if conversation_id is not None:
            raise HTTPException(400, "已有会话请先通过范围选择器更新文档范围")
        current_scope = document_scopes.resolve_scope(db, kb_id, document_ids, lock=True)
        conv.document_scope = current_scope
        current_revision = conv.scope_revision = int(bool(current_scope))
    elif not reset_scope and (current_scope or not isinstance(current_scope, list)):
        current_scope = document_scopes.validate_scope(db, kb_id, current_scope, lock=True)

    identifier, message_id = uuid.uuid4(), uuid.uuid4()
    db.add(ChatTurn(id=identifier, request_id=key, user_id=user_id, conversation_id=conv.id,
                    user_message_id=message_id, payload_hash=digest, status="processing", heartbeat_at=now))
    db.flush()
    db.add(Message(id=message_id, conversation_id=conv.id, turn_id=identifier, role="user",
                   content=question, status="processing"))
    conv.active_turn_id = identifier
    conv.lease_expires_at = now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)
    conv.updated_at = now
    db.commit()
    return Reservation(identifier, key, conv.id, message_id, user_id,
                       document_scope=current_scope, scope_revision=current_revision)


def set_document_scope(db: Session, conversation_id: uuid.UUID, document_ids,
                       expected_revision: int | None = None) -> tuple[list[dict], int]:
    """调用方先检查会话和知识库权限；与认领/完成共用会话行锁。"""
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        raise HTTPException(404, "会话不存在")
    if conv.mode != "rag":
        raise HTTPException(400, "普通对话不支持文档范围")
    if conv.active_turn_id is not None:
        if conv.lease_expires_at and conv.lease_expires_at > _now():
            raise HTTPException(409, "回答正在生成，请停止或等待完成后再调整文档范围")
        _expire_active(db, conv)
    revision = conv.scope_revision or 0
    if expected_revision is not None and revision != expected_revision:
        raise HTTPException(409, "文档范围已在其他页面改变，请刷新会话后重试")
    selected = document_scopes.resolve_scope(db, conv.kb_id, document_ids, lock=True)
    if selected != (conv.document_scope or []):
        conv.document_scope, conv.scope_revision, conv.updated_at = selected, revision + 1, _now()
        revision += 1
    db.commit()
    return selected, revision


def _active_locked(db: Session, reservation: Reservation):
    conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None or conv.active_turn_id != reservation.turn_id or not conv.lease_expires_at or conv.lease_expires_at <= _now():
        raise ChatLeaseLost("该回答已取消或处理租约已失效，请刷新会话后重试")
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == reservation.turn_id)
                     .with_for_update().execution_options(populate_existing=True))
    if turn is None or turn.status != "processing":
        raise ChatLeaseLost("该回答已结束，请刷新会话")
    return conv, turn


def complete_turn(db: Session, reservation: Reservation, answer: str, citations: list[dict],
                  latency_ms: int, trace: dict, *, scope_update: list[dict] | None = None) -> uuid.UUID:
    conv, turn = _active_locked(db, reservation)
    if (getattr(conv, "scope_revision", None) or 0) != reservation.scope_revision:
        raise ChatLeaseLost("文档范围已改变，请刷新会话后重试")
    selected = reservation.document_scope if scope_update is None else scope_update
    if selected:
        # 生成期间发生删除/取消/重建时，禁止将旧代次结果写成成功回答。
        selected = document_scopes.validate_scope(db, conv.kb_id, selected, lock=True)
    if scope_update is not None and selected != (getattr(conv, "document_scope", None) or []):
        conv.document_scope = selected
        conv.scope_revision = reservation.scope_revision + 1
    identifier = uuid.uuid4()
    db.add(Message(id=identifier, conversation_id=conv.id, turn_id=turn.id,
                   reply_to_id=reservation.user_message_id, status="completed", role="assistant",
                   content=answer, citations=citations or None, latency_ms=latency_ms))
    db.execute(update(Message).where(Message.id == reservation.user_message_id,
                                    Message.turn_id == turn.id).values(status="completed"))
    turn.status, turn.trace = "completed", trace
    conv.active_turn_id, conv.lease_expires_at, conv.updated_at = None, None, _now()
    db.commit()
    return identifier


def _terminate(db: Session, turn_id: uuid.UUID, conversation_id: uuid.UUID, status: str, *,
               error_code: str | None = None, error_message: str | None = None) -> bool:
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None or conv.active_turn_id != turn_id:
        return False
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == turn_id)
                     .with_for_update().execution_options(populate_existing=True))
    if turn is None or turn.status != "processing":
        return False
    turn.status = status
    if error_code:
        turn.trace = {"error_code": error_code, "error_message": (error_message or "请求已结束")[:500]}
    db.execute(update(Message).where(Message.turn_id == turn_id, Message.status == "processing").values(status=status))
    conv.active_turn_id, conv.lease_expires_at, conv.updated_at = None, None, _now()
    return True


def end_turn(reservation: Reservation, status: str = "cancelled", *,
             error_code: str | None = None, error_message: str | None = None) -> None:
    """完成后的清理是空操作；断连、异常和旧工作线程都不能覆盖更新的认领。"""
    try:
        with SessionLocal() as db:
            _terminate(db, reservation.turn_id, reservation.conversation_id, status,
                       error_code=error_code, error_message=error_message)
            db.commit()
    except Exception as exc:
        logger.warning("问答状态清理失败，将由租约到期恢复: %s", type(exc).__name__)


def clear_conversation_requests(db: Session, conversation_ids) -> None:
    """调用方锁住会话后，在删除事务内清除诊断、终止活跃请求，保留去重身份。"""
    ids = tuple(conversation_ids)
    if not ids:
        return
    db.execute(update(ChatTurn).where(ChatTurn.conversation_id.in_(ids)).values(trace=None))
    db.execute(update(ChatTurn).where(ChatTurn.conversation_id.in_(ids),
                                     ChatTurn.status == "processing").values(status="cancelled"))


def cancel_request(db: Session, request_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    turn = db.scalar(select(ChatTurn).where(ChatTurn.request_id == request_id, ChatTurn.user_id == user_id))
    if turn is None:
        raise HTTPException(404, "请求不存在")
    changed = _terminate(db, turn.id, turn.conversation_id, "cancelled")
    db.commit()
    return changed


def expire_request(db: Session, turn: ChatTurn) -> None:
    """读取请求状态时回收崩溃遗留的 processing，不影响同会话的新租约。"""
    if turn.status != "processing" or turn.conversation_id is None:
        return
    conv = db.scalar(select(Conversation).where(Conversation.id == turn.conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        return
    if conv.active_turn_id == turn.id and conv.lease_expires_at and conv.lease_expires_at > _now():
        db.commit()
        return
    active = conv.active_turn_id == turn.id
    current = db.scalar(select(ChatTurn).where(ChatTurn.id == turn.id)
                        .with_for_update().execution_options(populate_existing=True))
    if current is not None and current.status == "processing":
        current.status = "failed"
        current.trace = {"error_code": "lease_lost", "error_message": "处理租约已失效，请使用新请求重试"}
        db.execute(update(Message).where(Message.turn_id == turn.id, Message.status == "processing")
                   .values(status="failed"))
        if active:
            conv.active_turn_id, conv.lease_expires_at = None, None
    db.commit()


def check_lease(reservation: Reservation, renew: bool = False) -> bool:
    """短事务检查/续期；跨进程取消以数据库为准，不为每路问答创建常驻线程。"""
    with SessionLocal() as db:
        now = _now()
        valid = db.scalar(select(ChatTurn.id).join(
            Conversation, Conversation.id == ChatTurn.conversation_id,
        ).join(User, User.id == ChatTurn.user_id).outerjoin(
            KnowledgeBase, KnowledgeBase.id == Conversation.kb_id,
        ).where(
            Conversation.id == reservation.conversation_id,
            Conversation.active_turn_id == reservation.turn_id,
            Conversation.lease_expires_at > now,
            ChatTurn.id == reservation.turn_id, ChatTurn.status == "processing",
            User.id == reservation.user_id, User.is_active.is_(True),
            or_(Conversation.mode == "chat", User.role == "admin",
                KnowledgeBase.is_public.is_(True), KnowledgeBase.owner_id == reservation.user_id),
        ))
        if valid is None:
            return False
        if renew:
            updated = db.execute(update(Conversation).where(
                Conversation.id == reservation.conversation_id,
                Conversation.active_turn_id == reservation.turn_id,
                Conversation.lease_expires_at > now,
            ).values(lease_expires_at=now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)))
            turn = db.execute(update(ChatTurn).where(
                ChatTurn.id == reservation.turn_id, ChatTurn.status == "processing",
            ).values(heartbeat_at=now))
            if not updated.rowcount or not turn.rowcount:
                return False  # context manager rolls back a partial renewal
            db.commit()
        return True


@contextmanager
def keepalive(reservation: Reservation):
    stopped, lost = threading.Event(), threading.Event()
    interval = max(1.0, min(30.0, settings.CHAT_LEASE_SECONDS / 3))

    def renew():
        while not stopped.wait(interval):
            try:
                with SessionLocal() as db:
                    now = _now()
                    updated = db.execute(update(Conversation).where(
                        Conversation.id == reservation.conversation_id,
                        Conversation.active_turn_id == reservation.turn_id,
                        Conversation.lease_expires_at > now,
                    ).values(lease_expires_at=now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)))
                    if not updated.rowcount:
                        lost.set()
                        return
                    turn = db.execute(update(ChatTurn).where(ChatTurn.id == reservation.turn_id,
                                                             ChatTurn.status == "processing").values(heartbeat_at=now))
                    if not turn.rowcount:
                        lost.set()
                        return
                    db.commit()
            except Exception as exc:
                logger.warning("问答租约续期失败: %s", type(exc).__name__)

    thread = threading.Thread(target=renew, name=f"chat-heartbeat-{reservation.turn_id}", daemon=True)
    thread.start()
    try:
        def ensure_active():
            if lost.is_set():
                raise ChatLeaseLost("该回答已取消或已被新的请求替代")
        yield ensure_active
    finally:
        stopped.set()
        thread.join(timeout=2)
