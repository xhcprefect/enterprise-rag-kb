import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import require_admin
from app.db.session import get_db
from app.models import AuditLog, User
from app.schemas.common import ApiResponse, PageResult
from pydantic import BaseModel

router = APIRouter(prefix="/audit-logs", tags=["审计日志"])


class AuditLogRead(BaseModel):
    id: uuid.UUID
    username: str
    action: str
    target: str
    detail: dict | None
    ip: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=ApiResponse[PageResult[AuditLogRead]], summary="审计日志列表（管理员）")
def list_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    action: str | None = None,
    username: str | None = None,
    db: DBSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(AuditLog)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if username:
        stmt = stmt.where(AuditLog.username.ilike(f"%{username}%"))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()

    return ApiResponse(
        data=PageResult(
            total=total,
            items=[AuditLogRead.model_validate(r) for r in rows],
        )
    )
