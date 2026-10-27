"""Native asynchronous model streams with bounded parsing, pooled connections and prompt cancellation."""
from __future__ import annotations

import asyncio
import json
import threading
import weakref
from collections.abc import AsyncIterator
from contextlib import suppress

import anyio
import httpx
from langchain_core.messages import AIMessageChunk, BaseMessage

from app.core.config import settings
from app.services.cloud_models import CloudModelError, _Endpoint, _RETRY_STATUS, _format_error, _to_openai_message
from app.services.stream_control import StreamFault
from app.services.usage_tracking import ModelCall, UsageContext

# One bounded pool per event loop, shared across providers; credentials stay on requests.
# Different TestClient/asyncio.run loops must never reuse a connection attached to another loop.
_clients: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_clients_lock = threading.Lock()


def shared_async_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    with _clients_lock:
        client = _clients.get(loop)
        if client is None or client.is_closed:
            maximum = settings.LLM_ASYNC_MAX_CONNECTIONS
            client = httpx.AsyncClient(limits=httpx.Limits(
                max_connections=maximum, max_keepalive_connections=min(maximum, 32), keepalive_expiry=30,
            ))
            _clients[loop] = client
        return client


async def close_async_clients() -> None:
    """Close this application's loop pool; never operate on another thread's live loop."""
    with _clients_lock:
        client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()


async def _finish(call: ModelCall, status: str) -> None:
    # ASGI cancellation must not discard already received actual usage.
    with anyio.CancelScope(shield=True):
        await anyio.to_thread.run_sync(lambda: call.finish(status))


async def _close(response: httpx.Response | None) -> None:
    if response is not None:
        with anyio.CancelScope(shield=True):
            with suppress(Exception):
                await response.aclose()


async def _small_error(response: httpx.Response) -> str:
    parts, size = [], 0
    async for chunk in response.aiter_bytes():
        take = chunk[:max(0, 4096 - size)]
        parts.append(take)
        size += len(take)
        if size >= 4096:
            break
    return b"".join(parts).decode("utf-8", "replace")


async def _open_response(endpoint: _Endpoint, path: str, payload: dict, call: ModelCall, *, headers: dict) -> httpx.Response:
    """Retry only before a successful stream is opened, never replay an accepted generation."""
    client = shared_async_client()
    for attempt in range(3):
        if attempt:
            await anyio.to_thread.run_sync(call.retry)
        response = None
        try:
            request = client.build_request("POST", endpoint.url(path), headers=headers, json=payload,
                                           timeout=httpx.Timeout(endpoint.timeout, connect=min(15, endpoint.timeout)))
            response = await client.send(request, stream=True)
            if response.status_code >= 400:
                body = await _small_error(response)
                error = CloudModelError(_format_error(endpoint.label, response.status_code, body))
                retry = response.status_code in _RETRY_STATUS and attempt < 2
                await _close(response)
                response = None
                if not retry:
                    raise error
                await _finish(call, "failed")
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            return response
        except httpx.RequestError as exc:
            await _close(response)
            if attempt == 2:
                raise CloudModelError(f"调用 {endpoint.label} 失败：连接超时或网络异常") from exc
            await _finish(call, "failed")
            await asyncio.sleep(1.5 * (attempt + 1))
        except BaseException:
            await _close(response)
            raise
    raise CloudModelError(f"调用 {endpoint.label} 失败")


async def bounded_lines(response: httpx.Response) -> AsyncIterator[str]:
    """Bound a raw line before UTF-8 decoding, including a line that never receives LF."""
    buffer = bytearray()
    limit = settings.CHAT_STREAM_MAX_FRAME_BYTES
    async for chunk in response.aiter_bytes():
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end
            if len(buffer) + stop - start > limit:
                raise StreamFault("stream_limit", "模型流式事件超过允许的大小")
            buffer.extend(chunk[start:stop])
            if end < 0:
                break
            try:
                yield bytes(buffer).rstrip(b"\r").decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise CloudModelError("模型服务返回了无效的 UTF-8 数据") from exc
            buffer.clear()
            start = end + 1
    if buffer:
        try:
            yield bytes(buffer).rstrip(b"\r").decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise CloudModelError("模型服务返回了无效的 UTF-8 数据") from exc


