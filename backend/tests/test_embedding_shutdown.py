"""入库向量的停机注入：不连真实模型，验证重试/退避/作用域边界与 Ollama 超时。"""
import json
import threading
import time
import unittest
from unittest.mock import patch

from tests import _ingest_shutdown_env as _env  # noqa: F401 - 必须在 app imports 前设置环境

import httpx

from app.core.config import settings
from app.services import cloud_models

requires_db = unittest.skipUnless(_env.ENABLED, getattr(_env, "_REASON", "需要专用测试库"))


def _endpoint():
    return cloud_models._Endpoint("http://127.0.0.1:9/v1", "not-a-real-key", 120)


@requires_db
class EmbeddingStopInjectionTests(unittest.TestCase):
    def setUp(self):
        self.sleeps = []
        patcher = patch.object(cloud_models, "_sleep_or_stop",
                               side_effect=lambda seconds: self.sleeps.append(seconds))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _adapter(self, statuses, *, calls=None):
        """构造只在内存中响应给定状态码序列的向量适配器。

        200 返回与请求条数匹配的假向量；其余状态码返回错误体。
        """
        responses = iter(statuses)

        def handle(request):
            if calls is not None:
                calls.append(request)
            status = next(responses)
            if status == 200:
                count = len(json.loads(request.content)["input"])
                return httpx.Response(200, json={
                    "data": [{"index": i, "embedding": [1.0] + [0.0] * 1023} for i in range(count)],
                    "usage": {"prompt_tokens": count, "total_tokens": count},
                })
            return httpx.Response(status, json={"error": {"message": "synthetic"}})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        patcher = patch.object(cloud_models, "_shared_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return cloud_models.OpenAICompatibleEmbeddings(_endpoint(), "embed-test", 16)

    def test_stop_requested_before_call_starts_no_attempt_and_no_retry(self):
        stop = threading.Event()
        stop.set()
        calls = []
        adapter = self._adapter([200] * 3, calls=calls)
        with cloud_models.embedding_stop_scope(stop):
            with self.assertRaises(cloud_models.EmbeddingStopped):
                adapter.embed_documents(["a", "b"])
        self.assertEqual(calls, [])
        self.assertEqual(self.sleeps, [])
        self.assertIsInstance(cloud_models.EmbeddingStopped("x"), cloud_models.CloudModelError)

    def test_stop_during_backoff_wakes_immediately_without_new_attempt(self):
        stop = threading.Event()
        calls = []

        def handle(request):
            calls.append(request)
            stop.set()  # 第一次尝试拿到 503 时，停机信号到达
            return httpx.Response(503, json={"error": {"message": "synthetic"}})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        with patch.object(cloud_models, "_shared_client", return_value=client):
            adapter = cloud_models.OpenAICompatibleEmbeddings(_endpoint(), "embed-test", 16)
            started = time.perf_counter()
            with cloud_models.embedding_stop_scope(stop):
                with self.assertRaises(cloud_models.EmbeddingStopped):
                    adapter.embed_documents(["a"])
            elapsed = time.perf_counter() - started
        self.assertEqual(len(calls), 1, "停机后不得开始新重试")
        self.assertEqual(self.sleeps, [1.5])  # 退避经可唤醒实现，未 sleep 真时间
        self.assertLess(elapsed, 1.0)

    def test_successful_response_after_stop_signal_is_still_returned(self):
        stop = threading.Event()

        def handle(request):
            stop.set()  # 响应已成功返回的同时收到停止
            return httpx.Response(200, json={
                "data": [{"index": 0, "embedding": [1.0] + [0.0] * 1023}],
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            })

        client = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(client.close)
        with patch.object(cloud_models, "_shared_client", return_value=client):
            adapter = cloud_models.OpenAICompatibleEmbeddings(_endpoint(), "embed-test", 16)
            with cloud_models.embedding_stop_scope(stop):
                vectors = adapter.embed_documents(["a"])
        self.assertEqual(len(vectors), 1)

    def test_normal_call_keeps_three_attempts_after_scope_exits(self):
        calls = []
        adapter = self._adapter([503, 503, 200], calls=calls)
        # 作用域已退出：不得残留停机状态，必须按原逻辑重试到成功。
        with cloud_models.embedding_stop_scope(threading.Event()):
            pass
        vectors = adapter.embed_documents(["a"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps, [1.5, 3.0])
        self.assertEqual(len(vectors), 1)

    def test_context_is_reset_even_when_raising(self):
        stop = threading.Event()
        with self.assertRaises(RuntimeError):
            with cloud_models.embedding_stop_scope(stop):
                raise RuntimeError("synthetic")
        self.assertIsNone(cloud_models._embedding_stop.get())

    def test_stop_scope_does_not_affect_threads_without_it(self):
        stop = threading.Event()
        stop.set()
        seen = {}

        def other_thread():
            calls = []
            adapter = self._adapter([200], calls=calls)
            seen["vectors"] = adapter.embed_documents(["a"])
            seen["calls"] = calls

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join(timeout=10)
        self.assertEqual(len(seen["calls"]), 1)  # ContextVar 不跨线程泄漏
        self.assertEqual(len(seen["vectors"]), 1)


@requires_db
class OllamaTimeoutTests(unittest.TestCase):
    def test_ollama_embeddings_receive_embedding_timeout_on_both_clients(self):
        from app.services import vector_store

        vector_store.get_embeddings.cache_clear()
        self.addCleanup(vector_store.get_embeddings.cache_clear)
        with patch.object(settings, "EMBEDDING_PROVIDER", "ollama"), \
             patch.object(settings, "EMBEDDING_TIMEOUT", 7):
            emb = vector_store.get_embeddings()
        self.assertEqual(emb._client._client.timeout, httpx.Timeout(7))
        self.assertEqual(emb._async_client._client.timeout, httpx.Timeout(7))

    def test_cloud_embedding_endpoint_still_uses_embedding_timeout(self):
        with patch.object(settings, "EMBEDDING_TIMEOUT", 33):
            self.assertEqual(cloud_models._embedding_endpoint().timeout, 33)


if __name__ == "__main__":
    unittest.main()
