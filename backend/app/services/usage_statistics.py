"""按北京时间聚合实际用量与调用时的价格快照，缺失值不折算为免费。"""
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlparse

from fastapi import HTTPException
from sqlalchemy import and_, case, func, select, text
from sqlalchemy.dialects.postgresql import insert

from app.core.config import settings
from app.models import ModelPrice, ModelUsage, UsageTrackingState
from app.schemas.usage import PriceRead, UsageMetrics

LOCAL_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def initialize_usage(conn):
    conn.execute(insert(UsageTrackingState).values(id=1).on_conflict_do_nothing(index_elements=["id"]))
    # 只补录带明确实际 usage 的旧生成。未知历史模型/单价不猜成当前模型，更不补算费用。
    # CASE 防止坏 JSON 数字触发不安全转换；现代请求已有逐次记录时不再从 trace 重复补录。
    conn.execute(text("""
        WITH legacy AS (
            SELECT t.id, t.user_id, t.conversation_id, c.kb_id, t.created_at, t.updated_at,
              CASE WHEN jsonb_typeof(t.trace->'usage'->'input_tokens') = 'number'
                         AND (t.trace->'usage'->>'input_tokens') ~ '^[0-9]{1,13}$'
                   THEN (t.trace->'usage'->>'input_tokens')::bigint END AS incoming,
              CASE WHEN jsonb_typeof(t.trace->'usage'->'output_tokens') = 'number'
                         AND (t.trace->'usage'->>'output_tokens') ~ '^[0-9]{1,13}$'
                   THEN (t.trace->'usage'->>'output_tokens')::bigint END AS outgoing
            FROM chat_turns t LEFT JOIN conversations c ON c.id = t.conversation_id
            WHERE t.status = 'completed' AND t.trace->>'usage_scope' = 'generation_only'
        )
        INSERT INTO model_usage
          (id, origin_key, occurred_at, finished_at, user_id, kb_id, conversation_id, turn_id,
           provider, model, kind, operation, status, source, input_tokens, output_tokens, currency)
        SELECT md5('legacy-generation:' || id::text)::uuid, 'legacy-generation:' || id::text,
               created_at, updated_at, user_id, kb_id, conversation_id, id,
               'historical', 'historical-unknown', 'chat', 'generation', 'succeeded', 'legacy', incoming, outgoing, 'CNY'
        FROM legacy l
        WHERE incoming BETWEEN 0 AND 1000000000000 AND outgoing BETWEEN 0 AND 1000000000000
          AND NOT EXISTS (SELECT 1 FROM model_usage m WHERE m.turn_id = l.id AND m.operation = 'generation')
        ON CONFLICT DO NOTHING
    """))


def current_catalog():
    def provider(mode, base):
        return "ollama" if mode == "ollama" else (urlparse(base).hostname or "cloud").lower()
    result = {(provider(settings.LLM_PROVIDER, settings.LLM_BASE_URL), settings.LLM_MODEL, "chat"),
              (provider(settings.EMBEDDING_PROVIDER, settings.EMBEDDING_BASE_URL), settings.EMBEDDING_MODEL, "embedding")}
    if settings.RERANK_ENABLED and settings.RERANK_MODEL:
        result.add(((urlparse(settings.RERANK_BASE_URL).hostname or "rerank").lower(), settings.RERANK_MODEL, "rerank"))
    return result


def price_list(db):
    rows = db.scalars(select(ModelPrice).order_by(ModelPrice.provider, ModelPrice.model, ModelPrice.kind)).all()
    prices = {(row.provider, row.model, row.kind): PriceRead.model_validate(row) for row in rows}
    observed = db.execute(select(ModelUsage.provider, ModelUsage.model, ModelUsage.kind)
                          .where(ModelUsage.source != "legacy").distinct()).all()
    for provider, model, kind in current_catalog() | {tuple(row) for row in observed}:
        prices.setdefault((provider, model, kind), PriceRead(provider=provider, model=model, kind=kind))
    return [prices[key] for key in sorted(prices)]


def update_price(db, payload):
    identity = f"price:{payload.provider}:{payload.kind}:{payload.model}"
    lock = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=True)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock})
    row = db.scalar(select(ModelPrice).where(ModelPrice.provider == payload.provider,
                    ModelPrice.model == payload.model, ModelPrice.kind == payload.kind)
                    .with_for_update().execution_options(populate_existing=True))
    revision = row.revision if row is not None else 0
    if payload.expected_revision != revision:
        raise HTTPException(409, "模型单价已被其他页面修改，请刷新后重试")
    if row is None:
        row = ModelPrice(provider=payload.provider, model=payload.model, kind=payload.kind, currency="CNY")
        db.add(row)
    row.input_per_million = payload.input_per_million
    row.output_per_million = payload.output_per_million
    row.cached_input_per_million = payload.cached_input_per_million
    row.revision = revision + 1
    db.commit()
    db.refresh(row)
    return PriceRead.model_validate(row)


