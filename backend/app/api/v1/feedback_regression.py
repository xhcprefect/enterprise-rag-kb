"""反馈回归接口：纳入、准备重跑、当前轮导出、历史与报告导入。

所有路由都要求管理员；GET 详情/历史不写审计（避免浏览产生大量记录），
显式导出当前轮 JSONL 与导入报告写入白名单审计。响应不返回问题/答案/引用
或报告正文。
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session as DBSession

from app.api.deps import require_admin, write_audit
from app.db.session import get_db
from app.models import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.feedback_regression import (
    FeedbackRegressionCreateRequest,
    FeedbackRegressionImportRead,
    FeedbackRegressionPrepareRead,
    FeedbackRegressionPrepareRequest,
    FeedbackRegressionRead,
    FeedbackRegressionRunRead,
)
from app.services import feedback_regression
from app.services.feedback_regression import REPORT_MAX_BYTES

router = APIRouter(prefix="/feedback", tags=["feedback"])


async def _read_report_body(request: Request) -> bytes:
    """从原始请求流读取报告，最多 2 MiB；超限在任何解析前返回 413。"""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="Content-Length 不合法（content_length_invalid）"
            ) from None
        if length < 0:
            raise HTTPException(
                status_code=400, detail="Content-Length 不合法（content_length_invalid）"
            )
        if length > REPORT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"评测报告不能超过 {REPORT_MAX_BYTES} 字节（report_too_large）",
            )
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > REPORT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"评测报告不能超过 {REPORT_MAX_BYTES} 字节（report_too_large）",
            )
    return bytes(raw)


@router.get(
    "/{feedback_id}/regression",
    response_model=ApiResponse[FeedbackRegressionRead | None],
    summary="反馈的回归纳入状态与当前可用性（管理员）",
)
def get_feedback_regression(
    feedback_id: uuid.UUID,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    data = feedback_regression.get_regression(db, admin, feedback_id)
    return ApiResponse(data=data)


@router.post(
    "/{feedback_id}/regression",
    response_model=ApiResponse[FeedbackRegressionRead],
    summary="将单条已核实反馈纳入回归（管理员）",
)
def enroll_feedback_regression(
    feedback_id: uuid.UUID,
    payload: FeedbackRegressionCreateRequest,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = feedback_regression.enroll(db, admin, feedback_id, payload)
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_enroll",
        str(feedback_id),
        {"cause": data["cause"], "revision": data["revision"]},
    )
    return ApiResponse(data=data)


@router.get(
    "/{feedback_id}/regression/runs",
    response_model=ApiResponse[PageResult[FeedbackRegressionRunRead]],
    summary="回归重跑历史（管理员）",
)
def list_feedback_regression_runs(
    feedback_id: uuid.UUID,
    response: Response,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    total, items = feedback_regression.list_runs(
        db, admin, feedback_id, page=page, page_size=page_size
    )
    return ApiResponse(
        data=PageResult(total=total, items=items)
    )


@router.post(
    "/{feedback_id}/regression/runs",
    response_model=ApiResponse[FeedbackRegressionPrepareRead],
    summary="准备下一轮重跑并返回单条 JSONL（管理员）",
)
def prepare_feedback_regression_run(
    feedback_id: uuid.UUID,
    payload: FeedbackRegressionPrepareRequest,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    data = feedback_regression.prepare_run(db, admin, feedback_id, payload)
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_prepare",
        str(data["run"]["run_id"]),
        {
            "attempt_no": data["run"]["attempt_no"],
            "revision": data["regression_revision"],
        },
    )
    return ApiResponse(data=data)


@router.get(
    "/{feedback_id}/regression/runs/{run_id}/export",
    response_model=ApiResponse[FeedbackRegressionPrepareRead],
    summary="重新下载当前轮已绑定的 JSONL（管理员）",
)
def export_feedback_regression_run(
    feedback_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    data = feedback_regression.export_run(db, admin, feedback_id, run_id)
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_export",
        str(run_id),
        {"attempt_no": data["run"]["attempt_no"], "case_count": data["case_count"]},
    )
    return ApiResponse(data=data)


@router.post(
    "/{feedback_id}/regression/runs/{run_id}/import",
    response_model=ApiResponse[FeedbackRegressionImportRead],
    summary="导入一轮评测报告的白名单摘要（管理员）",
)
def import_feedback_regression_report(
    feedback_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    expected_revision: int = Query(..., ge=1),
    raw: bytes = Depends(_read_report_body),
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = feedback_regression.import_report(
        db,
        admin,
        feedback_id,
        run_id,
        expected_revision=expected_revision,
        raw=raw,
    )
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_import",
        str(run_id),
        {
            "attempt_no": data["run"]["attempt_no"],
            "validation_level": data["run"]["validation_level"],
            "passed": data["run"]["passed"],
            "failure_count": len(data["run"]["failures"]),
        },
    )
    return ApiResponse(data=data)
