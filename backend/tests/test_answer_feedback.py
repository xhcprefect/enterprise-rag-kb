"""答案反馈：真实隔离 PostgreSQL + 真 FastAPI/JWT 权限链路回归。

覆盖：仅会话所有者可提交、幂等、管理员列表/详情、核实 CAS 与约束、
结果指纹过期、失效来源、项目快照撤销传播，以及显式导出的 JSONL 校验。
"""
import hashlib
import json
import re
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from tests import _memory_project_test_gate as gate

TEST_URL = gate.TEST_DATABASE_URL

if TEST_URL:
    from sqlalchemy import create_engine, func, select, text
    from sqlalchemy.engine import make_url

    from app.db import session as db_session

    _target = make_url(TEST_URL)
    if db_session.engine.url != _target:
        db_session.engine.dispose()
        db_session.engine = create_engine(
            TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20
        )
        db_session.SessionLocal.configure(bind=db_session.engine)

    from app.core.security import create_access_token
    from app.db.init_db import init_db
    from app.models import (
        AnswerFeedback,
        AuditLog,
        ChatTurn,
        Conversation,
        Document,
        KnowledgeBase,
        Message,
        ProjectFileVersion,
        ProjectSnapshot,
        ProjectSnapshotFile,
        ProjectSource,
        User,
    )
    from app.services.answer_feedback import result_fingerprint
    from app.services.evaluation import dataset_identity, review_state, validate_cases

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


TABLES = (
    "users, knowledge_bases, documents, document_chunks, upload_sessions, "
    "audit_logs, conversations, chat_turns, messages, model_usage, "
    "usage_tracking_state, answer_feedbacks, project_sources, project_snapshots, "
    "project_file_versions, project_snapshot_files, project_chunks"
)


def _templates_digest() -> str | None:
    """仓库内评测模板目录的字节摘要；导出绝不能改动它们。"""
    root = Path(__file__).resolve().parents[1] / "evaluation" / "templates"
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


