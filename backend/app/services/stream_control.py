"""Bounded SSE lifecycle: one pending read, independent content deadlines, no per-stream waiting thread."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

import anyio

from app.core.config import settings

logger = logging.getLogger(__name__)


class StreamFault(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class StreamEvent:
    name: str
    data: dict


def encode_event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def public_limits() -> dict:
    return {
        "first_token_timeout_ms": round(settings.CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS * 1000),
        "idle_timeout_ms": round(settings.CHAT_STREAM_IDLE_TIMEOUT_SECONDS * 1000),
        "total_timeout_ms": round(settings.CHAT_STREAM_TOTAL_TIMEOUT_SECONDS * 1000),
        "heartbeat_interval_ms": round(settings.CHAT_STREAM_HEARTBEAT_SECONDS * 1000),
        "max_answer_chars": settings.CHAT_STREAM_MAX_ANSWER_CHARS,
    }


@dataclass
class StreamState:
    started_at: float = field(default_factory=time.monotonic)
    first_token_ms: int | None = None
    last_content_at: float | None = None
    answer_chars: int = 0

    def observe(self, event: StreamEvent) -> None:
        if event.name != "delta":
            return
        text = event.data.get("content")
        if not isinstance(text, str):
            raise StreamFault("invalid_stream", "模型返回的正文格式异常")
        self.answer_chars += len(text)
        if self.answer_chars > settings.CHAT_STREAM_MAX_ANSWER_CHARS:
            raise StreamFault("stream_limit", "模型回答超过允许的长度，请缩小问题范围")
        if text:
            now = time.monotonic()
            if self.first_token_ms is None and text.strip():
                self.first_token_ms = max(0, round((now - self.started_at) * 1000))
            if self.first_token_ms is not None:
                self.last_content_at = now

    def remaining(self) -> float:
        now = time.monotonic()
        total = settings.CHAT_STREAM_TOTAL_TIMEOUT_SECONDS - (now - self.started_at)
        if total <= 0:
            raise StreamFault("total_timeout", "本轮回答超过总处理时限，请稍后重试")
        if self.first_token_ms is None:
            first = settings.CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS - (now - self.started_at)
            if first <= 0:
                raise StreamFault("first_token_timeout", "等待回答正文超时，请稍后重试")
            return min(total, first)
        idle = settings.CHAT_STREAM_IDLE_TIMEOUT_SECONDS - (now - self.last_content_at)
        if idle <= 0:
            raise StreamFault("idle_timeout", "回答正文长时间未更新，连接已结束")
        return min(total, idle)


@dataclass
class _Active:
    loop: asyncio.AbstractEventLoop
    stopped: asyncio.Event
    reason: str = "cancelled"

    def stop(self, reason: str = "cancelled") -> None:
        self.reason = reason
        self.stopped.set()


class ActiveStreams:
    """Local fast cancellation; the database remains authoritative across processes."""
    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], _Active] = {}

    def add(self, user_id, request_id, active: _Active) -> None:
        with self._lock:
            self._items[(str(user_id), str(request_id))] = active

    def discard(self, user_id, request_id, active: _Active) -> None:
        key = (str(user_id), str(request_id))
        with self._lock:
            if self._items.get(key) is active:
                del self._items[key]

    def cancel(self, user_id, request_id) -> None:
        with self._lock:
            active = self._items.get((str(user_id), str(request_id)))
        if active is not None and not active.loop.is_closed():
            try:
                active.loop.call_soon_threadsafe(active.stop)
            except RuntimeError:
                pass  # The owning loop completed concurrently.


active_streams = ActiveStreams()


async def stream_events(
    source: AsyncIterator[StreamEvent], *, state: StreamState, user_id, request_id,
    check_lease: Callable[[bool], Awaitable[bool]] | None = None,
) -> AsyncIterator[str]:
    """Natural backpressure: at most one source next() in flight, no unbounded producer queue.

    Heartbeats and lease polling run while an upstream read or retrieval is pending.
    Neither heartbeat bytes nor status events extend first-content/idle/overall deadlines.
    """
    active = _Active(asyncio.get_running_loop(), asyncio.Event())
    active_streams.add(user_id, request_id, active)
    pending = watcher = stopper = None
    heartbeat_at = time.monotonic() + settings.CHAT_STREAM_HEARTBEAT_SECONDS
    sentinel = object()

    async def watch_lease():
        renewed_at = 0.0
        renew_every = max(1.0, min(30.0, settings.CHAT_LEASE_SECONDS / 3))
        while True:
            now = time.monotonic()
            renew = now - renewed_at >= renew_every
            try:
                valid = await check_lease(renew)
                if not valid:
                    active.stop("lease_lost")
                    return
                if renew:
                    renewed_at = now
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A transient database outage does not fabricate a successful lease.
                # The bounded overall deadline and completion fencing still apply.
                logger.warning("检查问答租约失败: %s", type(exc).__name__)
            await asyncio.sleep(settings.CHAT_STREAM_CANCEL_POLL_SECONDS)

    try:
        if check_lease is not None:
            # Reject a cancellation which committed before this response began iterating.
            try:
                valid = await asyncio.wait_for(check_lease(False), timeout=state.remaining())
            except TimeoutError:
                state.remaining()  # raises the appropriate first/overall deadline code
                raise StreamFault("first_token_timeout", "等待请求状态超时，请稍后重试")
            if not valid:
                raise StreamFault("cancelled", "该回答已停止")
            watcher = asyncio.create_task(watch_lease())
        stopper = asyncio.create_task(active.stopped.wait())
        while True:
            remaining = state.remaining()
            if active.stopped.is_set():
                code = active.reason
                raise StreamFault(code, "该回答已停止或处理权限已失效")
            if pending is None:
                pending = asyncio.create_task(anext(source, sentinel))
            timeout = min(remaining, max(0.0, heartbeat_at - time.monotonic()))
            ready, _ = await asyncio.wait((pending, stopper), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if stopper in ready:
                raise StreamFault(active.reason, "该回答已停止或处理权限已失效")
            # Check the deadline even if bytes arrive exactly at its boundary.
            state.remaining()
            if pending in ready:
                event = pending.result()
                pending = None
                if event is sentinel:
                    raise StreamFault("stream_interrupted", "回答连接提前中断，请重新读取请求结果")
                if not isinstance(event, StreamEvent) or event.name not in {"meta", "status", "delta", "done", "error"} or not isinstance(event.data, dict):
                    raise StreamFault("invalid_stream", "流式事件格式异常")
                state.observe(event)
                frame = encode_event(event.name, event.data)
                if len(frame.encode("utf-8")) > settings.CHAT_STREAM_MAX_FRAME_BYTES:
                    raise StreamFault("stream_limit", "流式事件超过允许的大小")
                yield frame
                heartbeat_at = time.monotonic() + settings.CHAT_STREAM_HEARTBEAT_SECONDS
                if event.name in {"done", "error"}:
                    return
            elif time.monotonic() >= heartbeat_at:
                yield ": ping\n\n"
                heartbeat_at = time.monotonic() + settings.CHAT_STREAM_HEARTBEAT_SECONDS
    finally:
        active_streams.discard(user_id, request_id, active)
        # Shield cleanup against ASGI disconnect cancellation. In particular, cancel
        # an awaiting model read before closing its generator and releasing admission.
        with anyio.CancelScope(shield=True):
            tasks = [task for task in (pending, watcher, stopper) if task is not None]
            for task in tasks:
                task.cancel()
            for task in tasks:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            with suppress(asyncio.CancelledError, Exception):
                await source.aclose()
