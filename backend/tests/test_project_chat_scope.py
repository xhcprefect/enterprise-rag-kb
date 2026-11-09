"""项目会话绑定：认领/发布/续期的纯逻辑契约 + 真实 PostgreSQL 的 ready/撤销回归。

纯逻辑部分不连库；真实 PG 部分先过共享 owner 门，只 TRUNCATE 本文件拥有的表，
无配置时 skip 并明说未运行。
"""
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import select, text

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from app.services import answer_sources, project_chat_scope
from app.services.answer_sources import SourceVerificationError


def kb(kind="project", *, kb_id=None, public=False, owner_id=None):
    return SimpleNamespace(id=kb_id or uuid.uuid4(), kind=kind, is_public=public,
                           owner_id=owner_id or uuid.uuid4())


def user(*, user_id=None):
    return SimpleNamespace(id=user_id or uuid.uuid4(), is_active=True, is_admin=False,
                           token_version=0)


class PlanClaimProjectTests(unittest.TestCase):
    def setUp(self):
        self.kb = kb()
        self.user = user()

    def test_document_scope_is_never_mixed_with_project_snapshot(self):
        for ids in ([], [uuid.uuid4()]):
            with self.subTest(ids=ids), self.assertRaises(HTTPException) as error:
                project_chat_scope.plan_claim_project(
                    MagicMock(), kb=self.kb, user=self.user, peek_conv=None,
                    explicit_sid=None, document_ids=ids, new_conv=True)
            self.assertEqual(error.exception.status_code, 400)

    def test_non_project_kb_is_rejected(self):
        with self.assertRaises(HTTPException) as error:
            project_chat_scope.plan_claim_project(
                MagicMock(), kb=kb("upload"), user=self.user, peek_conv=None,
                explicit_sid=uuid.uuid4(), document_ids=None, new_conv=True)
        self.assertEqual(error.exception.status_code, 400)

    def test_new_conversation_without_sid_uses_the_source_active_snapshot(self):
        snapshot_id = uuid.uuid4()
        db = MagicMock()
        db.scalar.return_value = SimpleNamespace(kb_id=self.kb.id, active_snapshot_id=snapshot_id)
        self.assertEqual(project_chat_scope.plan_claim_project(
            db, kb=self.kb, user=self.user, peek_conv=None, explicit_sid=None,
            document_ids=None, new_conv=True), snapshot_id)

    def test_new_conversation_without_ready_source_fails(self):
        for source in (None, SimpleNamespace(kb_id=uuid.uuid4(), active_snapshot_id=None)):
            with self.subTest(source=source), self.assertRaises(HTTPException) as error:
                db = MagicMock()
                db.scalar.return_value = source
                project_chat_scope.plan_claim_project(
                    db, kb=self.kb, user=self.user, peek_conv=None, explicit_sid=None,
                    document_ids=None, new_conv=True)
            self.assertEqual(error.exception.status_code, 409)

    def test_existing_conversation_keeps_its_bound_snapshot_and_rejects_a_switch(self):
        bound = uuid.uuid4()
        conv = SimpleNamespace(project_snapshot_id=bound)
        self.assertEqual(project_chat_scope.plan_claim_project(
            MagicMock(), kb=self.kb, user=self.user, peek_conv=conv, explicit_sid=None,
            document_ids=None, new_conv=False), bound)
        with self.assertRaises(HTTPException) as error:
            project_chat_scope.plan_claim_project(
                MagicMock(), kb=self.kb, user=self.user, peek_conv=conv,
                explicit_sid=uuid.uuid4(), document_ids=None, new_conv=False)
        self.assertEqual(error.exception.status_code, 409)

    def test_existing_conversation_without_binding_cannot_silently_pick_a_new_one(self):
        with self.assertRaises(HTTPException) as error:
            project_chat_scope.plan_claim_project(
                MagicMock(), kb=self.kb, user=self.user,
                peek_conv=SimpleNamespace(project_snapshot_id=None), explicit_sid=None,
                document_ids=None, new_conv=False)
        self.assertEqual(error.exception.status_code, 409)


