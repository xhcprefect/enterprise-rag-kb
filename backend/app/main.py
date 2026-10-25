"""应用入口。"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_router
from app.core.api_response import error_body, validation_message
from app.core.config import settings
from app.core.logging import setup_logging
from app.core.request_log import RequestLoggingMiddleware
from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.models import Document, DocumentStatus

setup_logging("app")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with SessionLocal() as db:
        queued = db.scalar(
            select(func.count()).select_from(Document).where(
                Document.status == DocumentStatus.PENDING.value
            )
        ) or 0
    if queued:
        logger.warning(
            "有 %d 个文档在等待入库；若 worker 未运行请执行 start-worker.bat", queued
        )
    logger.info(
        "服务已启动，LLM=%s(%s), Embedding=%s(%s)",
        settings.LLM_MODEL, settings.LLM_PROVIDER,
        settings.EMBEDDING_MODEL, settings.EMBEDDING_PROVIDER,
    )
    yield
    logger.info("服务已关闭")


def _worker_state() -> str:
    """推断 worker 状态：alive / stale / idle。

    - 有 processing 且心跳新鲜 → alive（worker 确实在处理）；
    - 有 pending 却没有任何新鲜心跳 → stale（没人来领取，worker 大概率没启动）；
    - 队列空 → idle（无法观测，但不能谎报存活）。

    旧实现把"空闲"直接当成 alive，导致 worker 根本没运行时 /health 仍然报正常。
    """
    deadline = datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS)
    with SessionLocal() as db:
        active = db.scalar(
            select(func.count()).select_from(Document).where(
                Document.status == DocumentStatus.PROCESSING.value,
                Document.heartbeat_at.is_not(None),
                Document.heartbeat_at >= deadline,
            )
        ) or 0
        queued = db.scalar(
            select(func.count()).select_from(Document).where(
                Document.status.in_(
                    [DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value]
                )
            )
        ) or 0
    if active:
        return "alive"
    if queued:
        return "stale"
    return "idle"


# 接口文档只在 DEBUG=true 时注册：传 None 让 FastAPI 根本不挂这几条路由，
# 而不是挂上去再在视图里判断——少一条路由就少一个可被探测的端点。
_docs_kwargs = (
    {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    if settings.DEBUG
    else {"docs_url": None, "redoc_url": None, "openapi_url": None}
)

app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    lifespan=lifespan,
    **_docs_kwargs,
)

# 注意顺序：后添加的中间件先执行。请求日志放在最外层，才能把 CORS 预检之外
# 的所有请求（含被鉴权拦下的 401/403）都记下来。
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],
)
app.add_middleware(RequestLoggingMiddleware)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """统一 HTTP 异常：返回 code/success/message/data，并把原因挂到请求日志。"""
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    request.state.error_detail = detail
    if exc.status_code >= 500:
        logger.error("%s %s 业务异常 %d: %s", request.method, request.url.path, exc.status_code, detail)
    elif exc.status_code >= 400:
        logger.warning("%s %s 请求被拒绝 %d: %s", request.method, request.url.path, exc.status_code, detail)
    headers = getattr(exc, "headers", None) or {}
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.status_code, detail),
        headers=headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """参数校验失败：message 给出可直接阅读的字段错误，data 保留原始校验明细。"""
    errors = jsonable_encoder(exc.errors())
    request.state.error_detail = f"参数校验失败: {errors}"
    logger.warning(
        "%s %s 参数校验失败: %s", request.method, request.url.path, errors
    )
    return JSONResponse(
        status_code=422,
        content=error_body(422, validation_message(errors), errors),
    )


@app.exception_handler(IntegrityError)
async def integrity_exception_handler(request: Request, exc: IntegrityError):
    """数据库唯一/外键约束冲突：转成可读的 400，而不是裸 500。"""
    detail = str(getattr(exc, "orig", exc))
    logger.warning(
        "%s %s 数据约束冲突: %s", request.method, request.url.path, detail
    )
    request.state.error_detail = detail
    message = "数据冲突：名称或唯一字段已存在" if "unique" in detail.lower() else "数据约束冲突"
    return JSONResponse(status_code=400, content=error_body(400, message))


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """兜底：任何未预期异常都返回统一结构，避免前端拿到空响应体。"""
    logger.exception("%s %s 未捕获异常", request.method, request.url.path)
    request.state.error_detail = str(exc)
    return JSONResponse(
        status_code=500,
        content=error_body(500, "服务器内部错误，请稍后重试"),
    )


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    """探活接口，公网可达，只回最小信息。

    以前还返回 app 名、模型名与服务商，等于把部署拓扑向未登录访客公开；
    监控只需要知道"活着 + worker 状态"，其余字段一律不外泄。
    """
    return {"status": "ok", "worker": _worker_state()}


@app.get("/", include_in_schema=False)
def root():
    if settings.DEBUG:
        return {"message": settings.APP_NAME, "docs": "/docs"}
    return {"message": settings.APP_NAME}
