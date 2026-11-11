"""反馈回归执行上下文：真实 PostgreSQL 根规划/加锁/固定范围与项目绑定回归。

覆盖显式成员私库、所有者撤权/禁用、管理员 token/角色等锁后变化、固定上传清单
（新增文档不进入、代次失效、显式空清单、256 上限、限定范围越界）、项目真实
snapshot/file/chunk/manifest/config 与 active 切换，以及批量跨用户/知识库稳定锁序。

未设置 TEST_DATABASE_URL 时整体跳过；不会连接其他数据库。
"""
from __future__ import annotations

import json
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from tests import _memory_project_test_gate as gate
from tests.test_answer_feedback import _Helpers

TEST_URL = gate.TEST_DATABASE_URL

TABLES = (
    "users, knowledge_bases, knowledge_base_members, documents, document_chunks, "
    "upload_sessions, audit_logs, conversations, chat_turns, messages, model_usage, "
    "usage_tracking_state, answer_feedbacks, feedback_regressions, "
    "feedback_regression_runs, project_sources, project_snapshots, "
    "project_file_versions, project_snapshot_files, project_chunks"
)

if TEST_URL:
    from sqlalchemy import select, text

    from app.db.init_db import init_db
    from app.models import (
        Conversation,
        Document,
        DocumentChunk,
        KnowledgeBase,
        KnowledgeBaseMember,
        ProjectChunk,
        ProjectFileVersion,
        ProjectSnapshot,
        ProjectSnapshotFile,
        ProjectSource,
        User,
    )
    from app.services import feedback_regression_context as context
    from tests.test_answer_feedback import SessionLocal, engine


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行执行上下文数据库测试")
class FeedbackRegressionContextTests(_Helpers, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)
        from unittest.mock import patch

        with patch("app.core.logging.setup_logging"):
            from app.main import app

        cls.app = app
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            other = User(username="ctx_other", role="user", is_active=True, password_hash="x")
            member = User(username="ctx_member", role="user", is_active=True, password_hash="x")
            admin = User(username="ctx_admin", role="admin", is_active=True, password_hash="x")
            db.add_all([other, member, admin])
            db.flush()
            kb = KnowledgeBase(
                name="ctx_private_" + uuid.uuid4().hex,
                collection_name="ctx_" + uuid.uuid4().hex,
                owner_id=other.id,
                is_public=False,
                kind="upload",
            )
            db.add(kb)
            db.flush()
            db.add(KnowledgeBaseMember(kb_id=kb.id, user_id=member.id, role="reader"))
            doc = self._new_document(db, kb.id, "source.md")
            db.add(doc)
            db.flush()
            db.add(
                DocumentChunk(
                    document_id=doc.id,
                    chunk_index=0,
                    content="上下文资料分块",
                    source_metadata={"filename": "source.md"},
                    search_text="上下文资料分块",
                    embedded=True,
                )
            )
            db.commit()
            self.other_id = other.id
            self.member_id = member.id
            self.admin_id = admin.id
            self.kb_id = kb.id
            self.doc_id = doc.id
            self.owner_id = member.id
        self.owner_headers = self.headers(self.owner_id, "user")
        self.admin_headers = self.headers(self.admin_id, "admin")

    # ------------------------------------------------------------ fixtures

    @staticmethod
    def _new_document(db, kb_id, filename):
        return Document(
            kb_id=kb_id,
            filename=filename,
            file_ext=".md",
            file_size=1,
            stored_path="/tmp/" + uuid.uuid4().hex + ".md",
            status="ready",
            index_version=0,
            cancel_requested=False,
            parse_done=True,
            chunk_count=1,
            enabled=True,
        )

    def verified_feedback(
        self, *, kb_id="default", expected_sources=None, citations=None, owner_id=None
    ):
        owner_id = self.owner_id if owner_id is None else owner_id
        target = self.add_answer(kb_id=kb_id, owner_id=owner_id, citations=citations)
        if expected_sources == []:
            # 线上核实接口明确拒绝“知识库回答无期望来源”；这里仅合成历史损坏/无来源
            # 反馈，验证执行清单的显式空值不会退化为全库。
            feedback_id = self.insert_verified_feedback(target, expected_sources=[])
            if owner_id != self.owner_id:
                from app.models import AnswerFeedback

                with SessionLocal() as db:
                    db.get(AnswerFeedback, uuid.UUID(str(feedback_id))).user_id = owner_id
                    db.commit()
            return feedback_id
        feedback_id = self.submit_ok(
            target.message_id, headers=self.headers(owner_id, "user")
        )
        kwargs = {}
        if expected_sources is not None:
            kwargs["expected_sources"] = expected_sources
        response = self.review(feedback_id, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return feedback_id

    def complete_project_binding(self, *, relative_path="src/main.py", chunks=1):
        kb_id, snapshot_id, file_version_id = self.project_binding(
            revoked=False, public=False, owner_id=self.other_id, relative_path=relative_path
        )
        with SessionLocal() as db:
            db.add(KnowledgeBaseMember(kb_id=kb_id, user_id=self.member_id, role="reader"))
            snapshot = db.get(ProjectSnapshot, snapshot_id)
            snapshot.manifest_fingerprint = "d" * 64
            snapshot.config_fingerprint = "e" * 64
            version = db.get(ProjectFileVersion, file_version_id)
            version.parse_status = "ready"
            version.parse_done = True
            for index in range(chunks):
                db.add(
                    ProjectChunk(
                        file_version_id=file_version_id,
                        chunk_index=index,
                        content=f"项目分块{index}",
                        search_text=f"项目分块{index}",
                        embedded=True,
                    )
                )
            db.commit()
        return kb_id, snapshot_id, file_version_id

    def project_feedback(self, *, relative_path="src/main.py", chunks=1, citations=None):
        kb_id, snapshot_id, file_version_id = self.complete_project_binding(
            relative_path=relative_path, chunks=chunks
        )
        target = self.add_answer(
            kb_id=kb_id, project_snapshot_id=snapshot_id, citations=citations
        )
        feedback_id = self.submit_ok(target.message_id)
        response = self.review(
            feedback_id,
            expected_sources=[
                {"document_id": str(file_version_id), "filename": relative_path,
                 "chunk_index": 0}
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        return feedback_id, kb_id, snapshot_id, file_version_id

    # ------------------------------------------------------------ helpers

    def build(self, feedback_ids, **kwargs):
        with SessionLocal() as db:
            return context.build_batch_contexts(
                db,
                admin_id=self.admin_id,
                auth_token_version=0,
                feedback_ids=feedback_ids,
                **kwargs,
            )

    def build_context(self, feedback_id, **kwargs):
        return self.build([feedback_id], **kwargs).contexts[0]

    def build_manual_context(self, feedback_id, **kwargs):
        """旧 manual 门禁：只锁标注来源/范围，不冻结整库。"""
        with SessionLocal() as db:
            plan = context.plan_roots(db, [feedback_id])
            locked = context.lock_roots(
                db,
                plan,
                admin_id=self.admin_id,
                auth_token_version=0,
                freeze_manifests=False,
            )
            return context.build_contexts(db, locked).contexts[0]

    def plan(self, feedback_ids):
        with SessionLocal() as db:
            return context.plan_roots(db, feedback_ids)

    # ------------------------------------------------------------ permissions / drift

    def test_explicit_member_private_kb_builds_detached_context(self):
        feedback_id = self.verified_feedback()
        ctx = self.build_context(feedback_id)
        self.assertEqual(ctx.execution_kind, "upload")
        self.assertEqual(ctx.owner_id, str(self.member_id))
        self.assertEqual(ctx.kb_id, str(self.kb_id))
        self.assertEqual(ctx.upload_manifest, ({"document_id": str(self.doc_id), "index_version": 0},))
        self.assertTrue(all(source["document_id"] == str(self.doc_id) for source in ctx.resolved_sources))
        payload = ctx.to_dict()
        json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Message", repr(payload))
        self.assertEqual(payload["turns"], ["合成问题"])

        # 持久作业只使用白名单摘要：不得含原问题、临时 case 或正文。
        summary = ctx.summary()
        json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("case", summary)
        self.assertNotIn("turns", summary)
        self.assertNotIn("合成问题", json.dumps(summary, ensure_ascii=False))
        self.assertEqual(summary["feedback_id"], str(feedback_id))
        self.assertEqual(summary["upload_manifest"], list(ctx.upload_manifest))
        self.assertEqual(
            set(summary["resolved_sources"][0]),
            {"document_id", "filename", "index_version"},
        )

    def test_owner_member_removal_or_disable_fails_closed(self):
        feedback_id = self.verified_feedback()
        with SessionLocal() as db:
            row = db.scalar(
                select(KnowledgeBaseMember).where(
                    KnowledgeBaseMember.kb_id == self.kb_id,
                    KnowledgeBaseMember.user_id == self.member_id,
                )
            )
            db.delete(row)
            db.commit()
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(feedback_id)
        self.assertEqual(raised.exception.reason, "owner_access_revoked")

        with SessionLocal() as db:
            db.add(KnowledgeBaseMember(kb_id=self.kb_id, user_id=self.member_id, role="reader"))
            db.commit()
        with SessionLocal() as db:
            db.get(User, self.member_id).is_active = False
            db.commit()
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(feedback_id)
        self.assertEqual(raised.exception.reason, "owner_inactive")

    def test_admin_token_or_role_change_after_plan_fails_closed(self):
        feedback_id = self.verified_feedback()
        plan = self.plan([feedback_id])
        with SessionLocal() as db:
            db.get(User, self.admin_id).token_version = 1
            db.commit()
        with SessionLocal() as db:
            with self.assertRaises(context.RegressionContextError) as raised:
                context.lock_roots(
                    db, plan, admin_id=self.admin_id, auth_token_version=0
                )
        self.assertEqual(raised.exception.status_code, 401)

        with SessionLocal() as db:
            admin = db.get(User, self.admin_id)
            admin.token_version = 0
            admin.role = "user"
            db.commit()
        with SessionLocal() as db:
            with self.assertRaises(context.RegressionContextError) as raised:
                context.lock_roots(
                    db, plan, admin_id=self.admin_id, auth_token_version=0
                )
        self.assertEqual(raised.exception.status_code, 403)

    def test_plan_drift_after_locking_is_rejected(self):
        feedback_id = self.verified_feedback()
        plan = self.plan([feedback_id])
        with SessionLocal() as db:
            conversation = db.get(Conversation, plan.items[0].conversation_id)
            conversation.scope_revision = 1
            db.commit()
        with SessionLocal() as db:
            with self.assertRaises(context.RegressionContextError) as raised:
                context.lock_roots(db, plan, admin_id=self.admin_id, auth_token_version=0)
        self.assertEqual(raised.exception.reason, "root_plan_changed")

    # ------------------------------------------------------------ fixed upload manifests

    def test_fixed_manifest_excludes_new_documents_and_revalidates_original(self):
        feedback_id = self.verified_feedback()
        frozen = self.build_context(feedback_id).upload_manifest
        with SessionLocal() as db:
            extra = self._new_document(db, self.kb_id, "new-after-freeze.md")
            db.add(extra)
            db.commit()
            extra_id = extra.id
        replay = self.build_context(
            feedback_id, fixed_manifests={uuid.UUID(str(feedback_id)): list(frozen)}
        )
        self.assertEqual(replay.upload_manifest, frozen)
        replay_none = self.build_context(
            feedback_id, fixed_manifests={uuid.UUID(str(feedback_id)): None}
        )
        self.assertEqual(
            {entry["document_id"] for entry in replay_none.upload_manifest},
            {entry["document_id"] for entry in frozen} | {str(extra_id)},
        )
        self.assertNotIn(str(extra_id), {entry["document_id"] for entry in replay.upload_manifest})

        with SessionLocal() as db:
            db.get(Document, self.doc_id).index_version = 1
            db.commit()
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(
                feedback_id, fixed_manifests={uuid.UUID(str(feedback_id)): list(frozen)}
            )
        self.assertEqual(raised.exception.reason, "version_mismatch")

        with SessionLocal() as db:
            db.get(Document, self.doc_id).index_version = 0
            db.get(Document, self.doc_id).enabled = False
            db.commit()
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(
                feedback_id, fixed_manifests={uuid.UUID(str(feedback_id)): list(frozen)}
            )
        self.assertEqual(raised.exception.reason, "source_invalid")

    def test_explicit_empty_manifest_stays_empty(self):
        # 该反馈允许无可标注来源；显式空清单不能被重算成全库。
        feedback_id = self.verified_feedback(expected_sources=[])
        first = self.build_context(feedback_id)
        self.assertEqual(first.upload_manifest, ({"document_id": str(self.doc_id), "index_version": 0},))
        empty = self.build_context(
            feedback_id, fixed_manifests={uuid.UUID(str(feedback_id)): []}
        )
        self.assertEqual(empty.upload_manifest, ())

    def test_limited_scope_manifest_and_out_of_scope_fixed_manifest(self):
        feedback_id = self.verified_feedback()
        # 直接按 feedback 找到会话并设置限定范围。
        from app.models import AnswerFeedback

        with SessionLocal() as db:
            feedback = db.get(AnswerFeedback, uuid.UUID(str(feedback_id)))
            conversation = db.get(Conversation, feedback.conversation_id)
            conversation.document_scope = [
                {"document_id": str(self.doc_id), "index_version": 0}
            ]
            conversation.scope_revision = 1
            db.commit()
        limited = self.build_context(feedback_id)
        self.assertEqual(
            limited.upload_manifest,
            ({"document_id": str(self.doc_id), "index_version": 0},),
        )
        self.assertEqual(limited.limited_scope, limited.upload_manifest)
        with SessionLocal() as db:
            extra = self._new_document(db, self.kb_id, "outside.md")
            db.add(extra)
            db.commit()
            extra_id = extra.id
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(
                feedback_id,
                fixed_manifests={
                    uuid.UUID(str(feedback_id)): [
                        {"document_id": str(self.doc_id), "index_version": 0},
                        {"document_id": str(extra_id), "index_version": 0},
                    ]
                },
            )
        self.assertEqual(raised.exception.reason, "scope_invalid")

    def test_full_library_manifest_over_256_is_rejected_without_truncation(self):
        with SessionLocal() as db:
            kb = KnowledgeBase(
                name="ctx_big_" + uuid.uuid4().hex,
                collection_name="ctx_big_" + uuid.uuid4().hex,
                owner_id=self.other_id,
                is_public=True,
                kind="upload",
            )
            db.add(kb)
            db.flush()
            for index in range(257):
                db.add(self._new_document(db, kb.id, f"doc-{index:03d}.md"))
            db.commit()
            big_kb_id = kb.id
        feedback_id = self.verified_feedback(kb_id=big_kb_id, expected_sources=[])
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(feedback_id)
        self.assertEqual(raised.exception.reason, "manifest_overflow")
        self.assertEqual(raised.exception.status_code, 409)
        with SessionLocal() as db:
            ids = db.scalars(
                select(Document.id).where(Document.kb_id == big_kb_id).order_by(Document.id)
            ).all()
        fixed = [
            {"document_id": str(identifier), "index_version": 0}
            for identifier in ids
        ]
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(
                feedback_id,
                fixed_manifests={uuid.UUID(str(feedback_id)): fixed},
            )
        self.assertEqual(raised.exception.reason, "manifest_overflow")

        # 旧 manual 门禁不枚举整库：同样 257 份文档仍可构建有限上下文。
        manual = self.build_manual_context(feedback_id)
        self.assertIsNone(manual.upload_manifest)
        self.assertEqual(manual.execution_kind, "upload")
        with SessionLocal() as db:
            plan = context.plan_roots(db, [feedback_id])
            with self.assertRaises(context.RegressionContextError) as raised:
                context.lock_roots(
                    db,
                    plan,
                    admin_id=self.admin_id,
                    auth_token_version=0,
                    fixed_manifests={uuid.UUID(str(feedback_id)): []},
                    freeze_manifests=False,
                )
        self.assertEqual(raised.exception.reason, "invalid_batch")

    # ------------------------------------------------------------ project binding

    def test_project_context_uses_frozen_snapshot_and_validates_real_chunks(self):
        feedback_id, kb_id, snapshot_id, file_version_id = self.project_feedback(chunks=2)
        ctx = self.build_context(feedback_id)
        self.assertEqual(ctx.execution_kind, "project")
        self.assertEqual(ctx.project["snapshot_id"], str(snapshot_id))
        self.assertEqual(ctx.project["source_id"], str(self._project_source_id(snapshot_id)))
        self.assertEqual(ctx.project["manifest_fingerprint"], "d" * 64)
        self.assertEqual(ctx.resolved_sources[0]["file_version_id"], str(file_version_id))
        self.assertTrue(ctx.has_fixed_execution)

        # active 快照切换到新 ready 快照，不得改变执行绑定。
        with SessionLocal() as db:
            source = db.get(ProjectSource, uuid.UUID(ctx.project["source_id"]))
            second = ProjectSnapshot(
                source_id=source.id,
                status="ready",
                manifest_fingerprint="f" * 64,
                config_fingerprint="a" * 64,
            )
            db.add(second)
            db.flush()
            source.active_snapshot_id = second.id
            db.commit()
        switched = self.build_context(feedback_id)
        self.assertEqual(switched.project["snapshot_id"], str(snapshot_id))
        self.assertEqual(switched.project["manifest_fingerprint"], "d" * 64)

        # 固定绑定重放必须仍匹配入队身份。
        incorrect = {**switched.project, "manifest_fingerprint": "0" * 64}
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(
                feedback_id,
                fixed_projects={uuid.UUID(str(feedback_id)): incorrect},
            )
        self.assertEqual(raised.exception.reason, "project_snapshot_changed")

        incorrect = {**switched.project, "config_fingerprint": "0" * 64}
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(
                feedback_id,
                fixed_projects={uuid.UUID(str(feedback_id)): incorrect},
            )
        self.assertEqual(raised.exception.reason, "project_snapshot_changed")

    def test_project_chunk_and_snapshot_mapping_mismatches_are_rejected(self):
        feedback_id, kb_id, snapshot_id, file_version_id = self.project_feedback(chunks=1)
        with SessionLocal() as db:
            chunk = db.scalar(
                select(ProjectChunk).where(ProjectChunk.file_version_id == file_version_id)
            )
            db.delete(chunk)
            db.commit()
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(feedback_id)
        self.assertEqual(raised.exception.reason, "chunk_missing")

        with SessionLocal() as db:
            db.add(
                ProjectChunk(
                    file_version_id=file_version_id,
                    chunk_index=0,
                    content="恢复分块",
                    search_text="恢复分块",
                    embedded=True,
                )
            )
            mapped = db.scalar(
                select(ProjectSnapshotFile).where(
                    ProjectSnapshotFile.snapshot_id == snapshot_id,
                    ProjectSnapshotFile.file_version_id == file_version_id,
                )
            )
            mapped.relative_path = "src/moved.py"
            db.commit()
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(feedback_id)
        self.assertEqual(raised.exception.reason, "source_invalid")

    def test_project_missing_manifest_config_is_rejected(self):
        feedback_id, kb_id, snapshot_id, _ = self.project_feedback()
        with SessionLocal() as db:
            db.get(ProjectSnapshot, snapshot_id).manifest_fingerprint = None
            db.commit()
        with self.assertRaises(context.RegressionContextError) as raised:
            self.build_context(feedback_id)
        self.assertEqual(raised.exception.reason, "project_snapshot_invalid")

    def test_stale_original_project_citation_does_not_block_expected_source(self):
        feedback_id, _, _, file_version_id = self.project_feedback(
            citations=[{"document_id": str(uuid.uuid4()), "filename": "old.py",
                        "chunk_index": 0, "index_version": 0}]
        )
        ctx = self.build_context(feedback_id)
        self.assertEqual(ctx.project["snapshot_id"], str(ctx.project["snapshot_id"]))
        self.assertEqual(ctx.resolved_sources[0]["file_version_id"], str(file_version_id))
        self.assertNotIn("old.py", json.dumps(ctx.to_dict(), ensure_ascii=False))

    @staticmethod
    def _project_source_id(snapshot_id):
        with SessionLocal() as db:
            return db.get(ProjectSnapshot, snapshot_id).source_id

    # ------------------------------------------------------------ batch lock order

    def test_batch_across_users_and_kbs_locks_in_stable_order(self):
        with SessionLocal() as db:
            second_user = User(
                username="ctx_second", role="user", is_active=True, password_hash="x"
            )
            db.add(second_user)
            db.flush()
            second_kb = KnowledgeBase(
                name="ctx_second_" + uuid.uuid4().hex,
                collection_name="ctx_second_" + uuid.uuid4().hex,
                owner_id=second_user.id,
                is_public=True,
                kind="upload",
            )
            db.add(second_kb)
            db.flush()
            second_doc = self._new_document(db, second_kb.id, "second.md")
            db.add(second_doc)
            db.commit()
            second_user_id, second_kb_id = second_user.id, second_kb.id

        first = self.verified_feedback()
        second = self.verified_feedback(
            kb_id=second_kb_id, expected_sources=[], owner_id=second_user_id
        )

        orders = [[first, second], [second, first]]
        barrier = threading.Barrier(2)

        def run(ids):
            barrier.wait()
            with SessionLocal() as db:
                return context.build_batch_contexts(
                    db,
                    admin_id=self.admin_id,
                    auth_token_version=0,
                    feedback_ids=ids,
                ).contexts

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, orders))
        self.assertTrue(all(len(batch) == 2 for batch in results))
        self.assertEqual(
            {ctx.feedback_id for ctx in results[0]},
            {str(first), str(second)},
        )


if __name__ == "__main__":
    unittest.main()