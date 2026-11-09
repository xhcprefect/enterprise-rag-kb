"""持久化认领/发布/租约的离线状态机与锁顺序契约；真实并发见隔离 PostgreSQL 测试。"""
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
from app.services import answer_sources
from app.services import chat_turns as turns

VERSION = 3


def fake_root(user_id, kb=None, *, version=VERSION, active=True, admin=False, owner_id=None):
    user = SimpleNamespace(id=user_id, is_active=active, token_version=version, is_admin=admin)
    if kb is not None and owner_id is None:
        owner_id = user_id
    if kb is not None:
        kb.owner_id = owner_id
    return answer_sources.SourceRoot(user=user, knowledge_base=kb)


def fake_kb(kind="upload", *, public=True, owner_id=None):
    return SimpleNamespace(id=uuid.uuid4(), kind=kind, is_public=public,
                           owner_id=owner_id or uuid.uuid4(), collection_name="c")


class ReservationShapeTests(unittest.TestCase):
    def test_positional_fields_are_preserved_and_new_fields_append_at_tail(self):
        turn_id, request_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        message_id, user_id, answer_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        reservation = turns.Reservation(turn_id, request_id, conv_id, message_id, user_id,
                                        "answer", [{"document_id": str(uuid.uuid4())}], 12,
                                        {"status": "ok"}, answer_id, [{"document_id": str(uuid.uuid4())}],
                                        4, uuid.uuid4(), VERSION, "rag", uuid.uuid4())
        self.assertEqual((reservation.turn_id, reservation.request_id, reservation.conversation_id),
                         (turn_id, request_id, conv_id))
        self.assertEqual((reservation.user_message_id, reservation.user_id), (message_id, user_id))
        self.assertEqual((reservation.replay_answer, reservation.replay_latency_ms), ("answer", 12))
        self.assertEqual(reservation.assistant_message_id, answer_id)
        self.assertEqual(reservation.scope_revision, 4)
        self.assertEqual(reservation.auth_token_version, VERSION)
        self.assertEqual(reservation.mode, "rag")


