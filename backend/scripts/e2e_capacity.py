"""有界容量探针：只打本仓库自己的 E2E 实验环境，不做压测、不做 DoS。

它是"链路是否在并发下仍然正确"的观测工具，不是性能验收：

* 必须提供 ``e2e_env.py`` 写出的 ``manifest.json``；
* manifest 里的 API/模型地址必须都是回环地址，否则直接拒绝；
* 必须能用模型替身的 ``/__control/state`` 证明"模型是本次本地替身"；
* 必须能用 API 的 ``/__e2e__/attestation`` 证明"这个 API 也是本次启动的"——
  只有 ``/health`` 返回 ok 的陌生服务会被拒绝，且**一个聊天请求都不会发出**；
* manifest 需未过期（默认 1 小时）且 API ``/health`` 可达，避免打到一个陈旧地址；
* 并发/请求数/总时长都有硬上限，默认就是小样本（并发 4、12 个请求、15 秒）。

总时长是**硬上限**：截止时间一到，在途 SSE 也会立刻放弃（socket 级超时按剩余
时间收紧），不会被一个每 20ms 发心跳、永不结束的慢流无限拖住。

结果语义：
* ``success``   —— 收到完整 SSE（``done`` 事件或正常结束）；
* ``rejected``  —— HTTP 429（入口限流按"被拒绝"统计，不混入模型故障）；
* ``error``     —— 其它 HTTP 错误、流内 ``error`` 事件、超时或连接失败。

报告只描述"本地替身 + 本机单进程 API"这一条链路，不能外推为真实模型吞吐。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import numbers
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from e2e_env import (  # noqa: E402
    assert_loopback_url,
    http_json,
    verify_attestation,
    verify_stub_owner,
)

DEFAULT_CONCURRENCY = 4
DEFAULT_REQUESTS = 12
DEFAULT_DURATION = 15.0

# 硬上限：无论命令行传什么都不会越过。工具只服务本地小样本。
HARD_MAX_CONCURRENCY = 8
HARD_MAX_REQUESTS = 40
HARD_MAX_DURATION = 30.0
HARD_REQUEST_TIMEOUT = 30.0

DEFAULT_USERNAME = "e2e-admin"
DEFAULT_PASSWORD = "E2eAdmin!2026"
MAX_MANIFEST_AGE_SECONDS = 3600.0


class ConfigError(RuntimeError):
    """配置/环境校验失败：不发起任何负载。"""


def _finite(name: str, value: Any, *, ceiling: float, integer: bool = False) -> float:
    """拒绝 NaN/Inf/布尔/非数字——``nan <= 0`` 是 False，只比大小拦不住它。"""
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ConfigError(f"{name} 必须是数字，收到 {value!r}")
    if integer and not isinstance(value, numbers.Integral):
        raise ConfigError(f"{name} 必须是整数，收到 {value!r}")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{name} 必须是有限数，收到 {value!r}")
    if number <= 0:
        raise ConfigError(f"{name} 必须为正数，收到 {value!r}")
    if number > ceiling:
        raise ConfigError(f"{name}={value} 超过本工具的硬上限 {ceiling}；容量探针只做小样本")
    return number


@dataclass(frozen=True)
class Limits:
    concurrency: int
    requests: int
    duration: float
    request_timeout: float

    @classmethod
    def build(cls, concurrency: int, requests: int, duration: float, request_timeout: float) -> "Limits":
        concurrency_v = _finite("concurrency", concurrency, ceiling=HARD_MAX_CONCURRENCY, integer=True)
        requests_v = _finite("requests", requests, ceiling=HARD_MAX_REQUESTS, integer=True)
        duration_v = _finite("duration", duration, ceiling=HARD_MAX_DURATION)
        timeout_v = _finite("request_timeout", request_timeout, ceiling=HARD_REQUEST_TIMEOUT)
        if requests_v < concurrency_v:
            raise ConfigError("requests 不能小于 concurrency，否则并发上限没有意义")
        return cls(int(concurrency_v), int(requests_v), duration_v, timeout_v)


@dataclass
class Sample:
    index: int
    outcome: str
    status: int | None
    elapsed_ms: float
    first_event_ms: float | None
    error: str | None = None


def load_manifest(path: Path, *, max_age: float = MAX_MANIFEST_AGE_SECONDS) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"manifest 不存在: {path}；先用 e2e_env.py up 启动环境")
    age = time.time() - path.stat().st_mtime
    if age > max_age:
        raise ConfigError(f"manifest 已过期({age:.0f}s > {max_age:.0f}s)，请重新启动 E2E 环境")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"manifest 不是合法 JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ConfigError("manifest 不是对象")
    owner = manifest.get("owner")
    if not isinstance(owner, str) or not owner:
        raise ConfigError("manifest 缺少 owner 标记，拒绝在无主环境上施压")
    nonce = manifest.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        raise ConfigError("manifest 缺少 nonce；该环境不是当前 harness 启动的，拒绝继续")
    api = (manifest.get("api") or {}).get("base_url")
    stub = (manifest.get("stub") or {}).get("base_url")
    if not api or not stub:
        raise ConfigError("manifest 缺少 api/stub 地址")
    # assert_loopback_url 抛的是 ValueError；这里必须转成 ConfigError，
    # 否则 main() 的“拒绝而非崩溃”分支接不住，工具会以 traceback 退出。
    for url, label in ((api, "manifest.api"), (stub, "manifest.stub")):
        try:
            assert_loopback_url(url, label)
        except ValueError as exc:
            raise ConfigError(f"{label} 未通过回环校验: {exc}") from exc
    return manifest


def _normalized_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    default = 443 if scheme == "https" else 80
    netloc = host if port in (None, default) else f"{host}:{port}"
    path = parsed.path or "/"
    return f"{scheme}://{netloc}{path}"


def attestation_url(manifest: dict[str, Any]) -> str:
    """attestation 必须由**实际要 POST 的 API 基址**派生。

    不能优先相信 manifest 里独立声明的 ``attestation.api_url``：否则可以把
    "A 的 attestation" 和 "B 的 api.base_url" 拼在一起，替身也能被独立 A 背书，
    校验通过但流量打到陌生服务。声明值只允许与派生值完全一致。
    """
    api = manifest["api"]["base_url"].rstrip("/")
    derived = api + "/__e2e__/attestation"
    declared = (manifest.get("attestation") or {}).get("api_url")
    if declared and _normalized_url(str(declared)) != _normalized_url(derived):
        raise ConfigError(
            f"manifest.attestation.api_url 与实际 POST 目标不一致：{declared!r} != {derived!r}"
        )
    return derived


def stub_state_url(manifest: dict[str, Any]) -> str:
    """替身控制端点同理：只能从 manifest.stub.base_url 派生。"""
    stub_base = manifest["stub"]["base_url"].rstrip("/")
    derived = stub_base.rsplit("/v1", 1)[0] + "/__control/state"
    declared = (manifest.get("attestation") or {}).get("stub_state_url")
    if declared and _normalized_url(str(declared)) != _normalized_url(derived):
        raise ConfigError(
            f"manifest.attestation.stub_state_url 与 stub.base_url 不一致：{declared!r} != {derived!r}"
        )
    return derived


def verify_environment(manifest: dict[str, Any], *, expect_owner: str | None = None) -> dict[str, Any]:
    """确认目标是我们自己的 lab：owner/nonce 双向绑定、替身控制端点、API 健康。

    仅靠 ``/health`` 无法证明 API 属于本次运行（生产 API 也会返回 ok），因此这里
    要求 API 的 attestation 与替身状态里的 owner/nonce **同时**匹配 manifest。
    任何一项不符都在登录与聊天之前抛出 ``ConfigError``：零聊天 POST。
    """
    owner = manifest["owner"]
    nonce = manifest["nonce"]
    if expect_owner and expect_owner != owner:
        raise ConfigError(f"owner 不匹配：manifest={owner} 期望={expect_owner}")
    stub_base = manifest["stub"]["base_url"].rstrip("/")

    # 先做地址一致性，再做任何网络请求：错配的 manifest 连探针都不发。
    control = stub_state_url(manifest)
    attest = attestation_url(manifest)
    try:
        assert_loopback_url(attest, "attestation.api_url")
        assert_loopback_url(control, "attestation.stub_state_url")
    except ValueError as exc:
        raise ConfigError(f"attestation 地址未通过回环校验: {exc}") from exc

    try:
        state = http_json(control, timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"模型替身控制端点不可用，拒绝在未知模型上施压: {exc}") from exc
    if not isinstance(state, dict) or "counts" not in state:
        raise ConfigError("控制端点响应不像本仓库的模型替身，拒绝继续")
    try:
        verify_stub_owner(state, owner=owner, nonce=nonce)
    except ValueError as exc:
        raise ConfigError(f"替身不属于本次运行: {exc}") from exc

    try:
        attestation = http_json(attest, timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            f"API 归属校验不可用（只有 /health 的服务不能作为目标）: {exc}"
        ) from exc
    try:
        verify_attestation(attestation, owner=owner, nonce=nonce, stub_base_url=stub_base)
    except ValueError as exc:
        raise ConfigError(f"API 不属于本次运行: {exc}") from exc

    api = manifest["api"]["base_url"].rstrip("/")
    try:
        health = http_json(f"{api}/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"API 健康检查失败，manifest 可能已陈旧: {exc}") from exc
    if health.get("status") != "ok":
        raise ConfigError(f"API 未就绪: {health}")
    return {"control_url": control, "attestation_url": attest,
            "stub_counts": state.get("counts", {})}


def login(api: str, username: str, password: str) -> str:
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    request = urllib.request.Request(f"{api}/api/v1/auth/login", data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ConfigError(f"登录失败 HTTP {exc.code}；请确认环境由 e2e_env.py 启动") from exc
    return body["data"]["access_token"]


MAX_STREAM_BYTES = 4 * 1024 * 1024
_READ_LINE_LIMIT = 1024 * 1024


async def _read_with_deadline(reader: asyncio.StreamReader, size: int, deadline: float) -> bytes:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError("deadline")
    return await asyncio.wait_for(reader.read(size), timeout=budget)


async def _read_exactly_with_deadline(reader: asyncio.StreamReader, size: int, deadline: float) -> bytes:
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError("deadline")
    if size <= 0:
        return b""
    return await asyncio.wait_for(reader.readexactly(size), timeout=budget)


async def _readline_with_deadline(reader: asyncio.StreamReader, deadline: float) -> bytes:
    """读一行；每次读都只给"剩余时间"，滴字节不换行会在这里被取消。"""
    budget = deadline - time.monotonic()
    if budget <= 0:
        raise TimeoutError("deadline")
    return await asyncio.wait_for(reader.readline(), timeout=budget)


async def _iter_body_lines(reader: asyncio.StreamReader, headers: dict[str, bytes],
                           deadline: float):
    """产出响应体里的每一行；chunked 与 content-length 都按剩余时间约束。

    覆盖三类"只给单次 IO 超时会被拖穿"的慢速形态：
    * 慢响应头 → ``_readline_with_deadline``；
    * 无换行滴字节 → ``readline`` 被 ``wait_for`` 取消；
    * 慢 chunk 元数据/慢 chunk 数据 → 每个 chunk-size 与 chunk 数据都受剩余时间约束。
    """
    buffer = b""
    total = 0
    chunked = b"chunked" in headers.get("transfer-encoding", b"").lower()

    def take_lines() -> list[bytes]:
        nonlocal buffer
        lines = []
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            lines.append(line)
        return lines

    if chunked:
        while True:
            size_line = await _readline_with_deadline(reader, deadline)
            if not size_line:
                break
            token = size_line.strip().split(b";")[0].strip()
            if not token:
                continue
            try:
                size = int(token, 16)
            except ValueError as exc:
                raise ValueError(f"非法的 chunk-size: {token!r}") from exc
            if size < 0 or size > MAX_STREAM_BYTES:
                raise ValueError(f"chunk-size 超界: {size}")
            if size == 0:
                while True:  # trailer
                    trailer = await _readline_with_deadline(reader, deadline)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                break
            data = await _read_exactly_with_deadline(reader, size, deadline)
            total += len(data)
            if total > MAX_STREAM_BYTES:
                raise ValueError("响应体超过容量探针上限")
            buffer += data
            # chunk 数据后的 CRLF 也必须受时间约束（慢 chunk 尾部同样能拖时间）。
            await _read_exactly_with_deadline(reader, 2, deadline)
            for line in take_lines():
                yield line
    else:
        length_header = headers.get("content-length")
        if length_header is not None:
            length = int(length_header)
            if length < 0 or length > MAX_STREAM_BYTES:
                raise ValueError(f"content-length 超界: {length}")
            buffer = await _read_exactly_with_deadline(reader, length, deadline)
            for line in take_lines():
                yield line
        else:
            while True:
                data = await _read_with_deadline(reader, 65536, deadline)
                if not data:
                    break
                total += len(data)
                if total > MAX_STREAM_BYTES:
                    raise ValueError("响应体超过容量探针上限")
                buffer += data
                for line in take_lines():
                    yield line
    if buffer.strip():
        yield buffer


async def _post_stream_async(api: str, token: str, payload: dict[str, Any], *,
                             deadline: float, request_timeout: float) -> Sample:
    """一次问答请求：单个事件循环里的可取消客户端，总时长是**硬期限**。

    不用线程池 + ``urlopen(timeout=...)``：后者只约束单次 IO，服务端每 20ms
    滴一个字节就能无限拖下去；``shutdown(wait=False)`` 也不保证线程真的停下。
    这里每个 ``await`` 都被剩余时间包裹，期限一到 ``wait_for`` 会取消传输并关闭
    socket，函数返回后不存在任何仍在读/写/发 POST 的任务。
    """
    index = int(payload.pop("_index", 0))
    started = time.monotonic()
    hard_deadline = min(started + request_timeout, deadline)
    parsed = urllib.parse.urlsplit(api)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = (parsed.path or "").rstrip("/") + "/api/v1/chat/stream"
    body = json.dumps(payload).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Content-Type: application/json\r\n"
        "Accept: text/event-stream\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("utf-8") + body

    writer: asyncio.StreamWriter | None = None
    first_event: float | None = None
    try:
        budget = hard_deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("deadline")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=_READ_LINE_LIMIT), timeout=budget)
        writer.write(request)
        budget = hard_deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("deadline")
        await asyncio.wait_for(writer.drain(), timeout=budget)

        status_line = await _readline_with_deadline(reader, hard_deadline)
        if not status_line:
            raise ConnectionError("服务器在响应头之前关闭连接")
        parts = status_line.decode("latin-1").split()
        if len(parts) < 2 or not parts[1].isdigit():
            raise ValueError(f"非法状态行: {status_line[:80]!r}")
        status = int(parts[1])

        headers: dict[str, bytes] = {}
        while True:
            line = await _readline_with_deadline(reader, hard_deadline)
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.partition(b":")
            headers[name.strip().lower().decode("latin-1")] = value.strip()
        if status == 429:
            return Sample(index, "rejected", status, (time.monotonic() - started) * 1000, None, None)
        if status >= 400:
            return Sample(index, "error", status, (time.monotonic() - started) * 1000,
                          None, f"HTTP {status}")

        saw_done = False
        saw_error: str | None = None
        async for raw in _iter_body_lines(reader, headers, hard_deadline):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if first_event is None:
                first_event = (time.monotonic() - started) * 1000
            if line.startswith("event: "):
                event = line.split(":", 1)[1].strip()
                if event == "done":
                    saw_done = True
                elif event == "error" and saw_error is None:
                    saw_error = "stream-error"
        elapsed = (time.monotonic() - started) * 1000
        if time.monotonic() > hard_deadline + 0.05:
            # 真发生就是 deadline 逻辑有漏：宁可报错也不谎报成功。
            return Sample(index, "error", status, elapsed, first_event, "deadline-exceeded")
        if saw_error:
            return Sample(index, "error", status, elapsed, first_event, saw_error)
        outcome = "success" if saw_done else "error"
        return Sample(index, outcome, status, elapsed, first_event,
                      None if saw_done else "stream-without-done")
    except (TimeoutError, asyncio.TimeoutError):
        return Sample(index, "error", None, (time.monotonic() - started) * 1000,
                      first_event, "timeout:deadline")
    except (ConnectionError, asyncio.IncompleteReadError) as exc:
        return Sample(index, "error", None, (time.monotonic() - started) * 1000,
                      first_event, f"connection:{type(exc).__name__}")
    except Exception as exc:  # noqa: BLE001
        return Sample(index, "error", None, (time.monotonic() - started) * 1000,
                      first_event, f"{type(exc).__name__}: {exc}")
    finally:
        if writer is not None:
            # 关闭也要有界：对端不读时不阻塞收口。
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass


def _post_stream(api: str, token: str, payload: dict[str, Any], *,
                 deadline: float, request_timeout: float) -> Sample:
    """同步包装：只给单测/单条调试用；并发探针走 ``_run_probe_async``。"""
    return asyncio.run(_post_stream_async(api, token, payload, deadline=deadline,
                                          request_timeout=request_timeout))


def percentile(values: list[float], ratio: float) -> float | None:
    """最近秩百分位：小样本下比插值更诚实，也不会给出不存在的观测值。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(ratio * len(ordered) + 0.5))))
    return round(ordered[rank - 1], 1)


