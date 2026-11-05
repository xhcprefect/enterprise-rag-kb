"""进程内轻量指标：HTTP 模板路由、流式问答生命周期与固定阶段耗时。

设计约束（务必保持）：

- **进程本地**：所有计数都在当前 Python 进程内，多个副本之间不共享，
  因此它不是"全局准入/并发"的真相，只能用于单进程观测与聚合后再汇总。
- **低基数**：标签只允许来自固定集合（方法、模板路由、状态码、模式、阶段）。
  任何看起来像原始路径/UUID/用户名/查询串的值都会被归一化，绝不进标签。
- **线程安全**：API 线程池、流式任务与测试可能并发更新，所有读写都在同一把
  ``RLock`` 内完成；渲染时基于快照，不长时间持锁。
- **不泄露业务数据**：模块不知道 user/request UUID、文档名、模型地址或密钥，
  调用方也没有接口把它们塞进来。

对外接口（A 在 ``chat.py`` 生命周期里使用）::

    from app.core.observability import start_chat

    observation = start_chat("rag")          # 生成开始：started+1，in_flight+1
    ...
    observation.finish(                      # 只生效一次（幂等）
        "completed",                         # completed / failed / cancelled
        first_token_ms=812.0,
        total_ms=5310.0,
        stages_ms={"retrieval": 120, "generation": 4800},
    )

``start_chat`` 只接受 ``chat`` / ``rag``，其余归入 ``other``；``finish`` 的
status 归入 ``completed`` / ``failed`` / ``cancelled`` / ``unknown``，未知阶段
归入 ``other``。负数、NaN、Inf 等非法耗时会直接丢弃，不会污染直方图。
"""
from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Iterable, Mapping

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# 固定阶段名；调用方多传的键归一化到 other，避免自由文本变成标签。
CHAT_STAGES: tuple[str, ...] = (
    "rewrite",
    "retrieval",
    "prompt",
    "first_token",
    "generation",
    "persistence",
)

CHAT_MODES: tuple[str, ...] = ("chat", "rag")
CHAT_FINISH_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled", "unknown")

_HTTP_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})
_COMPLETED = frozenset({"completed", "success", "ok", "done"})
_FAILED = frozenset({"failed", "error", "exception"})
_CANCELLED = frozenset({"cancelled", "canceled", "aborted", "cancel"})

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_MAX_ROUTE_LEN = 200

HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
FIRST_TOKEN_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)
TOTAL_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600)
STAGE_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)


