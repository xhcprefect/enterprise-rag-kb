"""API 契约测试的事务替身；状态机与真实 SQL 另有专用测试。"""
import uuid
from contextlib import contextmanager
from unittest.mock import patch

from app.models import Message
from app.services import chat_turns


@contextmanager
def keepalive(_reservation):
    yield lambda: None


def async_items(items):
    """离线适配器替身：每次调用返回全新的异步生成器。"""
    async def generate(*_args, **_kwargs):
        for item in items:
            yield item
    return generate


def async_factory(factory):
    async def generate(*args, **kwargs):
        for item in factory(*args, **kwargs):
            yield item
    return generate


def mock_turns(stack, conversation_id, writer, *, document_scope=None, scope_revision=0):
    writer.__enter__.return_value = writer

    def claim(db, **kwargs):
        turn_id, message_id = uuid.uuid4(), uuid.uuid4()
        db.add(Message(id=message_id, conversation_id=conversation_id, role="user", content=kwargs["question"],
                       turn_id=turn_id, status="processing"))
        return chat_turns.Reservation(turn_id, kwargs.get("request_id") or uuid.uuid4(), conversation_id,
                                      message_id, kwargs["user_id"], document_scope=document_scope or [],
                                      scope_revision=scope_revision)

    def complete(db, reservation, answer, citations, latency, trace, **_options):
        identifier = uuid.uuid4()
        db.add(Message(id=identifier, conversation_id=conversation_id, role="assistant", content=answer,
                       turn_id=reservation.turn_id, reply_to_id=reservation.user_message_id,
                       status="completed", citations=citations or None, latency_ms=latency))
        db.commit()
        return identifier

    stack.enter_context(patch.object(chat_turns, "claim_turn", side_effect=claim))
    stack.enter_context(patch.object(chat_turns, "complete_turn", side_effect=complete))
    stack.enter_context(patch.object(chat_turns, "end_turn"))
    stack.enter_context(patch.object(chat_turns, "check_lease", return_value=True))
    stack.enter_context(patch.object(chat_turns, "keepalive", side_effect=keepalive))
