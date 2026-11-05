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
from sqlalchemy.engine import make_url

from app.api.v1 import operations
from app.core.config import settings
from app.core.observability import metrics, start_chat
from app.core.security import create_access_token, hash_password
from app.db import session as db_session
from app.models import Document, DocumentStatus, KnowledgeBase, User


def _bind_engine() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    if not _TEST_URL:
        return
    target = make_url(_TEST_URL)
    if db_session.engine.url != target:
        db_session.engine.dispose()
        db_session.engine = create_engine(
            _TEST_URL, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800
        )
        db_session.SessionLocal.configure(bind=db_session.engine)


_bind_engine()

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  确保所有表已注册

if _TEST_URL:
    Base.metadata.create_all(bind=db_session.engine)


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
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def _admin_headers(self):
        token = create_access_token(str(self.admin.id), self.admin.role, token_version=self.admin.token_version)
        return {"Authorization": f"Bearer {token}"}

    def _member_headers(self):
        token = create_access_token(str(self.member.id), self.member.role, token_version=self.member.token_version)
        return {"Authorization": f"Bearer {token}"}

    def _add_document(self, status=DocumentStatus.PENDING.value, heartbeat=None):
        with db_session.SessionLocal() as db:
            doc = Document(kb_id=self.kb.id, filename=f"ops_test_{uuid.uuid4().hex[:8]}.txt",
                           file_ext="txt", file_size=1, stored_path="ops_test.txt",
                           status=status, heartbeat_at=heartbeat)
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
