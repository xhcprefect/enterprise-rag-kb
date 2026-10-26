"""持久化认领的离线状态机与 SQL 契约；跨连接并发另由隔离 PostgreSQL 测试验证。"""
import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from app.api.v1.chat import ManagedChatResponse
from app.models import ChatTurn, Message
from app.services import chat_turns as turns


class ChatTurnTests(unittest.TestCase):
    def setUp(self):
        self.user_id, self.conv_id, self.request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.kwargs = dict(user_id=self.user_id, conversation_id=self.conv_id, kb_id=None,
                           mode="chat", question="question", top_k=6, request_id=self.request_id)
        self.conv = SimpleNamespace(id=self.conv_id, mode="chat", kb_id=None, active_turn_id=None,
                                    lease_expires_at=None, updated_at=None)
        self.db = MagicMock()

    def claim(self):
        self.db.scalar.side_effect = [None, self.conv]
        return turns.claim_turn(self.db, **self.kwargs)

    def test_claim_atomically_records_request_question_and_active_token(self):
        reservation = self.claim()
        self.assertEqual(reservation.request_id, self.request_id)
        self.assertEqual(self.conv.active_turn_id, reservation.turn_id)
        self.assertGreater(self.conv.lease_expires_at, datetime.now(timezone.utc))
        added = [call.args[0] for call in self.db.add.call_args_list]
        self.assertEqual([type(row) for row in added], [ChatTurn, Message])
        self.assertEqual(added[0].payload_hash, turns.payload_hash("question", "chat", None, 6))
        self.assertEqual(added[1].turn_id, added[0].id)
        self.assertEqual(added[1].status, "processing")
        self.assertIn("pg_advisory_xact_lock", str(self.db.execute.call_args_list[0].args[0]))
        self.db.commit.assert_called_once()

    def test_new_conversation_is_created_inside_the_same_claim(self):
        self.db.scalar.return_value = None
        reservation = turns.claim_turn(self.db, **{**self.kwargs, "conversation_id": None})
        rows = [call.args[0] for call in self.db.add.call_args_list]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].id, reservation.conversation_id)
        self.assertEqual(rows[1].conversation_id, rows[0].id)
        self.db.commit.assert_called_once()

    def test_busy_conversation_rejects_before_saving_a_second_question(self):
        self.conv.active_turn_id = uuid.uuid4()
        self.conv.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        with self.assertRaises(HTTPException) as error:
            self.claim()
        self.assertEqual(error.exception.status_code, 409)
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_expired_claim_is_failed_before_replacing_its_fencing_token(self):
        old = uuid.uuid4()
        self.conv.active_turn_id, self.conv.lease_expires_at = old, datetime.now(timezone.utc) - timedelta(seconds=1)
        reservation = self.claim()
        self.assertNotEqual(reservation.turn_id, old)
        statements = self.db.execute.call_args_list
        self.assertEqual(len(statements), 3)
        self.assertIn("UPDATE chat_turns", str(statements[1].args[0]))
        self.assertEqual(statements[1].args[0].compile().params["status"], "failed")

    def previous(self, status="completed"):
        return SimpleNamespace(id=uuid.uuid4(), request_id=self.request_id, user_id=self.user_id,
                               conversation_id=self.conv_id, user_message_id=uuid.uuid4(), status=status,
                               payload_hash=turns.payload_hash("question", "chat", None, 6), trace={"status": "original"})

    def test_completed_duplicate_replays_without_new_messages_or_generation(self):
        previous = self.previous()
        answer = SimpleNamespace(id=uuid.uuid4(), content="saved", citations=None, latency_ms=12)
        self.db.scalar.side_effect = [previous, answer]
        result = turns.claim_turn(self.db, **self.kwargs)
        self.assertEqual(result.replay_answer, "saved")
        self.assertEqual(result.assistant_message_id, answer.id)
        self.db.add.assert_not_called()
        self.db.commit.assert_called_once()

    def test_deleted_conversation_keeps_a_request_tombstone_without_regeneration(self):
        for status in ["processing", "completed", "failed", "cancelled"]:
            with self.subTest(status=status):
                db = MagicMock()
                previous = self.previous(status)
                previous.conversation_id = None
                db.scalar.return_value = previous
                with self.assertRaises(HTTPException) as error:
                    turns.claim_turn(db, **{**self.kwargs, "conversation_id": None})
                self.assertEqual(error.exception.status_code, 410)
                db.add.assert_not_called()
                db.commit.assert_not_called()

    def test_same_key_cannot_change_question_parameters_or_target_conversation(self):
        for change in [dict(question="different"), dict(top_k=2), dict(conversation_id=uuid.uuid4())]:
            with self.subTest(change=change):
                db = MagicMock()
                db.scalar.return_value = self.previous()
                with self.assertRaises(HTTPException) as error:
                    turns.claim_turn(db, **{**self.kwargs, **change})
                self.assertEqual(error.exception.status_code, 409)
                db.add.assert_not_called()

    def test_processing_failed_and_cancelled_keys_never_reclaim_a_model_call(self):
        for status in ["processing", "failed", "cancelled"]:
            with self.subTest(status=status):
                db = MagicMock()
                db.scalar.return_value = self.previous(status)
                with self.assertRaises(HTTPException):
                    turns.claim_turn(db, **self.kwargs)
                db.add.assert_not_called()

    def test_complete_links_answer_to_the_exact_question_and_releases_lease(self):
        reservation = self.claim()
        run = SimpleNamespace(id=reservation.turn_id, status="processing", trace=None)
        writer = MagicMock()
        writer.scalar.side_effect = [self.conv, run]
        answer_id = turns.complete_turn(writer, reservation, "answer", [], 10, {"status": "original"})
        answer = writer.add.call_args.args[0]
        self.assertEqual(answer.id, answer_id)
        self.assertEqual(answer.reply_to_id, reservation.user_message_id)
        self.assertEqual(answer.turn_id, reservation.turn_id)
        self.assertEqual(run.status, "completed")
        self.assertIsNone(self.conv.active_turn_id)
        writer.commit.assert_called_once()

    def test_stale_or_expired_worker_cannot_persist_an_answer(self):
        reservation = self.claim()
        for active, expiry in [(uuid.uuid4(), datetime.now(timezone.utc) + timedelta(minutes=1)),
                               (reservation.turn_id, datetime.now(timezone.utc) - timedelta(seconds=1))]:
            writer = MagicMock()
            writer.scalar.return_value = SimpleNamespace(active_turn_id=active, lease_expires_at=expiry)
            with self.assertRaises(turns.ChatLeaseLost):
                turns.complete_turn(writer, reservation, "stale answer", [], 1, {})
            writer.add.assert_not_called()
            writer.commit.assert_not_called()

    def test_cancel_is_owner_scoped_and_cannot_release_another_request(self):
        db = MagicMock()
        db.scalar.return_value = None
        with self.assertRaises(HTTPException) as error:
            turns.cancel_request(db, self.request_id, uuid.uuid4())
        self.assertEqual(error.exception.status_code, 404)
        reservation = self.claim()
        db.scalar.side_effect = [self.previous(), SimpleNamespace(active_turn_id=uuid.uuid4())]
        self.assertFalse(turns.cancel_request(db, self.request_id, self.user_id))
        self.assertEqual(self.conv.active_turn_id, reservation.turn_id)

    def test_heartbeat_renews_only_its_unexpired_conversation_and_processing_request(self):
        reservation = self.claim()
        stopped, lost, worker, db = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        stopped.wait.side_effect = [False, True]
        lost.is_set.return_value = False
        db.execute.return_value.rowcount = 1
        def thread_factory(**kwargs):
            worker.start.side_effect = kwargs["target"]
            return worker
        with patch.object(turns.threading, "Event", side_effect=[stopped, lost]), \
             patch.object(turns.threading, "Thread", side_effect=thread_factory), \
             patch.object(turns, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            with turns.keepalive(reservation) as ensure_active:
                ensure_active()
        statements = [call.args[0] for call in db.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn("conversations.active_turn_id =", str(statements[0]))
        self.assertIn("conversations.lease_expires_at >", str(statements[0]))
        self.assertIn("chat_turns.status =", str(statements[1]))
        db.commit.assert_called_once()
        stopped.set.assert_called_once()
        worker.join.assert_called_once()

    def test_failed_heartbeat_match_fences_stream_and_rolls_back_partial_renewal(self):
        reservation = self.claim()
        for counts in [(0,), (1, 0)]:
            with self.subTest(counts=counts):
                stopped, lost, worker, db = MagicMock(), MagicMock(), MagicMock(), MagicMock()
                stopped.wait.return_value = False
                lost.is_set.side_effect = lambda: lost.set.called
                db.execute.side_effect = [SimpleNamespace(rowcount=count) for count in counts]
                def thread_factory(**kwargs):
                    worker.start.side_effect = kwargs["target"]
                    return worker
                with patch.object(turns.threading, "Event", side_effect=[stopped, lost]), \
                     patch.object(turns.threading, "Thread", side_effect=thread_factory), \
                     patch.object(turns, "SessionLocal") as sessions:
                    sessions.return_value.__enter__.return_value = db
                    with turns.keepalive(reservation) as ensure_active, self.assertRaises(turns.ChatLeaseLost):
                        ensure_active()
                db.commit.assert_not_called()
                worker.join.assert_called_once()

    def test_disconnect_before_iteration_still_runs_cleanup(self):
        cleanup = MagicMock()
        response = ManagedChatResponse(iter(["unused"]), cleanup)
        async def send(_message):
            raise OSError("client disconnected")
        async def receive():
            return {"type": "http.disconnect"}
        with self.assertRaises(ClientDisconnect):
            asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        cleanup.assert_called_once()