class ClaimBindingTests(unittest.TestCase):
    def setUp(self):
        self.kb = kb()
        self.snapshot_id = uuid.uuid4()
        self.source_id = uuid.uuid4()

    def binding(self, *, status="ready", revoked=None, source_kb=None):
        snapshot = SimpleNamespace(id=self.snapshot_id, source_id=self.source_id, status=status,
                                   manifest_fingerprint="a" * 64, config_fingerprint="b" * 64)
        source = SimpleNamespace(id=self.source_id, kb_id=source_kb or self.kb.id,
                                 revoked_at=revoked, active_snapshot_id=self.snapshot_id)
        return answer_sources.ProjectBinding(snapshots={self.snapshot_id: snapshot},
                                             sources={self.source_id: source})

    def test_lock_binding_maps_verification_failure_to_conflict(self):
        with patch.object(answer_sources, "lock_project_binding",
                          side_effect=SourceVerificationError("not ready")):
            with self.assertRaises(HTTPException) as error:
                project_chat_scope.lock_claim_binding(MagicMock(), kb=self.kb, user=user(),
                                                      snapshot_id=self.snapshot_id)
        self.assertEqual(error.exception.status_code, 409)

    def test_verify_accepts_a_matching_binding(self):
        conv = SimpleNamespace(kb_id=self.kb.id, project_snapshot_id=self.snapshot_id)
        project_chat_scope.verify_claim_binding(
            MagicMock(), conv=conv, binding=self.binding(), snapshot_id=self.snapshot_id,
            new_conv=True)

    def test_verify_rejects_missing_binding_wrong_kb_revoked_and_drifted_active(self):
        conv = SimpleNamespace(kb_id=self.kb.id, project_snapshot_id=self.snapshot_id)
        cases = [
            (None, True),
            (self.binding(source_kb=uuid.uuid4()), True),
            (self.binding(revoked="2026-01-01"), True),
        ]
        for binding, new_conv in cases:
            with self.subTest(binding=binding), self.assertRaises(HTTPException):
                project_chat_scope.verify_claim_binding(
                    MagicMock(), conv=conv, binding=binding, snapshot_id=self.snapshot_id,
                    new_conv=new_conv)
        drifted = self.binding()
        drifted.sources[self.source_id].active_snapshot_id = uuid.uuid4()
        with self.assertRaises(HTTPException) as error:
            project_chat_scope.verify_claim_binding(
                MagicMock(), conv=conv, binding=drifted, snapshot_id=self.snapshot_id, new_conv=True)
        self.assertEqual(error.exception.status_code, 409)
        # 已有会话不要求 active 仍是本快照（绑定已固定，切换走 CAS）。
        project_chat_scope.verify_claim_binding(
            MagicMock(), conv=conv, binding=drifted, snapshot_id=self.snapshot_id, new_conv=False)

    def test_verify_rejects_a_conversation_bound_to_another_snapshot(self):
        conv = SimpleNamespace(kb_id=self.kb.id, project_snapshot_id=uuid.uuid4())
        with self.assertRaises(HTTPException) as error:
            project_chat_scope.verify_claim_binding(
                MagicMock(), conv=conv, binding=self.binding(), snapshot_id=self.snapshot_id,
                new_conv=False)
        self.assertEqual(error.exception.status_code, 409)


class PublicationBindingTests(unittest.TestCase):
    def setUp(self):
        self.kb = kb()
        self.user = user()
        self.snapshot_id = uuid.uuid4()

    def source(self, snapshot_id=None, kind="project"):
        return {"source_kind": kind, "snapshot_id": str(snapshot_id or self.snapshot_id)}

    def test_publication_without_a_fixed_snapshot_is_rejected(self):
        with self.assertRaises(SourceVerificationError):
            project_chat_scope.lock_publication_binding(
                MagicMock(), kb=self.kb, user=self.user,
                reservation=SimpleNamespace(project_snapshot_id=None), sources=[self.source()])

    def test_sources_from_two_snapshots_are_rejected_before_locking(self):
        with patch.object(answer_sources, "lock_project_binding") as lock:
            with self.assertRaises(SourceVerificationError):
                project_chat_scope.lock_publication_binding(
                    MagicMock(), kb=self.kb, user=self.user,
                    reservation=SimpleNamespace(project_snapshot_id=self.snapshot_id),
                    sources=[self.source(), self.source(uuid.uuid4())])
        lock.assert_not_called()

    def test_publication_locks_exactly_the_fixed_snapshot(self):
        with patch.object(answer_sources, "lock_project_binding",
                          return_value=answer_sources.ProjectBinding(snapshots={}, sources={})) as lock:
            project_chat_scope.lock_publication_binding(
                MagicMock(), kb=self.kb, user=self.user,
                reservation=SimpleNamespace(project_snapshot_id=self.snapshot_id),
                sources=[self.source()])
        self.assertEqual(lock.call_args.args[1], [self.snapshot_id])

    def test_lease_binding_locks_the_fixed_snapshot_before_the_conversation(self):
        with patch.object(answer_sources, "lock_project_binding",
                          return_value=answer_sources.ProjectBinding(snapshots={}, sources={})) as lock:
            project_chat_scope.lock_lease_binding(MagicMock(), kb=self.kb, user=self.user,
                                                  snapshot_id=self.snapshot_id)
        self.assertEqual(lock.call_args.args[1], [self.snapshot_id])


