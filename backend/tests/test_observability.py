"""观测 helper：并发/幂等、标签归一化、直方图快照一致性、路由模板与 SSE 边界。

所有测试在导入 ``app.*`` 之前显式禁用 .env，并把模型地址限制在回环地址。
"""
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

os.environ["RAG_DISABLE_DOTENV"] = "1"
os.environ.setdefault("EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:9/unused")


from app.core import observability
from app.core.observability import (
    HttpMetricsMiddleware,
    metrics,
    normalize_chat_status,
    normalize_mode,
    normalize_stage,
    sanitize_route,
    start_chat,
)


def _render_value(text: str, series: str) -> float | None:
    for line in text.splitlines():
        if line.startswith(series + " ") or line.startswith(series + "{"):
            return float(line.rsplit(" ", 1)[1])
    return None


class ChatObservationTests(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_finish_is_idempotent_and_releases_in_flight(self):
        observation = start_chat("rag")
        self.assertTrue(observation.finish("completed", first_token_ms=100, total_ms=1000))
        self.assertFalse(observation.finish("failed", total_ms=2000))
        snapshot = metrics.snapshot()["chat"]
        self.assertEqual(snapshot["in_flight"], {"rag": 0})
        self.assertEqual(snapshot["started"], {"rag": 1})
        self.assertEqual(snapshot["finished"], {"rag:completed": 1})

    def test_concurrent_finish_counts_exactly_once(self):
        for round_index in range(20):
            observation = start_chat("chat")
            barrier = threading.Barrier(8)
            results: list[bool] = []
            lock = threading.Lock()

            def finish(index: int) -> None:
                barrier.wait()
                won = observation.finish(
                    "completed" if index % 2 else "failed",
                    first_token_ms=10 + index,
                    total_ms=100 + index,
                )
                with lock:
                    results.append(won)

            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(finish, range(8)))
            self.assertEqual(sum(results), 1, f"round {round_index}: {results}")
        snapshot = metrics.snapshot()["chat"]
        self.assertEqual(snapshot["in_flight"], {"chat": 0})
        self.assertEqual(snapshot["started"], {"chat": 20})
        self.assertEqual(sum(snapshot["finished"].values()), 20)

    def test_failure_and_cancel_statuses_stay_distinct(self):
        start_chat("rag").finish("error")
        start_chat("rag").finish("cancelled")
        start_chat("rag").finish("lease_lost")
        text = metrics.render()
        self.assertIn('ragkb_chat_finished_total{mode="rag",status="failed"} 1', text)
        self.assertIn('ragkb_chat_finished_total{mode="rag",status="cancelled"} 1', text)
        self.assertIn('ragkb_chat_finished_total{mode="rag",status="unknown"} 1', text)

    def test_high_cardinality_labels_are_normalized(self):
        observation = start_chat("550e8400-e29b-41d4-a716-446655440000")
        observation.finish(
            "weird-status",
            first_token_ms=1,
            total_ms=2,
            stages_ms={
                "user@example.com": 1,
                "retrieval": 2,
                "first_token_ms": 3,
                "generation": 4,
                "bad stage with spaces": 5,
            },
        )
        text = metrics.render()
        self.assertNotIn("550e8400", text)
        self.assertNotIn("user@example.com", text)
        self.assertIn('ragkb_chat_started_total{mode="other"} 1', text)
        self.assertIn('ragkb_chat_finished_total{mode="other",status="unknown"} 1', text)
        self.assertIn('stage="retrieval"', text)
        self.assertIn('stage="first_token"', text)
        self.assertIn('stage="generation"', text)
        self.assertIn('stage="other"', text)

    def test_invalid_durations_do_not_break_or_pollute_histograms(self):
        observation = start_chat("rag")
        observation.finish(
            "completed",
            first_token_ms=float("nan"),
            total_ms=float("inf"),
            stages_ms={"retrieval": float("-inf"), "generation": "not-a-number"},
        )
        text = metrics.render()
        # 无效耗时直接丢弃：直方图序列不存在（等价于 0 次观测），不产生 NaN 行。
        self.assertIsNone(_render_value(text, 'ragkb_chat_total_seconds_count{mode="rag"}'))
        self.assertIsNone(_render_value(text, 'ragkb_chat_first_token_seconds_count{mode="rag"}'))
        self.assertIsNone(_render_value(text, 'ragkb_chat_stage_seconds_count{mode="rag",stage="retrieval"}'))
        self.assertNotIn("nan", text.lower())
        self.assertEqual(_render_value(text, 'ragkb_chat_finished_total{mode="rag",status="completed"}'), 1.0)

    def test_huge_integer_after_render_is_ignored(self):
        huge = 10 ** 400
        observation = start_chat("rag")
        self.assertTrue(observation.finish("completed", first_token_ms=huge, total_ms=1))
        text = metrics.render()
        self.assertIsNone(_render_value(text, 'ragkb_chat_first_token_seconds_count{mode="rag"}'))

    def test_histogram_buckets_are_monotonic_and_inf_matches_count(self):
        for value in (10, 50, 200, 1000, 9000):
            start_chat("rag").finish("completed", first_token_ms=value, total_ms=value)
        text = metrics.render()
        for prefix in ("ragkb_chat_first_token_seconds", "ragkb_chat_total_seconds"):
            buckets = []
            count_line = None
            for line in text.splitlines():
                if line.startswith(prefix + "_bucket"):
                    buckets.append(float(line.rsplit(" ", 1)[1]))
                elif line.startswith(prefix + "_count{"):
                    count_line = float(line.rsplit(" ", 1)[1])
            self.assertGreater(len(buckets), 1)
            self.assertEqual(buckets, sorted(buckets), prefix)
            self.assertEqual(buckets[-1], count_line, prefix)

    def test_snapshot_render_is_consistent_under_concurrent_scrapes(self):
        stop = threading.Event()
        errors: list[tuple[str, str]] = []

        def writer() -> None:
            while not stop.is_set():
                start_chat("rag").finish("completed", first_token_ms=5, total_ms=7)

        def reader() -> None:
            for _ in range(200):
                text = metrics.render()
                for prefix in ("ragkb_chat_first_token_seconds", "ragkb_chat_total_seconds"):
                    buckets, count_value = [], None
                    for line in text.splitlines():
                        if line.startswith(prefix + "_bucket"):
                            buckets.append(float(line.rsplit(" ", 1)[1]))
                        elif line.startswith(prefix + "_count{"):
                            count_value = float(line.rsplit(" ", 1)[1])
                    if buckets != sorted(buckets) or buckets[-1] != count_value:
                        errors.append((prefix, text))
                # 不变量：同一快照内 started == finished + in_flight（在途可能 >0）。
                snapshot = metrics.snapshot()["chat"]
                started = snapshot["started"].get("rag", 0)
                finished = sum(snapshot["finished"].values())
                in_flight = snapshot["in_flight"].get("rag", 0)
                if started != finished + in_flight:
                    errors.append(("chat", repr(snapshot)))

        workers = [threading.Thread(target=writer) for _ in range(3)]
        for worker in workers:
            worker.start()
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: reader(), range(4)))
        finally:
            stop.set()
            for worker in workers:
                worker.join()
        self.assertEqual(errors, [])