async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    lines, size = [], 0
    source = bounded_lines(response)
    try:
        async for line in source:
            if not line:
                if lines:
                    yield "\n".join(lines)
                lines, size = [], 0
            elif line.startswith(":"):
                continue
            elif line.startswith("data:"):
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                size += len(value.encode("utf-8")) + 1
                if size > settings.CHAT_STREAM_MAX_FRAME_BYTES:
                    raise StreamFault("stream_limit", "模型流式事件超过允许的大小")
                lines.append(value)
        if lines:
            yield "\n".join(lines)
    finally:
        await source.aclose()


def _object(value: str) -> dict:
    try:
        event = json.loads(value)
    except (ValueError, RecursionError) as exc:
        raise CloudModelError("模型服务返回了无效的流式数据") from exc
    if not isinstance(event, dict) or event.get("error"):
        raise CloudModelError("模型服务在生成过程中返回错误，请稍后重试")
    return event


def _usage_metadata(call: ModelCall) -> dict | None:
    incoming, outgoing = call.usage["input_tokens"], call.usage["output_tokens"]
    if incoming is None or outgoing is None:
        return None
    return {"input_tokens": incoming, "output_tokens": outgoing, "total_tokens": incoming + outgoing}


async def openai_stream(model, messages: list[BaseMessage], *, usage_context: UsageContext | None = None) -> AsyncIterator[AIMessageChunk]:
    call = await anyio.to_thread.run_sync(lambda: ModelCall("chat", model.endpoint.label, model.model, context=usage_context))
    response, frames = None, None
    status, terminal, finish_at, answer_chars = "failed", False, None, 0
    try:
        response = await _open_response(model.endpoint, "/chat/completions", model._payload(messages, True), call,
                                        headers=model.endpoint.headers())
        frames = sse_data(response)
        while True:
            try:
                if terminal:
                    if not settings.LLM_STREAM_INCLUDE_USAGE or _usage_metadata(call) is not None:
                        break
                    remaining = finish_at + settings.LLM_STREAM_USAGE_GRACE_SECONDS - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    value = await asyncio.wait_for(anext(frames), remaining)
                else:
                    value = await anext(frames)
            except StopAsyncIteration:
                break
            except (TimeoutError, httpx.RequestError, CloudModelError, StreamFault):
                if terminal:
                    break  # A failed usage tail cannot invalidate already finished text.
                raise
            if value.strip() == "[DONE]":
                terminal = True
                break
            if terminal:
                # After finish_reason only actual usage is relevant. Malformed/extra tail
                # data cannot append text or turn a completed answer into a late failure.
                try:
                    event = _object(value)
                except CloudModelError:
                    break
                call.observe(event.get("usage"))
                usage = _usage_metadata(call)
                if usage is not None:
                    yield AIMessageChunk(content="", usage_metadata=usage)
                continue
            event = _object(value)
            call.observe(event.get("usage"))
            choices = event.get("choices", [])
            if not isinstance(choices, list):
                raise CloudModelError("模型返回的 choices 格式异常")
            text, reason = "", None
            if choices:
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise CloudModelError("模型返回的 choice 格式异常")
                reason = choice.get("finish_reason")
                if reason is not None and not isinstance(reason, str):
                    raise CloudModelError("模型返回的结束原因格式异常")
                if reason == "content_filter":
                    raise CloudModelError("模型服务拒绝生成该回答")
                if reason not in {None, "stop", "length"}:
                    raise CloudModelError("模型返回了不支持的结束原因")
                delta = choice.get("delta", choice.get("message", {}))
                if not isinstance(delta, dict):
                    raise CloudModelError("模型返回的 delta 格式异常")
                text = delta.get("content")
                if text is None:
                    text = ""
                if not isinstance(text, str):
                    raise CloudModelError("模型返回的正文格式异常")
                if terminal and text:
                    raise CloudModelError("模型在正文结束后继续返回正文")
            answer_chars += len(text)
            if answer_chars > settings.CHAT_STREAM_MAX_ANSWER_CHARS:
                raise StreamFault("stream_limit", "模型回答超过允许的长度，请缩小问题范围")
            if reason is not None and not terminal:
                terminal, finish_at = True, asyncio.get_running_loop().time()
            metadata = {"finish_reason": reason} if reason is not None else {}
            usage = _usage_metadata(call) if isinstance(event.get("usage"), dict) else None
            if text or metadata or usage:
                yield AIMessageChunk(content=text, response_metadata=metadata, usage_metadata=usage)
        if not terminal:
            raise CloudModelError("模型响应提前中断，请重试")
        status = "succeeded"
    except (asyncio.CancelledError, GeneratorExit):
        status = "cancelled"
        raise
    finally:
        with anyio.CancelScope(shield=True):
            if frames is not None:
                with suppress(Exception):
                    await frames.aclose()
            await _close(response)
            await _finish(call, status)