MANIFEST, CONFIG = "a" * 64, "b" * 64


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离项目绑定数据库测试")
class ProjectBindingDatabaseTests(unittest.TestCase):
    TABLES = ("users, knowledge_bases, project_sources, project_snapshots, "
              "project_file_versions, project_snapshot_files, project_chunks")

    @classmethod
    def setUpClass(cls):
        from app.db.init_db import init_db
        from app.db.session import engine
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)
        cls.engine = engine

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, self.engine)
        from app.db.session import SessionLocal
        from app.models import KnowledgeBase, ProjectSnapshot, ProjectSource, User
        self.SessionLocal, self.KnowledgeBase = SessionLocal, KnowledgeBase
        self.ProjectSnapshot, self.ProjectSource, self.User = ProjectSnapshot, ProjectSource, User
        with self.engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {self.TABLES} RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            owner = User(username="psc_owner", role="user", is_active=True, password_hash="x")
            db.add(owner)
            db.flush()
            project_kb = KnowledgeBase(name="psc-project", collection_name="p_" + uuid.uuid4().hex,
                                       owner_id=owner.id, is_public=False, kind="project")
            other_kb = KnowledgeBase(name="psc-other", collection_name="o_" + uuid.uuid4().hex,
                                     owner_id=owner.id, is_public=False, kind="project")
            db.add_all([project_kb, other_kb])
            db.flush()
            self.owner_id, self.kb_id, self.other_kb_id = owner.id, project_kb.id, other_kb.id
            db.commit()
        self.kb = db_kb(self.KnowledgeBase, self.kb_id)
        self.user = db_user(self.User, self.owner_id)

    def make_source(self, *, status="ready", revoked=False, active=True, kb_id=None):
        with self.SessionLocal() as db:
            source = self.ProjectSource(kb_id=kb_id or self.kb_id, name="src",
                                        root_key="root", relative_dir="",
                                        root_fingerprint="f" * 64, created_by=self.owner_id)
            db.add(source)
            db.flush()
            snapshot = self.ProjectSnapshot(source_id=source.id, status=status,
                                            manifest_fingerprint=MANIFEST,
                                            config_fingerprint=CONFIG)
            db.add(snapshot)
            db.flush()
            if revoked:
                from datetime import datetime, timezone
                source.revoked_at = datetime.now(timezone.utc)
            if active:
                source.active_snapshot_id = snapshot.id
            db.commit()
            return source.id, snapshot.id

    def test_ready_binding_locks_and_verifies(self):
        with self.SessionLocal() as db:
            source_id, snapshot_id = self.make_source()
            binding = project_chat_scope.lock_claim_binding(
                db, kb=db_kb(self.KnowledgeBase, self.kb_id), user=db_user(self.User, self.owner_id),
                snapshot_id=snapshot_id)
            self.assertEqual(set(binding.snapshots), {snapshot_id})
            self.assertEqual(set(binding.sources), {source_id})
            db.rollback()

    def test_nonready_snapshot_is_rejected(self):
        with self.SessionLocal() as db:
            _source_id, snapshot_id = self.make_source(status="pending")
            with self.assertRaises(HTTPException) as error:
                project_chat_scope.lock_claim_binding(
                    db, kb=db_kb(self.KnowledgeBase, self.kb_id),
                    user=db_user(self.User, self.owner_id), snapshot_id=snapshot_id)
            self.assertEqual(error.exception.status_code, 409)
            db.rollback()

    def test_revoked_source_is_rejected(self):
        with self.SessionLocal() as db:
            _source_id, snapshot_id = self.make_source(revoked=True)
            with self.assertRaises(HTTPException):
                project_chat_scope.lock_claim_binding(
                    db, kb=db_kb(self.KnowledgeBase, self.kb_id),
                    user=db_user(self.User, self.owner_id), snapshot_id=snapshot_id)
            db.rollback()

    def test_snapshot_from_another_kb_cannot_be_claimed(self):
        with self.SessionLocal() as db:
            _source_id, snapshot_id = self.make_source(kb_id=self.other_kb_id)
            with self.assertRaises(HTTPException) as error:
                project_chat_scope.lock_claim_binding(
                    db, kb=db_kb(self.KnowledgeBase, self.kb_id),
                    user=db_user(self.User, self.owner_id), snapshot_id=snapshot_id)
            self.assertEqual(error.exception.status_code, 409)
            db.rollback()

    def test_verify_active_project_detects_revocation(self):
        from app.services import chat_turns
        with self.SessionLocal() as db:
            source_id, snapshot_id = self.make_source()
            reservation = chat_turns.Reservation(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                                                 uuid.uuid4(), self.owner_id,
                                                 project_snapshot_id=snapshot_id,
                                                 auth_token_version=0, mode="rag", kb_id=self.kb_id)
            conv = SimpleNamespace(project_snapshot_id=snapshot_id, scope_revision=0,
                                   kb_id=self.kb_id)
            project_chat_scope.verify_active_project(db, conv, reservation)
            db.execute(text("UPDATE project_sources SET revoked_at = now() WHERE id = :id"),
                       {"id": source_id})
            db.commit()
            with self.assertRaises(project_chat_scope.ProjectBindingLost):
                project_chat_scope.verify_active_project(db, conv, reservation)


def db_kb(model, kb_id):
    from app.db.session import SessionLocal
    with SessionLocal() as session:
        return session.get(model, kb_id)


def db_user(model, user_id):
    from app.db.session import SessionLocal
    with SessionLocal() as session:
        return session.get(model, user_id)


if __name__ == "__main__":
    unittest.main()