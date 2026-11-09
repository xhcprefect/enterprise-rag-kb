"""精确模型费用测量：只读、有界、按请求/文档身份聚合。

与 ``usage_statistics`` 的按时间段估算不同，本模块只回答一个窄问题：

- 指定的 ``(user_id, request_id)`` 一次问答，实际发生了哪些模型尝试、各花了多少；
- 指定的文档在本轮窗口内实际发生了多少 ``document_embedding``。

它刻意不读 ``ChatTurn.trace``（可能含问题/答案正文），也不做“查询前后总量
相减”。``ModelUsage`` 每次尝试一行，失败/取消/中断的尝试同样保留，因此只要
身份与窗口精确，聚合就是精确的。缺失、未计价、未完成、截断一律保持未知，
绝不折算成 0，也绝不把“已知部分费用”称作总费用。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, text, tuple_
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.models import ChatTurn, Document, ModelUsage
from app.schemas.usage import MeasurementCoverageStatus

# 单次测量最多读取的用量行；超过即截断，并把整体覆盖标为 unknown。
MEASUREMENT_MAX_ROWS = 5000

# 后台任务产生的模型调用不属于“本次会话请求”的费用，必须显式排除。
BACKGROUND_OPERATIONS = ("summary", "project_embedding")
DOCUMENT_OPERATION = "document_embedding"

_STATUS_FIELDS = {
    "succeeded": "succeeded_attempts",
    "failed": "failed_attempts",
    "cancelled": "cancelled_attempts",
    "interrupted": "interrupted_attempts",
    "started": "started_attempts",
}

# 只有这些生命周期状态能证明请求/文档已结束。completed/ready 是成功终态；
# failed/cancelled 也是可信终态，其已经发生的已计价尝试不能当成免费或未知。
TURN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
TURN_NONTERMINAL_STATUSES = frozenset({"processing", "pending"})
DOCUMENT_TERMINAL_STATUSES = frozenset({"ready", "failed", "cancelled"})
DOCUMENT_NONTERMINAL_STATUSES = frozenset({"pending", "processing"})

# 已知尝试状态：started 表示已开始但未结束；其余是可信终结状态。
ATTEMPT_KNOWN_STATUSES = frozenset({"started", "succeeded", "failed", "cancelled", "interrupted"})


def _money(value: Decimal | None) -> str | None:
    return format(value, ".10f") if value is not None else None


@dataclass
class _Accumulator:
    attempts: int = 0
    succeeded_attempts: int = 0
    failed_attempts: int = 0
    cancelled_attempts: int = 0
    interrupted_attempts: int = 0
    started_attempts: int = 0
    other_status_attempts: int = 0

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    usage_known_attempts: int = 0
    usage_missing_attempts: int = 0

    priced_attempts: int = 0
    unpriced_attempts: int = 0
    unfinished_attempts: int = 0

    known_cost: Decimal = Decimal("0")
    has_known_cost: bool = False
    currencies: set[str] = field(default_factory=set)
    operations: set[str] = field(default_factory=set)
    issues: set[str] = field(default_factory=set)

    def add(self, row: ModelUsage) -> None:
        self.attempts += 1
        status = (row.status or "").lower()
        if status not in ATTEMPT_KNOWN_STATUSES:
            # 不认识的尝试状态不能仅因带有 finished_at/费用就获得“完成”语义。
            self.other_status_attempts += 1
            self.issues.add("attempt_status_unknown")
        else:
            field_name = _STATUS_FIELDS[status]
            setattr(self, field_name, getattr(self, field_name) + 1)
        self.operations.add(row.operation)

        # 可用列分别累计；只有输入与输出同时存在才算“用量已知”。
        if row.input_tokens is not None:
            self.input_tokens += int(row.input_tokens)
        if row.output_tokens is not None:
            self.output_tokens += int(row.output_tokens)
        if row.cached_input_tokens is not None:
            self.cached_input_tokens += int(row.cached_input_tokens)
        if row.input_tokens is not None and row.output_tokens is not None:
            self.usage_known_attempts += 1
        else:
            self.usage_missing_attempts += 1
            self.issues.add("usage_missing")

        currency = str(row.currency) if row.currency else ""
        if row.estimated_cost is not None:
            self.priced_attempts += 1
            # 只有明确 CNY 的金额才能进入人民币小计；非 CNY/缺币种不跨单位相加。
            if currency == "CNY":
                self.known_cost += Decimal(row.estimated_cost)
                self.has_known_cost = True
        else:
            self.unpriced_attempts += 1
            self.issues.add("unpriced")

        if row.finished_at is None or status == "started":
            self.unfinished_attempts += 1
            self.issues.add("unfinished")

        if currency:
            self.currencies.add(currency)
            # 本接口只输出 CNY；非 CNY 即使内部一致也不能当作完整人民币总费用。
            if currency != "CNY":
                self.issues.add("currency_mismatch")
        else:
            # 缺币种无法判断金额单位，必须显式未知，不能静默当作 CNY。
            self.issues.add("currency_missing")

    def metrics(self, *, total_cost: str | None = None,
                suppress_known_subtotal: bool = False) -> dict:
        return {
            "attempts": self.attempts,
            "succeeded_attempts": self.succeeded_attempts,
            "failed_attempts": self.failed_attempts,
            "cancelled_attempts": self.cancelled_attempts,
            "interrupted_attempts": self.interrupted_attempts,
            "started_attempts": self.started_attempts,
            "other_status_attempts": self.other_status_attempts,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "usage_known_attempts": self.usage_known_attempts,
            "usage_missing_attempts": self.usage_missing_attempts,
            "priced_attempts": self.priced_attempts,
            "unpriced_attempts": self.unpriced_attempts,
            "known_cost_subtotal": (
                None if suppress_known_subtotal or not self.has_known_cost
                else _money(self.known_cost)
            ),
            "total_cost": total_cost,
        }


def _coverage_status(found: bool, attempts: int, issues: set[str], truncated: bool) -> MeasurementCoverageStatus:
    if not found:
        return "unknown"
    if attempts == 0:
        return "unknown"
    if truncated or issues:
        return "partial"
    return "complete"


def _reason_list(issues: set[str]) -> list[str]:
    return sorted(issues)


def _set_statement_timeout(db) -> None:
    # 值只来自受控配置；SET LOCAL 只作用于当前事务，会话关闭后自动恢复。
    milliseconds = max(1, int(float(settings.OPERATIONS_DB_TIMEOUT_SECONDS) * 1000))
    db.execute(text(f"SET LOCAL statement_timeout = {milliseconds}"))


def measure(db, payload) -> dict:
    """按精确身份聚合；任何不确定都体现为 null/partial/unknown，不补零。"""
    start: datetime = payload.start
    end: datetime = payload.end
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "测量窗口必须包含时区")

    refs = [(item.user_id, item.request_id) for item in payload.requests]
    document_ids = list(payload.document_ids)
    now = datetime.now(timezone.utc)

    try:
        _set_statement_timeout(db)

        # ChatTurn 只取身份与状态，绝不选择 trace。unique(user_id, request_id)
        # 保证每个身份最多一行；仍显式 limit，避免依赖约束做无界假设。
        turn_rows = []
        if refs:
            turn_rows = db.execute(
                select(
                    ChatTurn.id,
                    ChatTurn.user_id,
                    ChatTurn.request_id,
                    ChatTurn.status,
                )
                .where(tuple_(ChatTurn.user_id, ChatTurn.request_id).in_(refs))
                .order_by(ChatTurn.created_at, ChatTurn.id)
                .limit(len(refs))
            ).all()
        turn_by_pair = {(row.user_id, row.request_id): row for row in turn_rows}
        turn_by_id = {row.id: row for row in turn_rows}

        document_rows = []
        if document_ids:
            document_rows = db.execute(
                select(Document.id, Document.status)
                .where(Document.id.in_(document_ids))
                .order_by(Document.id)
                .limit(len(document_ids))
            ).all()
        document_by_id = {row.id: row for row in document_rows}

        usage_rows: list[ModelUsage] = []
        truncated = False
        turn_ids = list(turn_by_id)
        conditions = []
        if turn_ids:
            conditions.append(ModelUsage.turn_id.in_(turn_ids))
        if document_ids:
            conditions.append(and_(
                ModelUsage.document_id.in_(document_ids),
                ModelUsage.operation == DOCUMENT_OPERATION,
            ))
        if conditions:
            usage_rows = db.scalars(
                select(ModelUsage)
                .where(
                    ModelUsage.occurred_at >= start,
                    ModelUsage.occurred_at < end,
                    or_(*conditions),
                )
                .order_by(ModelUsage.occurred_at, ModelUsage.id)
                .limit(MEASUREMENT_MAX_ROWS + 1)
            ).all()
            if len(usage_rows) > MEASUREMENT_MAX_ROWS:
                truncated = True
                usage_rows = usage_rows[:MEASUREMENT_MAX_ROWS]
    except SQLAlchemyError as exc:
        # 查询失败/语句超时是“未知”，不是零费用；不返回异常正文或连接信息。
        raise HTTPException(503, "模型费用测量查询失败或超时，结果不可用") from exc

    totals = _Accumulator()
    by_operation: dict[str, _Accumulator] = {}
    request_acc: dict[tuple[uuid.UUID, uuid.UUID], _Accumulator] = {
        pair: _Accumulator() for pair in refs
    }
    document_acc: dict[uuid.UUID, _Accumulator] = {
        document_id: _Accumulator() for document_id in document_ids
    }
    association_mismatch_attempts = 0
    global_currencies: set[str] = set()

    for row in usage_rows:
        operation = row.operation
        if operation in BACKGROUND_OPERATIONS:
            # 后台摘要/项目索引不属于本次请求，不并入任何汇总。
            continue

        request_pair = None
        if row.turn_id is not None and operation != DOCUMENT_OPERATION:
            turn = turn_by_id.get(row.turn_id)
            if turn is not None:
                # 行上的 user_id 若与 turn 的 user_id 冲突，说明计量关联异常；
                # 不并入总额，显式记为关联不一致，避免跨账号混算。
                if row.user_id is not None and row.user_id != turn.user_id:
                    association_mismatch_attempts += 1
                    request_acc[(turn.user_id, turn.request_id)].issues.add("association_mismatch")
                    continue
                request_pair = (turn.user_id, turn.request_id)
                request_acc[request_pair].add(row)

        document_id = None
        if operation == DOCUMENT_OPERATION and row.document_id in document_acc:
            document_id = row.document_id
            document_acc[document_id].add(row)

        if request_pair is None and document_id is None:
            # 查询条件之外的记录（例如 turn_id 命中但 operation 属后台）不并入。
            continue

        totals.add(row)
        by_operation.setdefault(operation, _Accumulator()).add(row)
        if row.currency:
            global_currencies.add(str(row.currency))

    if association_mismatch_attempts:
        totals.issues.add("association_mismatch")

    # 任何非 CNY/缺币种/混合币种都会让“无单位的小计”失去合法人民币含义。
    # 此时所有层级统一不再给出数值小计；priced 数量仍保留，便于定位缺失。
    currency_mismatch = (len(global_currencies) > 1
                         or bool(global_currencies - {"CNY"})
                         or "currency_missing" in totals.issues)
    if currency_mismatch:
        totals.issues.add("currency_mismatch")

    def _subtotal(acc: _Accumulator) -> str | None:
        if currency_mismatch or not acc.has_known_cost:
            return None
        return _money(acc.known_cost)

    # ---- 逐个请求/文档的覆盖判定 -------------------------------------------------
    request_results = []
    missing_requests = 0
    attempts_missing_requests = 0
    nonterminal_requests = 0
    unknown_request_statuses = 0
    for user_id, request_id in refs:
        turn = turn_by_pair.get((user_id, request_id))
        acc = request_acc[(user_id, request_id)]
        found = turn is not None
        if not found:
            missing_requests += 1
            reasons = {"turn_not_found"}
            status: MeasurementCoverageStatus = "unknown"
        else:
            reasons = set(acc.issues)
            turn_status = str(turn.status or "").lower()
            if turn_status not in TURN_TERMINAL_STATUSES:
                if turn_status in TURN_NONTERMINAL_STATUSES:
                    reasons.add("turn_not_terminal")
                    nonterminal_requests += 1
                else:
                    reasons.add("turn_status_unknown")
                    unknown_request_statuses += 1
            if acc.attempts == 0:
                attempts_missing_requests += 1
                reasons.add("no_usage_rows")
            status = _coverage_status(found, acc.attempts, reasons, truncated)
        if truncated and found:
            reasons.add("results_truncated")
            status = "partial" if acc.attempts else "unknown"
        request_results.append({
            "user_id": user_id,
            "request_id": request_id,
            "found": found,
            "turn_status": turn.status if turn is not None else None,
            "attempts": acc.attempts,
            "operations": sorted(acc.operations),
            "known_cost_subtotal": _subtotal(acc),
            "coverage_status": status,
            "reasons": _reason_list(reasons),
        })

    document_results = []
    missing_documents = 0
    attempts_missing_documents = 0
    nonterminal_documents = 0
    unknown_document_statuses = 0
    for document_id in document_ids:
        document = document_by_id.get(document_id)
        acc = document_acc[document_id]
        exists = document is not None
        reasons = set(acc.issues)
        if not exists:
            missing_documents += 1
            reasons.add("document_not_found")
        else:
            document_status = str(document.status or "").lower()
            if document_status not in DOCUMENT_TERMINAL_STATUSES:
                if document_status in DOCUMENT_NONTERMINAL_STATUSES:
                    reasons.add("document_not_terminal")
                    nonterminal_documents += 1
                else:
                    reasons.add("document_status_unknown")
                    unknown_document_statuses += 1
        if acc.attempts == 0:
            attempts_missing_documents += 1
            reasons.add("no_document_embedding_rows")
        status = _coverage_status(exists, acc.attempts, reasons, truncated)
        if truncated and exists:
            reasons.add("results_truncated")
            status = "partial" if acc.attempts else "unknown"
        document_results.append({
            "document_id": document_id,
            "exists": exists,
            "document_status": document.status if document is not None else None,
            "attempts": acc.attempts,
            "operations": sorted(acc.operations),
            "known_cost_subtotal": _subtotal(acc),
            "coverage_status": status,
            "reasons": _reason_list(reasons),
        })

    # ---- 整体覆盖与总费用 -------------------------------------------------------
    coverage_issues = set(totals.issues)
    if missing_requests or missing_documents:
        coverage_issues.add("selection_missing")
    if attempts_missing_requests or attempts_missing_documents:
        coverage_issues.add("metering_missing")
    if association_mismatch_attempts:
        coverage_issues.add("association_mismatch")
    if nonterminal_requests:
        coverage_issues.add("request_not_terminal")
    if nonterminal_documents:
        coverage_issues.add("document_not_terminal")
    if unknown_request_statuses:
        coverage_issues.add("request_status_unknown")
    if unknown_document_statuses:
        coverage_issues.add("document_status_unknown")

    if truncated:
        coverage_status: MeasurementCoverageStatus = "unknown"
    elif totals.attempts == 0:
        coverage_status = "unknown"
    elif coverage_issues:
        coverage_status = "partial"
    else:
        coverage_status = "complete"

    complete = coverage_status == "complete"
    total_cost = _money(totals.known_cost) if complete and totals.has_known_cost else None

    boundaries = [
        "request scope: only attempts linked by ChatTurn.turn_id; summary and "
        "project_embedding background calls are not measured",
        "document scope: only document_embedding rows for the selected document ids "
        "inside the window; older document history is excluded",
        "window: UTC half-open [start, end); a started attempt without a final row "
        "remains unfinished and unknown",
        "measurement: local metering snapshots, not a provider bill or an enterprise "
        "capacity guarantee; total_cost is null when coverage is incomplete",
    ]

    operation_results = [
        {"operation": operation, **accumulator.metrics(
            suppress_known_subtotal=currency_mismatch)}
        for operation, accumulator in sorted(by_operation.items())
    ]

    return {
        "schema": "rag-usage-measurement/v1",
        "generated_at": now,
        "window_start": start,
        "window_end": end,
        "currency": "CNY" if global_currencies == {"CNY"} and not currency_mismatch else None,
        "totals": totals.metrics(total_cost=total_cost,
                                 suppress_known_subtotal=currency_mismatch),
        "by_operation": operation_results,
        "requests": request_results,
        "documents": document_results,
        "coverage": {
            "status": coverage_status,
            "complete": complete,
            "truncated": truncated,
            "missing_requests": missing_requests,
            "missing_documents": missing_documents,
            "attempts_missing_requests": attempts_missing_requests,
            "attempts_missing_documents": attempts_missing_documents,
            "usage_missing_attempts": totals.usage_missing_attempts,
            "unpriced_attempts": totals.unpriced_attempts,
            "unfinished_attempts": totals.unfinished_attempts,
            "nonterminal_requests": nonterminal_requests,
            "nonterminal_documents": nonterminal_documents,
            "unknown_request_statuses": unknown_request_statuses,
            "unknown_document_statuses": unknown_document_statuses,
            "unknown_attempt_statuses": totals.other_status_attempts,
            "association_mismatch_attempts": association_mismatch_attempts,
            "currency_mismatch": currency_mismatch,
            "currencies": sorted(global_currencies),
            "background_operations_unmeasured": list(BACKGROUND_OPERATIONS),
            "notes": _reason_list(coverage_issues),
        },
        "boundaries": boundaries,
    }
