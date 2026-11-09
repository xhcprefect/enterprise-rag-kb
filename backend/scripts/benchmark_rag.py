#!/usr/bin/env python
"""有界、默认 dry-run 的 RAG 性能/费用测量 CLI。

默认只做本地配置/文件/计划校验，不发任何 HTTP；只有 ``--live`` 才创建 HTTP
客户端。它只测量本实例的真实计量快照，不是企业容量验收，也不是供应商账单。

退出码：0=测量完成（dry-run 为计划校验通过）；1=失败/预算停止；
2=输入或本地配置错误；3=结果未知（超时、成本覆盖不完整、协议不明）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.capacity_benchmark import (  # noqa: E402
    HARD_MAX_HTTP_REQUESTS,
    BenchmarkConfig,
    BenchmarkInputError,
    DocumentSpec,
    SseEvent,
    SseParser,
    SseProtocolError,
    load_config,
    percentile,
    validate_output_path,
)

SCHEMA = "rag-benchmark/v1"
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INPUT = 2
EXIT_UNKNOWN = 3

MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_TOKEN_CHARS = 8192
MAX_HTTP_STATUS_REPORT = 599


class _DeadlineExceeded(Exception):
    pass


class _RequestTimeout(Exception):
    pass


class _HttpBudgetExceeded(Exception):
    pass


class _RedirectDetected(Exception):
    pass


class _HttpStatus(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"http_{status}")


class _ProtocolFailure(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass
class _ChatSample:
    index: int
    stage_index: int
    request_id: str
    outcome: str
    http_status: int | None = None
    ttft_ms: float | None = None
    latency_ms: float | None = None
    bytes: int = 0
    events: int = 0
    content_chars: int = 0
    error: str | None = None
    sent: bool = False


@dataclass
class _UploadSample:
    index: int
    outcome: str
    document_id: str | None = None
    deduplicated: bool = False
    chunk_count: int = 0
    error: str | None = None
    sent: bool = False
    unconfirmed: bool = False


@dataclass
class _State:
    user_id: uuid.UUID
    started_wall: datetime
    window_start: datetime
    deadline: float
    budget: "_HttpBudget"
    work_issued: int = 0
    actual_network_requests: int = 0
    unconfirmed_uploads: int = 0
    issued_requests: list[tuple[uuid.UUID, uuid.UUID]] = field(default_factory=list)
    uploaded_document_ids: list[str] = field(default_factory=list)
    samples: list[_ChatSample] = field(default_factory=list)
    uploads: list[_UploadSample] = field(default_factory=list)
    cost_checks: list[dict] = field(default_factory=list)
    http_statuses: dict[str, int] = field(default_factory=dict)
    operations_before: dict | None = None
    operations_after: dict | None = None
    final_measurement_data: Any = None
    stopped: str | None = None
    deadline_hit: bool = False
    network_progress: dict | None = None


class _HttpBudget:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.maximum:
            raise _HttpBudgetExceeded
        self.used += 1


def _money(value: Decimal | None) -> str | None:
    return format(value, ".10f") if value is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _DeadlineExceeded
    return left


async def _wait(coro, *, run_deadline: float, request_deadline: float | None = None):
    now = time.monotonic()
    limit = run_deadline if request_deadline is None else min(run_deadline, request_deadline)
    left = limit - now
    if left <= 0:
        # 期限已过时不再 await；显式关闭协程，避免遗留“never awaited”的
        # 客户端发送/读取协程，也不让调用方误以为请求仍在后台进行。
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        if run_deadline <= now:
            raise _DeadlineExceeded
        raise _RequestTimeout
    try:
        return await asyncio.wait_for(coro, timeout=left)
    except asyncio.TimeoutError as exc:
        now = time.monotonic()
        if run_deadline <= now:
            raise _DeadlineExceeded from exc
        raise _RequestTimeout from exc


async def _close_response(response) -> None:
    try:
        await asyncio.wait_for(response.aclose(), timeout=1.0)
    except BaseException:
        pass


def _mark_request_sent(state: _State) -> None:
    """只在协程真正开始执行时占用 HTTP 预算并计入实际发送。

    期限在发送前已过时，``_wait`` 会关闭该协程而不执行它，因此预留数不会
    被误报成真实网络请求。
    """
    state.budget.consume()
    state.actual_network_requests += 1
    if state.network_progress is not None:
        # 让 main 在 SIGINT/外层失败时仍能报告已经真实发生的发送次数。
        state.network_progress["actual_network_requests"] = state.actual_network_requests


async def _send_started(client: httpx.AsyncClient, request: httpx.Request,
                        state: _State):
    _mark_request_sent(state)
    return await client.send(request, stream=True)


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    token: str,
    state: _State,
    json_body: dict | None = None,
    files: dict | None = None,
    max_bytes: int = MAX_JSON_RESPONSE_BYTES,
    soft_deadline: float | None = None,
) -> Any:
    """单个 HTTP 调用：无重定向、无重试、无环境代理；整体受运行期限约束。

    ``soft_deadline`` 是调用方给定的更严格截止（例如文档就绪窗口）；它与
    运行期限取 min，同样覆盖发送、响应头与响应体读取，到期按请求超时处理。
    """
    request_deadline = min(state.deadline, time.monotonic() + 30.0)
    if soft_deadline is not None:
        request_deadline = min(request_deadline, soft_deadline)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    request = client.build_request(method, url, headers=headers, json=json_body, files=files)
    response = None
    try:
        response = await _wait(_send_started(client, request, state),
                               run_deadline=state.deadline,
                               request_deadline=request_deadline)
        state.http_statuses[str(response.status_code)] = (
            state.http_statuses.get(str(response.status_code), 0) + 1
        )
        if 300 <= response.status_code < 400:
            raise _RedirectDetected
        if response.status_code >= 400:
            raise _HttpStatus(response.status_code)

        body = bytearray()
        iterator = response.aiter_bytes()
        try:
            while True:
                try:
                    chunk = await _wait(anext(iterator), run_deadline=state.deadline,
                                        request_deadline=request_deadline)
                except StopAsyncIteration:
                    break
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise _ProtocolFailure("response_too_large")
        finally:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                try:
                    await asyncio.wait_for(close(), timeout=1.0)
                except BaseException:
                    pass
        try:
            payload = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _ProtocolFailure("invalid_json") from exc
        if (not isinstance(payload, dict) or payload.get("code") != 0
                or payload.get("success") is not True or "data" not in payload):
            raise _ProtocolFailure("invalid_envelope")
        return payload["data"]
    finally:
        if response is not None:
            await _close_response(response)


def _parse_user(data: Any) -> uuid.UUID:
    if not isinstance(data, dict):
        raise _ProtocolFailure("invalid_user")
    raw = data.get("id")
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError) as exc:
        raise _ProtocolFailure("invalid_user") from exc


def _require_admin_user(data: Any) -> None:
    if not isinstance(data, dict) or data.get("role") != "admin":
        raise _ProtocolFailure("admin_required")


def _find_kb(data: Any, kb_id: str) -> dict:
    if not isinstance(data, list):
        raise _ProtocolFailure("invalid_kb_list")
    for item in data:
        if isinstance(item, dict) and str(item.get("id")) == kb_id:
            return item
    raise _ProtocolFailure("kb_not_found")


def _ops_snapshot(data: Any) -> dict:
    """只保留队列/数据库状态；队列不可用时保持 null，绝不补 0。"""
    if not isinstance(data, dict):
        raise _ProtocolFailure("invalid_operations")
    queue = data.get("queue")
    result = {
        "degraded": bool(data.get("degraded") is True),
        "queue": None,
        "database": None,
    }
    database = data.get("database")
    if isinstance(database, dict):
        result["database"] = database.get("status") if isinstance(database.get("status"), str) else None
    if isinstance(queue, dict):
        result["queue"] = {
            "state": queue.get("state") if isinstance(queue.get("state"), str) else None,
            "queued": queue.get("queued") if type(queue.get("queued")) is int else None,
            "stale_processing": (
                queue.get("stale_processing")
                if type(queue.get("stale_processing")) is int else None
            ),
        }
    return result


async def _sample_operations(client, config: BenchmarkConfig, token: str, state: _State) -> dict:
    try:
        data = await _json_request(client, "GET", f"{config.api_base_url}/api/v1/operations/summary",
                                   token=token, state=state)
        return {"status": "ok", **_ops_snapshot(data)}
    except (_DeadlineExceeded, _RequestTimeout, _HttpBudgetExceeded, _RedirectDetected,
            _HttpStatus, _ProtocolFailure, httpx.HTTPError):
        return {"status": "unavailable", "queue": None, "database": None}


def _measurement_body(config: BenchmarkConfig, state: _State) -> dict:
    return {
        "start": _iso(state.window_start),
        "end": _iso(datetime.now(timezone.utc) + timedelta(seconds=2)),
        "requests": [{"user_id": str(user_id), "request_id": str(request_id)}
                     for user_id, request_id in state.issued_requests],
        "document_ids": list(state.uploaded_document_ids),
    }


def _measurement_check(data: Any, *, phase: str, stage_index: int | None,
                       wave_index: int | None, budget: Decimal) -> dict:
    if not isinstance(data, dict):
        raise _ProtocolFailure("invalid_measurement")
    coverage = data.get("coverage") if isinstance(data.get("coverage"), dict) else {}
    totals = data.get("totals") if isinstance(data.get("totals"), dict) else {}
    currency = data.get("currency")
    complete = coverage.get("complete") is True and coverage.get("status") == "complete"
    total_cost = totals.get("total_cost")
    known_subtotal = totals.get("known_cost_subtotal")
    attempts = totals.get("attempts")
    total = None
    known = None
    try:
        if isinstance(total_cost, str):
            total = Decimal(total_cost)
            if not total.is_finite():
                total = None
        if isinstance(known_subtotal, str):
            known = Decimal(known_subtotal)
            if not known.is_finite():
                known = None
    except InvalidOperation:
        total, known = None, None

    if complete and total is not None and currency == "CNY":
        spent = total
        status = "budget" if spent >= budget else "ok"
    elif known is not None:
        spent = known
        status = "unknown"
    else:
        spent = None
        status = "unknown"

    return {
        "phase": phase,
        "stage_index": stage_index,
        "wave_index": wave_index,
        "status": status,
        "coverage": coverage.get("status") if isinstance(coverage.get("status"), str) else "unknown",
        "attempts": attempts if type(attempts) is int else None,
        "total_cost": (_money(total) if complete and total is not None and currency == "CNY"
                       else None),
        "known_cost_subtotal": _money(known) if known is not None else None,
        "currency": currency if isinstance(currency, str) else None,
        "budget_exceeded": status == "budget",
    }


async def _cost_check(client, config: BenchmarkConfig, admin_token: str, state: _State, *,
                      phase: str, stage_index: int | None = None,
                      wave_index: int | None = None) -> dict:
    if state.unconfirmed_uploads:
        # 已发出的上传没有确认身份/是否被接收：其潜在费用无法被其他已知文档
        # 的完整测量覆盖，也不能报成零。无需再发一次测量请求。
        check = {
            "phase": phase, "stage_index": stage_index, "wave_index": wave_index,
            "status": "unknown", "coverage": "unconfirmed-upload", "attempts": None,
            "total_cost": None, "known_cost_subtotal": None,
            "currency": None, "budget_exceeded": False,
        }
        state.cost_checks.append(check)
        return check
    if state.work_issued == 0:
        check = {
            "phase": phase, "stage_index": stage_index, "wave_index": wave_index,
            "status": "ok", "coverage": "no_work", "attempts": 0,
            "total_cost": "0.0000000000", "known_cost_subtotal": None,
            "currency": "CNY", "budget_exceeded": decimal_zero_budget(config),
        }
        state.cost_checks.append(check)
        return check
    if not state.issued_requests and not state.uploaded_document_ids:
        check = {
            "phase": phase, "stage_index": stage_index, "wave_index": wave_index,
            "status": "unknown", "coverage": "no_selection", "attempts": None,
            "total_cost": None, "known_cost_subtotal": None,
            "currency": None, "budget_exceeded": False,
        }
        state.cost_checks.append(check)
        return check
    try:
        data = await _json_request(client, "POST", f"{config.api_base_url}/api/v1/usage/measurement",
                                   token=admin_token, state=state,
                                   json_body=_measurement_body(config, state))
        if phase == "final":
            state.final_measurement_data = data
        check = _measurement_check(data, phase=phase, stage_index=stage_index,
                                   wave_index=wave_index, budget=config.max_cost_cny)
    except (_DeadlineExceeded, _RequestTimeout, _HttpBudgetExceeded, _RedirectDetected,
            _HttpStatus, _ProtocolFailure, httpx.HTTPError):
        # 取不到精确费用时，结果只能是未知；绝不把未知当 0，也不继续新付费工作。
        if isinstance(sys.exc_info()[1], _DeadlineExceeded):
            state.deadline_hit = True
        check = {
            "phase": phase, "stage_index": stage_index, "wave_index": wave_index,
            "status": "unknown", "coverage": "unavailable", "attempts": None,
            "total_cost": None, "known_cost_subtotal": None,
            "currency": None, "budget_exceeded": False,
        }
    state.cost_checks.append(check)
    return check


def decimal_zero_budget(config: BenchmarkConfig) -> bool:
    return config.max_cost_cny == 0


def _should_stop(check: dict) -> bool:
    return check["status"] in {"budget", "unknown"}


async def _upload_and_wait(client, config: BenchmarkConfig, admin_token: str,
                           document: DocumentSpec, state: _State) -> _UploadSample:
    filename = f"{document.code}{document.suffix}"
    url = f"{config.api_base_url}/api/v1/knowledge-bases/{config.kb_id}/documents"
    sent_before = state.actual_network_requests
    try:
        data = await _json_request(
            client, "POST", url, token=admin_token, state=state,
            files={"file": (filename, document.payload, "application/octet-stream")},
        )
    except _DeadlineExceeded:
        state.deadline_hit = True
        sent = state.actual_network_requests > sent_before
        return _UploadSample(index=document.index, outcome="timeout",
                             error="run_deadline", sent=sent, unconfirmed=sent)
    except (_RequestTimeout, _HttpBudgetExceeded, _RedirectDetected,
            _HttpStatus, _ProtocolFailure, httpx.HTTPError):
        # 请求一旦真正发出，服务端可能已接收并计费；没有确认 ID 就不能把
        # 这份潜在费用当作不存在，更不能被其他文档的完整测量覆盖。
        sent = state.actual_network_requests > sent_before
        return _UploadSample(index=document.index, outcome="upload_error",
                             error="upload_failed", sent=sent, unconfirmed=sent)
    sent = state.actual_network_requests > sent_before
    if not isinstance(data, dict):
        return _UploadSample(index=document.index, outcome="upload_error",
                             error="invalid_upload_response", sent=sent,
                             unconfirmed=sent)
    doc_id = data.get("id")
    try:
        doc_id = str(uuid.UUID(str(doc_id)))
    except (ValueError, TypeError, AttributeError):
        return _UploadSample(index=document.index, outcome="upload_error",
                             error="invalid_upload_response", sent=sent,
                             unconfirmed=sent)
    deduplicated = data.get("deduplicated") is True
    if deduplicated:
        return _UploadSample(index=document.index, outcome="deduplicated",
                             document_id=doc_id, deduplicated=True, sent=sent)

    state.uploaded_document_ids.append(doc_id)
    state.work_issued += 1
    ready_deadline = min(state.deadline, time.monotonic() + config.ready_timeout_seconds)
    last_chunks = 0
    delay = 0.5
    while True:
        # 就绪截止同时覆盖发送、响应头、响应体与等待；轮询结果只有在截止
        # 之前取得的才可能被认作 ready，晚到的 ready 不能伪装成按时完成。
        try:
            detail = await _json_request(
                client, "GET",
                f"{config.api_base_url}/api/v1/knowledge-bases/{config.kb_id}/documents/{doc_id}",
                token=admin_token, state=state, soft_deadline=ready_deadline,
            )
        except (_DeadlineExceeded, _RequestTimeout, _HttpBudgetExceeded, _RedirectDetected,
                _HttpStatus, _ProtocolFailure, httpx.HTTPError):
            now = time.monotonic()
            if now >= ready_deadline:
                return _UploadSample(index=document.index, outcome="timeout",
                                     document_id=doc_id, chunk_count=last_chunks,
                                     error="ready_timeout", sent=True)
            return _UploadSample(index=document.index, outcome="unknown",
                                 document_id=doc_id, error="status_unavailable", sent=True)
        if not isinstance(detail, dict):
            return _UploadSample(index=document.index, outcome="unknown",
                                 document_id=doc_id, error="invalid_status_response", sent=True)
        status = detail.get("status")
        if type(detail.get("chunk_count")) is int:
            last_chunks = detail["chunk_count"]
        if status == "ready":
            if time.monotonic() > ready_deadline:
                # 超过就绪窗口才收到的 ready 不算就绪；确认 ID 与潜在费用保留。
                return _UploadSample(index=document.index, outcome="timeout",
                                     document_id=doc_id, chunk_count=last_chunks,
                                     error="ready_timeout", sent=True)
            return _UploadSample(index=document.index, outcome="ready",
                                 document_id=doc_id, chunk_count=last_chunks, sent=True)
        if status in {"failed", "cancelled"}:
            return _UploadSample(index=document.index, outcome=str(status),
                                 document_id=doc_id, chunk_count=last_chunks,
                                 error=f"ingest_{status}", sent=True)
        now = time.monotonic()
        if now >= ready_deadline:
            return _UploadSample(index=document.index, outcome="timeout",
                                 document_id=doc_id, chunk_count=last_chunks,
                                 error="ready_timeout", sent=True)
        pause = min(delay, max(0.05, ready_deadline - now))
        delay = min(5.0, delay * 1.5)
        try:
            await asyncio.wait_for(asyncio.sleep(pause),
                                   timeout=max(0.01, min(state.deadline, ready_deadline) - now))
        except asyncio.TimeoutError:
            if time.monotonic() >= ready_deadline:
                return _UploadSample(index=document.index, outcome="timeout",
                                     document_id=doc_id, chunk_count=last_chunks,
                                     error="ready_timeout", sent=True)
            state.deadline_hit = True
            return _UploadSample(index=document.index, outcome="timeout",
                                 document_id=doc_id, chunk_count=last_chunks,
                                 error="run_deadline", sent=True)


def _classify_stream_event(event: SseEvent) -> str | None:
    return event.name


def _reject_event_after_done(event: SseEvent) -> None:
    """合法 done 之后已收到的事件都属于畸形尾部，绝不静默当作成功。"""
    if event.name == "done":
        raise _ProtocolFailure("duplicate_done")
    if event.name == "error":
        raise _ProtocolFailure("stream_error")
    raise _ProtocolFailure("event_after_done")


def _parse_event_json(event: SseEvent, code: str) -> dict:
    try:
        payload = json.loads(event.data)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _ProtocolFailure(code) from exc
    if not isinstance(payload, dict):
        raise _ProtocolFailure(code)
    return payload


async def _one_chat(client, config: BenchmarkConfig, token: str, state: _State,
                    index: int, stage_index: int) -> _ChatSample:
    started = time.monotonic()
    request_deadline = min(state.deadline, started + config.request_timeout_seconds)
    request_id = str(uuid.uuid4())
    payload = {
        "question": config.question_text,
        "mode": config.mode,
        "request_id": request_id,
    }
    if config.mode == "rag":
        payload["kb_id"] = config.kb_id
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
    }
    request = client.build_request("POST", f"{config.api_base_url}/api/v1/chat/stream",
                                   headers=headers, json=payload)
    response = None
    iterator = None
    parser = SseParser(max_bytes=config.limits.max_stream_bytes,
                       max_events=config.limits.max_sse_events)
    ttft_ms: float | None = None
    done: dict | None = None
    stream_error = False
    content_chars = 0

    def handle_event(event: SseEvent) -> bool:
        nonlocal ttft_ms, done, stream_error, content_chars
        name = event.name
        if name == "delta":
            body = _parse_event_json(event, "invalid_delta")
            content = body.get("content")
            if isinstance(content, str):
                content_chars += len(content)
                if content.strip() and ttft_ms is None:
                    ttft_ms = (time.monotonic() - started) * 1000
        elif name == "done":
            if done is not None:
                raise _ProtocolFailure("duplicate_done")
            body = _parse_event_json(event, "invalid_done")
            if "truncated" not in body or type(body.get("truncated")) is not bool:
                raise _ProtocolFailure("invalid_done")
            if "finish_reason" not in body:
                raise _ProtocolFailure("invalid_done")
            reason = body.get("finish_reason")
            if reason is not None and not isinstance(reason, str):
                raise _ProtocolFailure("invalid_done")
            done = body
            return True  # done 是终止事件，停止继续读取，避免无限心跳拖住
        elif name == "error":
            stream_error = True
            return True
        # meta/status/message 等事件不参与 TTFT，忽略。
        return False

    sent_started = False

    async def send_chat():
        nonlocal sent_started
        _mark_request_sent(state)
        sent_started = True
        state.work_issued += 1
        state.issued_requests.append((state.user_id, uuid.UUID(request_id)))
        return await client.send(request, stream=True)

    try:
        response = await _wait(send_chat(), run_deadline=state.deadline,
                               request_deadline=request_deadline)
        state.http_statuses[str(response.status_code)] = (
            state.http_statuses.get(str(response.status_code), 0) + 1
        )
        if 300 <= response.status_code < 400:
            raise _RedirectDetected
        if response.status_code == 429:
            return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                               outcome="rejected", http_status=429, sent=sent_started)
        if response.status_code >= 400:
            return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                               outcome="error", http_status=response.status_code,
                               error=f"http_{response.status_code}", sent=sent_started)
        # 严格 media type：主类型/子类型必须恰好是 text/event-stream，
        # 只允许后接合法参数（如 ; charset=utf-8）；"text/event-stream-other"
        # 之类的扩展子类型不能伪装成 SSE。
        content_type = response.headers.get("content-type", "")
        media_parts = content_type.split(";", 1)
        media_type = media_parts[0].strip().lower()
        if media_type != "text/event-stream" or not media_type:
            raise _ProtocolFailure("unexpected_content_type")

        iterator = response.aiter_bytes()
        terminated = False
        while not terminated:
            try:
                chunk = await _wait(anext(iterator), run_deadline=state.deadline,
                                    request_deadline=request_deadline)
            except StopAsyncIteration:
                break
            for event in parser.feed(chunk):
                if terminated:
                    # done 是终止事件；同一批已收到的事件若仍继续，属于畸形尾部，
                    # 不静默接受，也不把它当作成功。
                    _reject_event_after_done(event)
                if handle_event(event):
                    # 不在这里 break：同一块里紧跟 done 的非法事件也要检查。
                    terminated = True
        # done 之后不再等待后续字节/EOF；但已经收到并缓冲在同一批里的尾部
        # 必须在此有界检查，否则 “DONE + 半个 error 事件” 会被误判成功。
        for event in parser.finish():
            if terminated:
                _reject_event_after_done(event)
            if handle_event(event):
                terminated = True
        latency_ms = (time.monotonic() - started) * 1000
        if stream_error:
            return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                               outcome="error", http_status=response.status_code,
                               ttft_ms=ttft_ms, latency_ms=latency_ms,
                               bytes=parser.total_bytes, events=parser.event_count,
                               content_chars=content_chars, error="stream_error", sent=sent_started)
        if done is None:
            return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                               outcome="error", http_status=response.status_code,
                               ttft_ms=ttft_ms, latency_ms=latency_ms,
                               bytes=parser.total_bytes, events=parser.event_count,
                               content_chars=content_chars, error="missing_done", sent=sent_started)
        truncated = done.get("truncated") is True or done.get("finish_reason") == "length"
        return _ChatSample(
            index=index, stage_index=stage_index, request_id=request_id,
            outcome="truncated" if truncated else "success",
            http_status=response.status_code, ttft_ms=ttft_ms, latency_ms=latency_ms,
            bytes=parser.total_bytes, events=parser.event_count,
            content_chars=content_chars,
            error="finish_reason_length" if truncated else None, sent=sent_started,
        )
    except _DeadlineExceeded:
        state.deadline_hit = True
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="cancelled", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error="run_deadline", sent=sent_started)
    except _RequestTimeout:
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="timeout", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error="request_timeout", sent=sent_started)
    except _HttpBudgetExceeded:
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="error", error="http_budget", sent=False)
    except _RedirectDetected:
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="error", error="redirect", sent=sent_started)
    except _ProtocolFailure as exc:
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="error", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error=exc.code, sent=sent_started)
    except SseProtocolError as exc:
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="error", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error=exc.code, sent=sent_started)
    except (asyncio.IncompleteReadError, ConnectionError):
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="disconnected", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error="disconnected", sent=sent_started)
    except httpx.ReadTimeout:
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="timeout", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error="read_timeout", sent=sent_started)
    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.StreamError,
            httpx.WriteError, httpx.ConnectError, httpx.PoolTimeout):
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="disconnected", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error="transport_error", sent=sent_started)
    except Exception:
        return _ChatSample(index=index, stage_index=stage_index, request_id=request_id,
                           outcome="error", ttft_ms=ttft_ms,
                           latency_ms=(time.monotonic() - started) * 1000,
                           bytes=parser.total_bytes, events=parser.event_count,
                           content_chars=content_chars, error="unexpected_transport", sent=sent_started)
    finally:
        if iterator is not None:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                try:
                    await asyncio.wait_for(close(), timeout=1.0)
                except BaseException:
                    pass
        if response is not None:
            await _close_response(response)


async def _run_wave(client, config: BenchmarkConfig, token: str, state: _State,
                    stage_index: int, wave_index: int, count: int) -> list[_ChatSample]:
    tasks = [asyncio.create_task(_one_chat(client, config, token, state,
                                           index=0, stage_index=stage_index))
             for _ in range(count)]
    for offset, task in enumerate(tasks, start=1):
        # 只用于报告序号，不参与请求身份。
        task.set_name(f"benchmark-{stage_index}-{wave_index}-{offset}")
    results = await asyncio.gather(*tasks, return_exceptions=True)
    samples: list[_ChatSample] = []
    for result in results:
        if isinstance(result, _ChatSample):
            samples.append(result)
    return samples


def _request_indices_start(state: _State) -> int:
    return len(state.samples) + 1


def _redact_measurement_map(data: Any, key: str) -> dict:
    if not isinstance(data, dict):
        return {}
    items = data.get(key)
    if not isinstance(items, list):
        return {}
    mapped = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if key == "documents":
            identity = item.get("document_id")
        else:
            request_id = item.get("request_id")
            owner = item.get("user_id")
            identity = (str(owner), str(request_id)) if isinstance(request_id, str) else None
        if not isinstance(identity, (str, tuple)):
            continue
        mapped[identity] = {
            "known_cost_subtotal": item.get("known_cost_subtotal")
            if isinstance(item.get("known_cost_subtotal"), str) else None,
            "coverage_status": item.get("coverage_status")
            if isinstance(item.get("coverage_status"), str) else "unknown",
        }
    return mapped


async def _run_live(config: BenchmarkConfig, tokens: tuple[str, str], args,
                    progress: dict | None = None) -> dict:
    user_token, admin_token = tokens
    started = time.monotonic()
    deadline = started + config.run_timeout_seconds
    state = _State(
        user_id=uuid.UUID(int=0),
        started_wall=datetime.now(timezone.utc),
        window_start=datetime.now(timezone.utc) - timedelta(seconds=5),
        deadline=deadline,
        budget=_HttpBudget(config.limits.max_http_requests),
        network_progress=progress,
    )
    client: httpx.AsyncClient | None = None
    failure_reason: str | None = None
    failure_status = "completed"
    try:
        max_conn = max(4, max(stage.concurrency for stage in config.stages) + 2)
        client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=True,
            timeout=httpx.Timeout(config.request_timeout_seconds,
                                  connect=min(5.0, config.request_timeout_seconds)),
            limits=httpx.Limits(max_connections=max_conn,
                                max_keepalive_connections=max_conn),
        )
        # 1) 只读身份校验；任何付费工作之前完成。
        user_data = await _json_request(client, "GET", f"{config.api_base_url}/api/v1/auth/me",
                                        token=user_token, state=state)
        state.user_id = _parse_user(user_data)
        admin_data = await _json_request(client, "GET", f"{config.api_base_url}/api/v1/auth/me",
                                         token=admin_token, state=state)
        _require_admin_user(admin_data)

        # 2) KB 必须是已有的普通上传库；绝不自动创建/切换/删除。
        if config.kb_id:
            kb_data = await _json_request(client, "GET",
                                          f"{config.api_base_url}/api/v1/knowledge-bases",
                                          token=(admin_token if config.documents else user_token),
                                          state=state)
            kb = _find_kb(kb_data, config.kb_id)
            if kb.get("kind") != "upload":
                raise _ProtocolFailure("kb_not_upload_kind")

        state.operations_before = await _sample_operations(client, config, admin_token, state)

        # 3) 上传串行；每次上传前后都用精确计量做费用门禁。
        for document in config.documents:
            if state.stopped:
                break
            pre = await _cost_check(client, config, admin_token, state,
                                    phase="pre-upload")
            if _should_stop(pre):
                state.stopped = "stopped-budget" if pre["status"] == "budget" else "stopped-unknown-cost"
                break
            upload = await _upload_and_wait(client, config, admin_token, document, state)
            state.uploads.append(upload)
            if upload.unconfirmed:
                state.unconfirmed_uploads += 1
                state.stopped = "stopped-unknown-cost"
                break
            if upload.deduplicated:
                continue
            post = await _cost_check(client, config, admin_token, state,
                                     phase="post-upload")
            if _should_stop(post):
                state.stopped = "stopped-budget" if post["status"] == "budget" else "stopped-unknown-cost"
                break

        # 4) 并发阶梯：每个 stage 按 wave 发出，wave 前后核对累计费用。
        for stage_index, stage in enumerate(config.stages, start=1):
            if state.stopped or state.deadline_hit:
                break
            issued_in_stage = 0
            wave_index = 0
            while issued_in_stage < stage.requests:
                if time.monotonic() >= state.deadline:
                    state.deadline_hit = True
                    break
                pre = await _cost_check(client, config, admin_token, state,
                                        phase="pre-wave", stage_index=stage_index,
                                        wave_index=wave_index + 1)
                if _should_stop(pre):
                    state.stopped = ("stopped-budget" if pre["status"] == "budget"
                                     else "stopped-unknown-cost")
                    break
                count = min(stage.concurrency, stage.requests - issued_in_stage)
                wave_index += 1
                samples = await _run_wave(client, config, token=user_token, state=state,
                                          stage_index=stage_index, wave_index=wave_index,
                                          count=count)
                base = _request_indices_start(state)
                for offset, sample in enumerate(samples, start=1):
                    sample.index = base + offset - 1
                state.samples.extend(samples)
                issued_in_stage += len(samples)
                post = await _cost_check(client, config, admin_token, state,
                                         phase="post-wave", stage_index=stage_index,
                                         wave_index=wave_index)
                if _should_stop(post):
                    state.stopped = ("stopped-budget" if post["status"] == "budget"
                                     else "stopped-unknown-cost")
                    break

        state.operations_after = await _sample_operations(client, config, admin_token, state)

        # 5) 最终精确计量；报告中的金额只来自这里。
        final = await _cost_check(client, config, admin_token, state, phase="final")
        if state.deadline_hit:
            # 共享总期限已到，最终计量本身可能是被取消的；先如实报告超时，
            # 不把“没时间取得最终费用”改写成可解释的零费用或已完成。
            failure_status = "timeout"
        elif final["status"] == "unknown":
            failure_status = "unknown-cost"
        elif final["status"] == "budget":
            failure_status = "budget-exceeded"
        elif state.stopped:
            failure_status = state.stopped
        elif state.work_issued == 0 and any(not sample.sent for sample in state.samples):
            # 请求在期限/预算检查后仍未真正发出；不能把没测量的运行报成 completed。
            failure_status = "no-request-sent"
        else:
            failure_status = "completed"

        request_costs = _redact_measurement_map(state.final_measurement_data, "requests")
        document_costs = _redact_measurement_map(state.final_measurement_data, "documents")

        report = _live_report(config, state, args, failure_status,
                              request_costs, document_costs)
        return report
    except _DeadlineExceeded:
        state.deadline_hit = True
        failure_status = "timeout"
        failure_reason = "run_deadline"
    except _RequestTimeout:
        failure_status = "failed"
        failure_reason = "request_timeout"
    except _HttpBudgetExceeded:
        failure_status = "failed"
        failure_reason = "http_budget"
    except _RedirectDetected:
        failure_status = "failed"
        failure_reason = "redirect"
    except _HttpStatus as exc:
        failure_status = "failed"
        failure_reason = f"http_{exc.status}"
    except _ProtocolFailure as exc:
        failure_status = "failed"
        failure_reason = exc.code
    except httpx.HTTPError:
        failure_status = "failed"
        failure_reason = "transport_error"
    except Exception:
        failure_status = "failed"
        failure_reason = "unexpected_failure"
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), timeout=2.0)
            except BaseException:
                pass
        # 收口任何我们创建但尚未结束的任务；没有发生时计数为 0。
        current = asyncio.current_task()
        residual = [task for task in asyncio.all_tasks()
                    if task is not current and not task.done()]
        for task in residual:
            task.cancel()
        if residual:
            await asyncio.gather(*residual, return_exceptions=True)

    report = _live_report(config, state, args, failure_status, {}, {})
    if failure_reason is not None:
        report["reason"] = failure_reason
    return report


def _outcome_counts(samples: list[_ChatSample]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.outcome] = counts.get(sample.outcome, 0) + 1
    return counts


def _latency_group(samples: list[_ChatSample]) -> dict:
    ttft = [sample.ttft_ms for sample in samples if sample.ttft_ms is not None]
    full = [sample.latency_ms for sample in samples if sample.latency_ms is not None]
    return {
        "ttft_p50_ms": percentile(ttft, 0.50),
        "ttft_p95_ms": percentile(ttft, 0.95),
        "ttft_samples": len(ttft),
        "latency_p50_ms": percentile(full, 0.50),
        "latency_p95_ms": percentile(full, 0.95),
        "latency_samples": len(full),
    }


def _redacted_stage_groups(config: BenchmarkConfig, samples: list[_ChatSample]) -> list[dict]:
    groups = []
    for stage_index, stage in enumerate(config.stages, start=1):
        stage_samples = [sample for sample in samples if sample.stage_index == stage_index]
        sent_samples = [sample for sample in stage_samples if sample.sent]
        counts = _outcome_counts(sent_samples)
        groups.append({
            "stage_index": stage_index,
            "concurrency": stage.concurrency,
            "requested": stage.requests,
            "issued": len(sent_samples),
            "not_sent": len(stage_samples) - len(sent_samples),
            "counts": counts,
            **_latency_group(sent_samples),
        })
    return groups


def _request_reports(samples: list[_ChatSample], request_costs: dict,
                     user_id: uuid.UUID) -> list[dict]:
    result = []
    for sample in samples:
        cost_entry = request_costs.get((str(user_id), sample.request_id)) if sample.request_id else None
        result.append({
            "index": sample.index,
            "stage_index": sample.stage_index,
            "sent": sample.sent,
            "outcome": sample.outcome,
            "http_status": sample.http_status,
            "ttft_ms": round(sample.ttft_ms, 1) if sample.ttft_ms is not None else None,
            "latency_ms": round(sample.latency_ms, 1) if sample.latency_ms is not None else None,
            "response_bytes": sample.bytes,
            "event_count": sample.events,
            "content_chars": sample.content_chars,
            "error": sample.error,
            "known_cost_cny": cost_entry["known_cost_subtotal"] if cost_entry else None,
            "cost_coverage": cost_entry["coverage_status"] if cost_entry else "unknown",
        })
    return result


def _upload_reports(uploads: list[_UploadSample], document_costs: dict) -> dict:
    # 只有拿到确认 ID 且非去重的上传才算“新文档”；已发出但未确认的尝试
    # 单独计数，不能冒充创建结果，也不能被其他已知文档的完整费用覆盖。
    confirmed_new = [item for item in uploads
                     if item.document_id is not None and not item.deduplicated]
    ready = [item for item in confirmed_new if item.outcome == "ready"]
    chunks = sum(item.chunk_count for item in ready)
    items = []
    for upload in uploads:
        entry = document_costs.get(upload.document_id) if upload.document_id else None
        items.append({
            "index": upload.index,
            "outcome": upload.outcome,
            "sent": upload.sent,
            "unconfirmed": upload.unconfirmed,
            "deduplicated": upload.deduplicated,
            "chunk_count": upload.chunk_count if upload.outcome == "ready" else None,
            "error": upload.error,
            "known_cost_cny": entry["known_cost_subtotal"] if entry else None,
            "cost_coverage": entry["coverage_status"] if entry else "unknown",
        })
    return {
        "declared": len(uploads),
        "attempts": len(uploads),
        "sent_attempts": sum(1 for item in uploads if item.sent),
        "confirmed_new": len(confirmed_new),
        "new_documents": len(confirmed_new),
        "deduplicated": sum(1 for item in uploads if item.deduplicated),
        "unconfirmed": sum(1 for item in uploads if item.unconfirmed),
        "ready": len(ready),
        "failed": sum(1 for item in confirmed_new
                      if item.outcome in {"failed", "upload_error"}),
        "cancelled": sum(1 for item in confirmed_new if item.outcome == "cancelled"),
        "timeout": sum(1 for item in confirmed_new if item.outcome == "timeout"),
        "unknown": sum(1 for item in confirmed_new if item.outcome == "unknown"),
        "ready_rate": round(len(ready) / len(confirmed_new), 4) if confirmed_new else None,
        "chunks_ready_total": chunks,
        "items": items,
    }


def _cost_summary(state: _State, final_check: dict | None) -> dict:
    final = final_check or (state.cost_checks[-1] if state.cost_checks else None)
    return {
        "budget_cny": None,  # 由 _live_report 从 config 填入
        "final_status": final["status"] if final else "not_started",
        "coverage": final["coverage"] if final else "not_started",
        "total_cost": final["total_cost"] if final else None,
        "known_cost_subtotal": final["known_cost_subtotal"] if final else None,
        "currency": final["currency"] if final else None,
        "checks": state.cost_checks,
    }


def _live_report(config: BenchmarkConfig, state: _State, args, failure_status: str,
                 request_costs: dict, document_costs: dict) -> dict:
    final_check = state.cost_checks[-1] if state.cost_checks else None
    sent_samples = [sample for sample in state.samples if sample.sent]
    counts = _outcome_counts(sent_samples)
    total = len(sent_samples)
    not_sent = len(state.samples) - total
    work_issued = state.work_issued
    measured = work_issued > 0

    if failure_status == "completed":
        if state.deadline_hit:
            failure_status = "timeout"
        elif state.stopped:
            failure_status = state.stopped

    if failure_status == "completed":
        exit_code = EXIT_OK
    elif failure_status in {"unknown-cost", "stopped-unknown-cost", "timeout"}:
        exit_code = EXIT_UNKNOWN
    else:
        exit_code = EXIT_FAILURE
    if failure_status == "completed" and counts.get("success", 0) == 0 and total:
        exit_code = EXIT_FAILURE
        failure_status = "completed-with-errors"

    cost = _cost_summary(state, final_check)
    cost["budget_cny"] = _money(config.max_cost_cny)
    uploads = _upload_reports(state.uploads, document_costs)
    report = {
        "schema": SCHEMA,
        "mode": "live",
        "status": failure_status,
        "measured": measured,
        "acceptance": False,
        "capacity_acceptance": False,
        "actual_network_requests": state.actual_network_requests,
        "target_fingerprint": config.target_fingerprint,
        "plan": {
            "mode": config.mode,
            "kb_declared": config.kb_id is not None,
            "total_requests": config.total_requests,
            "stages": [{"index": index, "concurrency": stage.concurrency,
                        "requests": stage.requests}
                       for index, stage in enumerate(config.stages, start=1)],
            "planned_scale": {"documents": config.planned_documents,
                              "users": config.planned_users},
            "question_chars": len(config.question_text),
            "question_code_fingerprint": "sha256:" + hashlib.sha256(
                config.question_code.encode("utf-8")).hexdigest()[:16],
            "currency": config.cost_currency,
        },
        "result": {
            "requests_issued": total,
            "requests_completed": total,
            "requests_not_sent": not_sent,
            "counts": counts,
            "success_rate": round(counts.get("success", 0) / total, 4) if total else None,
            "http_statuses": dict(sorted(state.http_statuses.items())),
            "deadline_hit": state.deadline_hit,
        },
        "latency": {
            "groups": _redacted_stage_groups(config, state.samples),
            "overall": _latency_group(sent_samples),
        },
        "requests": _request_reports(state.samples, request_costs, state.user_id),
        "uploads": uploads,
        "cost": cost,
        "operations": {
            "before": state.operations_before,
            "after": state.operations_after,
            "queue_unknown_is_null": True,
        },
        "limits": {
            "max_http_requests": config.limits.max_http_requests,
            "max_upload_files": config.limits.max_upload_files,
            "max_upload_bytes": config.limits.max_upload_bytes,
            "max_stream_bytes": config.limits.max_stream_bytes,
            "max_sse_events": config.limits.max_sse_events,
            "request_timeout_seconds": config.request_timeout_seconds,
            "run_timeout_seconds": config.run_timeout_seconds,
            "ready_timeout_seconds": config.ready_timeout_seconds,
        },
        "limitations": _limitations(),
        "exit_code": exit_code,
    }
    return report


def _limitations() -> list[str]:
    return [
        "本地/单目标小样本测量，不是企业容量验收，也不是业务通过或生产上线结论。",
        "费用来自本实例计量快照，不是模型供应商账单；缺计量、未计价、未完成、截断或币种冲突时总费用为 null。",
        "上传文档可能触发 embedding 计费；本工具不会删除或回滚已上传文档与会话。",
        "并发阶梯按 wave 发出；费用门禁只阻止下一批新任务，不能撤回已发出的在途请求。",
        "不跟随重定向、不重试、不读取环境代理；只允许单一 HTTPS 目标或回环 HTTP 目标。",
        "报告不包含问题/答案/引用正文、文件名、路径、令牌、用户/请求 ID、模型地址或原始服务器响应。",
        "后台摘要与项目索引调用不在请求测量范围；它们的费用不计入本报告。",
    ]


def _dry_run_report(config: BenchmarkConfig, args) -> dict:
    return {
        "schema": SCHEMA,
        "mode": "dry-run",
        "status": "plan-valid",
        "measured": False,
        "acceptance": False,
        "capacity_acceptance": False,
        "actual_network_requests": 0,
        "target_fingerprint": config.target_fingerprint,
        "config": {"version": config.version, "bytes": config.config_bytes,
                   "sha256": config.config_sha256},
        "plan": {
            "mode": config.mode,
            "kb_declared": config.kb_id is not None,
            "total_requests": config.total_requests,
            "stages": [{"index": index, "concurrency": stage.concurrency,
                        "requests": stage.requests}
                       for index, stage in enumerate(config.stages, start=1)],
            "planned_scale": {"documents": config.planned_documents,
                              "users": config.planned_users},
            "question_chars": len(config.question_text),
            "question_code_fingerprint": "sha256:" + hashlib.sha256(
                config.question_code.encode("utf-8")).hexdigest()[:16],
            "currency": config.cost_currency,
            "max_cost_cny": _money(config.max_cost_cny),
        },
        "uploads": {
            "declared_files": len(config.documents),
            "total_bytes": config.upload_total_bytes,
            "max_files": config.limits.max_upload_files,
            "max_bytes": config.limits.max_upload_bytes,
        },
        "limits": {
            "max_http_requests": config.limits.max_http_requests,
            "max_stream_bytes": config.limits.max_stream_bytes,
            "max_sse_events": config.limits.max_sse_events,
            "request_timeout_seconds": config.request_timeout_seconds,
            "run_timeout_seconds": config.run_timeout_seconds,
            "ready_timeout_seconds": config.ready_timeout_seconds,
        },
        "live_requirements": {
            "live_flag": True,
            "confirm_target_required": True,
            "accept_model_cost_required": True,
            "allow_upload_required_if_documents": bool(config.documents),
        },
        "limitations": _limitations(),
        "exit_code": EXIT_OK,
    }


def _error_report(args, *, status: str, field: str | None = None,
                  reason: str, network_requests: int = 0) -> dict:
    payload = {
        "schema": SCHEMA,
        "mode": "live" if getattr(args, "live", False) else "dry-run",
        "status": status,
        "measured": False,
        "acceptance": False,
        "capacity_acceptance": False,
        "actual_network_requests": network_requests,
        "reason": reason,
        "exit_code": EXIT_INPUT if status == "input-error" else EXIT_FAILURE,
    }
    if field:
        payload["field"] = field
    return payload


def _load_tokens(config: BenchmarkConfig) -> tuple[str, str]:
    values = []
    for field_name, env_name in (("auth.user_token_env", config.user_token_env),
                                 ("auth.admin_token_env", config.admin_token_env)):
        value = os.environ.get(env_name, "")
        if not value:
            raise BenchmarkInputError(field_name, "missing_env")
        if len(value) > MAX_TOKEN_CHARS:
            raise BenchmarkInputError(field_name, "token_too_long")
        if any(char.isspace() or ord(char) < 32 for char in value):
            raise BenchmarkInputError(field_name, "token_has_whitespace")
        if not re.fullmatch(r"[\x21-\x7e]+", value):
            raise BenchmarkInputError(field_name, "token_invalid_chars")
        values.append(value)
    return values[0], values[1]


def _validate_live_gates(config: BenchmarkConfig, args) -> None:
    if args.confirm_target != config.target_fingerprint:
        raise BenchmarkInputError("confirm_target", "mismatch")
    if not args.accept_model_cost:
        raise BenchmarkInputError("accept_model_cost", "required")
    if config.documents and not args.allow_upload:
        raise BenchmarkInputError("allow_upload", "required")
    if config.documents and (config.mode != "rag" or not config.kb_id):
        raise BenchmarkInputError("documents", "requires_rag_kb")


def _write_report(report: dict, output: Path | None, *, overwrite: bool) -> None:
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if output is not None:
        parent = output.resolve(strict=False).parent
        fd, temp_name = tempfile.mkstemp(prefix=".ragbench-", suffix=".tmp", dir=parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                os.replace(temp_path, output)
            else:
                # 原子独占发布：目标已存在时 link 失败，绝不先删除或改写既有文件。
                try:
                    os.link(temp_path, output)
                except FileExistsError as exc:
                    raise BenchmarkInputError("output", "already_exists") from exc
        finally:
            # 只清理本次自建的临时文件；异常路径绝不按目标名盲删。
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
    print(payload)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="有界 RAG 性能/费用测量（默认 dry-run，只做本地校验）")
    parser.add_argument("--config", required=True, help="版本化 JSON 配置")
    parser.add_argument("--out", default="", help="可选报告输出路径（默认只打印到 stdout）")
    parser.add_argument("--overwrite", action="store_true", help="允许原子替换已存在的报告")
    parser.add_argument("--live", action="store_true", help="显式执行真实 HTTP 测量")
    parser.add_argument("--dry-run", action="store_true", help="仅本地计划校验（默认）")
    parser.add_argument("--confirm-target", default="",
                        help="必须等于 dry-run 输出的 target_fingerprint")
    parser.add_argument("--accept-model-cost", action="store_true",
                        help="确认 live 会产生真实模型/上传费用")
    parser.add_argument("--allow-upload", action="store_true",
                        help="允许按配置上传文档（仍受硬界约束）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    progress = {"actual_network_requests": 0}
    if args.live and args.dry_run:
        report = _error_report(args, status="input-error", field="mode", reason="conflicting_modes")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_INPUT
    try:
        config = load_config(args.config)
        output = validate_output_path(args.out, args.config, config, overwrite=args.overwrite)
        if not args.live:
            report = _dry_run_report(config, args)
            _write_report(report, output, overwrite=args.overwrite)
            return EXIT_OK

        _validate_live_gates(config, args)
        if config.max_cost_cny == 0:
            report = _live_report(config, _State(
                user_id=uuid.UUID(int=0),
                started_wall=datetime.now(timezone.utc),
                window_start=datetime.now(timezone.utc),
                deadline=time.monotonic() + config.run_timeout_seconds,
                budget=_HttpBudget(config.limits.max_http_requests),
            ), args, "stopped-budget", {}, {})
            report["actual_network_requests"] = 0
            report["reason"] = "zero_budget"
            _write_report(report, output, overwrite=args.overwrite)
            return EXIT_FAILURE

        tokens = _load_tokens(config)
        report = asyncio.run(_run_live(config, tokens, args, progress))
        exit_code = int(report.pop("exit_code", EXIT_FAILURE))
        try:
            _write_report(report, output, overwrite=args.overwrite)
        except (OSError, BenchmarkInputError):
            # HTTP 之后的写入失败/晚到冲突属于运行失败，不能回退成“输入错误、
            # 零 HTTP”；保留已经真实发生的发送计数与运行事实。
            failure = _error_report(args, status="failure", reason="output_write_failed",
                                    network_requests=report.get("actual_network_requests", 0))
            print(json.dumps(failure, ensure_ascii=False, indent=2))
            return EXIT_FAILURE
        return exit_code
    except BenchmarkInputError as exc:
        report = _error_report(args, status="input-error", field=exc.field, reason=exc.reason)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_INPUT
    except KeyboardInterrupt:
        report = _error_report(args, status="failure", reason="interrupted",
                               network_requests=progress["actual_network_requests"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_FAILURE
    except Exception:
        # 不把 traceback 当成成功，也不输出原始异常正文/路径/请求内容。
        report = _error_report(args, status="failure", reason="unexpected_failure",
                               network_requests=progress["actual_network_requests"])
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
