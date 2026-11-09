"""按 request_id 只读恢复：所属用户、当前权限、完成状态与诊断隔离。"""
import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.api.v1 import chat
from app.services import chat_turns


class RequestStateTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
        self.request_id, self.conv_id, self.turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.conv = SimpleNamespace(id=self.conv_id, user_id=self.user.id, mode="chat", kb_id=None,
                                    document_scope=[], scope_revision=0)
        self.turn = SimpleNamespace(id=self.turn_id, user_id=self.user.id, request_id=self.request_id,
                                    conversation_id=self.conv_id, user_message_id=uuid.uuid4(),
                                    status="completed", trace={"finish_reason": "length", "internal": "private prompt"})
        self.answer = SimpleNamespace(id=uuid.uuid4(), role="assistant", status="completed", turn_id=self.turn_id,
                                      reply_to_id=self.turn.user_message_id, content="已保存的正文[1]", citations=None,
                                      latency_ms=30, created_at=datetime.now(timezone.utc))
        self.db = MagicMock()
        self.db.get.return_value = self.conv
        self.db.scalar.side_effect = [self.turn, self.answer]
        patcher = patch.object(chat_turns, "expire_request")
        self.expire = patcher.start()
        self.addCleanup(patcher.stop)

    def read(self):
        return chat.get_request_state(self.request_id, self.db, self.user).data

    def test_completed_result_includes_original_message_and_truncation_without_diagnostics(self):
        with patch.object(chat.rag, "answer_astream") as model:
            result = self.read()
        self.assertEqual(result.message.id, self.answer.id)
        self.assertEqual(result.message.content, self.answer.content)
        self.assertTrue(result.truncated)
        self.assertTrue(result.message.truncated)
        self.assertEqual(result.finish_reason, "length")
        self.assertIsNone(result.message.citations)  # 普通对话不恢复伪引用
        self.assertNotIn("private prompt", result.model_dump_json())
        model.assert_not_called()
        statement = self.db.scalar.call_args_list[0].args[0]
        self.assertIn("chat_turns.user_id =", str(statement))
        self.assertEqual(statement.compile().params["user_id_1"], self.user.id)

    def test_processing_returns_no_partial_answer_or_trace(self):
        self.turn.status = "processing"
        result = self.read()
        self.assertEqual(result.status, "processing")
        self.assertIsNone(result.message)
        self.assertIsNone(result.error_code)
        self.assertEqual(self.db.scalar.call_count, 1)

    def test_failed_and_cancelled_return_safe_stored_error_only(self):
        for status in ("failed", "cancelled"):
            with self.subTest(status=status):
                self.db.scalar.reset_mock(side_effect=True)
                self.db.scalar.return_value = self.turn
                self.turn.status = status
                self.turn.trace = {"error_code": "idle_timeout", "error_message": "正文停顿超时", "usage": {"input_tokens": 99}}
                result = self.read()
                self.assertEqual(result.status, status)
                self.assertEqual(result.error_code, "idle_timeout")
                self.assertIsNone(result.message)
                self.assertNotIn("usage", result.model_dump_json())

    def test_unknown_or_other_users_request_is_404_even_for_admin(self):
        self.db.scalar.side_effect = None
        self.db.scalar.return_value = None
        self.user.is_admin = True
        with self.assertRaises(HTTPException) as fault:
            self.read()
        self.assertEqual(fault.exception.status_code, 404)
        self.assertIn("chat_turns.user_id =", str(self.db.scalar.call_args.args[0]))

    def test_deleted_conversation_tombstone_is_410(self):
        self.turn.conversation_id = None
        with self.assertRaises(HTTPException) as fault:
            self.read()
        self.assertEqual(fault.exception.status_code, 410)
        self.expire.assert_not_called()

    def test_current_knowledge_base_permission_is_checked_before_returning_saved_answer(self):
        self.conv.mode, self.conv.kb_id = "rag", uuid.uuid4()
        with patch.object(chat, "_get_kb", side_effect=HTTPException(403, "无权访问该知识库")) as access:
            with self.assertRaises(HTTPException) as fault:
                self.read()
        self.assertEqual(fault.exception.status_code, 403)
        access.assert_called_once_with(self.db, self.conv.kb_id, self.user)
        self.expire.assert_not_called()

    def test_completed_without_saved_answer_is_410_and_never_regenerates(self):
        self.db.scalar.side_effect = [self.turn, None]
        with patch.object(chat.rag, "answer_astream") as model:
            with self.assertRaises(HTTPException) as fault:
                self.read()
        self.assertEqual(fault.exception.status_code, 410)
        model.assert_not_called()

    def test_restored_citations_are_finalized_against_saved_body(self):
        self.conv.mode, self.conv.kb_id = "rag", uuid.uuid4()
        self.answer.citations = [{"source_index": 1, "filename": "kept.txt"}, {"source_index": 2, "filename": "unused.txt"}]
        with patch.object(chat, "_get_kb"):
            result = self.read()
        self.assertEqual([item.source_index for item in result.message.citations], [1])

    def test_cancel_commits_before_sending_local_fast_cancel(self):
        order = []
        with patch.object(chat_turns, "cancel_request", side_effect=lambda *_: order.append("commit")), \
             patch.object(chat.active_streams, "cancel", side_effect=lambda *_: order.append("signal")):
            chat.cancel_request(self.request_id, self.db, self.user)
        self.assertEqual(order, ["commit", "signal"])

    def test_replay_metadata_obeys_frame_limit_without_regenerating(self):
        from fastapi import Request
        from app.core.config import settings
        from app.schemas.chat import ChatRequest
        self.user.is_admin = True
        reservation = chat_turns.Reservation(self.turn_id, self.request_id, self.conv_id,
            self.turn.user_message_id, self.user.id, replay_answer="saved" * 1800,
            replay_trace={"finish_reason": "stop", "large_diagnostic": "x" * 20000}, assistant_message_id=self.answer.id,
            auth_token_version=7, mode="chat")
        async def consume(response):
            return [frame async for frame in response.body_iterator]
        with patch.object(chat_turns, "claim_turn", return_value=reservation), \
             patch.object(settings, "CHAT_STREAM_MAX_FRAME_BYTES", 16384), \
             patch.object(chat.rag, "answer_astream") as model:
            response = chat.chat_stream(ChatRequest(mode="chat", question="q", conversation_id=self.conv_id, include_trace=True),
                Request({"type": "http", "headers": []}), self.db, self.user)
            frames = asyncio.run(consume(response))
        self.assertTrue(all(len(frame.encode()) <= 16384 for frame in frames))
        self.assertIn('"code": "stream_limit"', frames[-1])
        model.assert_not_called()

    def test_lease_check_uses_owner_current_access_and_unexpired_processing_token(self):
        from app.services import answer_sources as sources
        reservation = chat_turns.Reservation(self.turn_id, self.request_id, self.conv_id,
                                             self.turn.user_message_id, self.user.id,
                                             auth_token_version=7, mode="chat")
        db = MagicMock()
        conv = SimpleNamespace(id=self.conv_id, user_id=self.user.id, mode="chat", kb_id=None,
                               active_turn_id=self.turn_id,
                               lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                               project_snapshot_id=None, scope_revision=0, document_scope=[])
        turn = SimpleNamespace(id=self.turn_id, user_id=self.user.id, request_id=self.request_id,
                               conversation_id=self.conv_id, user_message_id=self.turn.user_message_id,
                               status="processing", project_snapshot_id=None, heartbeat_at=None)
        db.scalar.side_effect = [conv, turn]
        root = sources.SourceRoot(
            user=SimpleNamespace(id=self.user.id, is_active=True, token_version=7, is_admin=False),
            knowledge_base=None)
        with patch.object(sources, "lock_root", return_value=root), \
             patch.object(chat_turns, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            self.assertTrue(chat_turns.check_lease(reservation, True))
        # 根锁复核锁定后的当前用户令牌与 ACL；会话/轮次锁后复核身份与租约。
        self.assertEqual(root.user.token_version, reservation.auth_token_version)
        for statement, clause in [(db.scalar.call_args_list[0].args[0], "conversations.id ="),
                                  (db.scalar.call_args_list[1].args[0], "chat_turns.id =")]:
            self.assertIn(clause, str(statement))
            self.assertIn("FOR UPDATE", str(statement))
        self.assertEqual(conv.lease_expires_at - datetime.now(timezone.utc) > timedelta(seconds=0), True)
        db.commit.assert_called_once()

    def test_lease_renewal_partial_match_rolls_back(self):
        from app.services import answer_sources as sources
        reservation = chat_turns.Reservation(self.turn_id, self.request_id, self.conv_id,
                                             self.turn.user_message_id, self.user.id,
                                             auth_token_version=7, mode="chat")
        db = MagicMock()
        # 活跃轮次与租约正确，但轮次身份漂移（request_id 错）→ 复核失败 → 回滚。
        conv = SimpleNamespace(id=self.conv_id, user_id=self.user.id, mode="chat", kb_id=None,
                               active_turn_id=self.turn_id,
                               lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                               project_snapshot_id=None, scope_revision=0, document_scope=[])
        turn = SimpleNamespace(id=self.turn_id, user_id=self.user.id, request_id=uuid.uuid4(),
                               conversation_id=self.conv_id, user_message_id=self.turn.user_message_id,
                               status="processing", project_snapshot_id=None, heartbeat_at=None)
        db.scalar.side_effect = [conv, turn]
        root = sources.SourceRoot(
            user=SimpleNamespace(id=self.user.id, is_active=True, token_version=7, is_admin=False),
            knowledge_base=None)
        with patch.object(sources, "lock_root", return_value=root), \
             patch.object(chat_turns, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            self.assertFalse(chat_turns.check_lease(reservation, True))
        db.commit.assert_not_called()
