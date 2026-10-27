"""用量与费用：普通用户仅本人，模型计价仅管理员可调整。"""
from datetime import date

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_admin, write_audit
from app.db.session import get_db
from app.models import User
from app.schemas.common import ApiResponse
from app.schemas.usage import (
    Granularity, PriceList, PriceRead, PriceUpdate, UsageOperation, UsageStatistics, UsageVisibility,
)
from app.services import usage_statistics

router = APIRouter(prefix="/usage", tags=["用量与费用"])


@router.get("/statistics", response_model=ApiResponse[UsageStatistics], summary="用量、费用与时间趋势")
def get_statistics(start: date | None = None, end: date | None = None, granularity: Granularity = "day",
                   scope: UsageVisibility | None = None, model: str | None = Query(default=None, max_length=256),
                   operation: UsageOperation | None = None, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    return ApiResponse(data=usage_statistics.statistics(db, user, start=start, end=end, granularity=granularity,
                                                       scope=scope, model=model, operation=operation))


@router.get("/prices", response_model=ApiResponse[PriceList], summary="模型单价（人民币/百万 Token）")
def get_prices(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return ApiResponse(data=PriceList(items=usage_statistics.price_list(db)))


@router.put("/prices", response_model=ApiResponse[PriceRead], summary="调整模型单价，仅影响后续调用")
def put_price(payload: PriceUpdate, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    result = usage_statistics.update_price(db, payload)
    write_audit(db, request, user, "model_price_update", payload.model,
                {"provider": payload.provider, "kind": payload.kind, "currency": "CNY", "revision": result.revision,
                 "input_per_million": str(result.input_per_million) if result.input_per_million is not None else None,
                 "output_per_million": str(result.output_per_million) if result.output_per_million is not None else None,
                 "cached_input_per_million": str(result.cached_input_per_million) if result.cached_input_per_million is not None else None})
    return ApiResponse(data=result)
