"""追问补全传输是单次、有总截止时间的轻量调用；使用内存 HTTP transport。"""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.services import cloud_models
from app.services.cloud_models import CloudModelError


class ShortChatTests(unittest.TestCase):
    def setUp(self):
        self.messages = [SystemMessage(content="仅补全检索问题"), HumanMessage(content="那未成年人呢？")]
        self.client_class = httpx.AsyncClient
        self.requests = []

    def client(self, handler):
        def create(**kwargs):
            return self.client_class(transport=httpx.MockTransport(handler), **kwargs)
        return create

    def test_cloud_request_has_small_output_no_thinking_and_no_retry_path(self):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "补全结果"}}]})
        with patch.object(settings, "LLM_PROVIDER", "cloud"), patch.object(settings, "LLM_MODEL", "Qwen3-test"), \
             patch.object(settings, "LLM_API_KEY", "test-only-key"), patch.object(settings, "LLM_BASE_URL", "https://example.test/v1/"), \
             patch.object(httpx, "AsyncClient", side_effect=self.client(handle)), patch.object(cloud_models, "_post") as normal_request:
            result = cloud_models.complete_short_chat(self.messages, timeout=1, max_tokens=64)
        self.assertEqual(result, "补全结果")
        normal_request.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(str(request.url), "https://example.test/v1/chat/completions")
        self.assertEqual(request.headers["Authorization"], "Bearer test-only-key")
        payload = json.loads(request.content)
        self.assertEqual(payload["temperature"], 0)
        self.assertEqual(payload["max_tokens"], 64)
        self.assertIs(payload["stream"], True)
        self.assertIs(payload["enable_thinking"], False)
        self.assertEqual([item["role"] for item in payload["messages"]], ["system", "user"])

    def test_deepseek_followup_uses_disabled_reasoning_and_ignores_reasoning_frames(self):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
                'data: {"choices":[{"delta":{"reasoning_content":"不应进入检索问题"}}]}\n\n'
                'data: {"choices":[{"delta":{"content":"补全结果"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            ))
        with patch.object(settings, "LLM_PROVIDER", "cloud"), patch.object(settings, "LLM_MODEL", "deepseek-flash"), \
             patch.object(settings, "LLM_BASE_URL", "https://api.deepseek.com"), \
             patch.object(settings, "LLM_API_KEY", "test-only-key"), patch.object(settings, "LLM_REASONING_EFFORT", "none"), \
             patch.object(httpx, "AsyncClient", side_effect=self.client(handle)), patch.object(cloud_models, "_post") as normal_request:
            self.assertEqual(cloud_models.complete_short_chat(self.messages, timeout=1, max_tokens=64), "补全结果")
        normal_request.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        payload = json.loads(self.requests[0].content)
        self.assertEqual(str(self.requests[0].url), "https://api.deepseek.com/chat/completions")
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["max_tokens"], 64)
        self.assertEqual(payload["temperature"], 0)
        self.assertIs(payload["stream"], True)
        self.assertNotIn("enable_thinking", payload)

    def test_ollama_uses_same_context_window_without_cloud_auth(self):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, json={"message": {"content": "本地补全结果"}})
        with patch.object(settings, "LLM_PROVIDER", "ollama"), patch.object(settings, "OLLAMA_BASE_URL", "http://localhost:11434/"), \
             patch.object(settings, "LLM_REASONING_EFFORT", "none"), patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 8192), \
             patch.object(httpx, "AsyncClient", side_effect=self.client(handle)):
            self.assertEqual(cloud_models.complete_short_chat(self.messages, timeout=1, max_tokens=48), "本地补全结果")
        request = self.requests[0]
        self.assertEqual(str(request.url), "http://localhost:11434/api/chat")
        self.assertNotIn("Authorization", request.headers)
        payload = json.loads(request.content)
        self.assertIs(payload["think"], False)
        self.assertNotIn("reasoning_effort", payload)
        self.assertEqual(payload["options"], {"temperature": 0, "num_predict": 48, "num_ctx": 8192})

    def test_error_statuses_are_not_retried(self):
        for status in [401, 429, 503]:
            self.requests.clear()
            def handle(request):
                self.requests.append(request)
                return httpx.Response(status, json={"error": "simulated upstream failure"})
            with self.subTest(status=status), patch.object(settings, "LLM_PROVIDER", "ollama"), \
                 patch.object(httpx, "AsyncClient", side_effect=self.client(handle)):
                with self.assertRaises(httpx.HTTPStatusError):
                    cloud_models.complete_short_chat(self.messages, timeout=1, max_tokens=64)
                self.assertEqual(len(self.requests), 1)

    def test_total_deadline_cancels_a_stalled_request_and_closes_transport(self):
        cancelled = []
        async def handle(request):
            self.requests.append(request)
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        with patch.object(settings, "LLM_PROVIDER", "ollama"), patch.object(httpx, "AsyncClient", side_effect=self.client(handle)):
            with self.assertRaises(TimeoutError):
                cloud_models.complete_short_chat(self.messages, timeout=0.03, max_tokens=64)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(cancelled, [True])

    def test_total_deadline_also_covers_reading_a_response_body(self):
        closed = []
        class StalledBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"message":'
                await asyncio.Event().wait()
            async def aclose(self):
                closed.append(True)
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, stream=StalledBody())
        with patch.object(settings, "LLM_PROVIDER", "ollama"), patch.object(httpx, "AsyncClient", side_effect=self.client(handle)):
            with self.assertRaises(TimeoutError):
                cloud_models.complete_short_chat(self.messages, timeout=0.03, max_tokens=64)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(closed, [True])

    def test_cloud_streaming_is_consumed_once_and_stops_at_the_terminal_frame(self):
        closed = []
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\r\n\r\n'
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\r\n\r\n'
                await asyncio.Event().wait()  # 已有终止帧，不必等待网关关闭连接。
            async def aclose(self):
                closed.append(True)
        def handle(request):
            self.requests.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())
        with patch.object(settings, "LLM_PROVIDER", "cloud"), patch.object(settings, "LLM_API_KEY", "test-key"), \
             patch.object(httpx, "AsyncClient", side_effect=self.client(handle)):
            self.assertEqual(cloud_models.complete_short_chat(self.messages, timeout=0.5, max_tokens=64), "OK")
        self.assertEqual(len(self.requests), 1)
        self.assertIs(json.loads(self.requests[0].content)["stream"], True)
        self.assertEqual(closed, [True])

    def test_unexpected_cloud_stream_requires_a_terminal_frame(self):
        def handle(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')
        with patch.object(settings, "LLM_PROVIDER", "cloud"), patch.object(settings, "LLM_API_KEY", "test-key"), \
             patch.object(httpx, "AsyncClient", side_effect=self.client(handle)):
            with self.assertRaises(CloudModelError):
                cloud_models.complete_short_chat(self.messages, timeout=1, max_tokens=64)

    def test_non_string_content_and_malformed_json_raise_for_planner_fallback(self):
        for data in [{"message": {"content": ["invalid"]}}, {"message": {"content": None}}]:
            with self.subTest(data=data), patch.object(settings, "LLM_PROVIDER", "ollama"), \
                 patch.object(httpx, "AsyncClient", side_effect=self.client(lambda request: httpx.Response(200, json=data))):
                with self.assertRaises(CloudModelError):
                    cloud_models.complete_short_chat(self.messages, timeout=1, max_tokens=64)
        with patch.object(settings, "LLM_PROVIDER", "ollama"), \
             patch.object(httpx, "AsyncClient", side_effect=self.client(lambda request: httpx.Response(200, text="not json"))):
            with self.assertRaises(ValueError):
                cloud_models.complete_short_chat(self.messages, timeout=1, max_tokens=64)