def _finite(value: float | int | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        # 超大 int 转 float 会抛 OverflowError；不能让脏值打断真实请求。
        return None
    return number if math.isfinite(number) else None


def normalize_mode(value: str | None) -> str:
    """只保留固定模式；任何自由文本（含 UUID/用户名）归入 other。"""
    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in CHAT_MODES:
            return mode
    return "other"


def normalize_chat_status(value: str | None) -> str:
    if isinstance(value, str):
        status = value.strip().lower()
        if status in _COMPLETED:
            return "completed"
        if status in _CANCELLED:
            return "cancelled"
        if status in _FAILED:
            return "failed"
    return "unknown"


def normalize_stage(value: str | None) -> str:
    if isinstance(value, str):
        stage = value.strip().lower()
        if stage.endswith("_ms"):
            stage = stage[:-3]
        if stage in CHAT_STAGES:
            return stage
    return "other"


def normalize_method(value: str | None) -> str:
    method = value.strip().upper() if isinstance(value, str) else ""
    return method if method in _HTTP_METHODS else "other"


def sanitize_route(value: str | None) -> str:
    """只接受模板路由；原始 UUID 路径、查询串、超长路径一律归入 unknown。

    ``scope["route"]`` 由 Starlette 设为匹配到的 Route 对象，模板形如
    ``/api/v1/knowledge/{kb_id}``。没有匹配（404）时为 None → unknown。
    """
    if not isinstance(value, str):
        return "unknown"
    route = value.strip()
    if not route or len(route) > _MAX_ROUTE_LEN:
        return "unknown"
    if not route.startswith("/") or "?" in route or "#" in route or "\n" in route:
        return "unknown"
    if _UUID_RE.search(route):
        return "unknown"
    return route


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_text(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{key}="{_escape_label(str(val))}"' for key, val in labels) + "}"


class _Histogram:
    __slots__ = ("buckets", "counts", "sum", "count")

    def __init__(self, buckets: Iterable[float]) -> None:
        self.buckets = tuple(buckets)
        self.counts = [0] * len(self.buckets)
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        # 只计入第一个满足 value <= bound 的桶；渲染时逐桶累加得到累积计数，
        # 超出所有上界的观测只体现在 +Inf 与 count。
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[index] += 1
                break
        self.sum += value
        self.count += 1

    def freeze(self) -> tuple[tuple[float, ...], tuple[int, ...], float, int]:
        """在锁内调用：返回不可变快照，渲染期不再触碰可变状态。"""
        return (self.buckets, tuple(self.counts), self.sum, self.count)


def _render_histogram(
    name: str,
    labels: tuple[tuple[str, str], ...],
    frozen: tuple[tuple[float, ...], tuple[int, ...], float, int],
) -> list[str]:
    buckets, counts, total_sum, total_count = frozen
    base = _labels_text(labels)
    lines = []
    cumulative = 0
    # counts 记录"落入该桶"的数量；Prometheus 要求 bucket 为累积计数。
    for bound, amount in zip(buckets, counts):
        cumulative += amount
        bound_text = repr(float(bound))
        if base:
            bucket_labels = base[:-1] + f',le="{bound_text}"' + "}"
        else:
            bucket_labels = f'{{le="{bound_text}"}}'
        lines.append(f"{name}_bucket{bucket_labels} {cumulative}")
    infinite_labels = base[:-1] + ',le="+Inf"' + "}" if base else '{le="+Inf"}'
    lines.append(f"{name}_bucket{infinite_labels} {total_count}")
    lines.append(f"{name}_sum{base} {_format_float(total_sum)}")
    lines.append(f"{name}_count{base} {total_count}")
    return lines


def _format_float(value: float) -> str:
    # 避免科学计数法带来的解析差异；Prometheus 接受普通小数。
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


class _Metrics:
    """线程安全的进程内指标容器。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._http_in_flight = 0
        self._http_route_in_flight: dict[str, int] = {}
        self._http_requests: dict[tuple[str, str, str, str], int] = {}
        self._http_durations: dict[tuple[str, str, str], _Histogram] = {}
        self._chat_in_flight: dict[str, int] = {}
        self._chat_started: dict[str, int] = {}
        self._chat_finished: dict[tuple[str, str], int] = {}
        self._chat_first_token: dict[str, _Histogram] = {}
        self._chat_total: dict[str, _Histogram] = {}
        self._chat_stage: dict[tuple[str, str], _Histogram] = {}
        # 审计写入失败：刻意不设任何标签（action/用户/目标都不参与），
        # 只暴露"这一进程累计失败了多少次"。
        self._audit_write_failures = 0

    # ---- HTTP ---------------------------------------------------------
    def http_request_started(self) -> None:
        with self._lock:
            self._http_in_flight += 1

    def note_http_route(self, route: str) -> str | None:
        """响应开始、路由已确定时登记 per-route 在途；返回登记的标签。"""
        route = sanitize_route(route)
        with self._lock:
            self._http_route_in_flight[route] = self._http_route_in_flight.get(route, 0) + 1
        return route

    def http_request_finished(
        self,
        method: str,
        route: str,
        status: int,
        duration_seconds: float,
        *,
        streaming: bool = False,
        noted_route: str | None = None,
    ) -> None:
        method_label = normalize_method(method)
        route_label = sanitize_route(route)
        try:
            code = int(status)
        except (TypeError, ValueError, OverflowError):
            code = 0
        # 只保留真实 HTTP 状态码，其余固定为 000，避免标签基数被脏值撑开。
        status_label = str(code) if 100 <= code <= 599 else "000"
        duration = _finite(duration_seconds)
        with self._lock:
            if self._http_in_flight > 0:
                self._http_in_flight -= 1
            if noted_route is not None:
                remaining = self._http_route_in_flight.get(noted_route, 0) - 1
                if remaining > 0:
                    self._http_route_in_flight[noted_route] = remaining
                else:
                    self._http_route_in_flight.pop(noted_route, None)
            key = (method_label, route_label, status_label, "true" if streaming else "false")
            self._http_requests[key] = self._http_requests.get(key, 0) + 1
            # SSE 的响应体结束等于生成结束，但它不是"完整生成时长"的唯一来源；
            # 完整时长由 start_chat 的真实生命周期记录，这里不重复统计，避免误读。
            if not streaming and duration is not None and duration >= 0:
                hist_key = (method_label, route_label, status_label)
                histogram = self._http_durations.get(hist_key)
                if histogram is None:
                    histogram = _Histogram(HTTP_DURATION_BUCKETS)
                    self._http_durations[hist_key] = histogram
                histogram.observe(duration)

    # ---- Chat ---------------------------------------------------------
    def start_chat(self, mode: str | None) -> "ChatObservation":
        mode_label = normalize_mode(mode)
        with self._lock:
            self._chat_started[mode_label] = self._chat_started.get(mode_label, 0) + 1
            self._chat_in_flight[mode_label] = self._chat_in_flight.get(mode_label, 0) + 1
        return ChatObservation(self, mode_label)

    def _finish_chat(
        self,
        observation: "ChatObservation",
        status: str,
        first_token_ms: float | int | None,
        total_ms: float | int | None,
        stages_ms: Mapping[str, float | int] | None,
    ) -> bool:
        first_token = _finite(first_token_ms)
        total = _finite(total_ms)
        with self._lock:
            # 幂等判定与收口必须在同一把锁内完成：并发调用 finish 时只有一个线程
            # 能通过这里，其余线程直接返回 False，不会重复递减在途或重复计数。
            if observation._finished:
                return False
            observation._finished = True
            mode = observation.mode
            in_flight = self._chat_in_flight.get(mode, 0)
            if in_flight > 0:
                in_flight -= 1
            self._chat_in_flight[mode] = in_flight
            key = (mode, status)
            self._chat_finished[key] = self._chat_finished.get(key, 0) + 1

            if first_token is not None and first_token >= 0:
                histogram = self._chat_first_token.get(mode)
                if histogram is None:
                    histogram = _Histogram(FIRST_TOKEN_BUCKETS)
                    self._chat_first_token[mode] = histogram
                histogram.observe(first_token / 1000.0)
            if total is not None and total >= 0:
                histogram = self._chat_total.get(mode)
                if histogram is None:
                    histogram = _Histogram(TOTAL_BUCKETS)
                    self._chat_total[mode] = histogram
                histogram.observe(total / 1000.0)
            if stages_ms:
                for raw_stage, raw_value in stages_ms.items():
                    value = _finite(raw_value)
                    if value is None or value < 0:
                        continue
                    stage = normalize_stage(raw_stage)
                    stage_key = (mode, stage)
                    histogram = self._chat_stage.get(stage_key)
                    if histogram is None:
                        histogram = _Histogram(STAGE_BUCKETS)
                        self._chat_stage[stage_key] = histogram
                    histogram.observe(value / 1000.0)
        return True

    # ---- Audit --------------------------------------------------------
    def audit_write_failed(self) -> None:
        """记录一次审计写入失败；无标签，任何调用方都无法注入业务数据。"""
        with self._lock:
            self._audit_write_failures += 1

    # ---- Snapshot / rendering ----------------------------------------
    def snapshot(self) -> dict:
        """给管理员摘要用的结构化快照；不包含任何用户/请求标识。"""
        with self._lock:
            return {
                "process_local": True,
                "http": {
                    "in_flight": self._http_in_flight,
                    "route_in_flight": dict(self._http_route_in_flight),
                },
                "chat": {
                    "in_flight": dict(self._chat_in_flight),
                    "started": dict(self._chat_started),
                    "finished": {
                        f"{mode}:{status}": count
                        for (mode, status), count in self._chat_finished.items()
                    },
                },
                "audit": {
                    "write_failures": self._audit_write_failures,
                },
            }

    def reset(self) -> None:
        """清空全部指标；仅供测试使用。"""
        with self._lock:
            self._http_in_flight = 0
            self._http_route_in_flight.clear()
            self._http_requests.clear()
            self._http_durations.clear()
            self._chat_in_flight.clear()
            self._chat_started.clear()
            self._chat_finished.clear()
            self._chat_first_token.clear()
            self._chat_total.clear()
            self._chat_stage.clear()
            self._audit_write_failures = 0

    def render(self) -> str:
        with self._lock:
            http_in_flight = self._http_in_flight
            route_in_flight = dict(self._http_route_in_flight)
            http_requests = dict(self._http_requests)
            # 直方图在锁内冻结为不可变元组；锁外渲染不会再看到更新线程的写入。
            http_durations = {key: histogram.freeze() for key, histogram in self._http_durations.items()}
            chat_in_flight = dict(self._chat_in_flight)
            chat_started = dict(self._chat_started)
            chat_finished = dict(self._chat_finished)
            chat_first_token = {key: histogram.freeze() for key, histogram in self._chat_first_token.items()}
            chat_total = {key: histogram.freeze() for key, histogram in self._chat_total.items()}
            chat_stage = {key: histogram.freeze() for key, histogram in self._chat_stage.items()}
            audit_write_failures = self._audit_write_failures

        lines: list[str] = []

        def family(name: str, kind: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")

        family("ragkb_http_in_flight_requests", "gauge", "HTTP requests currently being handled in this process.")
        lines.append(f"ragkb_http_in_flight_requests {http_in_flight}")

        family("ragkb_http_route_in_flight", "gauge", "HTTP responses currently being sent, by matched route template (including SSE).")
        for route in sorted(route_in_flight):
            lines.append(f'ragkb_http_route_in_flight{{route="{_escape_label(route)}"}} {route_in_flight[route]}')

        family("ragkb_http_requests_total", "counter", "HTTP requests handled, by method, route template, status and stream flag.")
        for method, route, status, streaming in sorted(http_requests):
            labels = (("method", method), ("route", route), ("status", status), ("stream", streaming))
            lines.append(f"ragkb_http_requests_total{_labels_text(labels)} {http_requests[(method, route, status, streaming)]}")

        family(
            "ragkb_http_request_duration_seconds",
            "histogram",
            "Non-SSE HTTP response duration; stream generation duration comes from ragkb_chat_total_seconds.",
        )
        for method, route, status in sorted(http_durations):
            labels = (("method", method), ("route", route), ("status", status))
            lines.extend(
                _render_histogram(
                    "ragkb_http_request_duration_seconds", labels, http_durations[(method, route, status)]
                )
            )

        family("ragkb_chat_in_flight", "gauge", "Chat generations in flight in this process, by mode.")
        for mode in sorted(chat_in_flight):
            lines.append(f'ragkb_chat_in_flight{{mode="{_escape_label(mode)}"}} {chat_in_flight[mode]}')

        family("ragkb_chat_started_total", "counter", "Chat generations started, by mode.")
        for mode in sorted(chat_started):
            lines.append(f'ragkb_chat_started_total{{mode="{_escape_label(mode)}"}} {chat_started[mode]}')

        family("ragkb_chat_finished_total", "counter", "Chat generations finished exactly once, by mode and status.")
        for mode, status in sorted(chat_finished):
            labels = (("mode", mode), ("status", status))
            lines.append(f"ragkb_chat_finished_total{_labels_text(labels)} {chat_finished[(mode, status)]}")

        family("ragkb_chat_first_token_seconds", "histogram", "Latency to first generated token, by mode.")
        for mode in sorted(chat_first_token):
            labels = (("mode", mode),)
            lines.extend(_render_histogram("ragkb_chat_first_token_seconds", labels, chat_first_token[mode]))

        family("ragkb_chat_total_seconds", "histogram", "Full chat generation duration, by mode.")
        for mode in sorted(chat_total):
            labels = (("mode", mode),)
            lines.extend(_render_histogram("ragkb_chat_total_seconds", labels, chat_total[mode]))

        family("ragkb_chat_stage_seconds", "histogram", "Fixed chat stage durations, by mode and stage.")
        for mode, stage in sorted(chat_stage):
            labels = (("mode", mode), ("stage", stage))
            lines.extend(_render_histogram("ragkb_chat_stage_seconds", labels, chat_stage[(mode, stage)]))

        family(
            "ragkb_audit_write_failures_total",
            "counter",
            "Audit-log write failures in this process; unlabeled so no business data can become a label.",
        )
        # 始终输出（含 0），与队列状态指标一致：告警可以直接 increase(...) > 0。
        lines.append(f"ragkb_audit_write_failures_total {audit_write_failures}")

        family("ragkb_metrics_process_local", "gauge", "Constant 1; metrics are process-local and not shared across replicas.")
        lines.append("ragkb_metrics_process_local 1")
        return "\n".join(lines) + "\n"


class ChatObservation:
    """单次生成的生命周期句柄；``finish`` 幂等，只统计一次。"""

    __slots__ = ("_metrics", "mode", "_finished", "_started_perf")

    def __init__(self, metrics: _Metrics, mode: str) -> None:
        self._metrics = metrics
        self.mode = mode
        self._finished = False
        self._started_perf = time.perf_counter()

    def finish(
        self,
        status: str,
        first_token_ms: float | int | None = None,
        total_ms: float | int | None = None,
        stages_ms: Mapping[str, float | int] | None = None,
    ) -> bool:
        """结束一次生成，返回是否为首次结束。

        - 第二次及以后调用直接返回 ``False``，不会重复计数；
        - ``total_ms`` 省略时按观察对象创建到现在的墙钟耗时补记；
        - ``first_token_ms`` 省略表示未知（未生成首字），不补零。
        """
        if total_ms is None:
            total_ms = (time.perf_counter() - self._started_perf) * 1000.0
        # 幂等判定与计数都在 _finish_chat 的同一把 RLock 内完成；
        # 并发 finish 只有一个线程拿到 True。
        return self._metrics._finish_chat(
            self, normalize_chat_status(status), first_token_ms, total_ms, stages_ms
        )


metrics = _Metrics()


def start_chat(mode: str | None) -> ChatObservation:
    """开始一次生成观测；A 在真实生成开始时调用，结束时调用 ``finish``。"""
    return metrics.start_chat(mode)


class HttpMetricsMiddleware:
    """纯 ASGI 中间件：按模板路由统计计数/耗时/在途。

    原始 UUID 路径不会成为标签：路由取值来自 ``scope["route"]`` 的模板，
    没有匹配时固定为 ``unknown``。SSE 不记录耗时直方图——完整生成时长
    由 ``start_chat`` 的真实生命周期提供。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        state: dict = {"status": 500, "streaming": False, "noted_route": None}
        method = scope.get("method", "")

        metrics.http_request_started()

        def _route_template() -> str:
            # FastAPI 新版把"带 include_router 前缀的完整模板"放在
            # scope["fastapi"]["effective_route_context"].path_format；
            # scope["route"].path_format 只是路由器内的局部模板（少前缀）。
            context = (scope.get("fastapi") or {}).get("effective_route_context")
            template = getattr(context, "path_format", None)
            if template is None:
                route = scope.get("route")
                if route is None:
                    return "unknown"
                template = getattr(route, "path_format", None) or getattr(route, "path", None)
            return sanitize_route(template)

        async def send_wrapper(message) -> None:
            if message.get("type") == "http.response.start":
                try:
                    state["status"] = int(message.get("status", 500))
                except (TypeError, ValueError):
                    state["status"] = 500
                state["streaming"] = _is_event_stream(message.get("headers") or ())
                state["noted_route"] = metrics.note_http_route(_route_template())
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            metrics.http_request_finished(
                method,
                _route_template(),
                state["status"],
                time.perf_counter() - started,
                streaming=state["streaming"],
                noted_route=state["noted_route"],
            )


def _is_event_stream(headers) -> bool:
    for name, value in headers:
        if name.lower() == b"content-type":
            try:
                decoded = value.decode("latin-1").lower()
            except AttributeError:
                return False
            return decoded.startswith("text/event-stream")
    return False
