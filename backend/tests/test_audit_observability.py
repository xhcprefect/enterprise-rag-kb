"""审计写入失败的可观测性：不阻断业务、只暴露安全信息、计数恰好一次。

覆盖 ``app.api.deps.write_audit`` 的失败路径：

- 构造 AuditLog / ``db.add`` / ``db.commit`` 任一步失败都只记录一次失败；
- ``rollback`` 自身失败也不会抛出，且 rollback 失败有独立的事件名；
- ERROR 日志只含固定事件名与安全异常类型，绝不含 SQL、异常正文、
  traceback、target/detail、用户名、IP、请求头或 URL；
- ``ragkb_audit_write_failures_total`` 无标签、初始为 0、成功不计数、可 reset，
  snapshot 与 Prometheus 文本都能读到。

不需要数据库：全部使用假 Session，真实 PG 由其它测试单独覆盖。
"""
import logging
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

os.environ["RAG_DISABLE_DOTENV"] = "1"
os.environ.setdefault("EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:9/unused")

from app.api import deps  # noqa: E402
from app.core.observability import metrics  # noqa: E402

# 合成敏感值：它们出现在异常正文 / 用户 / 请求里，但绝不允许出现在日志或指标中。
SYNTHETIC_SQL = "INSERT INTO audit_logs (target, detail) VALUES ('/secret/kb', 'pw=hunter2')"
SYNTHETIC_SECRET = "sk-live-audit-should-not-appear"
SYNTHETIC_TARGET = "/api/v1/knowledge/kb-secret-target"
SYNTHETIC_USERNAME = "alice.audit@example.com"
SYNTHETIC_IP = "203.0.113.77"
SYNTHETIC_UA = "SecretAgent/1.0"


class _FakeSession:
    """最小 Session 替身：分别注入 add / commit / rollback 的失败。"""

    def __init__(self, *, add_error=None, commit_error=None, rollback_error=None):
        self.add_error = add_error
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, obj):
        if self.add_error is not None:
            raise self.add_error
        self.added.append(obj)

    def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error


def _user():
    return SimpleNamespace(id=uuid.uuid4(), username=SYNTHETIC_USERNAME)


def _request():
    return SimpleNamespace(
        client=SimpleNamespace(host=SYNTHETIC_IP),
        headers={"user-agent": SYNTHETIC_UA},
    )


def _failure_count() -> int:
    return metrics.snapshot()["audit"]["write_failures"]


def _error_records(log_context) -> list[str]:
    return [record.getMessage() for record in log_context.records]