class ClaimTurnTests(unittest.TestCase):
    def setUp(self):
        self.user_id, self.conv_id = uuid.uuid4(), uuid.uuid4()
        self.request_id = uuid.uuid4()
        self.kwargs = dict(user_id=self.user_id, conversation_id=self.conv_id, kb_id=None,
                           mode="chat", question="question", top_k=6, request_id=self.request_id)
        self.conv = SimpleNamespace(id=self.conv_id, user_id=self.user_id, mode="chat", kb_id=None,
                                    active_turn_id=None, lease_expires_at=None, updated_at=None,
                                    document_scope=[], scope_revision=0, project_snapshot_id=None)
        self.db = MagicMock()

    def claim(self, db=None, root=None, **overrides):
        db = db or self.db
        with patch.object(answer_sources, "lock_root", return_value=root or fake_root(self.user_id)):
            return turns.claim_turn(db, **{**self.kwargs, "auth_token_version": VERSION, **overrides})

    def test_claim_records_request_question_and_active_token(self):
        self.db.scalar.side_effect = [None, self.conv]
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
        self.assertEqual((reservation.auth_token_version, reservation.mode), (VERSION, "chat"))
        self.db.commit.assert_called_once()

    def test_new_conversation_is_created_inside_the_same_claim(self):
        self.db.scalar.side_effect = [None]
        reservation = self.claim(conversation_id=None)
        rows = [call.args[0] for call in self.db.add.call_args_list]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].id, reservation.conversation_id)
        self.assertEqual(rows[1].conversation_id, rows[0].id)
        self.db.commit.assert_called_once()

    def test_another_users_conversation_cannot_be_claimed(self):
        self.conv.user_id = uuid.uuid4()
        self.db.scalar.side_effect = [None, self.conv]
        with self.assertRaises(HTTPException) as error:
            self.claim()
        self.assertEqual(error.exception.status_code, 404)
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_busy_conversation_rejects_before_saving_a_second_question(self):
        self.conv.active_turn_id = uuid.uuid4()
        self.conv.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
        self.db.scalar.side_effect = [None, self.conv]
        with self.assertRaises(HTTPException) as error:
            self.claim()
        self.assertEqual(error.exception.status_code, 409)
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_expired_claim_is_failed_before_replacing_its_fencing_token(self):
        old = uuid.uuid4()
        self.conv.active_turn_id = old
        self.conv.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        old_turn = SimpleNamespace(id=old, status="processing")
        old_message = SimpleNamespace(id=uuid.uuid4(), status="processing")
        self.db.scalar.side_effect = [None, self.conv, old_turn]
        self.db.scalars.return_value.all.return_value = [old_message]
        reservation = self.claim()
        self.assertNotEqual(reservation.turn_id, old)
        self.assertEqual((old_turn.status, old_message.status), ("failed", "failed"))
        self.assertIn("FOR UPDATE", str(self.db.scalar.call_args_list[-1].args[0]))
        self.assertIn("FROM messages", str(self.db.scalars.call_args.args[0]))

    def test_missing_or_malformed_token_version_is_rejected_before_any_write(self):
        for bad in (None, True, False, "3", 3.0, -1):
            with self.subTest(bad=bad):
                db = MagicMock()
                db.scalar.side_effect = [None, self.conv]
                with self.assertRaises(HTTPException) as error:
                    turns.claim_turn(db, **self.kwargs, auth_token_version=bad)
                self.assertEqual(error.exception.status_code, 401)
                db.add.assert_not_called()
                db.commit.assert_not_called()

    def test_stale_token_or_disabled_user_is_rejected(self):
        for root in (fake_root(self.user_id, version=VERSION + 1),
                     fake_root(self.user_id, active=False)):
            with self.subTest(root=root):
                db = MagicMock()
                db.scalar.side_effect = [None, self.conv]
                with self.assertRaises(HTTPException) as error:
                    self.claim(db, root=root)
                self.assertEqual(error.exception.status_code, 401)
                db.add.assert_not_called()

    def test_lost_kb_acl_rejects_claim(self):
        kb = fake_kb("upload", public=False)
        db = MagicMock()
        db.scalar.return_value = None
        with self.assertRaises(HTTPException) as error:
            self.claim(db, root=fake_root(self.user_id, kb), kb_id=kb.id, mode="rag")
        self.assertEqual(error.exception.status_code, 404)
        db.add.assert_not_called()

    def test_plain_chat_rejects_any_document_scope_or_project_binding(self):
        for change in (dict(document_ids=[]), dict(scope_revision=0), dict(project_snapshot_id=uuid.uuid4())):
            with self.subTest(change=change):
                db = MagicMock()
                with self.assertRaises(HTTPException) as error:
                    self.claim(db, **change)
                self.assertEqual(error.exception.status_code, 400)
                db.commit.assert_not_called()

    def test_unknown_mode_is_rejected(self):
        db = MagicMock()
        with self.assertRaises(HTTPException) as error:
            self.claim(db, mode="other")
        self.assertEqual(error.exception.status_code, 400)

    def test_project_snapshot_on_upload_kb_is_rejected(self):
        kb = fake_kb("upload")
        db = MagicMock()
        db.scalar.return_value = None
        with self.assertRaises(HTTPException) as error:
            self.claim(db, root=fake_root(self.user_id, kb), kb_id=kb.id, mode="rag",
                       project_snapshot_id=uuid.uuid4())
        self.assertEqual(error.exception.status_code, 400)

    def test_project_binding_is_locked_before_the_conversation_row(self):
        kb = fake_kb("project")
        events, snapshot_id = [], uuid.uuid4()
        db = MagicMock()
        db.scalar.side_effect = [None, self.conv, self.conv]
        self.conv.project_snapshot_id = snapshot_id
        binding = SimpleNamespace(snapshots={snapshot_id: SimpleNamespace(source_id=uuid.uuid4())},
                                  sources={})
        with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id, kb)), \
             patch.object(turns.project_chat_scope, "plan_claim_project",
                          side_effect=lambda *a, **k: (events.append("plan"), snapshot_id)[1]), \
             patch.object(turns.project_chat_scope, "lock_claim_binding",
                          side_effect=lambda *a, **k: (events.append("binding"), binding)[1]), \
             patch.object(turns.project_chat_scope, "verify_claim_binding",
                          side_effect=lambda *a, **k: events.append("verify")):
            self.conv.mode, self.conv.kb_id = "rag", kb.id
            turns.claim_turn(db, **{**self.kwargs, "mode": "rag", "kb_id": kb.id,
                                    "auth_token_version": VERSION})
        self.assertEqual(events, ["plan", "binding", "verify"])
        self.assertEqual(self.conv.project_snapshot_id, snapshot_id)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.user_id, self.conv_id, self.request_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.kwargs = dict(user_id=self.user_id, conversation_id=self.conv_id, kb_id=None,
                           mode="chat", question="question", top_k=6, request_id=self.request_id)
        self.conv = SimpleNamespace(id=self.conv_id, user_id=self.user_id, mode="chat", kb_id=None,
                                    document_scope=[{"document_id": str(uuid.uuid4())}],
                                    scope_revision=2, project_snapshot_id=None)
        self.db = MagicMock()

    def previous(self, status="completed", **overrides):
        values = dict(id=uuid.uuid4(), request_id=self.request_id, user_id=self.user_id,
                      conversation_id=self.conv_id, user_message_id=uuid.uuid4(), status=status,
                      payload_hash=turns.payload_hash("question", "chat", None, 6),
                      trace={"status": "original"}, project_snapshot_id=None,
                      project_snapshot_turn_id=None)
        values.update(overrides)
        return SimpleNamespace(**values)

    def replay(self, previous, answer=None, **overrides):
        answer = answer or SimpleNamespace(id=uuid.uuid4(), content="saved", citations=None, latency_ms=12)
        answer.role, answer.status = "assistant", "completed"
        answer.reply_to_id, answer.conversation_id = previous.user_message_id, self.conv_id
        answer.turn_id = previous.id
        question = SimpleNamespace(id=previous.user_message_id, role="user", status="completed",
                                   conversation_id=self.conv_id, turn_id=previous.id)
        self.db.scalar.side_effect = [previous, self.conv, previous]
        self.db.scalars.return_value.all.return_value = [question, answer]
        with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id)):
            return turns.claim_turn(self.db, **{**self.kwargs, "auth_token_version": VERSION, **overrides})

    def test_completed_duplicate_replays_without_new_messages_or_generation(self):
        answer = SimpleNamespace(id=uuid.uuid4(), content="saved", citations=None, latency_ms=12)
        result = self.replay(self.previous(), answer)
        self.assertEqual(result.replay_answer, "saved")
        self.assertEqual(result.assistant_message_id, answer.id)
        self.assertEqual((result.mode, result.kb_id), ("chat", None))
        self.db.add.assert_not_called()
        self.db.commit.assert_called_once()

    def test_deleted_conversation_keeps_a_request_tombstone_without_regeneration(self):
        for status in ["processing", "completed", "failed", "cancelled"]:
            with self.subTest(status=status):
                db = MagicMock()
                previous = self.previous(status, conversation_id=None)
                db.scalar.return_value = previous
                with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id)), \
                        self.assertRaises(HTTPException) as error:
                    turns.claim_turn(db, **{**self.kwargs, "conversation_id": None,
                                            "auth_token_version": VERSION})
                self.assertEqual(error.exception.status_code, 410)
                db.add.assert_not_called()
                db.commit.assert_not_called()

    def test_same_key_cannot_change_question_parameters_or_target_conversation(self):
        for change in [dict(question="different"), dict(top_k=2), dict(conversation_id=uuid.uuid4())]:
            with self.subTest(change=change):
                db = MagicMock()
                db.scalar.return_value = self.previous()
                with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id)), \
                        self.assertRaises(HTTPException) as error:
                    turns.claim_turn(db, **{**self.kwargs, **change, "auth_token_version": VERSION})
                self.assertEqual(error.exception.status_code, 409)
                db.add.assert_not_called()

    def test_processing_failed_and_cancelled_keys_never_reclaim_a_model_call(self):
        for status in ["processing", "failed", "cancelled"]:
            with self.subTest(status=status):
                db = MagicMock()
                previous = self.previous(status)
                db.scalar.side_effect = [previous, self.conv, previous]
                with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id)), \
                        self.assertRaises(HTTPException):
                    turns.claim_turn(db, **self.kwargs, auth_token_version=VERSION)
                db.add.assert_not_called()

    def test_replay_rejects_changed_conversation_configuration(self):
        self.conv.mode = "rag"
        with self.assertRaises(HTTPException) as error:
            self.replay(self.previous())
        self.assertEqual(error.exception.status_code, 409)
        self.db.add.assert_not_called()

    def test_replay_reports_stored_configuration_and_fixed_snapshot(self):
        self.conv.mode = "rag"
        self.kwargs.update(mode="rag")
        previous = self.previous(trace={"status": "original"},
                                 payload_hash=turns.payload_hash("question", "rag", None, 6))
        reservation = self.replay(previous, **{"mode": "rag"})
        self.assertEqual((reservation.mode, reservation.kb_id), ("rag", None))
        self.assertIsNone(reservation.project_snapshot_id)


