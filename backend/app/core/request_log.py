"""请求日志中间件：每个 API 请求输出一行结构化日志。

记录内容：方法、路径、状态码、耗时、来源 IP、当前用户、业务错误原因。
用途：线上排查"谁在什么时候调了什么、为什么失败、慢在哪"。

日志级别约定：
- 2xx/3xx → INFO
- 4xx     → WARNING（客户端错误，需要关注但不告警）
- 5xx     → ERROR（带堆栈）
- 慢请求  → 至少 WARNING（阈值 ``LOG_SLOW_REQUEST_MS``）
"""
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import settings

logger = logging.getLogger("app.request")

# 这些路径是探活/文档，噪声大且无排查价值，压到 DEBUG
_QUIET_PATHS = {"/health", "/", "/docs", "/redoc", "/openapi.json"}


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "-"


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        # 先取路径：后面 request.body() 之类的读取不影响它
        path = request.url.path
        if path in _QUIET_PATHS:
            return await call_next(request)

        request_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()
        ip = _client_ip(request)

        # 让下游（异常处理、业务日志）能关联到同一请求
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            logger.exception(
                "%s %s -> 500 (%.0fms) ip=%s req=%s 未捕获异常",
                request.method, path, elapsed, ip, request_id,
            )
            raise

        elapsed = (time.perf_counter() - started) * 1000
        status = response.status_code

        user = getattr(request.state, "user", None)
        who = user.username if user is not None else "-"
        query = f"?{request.url.query}" if request.url.query else ""
        args = (request.method, path, query, status, elapsed, who, ip, request_id)

        if status >= 500:
            logger.error(
                "%s %s%s -> %d (%.0fms) user=%s ip=%s req=%s", *args
            )
        elif status >= 400:
            # 业务错误原因通常由路由写进 response body，这里附上便于定位
            logger.warning(
                "%s %s%s -> %d (%.0fms) user=%s ip=%s req=%s", *args
            )
            detail = getattr(request.state, "error_detail", None)
            if detail:
                logger.warning("  └ 原因: %s", detail)
        elif elapsed > settings.LOG_SLOW_REQUEST_MS:
            logger.warning(
                "%s %s%s -> %d (%.0fms) user=%s ip=%s req=%s [慢请求 >%dms]",
                *args, settings.LOG_SLOW_REQUEST_MS,
            )
        else:
            logger.info(
                "%s %s%s -> %d (%.0fms) user=%s ip=%s req=%s", *args
            )

        response.headers["X-Request-ID"] = request_id
        return response