class AuditWriteFailureTests(unittest.TestCase):
    def setUp(self):
        metrics.reset()
        self.user = _user()
        self.request = _request()

    def tearDown(self):
        metrics.reset()

    def _write(self, session):
        return deps.write_audit(
            session,
            self.request,
            self.user,
            "knowledge.view",
            target=SYNTHETIC_TARGET,
            detail={"note": SYNTHETIC_SECRET},
        )

    def test_success_writes_and_does_not_increment(self):
        session = _FakeSession()
        with self.assertNoLogs("app.api.deps", level="ERROR"):
            self.assertIsNone(self._write(session))
        self.assertEqual(len(session.added), 1)
        self.assertEqual(session.commits, 1)
        self.assertEqual(session.rollbacks, 0)
        self.assertEqual(_failure_count(), 0)

    def test_model_construction_failure_is_swallowed_logged_and_counted(self):
        session = _FakeSession(rollback_error=RuntimeError("rollback also broken"))

        class _ExplodingAuditLog:
            def __init__(self, **kwargs):
                raise RuntimeError(f"{SYNTHETIC_SQL} {SYNTHETIC_SECRET}")

        with patch.object(deps, "AuditLog", _ExplodingAuditLog):
            with self.assertLogs("app.api.deps", level="ERROR") as captured:
                self.assertIsNone(self._write(session))

        messages = _error_records(captured)
        # 构造失败 + rollback 失败各一条；失败计数只加 1。
        self.assertEqual(len(messages), 2)
        self.assertIn(deps.AUDIT_WRITE_FAILED_EVENT, messages[0])
        self.assertIn("exc_type=RuntimeError", messages[0])
        self.assertIn(deps.AUDIT_ROLLBACK_FAILED_EVENT, messages[1])
        self.assertEqual(_failure_count(), 1)

    def test_add_failure_rolls_back_and_does_not_raise(self):
        session = _FakeSession(add_error=RuntimeError(SYNTHETIC_SQL))
        with self.assertLogs("app.api.deps", level="ERROR") as captured:
            self.assertIsNone(self._write(session))
        self.assertEqual(session.rollbacks, 1)
        self.assertEqual(session.commits, 0)
        self.assertEqual(_failure_count(), 1)
        self.assertEqual(len(_error_records(captured)), 1)

    def test_commit_failure_rolls_back_and_counts_exactly_once(self):
        session = _FakeSession(commit_error=RuntimeError(f"commit failed: {SYNTHETIC_SECRET}"))
        with self.assertLogs("app.api.deps", level="ERROR") as captured:
            self.assertIsNone(self._write(session))
        self.assertEqual(session.commits, 1)
        self.assertEqual(session.rollbacks, 1)
        self.assertEqual(_failure_count(), 1)
        self.assertEqual(len(_error_records(captured)), 1)

    def test_rollback_failure_does_not_escape_and_is_observed(self):
        session = _FakeSession(
            commit_error=RuntimeError("commit failed"),
            rollback_error=RuntimeError(SYNTHETIC_SQL),
        )
        # 关键断言：rollback 抛错也不能逃出 write_audit。
        with self.assertLogs("app.api.deps", level="ERROR") as captured:
            self.assertIsNone(self._write(session))
        messages = _error_records(captured)
        self.assertEqual(len(messages), 2)
        self.assertIn(deps.AUDIT_ROLLBACK_FAILED_EVENT, messages[1])
        self.assertIn("exc_type=RuntimeError", messages[1])
        # rollback 失败不重复计数：一次写入失败仍只 +1。
        self.assertEqual(_failure_count(), 1)

    def test_error_log_contains_no_business_data_or_traceback(self):
        session = _FakeSession(
            commit_error=RuntimeError(
                f"{SYNTHETIC_SQL} target={SYNTHETIC_TARGET} user={SYNTHETIC_USERNAME}"
            ),
            rollback_error=RuntimeError(f"rollback {SYNTHETIC_SECRET}"),
        )
        with self.assertLogs("app.api.deps", level="ERROR") as captured:
            self.assertIsNone(self._write(session))

        for record, message in zip(captured.records, _error_records(captured)):
            # 不输出异常对象/堆栈，只有固定事件名与安全类型。
            self.assertIsNone(record.exc_info)
            self.assertNotIn("Traceback", message)
            for forbidden in (
                SYNTHETIC_SQL,
                SYNTHETIC_SECRET,
                SYNTHETIC_TARGET,
                SYNTHETIC_USERNAME,
                SYNTHETIC_IP,
                SYNTHETIC_UA,
                "INSERT",
                "hunter2",
                "audit_logs",
            ):
                self.assertNotIn(forbidden, message)
            self.assertRegex(message, r"^audit_(write|rollback)_failed exc_type=[A-Za-z0-9_]+$")

    def test_exception_type_is_sanitized(self):
        evil = type("Bad\n-Type{label=\"x\"}", (Exception,), {})
        session = _FakeSession(commit_error=evil("boom"))
        with self.assertLogs("app.api.deps", level="ERROR") as captured:
            self.assertIsNone(self._write(session))
        message = _error_records(captured)[0]
        self.assertIn("exc_type=BadType", message)
        self.assertNotIn("\n", message)
        self.assertNotIn("{", message)

    def test_log_handler_failure_still_rolls_back_and_does_not_raise(self):
        """日志 handler 自身抛错时，rollback 仍必须被调用，调用栈不得炸。"""
        session = _FakeSession(commit_error=RuntimeError("db down"))

        class _ExplodingHandler(logging.Handler):
            def emit(self, record):
                raise OSError("log sink down")

        handler = _ExplodingHandler()
        deps.logger.addHandler(handler)
        try:
            # 关键断言：不抛异常，且 rollback 确实执行。
            self.assertIsNone(self._write(session))
        finally:
            deps.logger.removeHandler(handler)
        self.assertEqual(session.rollbacks, 1)
        self.assertEqual(_failure_count(), 1)

    def test_concurrent_failures_count_exactly_once_each(self):
        workers = 8
        per_worker = 50
        barrier = threading.Barrier(workers)

        class _CountingHandler(logging.Handler):
            def __init__(self):
                super().__init__()
                self.count = 0
                self._guard = threading.Lock()

            def emit(self, record):
                with self._guard:
                    self.count += 1

        # 单一日志捕获：在线程启动前挂好 handler。每线程各自 assertLogs 会并发
        # 替换同一个 logger.handlers，导致记录丢失/竞态，这里不再那样做。
        handler = _CountingHandler()
        deps.logger.addHandler(handler)
        try:
            def fail_many(_index: int) -> None:
                barrier.wait()
                for _ in range(per_worker):
                    session = _FakeSession(commit_error=RuntimeError("db down"))
                    deps.write_audit(session, None, None, "knowledge.view")

            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(fail_many, range(workers)))
        finally:
            deps.logger.removeHandler(handler)

        self.assertEqual(_failure_count(), workers * per_worker)
        self.assertEqual(handler.count, workers * per_worker)
        text = metrics.render()
        self.assertIn(f"ragkb_audit_write_failures_total {workers * per_worker}", text)

    def test_successes_in_parallel_do_not_increment(self):
        def succeed(_index: int) -> None:
            deps.write_audit(_FakeSession(), None, None, "knowledge.view")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(succeed, range(200)))
        self.assertEqual(_failure_count(), 0)


