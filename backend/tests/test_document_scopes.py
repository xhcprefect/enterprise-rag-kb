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
        # 治理字段必须显式给出：缺失即 fail closed，不能靠宽松默认过 mock。
        self.doc = SimpleNamespace(id=self.identifier, kb_id=self.kb_id, filename="服务器原名.pdf",
                                   status="ready", cancel_requested=False, index_version=2,
                                   enabled=True, superseded_at=None, superseded_by_id=None,
                                   valid_from=None, valid_until=None)
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
        # 新锁序：advisory 垂幕锁后先经 answer_sources.lock_root 锁 User/KB，
        # 再由 claim_turn 查会话；patch 掉根锁，scalar 序列只留会话查询。
        from app.services import answer_sources as sources
        root = sources.SourceRoot(
            user=SimpleNamespace(id=self.conv.user_id, is_active=True, token_version=0, is_admin=False),
            knowledge_base=SimpleNamespace(id=self.kb_id, kind="upload", is_public=True, owner_id=self.conv.user_id))
        self.db.scalar.side_effect = [None, self.conv]
        with patch.object(sources, "lock_root", return_value=root):
            result = chat_turns.claim_turn(self.db, user_id=self.conv.user_id, conversation_id=self.conv.id,
                                           kb_id=self.kb_id, mode="rag", question="内部级能否外发？", top_k=6,
                                           scope_revision=3, auth_token_version=0)
        self.assertEqual(result.document_scope[0]["document_id"], str(self.identifier))
        self.assertEqual(result.scope_revision, 3)
        self.assertIn("FOR UPDATE", str(self.db.scalar.call_args.args[0]))
        self.assertIn("FOR SHARE", str(self.db.scalars.call_args.args[0].compile(dialect=postgresql.dialect())))

    def test_invalid_scope_or_stale_revision_cannot_create_a_user_message(self):
        # 发布契约下轮次/消息先落、Document 锁最后取；mock db 无真实事务回滚，
        # 因此断言异常抛出且从不 commit（真实 PG 上异常即整轮回滚，无持久化写入）。
        for scope, revision in [([self.item], 2), ({}, 3), (None, 3), ([{**self.item, "index_version": 1}], 3)]:
            self.conv.document_scope = scope
            db = MagicMock()
            db.scalar.side_effect = [None, self.conv]
            db.scalars.return_value.all.return_value = [self.doc]
            from app.services import answer_sources as sources
            root = sources.SourceRoot(
                user=SimpleNamespace(id=self.conv.user_id, is_active=True, token_version=0, is_admin=False),
                knowledge_base=SimpleNamespace(id=self.kb_id, kind="upload", is_public=True, owner_id=self.conv.user_id))
            with self.subTest(scope=scope), patch.object(sources, "lock_root", return_value=root), \
                 self.assertRaises(HTTPException):
                chat_turns.claim_turn(db, user_id=self.conv.user_id, conversation_id=self.conv.id,
                                      kb_id=self.kb_id, mode="rag", question="q", top_k=6, scope_revision=revision,
                                      auth_token_version=0)
            db.commit.assert_not_called()

    def test_plain_chat_rejects_draft_and_scope_revision(self):
        for options in [dict(document_ids=[]), dict(scope_revision=0)]:
            with self.subTest(options=options), self.assertRaises(HTTPException):
                chat_turns.claim_turn(self.db, user_id=uuid.uuid4(), conversation_id=None, kb_id=None,
                                      mode="chat", question="q", top_k=6, auth_token_version=0, **options)
        self.db.add.assert_not_called()


class InternalScopeLimitTests(unittest.TestCase):
    """用户会话 5 份与内部检索清单 256 份的有界校验必须分开。"""

    def manifest(self, count):
        return [{"document_id": str(uuid.uuid4()), "index_version": 0} for _ in range(count)]

    def test_default_still_rejects_six_while_internal_manifest_accepts_six_and_256(self):
        items = self.manifest(257)
        self.assertEqual(len(scopes.scope_pairs(items[:5])), 5)
        with self.assertRaises(ValueError):
            scopes.scope_pairs(items[:6])
        self.assertEqual(len(scopes.scope_pairs(
            items[:6], max_documents=scopes.MAX_INTERNAL_SCOPE_DOCUMENTS)), 6)
        # 执行上下文提供的是 tuple；内部路径同样接受，用户选择上限仍由默认 5 限制。
        self.assertEqual(len(scopes.scope_pairs(
            tuple(items[:256]), max_documents=scopes.MAX_INTERNAL_SCOPE_DOCUMENTS)), 256)
        with self.assertRaises(ValueError):
            scopes.scope_pairs(items[:257], max_documents=scopes.MAX_INTERNAL_SCOPE_DOCUMENTS)
        with self.assertRaises(ValueError):
            scopes.scope_pairs(tuple(items[:257]), max_documents=scopes.MAX_INTERNAL_SCOPE_DOCUMENTS)
        # 新增内部上限不得放宽用户选择入口。
        with self.assertRaises(HTTPException):
            scopes.scope_ids([item["document_id"] for item in items[:6]])

    def test_internal_manifest_rejects_bad_identity_version_duplicate_and_bad_limit(self):
        valid = self.manifest(3)
        bad_scopes = [
            None, {}, "", [None], [{**valid[0], "document_id": "not-a-uuid"}],
            [{**valid[0], "index_version": True}], [{**valid[0], "index_version": 1.0}],
            [{**valid[0], "index_version": -1}], [valid[0], valid[0]],
        ]
        for value in bad_scopes:
            with self.subTest(scope=value), self.assertRaises(ValueError):
                scopes.scope_pairs(value, max_documents=scopes.MAX_INTERNAL_SCOPE_DOCUMENTS)
        for limit in [True, 5.0, -1, "256", None]:
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                scopes.scope_pairs(valid, max_documents=limit)

    def test_retrieval_sql_filter_and_contains_use_256_but_user_default_stays_five(self):
        items = self.manifest(257)
        self.assertEqual(str(scopes.scope_filter([])), "false")
        sql = str(scopes.scope_filter(items[:256]).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.assertIn(str(items[0]["document_id"]), sql)
        self.assertIn(str(items[255]["document_id"]), sql)
        self.assertIn("documents.index_version = 0", sql)
        with self.assertRaises(ValueError):
            scopes.scope_filter(items[:257])
        metadata = dict(items[256 - 1])
        self.assertTrue(scopes.contains(items[:256], metadata))
        self.assertFalse(scopes.contains(items[:256], metadata, max_documents=scopes.MAX_SCOPE_DOCUMENTS))
        self.assertFalse(scopes.contains(items[:256], {**metadata, "index_version": True},
                                         max_documents=scopes.MAX_INTERNAL_SCOPE_DOCUMENTS))
