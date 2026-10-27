"""真实 PostgreSQL 的流式恢复、权限、超时与跨进程取消回归；不请求真实模型。"""
import asyncio
import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url

from tests._env import TEST_URL
from tests.test_database import _bind_test_engine

_bind_test_engine()

from app.api.v1 import chat
from app.core.config import settings
from app.core.security import create_access_token
from app.db.init_db import init_db
from app.db.session import SessionLocal, engine
from app.models import ChatTurn, Conversation, KnowledgeBase, Message, User
from app.schemas.chat import ChatRequest
from app.services import chat_turns, rag


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离流式数据库测试")
class StreamDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL) or not engine.url.database.endswith("_test"):
            raise RuntimeError("拒绝在业务库运行流式回归")
        init_db(create_admin=False)
        with patch("app.core.logging.setup_logging"):
            from app.main import app
        cls.app = app

    def setUp(self):
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE users, knowledge_bases, conversations, model_usage RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            self.user = User(username="stream_user", role="user", is_active=True, password_hash="test-only")
            self.other = User(username="stream_admin", role="admin", is_active=True, password_hash="test-only")
            db.add_all([self.user, self.other])
            db.flush()
            self.kb = KnowledgeBase(name="stream_test", collection_name="stream_" + uuid.uuid4().hex,
                                    is_public=True, owner_id=self.other.id)
            db.add(self.kb)
            db.commit()
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer " + create_access_token(str(self.user.id), "user")}
        self.key = uuid.uuid4()
        self.payload = {"question": "synthetic question", "mode": "chat", "request_id": str(self.key)}
        self.request = Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": []})

    def state(self, headers=None):
        return self.client.get(f"/api/v1/chat/requests/{self.key}", headers=headers or self.headers)

    def complete(self, *, rag_mode=False):
        async def generate(*_args, **kwargs):
            kwargs["metrics"]["finish_reason"] = "length"
            yield "synthetic saved answer"
        payload = {**self.payload, "mode": "rag", "kb_id": str(self.kb.id)} if rag_mode else self.payload
        with patch.object(rag, "answer_astream", side_effect=generate) as model, patch.object(rag, "retrieve", return_value=[]):
            result = self.client.post("/api/v1/chat/stream", headers=self.headers, json=payload)
        self.assertIn("event: done", result.text)
        return result, model

    def test_read_recovers_saved_answer_when_done_is_not_received_and_never_generates_again(self):
        original, model = self.complete()
        # 假设浏览器丢失了最后的 done，只持有发送前生成的 request_id。
        with patch.object(rag, "answer_astream") as later_model:
            recovered = self.state()
            replay = self.client.post("/api/v1/chat/stream", headers=self.headers, json=self.payload)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        state = recovered.json()["data"]
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["message"]["content"], "synthetic saved answer")
        self.assertEqual(state["finish_reason"], "length")
        self.assertTrue(state["message"]["truncated"])
        self.assertIn('"replayed": true', replay.text)
        self.assertIn('"truncated": true', replay.text)
        model.assert_called_once()
        later_model.assert_not_called()
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ChatTurn)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 2)
        detail = self.client.get(f'/api/v1/chat/conversations/{state["conversation_id"]}', headers=self.headers).json()["data"]
        self.assertTrue(detail["messages"][-1]["truncated"])

    def test_request_state_is_owner_only_even_for_administrators(self):
        self.complete()
        other_headers = {"Authorization": "Bearer " + create_access_token(str(self.other.id), "admin")}
        result = self.state(other_headers)
        self.assertEqual(result.status_code, 404)
        self.assertNotIn("synthetic saved answer", result.text)

    def test_recovery_rechecks_current_kb_permission(self):
        self.complete(rag_mode=True)
        with SessionLocal() as db:
            db.execute(update(KnowledgeBase).where(KnowledgeBase.id == self.kb.id).values(is_public=False))
            db.commit()
        result = self.state()
        self.assertEqual(result.status_code, 403)
        self.assertNotIn("synthetic saved answer", result.text)

    def test_deleted_conversation_returns_tombstone_and_cannot_regenerate(self):
        self.complete()
        state = self.state().json()["data"]
        self.assertEqual(self.client.delete(f'/api/v1/chat/conversations/{state["conversation_id"]}', headers=self.headers).status_code, 200)
        self.assertEqual(self.state().status_code, 410)
        with patch.object(rag, "answer_astream") as model:
            replay = self.client.post("/api/v1/chat/stream", headers=self.headers, json=self.payload)
        self.assertEqual(replay.status_code, 410)
        model.assert_not_called()

    def test_expired_processing_request_becomes_failed_on_read(self):
        with SessionLocal() as db:
            reservation = chat_turns.claim_turn(db, user_id=self.user.id, conversation_id=None, kb_id=None,
                                                mode="chat", question="expired", top_k=6, request_id=self.key)
            db.execute(update(Conversation).where(Conversation.id == reservation.conversation_id)
                       .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            db.commit()
        result = self.state()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["data"]["status"], "failed")
        self.assertEqual(result.json()["data"]["error_code"], "lease_lost")
        with SessionLocal() as db:
            self.assertIsNone(db.get(Conversation, reservation.conversation_id).active_turn_id)
            self.assertEqual(db.scalar(select(Message)).status, "failed")

    def test_reading_legacy_stale_request_does_not_release_newer_claim(self):
        with SessionLocal() as db:
            old = chat_turns.claim_turn(db, user_id=self.user.id, conversation_id=None, kb_id=None,
                                       mode="chat", question="old", top_k=6, request_id=self.key)
            db.execute(update(Conversation).where(Conversation.id == old.conversation_id)
                       .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            db.commit()
            new = chat_turns.claim_turn(db, user_id=self.user.id, conversation_id=old.conversation_id, kb_id=None,
                                       mode="chat", question="new", top_k=6)
            db.execute(update(ChatTurn).where(ChatTurn.id == old.turn_id).values(status="processing"))
            db.commit()
        self.assertEqual(self.state().json()["data"]["status"], "failed")
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, new.conversation_id).active_turn_id, new.turn_id)
            self.assertEqual(db.get(ChatTurn, new.turn_id).status, "processing")

    def test_first_body_timeout_persists_failure_closes_source_and_releases_lease(self):
        closed = []
        async def stalled(*_args, **_kwargs):
            try:
                await asyncio.sleep(10)
                yield "never"
            finally:
                closed.append(True)
        with patch.object(rag, "answer_astream", side_effect=stalled), \
             patch.object(settings, "CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", 0.15), \
             patch.object(settings, "CHAT_STREAM_HEARTBEAT_SECONDS", 0.02):
            result = self.client.post("/api/v1/chat/stream", headers=self.headers, json=self.payload)
        self.assertIn(": ping", result.text)
        self.assertIn('"code": "first_token_timeout"', result.text)
        self.assertNotIn("event: done", result.text)
        self.assertEqual(closed, [True])
        state = self.state().json()["data"]
        self.assertEqual((state["status"], state["error_code"]), ("failed", "first_token_timeout"))
        self.assertIsNone(state["message"])
        with SessionLocal() as db:
            self.assertIsNone(db.get(Conversation, uuid.UUID(state["conversation_id"])).active_turn_id)

    def test_cross_process_cancel_while_model_is_stalled_cannot_save_answer(self):
        closed = []
        async def exercise():
            entered = asyncio.Event()
            async def stalled(*_args, **_kwargs):
                try:
                    entered.set()
                    await asyncio.sleep(10)
                    yield "late answer"
                finally:
                    closed.append(True)
            with SessionLocal() as db, patch.object(rag, "answer_astream", side_effect=stalled), \
                 patch.object(settings, "CHAT_STREAM_CANCEL_POLL_SECONDS", 0.01):
                response = chat.chat_stream(ChatRequest(**self.payload), self.request, db, self.user)
                async def consume():
                    return "".join([frame async for frame in response.body_iterator])
                task = asyncio.create_task(consume())
                await asyncio.wait_for(entered.wait(), 2)
                def cancel_from_another_process():
                    with SessionLocal() as other_db:
                        # 故意不发本地 active_streams 信号，验证数据库轮询能停止上游。
                        chat_turns.cancel_request(other_db, self.key, self.user.id)
                await asyncio.to_thread(cancel_from_another_process)
                return await asyncio.wait_for(task, 2)
        output = asyncio.run(exercise())
        self.assertIn("event: error", output)
        self.assertNotIn("event: done", output)
        self.assertEqual(closed, [True])
        self.assertEqual(self.state().json()["data"]["status"], "cancelled")
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Message).where(Message.role == "assistant")), 0)
