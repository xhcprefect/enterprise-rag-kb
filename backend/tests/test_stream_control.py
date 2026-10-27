"""流式生命周期的离线回归：内容截止时间独立于心跳，取消与背压有界。"""
import asyncio
import time
import unittest
from unittest.mock import MagicMock, patch

from starlette.requests import ClientDisconnect

from app.api.v1.chat import ManagedChatResponse
from app.core.config import settings
from app.services.stream_control import StreamEvent, StreamFault, StreamState, active_streams, stream_events


class StreamControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, value in {
            "CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS": 0.08,
            "CHAT_STREAM_IDLE_TIMEOUT_SECONDS": 0.04,
            "CHAT_STREAM_TOTAL_TIMEOUT_SECONDS": 0.25,
            "CHAT_STREAM_HEARTBEAT_SECONDS": 0.01,
            "CHAT_STREAM_CANCEL_POLL_SECONDS": 0.01,
        }.items():
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def events(self, source, **kwargs):
        return stream_events(source, state=kwargs.pop("state", StreamState()), user_id="user", request_id="request", **kwargs)

    async def test_heartbeat_does_not_extend_first_body_deadline_and_cancels_read(self):
        closed = asyncio.Event()
        async def source():
            try:
                yield StreamEvent("meta", {})
                await asyncio.sleep(10)
            finally:
                closed.set()
        frames = []
        started = time.monotonic()
        with self.assertRaises(StreamFault) as fault:
            async for frame in self.events(source()):
                frames.append(frame)
        self.assertEqual(fault.exception.code, "first_token_timeout")
        self.assertTrue(any(frame == ": ping\n\n" for frame in frames))
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(closed.is_set())

    async def test_status_and_whitespace_do_not_count_as_first_body(self):
        async def source():
            while True:
                yield StreamEvent("status", {"phase": "retrieving"})
                yield StreamEvent("delta", {"content": " "})
                await asyncio.sleep(0.005)
        with self.assertRaises(StreamFault) as fault:
            async for _ in self.events(source()):
                pass
        self.assertEqual(fault.exception.code, "first_token_timeout")

    async def test_heartbeat_does_not_extend_idle_deadline(self):
        async def source():
            yield StreamEvent("delta", {"content": "正文"})
            await asyncio.sleep(10)
        with self.assertRaises(StreamFault) as fault:
            async for _ in self.events(source()):
                pass
        self.assertEqual(fault.exception.code, "idle_timeout")

    async def test_real_content_keeps_idle_alive_but_not_total_deadline(self):
        async def source():
            while True:
                yield StreamEvent("delta", {"content": "文"})
                await asyncio.sleep(0.005)
        with patch.object(settings, "CHAT_STREAM_TOTAL_TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(StreamFault) as fault:
                async for _ in self.events(source()):
                    pass
        self.assertEqual(fault.exception.code, "total_timeout")

    async def test_done_stops_without_waiting_for_eof_and_closes_source(self):
        closed = asyncio.Event()
        async def source():
            try:
                yield StreamEvent("delta", {"content": "ok"})
                yield StreamEvent("done", {"citations": [], "latency_ms": 1})
                await asyncio.sleep(10)
            finally:
                closed.set()
        frames = [frame async for frame in self.events(source())]
        self.assertEqual(len(frames), 2)
        self.assertIn("event: done", frames[-1])
        self.assertTrue(closed.is_set())

    async def test_no_terminal_is_interruption(self):
        async def source():
            yield StreamEvent("delta", {"content": "partial"})
        with self.assertRaises(StreamFault) as fault:
            async for _ in self.events(source()):
                pass
        self.assertEqual(fault.exception.code, "stream_interrupted")

    async def test_natural_backpressure_does_not_run_producer_ahead(self):
        read = []
        async def source():
            for number in range(100):
                read.append(number)
                yield StreamEvent("delta", {"content": str(number)})
        controlled = self.events(source())
        await anext(controlled)
        await asyncio.sleep(0.02)
        self.assertEqual(read, [0])
        await controlled.aclose()

    async def test_cancel_interrupts_stalled_model_read(self):
        reading, closed = asyncio.Event(), asyncio.Event()
        async def source():
            try:
                yield StreamEvent("meta", {})
                reading.set()
                await asyncio.sleep(10)
            finally:
                closed.set()
        controlled = self.events(source())
        await anext(controlled)
        async def consume():
            return [frame async for frame in controlled]
        task = asyncio.create_task(consume())
        await reading.wait()
        active_streams.cancel("user", "request")
        with self.assertRaises(StreamFault) as fault:
            await asyncio.wait_for(task, 0.5)
        self.assertEqual(fault.exception.code, "cancelled")
        self.assertTrue(closed.is_set())

    async def test_cross_process_cancellation_polls_database_without_waiting_for_model(self):
        checks = []
        closed = asyncio.Event()
        async def lease(renew):
            checks.append(renew)
            return len(checks) < 3
        async def source():
            try:
                yield StreamEvent("meta", {})
                await asyncio.sleep(10)
            finally:
                closed.set()
        with self.assertRaises(StreamFault) as fault:
            async for _ in self.events(source(), check_lease=lease):
                pass
        self.assertEqual(fault.exception.code, "lease_lost")
        self.assertEqual(checks[0], False)
        self.assertTrue(closed.is_set())

    async def test_cancel_committed_before_iteration_never_starts_model(self):
        started = []
        async def source():
            started.append(True)
            yield StreamEvent("delta", {"content": "unexpected"})
        async def lease(_renew):
            return False
        with self.assertRaises(StreamFault) as fault:
            await anext(self.events(source(), check_lease=lease))
        self.assertEqual(fault.exception.code, "cancelled")
        self.assertEqual(started, [])

    async def test_initial_lease_check_is_also_bounded(self):
        async def source():
            yield StreamEvent("delta", {"content": "unexpected"})
        async def lease(_renew):
            await asyncio.sleep(10)
        with self.assertRaises(StreamFault) as fault:
            await anext(self.events(source(), check_lease=lease))
        self.assertEqual(fault.exception.code, "first_token_timeout")

    async def test_invalid_content_field_is_rejected(self):
        async def source():
            yield StreamEvent("delta", {"content": 123})
        with self.assertRaises(StreamFault) as fault:
            await anext(self.events(source()))
        self.assertEqual(fault.exception.code, "invalid_stream")

    async def test_answer_limit_is_cumulative(self):
        async def source():
            yield StreamEvent("delta", {"content": "abcd"})
            yield StreamEvent("delta", {"content": "ef"})
        with patch.object(settings, "CHAT_STREAM_MAX_ANSWER_CHARS", 5):
            with self.assertRaises(StreamFault) as fault:
                async for _ in self.events(source()):
                    pass
        self.assertEqual(fault.exception.code, "stream_limit")

    async def test_frame_limit_counts_utf8_bytes_not_characters(self):
        async def source():
            yield StreamEvent("delta", {"content": "文" * 40})
        with patch.object(settings, "CHAT_STREAM_MAX_FRAME_BYTES", 100):
            with self.assertRaises(StreamFault) as fault:
                await anext(self.events(source()))
        self.assertEqual(fault.exception.code, "stream_limit")

    async def test_first_token_metric_uses_visible_body_and_is_stable(self):
        state = StreamState(started_at=time.monotonic() - 0.01)
        state.observe(StreamEvent("meta", {}))
        state.observe(StreamEvent("delta", {"content": " "}))
        self.assertIsNone(state.first_token_ms)
        state.observe(StreamEvent("delta", {"content": "中"}))
        first = state.first_token_ms
        self.assertGreaterEqual(first, 10)
        state.observe(StreamEvent("delta", {"content": "文"}))
        self.assertEqual(state.first_token_ms, first)

    async def test_slow_client_send_is_bounded_and_closes_generator(self):
        closed = asyncio.Event()
        async def source():
            try:
                yield "event: meta\ndata: {}\n\n"
                await asyncio.sleep(10)
            finally:
                closed.set()
        async def send(message):
            if message["type"] == "http.response.body":
                await asyncio.sleep(10)
        async def receive():
            await asyncio.sleep(10)
        cleanup = MagicMock()
        response = ManagedChatResponse(source(), cleanup)
        with patch.object(settings, "CHAT_STREAM_SEND_TIMEOUT_SECONDS", 0.02):
            with self.assertRaises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertTrue(closed.is_set())
        cleanup.assert_called_once()

    async def test_one_hundred_waiting_streams_leave_event_loop_responsive(self):
        async def source():
            yield StreamEvent("meta", {})
            await asyncio.sleep(0.08)
            yield StreamEvent("delta", {"content": "ok"})
            yield StreamEvent("done", {"citations": [], "latency_ms": 80})
        async def consume(number):
            return [frame async for frame in stream_events(source(), state=StreamState(),
                        user_id=str(number), request_id=str(number))]
        ticks = []
        async def ordinary_work():
            for _ in range(4):
                await asyncio.sleep(0.005)
                ticks.append(time.monotonic())
        with patch.object(settings, "CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", 2), \
             patch.object(settings, "CHAT_STREAM_IDLE_TIMEOUT_SECONDS", 2), \
             patch.object(settings, "CHAT_STREAM_TOTAL_TIMEOUT_SECONDS", 4):
            results = await asyncio.gather(*(consume(number) for number in range(100)), ordinary_work())
        self.assertEqual(len(ticks), 4)
        self.assertTrue(all("event: done" in result[-1] for result in results[:-1]))

    async def test_cancelling_consumer_cleans_up_all_child_tasks(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def source():
            try:
                entered.set()
                await asyncio.sleep(10)
                yield StreamEvent("done", {})
            finally:
                closed.set()
        async def consume():
            async for _ in self.events(source()):
                pass
        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(closed.is_set())
        await asyncio.sleep(0)
        others = [item for item in asyncio.all_tasks() if item is not asyncio.current_task() and not item.done()]
        self.assertEqual(others, [])