def date_range(start: date | None, end: date | None, granularity: str):
    today = datetime.now(LOCAL_TZ).date()
    end = end or today
    start = start or end - timedelta(days=29)
    if not date(2000, 1, 1) <= start <= end <= date(2100, 12, 31):
        raise HTTPException(422, "请选择 2000–2100 年之间、开始日期不晚于结束日期的区间")
    days = (end - start).days + 1
    if days > 366 or (granularity == "hour" and days > 31):
        raise HTTPException(422, "统计区间最多 366 天，按小时统计最多 31 天")
    left = datetime.combine(start, time.min, LOCAL_TZ)
    right = datetime.combine(end + timedelta(days=1), time.min, LOCAL_TZ)
    return start, end, left, right


def _aggregates():
    known = and_(ModelUsage.input_tokens.is_not(None), ModelUsage.output_tokens.is_not(None))
    return [func.count().label("requests"),
            func.sum(case((ModelUsage.status == "succeeded", 1), else_=0)).label("successful_requests"),
            func.sum(case((ModelUsage.status.in_(["failed", "cancelled", "interrupted"]), 1), else_=0)).label("failed_requests"),
            func.sum(ModelUsage.input_tokens).label("input_tokens"),
            func.sum(ModelUsage.output_tokens).label("output_tokens"),
            func.sum(ModelUsage.cached_input_tokens).label("cached_input_tokens"),
            func.sum(case((known, 1), else_=0)).label("usage_known_requests"),
            func.sum(case((ModelUsage.estimated_cost.is_not(None), 1), else_=0)).label("priced_requests"),
            func.sum(ModelUsage.estimated_cost).label("estimated_cost"),
            func.sum(ModelUsage.input_cost).label("input_cost"), func.sum(ModelUsage.output_cost).label("output_cost")]


def _metrics(row):
    values = {key: int(row[key] or 0) for key in ("requests", "successful_requests", "failed_requests", "input_tokens",
              "output_tokens", "cached_input_tokens", "usage_known_requests", "priced_requests")}
    values.update(total_tokens=values["input_tokens"] + values["output_tokens"],
                  usage_missing_requests=values["requests"] - values["usage_known_requests"],
                  unpriced_requests=values["requests"] - values["priced_requests"])
    for key in ("estimated_cost", "input_cost", "output_cost"):
        values[key] = format(Decimal(row[key]), ".10f") if row[key] is not None else None
    return values


def _next_bucket(stamp, granularity):
    if granularity == "hour":
        return stamp + timedelta(hours=1)
    if granularity == "day":
        return stamp + timedelta(days=1)
    return stamp.replace(year=stamp.year + int(stamp.month == 12), month=stamp.month % 12 + 1)


def statistics(db, user, *, start=None, end=None, granularity="day", scope=None, model=None, operation=None):
    visibility = scope or ("all" if user.is_admin else "mine")
    if visibility == "all" and not user.is_admin:
        raise HTTPException(403, "全站用量统计仅管理员可查看")
    start, end, left, right = date_range(start, end, granularity)
    base = [ModelUsage.occurred_at >= left, ModelUsage.occurred_at < right]
    if visibility == "mine":
        base.append(ModelUsage.user_id == user.id)
    filters = list(base)
    if model:
        filters.append(ModelUsage.model == model)
    if operation:
        filters.append(ModelUsage.operation == operation)
    totals = _metrics(db.execute(select(*_aggregates()).where(*filters)).one()._mapping)
    bucket = func.date_trunc(granularity, func.timezone("Asia/Shanghai", ModelUsage.occurred_at)).label("period")
    rows = db.execute(select(bucket, *_aggregates()).where(*filters).group_by(bucket).order_by(bucket)).all()
    periods = {row.period.replace(tzinfo=LOCAL_TZ): _metrics(row._mapping) for row in rows}
    series, current = [], left.replace(day=1) if granularity == "month" else left
    while current < right:
        series.append({"period": current, **periods.get(current, UsageMetrics().model_dump())})
        current = _next_bucket(current, granularity)
    groups = [ModelUsage.provider, ModelUsage.model, ModelUsage.kind, ModelUsage.operation]
    rows = db.execute(select(*groups, *_aggregates()).where(*filters).group_by(*groups)
                      .order_by(func.count().desc(), *groups)).all()
    breakdown = [{**{column.key: getattr(row, column.key) for column in groups}, **_metrics(row._mapping)} for row in rows]
    models = db.scalars(select(ModelUsage.model).where(*base).distinct().order_by(ModelUsage.model)).all()
    started = db.scalar(select(UsageTrackingState.started_at).where(UsageTrackingState.id == 1))
    return {"start": start, "end": end, "granularity": granularity, "scope": visibility,
            "timezone": "Asia/Shanghai", "currency": "CNY", "tracking_started_at": started,
            "totals": totals, "series": series, "models": models, "breakdown": breakdown}
