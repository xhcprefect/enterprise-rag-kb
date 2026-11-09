"""答案反馈接口：提交、本人列表、管理员列表/详情/核实/导出。

路由声明顺序固定：``/mine``、``/export`` 必须在 ``/{feedback_id}`` 之前，
避免固定路径被 UUID 路径参数吞掉。
"""
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, require_admin, write_audit
from app.db.session import get_db
from app.models import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.feedback import (
    AnswerFeedbackAdminDetail,
    AnswerFeedbackAdminItem,
    AnswerFeedbackOwnerItem,
    FeedbackCreate,
    FeedbackExportPayload,
    FeedbackReviewRequest,
    FeedbackStatus,
)
from app.services import answer_feedback

router = APIRouter(prefix="/feedback", tags=["feedback"])


def _owner_item(feedback) -> AnswerFeedbackOwnerItem:
    return AnswerFeedbackOwnerItem.model_validate(feedback)


def _admin_item(feedback) -> AnswerFeedbackAdminItem:
    return AnswerFeedbackAdminItem.model_validate(answer_feedback.admin_item(feedback))


@router.post("", response_model=ApiResponse[AnswerFeedbackOwnerItem], summary="提交回答反馈")
def create_feedback(
    payload: FeedbackCreate,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    feedback = answer_feedback.create_feedback(db, user, payload)
    return ApiResponse(data=_owner_item(feedback))


@router.get("/mine", response_model=ApiResponse[PageResult[AnswerFeedbackOwnerItem]],
            summary="我的回答反馈")
def list_my_feedback(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: FeedbackStatus | None = None,
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    total, rows = answer_feedback.list_mine(
        db, user, page=page, page_size=page_size, status=status
    )
    return ApiResponse(
        data=PageResult(total=total, items=[_owner_item(row) for row in rows])
    )


@router.get("/export", response_model=ApiResponse[FeedbackExportPayload],
            summary="导出已核实反馈为评测 JSONL（管理员）")
def export_feedback(
    request: Request,
    feedback_id: uuid.UUID | None = None,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    payload = answer_feedback.export_feedback(db, admin, feedback_id=feedback_id)
    write_audit(
        db,
        request,
        admin,
        "feedback_export",
        "",
        {
            "case_count": payload["case_count"],
            "dataset_sha256": payload["dataset_sha256"],
            "feedback_count": payload["case_count"],
        },
    )
    return ApiResponse(data=FeedbackExportPayload(**payload))


@router.get("", response_model=ApiResponse[PageResult[AnswerFeedbackAdminItem]],
            summary="反馈列表（管理员）")
def list_feedback(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: FeedbackStatus | None = None,
    category: Literal["inaccurate_answer", "citation_error", "missing_material"] | None = None,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    total, rows = answer_feedback.list_admin(
        db, page=page, page_size=page_size, status=status, category=category
    )
    return ApiResponse(
        data=PageResult(total=total, items=[_admin_item(row) for row in rows])
    )


@router.patch("/{feedback_id}/review", response_model=ApiResponse[AnswerFeedbackAdminItem],
              summary="核实或驳回反馈（管理员）")
def review_feedback(
    feedback_id: uuid.UUID,
    payload: FeedbackReviewRequest,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    feedback = answer_feedback.review_feedback(db, admin, feedback_id, payload)
    write_audit(
        db,
        request,
        admin,
        "feedback_review",
        str(feedback.id),
        {"decision": feedback.status, "revision": feedback.revision},
    )
    return ApiResponse(data=_admin_item(feedback))


@router.get("/{feedback_id}", response_model=ApiResponse[AnswerFeedbackAdminDetail],
            summary="反馈详情（管理员）")
def get_feedback_detail(
    feedback_id: uuid.UUID,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    data = answer_feedback.get_detail(db, admin, feedback_id)
    # 审计只放标识，不放问题/答案/引用/备注正文。
    write_audit(
        db,
        request,
        admin,
        "feedback_view_detail",
        str(data["id"]),
        {
            "conversation_id": str(data["conversation_id"]),
            "message_id": str(data["message_id"]),
        },
    )
    return ApiResponse(data=AnswerFeedbackAdminDetail(**data))
