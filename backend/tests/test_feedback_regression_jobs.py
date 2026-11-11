"""反馈回归在线作业（R1b）：真实 PostgreSQL/API/租约/幂等/比较回归。

覆盖：单条与批量提交的幂等/路由绑定/原子失败零写入、真实并发同键、单活动竞争、
取消/租约过期/重试 fencing、owner 撤权与原 admin token 失效、上传固定清单与项目
固定快照、旧 manual 兼容与 online 拒绝人工导入、真实幂等迁移、反馈级联与用量保留、
历史成功作业比较/人工核验、未知价格与 UsageContext 兼容。

不调用任何模型：worker 契约用受控结果载荷模拟“模型等待后返回”。未设置
``TEST_DATABASE_URL`` 时整体跳过，不连接任何数据库。
"""
from __future__ import annotations

import json
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests import _memory_project_test_gate as gate
from tests.test_answer_feedback import _Helpers

TEST_URL = gate.TEST_DATABASE_URL

TABLES = (
    "users, knowledge_bases, documents, document_chunks, upload_sessions, "
    "audit_logs, conversations, chat_turns, messages, model_usage, "
    "usage_tracking_state, model_prices, answer_feedbacks, feedback_regressions, "
    "feedback_regression_runs, feedback_regression_batches, "
    "feedback_regression_jobs, feedback_regression_job_bodies, "
    "project_sources, project_snapshots, project_file_versions, "
    "project_snapshot_files, project_chunks"
)