class AuditMetricContractTests(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_initial_zero_snapshot_shape_and_reset(self):
        self.assertEqual(_failure_count(), 0)
        self.assertIn("ragkb_audit_write_failures_total 0", metrics.render())

        metrics.audit_write_failed()
        self.assertEqual(_failure_count(), 1)
        self.assertEqual(metrics.snapshot()["audit"], {"write_failures": 1})

        metrics.reset()
        self.assertEqual(_failure_count(), 0)
        # reset 必须把导出文本也归零，而不是残留旧计数。
        self.assertIn("ragkb_audit_write_failures_total 0", metrics.render())

    def test_metric_has_no_labels_and_render_matches_snapshot(self):
        for _ in range(3):
            metrics.audit_write_failed()
        text = metrics.render()
        series_lines = [
            line for line in text.splitlines()
            if line.startswith("ragkb_audit_write_failures_total")
            # HELP/TYPE 行以 "# " 开头，不会命中 startswith。
        ]
        self.assertEqual(series_lines, ["ragkb_audit_write_failures_total 3"])
        self.assertEqual(_failure_count(), 3)

        # snapshot 只暴露失败次数，不含任何字符串数据。
        audit = metrics.snapshot()["audit"]
        self.assertEqual(set(audit), {"write_failures"})
        self.assertIsInstance(audit["write_failures"], int)

    def test_render_and_snapshot_never_contain_sensitive_values(self):
        session = _FakeSession(
            commit_error=RuntimeError(
                f"{SYNTHETIC_SQL} target={SYNTHETIC_TARGET} user={SYNTHETIC_USERNAME} "
                f"ip={SYNTHETIC_IP} ua={SYNTHETIC_UA}"
            )
        )
        with self.assertLogs("app.api.deps", level="ERROR"):
            deps.write_audit(
                session,
                _request(),
                _user(),
                "knowledge.view",
                target=SYNTHETIC_TARGET,
                detail={"note": SYNTHETIC_SECRET},
            )

        payloads = (metrics.render(), repr(metrics.snapshot()))
        for payload in payloads:
            for forbidden in (
                SYNTHETIC_SQL,
                SYNTHETIC_SECRET,
                SYNTHETIC_TARGET,
                SYNTHETIC_USERNAME,
                SYNTHETIC_IP,
                SYNTHETIC_UA,
                "hunter2",
                "audit_logs",
            ):
                self.assertNotIn(forbidden, payload)
        self.assertIn("ragkb_audit_write_failures_total 1", metrics.render())


class AuditMetricsEndpointTests(unittest.TestCase):
    """/operations/metrics 用真实路由 + 真实 render；数据库摘要被打桩，无需 PG。"""

    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_metrics_endpoint_exposes_audit_counter_without_sensitive_data(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.v1 import operations
        from app.core.config import settings

        application = FastAPI()
        application.include_router(operations.router, prefix="/api/v1")
        client = TestClient(application)

        session = _FakeSession(commit_error=RuntimeError(f"{SYNTHETIC_SQL} {SYNTHETIC_SECRET}"))
        with self.assertLogs("app.api.deps", level="ERROR"):
            deps.write_audit(
                session,
                _request(),
                _user(),
                "knowledge.view",
                target=SYNTHETIC_TARGET,
                detail={"note": SYNTHETIC_SECRET},
            )

        async def _no_database():
            return {"status": "unavailable", "error": "timeout", "pool": {}}

        with patch.object(operations, "_database_summary", _no_database), \
             patch.object(settings, "METRICS_TOKEN", "audit-collector"):
            response = client.get(
                "/api/v1/operations/metrics",
                headers={"Authorization": "Bearer audit-collector"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain; version=0.0.4"))
        self.assertIn("ragkb_audit_write_failures_total 1", response.text)
        for forbidden in (
            SYNTHETIC_SQL,
            SYNTHETIC_SECRET,
            SYNTHETIC_TARGET,
            SYNTHETIC_USERNAME,
            SYNTHETIC_IP,
            SYNTHETIC_UA,
            "hunter2",
        ):
            self.assertNotIn(forbidden, response.text)


if __name__ == "__main__":
    unittest.main()
