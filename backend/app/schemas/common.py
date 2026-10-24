"""统一响应结构。

约定（所有 ``/api/v1`` 下的 JSON 接口）：

成功::

    {"code": 0, "success": true, "message": "ok", "data": ...}

失败::

    {"code": <HTTP 状态码>, "success": false, "message": "具体原因", "data": null}

``code`` 为 0 表示业务成功；出错时与 HTTP 状态码保持一致，前端只认 ``success``
就能判断成败，``message`` 可直接展示给用户。

例外：SSE 流式问答（``/api/v1/chat/stream``，``text/event-stream``）与
``/health``、``/`` 等探活/基础设施接口不套用该结构，详见 README「接口返回规范」。
"""
from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    code: int = 0
    success: bool = True
    message: str = "ok"
    data: T | None = None

    model_config = {"from_attributes": True}


class PageResult(BaseModel, Generic[T]):
    total: int
    items: list[T]