class _Helpers:
    """fixture 与请求工具；仅在 DB 门通过后执行。"""

    def headers(self, user_id, role):
        return {
            "Authorization": "Bearer " + create_access_token(str(user_id), role)
        }

    def add_answer(
        self,
        *,
        owner_id=None,
        mode="rag",
        kb_id="default",
        question="合成问题",
        answer="合成回答",
        status="completed",
        content=None,
        citations=None,
        with_turn=True,
        turn_seq=1,
        project_snapshot_id=None,
    ):
        owner_id = self.owner_id if owner_id is None else owner_id
        kb_id = self.kb_id if kb_id == "default" else kb_id
        with SessionLocal() as db:
            conversation = Conversation(
                user_id=owner_id,
                mode=mode,
                kb_id=kb_id,
                project_snapshot_id=project_snapshot_id,
                title="反馈测试",
            )
            db.add(conversation)
            db.flush()
            question_id = uuid.uuid4()
            turn_id = uuid.uuid4()
            question_row = Message(
                id=question_id, conversation_id=conversation.id, role="user",
                content=question, status="completed", turn_seq=turn_seq,
            )
            answer_row = Message(
                conversation_id=conversation.id, role="assistant",
                content=answer if content is None else content, status=status,
                turn_seq=turn_seq, reply_to_id=question_id, citations=citations,
            )
            if with_turn:
                db.add(
                    ChatTurn(
                        id=turn_id, request_id=uuid.uuid4(), user_id=owner_id,
                        conversation_id=conversation.id, turn_seq=turn_seq,
                        user_message_id=question_id, payload_hash="0" * 64,
                        status="completed", heartbeat_at=datetime.now(timezone.utc),
                    )
                )
                question_row.turn_id = turn_id
                answer_row.turn_id = turn_id
            db.add_all([question_row, answer_row])
            db.commit()
            return SimpleNamespace(
                conversation_id=conversation.id,
                message_id=answer_row.id,
                question_id=question_id,
                turn_id=turn_id if with_turn else None,
            )

    def create_multi_turn(self, questions, *, insert_order=None, mode="rag", kb_id="default"):
        kb_id = self.kb_id if kb_id == "default" else kb_id
        with SessionLocal() as db:
            conversation = Conversation(
                user_id=self.owner_id, mode=mode, kb_id=kb_id, title="多轮反馈"
            )
            db.add(conversation)
            db.flush()
            last = None
            order = insert_order or list(range(1, len(questions) + 1))
            # 故意按给定顺序插入，验证导出优先按 turn_seq 而非 created_at 排序。
            for seq in order:
                question = questions[seq - 1]
                question_id = uuid.uuid4()
                answer_id = uuid.uuid4()
                turn_id = uuid.uuid4()
                question_row = Message(
                    id=question_id, conversation_id=conversation.id, role="user",
                    content=question, status="completed", turn_seq=seq,
                )
                answer_row = Message(
                    id=answer_id, conversation_id=conversation.id, role="assistant",
                    content=f"回答{seq}", status="completed", turn_seq=seq,
                    reply_to_id=question_id, citations=[],
                )
                db.add(
                    ChatTurn(
                        id=turn_id, request_id=uuid.uuid4(), user_id=self.owner_id,
                        conversation_id=conversation.id, turn_seq=seq,
                        user_message_id=question_id, payload_hash="0" * 64,
                        status="completed", heartbeat_at=datetime.now(timezone.utc),
                    )
                )
                question_row.turn_id = turn_id
                answer_row.turn_id = turn_id
                db.add_all([question_row, answer_row])
                last = SimpleNamespace(
                    conversation_id=conversation.id,
                    message_id=answer_id,
                    question_id=question_id,
                    turn_id=turn_id,
                )
            db.commit()
            return last

    def project_binding(self, *, revoked, public, owner_id, relative_path=None):
        with SessionLocal() as db:
            kb = KnowledgeBase(
                name="proj-" + uuid.uuid4().hex,
                collection_name="proj_" + uuid.uuid4().hex,
                owner_id=owner_id,
                is_public=public,
                kind="project",
            )
            db.add(kb)
            db.flush()
            source = ProjectSource(
                kb_id=kb.id, name="source", root_key="root", relative_dir="",
                root_fingerprint="0" * 64, created_by=owner_id,
            )
            db.add(source)
            db.flush()
            snapshot = ProjectSnapshot(source_id=source.id, status="ready")
            db.add(snapshot)
            db.flush()
            source.active_snapshot_id = snapshot.id
            if revoked:
                source.revoked_at = datetime.now(timezone.utc)
            file_version_id = None
            if relative_path is not None:
                version = ProjectFileVersion(
                    source_id=source.id, relative_path=relative_path,
                    content_sha256="a" * 64,
                    blob_path="/tmp/" + uuid.uuid4().hex,
                    parse_done=True, parse_status="ready",
                )
                db.add(version)
                db.flush()
                db.add(
                    ProjectSnapshotFile(
                        snapshot_id=snapshot.id, relative_path=relative_path,
                        file_version_id=version.id,
                    )
                )
                file_version_id = version.id
            db.commit()
            return kb.id, snapshot.id, file_version_id

    def insert_verified_feedback(self, target, *, expected_sources=None):
        """直接落库一条已核实反馈，用于提交门本身已拒绝的历史/损坏场景。"""
        with SessionLocal() as db:
            message = db.get(Message, target.message_id)
            feedback = AnswerFeedback(
                user_id=self.owner_id,
                message_id=target.message_id,
                conversation_id=target.conversation_id,
                category="inaccurate_answer",
                note="",
                status="verified",
                revision=2,
                result_sha256=result_fingerprint(db, message),
                reviewer_id=self.admin_id,
                reviewer_name="fb_admin",
                reviewed_at=datetime.now(timezone.utc),
                corrected_outcome="answer",
                expected_sources=list(expected_sources or []),
                required_phrases=[],
                forbidden_phrases=[],
                unknowns=[],
                evidence_note="证据",
                reviewer_note="",
            )
            db.add(feedback)
            db.commit()
            return feedback.id

    def submit(self, message_id, *, category="inaccurate_answer", note="", headers=None, extra=None):
        body = {
            "message_id": str(message_id),
            "category": category,
            "note": note,
        }
        if extra:
            body.update(extra)
        return self.client.post(
            "/api/v1/feedback", headers=headers or self.owner_headers, json=body
        )

    def submit_ok(self, message_id, **kwargs):
        response = self.submit(message_id, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]["id"]

    def review(
        self,
        feedback_id,
        *,
        revision=1,
        decision="verified",
        corrected_outcome="answer",
        expected_sources=None,
        required_phrases=None,
        forbidden_phrases=None,
        unknowns=None,
        evidence_note="证据：原回答与引用不符",
        reviewer_note="核验备注",
        headers=None,
        omit=(),
    ):
        if expected_sources is None:
            expected_sources = [{"document_id": str(self.doc_id)}]
        body = {
            "expected_revision": revision,
            "decision": decision,
            "corrected_outcome": corrected_outcome,
            "expected_sources": expected_sources,
            "required_phrases": list(required_phrases or []),
            "forbidden_phrases": list(forbidden_phrases or []),
            "unknowns": list(unknowns or []),
            "evidence_note": evidence_note,
            "reviewer_note": reviewer_note,
        }
        for key in omit:
            body.pop(key, None)
        return self.client.patch(
            f"/api/v1/feedback/{feedback_id}/review",
            headers=headers or self.admin_headers,
            json=body,
        )

    def detail(self, feedback_id, *, headers=None):
        return self.client.get(
            f"/api/v1/feedback/{feedback_id}", headers=headers or self.admin_headers
        )

    def export(self, feedback_id=None, *, headers=None):
        url = "/api/v1/feedback/export"
        if feedback_id is not None:
            url += f"?feedback_id={feedback_id}"
        return self.client.get(url, headers=headers or self.admin_headers)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离答案反馈数据库测试")
