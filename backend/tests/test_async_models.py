"""Native asynchronous adapters: synthetic transports only, no paid or external model calls."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import HumanMessage

from app.core.config import settings
from app.services import async_models as adapters
from app.services.cloud_models import CloudModelError, OpenAICompatibleChat, _Endpoint
from app.services.stream_control import StreamFault
from app.services.usage_tracking import ModelCall as RealCall


class RecordedCall:
    calls = []
    def __init__(self, kind, provider, model, **_kwargs):
        self.kind, self.enabled, self.records = kind, True, []
        self.usage = {"input_tokens": None, "output_tokens": None, "cached_input_tokens": None}
        self.finished = False
        self.calls.append(self)
    observe = RealCall.observe
    def finish(self, status="succeeded"):
        if not self.finished:
            self.finished = True
            self.records.append((status, dict(self.usage)))
    def retry(self):
        self.finish("failed")
        self.finished = False
        self.usage = {"input_tokens": None, "output_tokens": None, "cached_input_tokens": None}


class Bytes(httpx.AsyncByteStream):
    def __init__(self, chunks, *, stall=False, failure=False, heartbeats=False):
        self.chunks, self.stall, self.failure, self.heartbeats = chunks, stall, failure, heartbeats
        self.closed = False
        self.waiting = asyncio.Event()
    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.waiting.set()
        if self.heartbeats:
            while True:
                await asyncio.sleep(0.005)
                yield b": ping\n\n"
        if self.stall:
            await asyncio.sleep(10)
        if self.failure:
            raise httpx.ReadError("synthetic disconnection")
    async def aclose(self):
        self.closed = True


def frame(event):
    return ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()


def text(value, reason=None, usage=None):
    event = {"choices": [{"delta": {"content": value}, "finish_reason": reason}]}
    if usage is not None:
        event["usage"] = usage
    return frame(event)


class AsyncModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        RecordedCall.calls = []
        self.model = OpenAICompatibleChat(_Endpoint("https://model.invalid/v1", "synthetic-key", 1), "synthetic-chat", 0)
        self.clients = []
        for patcher in (patch.object(adapters, "ModelCall", RecordedCall),
                        patch.object(settings, "LLM_STREAM_USAGE_GRACE_SECONDS", 0.03),
                        patch.object(settings, "LLM_STREAM_INCLUDE_USAGE", True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        await adapters.close_async_clients()

    def transport(self, stream, handler=None):
        calls = []
        def handle(request):
            calls.append(request)
            return handler(request, len(calls)) if handler else httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.clients.append(client)
        patcher = patch.object(adapters, "shared_async_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    async def consume(self):
        return [chunk async for chunk in self.model.astream([HumanMessage(content="synthetic")])]

    async def test_normal_text_terminal_and_late_usage_close_response(self):
        stream = Bytes([text("中文"), text("", "stop"), frame({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}})], stall=True)
        calls = self.transport(stream)
        chunks = await self.consume()
        self.assertEqual("".join(chunk.content for chunk in chunks), "中文")
        self.assertEqual(chunks[-1].usage_metadata["total_tokens"], 25)
        self.assertTrue(stream.closed)
        self.assertEqual(RecordedCall.calls[0].records[0][0], "succeeded")
        self.assertEqual(len(calls), 1)

    async def test_finish_reason_without_eof_has_bounded_usage_grace(self):
        stream = Bytes([text("complete", "stop")], stall=True)
        self.transport(stream)
        started = time.monotonic()
        chunks = await self.consume()
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(chunks[0].response_metadata["finish_reason"], "stop")
        self.assertTrue(stream.closed)
        self.assertIsNone(RecordedCall.calls[0].usage["input_tokens"])
        self.assertEqual(RecordedCall.calls[0].records[0][0], "succeeded")

    async def test_broken_usage_tail_cannot_invalidate_finished_text(self):
        for malformed in (False, True):
            stream = Bytes([text("complete", "stop"), *([b"data: not-json\n\n"] if malformed else [])], failure=not malformed)
            self.transport(stream)
            chunks = await self.consume()
            self.assertEqual("".join(chunk.content for chunk in chunks), "complete")
            self.assertEqual(RecordedCall.calls[-1].records[-1][0], "succeeded")
            self.assertTrue(stream.closed)

    async def test_tail_heartbeats_do_not_renew_usage_grace(self):
        stream = Bytes([text("complete", "stop")], heartbeats=True)
        self.transport(stream)
        started = time.monotonic()
        await self.consume()
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertTrue(stream.closed)

    async def test_usage_before_finish_and_duplicate_counts_are_not_added(self):
        usage = {"prompt_tokens": 20, "completion_tokens": 5}
        stream = Bytes([text("ok", usage=usage), frame({"choices": [], "usage": usage}), text("", "stop")], stall=True)
        self.transport(stream)
        await self.consume()
        self.assertEqual(RecordedCall.calls[0].usage["input_tokens"], 20)
        self.assertEqual(RecordedCall.calls[0].usage["output_tokens"], 5)
        self.assertTrue(stream.closed)

    async def test_length_is_a_successful_but_explicitly_truncated_answer(self):
        stream = Bytes([text("partial", "length")])
        self.transport(stream)
        chunks = await self.consume()
        self.assertEqual(chunks[0].content, "partial")
        self.assertEqual(chunks[0].response_metadata["finish_reason"], "length")
        self.assertEqual(RecordedCall.calls[0].records[-1][0], "succeeded")

    async def test_content_filter_is_not_a_success(self):
        stream = Bytes([text("partial"), text("", "content_filter")])
        self.transport(stream)
        with self.assertRaises(CloudModelError):
            await self.consume()
        self.assertTrue(stream.closed)
        self.assertEqual(RecordedCall.calls[0].records[-1][0], "failed")

    async def test_eof_without_terminal_is_not_a_success(self):
        stream = Bytes([text("partial")])
        self.transport(stream)
        with self.assertRaises(CloudModelError):
            await self.consume()
        self.assertTrue(stream.closed)

    async def test_done_marker_ends_without_eof(self):
        stream = Bytes([text("ok"), b"data: [DONE]\n\n"], stall=True)
        self.transport(stream)
        chunks = await asyncio.wait_for(self.consume(), 0.5)
        self.assertEqual("".join(chunk.content for chunk in chunks), "ok")
        self.assertTrue(stream.closed)

    async def test_malformed_fields_fail_closed(self):
        for event in ({"choices": 1}, {"choices": [1]}, {"choices": [{"delta": {"content": []}}]},
                      {"choices": [{"delta": {}, "finish_reason": 2}]}, {"error": {"message": "error"}}, [1]):
            with self.subTest(event=event):
                stream = Bytes([frame(event)])
                self.transport(stream)
                with self.assertRaises(CloudModelError):
                    await self.consume()
                self.assertTrue(stream.closed)

    async def test_unterminated_giant_line_is_bounded_before_decoding(self):
        stream = Bytes([b"data: " + b"x" * 10000], stall=True)
        self.transport(stream)
        with patch.object(settings, "CHAT_STREAM_MAX_FRAME_BYTES", 1024), self.assertRaises(StreamFault) as fault:
            await self.consume()
        self.assertEqual(fault.exception.code, "stream_limit")
        self.assertTrue(stream.closed)

    async def test_utf8_at_every_byte_and_crlf_multi_data_lines(self):
        payload = 'data: {"choices":\r\ndata: [{"delta":{"content":"中文🙂"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'.encode()
        stream = Bytes([payload[index:index + 1] for index in range(len(payload))])
        self.transport(stream)
        chunks = await self.consume()
        self.assertEqual("".join(chunk.content for chunk in chunks), "中文🙂")

    async def test_pending_read_cancel_closes_response_and_preserves_actual_usage(self):
        stream = Bytes([text("partial", usage={"prompt_tokens": 12, "completion_tokens": 3})], stall=True)
        self.transport(stream)
        source = self.model.astream([])
        await anext(source)
        task = asyncio.create_task(anext(source))
        await stream.waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)
        self.assertEqual(RecordedCall.calls[0].records[-1], ("cancelled", {
            "input_tokens": 12, "output_tokens": 3, "cached_input_tokens": None}))

    async def test_generator_close_closes_only_response_not_shared_client(self):
        stream = Bytes([text("partial")], stall=True)
        self.transport(stream)
        source = self.model.astream([])
        await anext(source)
        await source.aclose()
        self.assertTrue(stream.closed)
        self.assertFalse(self.clients[0].is_closed)
        self.assertEqual(RecordedCall.calls[0].records[-1][0], "cancelled")

    async def test_accepted_stream_read_error_is_never_retried(self):
        stream = Bytes([text("partial")], failure=True)
        calls = self.transport(stream)
        with self.assertRaises(httpx.ReadError):
            await self.consume()
        self.assertEqual(len(calls), 1)
        self.assertTrue(stream.closed)

    async def test_http_retry_is_async_and_accounts_for_each_attempt(self):
        stream = Bytes([text("ok", "stop")])
        calls = self.transport(stream, lambda request, number: httpx.Response(429, text='{"error":{"message":"busy"}}')
                               if number == 1 else httpx.Response(200, stream=stream))
        with patch.object(adapters.asyncio, "sleep", new=AsyncMock()):
            await self.consume()
        self.assertEqual(len(calls), 2)
        self.assertEqual([status for status, _ in RecordedCall.calls[0].records], ["failed", "succeeded"])

    async def test_pool_is_shared_bounded_and_closed_on_shutdown(self):
        first, second = adapters.shared_async_client(), adapters.shared_async_client()
        self.assertIs(first, second)
        self.assertEqual(first._transport._pool._max_connections, settings.LLM_ASYNC_MAX_CONNECTIONS)
        await adapters.close_async_clients()
        self.assertTrue(first.is_closed)
        third = adapters.shared_async_client()
        self.assertIsNot(first, third)

    async def test_ollama_keeps_options_usage_and_length_reason(self):
        stream = Bytes([
            '{"message":{"content":"中文","thinking":"hidden"},"done":false}\n'.encode(),
            b'{"message":{"content":""},"done":true,"done_reason":"length","prompt_eval_count":10,"eval_count":2}\n',
        ], stall=True)
        calls = self.transport(stream)
        chunks = [chunk async for chunk in adapters.AsyncOllamaChat().astream([])]
        self.assertEqual("".join(chunk.content for chunk in chunks), "中文")
        self.assertEqual(chunks[-1].response_metadata["finish_reason"], "length")
        self.assertEqual(chunks[-1].usage_metadata["total_tokens"], 12)
        payload = json.loads(calls[0].content)
        self.assertIs(payload["think"], False)
        self.assertEqual(payload["options"]["num_ctx"], settings.LLM_CONTEXT_WINDOW_TOKENS)
        self.assertTrue(stream.closed)

    async def test_async_rag_filter_and_finish_metadata_are_preserved(self):
        from app.services import rag
        stream = Bytes([text("<thi"), text("nk>hidden</think>中文"), text("", "length")])
        self.transport(stream)
        metrics = {}
        with patch.object(rag, "get_stream_chat", return_value=self.model):
            answer = "".join([part async for part in rag.answer_astream("synthetic", [], [], mode="chat", metrics=metrics)])
        self.assertEqual(answer, "中文")
        self.assertEqual(metrics["finish_reason"], "length")
