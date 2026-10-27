"""逐次模型请求的实际用量记录；生成器 yield 之间不保留 ContextVar token。"""
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.db.session import SessionLocal
from app.models import KnowledgeBase, ModelPrice, ModelUsage, User

logger = logging.getLogger(__name__)
MILLION = Decimal(1_000_000)
MONEY_STEP = Decimal("0.0000000001")
MAX_TOKENS = 1_000_000_000_000


@dataclass(frozen=True)
class UsageContext:
    user_id: uuid.UUID | None = None
    kb_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    turn_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    operation: str = "generation"

    def for_operation(self, operation: str):
        return replace(self, operation=operation)


_context: ContextVar[UsageContext | None] = ContextVar("model_usage_context", default=None)


@contextmanager
def usage_scope(context: UsageContext | None):
    token = _context.set(context)
    try:
        yield
    finally:
        _context.reset(token)


def iterate_with_context(iterator, context: UsageContext | None):
    """Starlette 可在不同线程/上下文逐次 next；只在一次 next 内设置并恢复上下文。"""
    iterator = iter(iterator)
    try:
        while True:
            with usage_scope(context):
                try:
                    item = next(iterator)
                except StopIteration:
                    return
            yield item
    finally:
        close = getattr(iterator, "close", None)
        if close:
            with usage_scope(context):
                close()


def token_count(value):
    return value if type(value) is int and 0 <= value <= MAX_TOKENS else None


def normalize_usage(raw, kind="chat") -> dict | None:
    if not isinstance(raw, dict):
        return None
    incoming = token_count(raw.get("prompt_tokens", raw.get("input_tokens")))
    outgoing = token_count(raw.get("completion_tokens", raw.get("output_tokens")))
    if kind in {"embedding", "rerank"}:
        if incoming is None:
            incoming = token_count(raw.get("total_tokens"))
        outgoing = 0 if incoming is not None else None
    if incoming is None and outgoing is None:
        return None
    details = raw.get("prompt_tokens_details") or raw.get("input_token_details") or {}
    cached = token_count(raw.get("prompt_cache_hit_tokens"))
    if cached is None and isinstance(details, dict):
        cached = token_count(details.get("cached_tokens", details.get("cache_read")))
    if incoming is None or cached is None or cached > incoming:
        cached = None
    missed = token_count(raw.get("prompt_cache_miss_tokens"))
    if cached is not None and missed is not None and incoming != cached + missed:
        cached = None
    return {"input_tokens": incoming, "output_tokens": outgoing, "cached_input_tokens": cached}


def estimate_cost(usage: dict, price: dict) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    incoming, outgoing = usage.get("input_tokens"), usage.get("output_tokens")
    input_rate, output_rate = price.get("input_per_million"), price.get("output_per_million")
    if None in (incoming, outgoing, input_rate, output_rate):
        return None, None, None
    cache_rate, cached = price.get("cached_input_per_million"), usage.get("cached_input_tokens")
    if cache_rate is not None and cache_rate != input_rate and cached is None:
        return None, None, None
    if cache_rate is None or cached is None:
        input_cost = Decimal(incoming) * input_rate / MILLION
    else:
        input_cost = (Decimal(incoming - cached) * input_rate + Decimal(cached) * cache_rate) / MILLION
    output_cost = Decimal(outgoing) * output_rate / MILLION
    input_cost, output_cost = input_cost.quantize(MONEY_STEP), output_cost.quantize(MONEY_STEP)
    return input_cost, output_cost, input_cost + output_cost


class ModelCall:
    """先落 started，再幂等更新最终值；重试独立落行，进程中断保留未知记录。"""
    def __init__(self, kind, provider, model, *, context: UsageContext | None = None):
        self.context = context if context is not None else _context.get()
        self.enabled = self.context is not None
        self.kind, self.provider, self.model = kind, str(provider)[:128], str(model)[:256]
        self.price = {"price_revision": None, "input_per_million": None, "output_per_million": None,
                      "cached_input_per_million": None}
        if self.enabled:
            try:
                with SessionLocal() as db:
                    row = db.scalar(select(ModelPrice).where(ModelPrice.provider == self.provider,
                                    ModelPrice.model == self.model, ModelPrice.kind == kind))
                    if row is not None:
                        self.price = {"price_revision": row.revision, "input_per_million": row.input_per_million,
                                      "output_per_million": row.output_per_million,
                                      "cached_input_per_million": row.cached_input_per_million}
            except Exception as exc:
                logger.warning("读取模型计价失败，本次费用记为未知: %s", type(exc).__name__)
        self._start()

    def _start(self):
        self.id, self.occurred_at = uuid.uuid4(), datetime.now(timezone.utc)
        self.finished = False
        self.usage = {"input_tokens": None, "output_tokens": None, "cached_input_tokens": None}
        self._persist("started")

    def observe(self, raw):
        normalized = normalize_usage(raw, self.kind)
        if normalized is None:
            return
        # OpenAI 的 usage 帧是累计值，重复帧不能重复计量；缓存明细可在末帧补齐。
        old_total = sum(value or 0 for key, value in self.usage.items() if key != "cached_input_tokens")
        new_total = sum(value or 0 for key, value in normalized.items() if key != "cached_input_tokens")
        if new_total >= old_total:
            if normalized["input_tokens"] == self.usage["input_tokens"] and normalized["cached_input_tokens"] is None:
                normalized["cached_input_tokens"] = self.usage["cached_input_tokens"]
            self.usage = normalized

    def retry(self):
        self.finish("failed")
        self._start()

    def finish(self, status="succeeded"):
        if self.finished:
            return
        self.finished = True
        self._persist(status)

    def _persist(self, status):
        if not self.enabled:
            return
        ctx = self.context
        operation = "rerank" if self.kind == "rerank" else ctx.operation
        input_cost, output_cost, cost = estimate_cost(self.usage, self.price)
        # 删除用户/知识库不能抹掉费用，也不能让迟到写回因外键失败丢失已发生用量。
        values = dict(id=self.id, occurred_at=self.occurred_at,
                      finished_at=datetime.now(timezone.utc) if self.finished else None,
                      user_id=select(User.id).where(User.id == ctx.user_id).scalar_subquery() if ctx.user_id else None,
                      kb_id=select(KnowledgeBase.id).where(KnowledgeBase.id == ctx.kb_id).scalar_subquery() if ctx.kb_id else None,
                      conversation_id=ctx.conversation_id, turn_id=ctx.turn_id, document_id=ctx.document_id,
                      provider=self.provider, model=self.model, kind=self.kind, operation=operation,
                      status=status, source="provider", currency="CNY", **self.usage, **self.price,
                      input_cost=input_cost, output_cost=output_cost, estimated_cost=cost)
        try:
            statement = insert(ModelUsage).values(**values)
            with SessionLocal() as db:
                db.execute(statement.on_conflict_do_update(index_elements=["id"], set_={
                    key: getattr(statement.excluded, key) for key in values if key not in {"id", "occurred_at"}
                }))
                db.commit()
        except Exception as exc:
            # 计量异常不妨碍已有问答/入库，记录失败也不会被伪装成零费用。
            logger.warning("模型用量写入失败: call=%s status=%s error=%s", self.id, status, type(exc).__name__)