class AnswerFeedbackTests(_Helpers, unittest.TestCase):
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
            owner = User(username="fb_owner", role="user", is_active=True, password_hash="x")
            other = User(username="fb_other", role="user", is_active=True, password_hash="x")
            admin = User(username="fb_admin", role="admin", is_active=True, password_hash="x")
            db.add_all([owner, other, admin])
            db.flush()
            kb = KnowledgeBase(
                name="fb_kb", collection_name="fb_" + uuid.uuid4().hex,
                owner_id=owner.id, is_public=True, kind="upload",
            )
            private_kb = KnowledgeBase(
                name="fb_private", collection_name="fb_" + uuid.uuid4().hex,
                owner_id=admin.id, is_public=False, kind="upload",
            )
            db.add_all([kb, private_kb])
            db.flush()
            doc = Document(
                kb_id=kb.id, filename="source.md", file_ext=".md", file_size=1,
                stored_path="/tmp/" + uuid.uuid4().hex + ".md", status="ready",
                index_version=0, cancel_requested=False, parse_done=True,
                chunk_count=1, enabled=True,
            )
            db.add(doc)
            db.commit()
            self.owner_id, self.other_id, self.admin_id = owner.id, other.id, admin.id
            self.kb_id, self.private_kb_id = kb.id, private_kb.id
            self.doc_id = doc.id
        self.owner_headers = self.headers(self.owner_id, "user")
        self.other_headers = self.headers(self.other_id, "user")
        self.admin_headers = self.headers(self.admin_id, "admin")

    # ---------------------------------------------------------------- 提交

    def test_only_conversation_owner_can_submit(self):
        target = self.add_answer()
        self.assertEqual(self.submit(target.message_id, headers=self.other_headers).status_code, 404)
        # 管理员也不能替别人提交。
        self.assertEqual(self.submit(target.message_id, headers=self.admin_headers).status_code, 404)
        self.assertEqual(self.submit(target.message_id).status_code, 200)

    def test_only_completed_assistant_answer_accepts_feedback(self):
        failed = self.add_answer(status="failed")
        self.assertEqual(self.submit(failed.message_id).status_code, 409)
        blank = self.add_answer(content="   ")
        self.assertEqual(self.submit(blank.message_id).status_code, 409)
        user_message = self.add_answer()
        self.assertEqual(self.submit(user_message.question_id).status_code, 400)
        self.assertEqual(self.submit(uuid.uuid4()).status_code, 404)

    def test_forged_answer_fields_are_rejected(self):
        target = self.add_answer()
        for extra in (
            {"question": "伪造问题"},
            {"answer": "伪造回答"},
            {"citations": []},
            {"content": "伪造正文"},
            {"status": "verified"},
            {"conversation_id": str(uuid.uuid4())},
        ):
            with self.subTest(extra=extra):
                response = self.submit(target.message_id, extra=extra)
                self.assertEqual(response.status_code, 422, response.text)

    def test_duplicate_submission_is_idempotent(self):
        target = self.add_answer()
        first = self.submit(target.message_id, category="inaccurate_answer", note="第一次备注")
        self.assertEqual(first.status_code, 200, first.text)
        first_data = first.json()["data"]
        second = self.submit(target.message_id, category="citation_error", note="第二次备注")
        self.assertEqual(second.status_code, 200, second.text)
        second_data = second.json()["data"]
        self.assertEqual(second_data["id"], first_data["id"])
        self.assertEqual(second_data["category"], "inaccurate_answer")
        self.assertEqual(second_data["note"], "第一次备注")
        self.assertEqual(second_data["status"], "pending")
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(AnswerFeedback)), 1)

    def test_note_validation(self):
        target = self.add_answer()
        self.assertEqual(
            self.submit(target.message_id, note="a" * 1001).status_code, 422
        )
        self.assertEqual(
            self.submit(target.message_id, note="bad\x00note").status_code, 422
        )
        self.assertEqual(
            self.submit(target.message_id, note="bad\x07note").status_code, 422
        )

    # ---------------------------------------------------------------- 列表

    def test_admin_list_hides_body_and_reports_note_presence(self):
        target = self.add_answer(
            question="绝密问题ABC",
            answer="绝密回答DEF",
            citations=[{
                "document_id": str(self.doc_id), "filename": "source.md",
                "snippet": "绝密片段GHI", "score": 0.9,
            }],
        )
        with_note = self.submit_ok(target.message_id, note="绝密备注JKL", category="citation_error")
        without_note = self.submit_ok(self.add_answer().message_id, note="")
        response = self.client.get("/api/v1/feedback", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        for secret in ("绝密问题ABC", "绝密回答DEF", "绝密片段GHI", "绝密备注JKL"):
            self.assertNotIn(secret, response.text)
        items = {item["id"]: item for item in response.json()["data"]["items"]}
        self.assertTrue(items[with_note]["note_present"])
        self.assertFalse(items[without_note]["note_present"])
        self.assertNotIn("note", items[with_note])
        self.assertNotIn("question", items[with_note])
        self.assertNotIn("answer", items[with_note])

        filtered = self.client.get(
            "/api/v1/feedback?category=citation_error", headers=self.admin_headers
        )
        self.assertEqual(filtered.status_code, 200, filtered.text)
        self.assertEqual(
            [item["id"] for item in filtered.json()["data"]["items"]], [with_note]
        )
        # 非管理员不能访问管理员列表。
        self.assertEqual(
            self.client.get("/api/v1/feedback", headers=self.owner_headers).status_code, 403
        )

    def test_mine_only_own_with_pagination_and_status_filter(self):
        first = self.submit_ok(self.add_answer(question="一").message_id)
        second = self.submit_ok(self.add_answer(question="二").message_id)
        third = self.submit_ok(self.add_answer(question="三").message_id)
        other_answer = self.add_answer(owner_id=self.other_id, question="别人的")
        other_feedback = self.submit_ok(other_answer.message_id, headers=self.other_headers)

        mine = self.client.get("/api/v1/feedback/mine", headers=self.owner_headers)
        self.assertEqual(mine.status_code, 200, mine.text)
        self.assertEqual(mine.json()["data"]["total"], 3)
        self.assertEqual(
            {item["id"] for item in mine.json()["data"]["items"]}, {first, second, third}
        )
        page_one = self.client.get(
            "/api/v1/feedback/mine?page=1&page_size=2", headers=self.owner_headers
        )
        page_two = self.client.get(
            "/api/v1/feedback/mine?page=2&page_size=2", headers=self.owner_headers
        )
        self.assertEqual(page_one.json()["data"]["total"], 3)
        self.assertEqual(len(page_one.json()["data"]["items"]), 2)
        self.assertEqual(len(page_two.json()["data"]["items"]), 1)
        self.assertEqual(
            {item["id"] for item in page_one.json()["data"]["items"]}
            | {item["id"] for item in page_two.json()["data"]["items"]},
            {first, second, third},
        )
        other_mine = self.client.get("/api/v1/feedback/mine", headers=self.other_headers)
        self.assertEqual(
            [item["id"] for item in other_mine.json()["data"]["items"]], [other_feedback]
        )

        self.assertEqual(self.review(first).status_code, 200)
        verified = self.client.get(
            "/api/v1/feedback/mine?status=verified", headers=self.owner_headers
        )
        pending = self.client.get(
            "/api/v1/feedback/mine?status=pending", headers=self.owner_headers
        )
        self.assertEqual([item["id"] for item in verified.json()["data"]["items"]], [first])
        self.assertEqual(
            {item["id"] for item in pending.json()["data"]["items"]}, {second, third}
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/feedback/mine?status=bogus", headers=self.owner_headers
            ).status_code,
            422,
        )

    # ---------------------------------------------------------------- 核实

    def test_review_requires_revision_and_blocks_second_review(self):
        feedback_id = self.submit_ok(self.add_answer().message_id)
        missing = self.review(feedback_id, omit=("expected_revision",))
        self.assertEqual(missing.status_code, 422, missing.text)
        conflict = self.review(feedback_id, revision=9)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("反馈已被他人处理，请刷新", conflict.json()["message"])
        success = self.review(
            feedback_id, required_phrases=["甲", "甲", "乙"], forbidden_phrases=["丙"]
        )
        self.assertEqual(success.status_code, 200, success.text)
        data = success.json()["data"]
        self.assertEqual(data["status"], "verified")
        self.assertEqual(data["revision"], 2)
        self.assertEqual(data["reviewer_name"], "fb_admin")
        detail = self.detail(feedback_id).json()["data"]
        self.assertEqual(detail["required_phrases"], ["甲", "乙"])
        self.assertEqual(detail["forbidden_phrases"], ["丙"])
        self.assertEqual(detail["expected_sources"], [{"document_id": str(self.doc_id)}])
        # 已核实不可再次修改。
        again = self.review(feedback_id, revision=2)
        self.assertEqual(again.status_code, 409, again.text)
        detail = self.detail(feedback_id).json()["data"]
        self.assertEqual(detail["status"], "verified")
        self.assertEqual(detail["revision"], 2)

    def test_verified_and_rejected_constraints(self):
        rag_answer = self.add_answer()
        subtests = (
            ("missing_outcome", {"corrected_outcome": None}),
            ("missing_evidence", {"evidence_note": "   "}),
            ("unknowns_not_empty", {"unknowns": ["仍有缺口"]}),
            ("rag_answer_without_source", {"expected_sources": []}),
            ("too_many_phrases", {"required_phrases": [f"p{i}" for i in range(21)]}),
            ("phrase_too_long", {"required_phrases": ["长" * 201]}),
        )
        for name, kwargs in subtests:
            with self.subTest(name=name):
                feedback_id = self.submit_ok(rag_answer.message_id, note="")
                response = self.review(feedback_id, **kwargs)
                self.assertEqual(response.status_code, 400, response.text)
                detail = self.detail(feedback_id).json()["data"]
                self.assertEqual(detail["status"], "pending")
                self.assertEqual(detail["revision"], 1)
                # 清理本轮反馈，下一次用同一消息重新提交（unique user_id/message_id）。
                with SessionLocal() as db:
                    db.delete(db.get(AnswerFeedback, uuid.UUID(feedback_id)))
                    db.commit()

        chat_answer = self.add_answer(mode="chat", kb_id=None)
        feedback_id = self.submit_ok(chat_answer.message_id)
        response = self.review(
            feedback_id, expected_sources=[{"document_id": str(self.doc_id)}]
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("普通对话", response.json()["message"])

        rejected = self.submit_ok(self.add_answer().message_id)
        response = self.review(
            rejected, decision="rejected", reviewer_note="  ",
            corrected_outcome=None, expected_sources=[],
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("驳回", response.json()["message"])

    def test_rejected_clears_corrected_fields(self):
        feedback_id = self.submit_ok(self.add_answer().message_id)
        response = self.review(
            feedback_id,
            decision="rejected",
            reviewer_note="标注与原文不符",
            evidence_note="复核说明",
            required_phrases=["甲"],
            forbidden_phrases=["乙"],
            unknowns=["待查"],
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["status"], "rejected")
        detail = self.detail(feedback_id).json()["data"]
        self.assertIsNone(detail["corrected_outcome"])
        self.assertEqual(detail["expected_sources"], [])
        self.assertEqual(detail["required_phrases"], [])
        self.assertEqual(detail["forbidden_phrases"], [])
        self.assertEqual(detail["unknowns"], [])
        self.assertEqual(detail["reviewer_note"], "标注与原文不符")
        self.assertEqual(detail["evidence_note"], "复核说明")

    # ---------------------------------------------------------------- 详情

    def test_detail_sanitizes_citations_and_reports_invalid_sources(self):
        missing_document = uuid.uuid4()
        target = self.add_answer(
            citations=[
                {
                    "document_id": str(self.doc_id), "filename": "source.md",
                    "chunk_index": 0, "index_version": 0, "snippet": "片段",
                    "score": 0.8, "evil": {"nested": "x"},
                },
                {
                    "document_id": str(missing_document), "filename": "gone.md",
                    "snippet": "丢失的片段",
                },
            ]
        )
        feedback_id = self.submit_ok(target.message_id)
        response = self.detail(feedback_id)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(len(data["citations"]), 2)
        self.assertEqual(
            set(data["citations"][0]),
            {"document_id", "filename", "chunk_index", "index_version", "snippet"},
        )
        self.assertNotIn("score", data["citations"][0])
        self.assertNotIn("evil", data["citations"][0])
        self.assertEqual(data["invalid_source_ids"], [str(missing_document)])
        self.assertFalse(data["result_stale"])
        self.assertEqual(data["question"], "合成问题")
        self.assertEqual(data["answer"], "合成回答")
        self.assertEqual(data["note"], "")

        self.assertEqual(self.detail(uuid.uuid4()).status_code, 404)
        self.assertEqual(
            self.detail(feedback_id, headers=self.owner_headers).status_code, 403
        )

    def test_detail_exposes_conversation_mode(self):
        rag_id = self.submit_ok(self.add_answer().message_id)
        self.assertEqual(self.detail(rag_id).json()["data"]["mode"], "rag")
        chat_id = self.submit_ok(self.add_answer(mode="chat", kb_id=None).message_id)
        self.assertEqual(self.detail(chat_id).json()["data"]["mode"], "chat")

    def test_project_citations_and_sources_use_snapshot_file_versions(self):
        kb_id, snapshot_id, file_version_id = self.project_binding(
            revoked=False, public=True, owner_id=self.owner_id,
            relative_path="src/main.py",
        )
        target = self.add_answer(
            kb_id=kb_id,
            project_snapshot_id=snapshot_id,
            citations=[{
                "document_id": str(file_version_id),
                "filename": "src/main.py",
                "chunk_index": 0,
                "index_version": 0,
            }],
        )
        feedback_id = self.submit_ok(target.message_id)
        detail = self.detail(feedback_id).json()["data"]
        self.assertEqual(detail["mode"], "rag")
        # 项目引用的 document_id 是 file_version_id，必须按快照映射判定有效。
        self.assertEqual(detail["invalid_source_ids"], [])
        reviewed = self.review(
            feedback_id,
            expected_sources=[{
                "document_id": str(file_version_id), "filename": "src/main.py",
            }],
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        exported = self.export(feedback_id)
        self.assertEqual(exported.status_code, 200, exported.text)
        case = json.loads(exported.json()["data"]["jsonl"].splitlines()[0])
        validate_cases([case])
        self.assertEqual(
            case["expected_sources"],
            [{"document_id": str(file_version_id), "filename": "src/main.py"}],
        )
        self.assertEqual(case["kb_id"], str(kb_id))

        # 只有 filename 时按 ProjectFileVersion.relative_path 在快照内解析。
        second = self.add_answer(
            kb_id=kb_id, project_snapshot_id=snapshot_id, citations=[]
        )
        second_id = self.submit_ok(second.message_id)
        reviewed = self.review(
            second_id, expected_sources=[{"filename": "src/main.py"}]
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        exported = self.export(second_id)
        self.assertEqual(exported.status_code, 200, exported.text)
        second_case = json.loads(exported.json()["data"]["jsonl"].splitlines()[0])
        validate_cases([second_case])
        self.assertEqual(
            second_case["expected_sources"], [{"filename": "src/main.py"}]
        )

    def test_project_conversation_without_snapshot_is_unavailable(self):
        with SessionLocal() as db:
            kb = KnowledgeBase(
                name="proj-empty-" + uuid.uuid4().hex,
                collection_name="proj_" + uuid.uuid4().hex,
                owner_id=self.owner_id, is_public=True, kind="project",
            )
            db.add(kb)
            db.commit()
            kb_id = kb.id
        target = self.add_answer(kb_id=kb_id, project_snapshot_id=None)
        submitted = self.submit(target.message_id)
        self.assertEqual(submitted.status_code, 409, submitted.text)
        self.assertIn("项目快照已不可用", submitted.json()["message"])

        feedback_id = self.insert_verified_feedback(target)
        detail = self.detail(feedback_id)
        self.assertEqual(detail.status_code, 409, detail.text)
        self.assertIn("项目快照已不可用", detail.json()["message"])
        exported = self.export(feedback_id)
        self.assertEqual(exported.status_code, 409, exported.text)
        self.assertIn("项目快照已不可用", exported.json()["message"])

    def test_rag_conversation_without_kb_is_not_found(self):
        target = self.add_answer(mode="rag", kb_id=None)
        submitted = self.submit(target.message_id)
        self.assertEqual(submitted.status_code, 404, submitted.text)
        self.assertIn("知识库不存在", submitted.json()["message"])

        feedback_id = self.insert_verified_feedback(target)
        detail = self.detail(feedback_id)
        self.assertEqual(detail.status_code, 404, detail.text)
        self.assertIn("知识库不存在", detail.json()["message"])
        exported = self.export(feedback_id)
        self.assertEqual(exported.status_code, 404, exported.text)
        self.assertIn("知识库不存在", exported.json()["message"])

    def test_fingerprint_staleness_blocks_detail_review_and_export(self):
        target = self.add_answer()
        feedback_id = self.submit_ok(target.message_id)
        self.assertFalse(self.detail(feedback_id).json()["data"]["result_stale"])
        with SessionLocal() as db:
            db.get(Message, target.message_id).content = "被篡改后的回答"
            db.commit()
        self.assertTrue(self.detail(feedback_id).json()["data"]["result_stale"])
        self.assertEqual(self.review(feedback_id).status_code, 409)

        verified_target = self.add_answer()
        verified_id = self.submit_ok(verified_target.message_id)
        self.assertEqual(self.review(verified_id).status_code, 200)
        with SessionLocal() as db:
            db.get(Message, verified_target.message_id).content = "导出前被篡改"
            db.commit()
        self.assertTrue(self.detail(verified_id).json()["data"]["result_stale"])
        response = self.export(verified_id)
        self.assertEqual(response.status_code, 409, response.text)

    # ---------------------------------------------------------------- 导出

    def test_export_rejects_unverified_and_empty_batch(self):
        empty = self.export()
        self.assertEqual(empty.status_code, 400, empty.text)
        self.assertIn("没有可导出的已核实反馈", empty.json()["message"])
        pending = self.submit_ok(self.add_answer().message_id)
        response = self.export(pending)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.export(uuid.uuid4()).status_code, 404)
        self.assertEqual(self.export(headers=self.owner_headers).status_code, 403)

    def test_verified_export_jsonl_is_valid_and_dataset_identity_changes(self):
        before = _templates_digest()
        target = self.add_answer(
            question="导出问题一",
            answer="导出回答一",
            citations=[{
                "document_id": str(self.doc_id), "filename": "source.md",
                "chunk_index": 0, "index_version": 0, "snippet": "资料片段",
            }],
        )
        feedback_id = self.submit_ok(target.message_id, note="备注")
        reviewed = self.review(
            feedback_id, required_phrases=["合规", "合规"], forbidden_phrases=["违法"]
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        response = self.export(feedback_id)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertRegex(data["filename"], r"^answer-feedback-\d{8}T\d{6}Z\.jsonl$")
        self.assertEqual(data["case_count"], 1)
        self.assertTrue(data["jsonl"].endswith("\n"))
        lines = data["jsonl"].splitlines()
        self.assertEqual(len(lines), 1)
        case = json.loads(lines[0])
        validate_cases([case])
        self.assertEqual(review_state(case), "complete")
        self.assertIs(case["reviewed"], True)
        self.assertEqual(case["id"], f"feedback-{feedback_id}")
        self.assertEqual(case["turns"], ["导出问题一"])
        self.assertEqual(case["question"], "导出问题一")
        self.assertEqual(case["mode"], "rag")
        self.assertEqual(case["expected_outcome"], "answer")
        self.assertEqual(case["expected_sources"], [{"document_id": str(self.doc_id)}])
        self.assertEqual(case["required_phrases"], ["合规"])
        self.assertEqual(case["forbidden_phrases"], ["违法"])
        self.assertEqual(case["unknowns"], [])
        self.assertEqual(case["actor"], "member")
        self.assertEqual(case["verified_by"], "fb_admin")
        self.assertRegex(case["verified_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(case["kb_id"], str(self.kb_id))
        for forbidden in ("expected_http_status", "conversation_id", "document_ids"):
            self.assertNotIn(forbidden, case)
        self.assertEqual(dataset_identity([case])["sha256"], data["dataset_sha256"])
        # 导出不写服务器文件，也不改动仓库模板字节。
        self.assertEqual(_templates_digest(), before)

        second_target = self.add_answer(question="导出问题二", answer="导出回答二")
        second_id = self.submit_ok(second_target.message_id)
        self.assertEqual(self.review(second_id).status_code, 200)
        batch = self.export()
        self.assertEqual(batch.status_code, 200, batch.text)
        batch_data = batch.json()["data"]
        self.assertEqual(batch_data["case_count"], 2)
        self.assertNotEqual(batch_data["dataset_sha256"], data["dataset_sha256"])
        ids = {json.loads(line)["id"] for line in batch_data["jsonl"].splitlines()}
        self.assertEqual(ids, {f"feedback-{feedback_id}", f"feedback-{second_id}"})
        self.assertEqual(_templates_digest(), before)

    def test_multi_turn_export_keeps_order_and_caps_at_ten(self):
        multi = self.create_multi_turn(
            ["问题一", "问题二", "问题三"], insert_order=[2, 1, 3]
        )
        feedback_id = self.submit_ok(multi.message_id)
        self.assertEqual(self.review(feedback_id).status_code, 200)
        response = self.export(feedback_id)
        self.assertEqual(response.status_code, 200, response.text)
        case = json.loads(response.json()["data"]["jsonl"].splitlines()[0])
        # 尽管插入顺序是 2,1,3，导出仍按 turn_seq 稳定排序，当前问题在最后。
        self.assertEqual(case["turns"], ["问题一", "问题二", "问题三"])
        self.assertEqual(case["question"], "问题三")

        eleven = self.create_multi_turn([f"长问题{i}" for i in range(1, 12)])
        feedback_id = self.submit_ok(eleven.message_id)
        self.assertEqual(self.review(feedback_id).status_code, 200)
        response = self.export(feedback_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("多轮上下文超过导出上限，请人工整理", response.json()["message"])

    def test_invalid_sources_and_project_binding_are_propagated(self):
        feedback_id = self.submit_ok(self.add_answer().message_id)
        self.assertEqual(
            self.review(
                feedback_id,
                expected_sources=[{"document_id": str(uuid.uuid4())}],
            ).status_code,
            200,
        )
        response = self.export(feedback_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("期望来源已失效或无权访问", response.json()["message"])

        bogus = self.add_answer(project_snapshot_id=uuid.uuid4())
        self.assertEqual(self.submit(bogus.message_id).status_code, 409)

        revoked_kb_id, revoked_snapshot_id, _ = self.project_binding(
            revoked=True, public=True, owner_id=self.owner_id
        )
        revoked = self.add_answer(
            kb_id=revoked_kb_id, project_snapshot_id=revoked_snapshot_id
        )
        self.assertEqual(self.submit(revoked.message_id).status_code, 410)

        private_kb_id, private_snapshot_id, _ = self.project_binding(
            revoked=False, public=False, owner_id=self.admin_id
        )
        denied = self.add_answer(
            kb_id=self.kb_id, project_snapshot_id=private_snapshot_id
        )
        self.assertEqual(self.submit(denied.message_id).status_code, 404)

    def test_export_rejects_filename_source_when_document_not_retrievable(self):
        target = self.add_answer()
        feedback_id = self.submit_ok(target.message_id)
        self.assertEqual(
            self.review(feedback_id, expected_sources=[{"filename": "source.md"}]).status_code,
            200,
        )
        with SessionLocal() as db:
            document = db.get(Document, self.doc_id)
            document.enabled = False
            db.commit()
        response = self.export(feedback_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("期望来源已失效或无权访问", response.json()["message"])

    # ---------------------------------------------------------------- 审计

    def test_detail_and_export_audits_omit_body(self):
        target = self.add_answer(
            question="审计问题ABC",
            answer="审计回答DEF",
            citations=[{"document_id": str(self.doc_id), "snippet": "审计片段GHI"}],
        )
        feedback_id = self.submit_ok(target.message_id, note="审计备注JKL")
        self.assertEqual(self.detail(feedback_id).status_code, 200)
        self.assertEqual(self.review(feedback_id).status_code, 200)
        self.assertEqual(self.export(feedback_id).status_code, 200)
        with SessionLocal() as db:
            rows = db.scalars(
                select(AuditLog).where(
                    AuditLog.action.in_(
                        ["feedback_view_detail", "feedback_review", "feedback_export"]
                    )
                )
            ).all()
            self.assertEqual(len(rows), 3)
            logs = {row.action: row for row in rows}
            self.assertEqual(
                set(logs["feedback_view_detail"].detail), {"conversation_id", "message_id"}
            )
            self.assertEqual(set(logs["feedback_review"].detail), {"decision", "revision"})
            self.assertEqual(
                set(logs["feedback_export"].detail),
                {"case_count", "dataset_sha256", "feedback_count"},
            )
            serialized = json.dumps(
                {action: row.detail for action, row in logs.items()}, ensure_ascii=False
            )
        for secret in ("审计问题ABC", "审计回答DEF", "审计片段GHI", "审计备注JKL"):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("审计问题ABC", json.dumps(
            {action: row.target for action, row in logs.items()}, ensure_ascii=False
        ))
