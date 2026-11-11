"""反馈回归在线作业 API：提交/找回、状态、取消、比较与人工核验。

所有路由要求管理员。列表/详情/找回接口绝不返回答案、引用或 trace 正文；只有显式
比较接口在复核当前权限、固定绑定与实际来源后才返回。审计只写 ID/状态/计数。
"""
import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DBSession

from app.api.deps import require_admin, write_audit
from app.db.session import get_db
from app.models import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.feedback_regression_jobs import (
    FeedbackRegressionBatchCreateRequest,
    FeedbackRegressionBatchRead,
    FeedbackRegressionCancelRequest,
    FeedbackRegressionJobComparisonRead,
    FeedbackRegressionJobCreateRequest,
    FeedbackRegressionJobRead,
    FeedbackRegressionRequestRead,
    FeedbackRegressionReviewRequest,
)
from app.services import feedback_regression_jobs as jobs

router = APIRouter(prefix="/feedback", tags=["feedback"])


def _call(function, db, *args, **kwargs):
    try:
        return function(db, *args, **kwargs)
    except jobs.JobError as exc:
        admin = args[0] if args and isinstance(args[0], User) else None
        # 服务层错误路径会 rollback，届时 ORM 管理员对象被 expire；请求日志中间件
        # 在依赖关闭后仍需读取用户名，这里在会话仍可用时重新加载一次。
        if admin is not None:
            try:
                db.refresh(admin)
            except Exception:
                pass
        raise jobs.to_http(exc) from None


@router.post(
    "/regression/jobs",
    response_model=ApiResponse[FeedbackRegressionJobRead],
    summary="创建单条反馈在线回归作业（管理员）",
)
def create_regression_job(
    payload: FeedbackRegressionJobCreateRequest,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    data = _call(
        jobs.enqueue_job,
        db,
        admin,
        payload.feedback_id,
        payload.expected_revision,
        payload.request_id,
        auth_token_version=request.state.auth_token_version,
    )
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_job_create",
        str(data["job_id"]),
        {
            "status": data["status"],
            "run_attempt_no": data["run_attempt_no"],
            "attempt_count": data["attempt_count"],
        },
    )
    return ApiResponse(data=data)


@router.post(
    "/regression/batches",
    response_model=ApiResponse[FeedbackRegressionBatchRead],
    summary="创建批量反馈在线回归作业（管理员，1–20 条原子提交）",
)
def create_regression_batch(
    payload: FeedbackRegressionBatchCreateRequest,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    items = [
        (item.feedback_id, item.expected_revision) for item in payload.items
    ]
    data = _call(
        jobs.enqueue_batch,
        db,
        admin,
        payload.request_id,
        items,
        auth_token_version=request.state.auth_token_version,
    )
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_batch_create",
        str(data["batch_id"]),
        {"revision": data["revision"], "count": data["counts"]["total"]},
    )
    return ApiResponse(data=data)


@router.get(
    "/regression/jobs",
    response_model=ApiResponse[PageResult[FeedbackRegressionJobRead]],
    summary="分页查看在线回归作业（管理员）",
)
def list_regression_jobs(
    response: Response,
    feedback_id: uuid.UUID | None = Query(None),
    batch_id: uuid.UUID | None = Query(None),
    request_id: uuid.UUID | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    total, items = _call(
        jobs.list_jobs,
        db,
        admin,
        feedback_id=feedback_id,
        batch_id=batch_id,
        request_id=request_id,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(data=PageResult(total=total, items=items))


@router.get(
    "/regression/jobs/{job_id}",
    response_model=ApiResponse[FeedbackRegressionJobRead],
    summary="查看在线回归作业状态（管理员，不含正文）",
)
def get_regression_job(
    job_id: uuid.UUID,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=_call(jobs.get_job, db, job_id))


@router.get(
    "/regression/jobs/{job_id}/comparison",
    response_model=ApiResponse[FeedbackRegressionJobComparisonRead],
    summary="比较历史旧结果与在线新结果（管理员，当前权限/来源复核）",
)
def get_regression_job_comparison(
    job_id: uuid.UUID,
    request: Request,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    data = _call(
        jobs.comparison,
        db,
        admin,
        job_id,
        auth_token_version=request.state.auth_token_version,
    )
    return ApiResponse(data=data)


@router.post(
    "/regression/jobs/{job_id}/cancel",
    response_model=ApiResponse[FeedbackRegressionJobRead],
    summary="取消在线回归作业（管理员，CAS）",
)
def cancel_regression_job(
    job_id: uuid.UUID,
    payload: FeedbackRegressionCancelRequest,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = _call(
        jobs.cancel_job,
        db,
        admin,
        job_id,
        payload.expected_revision,
        auth_token_version=request.state.auth_token_version,
    )
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_job_cancel",
        str(job_id),
        {"status": data["status"], "revision": data["revision"]},
    )
    return ApiResponse(data=data)


@router.put(
    "/regression/jobs/{job_id}/review",
    response_model=ApiResponse[FeedbackRegressionJobRead],
    summary="在线回归结果人工核验（管理员，绑定 run/result 指纹）",
)
def review_regression_job(
    job_id: uuid.UUID,
    payload: FeedbackRegressionReviewRequest,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = _call(
        jobs.review_job,
        db,
        admin,
        job_id,
        expected_revision=payload.expected_revision,
        run_id=payload.run_id,
        result_sha256=payload.result_sha256,
        status=payload.status,
        notes=payload.notes,
        auth_token_version=request.state.auth_token_version,
    )
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_job_review",
        str(job_id),
        {
            "status": data["review"]["status"],
            "revision": data["revision"],
        },
    )
    return ApiResponse(data=data)


@router.get(
    "/regression/batches/{batch_id}",
    response_model=ApiResponse[FeedbackRegressionBatchRead],
    summary="查看批量提交的实际状态计数（管理员）",
)
def get_regression_batch(
    batch_id: uuid.UUID,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=_call(jobs.get_batch, db, batch_id))


@router.post(
    "/regression/batches/{batch_id}/cancel",
    response_model=ApiResponse[FeedbackRegressionBatchRead],
    summary="取消批次内活动作业（管理员，CAS）",
)
def cancel_regression_batch(
    batch_id: uuid.UUID,
    payload: FeedbackRegressionCancelRequest,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = _call(
        jobs.cancel_batch,
        db,
        admin,
        batch_id,
        payload.expected_revision,
        auth_token_version=request.state.auth_token_version,
    )
    write_audit(
        db,
        request,
        admin,
        "feedback_regression_batch_cancel",
        str(batch_id),
        {"revision": data["revision"], "counts": data["counts"]},
    )
    return ApiResponse(data=data)


@router.get(
    "/regression/requests/{request_id}",
    response_model=ApiResponse[FeedbackRegressionRequestRead],
    summary="按原 request_id 只读找回单条/批次提交结果（管理员，无正文）",
)
def lookup_regression_request(
    request_id: uuid.UUID,
    response: Response,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=_call(jobs.lookup_request, db, admin, request_id))