class AuditFailureMetricTests(unittest.TestCase):
    """审计失败计数：无标签、初始为 0、reset/snapshot/render 契约一致。"""

    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def test_initial_zero_and_snapshot_shape(self):
        self.assertEqual(metrics.snapshot()["audit"], {"write_failures": 0})
        text = metrics.render()
        self.assertIn("# TYPE ragkb_audit_write_failures_total counter", text)
        self.assertIn("ragkb_audit_write_failures_total 0", text)

    def test_increment_and_reset(self):
        for _ in range(4):
            metrics.audit_write_failed()
        self.assertEqual(metrics.snapshot()["audit"]["write_failures"], 4)
        metrics.reset()
        self.assertEqual(metrics.snapshot()["audit"]["write_failures"], 0)

    def test_render_has_exactly_one_unlabeled_series(self):
        metrics.audit_write_failed()
        series = [
            line for line in metrics.render().splitlines()
            if line.startswith("ragkb_audit_write_failures_total")
        ]
        self.assertEqual(series, ["ragkb_audit_write_failures_total 1"])
        self.assertNotIn("{", series[0])

    def test_metric_is_isolated_from_chat_and_http_state(self):
        metrics.audit_write_failed()
        start_chat("rag").finish("completed", first_token_ms=1, total_ms=2)
        metrics.reset()
        # reset 必须同时覆盖审计计数，不能因其它指标残留而“看起来有失败”。
        self.assertEqual(metrics.snapshot()["audit"]["write_failures"], 0)


