"""反馈回归闭环：真实隔离 PostgreSQL + FastAPI/JWT 集成回归。

覆盖：仅管理员可用、待处理/驳回/过期/项目/范围/轮次/来源歧义与失效阻断、
纳入幂等与冲突、准备的幂等/新轮/旧轮、错误报告零写入、无正文与有正文报告
导入、同轮不同报告 409、旧轮迟到 409、并发导入单次写入、删除级联，以及
审计白名单（不含正文/原因说明）与 GET 不写审计。

没有适配层 ``feedback_regression_evaluation`` 时这些用例无法通过；未设置
``TEST_DATABASE_URL`` 时整体跳过，不做任何数据库连接。
"""
from __future__ import annotations

import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from tests import _memory_project_test_gate as gate
from tests.test_answer_feedback import _Helpers

TEST_URL = gate.TEST_DATABASE_URL

TABLES = (
    "users, knowledge_bases, documents, document_chunks, upload_sessions, "
    "audit_logs, conversations, chat_turns, messages, model_usage, "
    "usage_tracking_state, answer_feedbacks, feedback_regressions, "
    "feedback_regression_runs, project_sources, project_snapshots, "
    "project_file_versions, project_snapshot_files, project_chunks"
)

if TEST_URL:
    from sqlalchemy import func, select, text

    from app.db.init_db import init_db
    from app.models import (
        AnswerFeedback,
        AuditLog,
        Conversation,
        Document,
        DocumentChunk,
        FeedbackRegression,
        FeedbackRegressionRun,
        Message,
        User,
    )
    from app.services.evaluation import (
        build_identity,
        grade_case,
    )
    from tests.test_answer_feedback import SessionLocal, engine


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离反馈回归数据库测试")
class FeedbackRegressionTests(_Helpers, unittest.TestCase):
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
            owner = User(username="reg_owner", role="user", is_active=True, password_hash="x")
            other = User(username="reg_other", role="user", is_active=True, password_hash="x")
            admin = User(username="reg_admin", role="admin", is_active=True, password_hash="x")
            db.add_all([owner, other, admin])
            db.flush()
            kb = self._new_upload_kb(db, owner.id)
            db.add(kb)
            db.flush()
            doc = self._new_document(db, kb.id, "source.md")
            db.add(doc)
            db.flush()
            db.add(
                DocumentChunk(
                    document_id=doc.id,
                    chunk_index=0,
                    content="回归资料分块",
                    source_metadata={"filename": "source.md"},
                    search_text="回归资料分块",
                    embedded=True,
                )
            )
            db.commit()
            self.owner_id, self.other_id, self.admin_id = owner.id, other.id, admin.id
            self.kb_id = kb.id
            self.doc_id = doc.id
        self.owner_headers = self.headers(self.owner_id, "user")
        self.other_headers = self.headers(self.other_id, "user")
        self.admin_headers = self.headers(self.admin_id, "admin")

    # ------------------------------------------------------------ fixtures

    @staticmethod
    def _new_upload_kb(db, owner_id):
        from app.models import KnowledgeBase

        return KnowledgeBase(
            name="reg_kb_" + uuid.uuid4().hex,
            collection_name="reg_" + uuid.uuid4().hex,
            owner_id=owner_id,
            is_public=True,
            kind="upload",
        )

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
        self,
        *,
        question="回归问题",
        answer="回归回答",
        mode="rag",
        kb_id="default",
        review_kwargs=None,
    ):
        target = self.add_answer(
            question=question, answer=answer, mode=mode, kb_id=kb_id
        )
        feedback_id = self.submit_ok(target.message_id)
        kwargs = dict(review_kwargs or {})
        if mode == "chat" and "expected_sources" not in kwargs:
            kwargs["expected_sources"] = []
        response = self.review(feedback_id, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return feedback_id, target

    # ------------------------------------------------------------ request helpers

    def enroll(
        self,
        feedback_id,
        *,
        revision=2,
        cause="retrieval",
        cause_note="检索未召回",
        headers=None,
        extra=None,
    ):
        body = {
            "expected_feedback_revision": revision,
            "cause": cause,
            "cause_note": cause_note,
        }
        if extra:
            body.update(extra)
        return self.client.post(
            f"/api/v1/feedback/{feedback_id}/regression",
            headers=headers or self.admin_headers,
            json=body,
        )

    def enroll_ok(self, feedback_id, **kwargs):
        response = self.enroll(feedback_id, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def get_regression(self, feedback_id, *, headers=None):
        return self.client.get(
            f"/api/v1/feedback/{feedback_id}/regression",
            headers=headers or self.admin_headers,
        )

    def prepare(
        self,
        feedback_id,
        *,
        revision,
        mode="offline",
        request_id=None,
        headers=None,
    ):
        return self.client.post(
            f"/api/v1/feedback/{feedback_id}/regression/runs",
            headers=headers or self.admin_headers,
            json={
                "expected_revision": revision,
                "requested_mode": mode,
                "prepare_request_id": str(request_id or uuid.uuid4()),
            },
        )

    def prepare_ok(self, feedback_id, **kwargs):
        response = self.prepare(feedback_id, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def export_run(self, feedback_id, run_id, *, headers=None):
        return self.client.get(
            f"/api/v1/feedback/{feedback_id}/regression/runs/{run_id}/export",
            headers=headers or self.admin_headers,
        )

    def history(self, feedback_id, *, page=1, page_size=20, headers=None):
        return self.client.get(
            f"/api/v1/feedback/{feedback_id}/regression/runs",
            headers=headers or self.admin_headers,
            params={"page": page, "page_size": page_size},
        )

    def import_report(
        self, feedback_id, run_id, *, revision, body, headers=None
    ):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.client.post(
            f"/api/v1/feedback/{feedback_id}/regression/runs/{run_id}/import",
            headers=headers or self.admin_headers,
            params={"expected_revision": revision},
            content=body,
        )

    # ------------------------------------------------------------ report builders

    def build_report(
        self,
        prepared,
        *,
        include_content=False,
        answer="回归回答",
        run_id=None,
        dataset_sha256=None,
        case=None,
        mode=None,
    ):
        from app.services.evaluation import make_report

        case = case or json.loads(prepared["jsonl"].splitlines()[0])
        mode = mode or prepared["run"]["requested_mode"]
        citations = []
        for source in case.get("expected_sources", []):
            citation = {"snippet": "回归资料分块"}
            for key in ("document_id", "filename", "chunk_index", "index_version"):
                if source.get(key) is not None:
                    citation[key] = source[key]
            citations.append(citation)
        response = {
            "id": case["id"],
            "answer": answer,
            "citations": citations,
            "trace": {},
            "http_status": 200,
        }
        result = grade_case(case, response, include_content=include_content)
        identity = build_identity([case], mode=mode)
        if dataset_sha256 is not None:
            identity["dataset"]["sha256"] = dataset_sha256
        report = make_report(
            [result],
            run_id or prepared["run"]["run_id"],
            identity=identity,
        )
        report["mode"] = mode
        return report

    @staticmethod
    def report_bytes(report):
        return json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")

    def run_row(self, run_id):
        with SessionLocal() as db:
            return db.get(FeedbackRegressionRun, uuid.UUID(str(run_id)))

    def regression_row(self, feedback_id):
        with SessionLocal() as db:
            return db.scalar(
                select(FeedbackRegression).where(
                    FeedbackRegression.feedback_id == uuid.UUID(str(feedback_id))
                )
            )

    # ------------------------------------------------------------ membership

    def test_member_is_forbidden_on_all_regression_endpoints(self):
        feedback_id, _ = self.verified_feedback()
        run_id = uuid.uuid4()
        body = {
            "expected_feedback_revision": 2,
            "cause": "retrieval",
            "cause_note": "检索未召回",
        }
        prepare_body = {
            "expected_revision": 1,
            "requested_mode": "offline",
            "prepare_request_id": str(uuid.uuid4()),
        }
        calls = (
            ("GET", f"/api/v1/feedback/{feedback_id}/regression", None, None),
            ("POST", f"/api/v1/feedback/{feedback_id}/regression", body, None),
            ("GET", f"/api/v1/feedback/{feedback_id}/regression/runs", None, None),
            ("POST", f"/api/v1/feedback/{feedback_id}/regression/runs", prepare_body, None),
            (
                "GET",
                f"/api/v1/feedback/{feedback_id}/regression/runs/{run_id}/export",
                None,
                None,
            ),
            (
                "POST",
                f"/api/v1/feedback/{feedback_id}/regression/runs/{run_id}/import",
                None,
                b"{}",
            ),
        )
        for method, url, payload, content in calls:
            with self.subTest(url=url, method=method):
                kwargs = {"headers": self.owner_headers}
                if payload is not None:
                    kwargs["json"] = payload
                if content is not None:
                    kwargs["content"] = content
                    kwargs["params"] = {"expected_revision": 1}
                response = self.client.request(method, url, **kwargs)
                self.assertEqual(response.status_code, 403, response.text)
        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(FeedbackRegression)), 0
            )

    # ------------------------------------------------------------ enrollment gates

    def test_enrollment_blocks_unverified_and_validates_input(self):
        pending_id = self.submit_ok(self.add_answer().message_id)
        pending = self.enroll(pending_id, revision=1)
        self.assertEqual(pending.status_code, 409, pending.text)
        self.assertIn("feedback_not_verified", pending.json()["message"])

        rejected_id = self.submit_ok(self.add_answer().message_id)
        rejected = self.review(
            rejected_id,
            decision="rejected",
            reviewer_note="标注不成立",
            corrected_outcome=None,
            expected_sources=[],
        )
        self.assertEqual(rejected.status_code, 200, rejected.text)
        blocked = self.enroll(rejected_id, revision=2)
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("feedback_not_verified", blocked.json()["message"])

        feedback_id, _ = self.verified_feedback()
        revision_conflict = self.enroll(feedback_id, revision=99)
        self.assertEqual(revision_conflict.status_code, 409)
        self.assertIn("revision_conflict", revision_conflict.json()["message"])

        invalid_bodies = (
            ({"cause_note": ""}, 422),
            ({"cause_note": "   "}, 422),
            ({"cause_note": "长" * 1001}, 422),
            ({"cause_note": "bad\x00note"}, 422),
            ({"cause_note": "bad\x07note"}, 422),
            ({"cause": "bogus"}, 422),
        )
        for extra, expected in invalid_bodies:
            with self.subTest(extra=extra):
                body = {
                    "expected_feedback_revision": 2,
                    "cause": "retrieval",
                    "cause_note": "检索未召回",
                }
                body.update(extra)
                response = self.enroll(feedback_id, extra=body)
                self.assertEqual(response.status_code, expected, response.text)

        forged = {
            "expected_feedback_revision": 2,
            "cause": "retrieval",
            "cause_note": "检索未召回",
            "question": "伪造",
            "cause_note_extra": "x",
        }
        self.assertEqual(self.enroll(feedback_id, extra=forged).status_code, 422)
        self.assertEqual(self.enroll(uuid.uuid4()).status_code, 404)
        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(FeedbackRegression)), 0
            )

    def test_enrollment_is_idempotent_and_rejects_different_reason(self):
        feedback_id, _ = self.verified_feedback()
        first = self.enroll_ok(feedback_id, cause_note="  检索未召回  ")
        self.assertEqual(first["feedback_id"], feedback_id)
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["cause"], "retrieval")
        self.assertEqual(first["cause_note"], "检索未召回")
        self.assertEqual(first["current_attempt_no"], 0)
        self.assertTrue(first["available"])
        self.assertIsNone(first["unavailable_reason"])
        self.assertIsNone(first["current_run"])

        second = self.enroll_ok(feedback_id, cause_note="检索未召回")
        self.assertEqual(second["revision"], 1)
        self.assertEqual(second["cause_note"], "检索未召回")
        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(FeedbackRegression)), 1
            )

        different_cause = self.enroll(feedback_id, cause="generation")
        self.assertEqual(different_cause.status_code, 409, different_cause.text)
        self.assertIn("regression_exists", different_cause.json()["message"])
        different_note = self.enroll(feedback_id, cause_note="另一种说明")
        self.assertEqual(different_note.status_code, 409, different_note.text)

    def test_get_marks_stale_and_actions_are_blocked(self):
        feedback_id, target = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare_ok(feedback_id, revision=1)
        with SessionLocal() as db:
            db.get(Message, target.message_id).content = "被篡改后的回答"
            db.commit()

        data = self.get_regression(feedback_id).json()["data"]
        self.assertFalse(data["available"])
        self.assertEqual(data["unavailable_reason"], "feedback_stale")
        # 契约规定未导入轮次状态固定为 awaiting_report；不可用性由 available 表达。
        self.assertEqual(data["current_run"]["state"], "awaiting_report")

        prepare = self.prepare(feedback_id, revision=prepared["regression_revision"])
        self.assertEqual(prepare.status_code, 409, prepare.text)
        self.assertIn("feedback_stale", prepare.json()["message"])
        export = self.export_run(feedback_id, prepared["run"]["run_id"])
        self.assertEqual(export.status_code, 409, export.text)
        self.assertIn("feedback_stale", export.json()["message"])

    def test_get_marks_source_invalid_chunk_missing_and_version_mismatch(self):
        # 来源失效：文档被停用。
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        with SessionLocal() as db:
            db.get(Document, self.doc_id).enabled = False
            db.commit()
        data = self.get_regression(feedback_id).json()["data"]
        self.assertFalse(data["available"])
        self.assertEqual(data["unavailable_reason"], "source_invalid")
        self.assertIn(
            "source_invalid",
            self.prepare(feedback_id, revision=1).json()["message"],
        )

        # 恢复文档，另建反馈验证块缺失。
        with SessionLocal() as db:
            db.get(Document, self.doc_id).enabled = True
            db.commit()
        chunk_feedback, _ = self.verified_feedback(
            review_kwargs={
                "expected_sources": [
                    {"document_id": str(self.doc_id), "chunk_index": 0}
                ]
            }
        )
        self.enroll_ok(chunk_feedback, cause="parsing")
        with SessionLocal() as db:
            db.delete(
                db.scalar(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == self.doc_id,
                        DocumentChunk.chunk_index == 0,
                    )
                )
            )
            db.commit()
        chunk_data = self.get_regression(chunk_feedback).json()["data"]
        self.assertFalse(chunk_data["available"])
        self.assertEqual(chunk_data["unavailable_reason"], "chunk_missing")

        # 重建分块，另建反馈验证代次不匹配。
        with SessionLocal() as db:
            db.add(
                DocumentChunk(
                    document_id=self.doc_id,
                    chunk_index=0,
                    content="回归资料分块",
                    source_metadata={"filename": "source.md"},
                    search_text="回归资料分块",
                    embedded=True,
                )
            )
            db.commit()
        version_feedback, _ = self.verified_feedback(
            review_kwargs={
                "expected_sources": [
                    {
                        "document_id": str(self.doc_id),
                        "chunk_index": 0,
                        "index_version": 0,
                    }
                ]
            }
        )
        self.enroll_ok(version_feedback, cause="document_version")
        with SessionLocal() as db:
            db.get(Document, self.doc_id).index_version = 1
            db.commit()
        version_data = self.get_regression(version_feedback).json()["data"]
        self.assertFalse(version_data["available"])
        self.assertEqual(version_data["unavailable_reason"], "version_mismatch")

    def test_chat_conversation_can_enroll_and_prepare(self):
        feedback_id, _ = self.verified_feedback(mode="chat", kb_id=None)
        self.enroll_ok(feedback_id, cause="generation", cause_note="回答生成偏差")
        prepared = self.prepare_ok(feedback_id, revision=1)
        case = json.loads(prepared["jsonl"])
        self.assertEqual(case["mode"], "chat")
        self.assertNotIn("kb_id", case)
        self.assertEqual(case["expected_sources"], [])
        self.assertEqual(case["regression_mode"], "offline")

    def test_get_reports_reason_for_non_enrollable_feedback(self):
        # 历史行存在但当前资格已不满足时，GET 必须给出对应 reason（不 500）。
        pending_id = self.submit_ok(self.add_answer().message_id)

        project_kb_id, snapshot_id, file_version_id = self.project_binding(
            revoked=False,
            public=True,
            owner_id=self.owner_id,
            relative_path="src/main.py",
        )
        project_answer = self.add_answer(
            kb_id=project_kb_id, project_snapshot_id=snapshot_id
        )
        project_feedback = self.submit_ok(project_answer.message_id)
        self.assertEqual(
            self.review(
                project_feedback,
                expected_sources=[
                    {
                        "document_id": str(file_version_id),
                        "filename": "src/main.py",
                    }
                ],
            ).status_code,
            200,
        )

        with SessionLocal() as db:
            for feedback_id in (pending_id, project_feedback):
                db.add(
                    FeedbackRegression(
                        feedback_id=uuid.UUID(str(feedback_id)),
                        cause="other",
                        cause_note="历史记录",
                        created_by_name="reg_admin",
                        case_sha256="0" * 64,
                        dataset_sha256="1" * 64,
                        binding_sha256="2" * 64,
                    )
                )
            db.commit()

        pending = self.get_regression(pending_id).json()["data"]
        self.assertFalse(pending["available"])
        self.assertEqual(pending["unavailable_reason"], "feedback_not_verified")
        project = self.get_regression(project_feedback).json()["data"]
        self.assertFalse(project["available"])
        self.assertEqual(project["unavailable_reason"], "project_unsupported")

    def test_enrollment_blocks_project_scope_turns_ambiguity_and_incomplete(self):
        # 项目库/项目快照。
        project_kb_id, snapshot_id, file_version_id = self.project_binding(
            revoked=False,
            public=True,
            owner_id=self.owner_id,
            relative_path="src/main.py",
        )
        project_answer = self.add_answer(
            kb_id=project_kb_id, project_snapshot_id=snapshot_id
        )
        project_feedback = self.submit_ok(project_answer.message_id)
        reviewed = self.review(
            project_feedback,
            expected_sources=[
                {
                    "document_id": str(file_version_id),
                    "filename": "src/main.py",
                }
            ],
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        project = self.enroll(project_feedback)
        self.assertEqual(project.status_code, 409, project.text)
        self.assertIn("project_unsupported", project.json()["message"])

        # 曾变更文档范围。
        scoped = self.add_answer()
        with SessionLocal() as db:
            db.get(Conversation, scoped.conversation_id).scope_revision = 1
            db.commit()
        scoped_feedback = self.submit_ok(scoped.message_id)
        self.assertEqual(self.review(scoped_feedback).status_code, 200)
        scope = self.enroll(scoped_feedback)
        self.assertEqual(scope.status_code, 409, scope.text)
        self.assertIn("scope_unsupported", scope.json()["message"])

        # 超过 10 轮。
        many = self.create_multi_turn([f"回归问题{i}" for i in range(1, 12)])
        many_feedback = self.submit_ok(many.message_id)
        self.assertEqual(self.review(many_feedback).status_code, 200)
        turns = self.enroll(many_feedback)
        self.assertEqual(turns.status_code, 409, turns.text)
        self.assertIn("turns_overflow", turns.json()["message"])

        # 文件名在库内解析为多个文档。
        ambiguous_feedback, _ = self.verified_feedback(
            review_kwargs={"expected_sources": [{"filename": "source.md"}]}
        )
        self.assertIsNone(self.get_regression(ambiguous_feedback).json()["data"])
        with SessionLocal() as db:
            db.add(self._new_document(db, self.kb_id, "source.md"))
            db.commit()
        ambiguous = self.enroll(ambiguous_feedback)
        self.assertEqual(ambiguous.status_code, 409, ambiguous.text)
        self.assertIn("source_ambiguous", ambiguous.json()["message"])

        # 已核实但标注被破坏：仍必须阻断。
        incomplete_feedback, _ = self.verified_feedback()
        with SessionLocal() as db:
            db.get(AnswerFeedback, uuid.UUID(incomplete_feedback)).corrected_outcome = None
            db.commit()
        incomplete = self.enroll(incomplete_feedback)
        self.assertEqual(incomplete.status_code, 409, incomplete.text)
        self.assertIn("annotation_incomplete", incomplete.json()["message"])

    # ------------------------------------------------------------ prepare/export

    def test_prepare_rounds_idempotency_export_and_history(self):
        feedback_id, _ = self.verified_feedback(question="多轮之外的问题")
        self.enroll_ok(feedback_id, cause="retrieval", cause_note="检索未召回")
        request_id = uuid.uuid4()
        first_response = self.prepare(
            feedback_id, revision=1, mode="offline", request_id=request_id
        )
        self.assertEqual(first_response.status_code, 200, first_response.text)
        self.assertEqual(first_response.headers.get("cache-control"), "no-store")
        first = first_response.json()["data"]
        self.assertEqual(first["regression_revision"], 2)
        run = first["run"]
        self.assertEqual(run["attempt_no"], 1)
        self.assertTrue(run["is_current"])
        self.assertEqual(run["state"], "awaiting_report")
        self.assertEqual(run["requested_mode"], "offline")
        self.assertEqual(run["protocol_version"], 1)
        self.assertEqual(first["case_count"], 1)
        self.assertTrue(first["filename"].endswith(".jsonl"))
        self.assertTrue(first["jsonl"].endswith("\n"))
        lines = first["jsonl"].splitlines()
        self.assertEqual(len(lines), 1)
        case = json.loads(lines[0])
        self.assertEqual(case["id"], f"feedback-{feedback_id}")
        self.assertEqual(case["turns"], ["多轮之外的问题"])
        self.assertEqual(case["question"], "多轮之外的问题")
        self.assertEqual(case["top_k"], 6)
        self.assertEqual(case["regression_protocol"], 1)
        self.assertEqual(case["regression_mode"], "offline")
        self.assertNotIn("conversation_id", case)
        self.assertIn("--run-id " + run["run_id"], first["cli_hint"])
        self.assertIn("--require-reviewed", first["cli_hint"])
        self.assertNotIn("RAG_EVAL_TOKEN", first["cli_hint"])
        self.assertIn("--responses", first["cli_hint"])
        self.assertEqual(
            build_identity([case], mode="offline")["dataset"]["sha256"],
            first["dataset_sha256"],
        )

        replay = self.prepare_ok(
            feedback_id, revision=1, mode="offline", request_id=request_id
        )
        self.assertEqual(replay["run"]["run_id"], run["run_id"])
        self.assertEqual(replay["regression_revision"], 2)
        self.assertEqual(replay["jsonl"], first["jsonl"])
        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(
                    select(func.count())
                    .select_from(FeedbackRegressionRun)
                    .where(
                        FeedbackRegressionRun.regression_id
                        == uuid.UUID(str(self.regression_row(feedback_id).id))
                    )
                ),
                1,
            )

        wrong_mode = self.prepare(
            feedback_id, revision=2, mode="live", request_id=request_id
        )
        self.assertEqual(wrong_mode.status_code, 409, wrong_mode.text)
        self.assertIn("prepare_mode_conflict", wrong_mode.json()["message"])

        wrong_revision = self.prepare(feedback_id, revision=99)
        self.assertEqual(wrong_revision.status_code, 409, wrong_revision.text)
        self.assertIn("revision_conflict", wrong_revision.json()["message"])

        second = self.prepare_ok(feedback_id, revision=2, mode="live")
        self.assertEqual(second["regression_revision"], 3)
        self.assertEqual(second["run"]["attempt_no"], 2)
        self.assertEqual(second["run"]["requested_mode"], "live")
        self.assertIn("--live", second["cli_hint"])

        old_export = self.export_run(feedback_id, run["run_id"])
        self.assertEqual(old_export.status_code, 409, old_export.text)
        self.assertIn("run_not_current", old_export.json()["message"])
        current_export = self.export_run(feedback_id, second["run"]["run_id"])
        self.assertEqual(current_export.status_code, 200, current_export.text)
        self.assertEqual(
            current_export.headers.get("cache-control"), "no-store"
        )
        self.assertEqual(
            current_export.json()["data"]["run"]["run_id"],
            second["run"]["run_id"],
        )
        self.assertIn(
            "--run-id " + second["run"]["run_id"],
            current_export.json()["data"]["cli_hint"],
        )

        page_one = self.history(feedback_id, page=1, page_size=1)
        self.assertEqual(page_one.status_code, 200, page_one.text)
        self.assertEqual(page_one.json()["data"]["total"], 2)
        self.assertEqual(page_one.json()["data"]["items"][0]["attempt_no"], 2)
        self.assertTrue(page_one.json()["data"]["items"][0]["is_current"])
        page_two = self.history(feedback_id, page=2, page_size=1)
        self.assertEqual(page_two.json()["data"]["items"][0]["attempt_no"], 1)
        self.assertFalse(page_two.json()["data"]["items"][0]["is_current"])

        current = self.get_regression(feedback_id).json()["data"]
        self.assertEqual(current["revision"], 3)
        self.assertEqual(current["current_attempt_no"], 2)
        self.assertEqual(current["current_run"]["attempt_no"], 2)
        self.assertTrue(current["available"])
        self.assertIsNone(current["unavailable_reason"])

    def test_prepare_refuses_changed_sample_without_new_round(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare_ok(feedback_id, revision=1)
        with SessionLocal() as db:
            db.get(Document, self.doc_id).enabled = False
            db.commit()
        response = self.prepare(feedback_id, revision=prepared["regression_revision"])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("source_invalid", response.json()["message"])
        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(FeedbackRegressionRun)), 1
            )
            regression = self.regression_row(feedback_id)
            self.assertEqual(regression.revision, prepared["regression_revision"])

    # ------------------------------------------------------------ import

    def test_import_offline_consistency_only_idempotent_and_history(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare_ok(feedback_id, revision=1)
        report = self.build_report(prepared)
        raw = self.report_bytes(report)
        response = self.import_report(
            feedback_id,
            prepared["run"]["run_id"],
            revision=prepared["regression_revision"],
            body=raw,
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["regression_revision"], 3)
        run = data["run"]
        self.assertEqual(run["state"], "imported")
        self.assertEqual(run["validation_level"], "consistency_only")
        self.assertEqual(run["report_mode"], "offline")
        self.assertTrue(run["passed"])
        self.assertEqual(run["failures"], [])
        self.assertEqual(run["outcome"], "answer")
        self.assertEqual(run["http_status"], 200)
        self.assertEqual(run["citation_count"], 1)
        self.assertRegex(run["report_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(run["result_sha256"], r"^[a-f0-9]{64}$")
        self.assertRegex(run["machine_sha256"], r"^[a-f0-9]{64}$")
        self.assertIsNotNone(run["imported_at"])
        self.assertEqual(run["imported_by_name"], "reg_admin")

        detail_response = self.get_regression(feedback_id)
        self.assertEqual(detail_response.headers.get("cache-control"), "no-store")
        detail = detail_response.json()["data"]
        self.assertEqual(detail["revision"], 3)
        self.assertEqual(detail["current_run"]["state"], "imported")
        self.assertTrue(detail["current_run"]["is_current"])

        history = self.history(feedback_id)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.headers.get("cache-control"), "no-store")
        item = history.json()["data"]["items"][0]
        self.assertEqual(item["state"], "imported")
        self.assertEqual(item["validation_level"], "consistency_only")
        self.assertRegex(item["report_sha256"], r"^[a-f0-9]{64}$")

        # 客户端超时后携带旧 revision 重放：同一规范工件必须幂等返回。
        replay = self.import_report(
            feedback_id,
            prepared["run"]["run_id"],
            revision=2,
            body=raw,
        )
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["data"]["regression_revision"], 3)
        with SessionLocal() as db:
            regression = self.regression_row(feedback_id)
            self.assertEqual(regression.revision, 3)
            stored = db.get(
                FeedbackRegressionRun, uuid.UUID(prepared["run"]["run_id"])
            )
            self.assertEqual(stored.validation_level, "consistency_only")
            self.assertEqual(stored.imported_by_name, "reg_admin")

        # 新准备不能让已导入的历史轮次变成 stale（绑定不含 attempt/revision）。
        self.prepare_ok(feedback_id, revision=3)
        history_after = self.history(feedback_id).json()["data"]
        by_attempt = {item["attempt_no"]: item for item in history_after["items"]}
        self.assertEqual(by_attempt[1]["state"], "imported")
        self.assertFalse(by_attempt[1]["is_current"])
        self.assertEqual(by_attempt[2]["state"], "awaiting_report")
        self.assertTrue(by_attempt[2]["is_current"])

        # 不含任何正文/报告列。
        columns = set(FeedbackRegressionRun.__table__.columns.keys())
        self.assertNotIn("content", columns)
        self.assertNotIn("jsonl", columns)
        self.assertNotIn("report", columns)

        # 样例失效后，已导入轮次只能作为 stale 历史，不能继续显示为当前通过。
        with SessionLocal() as db:
            db.get(Message, self._message_id(feedback_id)).content = "被修改的回答"
            db.commit()
        stale_history = self.history(feedback_id).json()["data"]
        self.assertEqual(
            {item["attempt_no"]: item["state"] for item in stale_history["items"]},
            {1: "stale", 2: "awaiting_report"},
        )
        stale_detail = self.get_regression(feedback_id).json()["data"]
        self.assertFalse(stale_detail["available"])
        self.assertEqual(stale_detail["unavailable_reason"], "feedback_stale")
        self.assertEqual(stale_detail["current_run"]["state"], "awaiting_report")

    @staticmethod
    def _message_id(feedback_id):
        with SessionLocal() as db:
            return db.scalar(
                select(AnswerFeedback.message_id).where(
                    AnswerFeedback.id == uuid.UUID(str(feedback_id))
                )
            )

    def test_import_rejects_invalid_reports_without_writes(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare_ok(feedback_id, revision=1)
        run_id = prepared["run"]["run_id"]
        valid = self.build_report(prepared)

        wrong_run_id = self.build_report(prepared, run_id=str(uuid.uuid4()))
        wrong_dataset = self.build_report(
            prepared, dataset_sha256="0" * 64
        )
        invalid_reports = (
            b"{not-json",
            self.report_bytes({**valid, "run_id": str(uuid.uuid4())}),
            self.report_bytes(wrong_run_id),
            self.report_bytes(wrong_dataset),
        )
        for raw in invalid_reports:
            with self.subTest(size=len(raw)):
                response = self.import_report(
                    feedback_id,
                    run_id,
                    revision=prepared["regression_revision"],
                    body=raw,
                )
                self.assertIn(response.status_code, (400, 409), response.text)
                stored = self.run_row(run_id)
                self.assertIsNone(stored.imported_at)
                self.assertIsNone(stored.report_sha256)
                self.assertEqual(
                    self.regression_row(feedback_id).revision,
                    prepared["regression_revision"],
                )

        oversized = self.import_report(
            feedback_id,
            run_id,
            revision=prepared["regression_revision"],
            body=b"x" * (2 * 1024 * 1024 + 1),
        )
        self.assertEqual(oversized.status_code, 413, oversized.text)
        empty = self.import_report(
            feedback_id,
            run_id,
            revision=prepared["regression_revision"],
            body=b"",
        )
        self.assertEqual(empty.status_code, 400, empty.text)
        stored = self.run_row(run_id)
        self.assertIsNone(stored.imported_at)
        self.assertEqual(
            self.regression_row(feedback_id).revision,
            prepared["regression_revision"],
        )

    def test_import_content_regraded(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare_ok(feedback_id, revision=1)
        report = self.build_report(prepared, include_content=True)
        response = self.import_report(
            feedback_id,
            prepared["run"]["run_id"],
            revision=prepared["regression_revision"],
            body=self.report_bytes(report),
        )
        self.assertEqual(response.status_code, 200, response.text)
        run = response.json()["data"]["run"]
        self.assertEqual(run["validation_level"], "content_regraded")
        self.assertEqual(run["report_mode"], "offline")
        self.assertTrue(run["passed"])
        self.assertEqual(run["failures"], [])

        # 有正文报告被篡改内容：必须拒绝且零写入。
        second = self.prepare_ok(feedback_id, revision=3)
        tampered = self.build_report(second, include_content=True)
        tampered["results"][0]["content"]["answer"] = "被篡改的回答"
        response = self.import_report(
            feedback_id,
            second["run"]["run_id"],
            revision=second["regression_revision"],
            body=self.report_bytes(tampered),
        )
        self.assertIn(response.status_code, (400, 409), response.text)
        stored = self.run_row(second["run"]["run_id"])
        self.assertIsNone(stored.imported_at)
        self.assertIsNone(stored.report_sha256)

    def test_same_run_different_report_and_old_round_late_import(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        first = self.prepare_ok(feedback_id, revision=1, mode="offline")
        second = self.prepare_ok(feedback_id, revision=2, mode="offline")

        late = self.import_report(
            feedback_id,
            first["run"]["run_id"],
            revision=second["regression_revision"],
            body=self.report_bytes(self.build_report(first)),
        )
        self.assertEqual(late.status_code, 409, late.text)
        self.assertIn("run_not_current", late.json()["message"])
        self.assertIsNone(self.run_row(first["run"]["run_id"]).imported_at)

        good = self.import_report(
            feedback_id,
            second["run"]["run_id"],
            revision=second["regression_revision"],
            body=self.report_bytes(self.build_report(second)),
        )
        self.assertEqual(good.status_code, 200, good.text)

        conflicting = self.import_report(
            feedback_id,
            second["run"]["run_id"],
            revision=second["regression_revision"],
            body=self.report_bytes(self.build_report(second, answer="另一个回答")),
        )
        self.assertEqual(conflicting.status_code, 409, conflicting.text)
        self.assertIn("report_conflict", conflicting.json()["message"])
        stored = self.run_row(second["run"]["run_id"])
        self.assertEqual(
            stored.report_sha256,
            good.json()["data"]["run"]["report_sha256"],
        )
        self.assertEqual(self.regression_row(feedback_id).revision, 4)

    def test_concurrent_import_writes_once(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare_ok(feedback_id, revision=1)
        raw = self.report_bytes(self.build_report(prepared))
        url = (
            f"/api/v1/feedback/{feedback_id}/regression/runs/"
            f"{prepared['run']['run_id']}/import"
        )
        headers = dict(self.admin_headers)

        def send():
            client = TestClient(self.app)
            try:
                return client.post(
                    url,
                    headers=headers,
                    params={"expected_revision": 2},
                    content=raw,
                )
            finally:
                client.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: send(), range(2)))
        statuses = sorted(response.status_code for response in responses)
        self.assertTrue(all(status in (200, 409) for status in statuses), statuses)
        self.assertIn(200, statuses)
        with SessionLocal() as db:
            run = db.get(FeedbackRegressionRun, uuid.UUID(prepared["run"]["run_id"]))
            self.assertIsNotNone(run.imported_at)
            self.assertRegex(run.report_sha256, r"^[a-f0-9]{64}$")
            regression = self.regression_row(feedback_id)
            self.assertEqual(regression.revision, 3)

    def test_delete_feedback_cascades_regression_and_runs(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare_ok(feedback_id, revision=1)
        imported = self.import_report(
            feedback_id,
            prepared["run"]["run_id"],
            revision=prepared["regression_revision"],
            body=self.report_bytes(self.build_report(prepared)),
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        with SessionLocal() as db:
            db.delete(db.get(AnswerFeedback, uuid.UUID(feedback_id)))
            db.commit()
        with SessionLocal() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(FeedbackRegression)), 0
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(FeedbackRegressionRun)), 0
            )
        self.assertEqual(self.get_regression(feedback_id).status_code, 404)

    # ------------------------------------------------------------ audit

    def test_audit_whitelist_omits_body_and_get_does_not_audit(self):
        feedback_id, _ = self.verified_feedback(
            question="绝密回归问题ABC",
            answer="绝密回归回答DEF",
        )
        self.enroll_ok(feedback_id, cause_note="绝密原因说明GHI")
        prepared = self.prepare_ok(feedback_id, revision=1)
        export = self.export_run(feedback_id, prepared["run"]["run_id"])
        self.assertEqual(export.status_code, 200, export.text)
        imported = self.import_report(
            feedback_id,
            prepared["run"]["run_id"],
            revision=prepared["regression_revision"],
            body=self.report_bytes(self.build_report(prepared)),
        )
        self.assertEqual(imported.status_code, 200, imported.text)

        actions = (
            "feedback_regression_enroll",
            "feedback_regression_prepare",
            "feedback_regression_export",
            "feedback_regression_import",
        )
        with SessionLocal() as db:
            rows = db.scalars(
                select(AuditLog).where(AuditLog.action.in_(actions))
            ).all()
        self.assertEqual({row.action for row in rows}, set(actions))
        serialized = json.dumps(
            {
                row.action: {"target": row.target, "detail": row.detail}
                for row in rows
            },
            ensure_ascii=False,
        )
        for secret in ("绝密回归问题ABC", "绝密回归回答DEF", "绝密原因说明GHI"):
            self.assertNotIn(secret, serialized)
        details = {row.action: set(row.detail or {}) for row in rows}
        self.assertEqual(
            details["feedback_regression_enroll"], {"cause", "revision"}
        )
        self.assertEqual(
            details["feedback_regression_prepare"], {"attempt_no", "revision"}
        )
        self.assertEqual(
            details["feedback_regression_export"], {"attempt_no", "case_count"}
        )
        self.assertEqual(
            details["feedback_regression_import"],
            {"attempt_no", "validation_level", "passed", "failure_count"},
        )

        with SessionLocal() as db:
            before = db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action.in_(actions))
            )
        self.assertEqual(self.get_regression(feedback_id).status_code, 200)
        self.assertEqual(self.history(feedback_id).status_code, 200)
        with SessionLocal() as db:
            after = db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action.in_(actions))
            )
        self.assertEqual(before, after)