class CompleteTurnTests(unittest.TestCase):
    def setUp(self):
        self.user_id, self.conv_id = uuid.uuid4(), uuid.uuid4()
        self.turn_id, self.message_id = uuid.uuid4(), uuid.uuid4()
        self.request_id = uuid.uuid4()
        self.db = MagicMock()
        self.reservation = turns.Reservation(self.turn_id, self.request_id, self.conv_id, self.message_id,
                                             self.user_id, auth_token_version=VERSION, mode="chat", kb_id=None)
        self.conv = SimpleNamespace(id=self.conv_id, user_id=self.user_id, mode="chat", kb_id=None,
                                    active_turn_id=self.turn_id,
                                    lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                                    document_scope=[], scope_revision=0, project_snapshot_id=None)
        self.turn = SimpleNamespace(id=self.turn_id, status="processing", trace=None, turn_seq=1,
                                    user_id=self.user_id, request_id=self.request_id, conversation_id=self.conv_id,
                                    user_message_id=self.message_id, project_snapshot_id=None)
        self.question = SimpleNamespace(id=self.message_id, role="user", status="processing",
                                        turn_id=self.turn_id, conversation_id=self.conv_id)
        self.db.scalars.return_value.all.return_value = [self.question]

    def complete(self, trace=None, citations=None, root=None, **options):
        trace = {"source_snapshot_version": 1, "supplied_sources": [], "history_sources": []} if trace is None else trace
        self.db.scalar.side_effect = [self.conv, self.turn]
        with patch.object(answer_sources, "lock_root", return_value=root or fake_root(self.user_id)):
            return turns.complete_turn(self.db, self.reservation, "answer", citations or [], 10, trace, **options)

    def test_complete_links_answer_to_the_exact_question_and_releases_lease(self):
        answer_id = self.complete()
        answer = self.db.add.call_args.args[0]
        self.assertEqual(answer.id, answer_id)
        self.assertEqual(answer.reply_to_id, self.message_id)
        self.assertEqual(answer.turn_id, self.turn_id)
        self.assertEqual(self.turn.status, "completed")
        self.assertEqual(self.turn.trace["source_snapshot_version"], 1)
        self.assertIsNone(self.conv.active_turn_id)
        self.db.commit.assert_called_once()

    def test_missing_or_malformed_trace_is_rejected_before_locking_or_writing(self):
        cases = [None, {}, {"supplied_sources": [], "history_sources": []},
                 {"source_snapshot_version": 1, "supplied_sources": None, "history_sources": []},
                 {"source_snapshot_version": 1, "supplied_sources": "x", "history_sources": []},
                 {"source_snapshot_version": True, "supplied_sources": [], "history_sources": []}]
        for trace in cases:
            with self.subTest(trace=trace):
                db = MagicMock()
                with patch.object(answer_sources, "lock_root") as root:
                    with self.assertRaises(turns.ChatLeaseLost):
                        turns.complete_turn(db, self.reservation, "answer", [], 1, trace)
                root.assert_not_called()
                db.add.assert_not_called()
                db.commit.assert_not_called()

    def test_plain_chat_requires_explicit_empty_snapshot_and_never_acquires_sources(self):
        with self.assertRaises(turns.ChatLeaseLost):
            self.complete(trace={})
        self.db.add.assert_not_called()
        self.db.reset_mock()
        with patch.object(answer_sources, "validate_sources") as validate, \
             patch.object(answer_sources, "lock_document_ids") as lock_docs:
            self.complete()
        validate.assert_not_called()
        lock_docs.assert_not_called()

    def test_stale_or_expired_worker_cannot_persist_an_answer(self):
        for active, expiry in [(uuid.uuid4(), datetime.now(timezone.utc) + timedelta(minutes=1)),
                               (self.turn_id, datetime.now(timezone.utc) - timedelta(seconds=1))]:
            with self.subTest(active=active):
                db = MagicMock()
                db.scalar.side_effect = [SimpleNamespace(id=self.conv_id, active_turn_id=active,
                                                         lease_expires_at=expiry)]
                with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id)), \
                        self.assertRaises(turns.ChatLeaseLost):
                    turns.complete_turn(db, self.reservation, "stale answer", [], 1,
                                        {"source_snapshot_version": 1,
                                         "supplied_sources": [], "history_sources": []})
                db.add.assert_not_called()
                db.commit.assert_not_called()

    def test_revoked_token_blocks_publication_even_with_empty_citations(self):
        db = MagicMock()
        with patch.object(answer_sources, "lock_root",
                          return_value=fake_root(self.user_id, version=VERSION + 1)), \
                self.assertRaises(turns.ChatLeaseLost):
            turns.complete_turn(db, self.reservation, "拒绝回答。", [], 1,
                                {"source_snapshot_version": 1, "supplied_sources": [], "history_sources": []})
        db.add.assert_not_called()
        db.commit.assert_not_called()

    def test_supplied_document_source_is_locked_after_message_and_revalidated_with_fresh_now(self):
        document_id, kb = uuid.uuid4(), fake_kb("upload", public=True)
        reservation = turns.Reservation(self.turn_id, self.request_id, self.conv_id, self.message_id,
                                        self.user_id, auth_token_version=VERSION, mode="rag", kb_id=kb.id)
        self.conv.mode, self.conv.kb_id = "rag", kb.id
        self.turn.project_snapshot_id = None
        source = {"document_id": str(document_id), "index_version": 0, "chunk_index": 0}
        trace = {"source_snapshot_version": 1, "supplied_sources": [source], "history_sources": []}
        events = {}
        self.db.scalar.side_effect = [self.conv, self.turn]

        def lock_docs(db, kb_arg, ids):
            events["docs"] = (db, list(ids))

        def validate(db, kb_arg, sources, *, user, lock, binding, now):
            events["validate"] = (list(sources), lock, binding, now)
            return tuple(sources)

        with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id, kb)), \
             patch.object(turns, "_lock_turn_messages", side_effect=lambda db, tid: (events.setdefault("messages", tid), [self.question])[1]), \
             patch.object(answer_sources, "lock_document_ids", side_effect=lock_docs), \
             patch.object(answer_sources, "validate_sources", side_effect=validate):
            turns.complete_turn(self.db, reservation, "answer", [], 1, trace)
        self.assertEqual(events["docs"][1], [document_id])
        self.assertEqual(events["messages"], self.turn_id)
        self.assertEqual(events["validate"][1], False)
        self.assertIsNotNone(events["validate"][3].tzinfo, "now 必须是 aware UTC")
        self.assertLessEqual(abs((datetime.now(timezone.utc) - events["validate"][3]).total_seconds()), 5)

    def test_scope_and_provenance_documents_are_locked_in_one_sorted_batch(self):
        kb = fake_kb("upload")
        scope_doc, provenance_doc = uuid.uuid4(), uuid.uuid4()
        reservation = turns.Reservation(self.turn_id, self.request_id, self.conv_id, self.message_id,
                                        self.user_id,
                                        document_scope=[{"document_id": str(scope_doc), "index_version": 0}],
                                        scope_revision=1, auth_token_version=VERSION, mode="rag", kb_id=kb.id)
        self.conv.mode, self.conv.kb_id = "rag", kb.id
        self.conv.document_scope, self.conv.scope_revision = reservation.document_scope, reservation.scope_revision
        self.db.scalar.side_effect = [self.conv, self.turn]
        trace = {"source_snapshot_version": 1,
                 "supplied_sources": [{"document_id": str(provenance_doc), "index_version": 0,
                                       "chunk_index": 0}], "history_sources": []}
        with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id, kb)), \
             patch.object(turns, "_lock_turn_messages", return_value=[self.question]), \
             patch.object(turns.document_scopes, "validate_scope", return_value=reservation.document_scope), \
             patch.object(answer_sources, "lock_document_ids") as lock_docs, \
             patch.object(answer_sources, "validate_sources", return_value=()):
            turns.complete_turn(self.db, reservation, "a", [], 1, trace)
        self.assertEqual(sorted(lock_docs.call_args.args[2]), sorted([scope_doc, provenance_doc]))

    def test_source_verification_failure_never_saves_a_successful_answer(self):
        kb = fake_kb("upload")
        reservation = turns.Reservation(self.turn_id, self.request_id, self.conv_id, self.message_id,
                                        self.user_id, auth_token_version=VERSION, mode="rag", kb_id=kb.id)
        self.conv.mode, self.conv.kb_id = "rag", kb.id
        self.db.scalar.side_effect = [self.conv, self.turn]
        trace = {"source_snapshot_version": 1,
                 "supplied_sources": [{"document_id": str(uuid.uuid4()), "index_version": 0,
                                       "chunk_index": 0}], "history_sources": []}
        with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id, kb)), \
             patch.object(turns, "_lock_turn_messages", return_value=[self.question]), \
             patch.object(answer_sources, "lock_document_ids"), \
             patch.object(answer_sources, "validate_sources",
                          side_effect=answer_sources.SourceVerificationError("gone")):
            with self.assertRaises(turns.ChatLeaseLost):
                turns.complete_turn(self.db, reservation, "a", [], 1, trace)
        self.db.add.assert_not_called()
        self.db.commit.assert_not_called()

    def test_turn_identity_mismatch_is_rejected(self):
        self.turn.request_id = uuid.uuid4()
        with self.assertRaises(turns.ChatLeaseLost):
            self.complete()
        self.db.add.assert_not_called()