async def _async_login(api: str, username: str, password: str, *, timeout: float = 15.0) -> str:
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    deadline = time.monotonic() + timeout
    parsed = urllib.parse.urlsplit(f"{api}/api/v1/auth/login")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    request = (
        f"POST {parsed.path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Content-Type: application/json\r\nConnection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("utf-8") + body
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=_READ_LINE_LIMIT), timeout=timeout)
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        status_line = await _readline_with_deadline(reader, deadline)
        status = int(status_line.split()[1])
        headers: dict[str, bytes] = {}
        while True:
            line = await _readline_with_deadline(reader, deadline)
            if line in (b"\r\n", b"\n", b""):
                break
            name, _, value = line.partition(b":")
            headers[name.strip().lower().decode("latin-1")] = value.strip()
        payload_bytes = b""
        async for raw in _iter_body_lines(reader, headers, deadline):
            payload_bytes += raw + b"\n"
        if status >= 400:
            raise ConfigError(f"登录失败 HTTP {status}；请确认环境由 e2e_env.py 启动")
        data = json.loads(payload_bytes.decode("utf-8"))
        return str(data["data"]["access_token"])
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except Exception:  # noqa: BLE001
                pass


async def _run_probe_async(*, manifest: dict[str, Any], limits: Limits, token: str,
                           mode: str, kb_id: str | None, question: str) -> tuple[list[Sample],
                                                                                  int, float]:
    api = manifest["api"]["base_url"].rstrip("/")
    started = time.monotonic()
    deadline = started + limits.duration
    samples: list[Sample] = []
    issued = 0
    semaphore = asyncio.Semaphore(limits.concurrency)

    async def one(index: int) -> None:
        nonlocal issued
        if time.monotonic() >= deadline:
            return
        async with semaphore:
            if time.monotonic() >= deadline:
                return
            issued += 1
            payload = {"_index": index, "question": question, "mode": mode,
                       "kb_id": kb_id, "request_id": str(uuid.uuid4())}
            samples.append(await _post_stream_async(
                api, token, payload, deadline=deadline,
                request_timeout=limits.request_timeout))

    tasks = [asyncio.create_task(one(index)) for index in range(limits.requests)]
    budget = max(0.0, deadline - time.monotonic()) + 0.5
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=budget)
    except (TimeoutError, asyncio.TimeoutError):
        # 理论上 _post_stream_async 自己会在硬期限收口；这里只是最后的保险，
        # 取消后不再有任务在发 POST。
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return samples, issued, time.monotonic() - started


