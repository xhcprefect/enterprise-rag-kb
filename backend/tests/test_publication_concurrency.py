"""发布、撤销、过期与续期在自有 PostgreSQL 上的真实锁等待回归。"""
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

from fastapi import HTTPException
from sqlalchemy import select, text

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL
from tests._pg_lock_wait import wait_for_blocking
from app.services import chat_turns
from app.services.answer_sources import SOURCE_SNAPSHOT_VERSION

if TEST_URL:
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, engine
    from app.models import ChatTurn, Conversation, Document, DocumentChunk, KnowledgeBase, Message, User

TOKEN = 2
TABLES = "users, knowledge_bases, documents, document_chunks, conversations, chat_turns, messages, audit_logs"


def _empty():
    return {"source_snapshot_version": SOURCE_SNAPSHOT_VERSION, "supplied_sources": [], "history_sources": []}


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离并发测试")
class PublicationConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            user = User(username="conc_user", role="user", is_active=True, password_hash="x", token_version=TOKEN)
            db.add(user)
            db.flush()
            kb = KnowledgeBase(name="conc-kb", collection_name="c_" + uuid.uuid4().hex,
                               owner_id=user.id, is_public=True, kind="upload")
            db.add(kb)
            db.commit()
            self.user_id, self.kb_id = user.id, kb.id

    def add_document(self, *, valid_until=None):
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename="a.txt", file_ext=".txt", file_size=1,
                           stored_path=f"/tmp/{uuid.uuid4().hex}.txt", status="ready",
                           cancel_requested=False, index_version=0, parse_done=True,
                           valid_until=valid_until, chunk_count=1)
            db.add(doc)
            db.flush()
            db.add(DocumentChunk(document_id=doc.id, chunk_index=0, content="正文", embedded=True))
            db.commit()
            return doc.id

    def claim_in(self, db, *, conversation_id=None):
        return chat_turns.claim_turn(db, user_id=self.user_id, conversation_id=conversation_id,
                                    kb_id=self.kb_id, mode="rag", question="问题", top_k=6,
                                    request_id=uuid.uuid4(), auth_token_version=TOKEN)

    def claim(self, *, conversation_id=None):
        with SessionLocal() as db:
            return self.claim_in(db, conversation_id=conversation_id)

    def complete_in(self, db, reservation, trace=None):
        return chat_turns.complete_turn(db, reservation, "回答", [], 1, _empty() if trace is None else trace)

    def complete(self, reservation):
        with SessionLocal() as db:
            return self.complete_in(db, reservation)

    def race(self, hold, act, *, while_blocked=None):
        """只有精确观察 actor 被 holder 阻塞后才提交；任何退出路径均收回两个连接。"""
        armed, actor_ready, release = Event(), Event(), Event()
        pids = {}
        pool = ThreadPoolExecutor(max_workers=2)
        actor = None

        def holder():
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["holder"] = db.scalar(text("SELECT pg_backend_pid()"))
                hold(db)
                armed.set()
                self.assertTrue(release.wait(20), "测试必须有界释放持锁事务")
                db.commit()

        def action():
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["actor"] = db.scalar(text("SELECT pg_backend_pid()"))
                actor_ready.set()
                try:
                    return act(db)
                except (HTTPException, chat_turns.ChatLeaseLost) as exc:
                    db.rollback()
                    return exc

        held = pool.submit(holder)
        try:
            self.assertTrue(armed.wait(8), "holder 未就绪")
            actor = pool.submit(action)
            self.assertTrue(actor_ready.wait(8), "actor 未就绪")
            self.assertTrue(wait_for_blocking(engine, pids["holder"], pids["actor"]),
                            "必须观察到本次两个 PID 的真实阻塞关系")
            self.assertFalse(actor.done())
            if while_blocked:
                while_blocked()
        finally:
            release.set()
            try:
                held.result(timeout=20)
                if actor is not None:
                    actor.result(timeout=20)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        return actor.result()

    def assert_no_answer(self, reservation):
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Message.id).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant")))
            self.assertEqual(db.get(ChatTurn, reservation.turn_id).status, "processing")
            chat_turns.cancel_request(db, reservation.request_id, self.user_id)

    def test_publication_waits_for_document_row_lock_then_revalidates_with_fresh_time(self):
        expiry = datetime.now(timezone.utc) + timedelta(seconds=4)
        document_id = self.add_document(valid_until=expiry)
        reservation = self.claim()
        trace = {"source_snapshot_version": 1, "supplied_sources": [
            {"document_id": str(document_id), "index_version": 0, "chunk_index": 0}], "history_sources": []}

        def hold(db):
            db.scalar(select(Document.id).where(Document.id == document_id).with_for_update())

        def cross_expiry():
            self.assertLess(datetime.now(timezone.utc), expiry, "应在有效期内开始真实等待")
            Event().wait(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.05)

        result = self.race(hold, lambda db: self.complete_in(db, reservation, trace), while_blocked=cross_expiry)
        self.assertIsInstance(result, chat_turns.ChatLeaseLost)
        self.assert_no_answer(reservation)

    def test_token_revocation_racing_publication_never_leaves_a_successful_answer(self):
        reservation = self.claim()

        def hold(db):
            user = db.scalar(select(User).where(User.id == self.user_id).with_for_update())
            user.token_version += 1
            db.flush()

        self.assertIsInstance(self.race(hold, lambda db: self.complete_in(db, reservation)), chat_turns.ChatLeaseLost)
        self.assert_no_answer(reservation)

    def test_disable_racing_claim_rejects_without_partial_conversation_or_turn(self):
        def hold(db):
            user = db.scalar(select(User).where(User.id == self.user_id).with_for_update())
            user.is_active = False
            db.flush()

        result = self.race(hold, self.claim_in)
        self.assertIsInstance(result, HTTPException)
        self.assertEqual(result.status_code, 401)
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(ChatTurn.id)))
            self.assertIsNone(db.scalar(select(Conversation.id)))

    def test_reclaim_does_not_fail_a_turn_renewed_while_it_waited_for_the_lock(self):
        reservation = self.claim()
        with engine.begin() as conn:
            conn.execute(text("UPDATE conversations SET lease_expires_at = now() - interval '1 hour' WHERE id = :id"),
                         {"id": reservation.conversation_id})

        def hold(db):
            conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id).with_for_update())
            conv.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
            db.flush()

        self.assertEqual(self.race(hold, chat_turns.reclaim_expired), 0)
        with SessionLocal() as db:
            self.assertEqual(db.get(ChatTurn, reservation.turn_id).status, "processing")
            chat_turns.cancel_request(db, reservation.request_id, self.user_id)

    def test_expired_lease_blocks_publication_after_lock_wait(self):
        reservation = self.claim()

        def hold(db):
            conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id).with_for_update())
            conv.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.flush()

        self.assertIsInstance(self.race(hold, lambda db: self.complete_in(db, reservation)), chat_turns.ChatLeaseLost)
        self.assert_no_answer(reservation)

    def test_claim_and_publication_serialize_without_deadlock(self):
        first = self.claim()
        with ThreadPoolExecutor(max_workers=2) as pool:
            published = pool.submit(self.complete, first)
            second = pool.submit(self.claim, conversation_id=first.conversation_id)
            self.assertIsNotNone(published.result(timeout=20))
            try:
                result = second.result(timeout=20)
            except HTTPException as exc:
                self.assertEqual(exc.status_code, 409)
            else:
                with SessionLocal() as db:
                    chat_turns.cancel_request(db, result.request_id, self.user_id)
        with SessionLocal() as db:
            self.assertEqual(db.get(ChatTurn, first.turn_id).status, "completed")

    def test_concurrent_claims_produce_exactly_one_lease(self):
        first = self.claim()
        with SessionLocal() as db:
            chat_turns.cancel_request(db, first.request_id, self.user_id)
        start = Event()

        def contender():
            self.assertTrue(start.wait(10))
            try:
                return self.claim(conversation_id=first.conversation_id)
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender) for _ in range(2)]
            start.set()
            outcomes = [future.result(timeout=20) for future in futures]
        self.assertEqual(outcomes.count(409), 1)
        successful = [outcome for outcome in outcomes if isinstance(outcome, chat_turns.Reservation)]
        self.assertEqual(len(successful), 1)
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, first.conversation_id).active_turn_id, successful[0].turn_id)
            chat_turns.cancel_request(db, successful[0].request_id, self.user_id)


if __name__ == "__main__":
    unittest.main()