if TEST_URL:
    from sqlalchemy import event, func, select, text
    from sqlalchemy.exc import IntegrityError

    from app.db.init_db import init_db
    from app.models import (
        AnswerFeedback,
        ChatTurn,
        Conversation,
        Document,
        DocumentChunk,
        FeedbackRegression,
        FeedbackRegressionBatch,
        FeedbackRegressionJob,
        FeedbackRegressionJobBody,
        FeedbackRegressionRun,
        KnowledgeBase,
        Message,
        ModelPrice,
        ModelUsage,
        ProjectChunk,
        ProjectFileVersion,
        ProjectSnapshot,
        ProjectSource,
        User,
    )
    from app.services import answer_sources
    from app.services import feedback_regression_jobs as jobs
    from app.services import usage_tracking
    from app.services.evaluation import build_identity, grade_case, make_report
    from app.services.usage_tracking import ModelCall, UsageContext, usage_scope
    from tests.test_answer_feedback import SessionLocal, engine


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行反馈回归在线作业数据库测试")
class FeedbackRegressionJobTests(_Helpers, unittest.TestCase):
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
            owner = User(username="job_owner", role="user", is_active=True, password_hash="x")
            other = User(username="job_other", role="user", is_active=True, password_hash="x")
            admin = User(username="job_admin", role="admin", is_active=True, password_hash="x")
            second_admin = User(
                username="job_admin2", role="admin", is_active=True, password_hash="x"
            )
            db.add_all([owner, other, admin, second_admin])
            db.flush()
            kb = KnowledgeBase(
                name="job_kb_" + uuid.uuid4().hex,
                collection_name="job_" + uuid.uuid4().hex,
                owner_id=owner.id,
                is_public=True,
                kind="upload",
            )
            db.add(kb)
            db.flush()
            doc = Document(
                kb_id=kb.id,
                filename="source.md",
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
            db.add(doc)
            db.flush()
            db.add(
                DocumentChunk(
                    document_id=doc.id,
                    chunk_index=0,
                    content="在线回归资料分块",
                    source_metadata={"filename": "source.md"},
                    search_text="在线回归资料分块",
                    embedded=True,
                )
            )
            db.commit()
            self.owner_id = owner.id
            self.other_id = other.id
            self.admin_id = admin.id
            self.second_admin_id = second_admin.id
            self.kb_id = kb.id
            self.doc_id = doc.id
        self.owner_headers = self.headers(self.owner_id, "user")
        self.other_headers = self.headers(self.other_id, "user")
        self.admin_headers = self.headers(self.admin_id, "admin")
        self.second_admin_headers = self.headers(self.second_admin_id, "admin")

    # ------------------------------------------------------------ fixtures

    def complete_project_binding(self, *, relative_path="src/main.py", chunks=1):
        kb_id, snapshot_id, file_version_id = self.project_binding(
            revoked=False,
            public=True,
            owner_id=self.owner_id,
            relative_path=relative_path,
        )
        with SessionLocal() as db:
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

    def verified_feedback(self, *, question="在线回归问题", answer="原始回归回答"):
        target = self.add_answer(question=question, answer=answer)
        # 本模块自有 fixture：补真实 v1 来源快照（共享 helpers 保持冻结）。
        if target.turn_id is not None:
            with SessionLocal() as db:
                turn = db.get(ChatTurn, target.turn_id)
                turn.trace = answer_sources.build_source_snapshot([], [], mode="rag")
                db.commit()
        feedback_id = self.submit_ok(target.message_id)
        response = self.review(feedback_id)
        self.assertEqual(response.status_code, 200, response.text)
        return feedback_id, target

    def enroll(self, feedback_id, *, revision=2, cause="retrieval", cause_note="检索未召回"):
        return self.client.post(
            f"/api/v1/feedback/{feedback_id}/regression",
            headers=self.admin_headers,
            json={
                "expected_feedback_revision": revision,
                "cause": cause,
                "cause_note": cause_note,
            },
        )

    def enroll_ok(self, feedback_id, **kwargs):
        response = self.enroll(feedback_id, **kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def create_job(self, feedback_id, *, revision, request_id=None, headers=None):
        return self.client.post(
            "/api/v1/feedback/regression/jobs",
            headers=headers or self.admin_headers,
            json={
                "feedback_id": str(feedback_id),
                "expected_revision": revision,
                "request_id": str(request_id or uuid.uuid4()),
            },
        )

    def create_job_ok(self, feedback_id, *, revision, request_id=None):
        response = self.create_job(
            feedback_id, revision=revision, request_id=request_id
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]

    def create_batch(self, items, *, request_id=None, headers=None):
        return self.client.post(
            "/api/v1/feedback/regression/batches",
            headers=headers or self.admin_headers,
            json={
                "request_id": str(request_id or uuid.uuid4()),
                "items": [
                    {
                        "feedback_id": str(feedback_id),
                        "expected_revision": revision,
                    }
                    for feedback_id, revision in items
                ],
            },
        )

    def get_job(self, job_id):
        return self.client.get(
            f"/api/v1/feedback/regression/jobs/{job_id}", headers=self.admin_headers
        )

    def lookup_request(self, request_id, *, headers=None):
        return self.client.get(
            f"/api/v1/feedback/regression/requests/{request_id}",
            headers=headers or self.admin_headers,
        )

    def get_batch(self, batch_id):
        return self.client.get(
            f"/api/v1/feedback/regression/batches/{batch_id}",
            headers=self.admin_headers,
        )

    def cancel_job(self, job_id, revision, *, headers=None):
        return self.client.post(
            f"/api/v1/feedback/regression/jobs/{job_id}/cancel",
            headers=headers or self.admin_headers,
            json={"expected_revision": revision},
        )

    def cancel_batch(self, batch_id, revision, *, headers=None):
        return self.client.post(
            f"/api/v1/feedback/regression/batches/{batch_id}/cancel",
            headers=headers or self.admin_headers,
            json={"expected_revision": revision},
        )

    def prepare(self, feedback_id, *, revision, mode="offline", request_id=None):
        return self.client.post(
            f"/api/v1/feedback/{feedback_id}/regression/runs",
            headers=self.admin_headers,
            json={
                "expected_revision": revision,
                "requested_mode": mode,
                "prepare_request_id": str(request_id or uuid.uuid4()),
            },
        )

    def import_report(self, feedback_id, run_id, *, revision, body):
        if isinstance(body, dict):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return self.client.post(
            f"/api/v1/feedback/{feedback_id}/regression/runs/{run_id}/import",
            headers=self.admin_headers,
            params={"expected_revision": revision},
            content=body,
        )

    def export_run(self, feedback_id, run_id):
        return self.client.get(
            f"/api/v1/feedback/{feedback_id}/regression/runs/{run_id}/export",
            headers=self.admin_headers,
        )

    def regression_detail(self, feedback_id):
        return self.client.get(
            f"/api/v1/feedback/{feedback_id}/regression",
            headers=self.admin_headers,
        ).json()["data"]

    def citation(self, *, document_id=None, index_version=0):
        return {
            "document_id": str(document_id or self.doc_id),
            "filename": "source.md",
            "index_version": index_version,
            "chunk_index": 0,
            "chunk_start": 0,
            "chunk_end": 0,
            "snippet": "在线回归资料分块",
        }

    def make_claimable(self, job_id):
        with SessionLocal() as db:
            row = db.get(FeedbackRegressionJob, uuid.UUID(str(job_id)))
            row.available_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
            db.commit()

    def claim(self, expected_job_id=None):
        with SessionLocal() as db:
            claim = jobs.claim(db)
        self.assertIsNotNone(claim)
        if expected_job_id is not None:
            self.assertEqual(claim.job_id, uuid.UUID(str(expected_job_id)))
        return claim

    def load(self, claim):
        with SessionLocal() as db:
            return jobs.load_execution(
                db, job_id=claim.job_id, lease_id=claim.lease_id
            )

    def complete(self, claim, *, answer="在线回归回答 [1]", citations=None,
                 supplied=None, history=None, trace=None):
        citations = [self.citation()] if citations is None else citations
        supplied = citations if supplied is None else supplied
        with SessionLocal() as db:
            return jobs.complete(
                db,
                job_id=claim.job_id,
                lease_id=claim.lease_id,
                answer=answer,
                citations=citations,
                supplied_sources=supplied,
                history_sources=history,
                trace=trace or {},
            )

    def finish_job(self, feedback_id, *, revision):
        job = self.create_job_ok(feedback_id, revision=revision)
        claim = self.claim(job["job_id"])
        self.load(claim)
        completion = self.complete(claim)
        return job, claim, completion

    def job_row(self, job_id):
        with SessionLocal() as db:
            return db.get(FeedbackRegressionJob, uuid.UUID(str(job_id)))

    def body_row(self, job_id):
        with SessionLocal() as db:
            return db.get(FeedbackRegressionJobBody, uuid.UUID(str(job_id)))

    def counts(self):
        with SessionLocal() as db:
            return {
                "batches": db.scalar(
                    select(func.count()).select_from(FeedbackRegressionBatch)
                ),
                "jobs": db.scalar(
                    select(func.count()).select_from(FeedbackRegressionJob)
                ),
                "runs": db.scalar(
                    select(func.count()).select_from(FeedbackRegressionRun)
                ),
                "bodies": db.scalar(
                    select(func.count()).select_from(FeedbackRegressionJobBody)
                ),
            }

    # ------------------------------------------------------------ submit/lookup

    def test_single_submit_idempotency_route_binding_and_recovery_lookup(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        request_id = uuid.uuid4()
        first = self.create_job_ok(
            feedback_id, revision=1, request_id=request_id
        )
        self.assertEqual(first["status"], "queued")
        self.assertEqual(first["execution_kind"], "online")
        self.assertFalse(first["can_import"])
        self.assertEqual(first["run_attempt_no"], 1)
        self.assertEqual(first["review"]["status"], "unknown")

        replay = self.create_job_ok(
            feedback_id, revision=1, request_id=request_id
        )
        self.assertEqual(replay["job_id"], first["job_id"])
        self.assertEqual(self.counts(), {"batches": 0, "jobs": 1, "runs": 1, "bodies": 0})

        conflict = self.create_job(
            feedback_id, revision=2, request_id=request_id
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("request_id_conflict", conflict.json()["message"])

        route_conflict = self.create_batch(
            [(feedback_id, 1)], request_id=request_id
        )
        self.assertEqual(route_conflict.status_code, 409, route_conflict.text)

        recovered = self.lookup_request(request_id)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        data = recovered.json()["data"]
        self.assertEqual(data["kind"], "job")
        self.assertEqual(data["job"]["job_id"], first["job_id"])
        self.assertIsNone(data["batch"])
        self.assertNotIn("answer", data["job"])

        # 另一个管理员不能按 request_id 枚举别人的提交。
        other = self.lookup_request(request_id, headers=self.second_admin_headers)
        self.assertEqual(other.status_code, 404, other.text)

        listed = self.client.get(
            "/api/v1/feedback/regression/jobs",
            headers=self.admin_headers,
            params={"request_id": str(request_id)},
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["data"]["total"], 1)

        # 非管理员不能提交或查看。
        self.assertEqual(
            self.create_job(feedback_id, revision=2, headers=self.owner_headers).status_code,
            403,
        )
        self.assertEqual(
            self.get_job(first["job_id"]).status_code, 200
        )

    def test_batch_is_atomic_and_concurrent_same_key_creates_one_batch(self):
        first_id, _ = self.verified_feedback()
        second_id, _ = self.verified_feedback(question="第二个在线问题")
        self.enroll_ok(first_id)
        self.enroll_ok(second_id)

        # 一条 revision 不匹配：整批零写入（不落 batch/job/run）。
        invalid = self.create_batch([(first_id, 1), (second_id, 2)])
        self.assertEqual(invalid.status_code, 409, invalid.text)
        self.assertEqual(self.counts(), {"batches": 0, "jobs": 0, "runs": 0, "bodies": 0})

        # 一条未纳入：同样零写入。
        third_target = self.add_answer(question="未纳入问题")
        third_id = self.submit_ok(third_target.message_id)
        self.assertEqual(self.review(third_id).status_code, 200)
        missing = self.create_batch([(first_id, 1), (third_id, 1)])
        self.assertEqual(missing.status_code, 404, missing.text)
        self.assertEqual(self.counts(), {"batches": 0, "jobs": 0, "runs": 0, "bodies": 0})

        request_id = uuid.uuid4()
        barrier = threading.Barrier(2)

        def submit():
            barrier.wait()
            return self.create_batch(
                [(first_id, 1), (second_id, 1)], request_id=request_id
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: submit(), range(2)))
        self.assertTrue(
            all(response.status_code == 200 for response in responses),
            [response.text for response in responses],
        )
        batch_ids = {response.json()["data"]["batch_id"] for response in responses}
        self.assertEqual(len(batch_ids), 1)
        counts = self.counts()
        self.assertEqual(counts, {"batches": 1, "jobs": 2, "runs": 2, "bodies": 0})

        batch_id = next(iter(batch_ids))
        detail = self.get_batch(batch_id).json()["data"]
        self.assertEqual(detail["counts"]["total"], 2)
        self.assertEqual(detail["counts"]["queued"], 2)
        self.assertEqual(detail["revision"], 1)
        self.assertEqual(
            {str(job["feedback_id"]) for job in detail["jobs"]},
            {str(first_id), str(second_id)},
        )

        different = self.create_batch([(first_id, 1)], request_id=request_id)
        self.assertEqual(different.status_code, 409, different.text)

        recovered = self.lookup_request(request_id).json()["data"]
        self.assertEqual(recovered["kind"], "batch")
        self.assertEqual(recovered["batch"]["batch_id"], batch_id)

        # 批次取消是显式批操作：CAS 推进 batch revision，成员作业同步取消。
        cancelled = self.cancel_batch(batch_id, 1)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        data = cancelled.json()["data"]
        self.assertEqual(data["revision"], 2)
        self.assertEqual(data["counts"]["cancelled"], 2)
        self.assertEqual(self.cancel_batch(batch_id, 1).status_code, 409)

    def test_single_active_job_and_db_partial_unique_constraint(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        first = self.create_job_ok(feedback_id, revision=1)

        # 服务层：同回归已有活动作业时，第二份新提交被明确拒绝。
        active = self.create_job(feedback_id, revision=2)
        self.assertEqual(active.status_code, 409, active.text)
        self.assertIn("regression_job_active", active.json()["message"])

        # 数据库层：绕过服务直接插入第二个活动作业必须撞部分唯一索引。
        with SessionLocal() as db:
            regression = db.scalar(
                select(FeedbackRegression).where(
                    FeedbackRegression.feedback_id == uuid.UUID(str(feedback_id))
                )
            )
            run = FeedbackRegressionRun(
                regression_id=regression.id,
                attempt_no=3,
                prepare_request_id=uuid.uuid4(),
                protocol_version=1,
                requested_mode="live",
                execution_kind="online",
                feedback_revision=2,
                feedback_result_sha256="a" * 64,
                case_sha256="b" * 64,
                dataset_sha256="c" * 64,
                binding_sha256="d" * 64,
                prepared_by=self.admin_id,
                prepared_by_name="job_admin",
            )
            db.add(run)
            db.flush()
            db.add(
                FeedbackRegressionJob(
                    regression_id=regression.id,
                    feedback_id=uuid.UUID(str(feedback_id)),
                    run_id=run.id,
                    run_attempt_no=3,
                    admin_id=self.admin_id,
                    admin_name="job_admin",
                    admin_token_version=0,
                    context_summary={},
                    root_sha256="e" * 64,
                    case_sha256="b" * 64,
                    dataset_sha256="c" * 64,
                    binding_sha256="d" * 64,
                )
            )
            with self.assertRaises(IntegrityError):
                db.commit()
            db.rollback()

        # 取消后活动槽位释放，新提交创建下一轮 attempt。
        cancelled = self.cancel_job(first["job_id"], 1)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        second = self.create_job_ok(feedback_id, revision=2)
        self.assertEqual(second["run_attempt_no"], 2)
        self.assertNotEqual(second["job_id"], first["job_id"])


    def test_request_id_scoped_per_admin_and_cross_route_same_admin(self):
        first_id, _ = self.verified_feedback()
        second_id, _ = self.verified_feedback(question="管理员 B 的问题")
        self.enroll_ok(first_id)
        self.enroll_ok(second_id)
        request_id = uuid.uuid4()
        first = self.create_job(first_id, revision=1, request_id=request_id)
        self.assertEqual(first.status_code, 200, first.text)
        # 不同管理员使用同一客户端 UUID 是独立请求，不得互相 409。
        other = self.create_job(
            second_id, revision=1, request_id=request_id,
            headers=self.second_admin_headers,
        )
        self.assertEqual(other.status_code, 200, other.text)
        self.assertNotEqual(other.json()["data"]["job_id"], first.json()["data"]["job_id"])

        # 同一管理员跨路由使用同一 UUID 仍然冲突。
        batch_same_admin = self.create_batch([(second_id, 1)], request_id=request_id)
        self.assertEqual(batch_same_admin.status_code, 409, batch_same_admin.text)
        self.assertIn("request_id_conflict", batch_same_admin.json()["message"])

        # 两个管理员各自按原 UUID 精确读回自己的提交，互不串扰。
        mine = self.lookup_request(request_id).json()["data"]
        theirs = self.lookup_request(
            request_id, headers=self.second_admin_headers
        ).json()["data"]
        self.assertEqual(mine["job"]["job_id"], first.json()["data"]["job_id"])
        self.assertEqual(theirs["job"]["job_id"], other.json()["data"]["job_id"])

        # GET jobs 的 request_id 过滤同样按当前管理员隔离。
        mine_list = self.client.get(
            "/api/v1/feedback/regression/jobs",
            headers=self.admin_headers,
            params={"request_id": str(request_id)},
        ).json()["data"]
        theirs_list = self.client.get(
            "/api/v1/feedback/regression/jobs",
            headers=self.second_admin_headers,
            params={"request_id": str(request_id)},
        ).json()["data"]
        self.assertEqual(mine_list["total"], 1)
        self.assertEqual(theirs_list["total"], 1)
        self.assertNotEqual(
            mine_list["items"][0]["job_id"], theirs_list["items"][0]["job_id"]
        )

    def test_batch_request_id_is_scoped_per_admin(self):
        first_id, _ = self.verified_feedback()
        second_id, _ = self.verified_feedback(question="批量管理员 B 的问题")
        self.enroll_ok(first_id)
        self.enroll_ok(second_id)
        request_id = uuid.uuid4()
        first = self.create_batch([(first_id, 1)], request_id=request_id)
        self.assertEqual(first.status_code, 200, first.text)
        second = self.create_batch(
            [(second_id, 1)], request_id=request_id,
            headers=self.second_admin_headers,
        )
        self.assertEqual(second.status_code, 200, second.text)
        self.assertNotEqual(
            first.json()["data"]["batch_id"], second.json()["data"]["batch_id"]
        )
        self.assertEqual(
            self.lookup_request(request_id).json()["data"]["batch"]["batch_id"],
            first.json()["data"]["batch_id"],
        )
        self.assertEqual(
            self.lookup_request(
                request_id, headers=self.second_admin_headers
            ).json()["data"]["batch"]["batch_id"],
            second.json()["data"]["batch_id"],
        )

    def test_completion_rechecks_deadline_after_grading(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        self.load(claim)
        with SessionLocal() as db:
            row = db.get(FeedbackRegressionJob, claim.job_id)
            row.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=1)
            row.lease_expires_at = row.deadline_at
            deadline = row.deadline_at
            db.commit()

        real_grade = jobs.grade_case

        def delayed_grade(*args, **kwargs):
            result = real_grade(*args, **kwargs)
            time.sleep(1.2)
            return result

        with patch.object(jobs, "grade_case", side_effect=delayed_grade):
            with self.assertRaises(jobs.JobError) as raised:
                self.complete(claim)
        self.assertGreater(datetime.now(timezone.utc), deadline)
        self.assertEqual(raised.exception.reason, "lease_invalid")
        self.assertIsNone(self.body_row(job["job_id"]))
        stored = self.job_row(job["job_id"])
        self.assertNotEqual(stored.status, "succeeded")
        with SessionLocal() as db:
            recovered = jobs.recover(db)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].reason, "deadline_exceeded")
        self.assertEqual(self.job_row(job["job_id"]).status, "failed")

    def test_persisted_body_is_the_only_authoritative_scored_result(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        citation = self.citation()
        completion = self.complete(
            claim,
            citations=[citation],
            supplied=[citation],
            trace={
                "supplied_sources": [],
                "candidate_sources": [citation],
            },
        )
        body = self.body_row(job["job_id"])
        self.assertEqual(len(body.trace["supplied_sources"]), 1)
        self.assertEqual(len(body.trace["candidate_sources"]), 1)
        regraded = grade_case(
            loaded.execution.case,
            {
                "answer": body.answer,
                "citations": body.citations,
                "trace": body.trace,
                "http_status": 200,
            },
            include_content=False,
        )
        self.assertTrue(completion.passed)
        self.assertTrue(regraded["passed"], regraded["failures"])
        self.assertEqual(completion.result_sha256, regraded["result_sha256"])
        self.assertEqual(completion.machine_sha256, regraded["machine_sha256"])

        # 未核验身份的 candidate 不能被持久化，也不能影响评分。
        second = self.create_job_ok(feedback_id, revision=2)
        second_claim = self.claim(second["job_id"])
        self.load(second_claim)
        bogus = self.citation()
        bogus["document_id"] = str(uuid.uuid4())
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=second_claim.job_id,
                    lease_id=second_claim.lease_id,
                    answer="不应发布 [1]",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                    trace={"candidate_sources": [bogus]},
                )
        self.assertIn(raised.exception.reason, {"source_verification_failed", "result_invalid"})
        self.assertIsNone(self.body_row(second["job_id"]))

    def test_saved_citations_are_normalized_before_scoring_and_hashing(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        citation = self.citation()
        citation["snippet"] = "s" * 301
        completion = self.complete(claim, citations=[citation], supplied=[citation])
        body = self.body_row(job["job_id"])
        self.assertEqual(len(body.citations[0]["snippet"]), 300)
        regraded = grade_case(
            loaded.execution.case,
            {
                "answer": body.answer,
                "citations": body.citations,
                "trace": body.trace,
                "http_status": 200,
            },
            include_content=False,
        )
        self.assertEqual(completion.result_sha256, regraded["result_sha256"])
        self.assertEqual(completion.machine_sha256, regraded["machine_sha256"])
        comparison = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(comparison.status_code, 200, comparison.text)
        self.assertEqual(comparison.json()["data"]["new"]["result_sha256"], completion.result_sha256)

    def test_history_sources_are_persisted_revalidated_and_transmitted(self):
        """扁平 history_sources 必须进入 v1 trace、评分、比较与下一轮传递。"""
        feedback_id, _ = self.verified_feedback()
        with SessionLocal() as db:
            inherited_doc = Document(
                kb_id=self.kb_id,
                filename="inherited.md",
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
            db.add(inherited_doc)
            db.flush()
            inherited_id = inherited_doc.id
            db.add(
                DocumentChunk(
                    document_id=inherited_id,
                    chunk_index=0,
                    content="继承来源资料",
                    source_metadata={"filename": "inherited.md"},
                    search_text="继承来源资料",
                    embedded=True,
                )
            )
            db.commit()

        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        supplied = self.citation()
        inherited = self.citation(document_id=inherited_id)
        inherited["filename"] = "inherited.md"
        completion = self.complete(
            claim,
            citations=[supplied],
            supplied=[supplied],
            history=[inherited],
        )
        body = self.body_row(job["job_id"])
        self.assertIsNotNone(body)
        self.assertEqual(body.trace["history_sources"], [
            dict(answer_sources.parse_sources([inherited])[0])
        ])
        self.assertEqual(
            {item["document_id"] for item in body.trace["history_sources"]},
            {str(inherited_id)},
        )
        self.assertEqual(
            {item["document_id"] for item in jobs._canonical_actual_sources(
                "rag",
                body.trace["supplied_sources"],
                body.trace["history_sources"],
                body.citations,
            )},
            {str(self.doc_id), str(inherited_id)},
        )
        regraded = grade_case(
            loaded.execution.case,
            {
                "answer": body.answer,
                "citations": body.citations,
                "trace": body.trace,
                "http_status": 200,
            },
            include_content=False,
        )
        self.assertEqual(completion.result_sha256, regraded["result_sha256"])
        self.assertEqual(completion.machine_sha256, regraded["machine_sha256"])

        comparison = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(comparison.status_code, 200, comparison.text)
        self.assertEqual(
            comparison.json()["data"]["new"]["trace"]["history_sources"],
            body.trace["history_sources"],
        )

        # 第二轮把第一轮持久化的 history_sources 原样传回，不能在再次规范化时丢失。
        current_revision = self.regression_detail(feedback_id)["revision"]
        second = self.create_job_ok(feedback_id, revision=current_revision)
        second_claim = self.claim(second["job_id"])
        second_loaded = self.load(second_claim)
        second_completion = self.complete(
            second_claim,
            citations=[supplied],
            supplied=[supplied],
            history=body.trace["history_sources"],
        )
        second_body = self.body_row(second["job_id"])
        self.assertEqual(
            second_body.trace["history_sources"], body.trace["history_sources"]
        )
        self.assertEqual(
            second_completion.result_sha256,
            grade_case(
                second_loaded.execution.case,
                {
                    "answer": second_body.answer,
                    "citations": second_body.citations,
                    "trace": second_body.trace,
                    "http_status": 200,
                },
                include_content=False,
            )["result_sha256"],
        )

    def test_history_sources_fail_closed_before_publish(self):
        """畸形或冻结清单外的 history source 必须拒绝且不写 body。"""
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        first = self.create_job_ok(feedback_id, revision=1)
        first_claim = self.claim(first["job_id"])
        self.load(first_claim)
        malformed = self.citation()
        malformed["document_id"] = "not-a-uuid"
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=first_claim.job_id,
                    lease_id=first_claim.lease_id,
                    answer="不应发布 [1]",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                    history_sources=[malformed],
                )
        self.assertEqual(raised.exception.reason, "result_invalid")
        self.assertIsNone(self.body_row(first["job_id"]))
        self.assertNotEqual(self.job_row(first["job_id"]).status, "succeeded")
        with SessionLocal() as db:
            jobs.fail(
                db,
                job_id=first_claim.job_id,
                lease_id=first_claim.lease_id,
                error_code="model_error",
                retryable=False,
            )

        revision = self.regression_detail(feedback_id)["revision"]
        second = self.create_job_ok(feedback_id, revision=revision)
        with SessionLocal() as db:
            outside_doc = Document(
                kb_id=self.kb_id,
                filename="outside-manifest.md",
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
            db.add(outside_doc)
            db.flush()
            outside_id = outside_doc.id
            db.add(
                DocumentChunk(
                    document_id=outside_id,
                    chunk_index=0,
                    content="清单外来源资料",
                    source_metadata={"filename": "outside-manifest.md"},
                    search_text="清单外来源资料",
                    embedded=True,
                )
            )
            db.commit()
        second_claim = self.claim(second["job_id"])
        self.load(second_claim)
        outside = self.citation(document_id=outside_id)
        outside["filename"] = "outside-manifest.md"
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=second_claim.job_id,
                    lease_id=second_claim.lease_id,
                    answer="不应发布 [1]",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                    history_sources=[outside],
                )
        self.assertEqual(raised.exception.reason, "source_verification_failed")
        self.assertIsNone(self.body_row(second["job_id"]))
        self.assertNotEqual(self.job_row(second["job_id"]).status, "succeeded")

    def test_comparison_requires_currently_valid_old_answer_sources(self):
        old_citation = self.citation()
        target = self.add_answer(
            answer="SYNTHETIC_OLD_GOVERNED_ANSWER", citations=[old_citation]
        )
        with SessionLocal() as db:
            turn = db.get(ChatTurn, target.turn_id)
            turn.trace = answer_sources.build_source_snapshot(
                [old_citation], [], mode="rag"
            )
            corrected = Document(
                kb_id=self.kb_id,
                filename="corrected.md",
                file_ext=".md",
                file_size=1,
                stored_path="/tmp/" + uuid.uuid4().hex,
                status="ready",
                index_version=0,
                cancel_requested=False,
                parse_done=True,
                chunk_count=1,
                enabled=True,
            )
            db.add(corrected)
            db.flush()
            corrected_id = corrected.id
            db.add(
                DocumentChunk(
                    document_id=corrected_id,
                    chunk_index=0,
                    content="Synthetic corrected source",
                    search_text="Synthetic corrected source",
                    source_metadata={"filename": "corrected.md"},
                    embedded=True,
                )
            )
            db.commit()
        feedback_id = self.submit_ok(target.message_id)
        review = self.review(
            feedback_id, expected_sources=[{"document_id": str(corrected_id)}]
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.enroll_ok(feedback_id)

        # 旧错误来源被停用：纳入与新执行仍允许（纠错语义），但比较不得回显旧正文。
        with SessionLocal() as db:
            db.get(Document, self.doc_id).enabled = False
            db.commit()
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        self.load(claim)
        new_citation = self.citation(document_id=corrected_id)
        new_citation["filename"] = "corrected.md"
        completion = self.complete(
            claim, citations=[new_citation], supplied=[new_citation]
        )
        self.assertTrue(completion.passed)

        comparison = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(comparison.status_code, 409, comparison.text)
        self.assertNotIn("SYNTHETIC_OLD_GOVERNED_ANSWER", comparison.text)
        self.assertIsNone(comparison.json().get("data"))

        current = self.get_job(job["job_id"]).json()["data"]
        review_attempt = self.client.put(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/review",
            headers=self.admin_headers,
            json={
                "expected_revision": current["revision"],
                "run_id": str(current["run_id"]),
                "result_sha256": completion.result_sha256,
                "status": "verified",
                "notes": "不可比较结果不应通过",
            },
        )
        self.assertEqual(review_attempt.status_code, 409, review_attempt.text)
        self.assertEqual(
            self.get_job(job["job_id"]).json()["data"]["review"]["status"], "unknown"
        )

        # 旧来源恢复可用后，历史成功作业仍可比较。
        with SessionLocal() as db:
            db.get(Document, self.doc_id).enabled = True
            db.commit()
        again = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(
            again.json()["data"]["old"]["answer"], "SYNTHETIC_OLD_GOVERNED_ANSWER"
        )

    def test_chat_without_snapshot_keeps_no_trace_exemption(self):
        target = self.add_answer(mode="chat", answer="普通对话旧回答", citations=[])
        with SessionLocal() as db:
            db.get(ChatTurn, target.turn_id).trace = None
            db.commit()
        feedback_id = self.submit_ok(target.message_id)
        self.assertEqual(self.review(feedback_id, expected_sources=[]).status_code, 200)
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        self.load(claim)
        self.complete(claim, answer="普通对话新回答", citations=[], supplied=[])
        comparison = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(comparison.status_code, 200, comparison.text)
        self.assertEqual(comparison.json()["data"]["old"]["answer"], "普通对话旧回答")
        self.assertEqual(comparison.json()["data"]["old"]["citations"], [])

    # ------------------------------------------------------------ cancel/lease

    def test_cancel_invalidates_lease_and_late_result_cannot_publish(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        self.load(claim)

        heartbeat = None
        with SessionLocal() as db:
            self.assertTrue(
                jobs.heartbeat(db, job_id=claim.job_id, lease_id=claim.lease_id)
            )
        heartbeat = self.job_row(job["job_id"])
        self.assertEqual(heartbeat.revision, 2)  # heartbeat 不推进 UI revision

        cancelled = self.cancel_job(job["job_id"], 2)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["data"]["status"], "cancelled")

        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=claim.job_id,
                    lease_id=claim.lease_id,
                    answer="迟到回答",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                )
        self.assertEqual(raised.exception.reason, "lease_invalid")
        self.assertIsNone(self.body_row(job["job_id"]))
        self.assertEqual(self.job_row(job["job_id"]).status, "cancelled")

        with SessionLocal() as db:
            transition = jobs.fail(
                db,
                job_id=claim.job_id,
                lease_id=claim.lease_id,
                error_code="model_timeout",
                retryable=True,
            )
        self.assertFalse(transition.applied)
        self.assertEqual(transition.reason, "cancelled")

        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError):
                jobs.load_execution(
                    db, job_id=claim.job_id, lease_id=claim.lease_id
                )

    def test_expired_lease_recovery_fences_old_attempt_and_retries(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        first = self.claim(job["job_id"])
        self.load(first)

        # 模拟第一次执行超过租约但尚未提交。
        with SessionLocal() as db:
            row = db.get(FeedbackRegressionJob, first.job_id)
            row.lease_expires_at = datetime.now(timezone.utc)
            db.commit()
        with SessionLocal() as db:
            check = jobs.check_lease(
                db, job_id=first.job_id, lease_id=first.lease_id
            )
        self.assertFalse(check.valid)
        self.assertEqual(check.reason, "lease_expired")
        with SessionLocal() as db:
            self.assertFalse(
                jobs.heartbeat(db, job_id=first.job_id, lease_id=first.lease_id)
            )
            recovered = jobs.recover(db)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].status, "queued")
        self.assertEqual(recovered[0].reason, "lease_expired")
        self.make_claimable(job["job_id"])

        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=first.job_id,
                    lease_id=first.lease_id,
                    answer="旧租约迟到回答",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                )
        self.assertEqual(raised.exception.reason, "lease_invalid")
        self.assertIsNone(self.body_row(job["job_id"]))

        second = self.claim(job["job_id"])
        self.assertNotEqual(second.lease_id, first.lease_id)
        self.assertEqual(second.attempt_count, 2)
        self.load(second)
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError):
                jobs.complete(
                    db,
                    job_id=job["job_id"],
                    lease_id=first.lease_id,
                    answer="旧租约第二次迟到",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                )
        completion = self.complete(second)
        self.assertEqual(completion.status, "succeeded")
        self.assertTrue(completion.passed)
        stored = self.job_row(job["job_id"])
        self.assertEqual(stored.status, "succeeded")
        self.assertIsNotNone(self.body_row(job["job_id"]))

    def test_worker_fail_release_retry_limits_and_deadline(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        first = self.claim(job["job_id"])
        with SessionLocal() as db:
            transition = jobs.fail(
                db,
                job_id=first.job_id,
                lease_id=first.lease_id,
                error_code="model_timeout",
                retryable=True,
            )
        self.assertTrue(transition.applied)
        self.assertEqual(transition.status, "queued")
        self.assertEqual(transition.attempt_count, 1)
        self.make_claimable(job["job_id"])

        second = self.claim(job["job_id"])
        with SessionLocal() as db:
            released = jobs.release(
                db, job_id=second.job_id, lease_id=second.lease_id
            )
        self.assertTrue(released.applied)
        self.assertEqual(released.status, "queued")
        self.assertEqual(released.attempt_count, 2)
        self.make_claimable(job["job_id"])

        third = self.claim(job["job_id"])
        with SessionLocal() as db:
            failed = jobs.fail(
                db,
                job_id=third.job_id,
                lease_id=third.lease_id,
                error_code="stream_interrupted",
                retryable=True,
            )
        self.assertTrue(failed.applied)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.attempt_count, 3)
        self.assertEqual(self.job_row(job["job_id"]).error_code, "stream_interrupted")

        # 未知错误码收敛；非可恢复错误立即失败。
        second_feedback, _ = self.verified_feedback(question="第二个失败问题")
        self.enroll_ok(second_feedback)
        second_job = self.create_job_ok(second_feedback, revision=1)
        claim = self.claim(second_job["job_id"])
        with SessionLocal() as db:
            transition = jobs.fail(
                db,
                job_id=claim.job_id,
                lease_id=claim.lease_id,
                error_code="raw exception text with 正文",
                retryable=True,
            )
        self.assertEqual(transition.status, "failed")
        self.assertEqual(
            self.job_row(second_job["job_id"]).error_code, "execution_failed"
        )

    # ------------------------------------------------------------ permissions

    def test_owner_and_admin_permissions_are_rechecked_after_wait(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        self.load(claim)  # 模拟模型等待已完成，发布前复核。

        with SessionLocal() as db:
            db.get(User, self.owner_id).is_active = False
            db.commit()
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=claim.job_id,
                    lease_id=claim.lease_id,
                    answer="不应发布",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                )
        self.assertEqual(raised.exception.reason, "owner_inactive")
        self.assertIsNone(self.body_row(job["job_id"]))

        # 恢复 owner，改用第二个反馈：等待期间 admin 原 token 失效。
        with SessionLocal() as db:
            db.get(User, self.owner_id).is_active = True
            db.commit()
        second_id, _ = self.verified_feedback(question="token 失效问题")
        self.enroll_ok(second_id)
        second_job = self.create_job_ok(second_id, revision=1)
        second_claim = self.claim(second_job["job_id"])
        self.load(second_claim)
        with SessionLocal() as db:
            admin = db.get(User, self.admin_id)
            admin.token_version += 1
            db.commit()
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=second_claim.job_id,
                    lease_id=second_claim.lease_id,
                    answer="不应发布",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                )
        self.assertEqual(raised.exception.reason, "auth_invalid")
        self.assertEqual(raised.exception.status_code, 401)

        # owner ACL 撤销：知识库改为他人私有，原 owner 不再可读。
        with SessionLocal() as db:
            db.get(User, self.admin_id).token_version -= 1
            db.commit()
        third_id, _ = self.verified_feedback(question="ACL 撤销问题")
        self.enroll_ok(third_id)
        third_job = self.create_job_ok(third_id, revision=1)
        third_claim = self.claim(third_job["job_id"])
        self.load(third_claim)
        with SessionLocal() as db:
            kb = db.get(KnowledgeBase, self.kb_id)
            kb.owner_id = self.other_id
            kb.is_public = False
            db.commit()
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=third_claim.job_id,
                    lease_id=third_claim.lease_id,
                    answer="不应发布",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                )
        self.assertEqual(raised.exception.reason, "owner_access_revoked")
        for job_id in (job["job_id"], second_job["job_id"], third_job["job_id"]):
            self.assertIsNone(self.body_row(job_id))

    # ------------------------------------------------------------ fixed scope

    def test_upload_manifest_is_frozen_and_version_change_fails_closed(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        with SessionLocal() as db:
            db.add(
                Document(
                    kb_id=self.kb_id,
                    filename="new-after-enqueue.md",
                    file_ext=".md",
                    file_size=1,
                    stored_path="/tmp/" + uuid.uuid4().hex + ".md",
                    status="ready",
                    index_version=0,
                    cancel_requested=False,
                    parse_done=True,
                    chunk_count=0,
                    enabled=True,
                )
            )
            db.commit()
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        manifest = list(loaded.execution.upload_manifest or [])
        self.assertEqual(
            manifest, [{"document_id": str(self.doc_id), "index_version": 0}]
        )
        completion = self.complete(claim)
        self.assertTrue(completion.passed)

        # 新作业入队后原文档代次变化：加载执行上下文必须失败关闭。
        second = self.create_job_ok(feedback_id, revision=2)
        with SessionLocal() as db:
            db.get(Document, self.doc_id).index_version = 1
            db.commit()
        second_claim = self.claim(second["job_id"])
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.load_execution(
                    db,
                    job_id=second_claim.job_id,
                    lease_id=second_claim.lease_id,
                )
        self.assertEqual(raised.exception.reason, "version_mismatch")
        self.assertIsNone(self.body_row(second["job_id"]))

    def test_project_snapshot_and_config_identity_are_fixed(self):
        kb_id, snapshot_id, file_version_id = self.complete_project_binding(
            relative_path="src/main.py", chunks=1
        )
        target = self.add_answer(
            kb_id=kb_id, project_snapshot_id=snapshot_id
        )
        feedback_id = self.submit_ok(target.message_id)
        review = self.review(
            feedback_id,
            expected_sources=[
                {"document_id": str(file_version_id), "filename": "src/main.py"}
            ],
        )
        self.assertEqual(review.status_code, 200, review.text)
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)

        with SessionLocal() as db:
            snapshot = db.get(ProjectSnapshot, snapshot_id)
            source_id = snapshot.source_id
            snapshot.config_fingerprint = "f" * 64
            db.commit()

        claim = self.claim(job["job_id"])
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.load_execution(
                    db, job_id=claim.job_id, lease_id=claim.lease_id
                )
        self.assertEqual(raised.exception.reason, "project_snapshot_changed")
        self.assertIsNone(self.body_row(job["job_id"]))

        # 恢复冻结值后可正常执行；实际项目来源必须匹配原快照身份。
        with SessionLocal() as db:
            db.get(ProjectSnapshot, snapshot_id).config_fingerprint = "e" * 64
            db.commit()
        loaded = self.load(claim)
        self.assertEqual(
            loaded.execution.project["snapshot_id"], str(snapshot_id)
        )
        project_source = {
            "source_kind": "project",
            "document_id": str(file_version_id),
            "file_version_id": str(file_version_id),
            "snapshot_id": str(snapshot_id),
            "source_id": str(source_id),
            "relative_path": "src/main.py",
            "manifest_fingerprint": "d" * 64,
            "config_fingerprint": "e" * 64,
            "chunk_index": 0,
            "chunk_start": 0,
            "chunk_end": 0,
        }
        completion = self.complete(
            claim, citations=[project_source], supplied=[project_source]
        )
        self.assertTrue(completion.passed)

    # ------------------------------------------------------------ manual compat

    def test_old_manual_flow_still_works_and_online_run_cannot_import(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare(feedback_id, revision=1)
        self.assertEqual(prepared.status_code, 200, prepared.text)
        prepared_data = prepared.json()["data"]
        run = prepared_data["run"]
        self.assertEqual(run["execution_kind"], "manual")
        self.assertTrue(run["can_import"])
        exported = self.export_run(feedback_id, run["run_id"])
        self.assertEqual(exported.status_code, 200, exported.text)
        report = self.build_report(prepared_data)
        imported = self.import_report(
            feedback_id,
            run["run_id"],
            revision=prepared_data["regression_revision"],
            body=report,
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["data"]["run"]["state"], "imported")

        # 在线作业：旧导出/导入明确拒绝；prepare 在活动期间冲突。
        online = self.create_job_ok(feedback_id, revision=3)
        detail = self.regression_detail(feedback_id)
        self.assertEqual(detail["current_run"]["execution_kind"], "online")
        self.assertFalse(detail["current_run"]["can_import"])
        self.assertEqual(detail["current_run"]["state"], "online")
        self.assertEqual(detail["current_run"]["job_status"], "queued")
        self.assertEqual(detail["current_run"]["job_id"], online["job_id"])
        self.assertEqual(
            self.export_run(feedback_id, online["run_id"]).status_code, 409
        )
        self.assertEqual(
            self.import_report(
                feedback_id,
                online["run_id"],
                revision=detail["revision"],
                body={"anything": True},
            ).status_code,
            409,
        )
        blocked = self.prepare(feedback_id, revision=detail["revision"])
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("regression_job_active", blocked.json()["message"])

        # 取消后旧 manual 准备恢复可用，并成为新的当前轮。
        self.assertEqual(
            self.cancel_job(online["job_id"], online["revision"]).status_code, 200
        )
        after = self.regression_detail(feedback_id)
        followup = self.prepare(feedback_id, revision=after["revision"])
        self.assertEqual(followup.status_code, 200, followup.text)
        self.assertEqual(followup.json()["data"]["run"]["execution_kind"], "manual")
        history = self.client.get(
            f"/api/v1/feedback/{feedback_id}/regression/runs",
            headers=self.admin_headers,
        ).json()["data"]["items"]
        by_attempt = {item["attempt_no"]: item for item in history}
        self.assertEqual(by_attempt[1]["state"], "imported")
        self.assertEqual(by_attempt[2]["state"], "online")
        self.assertEqual(by_attempt[3]["state"], "awaiting_report")

    def build_report(self, prepared):
        from app.services.evaluation import make_report

        case = json.loads(prepared["jsonl"].splitlines()[0])
        citations = []
        for source in case.get("expected_sources", []):
            citation = {"snippet": "在线回归资料分块"}
            for key in ("document_id", "filename", "chunk_index", "index_version"):
                if source.get(key) is not None:
                    citation[key] = source[key]
            citations.append(citation)
        response = {
            "id": case["id"],
            "answer": "手动回归回答",
            "citations": citations,
            "trace": {},
            "http_status": 200,
        }
        result = grade_case(case, response, include_content=False)
        mode = prepared["run"]["requested_mode"]
        identity = build_identity([case], mode=mode)
        report = make_report(
            [result], prepared["run"]["run_id"], identity=identity
        )
        report["mode"] = mode
        return report

    # ------------------------------------------------------------ comparison/review

    def test_comparison_and_review_do_not_inherit_history(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        self.load(claim)
        completion = self.complete(claim, answer="新答案 [1]")
        self.assertTrue(completion.passed)

        comparison = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(comparison.status_code, 200, comparison.text)
        data = comparison.json()["data"]
        self.assertEqual(data["old"]["answer"], "原始回归回答")
        self.assertEqual(data["new"]["answer"], "新答案 [1]")
        self.assertEqual(
            data["new"]["result_sha256"], completion.result_sha256
        )
        self.assertEqual(data["job"]["review"]["status"], "unknown")
        self.assertIn("passed", data["new"])

        # 先准备下一轮 manual：历史成功作业仍可比较/核验。
        prepared = self.prepare(feedback_id, revision=2)
        self.assertEqual(prepared.status_code, 200, prepared.text)
        again = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(again.status_code, 200, again.text)

        current = self.get_job(job["job_id"]).json()["data"]
        wrong = self.client.put(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/review",
            headers=self.admin_headers,
            json={
                "expected_revision": current["revision"],
                "run_id": str(uuid.uuid4()),
                "result_sha256": completion.result_sha256,
                "status": "verified",
                "notes": "错误绑定",
            },
        )
        self.assertEqual(wrong.status_code, 409, wrong.text)
        correct = self.client.put(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/review",
            headers=self.admin_headers,
            json={
                "expected_revision": current["revision"],
                "run_id": str(current["run_id"]),
                "result_sha256": completion.result_sha256,
                "status": "verified",
                "notes": "人工确认通过",
            },
        )
        self.assertEqual(correct.status_code, 200, correct.text)
        self.assertEqual(correct.json()["data"]["review"]["status"], "verified")

        # 相同新结果的新作业仍从 unknown 开始，不继承旧核验。
        second = self.create_job_ok(feedback_id, revision=3)
        second_claim = self.claim(second["job_id"])
        self.load(second_claim)
        second_completion = self.complete(second_claim, answer="新答案 [1]")
        self.assertEqual(
            second_completion.result_sha256, completion.result_sha256
        )
        self.assertEqual(
            self.get_job(second["job_id"]).json()["data"]["review"]["status"],
            "unknown",
        )

        # 绑定变化后历史比较必须失败关闭。
        with SessionLocal() as db:
            db.get(Document, self.doc_id).index_version = 1
            db.commit()
        stale = self.client.get(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/comparison",
            headers=self.admin_headers,
        )
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_review_requires_success_and_body_limits_are_enforced(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        self.load(claim)

        # 尚未成功的作业不能核验。
        pending = self.get_job(job["job_id"]).json()["data"]
        review = self.client.put(
            f"/api/v1/feedback/regression/jobs/{job['job_id']}/review",
            headers=self.admin_headers,
            json={
                "expected_revision": pending["revision"],
                "run_id": str(pending["run_id"]),
                "result_sha256": "0" * 64,
                "status": "verified",
                "notes": "",
            },
        )
        self.assertEqual(review.status_code, 409, review.text)

        # 答案超界：不落 body，作业仍可由 worker 显式失败。
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=claim.job_id,
                    lease_id=claim.lease_id,
                    answer="x" * 131073,
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                )
        self.assertEqual(raised.exception.reason, "result_too_large")
        self.assertIsNone(self.body_row(job["job_id"]))

        # 白名单 trace 的 1 MiB 硬限：候选来源元数据超界同样拒绝。
        # 未核验身份的 candidate 必须在评分/持久化前拒绝（不得借 raw trace 混入）。
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=claim.job_id,
                    lease_id=claim.lease_id,
                    answer="非法来源 [1]",
                    citations=[self.citation()],
                    supplied_sources=[self.citation()],
                    trace={
                        "candidate_sources": [
                            {
                                "document_id": "d" * 2048,
                                "filename": "f" * 2048,
                                "index_version": 0,
                                "chunk_index": 0,
                            }
                        ]
                    },
                )
        self.assertEqual(raised.exception.reason, "result_invalid")
        self.assertIsNone(self.body_row(job["job_id"]))
        self.assertEqual(self.job_row(job["job_id"]).status, "running")

        # 身份合法但规范化后的正文超过 1 MiB 硬上限：拒绝且不发布。
        huge = self.citation()
        for key in ("language", "symbol", "symbol_kind"):
            huge[key] = "x" * 2048
        huge_citations = [dict(huge) for _ in range(200)]
        with SessionLocal() as db:
            with self.assertRaises(jobs.JobError) as raised:
                jobs.complete(
                    db,
                    job_id=claim.job_id,
                    lease_id=claim.lease_id,
                    answer="超大正文 [1]",
                    citations=huge_citations,
                    supplied_sources=[self.citation()],
                )
        self.assertEqual(raised.exception.reason, "result_too_large")
        self.assertIsNone(self.body_row(job["job_id"]))
        self.assertEqual(self.job_row(job["job_id"]).status, "running")

        self.assertEqual(raised.exception.reason, "result_too_large")
        self.assertIsNone(self.body_row(job["job_id"]))

        with SessionLocal() as db:
            transition = jobs.fail(
                db,
                job_id=claim.job_id,
                lease_id=claim.lease_id,
                error_code="invalid_result",
                retryable=False,
            )
        self.assertEqual(transition.status, "failed")

    # ------------------------------------------------------------ usage

    def test_usage_context_association_retries_and_unknown_price(self):
        # 位置参数兼容：原六字段 + 可选追加字段。
        identifier = uuid.uuid4()
        legacy = UsageContext(
            identifier, self.kb_id, uuid.uuid4(), uuid.uuid4(), self.doc_id,
            "rewrite",
        )
        self.assertIsNone(legacy.regression_job_id)
        job_identifier = uuid.uuid4()
        context = UsageContext(
            identifier, self.kb_id, None, None, None, "generation", job_identifier
        )
        self.assertEqual(context.for_operation("query_embedding").regression_job_id, job_identifier)
        self.assertEqual(context.for_operation("query_embedding").operation, "query_embedding")

        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        self.assertEqual(loaded.usage_context.regression_job_id, claim.job_id)
        with SessionLocal() as db:
            db.add(
                ModelPrice(
                    provider="usage.test",
                    model="priced-chat",
                    kind="chat",
                    currency="CNY",
                    input_per_million=Decimal("2"),
                    output_per_million=Decimal("8"),
                )
            )
            db.commit()
        self.assertIsNone(loaded.usage_context.conversation_id)
        self.assertIsNone(loaded.usage_context.turn_id)

        with usage_scope(loaded.usage_context):
            call = ModelCall("chat", "usage.test", "unknown-price")
            call.observe({"prompt_tokens": 1000, "completion_tokens": 100})
            call.retry()
            call.observe({"prompt_tokens": 1200, "completion_tokens": 120})
            call.finish()
            rewrite = ModelCall(
                "chat", "usage.test", "unknown-price",
                context=loaded.usage_context.for_operation("rewrite"),
            )
            rewrite.observe({"prompt_tokens": 50, "completion_tokens": 5})
            rewrite.finish()
            priced = ModelCall("chat", "usage.test", "priced-chat")
            priced.observe({"prompt_tokens": 1000, "completion_tokens": 100})
            priced.finish()

        with SessionLocal() as db:
            rows = db.scalars(
                select(ModelUsage)
                .where(ModelUsage.regression_job_id == claim.job_id)
                .order_by(ModelUsage.occurred_at)
            ).all()
        self.assertEqual(len(rows), 4)  # 两次重试 + rewrite + priced
        for row in rows:
            self.assertEqual(row.regression_job_id, claim.job_id)
            self.assertEqual(row.user_id, self.admin_id)
            self.assertNotEqual(row.user_id, self.owner_id)
            self.assertEqual(row.kb_id, self.kb_id)
            self.assertIsNone(row.conversation_id)
            self.assertIsNone(row.turn_id)
        unknown = [row for row in rows if row.model == "unknown-price"]
        self.assertTrue(unknown)
        generation_tokens = {
            row.input_tokens for row in unknown if row.operation == "generation"
        }
        self.assertEqual(generation_tokens, {1000, 1200})
        for row in unknown:
            self.assertIsNone(row.estimated_cost)
            self.assertIsNone(row.input_per_million)
        self.assertIn("rewrite", {row.operation for row in rows})
        priced_row = [row for row in rows if row.model == "priced-chat"][0]
        self.assertIsNotNone(priced_row.estimated_cost)
        self.assertEqual(priced_row.input_tokens, 1000)

        completion = self.complete(claim)
        self.assertTrue(completion.passed)

    def usage_rows_for(self, job_id):
        with SessionLocal() as db:
            return db.scalars(
                select(ModelUsage)
                .where(ModelUsage.regression_job_id == uuid.UUID(str(job_id)))
                .order_by(ModelUsage.occurred_at, ModelUsage.id)
            ).all()

    def assert_no_body_leak(self, payload):
        text_body = json.dumps(payload, ensure_ascii=False)
        for marker in (
            "在线回归回答",
            "原始回归回答",
            "snippet",
            "在线回归资料分块",
            "trace",
            "answer",
        ):
            self.assertNotIn(marker, text_body)

    def test_usage_summary_on_every_job_read_and_api_response(self):
        """所有状态/读取路径都带同一份按 regression_job_id 的全操作/全重试汇总。"""
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        request_id = uuid.uuid4()
        created_response = self.create_job(
            feedback_id, revision=1, request_id=request_id
        )
        self.assertEqual(created_response.status_code, 200, created_response.text)
        job = created_response.json()["data"]
        # 创建响应本身即 queued 且 usage 顶层存在、全部为未知而非 0。
        created = self.get_job(job["job_id"]).json()["data"]
        self.assertEqual(created["status"], "queued")
        empty = created["usage"]
        self.assertEqual(created_response.json()["data"]["usage"], empty)
        self.assertEqual(empty["attempts"], 0)
        self.assertIsNone(empty["input_tokens"])
        self.assertIsNone(empty["known_input_tokens"])
        self.assertIsNone(empty["total_cost"])
        self.assertIsNone(empty["currency"])
        self.assertFalse(empty["currency_mismatch"])

        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        with SessionLocal() as db:
            db.add(
                ModelPrice(
                    provider="usage.test",
                    model="priced-chat",
                    kind="chat",
                    currency="CNY",
                    input_per_million=Decimal("2"),
                    output_per_million=Decimal("8"),
                )
            )
            db.commit()

        # 多 operation + 重试 + 一条纯 embedding（kind 无价格） + 未知价格模型。
        with usage_scope(loaded.usage_context):
            generation = ModelCall("chat", "usage.test", "priced-chat")
            generation.observe({"prompt_tokens": 1000, "completion_tokens": 100})
            generation.finish()
            retry = ModelCall("chat", "usage.test", "priced-chat")
            retry.observe({"prompt_tokens": 700, "completion_tokens": 70})
            retry.finish("failed")  # 失败重试仍计入用量
            rewrite = ModelCall(
                "chat", "usage.test", "unknown-price",
                context=loaded.usage_context.for_operation("rewrite"),
            )
            rewrite.observe({"prompt_tokens": 50, "completion_tokens": 5})
            rewrite.finish()
            embedding = ModelCall(
                "embedding", "usage.test", "priced-chat",
                context=loaded.usage_context.for_operation("query_embedding"),
            )
            embedding.observe({"total_tokens": 33})
            embedding.finish()
            second_generation = ModelCall("chat", "usage.test", "priced-chat")
            second_generation.observe(
                {"prompt_tokens": 10, "completion_tokens": 2}
            )
            second_generation.finish()

        rows = self.usage_rows_for(job["job_id"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            {row.operation for row in rows},
            {"generation", "rewrite", "query_embedding"},
        )
        self.assertEqual(
            {row.status for row in rows}, {"succeeded", "failed"}
        )
        for row in rows:
            self.assertIsNone(row.conversation_id)
            self.assertIsNone(row.turn_id)

        # 取消（仍会带取消前已发生的全部调用）——先看 running 详情。
        running = self.get_job(job["job_id"]).json()["data"]
        self.assertEqual(running["status"], "running")
        usage = running["usage"]
        self.assertEqual(usage["attempts"], 5)
        self.assertEqual(usage["succeeded_attempts"], 4)
        self.assertEqual(usage["failed_attempts"], 1)
        # 一条 embedding 只测到 input，一条未知价格仍测到 token → token 总量可给。
        self.assertEqual(usage["usage_missing_attempts"], 0)
        self.assertEqual(usage["input_tokens"], 1000 + 700 + 50 + 33 + 10)
        self.assertEqual(usage["output_tokens"], 100 + 70 + 5 + 0 + 2)
        self.assertEqual(usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"])
        self.assertEqual(usage["known_input_tokens"], usage["input_tokens"])
        self.assertEqual(usage["known_output_tokens"], usage["output_tokens"])
        # 三条已定价（三次 priced-chat），embedding 无对应 kind 价格、rewrite 未知价格。
        self.assertEqual(usage["priced_attempts"], 3)
        self.assertEqual(usage["unpriced_attempts"], 2)
        # 单币种（ModelUsage 落库币种为 CNY）：未知价格的调用不阻止已知小计，
        # 但因存在未定价调用，完整 total_cost 保持 null。
        self.assertFalse(usage["currency_mismatch"])
        self.assertEqual(usage["currency"], "CNY")
        self.assertIsNotNone(usage["known_cost_subtotal"])
        self.assertIsNone(usage["total_cost"])

        cancelled = self.cancel_job(job["job_id"], running["revision"])
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        cancelled_usage = cancelled.json()["data"]["usage"]
        self.assertEqual(cancelled_usage["attempts"], 5)
        self.assertEqual(cancelled_usage["input_tokens"], usage["input_tokens"])
        # 取消响应仍不含任何正文/答案/trace。
        self.assertNotIn("在线回归回答", cancelled.text)
        self.assertNotIn("trace", cancelled.text)

        # 单条找回、分页列表、批次视图与取消后详情都复用同一聚合，且不含正文。
        recovered = self.lookup_request(request_id)
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["data"]["job"]["usage"]["attempts"], 5)
        listed = self.client.get(
            "/api/v1/feedback/regression/jobs",
            headers=self.admin_headers,
            params={"feedback_id": str(feedback_id)},
        ).json()["data"]
        self.assertEqual(listed["total"], 1)
        listed_usage = listed["items"][0]["usage"]
        self.assertEqual(listed_usage, cancelled_usage)
        self.assert_no_body_leak(listed)

        # 批次视图同样一次聚合全部成员，且每个成员都带 usage。
        batch = self.create_batch([(feedback_id, 2)])
        self.assertEqual(batch.status_code, 200, batch.text)
        batch_detail = self.get_batch(batch.json()["data"]["batch_id"]).json()["data"]
        self.assertEqual(len(batch_detail["jobs"]), 1)
        self.assertIn("usage", batch_detail["jobs"][0])
        self.assertEqual(batch_detail["jobs"][0]["usage"]["attempts"], 0)
        self.assertIsNone(batch_detail["jobs"][0]["usage"]["known_input_tokens"])

    def test_usage_summary_is_batched_not_per_job_queries(self):
        """列表/批次只发一次有界用量分组查询，不按 job 扫表。"""
        feedback_ids = []
        for index in range(3):
            feedback_id, _ = self.verified_feedback(
                question=f"批量用量问题{index}"
            )
            self.enroll_ok(feedback_id)
            feedback_ids.append(feedback_id)
        batch = self.create_batch([(fid, 1) for fid in feedback_ids])
        self.assertEqual(batch.status_code, 200, batch.text)
        batch_id = batch.json()["data"]["batch_id"]

        usage_statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if "model_usage" in statement and "group by" in statement.lower():
                usage_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            detail = self.get_batch(batch_id)
            self.assertEqual(detail.status_code, 200, detail.text)
            listed = self.client.get(
                "/api/v1/feedback/regression/jobs",
                headers=self.admin_headers,
                params={"batch_id": batch_id, "page_size": 100},
            )
            self.assertEqual(listed.status_code, 200, listed.text)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        self.assertEqual(len(usage_statements), 2)
        self.assertEqual(listed.json()["data"]["total"], 3)
        for item in listed.json()["data"]["items"]:
            self.assertEqual(item["usage"]["attempts"], 0)
            self.assertIsNone(item["usage"]["input_tokens"])

    def test_usage_summary_is_present_for_succeeded_and_failed_jobs(self):
        """成功与失败的终态作业同样带实际用量（失败不是 0 用量）。"""
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        with usage_scope(loaded.usage_context):
            call = ModelCall("chat", "usage.test", "priced-chat")
            call.observe({"prompt_tokens": 300, "completion_tokens": 30})
            call.finish()
        with SessionLocal() as db:
            transition = jobs.fail(
                db,
                job_id=claim.job_id,
                lease_id=claim.lease_id,
                error_code="model_error",
                retryable=False,
            )
        self.assertEqual(transition.status, "failed")
        failed = self.get_job(job["job_id"]).json()["data"]
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["usage"]["attempts"], 1)
        self.assertEqual(failed["usage"]["input_tokens"], 300)
        self.assertEqual(failed["usage"]["output_tokens"], 30)
        self.assertIsNone(failed["usage"]["total_cost"])
        self.assertNotIn("trace", failed)

        second = self.create_job_ok(feedback_id, revision=2)
        second_claim = self.claim(second["job_id"])
        second_loaded = self.load(second_claim)
        with usage_scope(second_loaded.usage_context):
            call = ModelCall("chat", "usage.test", "priced-chat")
            call.observe({"prompt_tokens": 100, "completion_tokens": 10})
            call.finish()
        completion = self.complete(second_claim)
        self.assertTrue(completion.passed)
        succeeded = self.get_job(second["job_id"]).json()["data"]
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["usage"]["attempts"], 1)
        self.assertEqual(succeeded["usage"]["input_tokens"], 100)

    def test_usage_summary_partial_missing_keeps_known_subtotals_and_multi_currency(self):
        """部分缺测保留已知 token 小计；完全未测时小计为 null；多币种不相加。"""
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        with SessionLocal() as db:
            db.add(
                ModelPrice(
                    provider="usage.test",
                    model="priced-chat",
                    kind="chat",
                    currency="CNY",
                    input_per_million=Decimal("2"),
                    output_per_million=Decimal("8"),
                )
            )
            db.commit()
        # 直接落真实 ModelUsage 行覆盖“已结束但缺 usage”的调用：模拟 provider
        # 只回了部分字段，聚合必须保留已测部分而不是补 0。
        with usage_scope(loaded.usage_context):
            measured = ModelCall("chat", "usage.test", "priced-chat")
            measured.observe({"prompt_tokens": 400, "completion_tokens": 40})
            measured.finish()
            partial = ModelCall("chat", "usage.test", "priced-chat")
            partial.observe({"prompt_tokens": 90})
            partial.finish()

        with SessionLocal() as db:
            db.add(
                ModelUsage(
                    user_id=self.admin_id,
                    kb_id=self.kb_id,
                    regression_job_id=claim.job_id,
                    provider="usage.test",
                    model="priced-chat",
                    kind="chat",
                    operation="rerank",
                    status="succeeded",
                    currency="CNY",
                    input_tokens=None,
                    output_tokens=None,
                    cached_input_tokens=None,
                    estimated_cost=None,
                    finished_at=datetime.now(timezone.utc),
                )
            )
            db.commit()

        usage = self.get_job(job["job_id"]).json()["data"]["usage"]
        self.assertEqual(usage["attempts"], 3)
        # 两条缺测（一条只测到 input、一条完全缺测）：总量与完整费用保持 null。
        self.assertEqual(usage["usage_missing_attempts"], 2)
        self.assertEqual(usage["usage_known_attempts"], 1)
        self.assertIsNone(usage["input_tokens"])
        self.assertIsNone(usage["output_tokens"])
        self.assertIsNone(usage["total_tokens"])
        self.assertIsNone(usage["total_cost"])
        # 已知部分仍有小计：input 400+90=490，output 只有 40（另一条缺 output）。
        self.assertEqual(usage["known_input_tokens"], 490)
        self.assertEqual(usage["known_output_tokens"], 40)
        # 只有 1 条同时测到 token 且已定价，已知小计可给；总量仍因缺测为 null。
        self.assertEqual(usage["priced_attempts"], 1)
        self.assertEqual(usage["unpriced_attempts"], 2)
        self.assertEqual(usage["currency"], "CNY")
        self.assertFalse(usage["currency_mismatch"])
        self.assertIsNotNone(usage["known_cost_subtotal"])

        with SessionLocal() as db:
            db.add(
                ModelUsage(
                    user_id=self.admin_id,
                    kb_id=self.kb_id,
                    regression_job_id=claim.job_id,
                    provider="usage.test",
                    model="priced-chat",
                    kind="chat",
                    operation="generation",
                    status="succeeded",
                    currency="USD",
                    input_tokens=1,
                    output_tokens=1,
                    cached_input_tokens=None,
                    estimated_cost=Decimal("0.000001"),
                    finished_at=datetime.now(timezone.utc),
                )
            )
            db.commit()
        mixed = self.get_job(job["job_id"]).json()["data"]["usage"]
        self.assertTrue(mixed["currency_mismatch"])
        self.assertIsNone(mixed["currency"])
        self.assertIsNone(mixed["known_cost_subtotal"])
        self.assertIsNone(mixed["total_cost"])

    # ------------------------------------------------------------ cascade/migration

    def test_feedback_delete_cascades_job_but_keeps_usage_uuid(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        job = self.create_job_ok(feedback_id, revision=1)
        online_run_id = job["run_id"]
        claim = self.claim(job["job_id"])
        loaded = self.load(claim)
        with usage_scope(loaded.usage_context):
            call = ModelCall("chat", "usage.test", "cascade-model")
            call.observe({"prompt_tokens": 10, "completion_tokens": 2})
            call.finish()
        self.complete(claim)
        self.assertIsNotNone(self.body_row(job["job_id"]))

        with SessionLocal() as db:
            db.delete(db.get(AnswerFeedback, uuid.UUID(str(feedback_id))))
            db.commit()

        with SessionLocal() as db:
            self.assertIsNone(
                db.scalar(
                    select(FeedbackRegressionJob).where(
                        FeedbackRegressionJob.id == uuid.UUID(str(job["job_id"]))
                    )
                )
            )
            self.assertIsNone(
                db.get(FeedbackRegressionJobBody, uuid.UUID(str(job["job_id"])))
            )
            self.assertIsNone(
                db.get(FeedbackRegressionRun, uuid.UUID(str(online_run_id)))
            )
            usage_rows = db.scalars(
                select(ModelUsage).where(
                    ModelUsage.regression_job_id == uuid.UUID(str(job["job_id"]))
                )
            ).all()
        self.assertEqual(len(usage_rows), 1)
        self.assertEqual(usage_rows[0].regression_job_id, uuid.UUID(str(job["job_id"])))

    def test_z_idempotent_migration_restores_columns_and_defaults(self):
        feedback_id, _ = self.verified_feedback()
        self.enroll_ok(feedback_id)
        prepared = self.prepare(feedback_id, revision=1)
        self.assertEqual(prepared.status_code, 200, prepared.text)
        run_id = prepared.json()["data"]["run"]["run_id"]

        with engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE feedback_regression_runs "
                    "DROP COLUMN IF EXISTS execution_kind"
                )
            )
            conn.execute(
                text("DROP INDEX IF EXISTS ix_model_usage_regression_job")
            )
            conn.execute(
                text(
                    "ALTER TABLE model_usage "
                    "DROP COLUMN IF EXISTS regression_job_id"
                )
            )

        init_db(create_admin=False)
        init_db(create_admin=False)

        with engine.begin() as conn:
            run_kind = conn.execute(
                text(
                    "SELECT execution_kind FROM feedback_regression_runs "
                    "WHERE id = :id"
                ),
                {"id": run_id},
            ).scalar()
            default = conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'feedback_regression_runs' "
                    "AND column_name = 'execution_kind'"
                )
            ).scalar()
            usage_column = conn.execute(
                text(
                    "SELECT data_type FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = 'model_usage' "
                    "AND column_name = 'regression_job_id'"
                )
            ).scalar()
            usage_index = conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = current_schema() "
                    "AND tablename = 'model_usage' "
                    "AND indexname = 'ix_model_usage_regression_job'"
                )
            ).scalar()
        self.assertEqual(run_kind, "manual")
        self.assertIn("manual", str(default))
        self.assertEqual(usage_column, "uuid")
        self.assertEqual(usage_index, "ix_model_usage_regression_job")


if __name__ == "__main__":
    unittest.main()