def run_probe(*, manifest: dict[str, Any], limits: Limits, username: str, password: str,
              mode: str, kb_id: str | None, question: str) -> dict[str, Any]:
    """发起有界小样本。调用前必须已通过 ``verify_environment``。"""
    api = manifest["api"]["base_url"].rstrip("/")
    if mode == "rag" and not kb_id:
        raise ConfigError("mode=rag 需要 --kb-id")
    token = asyncio.run(_async_login(api, username, password))

    before = http_json(stub_state_url(manifest), timeout=5)
    samples, issued, elapsed_total = asyncio.run(_run_probe_async(
        manifest=manifest, limits=limits, token=token, mode=mode, kb_id=kb_id,
        question=question))
    after = http_json(stub_state_url(manifest), timeout=5)
    model_calls = int(after.get("counts", {}).get("chat_stream_calls", 0)) - int(
        before.get("counts", {}).get("chat_stream_calls", 0)
    )
    elapsed_ms = [sample.elapsed_ms for sample in samples]
    first_ms = [sample.first_event_ms for sample in samples if sample.first_event_ms is not None]
    counts = {"success": 0, "rejected": 0, "error": 0}
    for sample in samples:
        counts[sample.outcome] = counts.get(sample.outcome, 0) + 1
    total = len(samples)
    error_messages = sorted({sample.error for sample in samples if sample.error})
    return {
        "kind": "e2e-capacity-probe",
        "owner": manifest["owner"],
        "nonce": manifest["nonce"],
        "environment": {
            "api": api,
            "stub": manifest["stub"]["base_url"],
            "worker_pid": (manifest.get("worker") or {}).get("pid"),
            "attestation": "owner+nonce 已与 manifest 双向核验",
        },
        "limits": asdict(limits),
        "duration_seconds": round(elapsed_total, 3),
        # 硬期限 + 有界收口：允许 0.5s 的调度余量，不再是 +5s 的橡皮尺。
        "deadline_respected": elapsed_total <= limits.duration + 0.5,
        "issued": issued,
        "not_issued": limits.requests - issued,
        "completed": total,
        "counts": counts,
        "rates": {
            "success": round(counts["success"] / total, 4) if total else None,
            "rejected": round(counts["rejected"] / total, 4) if total else None,
            "error": round(counts["error"] / total, 4) if total else None,
        },
        "latency_ms": {
            "p50": percentile(elapsed_ms, 0.50),
            "p95": percentile(elapsed_ms, 0.95),
            "max": round(max(elapsed_ms), 1) if elapsed_ms else None,
            "first_event_p50": percentile(first_ms, 0.50),
        },
        "model_stream_calls": model_calls,
        "errors": error_messages,
        "limitations": [
            "模型只有回环确定性替身：延迟与吞吐不代表真实模型服务。",
            "API 是单机单进程 uvicorn，本报告不能外推为生产容量。",
            "这是有界小样本探针，不是压测，不用于 DoS 或容量承诺。",
            "429 计入 rejected（入口限流），不视为模型故障。",
            "总时长是硬上限：到点后仍在途的流会被取消并计入 error(timeout)。",
            "未覆盖：真实模型网络抖动、多机部署、跨进程 worker 扩容。",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E2E 有界容量探针（仅本地替身链路）")
    parser.add_argument("--manifest", required=True, help="e2e_env.py up 写出的 manifest.json")
    parser.add_argument("--expect-owner", default="", help="必须与 manifest.owner 一致")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION,
                        help="总时长硬上限（秒）")
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--mode", choices=["chat", "rag"], default="chat")
    parser.add_argument("--kb-id", default=None)
    parser.add_argument("--question", default="容量探针：请用一句话确认链路可用。")
    parser.add_argument("--username", default=os.environ.get("RAG_E2E_USERNAME", DEFAULT_USERNAME))
    parser.add_argument("--password", default=os.environ.get("RAG_E2E_PASSWORD", DEFAULT_PASSWORD))
    parser.add_argument("--out", default="", help="可选：把 JSON 报告写到该路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        limits = Limits.build(args.concurrency, args.requests, args.duration, args.request_timeout)
        manifest = load_manifest(Path(args.manifest))
        verify_environment(manifest, expect_owner=args.expect_owner or None)
        report = run_probe(manifest=manifest, limits=limits, username=args.username,
                           password=args.password, mode=args.mode, kb_id=args.kb_id,
                           question=args.question)
    except ConfigError as exc:
        print(json.dumps({"kind": "e2e-capacity-probe", "status": "refused", "reason": str(exc)},
                         ensure_ascii=False, indent=2))
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
    print(payload)
    return 0 if report["counts"]["error"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