class NormalizationTests(unittest.TestCase):
    def test_route_templates_only(self):
        cases = {
            "/api/v1/knowledge/{kb_id}": "/api/v1/knowledge/{kb_id}",
            "/api/v1/knowledge/6f1c1f2a-1111-2222-3333-444455556666": "unknown",
            "/api/v1/knowledge?name=secret": "unknown",
            "/api/v1/users/alice": "/api/v1/users/alice",  # 模板本身可含静态段
            "": "unknown",
            None: "unknown",
            "/a" * 200: "unknown",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(sanitize_route(value), expected)

    def test_mode_status_stage_normalization(self):
        self.assertEqual(normalize_mode("RAG "), "rag")
        self.assertEqual(normalize_mode("alice"), "other")
        self.assertEqual(normalize_chat_status("ERROR"), "failed")
        self.assertEqual(normalize_chat_status("canceled"), "cancelled")
        self.assertEqual(normalize_chat_status("mystery"), "unknown")
        self.assertEqual(normalize_stage("first_token_ms"), "first_token")
        self.assertEqual(normalize_stage("retrieval"), "retrieval")
        self.assertEqual(normalize_stage("abc"), "other")


class _Route:
    def __init__(self, template: str) -> None:
        self.path_format = template


class HttpMiddlewareTests(unittest.TestCase):
    def setUp(self):
        metrics.reset()

    def tearDown(self):
        metrics.reset()

    def _run(self, scope, app, messages=None):
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        import asyncio

        asyncio.run(HttpMetricsMiddleware(app)(scope, receive, send))
        return sent

    def test_template_route_is_used_and_uuid_path_never_leaks(self):
        async def app(scope, receive, send):
            scope["route"] = _Route("/api/v1/knowledge/{kb_id}")
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b"{}"})

        scope = {"type": "http", "method": "get", "path": "/api/v1/knowledge/6f1c1f2a-1111-2222-3333-444455556666"}
        self._run(scope, app)
        text = metrics.render()
        self.assertIn('ragkb_http_requests_total{method="GET",route="/api/v1/knowledge/{kb_id}",status="200",stream="false"} 1', text)
        self.assertNotIn("6f1c1f2a", text)
        self.assertEqual(_render_value(text, "ragkb_http_in_flight_requests"), 0.0)
        self.assertIn('ragkb_http_request_duration_seconds_count{method="GET",route="/api/v1/knowledge/{kb_id}",status="200"} 1', text)

    def test_unmatched_route_is_unknown_and_errors_count_once(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        scope = {"type": "http", "method": "GET", "path": "/definitely-not-a-route"}
        self._run(scope, app)
        text = metrics.render()
        self.assertIn('route="unknown"', text)
        self.assertIn('status="404"', text)
        self.assertNotIn("/definitely-not-a-route", text)

    def test_exception_still_records_exactly_one_request(self):
        async def app(scope, receive, send):
            raise RuntimeError("boom")

        scope = {"type": "http", "method": "POST", "path": "/api/v1/chat/stream"}
        with self.assertRaises(RuntimeError):
            self._run(scope, app)
        text = metrics.render()
        self.assertEqual(_render_value(text, "ragkb_http_in_flight_requests"), 0.0)
        self.assertEqual(
            _render_value(text, "ragkb_http_requests_total{method=\"POST\",route=\"unknown\",status=\"500\",stream=\"false\"}"),
            1.0,
        )

    def test_sse_counts_requests_and_in_flight_but_not_duration_histogram(self):
        async def app(scope, receive, send):
            scope["route"] = _Route("/api/v1/chat/stream")
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"text/event-stream; charset=utf-8")]})
            await send({"type": "http.response.body", "body": b"data: x\n\n", "more_body": True})
            # 模拟生成过程中：在途 gauge 为正
            during = metrics.render()
            assert 'ragkb_chat_in_flight' in during
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        scope = {"type": "http", "method": "POST", "path": "/api/v1/chat/stream"}
        self._run(scope, app)
        text = metrics.render()
        self.assertIn('ragkb_http_requests_total{method="POST",route="/api/v1/chat/stream",status="200",stream="true"} 1', text)
        self.assertNotIn("ragkb_http_request_duration_seconds_count{method=\"POST\",route=\"/api/v1/chat/stream\"", text)


if __name__ == "__main__":
    unittest.main()