class AsyncOllamaChat:
    async def astream(self, messages: list[BaseMessage], *, usage_context: UsageContext | None = None) -> AsyncIterator[AIMessageChunk]:
        endpoint = _Endpoint(settings.OLLAMA_BASE_URL, "", settings.LLM_TIMEOUT)
        call = await anyio.to_thread.run_sync(lambda: ModelCall("chat", "ollama", settings.LLM_MODEL, context=usage_context))
        response, lines = None, None
        status, terminal, answer_chars = "failed", False, 0
        payload = {"model": settings.LLM_MODEL, "messages": [_to_openai_message(message) for message in messages],
                   "stream": True, "think": False,
                   "options": {"temperature": settings.LLM_TEMPERATURE, "num_predict": settings.LLM_MAX_OUTPUT_TOKENS,
                               "num_ctx": settings.LLM_CONTEXT_WINDOW_TOKENS}}
        try:
            response = await _open_response(endpoint, "/api/chat", payload, call, headers={"Content-Type": "application/json"})
            lines = bounded_lines(response)
            async for line in lines:
                if not line.strip():
                    continue
                event = _object(line)
                done = event.get("done", False)
                if not isinstance(done, bool):
                    raise CloudModelError("Ollama 返回的结束状态格式异常")
                message = event.get("message", {})
                if not isinstance(message, dict):
                    raise CloudModelError("Ollama 返回的消息格式异常")
                text = message.get("content", "")
                if not isinstance(text, str):
                    raise CloudModelError("Ollama 返回的正文格式异常")
                answer_chars += len(text)
                if answer_chars > settings.CHAT_STREAM_MAX_ANSWER_CHARS:
                    raise StreamFault("stream_limit", "模型回答超过允许的长度，请缩小问题范围")
                call.observe({"input_tokens": event.get("prompt_eval_count"), "output_tokens": event.get("eval_count")})
                reason = event.get("done_reason", "stop") if done else None
                if reason is not None and not isinstance(reason, str):
                    raise CloudModelError("Ollama 返回的结束原因格式异常")
                reason = "length" if reason in {"length", "max_tokens"} else reason
                if reason not in {None, "stop", "length"}:
                    raise CloudModelError("Ollama 返回了不支持的结束原因")
                metadata = {"finish_reason": reason} if done else {}
                usage = _usage_metadata(call)
                if text or metadata or usage:
                    yield AIMessageChunk(content=text, response_metadata=metadata, usage_metadata=usage)
                if done:
                    terminal = True
                    break
            if not terminal:
                raise CloudModelError("Ollama 响应提前中断，请重试")
            status = "succeeded"
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        finally:
            with anyio.CancelScope(shield=True):
                if lines is not None:
                    with suppress(Exception):
                        await lines.aclose()
                await _close(response)
                await _finish(call, status)
