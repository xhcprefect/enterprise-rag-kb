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
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import ChatTurn, Conversation, Message

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


def _now():
    return datetime.now(timezone.utc)


def payload_hash(question: str, mode: str, kb_id, top_k: int) -> str:
    data = json.dumps([question, mode, str(kb_id) if kb_id else None, top_k], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _request_lock(user_id: uuid.UUID, request_id: uuid.UUID) -> int:
    return int.from_bytes(hashlib.sha256(f"chat:{user_id}:{request_id}".encode()).digest()[:8], "big", signed=True)


def _expire_active(db: Session, conv: Conversation) -> None:
    if conv.active_turn_id is not None:
        db.execute(update(ChatTurn).where(ChatTurn.id == conv.active_turn_id, ChatTurn.status == "processing").values(status="failed"))
        db.execute(update(Message).where(Message.turn_id == conv.active_turn_id, Message.status == "processing").values(status="failed"))
    conv.active_turn_id, conv.lease_expires_at = None, None


def claim_turn(db: Session, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None,
               kb_id: uuid.UUID | None, mode: str, question: str, top_k: int,
               request_id: uuid.UUID | None = None) -> Reservation:
    """只在短事务内认领；同键重放不新建会话/消息，也不再次调用模型。"""
    key = request_id or uuid.uuid4()
    digest = payload_hash(question, mode, kb_id, top_k)
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
                            title=question.strip()[:30] or "新会话")
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
    return Reservation(identifier, key, conv.id, message_id, user_id)


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
                  latency_ms: int, trace: dict) -> uuid.UUID:
    conv, turn = _active_locked(db, reservation)
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


def _terminate(db: Session, turn_id: uuid.UUID, conversation_id: uuid.UUID, status: str) -> bool:
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None or conv.active_turn_id != turn_id:
        return False
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == turn_id)
                     .with_for_update().execution_options(populate_existing=True))
    if turn is None or turn.status != "processing":
        return False
    turn.status = status
    db.execute(update(Message).where(Message.turn_id == turn_id, Message.status == "processing").values(status=status))
    conv.active_turn_id, conv.lease_expires_at, conv.updated_at = None, None, _now()
    return True


def end_turn(reservation: Reservation, status: str = "cancelled") -> None:
    """完成后的清理是空操作；断连、异常和旧工作线程都不能覆盖更新的认领。"""
    try:
        with SessionLocal() as db:
            _terminate(db, reservation.turn_id, reservation.conversation_id, status)
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
