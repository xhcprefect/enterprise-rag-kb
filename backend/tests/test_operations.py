"""运维接口：指标采集鉴权/关闭、管理员摘要与数据库不可用降级。

仅使用 F 专属 ``_test`` 库；所有导入前显式禁用 .env，模型地址固定在回环。
"""
import os
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["RAG_DISABLE_DOTENV"] = "1"
os.environ.setdefault("EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

from tests._env import TEST_URL, prepare  # noqa: E402

# 与既有数据库用例同一约定：只接受显式提供、库名以 _test 结尾的专用库，
# 未提供时整组跳过（不算通过）；凭据不写进仓库。
prepare()
_TEST_URL = TEST_URL


from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.v1 import operations
from app.core.config import settings
from app.core.observability import metrics, start_chat
from app.core.security import create_access_token, hash_password
from app.db import session as db_session
from app.models import (
    Conversation,
    Document,
    DocumentStatus,
    KnowledgeBase,
    SummaryJob,
    SummaryJobStatus,
    User,
    WorkerHeartbeat,
)
from app.services import worker_status as ws


from tests._memory_project_test_gate import verify  # noqa: E402


def _assert_owned_test_database():
    return verify(_TEST_URL,
                  os.environ.get("TEST_DATABASE_OWNER", "") or os.environ.get("TEST_EXPECTED_OWNER", ""),
                  db_session.engine)


# 不重绑实际 app engine；配置不匹配必须在连接/建表之前失败。
if _TEST_URL:
    _assert_owned_test_database()
    from app.db.init_db import init_db
    init_db(create_admin=False)


def _async_return(value):
    async def _inner():
        return value

    return _inner


def build_app() -> FastAPI:
    from app.core.observability import HttpMetricsMiddleware

    application = FastAPI()
    application.add_middleware(HttpMetricsMiddleware)
    application.include_router(operations.router, prefix="/api/v1")
    return application


@unittest.skipUnless(TEST_URL, "需要显式 TEST_DATABASE_URL（库名以 _test 结尾）")
class OperationsTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _assert_owned_test_database()
        cls.app = build_app()
        cls.client = TestClient(cls.app)
        cls._purge()
        with db_session.SessionLocal() as db:
            cls.admin = User(username=f"ops_admin_{uuid.uuid4().hex[:8]}", role="admin",
                             is_active=True, password_hash=hash_password("AdminPass123"))
            cls.member = User(username=f"ops_member_{uuid.uuid4().hex[:8]}", role="user",
                              is_active=True, password_hash=hash_password("MemberPass123"))
            db.add_all([cls.admin, cls.member])
            db.commit()
            db.refresh(cls.admin)
            db.refresh(cls.member)
            cls.kb = KnowledgeBase(name=f"ops_kb_{uuid.uuid4().hex[:8]}",
                                   collection_name=f"ops_col_{uuid.uuid4().hex[:8]}")
            db.add(cls.kb)
            db.commit()
            db.refresh(cls.kb)

    @classmethod
    def tearDownClass(cls):
        cls._purge()
        with db_session.SessionLocal() as db:
            db.execute(delete(User).where(User.username.in_([cls.admin.username, cls.member.username])))
            db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == cls.kb.id))
            db.commit()

    @classmethod
    def _purge(cls):
        with db_session.SessionLocal() as db:
            db.execute(delete(Document).where(Document.filename.like("ops_test_%")))
            db.commit()

    def setUp(self):
        _assert_owned_test_database()
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def _admin_headers(self):
        token = create_access_token(str(self.admin.id), self.admin.role, token_version=self.admin.token_version)
        return {"Authorization": f"Bearer {token}"}

    def _member_headers(self):
        token = create_access_token(str(self.member.id), self.member.role, token_version=self.member.token_version)
        return {"Authorization": f"Bearer {token}"}

    def _add_document(self, status=DocumentStatus.PENDING.value, heartbeat=None, lease_id=None, **retry):
        with db_session.SessionLocal() as db:
            doc = Document(kb_id=self.kb.id, filename=f"ops_test_{uuid.uuid4().hex[:8]}.txt",
                           file_ext="txt", file_size=1, stored_path="ops_test.txt",
                           status=status, heartbeat_at=heartbeat, lease_id=lease_id, **retry)
            db.add(doc)
            db.commit()
            db.refresh(doc)
            return doc.id

    # ---- metrics ------------------------------------------------------
    def test_metrics_disabled_when_token_empty_even_with_admin_jwt(self):
        with patch.object(settings, "METRICS_TOKEN", ""):
            response = self.client.get("/api/v1/operations/metrics", headers=self._admin_headers())
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("ragkb_", response.text)

    def test_metrics_requires_its_own_token_and_rejects_admin_jwt(self):
        with patch.object(settings, "METRICS_TOKEN", "collector-token-xyz"):
            missing = self.client.get("/api/v1/operations/metrics")
            wrong = self.client.get("/api/v1/operations/metrics", headers={"Authorization": "Bearer nope"})
            admin_jwt = self.client.get("/api/v1/operations/metrics", headers=self._admin_headers())
            ok = self.client.get("/api/v1/operations/metrics",
                                 headers={"Authorization": "Bearer collector-token-xyz"})
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(admin_jwt.status_code, 401)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.headers["content-type"].startswith("text/plain; version=0.0.4"))
        self.assertIn("ragkb_metrics_process_local 1", ok.text)

    def test_metrics_never_contains_identifiers_or_secrets(self):
        marker = uuid.uuid4()
        start_chat("rag").finish("completed", first_token_ms=10, total_ms=20)
        with patch.object(settings, "METRICS_TOKEN", "t0ken"), \
             patch.object(settings, "LLM_API_KEY", "sk-should-not-appear"):
            response = self.client.get("/api/v1/operations/metrics",
                                       headers={"Authorization": "Bearer t0ken"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(marker), response.text)
        self.assertNotIn("sk-should-not-appear", response.text)
        self.assertNotIn(self.admin.username, response.text)
        self.assertNotIn("127.0.0.1:9", response.text)

    def test_protected_route_via_real_middleware_uses_template_without_uuid(self):
        # 只验证 metrics 端点自身的标签路径（没有 UUID 参数）不含原始路径成分。
        with patch.object(settings, "METRICS_TOKEN", "t0ken"):
            self.client.get("/api/v1/operations/metrics", headers={"Authorization": "Bearer t0ken"})
        text = metrics.render()
        self.assertIn('route="/api/v1/operations/metrics"', text)
        self.assertIn('status="200"', text)

    # ---- metrics 中的队列/连接池 gauge --------------------------------
    def test_metrics_exports_queue_and_pool_gauges_from_real_database(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS + 60)
        self._add_document(DocumentStatus.PENDING.value)
        stale_id = self._add_document(DocumentStatus.PROCESSING.value, heartbeat=old)
        try:
            with patch.object(settings, "METRICS_TOKEN", "t0ken"):
                response = self.client.get("/api/v1/operations/metrics",
                                           headers={"Authorization": "Bearer t0ken"})
            self.assertEqual(response.status_code, 200)
            text = response.text
            self.assertIn("ragkb_db_available 1", text)
            for item in DocumentStatus:
                self.assertIn(f'ragkb_ingest_documents{{status="{item.value}"}}', text)
            pending = next(
                int(line.rsplit(" ", 1)[1]) for line in text.splitlines()
                if line.startswith('ragkb_ingest_documents{status="pending"}')
            )
            processing = next(
                int(line.rsplit(" ", 1)[1]) for line in text.splitlines()
                if line.startswith('ragkb_ingest_documents{status="processing"}')
            )
            queued = next(
                int(line.rsplit(" ", 1)[1]) for line in text.splitlines()
                if line.startswith("ragkb_ingest_queued_documents ")
            )
            stale = next(
                int(line.rsplit(" ", 1)[1]) for line in text.splitlines()
                if line.startswith("ragkb_ingest_stale_processing_documents ")
            )
            self.assertGreaterEqual(pending, 1)
            self.assertGreaterEqual(processing, 1)
            self.assertEqual(queued, pending + processing)
            self.assertGreaterEqual(stale, 1)
            self.assertIn("ragkb_ingest_oldest_pending_age_seconds ", text)
            self.assertIn("ragkb_db_pool_capacity ", text)
            self.assertIn("ragkb_db_pool_checked_out ", text)
            # 与 summary 一样不得泄露标识/文档名/模型地址
            self.assertNotIn(str(stale_id), text)
            self.assertNotIn("ops_test_", text)
            self.assertNotIn(self.admin.username, text)
            self.assertNotIn("127.0.0.1:9", text)
        finally:
            self._purge()

    def test_retry_waiting_and_terminal_failures_match_real_database_and_metrics(self):
        self._purge()
        before = operations._collect_db_stats(2.0)
        now = datetime.now(timezone.utc)
        self._add_document(ingest_attempts=1, last_error_code="network",
                           next_attempt_at=now + timedelta(minutes=10))
        self._add_document(ingest_attempts=2, last_error_code="timeout",
                           next_attempt_at=now - timedelta(seconds=1))
        self._add_document()  # 从未失败的普通 pending 仍计入 queued。
        self._add_document(ingest_attempts=1, last_error_code="network", cancel_requested=True,
                           next_attempt_at=now + timedelta(minutes=10))  # 取消清理可立即领，不算等待。
        self._add_document(DocumentStatus.PROCESSING.value, heartbeat=now, lease_id=uuid.uuid4(),
                           ingest_attempts=1, last_error_code="network")
        self._add_document(DocumentStatus.FAILED.value, ingest_attempts=1, last_error_code="empty_content")
        self._add_document(DocumentStatus.FAILED.value, ingest_attempts=3, last_error_code="network")
        try:
            response = self.client.get("/api/v1/operations/summary", headers=self._admin_headers())
            self.assertEqual(response.status_code, 200)
            queue = response.json()["data"]["queue"]
            self.assertEqual(queue["retry_waiting"], before["retry_waiting"] + 1)
            self.assertEqual(queue["failed"], before["failed"] + 2)
            self.assertEqual(queue["queued"], before["queued"] + 5)
            self.assertEqual(queue["queued"], queue["statuses"]["pending"] + queue["statuses"]["processing"])
            with patch.object(settings, "METRICS_TOKEN", "t0ken"):
                metrics_response = self.client.get("/api/v1/operations/metrics",
                                                   headers={"Authorization": "Bearer t0ken"})
            self.assertEqual(metrics_response.status_code, 200)
            for field, metric in (("retry_waiting", "ragkb_ingest_retry_waiting_documents"),
                                  ("failed", "ragkb_ingest_failed_documents"),
                                  ("queued", "ragkb_ingest_queued_documents")):
                values = [line for line in metrics_response.text.splitlines() if line.startswith(metric + " ")]
                self.assertEqual(values, [f"{metric} {queue[field]}"])
        finally:
            self._purge()

    def test_metrics_omits_unknown_queue_values_when_database_unavailable(self):
        from sqlalchemy import create_engine

        broken = create_engine("postgresql+psycopg://ragtest:bad@127.0.0.1:9/unreachable",
                               connect_args={"connect_timeout": 1})
        with patch.object(db_session, "engine", broken), \
             patch.object(settings, "METRICS_TOKEN", "t0ken"), \
             patch.object(settings, "OPERATIONS_DB_TIMEOUT_SECONDS", 1.0):
            response = self.client.get("/api/v1/operations/metrics",
                                       headers={"Authorization": "Bearer t0ken"})
        broken.dispose()
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("ragkb_db_available 0", text)
        # 未知队列长度不导出，避免被误读成"队列为空"
        self.assertNotIn("ragkb_ingest_documents", text)
        self.assertNotIn("ragkb_ingest_queued_documents", text)
        self.assertNotIn("ragkb_ingest_stale_processing_documents", text)
        self.assertNotIn("ragkb_ingest_oldest_pending_age_seconds", text)
        self.assertNotIn("ragkb_ingest_retry_waiting_documents", text)
        self.assertNotIn("ragkb_ingest_failed_documents", text)
        self.assertNotIn("bad", text)

    def test_metrics_auth_is_checked_before_touching_the_database(self):
        with patch.object(operations, "_collect_db_stats",
                          side_effect=AssertionError("未授权请求不应查询数据库")):
            with patch.object(settings, "METRICS_TOKEN", "t0ken"):
                missing = self.client.get("/api/v1/operations/metrics")
                wrong = self.client.get("/api/v1/operations/metrics",
                                        headers={"Authorization": "Bearer nope"})
                admin_jwt = self.client.get("/api/v1/operations/metrics",
                                            headers=self._admin_headers())
            with patch.object(settings, "METRICS_TOKEN", ""):
                disabled = self.client.get("/api/v1/operations/metrics")
        self.assertEqual([missing.status_code, wrong.status_code, admin_jwt.status_code], [401, 401, 401])
        self.assertEqual(disabled.status_code, 503)

    def test_metrics_database_timeout_is_bounded_and_not_zeroed(self):
        def slow(timeout_seconds):
            time.sleep(5)
            return {"status": "ok"}

        started = time.perf_counter()
        with patch.object(operations, "_collect_db_stats", side_effect=slow), \
             patch.object(settings, "METRICS_TOKEN", "t0ken"), \
             patch.object(settings, "OPERATIONS_DB_TIMEOUT_SECONDS", 0.2):
            response = self.client.get("/api/v1/operations/metrics",
                                       headers={"Authorization": "Bearer t0ken"})
        elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        self.assertLess(elapsed, 4.0)
        self.assertIn("ragkb_db_available 0", response.text)
        self.assertNotIn("ragkb_ingest_queued_documents", response.text)
        self.assertNotIn('ragkb_ingest_documents{status="pending"} 0', response.text)
        self.assertNotIn("ragkb_ingest_retry_waiting_documents", response.text)
        self.assertNotIn("ragkb_ingest_failed_documents", response.text)

    # ---- summary ------------------------------------------------------
    def test_summary_requires_admin(self):
        self.assertEqual(self.client.get("/api/v1/operations/summary").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/operations/summary", headers=self._member_headers()).status_code, 403)

    def test_summary_reports_queue_and_pool_for_admin(self):
        old = datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS + 60)
        self._add_document(DocumentStatus.PENDING.value)
        stale_id = self._add_document(DocumentStatus.PROCESSING.value, heartbeat=old)
        try:
            response = self.client.get("/api/v1/operations/summary", headers=self._admin_headers())
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertTrue(body["success"])
            data = body["data"]
            self.assertTrue(data["process_local"])
            self.assertFalse(data["degraded"])
            self.assertEqual(data["audit"], {"write_failures": 0})
            self.assertIsNotNone(data["queue"])
            self.assertGreaterEqual(data["queue"]["statuses"]["pending"], 1)
            self.assertGreaterEqual(data["queue"]["stale_processing"], 1)
            self.assertGreater(data["queue"]["queued"], 0)
            self.assertIsNotNone(data["database"]["pool"]["capacity"])
            # 不暴露文档名/UUID/用户名/模型地址
            blob = response.text
            self.assertNotIn(str(stale_id), blob)
            self.assertNotIn(self.admin.username, blob)
            self.assertNotIn("ops_test_", blob)
            self.assertNotIn("127.0.0.1:9", blob)
        finally:
            self._purge()

    def test_summary_degrades_when_database_unavailable(self):
        from sqlalchemy import create_engine

        metrics.audit_write_failed()
        broken = create_engine("postgresql+psycopg://ragtest:bad@127.0.0.1:9/unreachable",
                               connect_args={"connect_timeout": 1})
        with patch.object(db_session, "engine", broken), \
             patch.object(settings, "OPERATIONS_DB_TIMEOUT_SECONDS", 1.0):
            response = self.client.get("/api/v1/operations/summary", headers=self._admin_headers())
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["degraded"])
        self.assertEqual(data["audit"], {"write_failures": 1})
        self.assertIsNone(data["queue"])
        self.assertEqual(data["database"]["status"], "unavailable")
        self.assertIn(data["database"]["error"], {"query_failed", "timeout"})
        self.assertNotIn("bad", response.text)
        broken.dispose()

    def test_summary_timeout_is_bounded_and_not_zeroed(self):
        def slow(timeout_seconds):
            time.sleep(5)
            return {"status": "ok"}

        started = time.perf_counter()
        with patch.object(operations, "_collect_db_stats", side_effect=slow), \
             patch.object(settings, "OPERATIONS_DB_TIMEOUT_SECONDS", 0.2):
            response = self.client.get("/api/v1/operations/summary", headers=self._admin_headers())
        elapsed = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["degraded"])
        self.assertIsNone(data["queue"])  # 不把超时误报成"队列为空"
        self.assertEqual(data["database"]["error"], "timeout")
        self.assertLess(elapsed, 4.0)


    # ---- worker 状态 --------------------------------------------------
    def _add_heartbeat(self, role, *, age=0, stopped=False):
        with db_session.SessionLocal() as db:
            row = WorkerHeartbeat(
                role=role,
                started_at=datetime.now(timezone.utc) - timedelta(seconds=age + 5),
                heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=age),
                stopped_at=datetime.now(timezone.utc) if stopped else None,
            )
            db.add(row)
            db.commit()
            return row.instance_id

    def _add_summary_job(self, status, *, heartbeat=None, error_code=None):
        with db_session.SessionLocal() as db:
            conv = Conversation(
                user_id=self.admin.id, mode="chat",
                title=f"ops_test_conv_{uuid.uuid4().hex[:8]}",
            )
            db.add(conv)
            db.flush()
            job = SummaryJob(
                conversation_id=conv.id, idempotency_key=f"ops-{uuid.uuid4().hex}",
                status=status, heartbeat_at=heartbeat, error_code=error_code,
            )
            db.add(job)
            db.commit()
            return job.id

    def _purge_worker_observations(self):
        with db_session.SessionLocal() as db:
            db.execute(delete(SummaryJob).where(SummaryJob.idempotency_key.like("ops-%")))
            db.execute(delete(Conversation).where(Conversation.title.like("ops_test_conv_%")))
            db.execute(delete(WorkerHeartbeat))
            db.commit()

    def test_workers_requires_admin_and_no_store(self):
        self.assertEqual(self.client.get("/api/v1/operations/workers").status_code, 401)
        self.assertEqual(
            self.client.get("/api/v1/operations/workers", headers=self._member_headers()).status_code,
            403,
        )
        response = self.client.get("/api/v1/operations/workers", headers=self._admin_headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["code"], 0)

    def test_workers_auth_is_checked_before_querying(self):
        with patch.object(operations.worker_status_service, "collect_worker_status",
                          side_effect=AssertionError("未授权请求不应查询数据库")):
            self.assertEqual(self.client.get("/api/v1/operations/workers").status_code, 401)
            self.assertEqual(
                self.client.get(
                    "/api/v1/operations/workers", headers=self._member_headers()
                ).status_code,
                403,
            )

    def test_workers_payload_contract_and_no_leaks(self):
        old_source = self._add_heartbeat("summary", age=ws.STALE_AFTER_SECONDS + 10)
        self._add_heartbeat("ingest", age=1)
        self._add_heartbeat("ingest", age=2)
        pending_id = self._add_document(DocumentStatus.PENDING.value)
        stale_id = self._add_document(
            DocumentStatus.PROCESSING.value,
            heartbeat=datetime.now(timezone.utc) - timedelta(
                seconds=settings.INGEST_STALE_SECONDS + 30
            ),
            lease_id=uuid.uuid4(),
        )
        failure_id = self._add_document(DocumentStatus.FAILED.value)
        job_id = self._add_summary_job(
            SummaryJobStatus.FAILED.value, error_code="C:\\secret\\private\\name.txt"
        )
        try:
            with patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
                 patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"demo": "/tmp/demo"}):
                response = self.client.get(
                    "/api/v1/operations/workers", headers=self._admin_headers()
                )
            self.assertEqual(response.status_code, 200)
            data = response.json()["data"]
            self.assertEqual(data["stale_after_seconds"], 45)
            self.assertFalse(data["degraded"])
            self.assertIn("generated_at", data)
            roles = {item["role"]: item for item in data["roles"]}
            self.assertEqual(list(roles), ["ingest", "summary", "project", "regression"])
            for role in ("ingest", "summary", "project", "regression"):
                self.assertEqual(
                    set(roles[role]),
                    {
                        "role", "enabled", "state", "alive_count", "last_heartbeat_at",
                        "pending", "processing", "stale_tasks",
                        "oldest_pending_age_seconds", "last_failure",
                    },
                )
            self.assertTrue(roles["ingest"]["enabled"])
            self.assertEqual(roles["ingest"]["state"], "running")
            self.assertEqual(roles["ingest"]["alive_count"], 2)
            self.assertGreaterEqual(roles["ingest"]["pending"], 1)
            self.assertGreaterEqual(roles["ingest"]["processing"], 1)
            self.assertGreaterEqual(roles["ingest"]["stale_tasks"], 1)
            self.assertIsNotNone(roles["ingest"]["oldest_pending_age_seconds"])
            self.assertEqual(roles["ingest"]["last_failure"]["code"], "document-failed")
            self.assertEqual(roles["summary"]["state"], "stale")
            self.assertEqual(roles["summary"]["alive_count"], 0)
            self.assertEqual(roles["summary"]["last_failure"]["code"], "failed")
            self.assertIsNotNone(roles["summary"]["last_failure"]["updated_at"])
            self.assertIn(roles["project"]["state"], {"not_started", "disabled"})
            self.assertTrue(roles["regression"]["enabled"])
            self.assertIn(roles["regression"]["state"], {"not_started", "unknown"})
            blob = response.text
            for leaked in (
                str(pending_id), str(stale_id), str(failure_id), str(job_id),
                str(old_source), "secret", "private", "name.txt", "ops_test_",
                self.admin.username, "127.0.0.1:9",
            ):
                self.assertNotIn(leaked, blob)
        finally:
            self._purge()
            self._purge_worker_observations()

    def test_workers_degrade_to_null_when_database_unavailable(self):
        from sqlalchemy import create_engine

        broken = create_engine(
            "postgresql+psycopg://ragtest:bad@127.0.0.1:9/unreachable",
            connect_args={"connect_timeout": 1},
        )
        with patch.object(db_session, "engine", broken), \
             patch.object(settings, "OPERATIONS_DB_TIMEOUT_SECONDS", 1.0), \
             patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
             patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"demo": "/tmp/demo"}):
            response = self.client.get(
                "/api/v1/operations/workers", headers=self._admin_headers()
            )
        broken.dispose()
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["degraded"])
        for item in data["roles"]:
            self.assertTrue(item["enabled"])
            self.assertEqual(item["state"], "unknown")
            self.assertIsNone(item["alive_count"])
            self.assertIsNone(item["pending"])
            self.assertIsNone(item["processing"])
            self.assertIsNone(item["stale_tasks"])
            self.assertIsNone(item["oldest_pending_age_seconds"])
            self.assertIsNone(item["last_failure"])
        self.assertNotIn("bad", response.text)

    def test_workers_timeout_is_bounded_and_not_zeroed(self):
        # 超时由 collect_worker_status 内部的 anyio 超时收敛（见 test_worker_status），
        # 这里验证接口在超时/异常结果下仍返回 degraded + null，而不是 500 或补零。
        async def timeout_result():
            return {"status": "unavailable", "error": "timeout"}

        with patch.object(operations.worker_status_service, "collect_worker_status",
                          new=timeout_result), \
             patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
             patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"demo": "/tmp/demo"}):
            response = self.client.get(
                "/api/v1/operations/workers", headers=self._admin_headers()
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["degraded"])
        for item in data["roles"]:
            self.assertTrue(item["enabled"])
            self.assertEqual(item["state"], "unknown")
            self.assertIsNone(item["pending"])

    def test_worker_observation_failure_never_breaks_existing_summary(self):
        # worker 观测失败不能让已有 /summary 变成 500 或改变其语义。
        with patch.object(operations.worker_status_service, "collect_worker_status",
                          side_effect=RuntimeError("broken")):
            response = self.client.get("/api/v1/operations/summary", headers=self._admin_headers())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["degraded"])

    # ---- worker metrics ------------------------------------------------
    def test_metrics_export_worker_roles(self):
        self._add_heartbeat("ingest", age=1)
        try:
            with patch.object(settings, "METRICS_TOKEN", "t0ken"), \
                 patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
                 patch.object(settings, "PROJECT_ALLOWED_ROOTS", {}):
                response = self.client.get(
                    "/api/v1/operations/metrics",
                    headers={"Authorization": "Bearer t0ken"},
                )
            self.assertEqual(response.status_code, 200)
            text = response.text
            self.assertIn("ragkb_workers_available 1", text)
            for role in ("ingest", "summary", "project", "regression"):
                self.assertIn('ragkb_worker_enabled{role="%s"}' % role, text)
                self.assertIn('ragkb_worker_alive{role="%s"}' % role, text)
                self.assertIn('ragkb_worker_pending_tasks{role="%s"}' % role, text)
                self.assertIn('ragkb_worker_processing_tasks{role="%s"}' % role, text)
                self.assertIn('ragkb_worker_stale_tasks{role="%s"}' % role, text)
            self.assertIn('ragkb_worker_enabled{role="ingest"} 1', text)
            self.assertIn('ragkb_worker_enabled{role="project"} 0', text)
            self.assertIn('ragkb_worker_enabled{role="regression"} 1', text)
            self.assertIn('ragkb_worker_alive{role="ingest"} 1', text)
        finally:
            self._purge_worker_observations()

    def test_metrics_omit_worker_values_when_observation_fails(self):
        broken = {"status": "unavailable", "error": "query_failed"}
        with patch.object(settings, "METRICS_TOKEN", "t0ken"), \
             patch.object(operations.worker_status_service, "collect_worker_status",
                          new=_async_return(broken)):
            response = self.client.get(
                "/api/v1/operations/metrics", headers={"Authorization": "Bearer t0ken"}
            )
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("ragkb_workers_available 0", text)
        # 未知不导出，避免被告警读成 0；enabled 只来自配置，仍可输出。
        self.assertNotIn("ragkb_worker_alive", text)
        self.assertNotIn("ragkb_worker_pending_tasks", text)
        self.assertNotIn("ragkb_worker_processing_tasks", text)
        self.assertNotIn("ragkb_worker_stale_tasks", text)
        self.assertNotIn("ragkb_worker_last_failure", text)
        # 现有数据库指标仍按自身结果独立输出。
        self.assertIn("ragkb_db_available 1", text)

    def test_metrics_worker_failure_does_not_break_existing_metrics(self):
        with patch.object(settings, "METRICS_TOKEN", "t0ken"), \
             patch.object(operations.worker_status_service, "collect_worker_status",
                          side_effect=RuntimeError("boom")):
            response = self.client.get(
                "/api/v1/operations/metrics", headers={"Authorization": "Bearer t0ken"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("ragkb_workers_available 0", response.text)
        self.assertIn("ragkb_db_available 1", response.text)
        self.assertIn("ragkb_metrics_process_local 1", response.text)

    def test_metrics_worker_failure_code_is_sanitized(self):
        self._add_summary_job(
            SummaryJobStatus.FAILED.value, error_code='evil code "{x="y"}'
        )
        try:
            with patch.object(settings, "METRICS_TOKEN", "t0ken"), \
                 patch.object(settings, "CHAT_MEMORY_ENABLED", True):
                response = self.client.get(
                    "/api/v1/operations/metrics",
                    headers={"Authorization": "Bearer t0ken"},
                )
            text = response.text
            self.assertIn(
                'ragkb_worker_last_failure_info{role="summary",code="failed"} 1', text
            )
            self.assertNotIn("evil", text)
            self.assertIn("ragkb_worker_last_failure_timestamp_seconds", text)
        finally:
            self._purge_worker_observations()

    def test_summary_chat_counters_are_process_local(self):
        metrics.audit_write_failed()
        start_chat("rag").finish("completed", first_token_ms=5, total_ms=6)
        response = self.client.get("/api/v1/operations/summary", headers=self._admin_headers())
        data = response.json()["data"]
        self.assertEqual(data["chat"]["finished"].get("rag:completed"), 1)
        self.assertEqual(data["audit"], {"write_failures": 1})
        self.assertTrue(data["process_local"])


if __name__ == "__main__":
    unittest.main()
