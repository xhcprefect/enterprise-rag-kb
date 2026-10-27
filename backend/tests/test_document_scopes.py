"""文档范围验证、SQL 过滤、并发版本和请求幂等；不访问外部服务。"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import chat
from app.schemas.chat import DocumentScopeUpdate
from app.services import chat_turns, document_scopes as scopes


class DocumentScopeTests(unittest.TestCase):
    def setUp(self):
        self.kb_id, self.identifier = uuid.uuid4(), uuid.uuid4()
        self.item = {"document_id": str(self.identifier), "filename": "手册.pdf", "index_version": 2}
        self.doc = SimpleNamespace(id=self.identifier, kb_id=self.kb_id, filename="服务器原名.pdf",
                                   status="ready", cancel_requested=False, index_version=2)
        self.conv = SimpleNamespace(id=uuid.uuid4(), kb_id=self.kb_id, mode="rag", user_id=uuid.uuid4(),
                                    document_scope=[self.item], scope_revision=3,
                                    active_turn_id=None, lease_expires_at=None)
        self.db = MagicMock()
        self.db.scalars.return_value.all.return_value = [self.doc]
        self.db.scalar.return_value = self.conv

    def test_ids_are_canonical_deduplicated_and_bounded(self):
        self.assertEqual(scopes.scope_ids([str(self.identifier).upper(), self.identifier]), [self.identifier])
        for value in [None, "invalid", ["invalid"], [uuid.uuid4() for _ in range(6)]]:
            with self.subTest(value=value), self.assertRaises(HTTPException):
                scopes.scope_ids(value)

    def test_resolution_uses_database_names_and_both_kb_and_identity(self):
        result = scopes.resolve_scope(self.db, self.kb_id, [self.identifier])
        self.assertEqual(result[0]["filename"], self.doc.filename)
        sql = str(self.db.scalars.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.assertIn(str(self.kb_id), sql)
        self.assertIn(str(self.identifier), sql)
        self.assertIn("documents.kb_id =", sql)

    def test_deleted_foreign_unready_cancelled_and_reindexed_scopes_fail_closed(self):
        self.db.scalars.return_value.all.return_value = []
        with self.assertRaises(HTTPException) as error:
            scopes.validate_scope(self.db, self.kb_id, [self.item])
        self.assertEqual(error.exception.status_code, 404)
        self.db.scalars.return_value.all.return_value = [self.doc]
        for changes in [dict(status="processing"), dict(cancel_requested=True), dict(index_version=3)]:
            before = vars(self.doc).copy()
            self.doc.__dict__.update(changes)
            with self.subTest(changes=changes), self.assertRaises(HTTPException) as error:
                scopes.validate_scope(self.db, self.kb_id, [self.item])
            self.assertEqual(error.exception.status_code, 409)
            self.doc.__dict__.update(before)

    def test_corrupt_scope_and_boolean_versions_are_rejected(self):
        for value in [None, {}, "", [None], [{**self.item, "index_version": True}],
                      [{**self.item, "index_version": -1}], [self.item, self.item]]:
            with self.subTest(value=value), self.assertRaises(HTTPException):
                scopes.validate_scope(self.db, self.kb_id, value)
        self.assertFalse(scopes.contains([self.item], {**self.item, "index_version": True}))

    def test_sql_empty_scope_is_false_and_nonempty_scope_matches_id_and_generation(self):
        self.assertEqual(str(scopes.scope_filter([])), "false")
        sql = str(scopes.scope_filter([self.item]).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.assertIn(str(self.identifier), sql)
        self.assertIn("documents.index_version = 2", sql)

    def test_patch_locks_conversation_and_increments_revision_without_touching_messages(self):
        selected, revision = chat_turns.set_document_scope(self.db, self.conv.id, [self.identifier], 3)
        self.assertEqual(revision, 4)
        self.assertEqual(selected[0]["filename"], self.doc.filename)
        self.assertEqual(self.conv.document_scope, selected)
        self.assertIn("FOR UPDATE", str(self.db.scalar.call_args.args[0]))
        self.db.add.assert_not_called()
        self.db.commit.assert_called_once()

    def test_clear_can_recover_a_deleted_scope_without_requiring_the_old_document(self):
        selected, revision = chat_turns.set_document_scope(self.db, self.conv.id, [], 3)
        self.assertEqual((selected, revision), ([], 4))
        self.db.scalars.assert_not_called()

    def test_busy_patch_and_stale_revision_reject_before_replacing_scope(self):
        for busy in [False, True]:
            self.conv.active_turn_id = uuid.uuid4() if busy else None
            self.conv.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)
            with self.subTest(busy=busy), self.assertRaises(HTTPException) as error:
                chat_turns.set_document_scope(self.db, self.conv.id, [], 2 if not busy else 3)
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(self.conv.document_scope, [self.item])
        self.db.commit.assert_not_called()

    def test_patch_rechecks_current_knowledge_base_permission(self):
        user = SimpleNamespace(id=self.conv.user_id)
        with patch.object(chat, "_get_conversation", return_value=self.conv), \
             patch.object(chat, "_get_kb", side_effect=HTTPException(403, "无权访问")), \
             patch.object(chat_turns, "set_document_scope") as write:
            with self.assertRaises(HTTPException):
                chat.set_document_scope(self.conv.id, DocumentScopeUpdate(document_ids=[]), self.db, user)
        write.assert_not_called()

    def test_scope_is_in_request_identity_only_when_explicitly_provided(self):
        old = chat_turns.payload_hash("q", "rag", self.kb_id, 6)
        same = chat_turns.payload_hash("q", "rag", self.kb_id, 6, document_ids=None, scope_revision=None)
        self.assertEqual(old, same)
        self.assertNotEqual(old, chat_turns.payload_hash("q", "rag", self.kb_id, 6, document_ids=[]))
        self.assertNotEqual(old, chat_turns.payload_hash("q", "rag", self.kb_id, 6, scope_revision=0))
        self.assertEqual(chat_turns.payload_hash("q", "rag", self.kb_id, 6, document_ids=[self.identifier]),
                         chat_turns.payload_hash("q", "rag", self.kb_id, 6, document_ids=[self.identifier, self.identifier]))

    def test_claim_checks_scope_before_saving_question_and_keeps_its_snapshot(self):
        self.db.scalar.side_effect = [None, self.conv]
        result = chat_turns.claim_turn(self.db, user_id=self.conv.user_id, conversation_id=self.conv.id,
                                      kb_id=self.kb_id, mode="rag", question="内部级能否外发？", top_k=6,
                                      scope_revision=3)
        self.assertEqual(result.document_scope[0]["document_id"], str(self.identifier))
        self.assertEqual(result.scope_revision, 3)
        self.assertIn("FOR UPDATE", str(self.db.scalar.call_args.args[0]))
        self.assertIn("FOR SHARE", str(self.db.scalars.call_args.args[0].compile(dialect=postgresql.dialect())))

    def test_invalid_scope_or_stale_revision_cannot_create_a_user_message(self):
        for scope, revision in [([self.item], 2), ({}, 3), (None, 3), ([{**self.item, "index_version": 1}], 3)]:
            self.conv.document_scope = scope
            db = MagicMock()
            db.scalar.side_effect = [None, self.conv]
            db.scalars.return_value.all.return_value = [self.doc]
            with self.subTest(scope=scope), self.assertRaises(HTTPException):
                chat_turns.claim_turn(db, user_id=self.conv.user_id, conversation_id=self.conv.id,
                                      kb_id=self.kb_id, mode="rag", question="q", top_k=6, scope_revision=revision)
            db.add.assert_not_called()
            db.commit.assert_not_called()

    def test_plain_chat_rejects_draft_and_scope_revision(self):
        for options in [dict(document_ids=[]), dict(scope_revision=0)]:
            with self.subTest(options=options), self.assertRaises(HTTPException):
                chat_turns.claim_turn(self.db, user_id=uuid.uuid4(), conversation_id=None, kb_id=None,
                                      mode="chat", question="q", top_k=6, **options)
        self.db.add.assert_not_called()