class TerminateAndLeaseTests(unittest.TestCase):
    def setUp(self):
        self.user_id, self.conv_id, self.turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.reservation = turns.Reservation(self.turn_id, uuid.uuid4(), self.conv_id, uuid.uuid4(),
                                             self.user_id, auth_token_version=VERSION, mode="chat", kb_id=None)

    def test_cancel_is_owner_scoped_and_statuses_suffix_order(self):
        db = MagicMock()
        db.scalar.return_value = None
        with self.assertRaises(HTTPException) as error:
            turns.cancel_request(db, uuid.uuid4(), uuid.uuid4())
        self.assertEqual(error.exception.status_code, 404)
        conv = SimpleNamespace(active_turn_id=self.turn_id, lease_expires_at=None)
        turn = SimpleNamespace(status="processing", trace=None)
        db.scalar.side_effect = [SimpleNamespace(id=self.turn_id, conversation_id=self.conv_id), conv, turn]
        db.scalars.return_value.all.return_value = [SimpleNamespace(status="processing")]
        self.assertTrue(turns.cancel_request(db, uuid.uuid4(), self.user_id))
        self.assertEqual(turn.status, "cancelled")

    def test_keepalive_reuses_auth_aware_check_instead_of_blind_updates(self):
        stopped, lost, worker = MagicMock(), MagicMock(), MagicMock()
        stopped.wait.side_effect = [False, True]
        lost.is_set.return_value = False
        def thread_factory(**kwargs):
            worker.start.side_effect = kwargs["target"]
            return worker
        with patch.object(turns.threading, "Event", side_effect=[stopped, lost]), \
             patch.object(turns.threading, "Thread", side_effect=thread_factory), \
             patch.object(turns, "check_lease", return_value=True) as check:
            with turns.keepalive(self.reservation) as ensure_active:
                ensure_active()
        check.assert_called_once_with(self.reservation, renew=True)
        lost.set.assert_not_called()
        stopped.set.assert_called_once()
        worker.join.assert_called_once()

    def test_keepalive_marks_lost_when_renewal_check_fails(self):
        stopped, lost, worker = MagicMock(), MagicMock(), MagicMock()
        stopped.wait.return_value = False
        lost.is_set.return_value = False
        def thread_factory(**kwargs):
            worker.start.side_effect = kwargs["target"]
            return worker
        with patch.object(turns.threading, "Event", side_effect=[stopped, lost]), \
             patch.object(turns.threading, "Thread", side_effect=thread_factory), \
             patch.object(turns, "check_lease", return_value=False):
            with turns.keepalive(self.reservation):
                pass
        lost.set.assert_called_once()

    def test_check_lease_checks_token_acl_and_fresh_deadline(self):
        db = MagicMock()
        db.scalar.side_effect = [self.conv_row(), self.turn_row(), self.conv_row(), self.turn_row()]
        db.scalars.return_value = []
        with patch.object(answer_sources, "lock_root", return_value=fake_root(self.user_id)), \
             patch.object(turns, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            self.assertTrue(turns.check_lease(self.reservation, True))
        db.commit.assert_called_once()

    def conv_row(self, *, active=None, expires=None):
        return SimpleNamespace(id=self.conv_id, active_turn_id=active or self.turn_id,
                               lease_expires_at=expires or datetime.now(timezone.utc) + timedelta(minutes=5),
                               user_id=self.user_id, mode="chat", kb_id=None,
                               scope_revision=0, project_snapshot_id=None)

    def turn_row(self):
        return SimpleNamespace(id=self.turn_id, status="processing", user_id=self.user_id,
                               conversation_id=self.conv_id, request_id=self.reservation.request_id,
                               user_message_id=self.reservation.user_message_id, project_snapshot_id=None)

    def test_check_lease_rejects_revoked_token(self):
        with patch.object(answer_sources, "lock_root",
                          return_value=fake_root(self.user_id, version=VERSION + 1)), \
             patch.object(turns, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = MagicMock()
            self.assertFalse(turns.check_lease(self.reservation, True))

    def test_disconnect_before_iteration_still_runs_cleanup(self):
        cleanup = MagicMock()
        response = ManagedChatResponse(iter(["unused"]), cleanup)
        async def send(_message):
            raise OSError("client disconnected")
        async def receive():
            return {"type": "http/disconnect"}
        with self.assertRaises(ClientDisconnect):
            asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send))
        cleanup.assert_called_once()


if __name__ == "__main__":
    unittest.main()