"""反馈回归闭环服务：纳入、准备重跑、当前轮导出、导入与历史。

业务边界与冻结契约一致：

- 每条反馈最多一条回归记录；不复制问题/答案/引用正文。
- 准备与导入都在回归行锁 + revision CAS 下推进；current_attempt_no 只看服务端
  序号，不依赖客户端时间。
- 评测 case 构造、来源稳定化、绑定摘要与报告严格解析全部委托
  ``feedback_regression_evaluation``（纯适配层）。本模块只做编排与事务。
- 上传报告正文只在本次请求内校验；落库字段严格来自白名单摘要。

错误统一使用中文 ``HTTPException``，消息末尾带稳定 reason 码，便于支持定位。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.models import (
    AnswerFeedback,
    Conversation,
    KnowledgeBase,
    Message,
    User,
)
from app.models.feedback import FEEDBACK_STATUS_VERIFIED
from app.models.feedback_regression import (
    REGRESSION_PROTOCOL_VERSION,
    FeedbackRegression,
    FeedbackRegressionRun,
)
from app.models.feedback_regression_job import (
    ACTIVE_JOB_STATUSES,
    EXECUTION_KIND_ONLINE,
    FeedbackRegressionJob,
)
from app.schemas.feedback_regression import (
    FeedbackRegressionCreateRequest,
    FeedbackRegressionPrepareRequest,
)
from app.services import answer_feedback
from app.services.evaluation import EvaluationError

# 单个上传报告正文的硬上限；端点从 request.stream() 先做同样的实际字节限制。
REPORT_MAX_BYTES = 2 * 1024 * 1024

# 阻断原因码 -> 面向管理员的中文说明；reason 本身始终保留在括号内。
_REASON_MESSAGES = {
    "feedback_not_verified": "反馈尚未核实，不能纳入回归",
    "feedback_stale": "原回答或提问已变化，反馈结果已过期",
    "project_unsupported": "项目库会话不支持回归重跑",
    "scope_unsupported": "文档范围会话不支持回归重跑",
    "turns_overflow": "多轮上下文超过回归上限",
    "annotation_incomplete": "反馈核验信息不完整，无法纳入回归",
    "source_ambiguous": "期望来源存在歧义，请先修正标注",
    "source_invalid": "期望来源已失效或无权访问",
    "chunk_missing": "期望来源的实际分块不存在",
    "version_mismatch": "期望来源的索引代次不匹配",
    "sample_changed": "回归样例已变化，请重新纳入或重新准备",
    "owner_inactive": "反馈所有者已禁用，不能按原权限重跑",
    "owner_access_revoked": "反馈所有者已无权访问该知识库",
    "root_plan_changed": "反馈根身份在等锁期间发生变化，请重试",
    "manifest_overflow": "执行清单超过 256 项上限，已拒绝",
    "manifest_invalid": "执行清单结构不合法",
    "scope_invalid": "会话限定文档范围已失效或不一致",
    "project_snapshot_invalid": "项目快照已失效或未就绪",
    "project_snapshot_changed": "项目快照身份与入队时冻结值不一致",
    "live_fixed_scope_unsupported": "旧 live CLI 不能承载固定文档范围或项目快照，请使用作业执行",
    "auth_invalid": "登录状态已失效，请重新登录",
    "admin_required": "需要管理员权限",
    "invalid_batch": "批量回归请求不合法",
    "regression_job_active": "该反馈存在进行中的在线回归作业，请等待完成或取消",
    "online_run_not_importable": "在线回归轮次只能通过作业比较/核验，不能人工导出或导入",
}

_VALID_MODES = ("offline", "live")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def reasoning_detail(reason: str, message: str | None = None) -> str:
    """中文说明 + 稳定 reason 码；调用方不得把正文拼进 message。"""
    base = (message or _REASON_MESSAGES.get(reason) or "回归操作被阻断").strip()
    if reason in base:
        return base
    return f"{base}（{reason}）"


def _fail(status_code: int, reason: str, message: str | None = None):
    raise HTTPException(status_code=status_code, detail=reasoning_detail(reason, message))


class _Unavailable(Exception):
    """适配层或服务前置检查给出的可用性阻断。"""

    def __init__(self, reason: str, message: str | None = None, status_code: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.message = message or _REASON_MESSAGES.get(reason) or "回归操作被阻断"
        self.status_code = status_code


@dataclass
class _CaseContext:
    feedback: AnswerFeedback
    message: Message
    conversation: Conversation
    case: dict
    resolved_sources: list[dict]
    kb_id: str | None
    mode: str
    cause: str
    case_sha256: str
    dataset_sha256: str
    binding_sha256: str


def _adapter():
    """延迟导入适配层。

    适配文件由独立子任务落盘；延迟导入让模型/schema/API 可以在适配层尚未
    出现时完成静态检查，同时正式调用仍严格依赖它。
    """
    from app.services import feedback_regression_evaluation

    return feedback_regression_evaluation


def _store_report_error(exc: Exception) -> HTTPException:
    """把适配层报告错误映射为稳定的 4xx，绝不回显报告正文。"""
    reason = getattr(exc, "reason", None) or "report_invalid"
    message = getattr(exc, "message", None) or "评测报告校验失败"
    status = getattr(exc, "http_status", None)
    if type(status) is not int or not 400 <= status <= 499:
        status = 400
    return HTTPException(status_code=status, detail=reasoning_detail(str(reason), str(message)))


# ---------------------------------------------------------------------------
# 读取与可用性探测
# ---------------------------------------------------------------------------


def _regression_for_feedback(
    db: DBSession, feedback_id: uuid.UUID, *, for_update: bool = False
) -> FeedbackRegression | None:
    stmt = select(FeedbackRegression).where(
        FeedbackRegression.feedback_id == feedback_id
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalar(stmt)


def _locked_feedback(db: DBSession, feedback_id: uuid.UUID) -> AnswerFeedback:
    feedback = db.scalar(
        select(AnswerFeedback)
        .where(AnswerFeedback.id == feedback_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    return feedback


def _locked_regression(db: DBSession, feedback_id: uuid.UUID) -> FeedbackRegression:
    regression = _regression_for_feedback(db, feedback_id, for_update=True)
    if regression is None:
        _fail(404, "regression_not_found", "该反馈尚未纳入回归")
    return regression


def _context_service():
    """延迟导入上下文服务（它依赖适配层，但本模块保持可静态导入）。"""
    from app.services import feedback_regression_context

    return feedback_regression_context


def _gate_roots(
    db: DBSession,
    admin: User,
    feedback_id: uuid.UUID,
    auth_token_version: int,
):
    """统一根门禁：先规划全部根，再按全局顺序加锁。

    旧 manual 回归（enroll/prepare/export/import）只锁/校验标注来源与非空限定范围
    所需的根，不枚举或冻结整库，也不套用在线执行清单的 256 项上限；在线作业入口
    应直接使用 ``feedback_regression_context.build_batch_contexts`` 的冻结节。
    """
    service = _context_service()
    try:
        plan = service.plan_roots(db, [feedback_id])
        return service.lock_roots(
            db,
            plan,
            admin_id=admin.id,
            auth_token_version=auth_token_version,
            freeze_manifests=False,
        )
    except service.RegressionContextError as exc:
        raise _Unavailable(exc.reason, exc.message, exc.status_code) from None


def _execution_from_locked(db: DBSession, locked, feedback_id: uuid.UUID):
    service = _context_service()
    try:
        batch = service.build_contexts(db, locked)
    except service.RegressionContextError as exc:
        raise _Unavailable(exc.reason, exc.message, exc.status_code) from None
    for execution in batch.contexts:
        if execution.feedback_id == str(feedback_id):
            return execution
    raise _Unavailable("root_plan_changed", "执行上下文缺少目标反馈")


def _case_context_from_execution(
    db: DBSession, execution, *, cause: str
) -> _CaseContext:
    feedback = db.get(AnswerFeedback, uuid.UUID(execution.feedback_id))
    message = db.get(Message, uuid.UUID(execution.message_id))
    conversation = db.get(Conversation, uuid.UUID(execution.conversation_id))
    if feedback is None or message is None or conversation is None:
        raise _Unavailable("feedback_stale", "原回答已不存在")
    case = execution.case
    resolved = [dict(source) for source in execution.resolved_sources]
    adapter = _adapter()
    try:
        binding = adapter.binding_sha256(
            adapter.base_binding_payload(
                case,
                feedback=feedback,
                resolved_sources=resolved,
                kb_id=execution.kb_id,
                mode=execution.mode,
                cause=cause,
            )
        )
        fingerprints = adapter.case_fingerprints(case)
    except adapter.RegressionUnavailable as exc:
        reason = getattr(exc, "reason", "source_invalid")
        message_text = getattr(exc, "message", None)
        raise _Unavailable(str(reason), message_text) from None

    return _CaseContext(
        feedback=feedback,
        message=message,
        conversation=conversation,
        case=case,
        resolved_sources=resolved,
        kb_id=execution.kb_id,
        mode=execution.mode,
        cause=cause,
        case_sha256=fingerprints["case_sha256"],
        dataset_sha256=fingerprints["dataset_sha256"],
        binding_sha256=binding,
    )


def _inspect_case(
    db: DBSession, feedback_id: uuid.UUID, *, cause: str
) -> _CaseContext:
    """只读可用性探测；不加锁、不要求管理员 token。"""
    service = _context_service()
    try:
        execution = service.inspect_context(db, feedback_id)
    except service.RegressionContextError as exc:
        raise _Unavailable(exc.reason, exc.message, exc.status_code) from None
    return _case_context_from_execution(db, execution, cause=cause)


def _enrollment_matches(regression: FeedbackRegression, ctx: _CaseContext) -> bool:
    return (
        regression.case_sha256 == ctx.case_sha256
        and regression.dataset_sha256 == ctx.dataset_sha256
        and regression.binding_sha256 == ctx.binding_sha256
    )


def _availability(
    db: DBSession, admin: User, feedback: AnswerFeedback, regression: FeedbackRegression
) -> tuple[_CaseContext | None, _Unavailable | None]:
    try:
        ctx = _inspect_case(db, feedback.id, cause=regression.cause)
    except _Unavailable as exc:
        return None, exc
    if not _enrollment_matches(regression, ctx):
        return ctx, _Unavailable("sample_changed")
    return ctx, None


# ---------------------------------------------------------------------------
# 轮次视图
# ---------------------------------------------------------------------------


def _run_read(
    run: FeedbackRegressionRun,
    *,
    current_attempt_no: int,
    state: str,
    job: FeedbackRegressionJob | None = None,
) -> dict:
    failures = run.failures if isinstance(run.failures, list) else []
    execution_kind = getattr(run, "execution_kind", "manual") or "manual"
    return {
        "run_id": run.id,
        "attempt_no": run.attempt_no,
        "is_current": run.attempt_no == current_attempt_no,
        "state": state,
        "execution_kind": execution_kind,
        "can_import": execution_kind != EXECUTION_KIND_ONLINE,
        "job_id": job.id if job is not None else None,
        "job_status": job.status if job is not None else None,
        "job_revision": job.revision if job is not None else None,
        "requested_mode": run.requested_mode,
        "protocol_version": run.protocol_version,
        "case_sha256": run.case_sha256,
        "dataset_sha256": run.dataset_sha256,
        "binding_sha256": run.binding_sha256,
        "prepared_by_name": run.prepared_by_name,
        "created_at": run.created_at,
        "imported_at": run.imported_at,
        "imported_by_name": run.imported_by_name,
        "report_mode": run.report_mode,
        "validation_level": run.validation_level,
        "passed": run.passed,
        "failures": [str(item) for item in failures if isinstance(item, str)],
        "outcome": run.outcome,
        "http_status": run.http_status,
        "citation_count": run.citation_count,
        "dataset_report_sha256": run.dataset_report_sha256,
        "report_sha256": run.report_sha256,
        "result_sha256": run.result_sha256,
        "machine_sha256": run.machine_sha256,
    }


def _current_run(
    db: DBSession, regression: FeedbackRegression
) -> FeedbackRegressionRun | None:
    if regression.current_attempt_no <= 0:
        return None
    return db.scalar(
        select(FeedbackRegressionRun).where(
            FeedbackRegressionRun.regression_id == regression.id,
            FeedbackRegressionRun.attempt_no == regression.current_attempt_no,
        )
    )


def _job_for_run(
    db: DBSession, run: FeedbackRegressionRun
) -> FeedbackRegressionJob | None:
    return db.scalar(
        select(FeedbackRegressionJob).where(FeedbackRegressionJob.run_id == run.id)
    )


def _jobs_for_runs(
    db: DBSession, runs: list[FeedbackRegressionRun]
) -> dict[uuid.UUID, FeedbackRegressionJob]:
    run_ids = [run.id for run in runs]
    if not run_ids:
        return {}
    return {
        job.run_id: job
        for job in db.scalars(
            select(FeedbackRegressionJob).where(
                FeedbackRegressionJob.run_id.in_(run_ids)
            )
        ).all()
    }


def _current_run_read(
    db: DBSession,
    admin: User,
    feedback: AnswerFeedback,
    regression: FeedbackRegression,
) -> dict | None:
    current = _current_run(db, regression)
    if current is None:
        return None
    state = _run_states(db, admin, feedback, regression, [current])[current.id]
    return _run_read(
        current,
        current_attempt_no=regression.current_attempt_no,
        state=state,
        job=_job_for_run(db, current),
    )


def _run_states(
    db: DBSession,
    admin: User,
    feedback: AnswerFeedback,
    regression: FeedbackRegression,
    runs: list[FeedbackRegressionRun],
) -> dict[uuid.UUID, str]:
    """按当前绑定实时判定历史轮次状态；不修改任何行。

    online 轮次不是“待导入”工件，固定返回 online；其实际执行状态由关联作业
    （``job_status``）表达，不能与旧 manual 的 awaiting_report/imported 混淆。
    """
    manual_runs = [
        run
        for run in runs
        if (getattr(run, "execution_kind", "manual") or "manual")
        != EXECUTION_KIND_ONLINE
    ]
    states = {
        run.id: ("awaiting_report" if run.imported_at is None else "stale")
        for run in manual_runs
    }
    manual_ids = {run.id for run in manual_runs}
    for run in runs:
        if run.id not in manual_ids:
            states[run.id] = "online"
    imported = [run for run in manual_runs if run.imported_at is not None]
    if not imported:
        return states

    ctx, unavailable = _availability(db, admin, feedback, regression)
    if unavailable is not None or ctx is None:
        # 当前样例已不可重建：已导入历史只能标 stale，不能沿用为当前通过。
        return states

    adapter = _adapter()
    cache: dict[str, tuple[dict, str] | None] = {}
    for run in imported:
        mode = run.requested_mode
        if mode not in _VALID_MODES:
            continue
        if mode not in cache:
            try:
                mode_case = adapter.mark_mode(ctx.case, mode)
                fingerprints = adapter.case_fingerprints(mode_case)
                binding = adapter.binding_sha256(
                    adapter.base_binding_payload(
                        mode_case,
                        feedback=feedback,
                        resolved_sources=ctx.resolved_sources,
                        kb_id=ctx.kb_id,
                        mode=ctx.mode,
                        cause=ctx.cause,
                    )
                )
                cache[mode] = (fingerprints, binding)
            except (adapter.RegressionUnavailable, EvaluationError, ValueError, TypeError):
                cache[mode] = None
        value = cache.get(mode)
        if value is None:
            continue
        fingerprints, binding = value
        if (
            binding == run.binding_sha256
            and fingerprints["case_sha256"] == run.case_sha256
            and fingerprints["dataset_sha256"] == run.dataset_sha256
            and run.feedback_revision == feedback.revision
            and run.feedback_result_sha256 == feedback.result_sha256
        ):
            states[run.id] = "imported"
    return states


def _prepare_read(
    regression: FeedbackRegression,
    run: FeedbackRegressionRun,
    *,
    jsonl: str,
    filename: str,
) -> dict:
    current_attempt = regression.current_attempt_no
    state = "awaiting_report" if run.imported_at is None else "imported"
    return {
        "regression_revision": regression.revision,
        "run": _run_read(run, current_attempt_no=current_attempt, state=state),
        "filename": filename,
        "case_count": 1,
        "dataset_sha256": run.dataset_sha256,
        "protocol_version": run.protocol_version,
        "jsonl": jsonl,
        "cli_hint": _cli_hint(run.requested_mode, filename, run.id),
    }


def _filename(feedback_id: uuid.UUID, attempt_no: int) -> str:
    return f"feedback-regression-{feedback_id}-attempt-{attempt_no}.jsonl"


def _cli_hint(mode: str, filename: str, run_id: uuid.UUID) -> str:
    base = (
        f"python -m scripts.evaluate_rag --dataset {filename} "
        f"--run-id {run_id} --require-reviewed"
    )
    if mode == "live":
        return base + " --live"
    return base + " --responses <离线响应 JSONL>"


def _jsonl(case: dict) -> str:
    return (
        json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )


def _mode_case(
    ctx: _CaseContext, mode: str
) -> tuple[dict, dict, str]:
    """模式化 case + 本轮指纹/绑定摘要（不含 run/attempt/revision）。"""
    adapter = _adapter()
    case = adapter.mark_mode(ctx.case, mode)
    fingerprints = adapter.case_fingerprints(case)
    binding = adapter.binding_sha256(
        adapter.base_binding_payload(
            case,
            feedback=ctx.feedback,
            resolved_sources=ctx.resolved_sources,
            kb_id=ctx.kb_id,
            mode=ctx.mode,
            cause=ctx.cause,
        )
    )
    return case, fingerprints, binding


def _assert_run_binding(
    run: FeedbackRegressionRun,
    ctx: _CaseContext,
    fingerprints: dict,
    binding: str,
) -> None:
    if (
        run.binding_sha256 != binding
        or run.case_sha256 != fingerprints["case_sha256"]
        or run.dataset_sha256 != fingerprints["dataset_sha256"]
        or run.feedback_revision != ctx.feedback.revision
        or run.feedback_result_sha256 != ctx.feedback.result_sha256
    ):
        _fail(409, "sample_changed", "回归样例已变化，请重新准备本轮重跑")


# ---------------------------------------------------------------------------
# GET 回归详情
# ---------------------------------------------------------------------------


def get_regression(
    db: DBSession, admin: User, feedback_id: uuid.UUID
) -> dict | None:
    """回归记录或 null；不写审计、不返回正文。"""
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    regression = _regression_for_feedback(db, feedback_id)
    if regression is None:
        return None

    ctx, unavailable = _availability(db, admin, feedback, regression)
    current_read = _current_run_read(db, admin, feedback, regression)
    return {
        "feedback_id": regression.feedback_id,
        "revision": regression.revision,
        "cause": regression.cause,
        "cause_note": regression.cause_note,
        "created_by_name": regression.created_by_name,
        "created_at": regression.created_at,
        "current_attempt_no": regression.current_attempt_no,
        "available": unavailable is None and ctx is not None,
        "unavailable_reason": None if unavailable is None else unavailable.reason,
        "current_run": current_read,
    }


# ---------------------------------------------------------------------------
# 纳入回归
# ---------------------------------------------------------------------------


def enroll(
    db: DBSession,
    admin: User,
    feedback_id: uuid.UUID,
    payload: FeedbackRegressionCreateRequest,
    *,
    auth_token_version: int,
) -> dict:
    try:
        locked = _gate_roots(db, admin, feedback_id, auth_token_version)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    if feedback.revision != payload.expected_feedback_revision:
        _fail(409, "revision_conflict", "反馈已被他人处理，请刷新")

    existing = _regression_for_feedback(db, feedback_id)
    if existing is not None:
        if (
            existing.cause == payload.cause
            and existing.cause_note == payload.cause_note
        ):
            return _regression_read(db, admin, feedback, existing)
        _fail(409, "regression_exists", "该反馈已按其他原因纳入回归")

    try:
        execution = _execution_from_locked(db, locked, feedback_id)
        ctx = _case_context_from_execution(db, execution, cause=payload.cause)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)

    regression = FeedbackRegression(
        feedback_id=feedback.id,
        cause=payload.cause,
        cause_note=payload.cause_note,
        created_by=admin.id,
        created_by_name=admin.username,
        revision=1,
        current_attempt_no=0,
        case_sha256=ctx.case_sha256,
        dataset_sha256=ctx.dataset_sha256,
        binding_sha256=ctx.binding_sha256,
    )
    db.add(regression)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # 唯一冲突后的重放也必须重新规划/加锁/复核原认证快照与当前 ACL。
        try:
            _gate_roots(db, admin, feedback_id, auth_token_version)
        except _Unavailable as exc:
            _fail(exc.status_code, exc.reason, exc.message)
        existing = _regression_for_feedback(db, feedback_id)
        if existing is None:
            raise
        if (
            existing.cause == payload.cause
            and existing.cause_note == payload.cause_note
        ):
            return _regression_read(db, admin, feedback, existing)
        _fail(409, "regression_exists", "该反馈已按其他原因纳入回归")
    db.refresh(regression)
    return _regression_read(db, admin, feedback, regression)


def _regression_read(
    db: DBSession,
    admin: User,
    feedback: AnswerFeedback,
    regression: FeedbackRegression,
) -> dict:
    ctx, unavailable = _availability(db, admin, feedback, regression)
    current_read = _current_run_read(db, admin, feedback, regression)
    return {
        "feedback_id": regression.feedback_id,
        "revision": regression.revision,
        "cause": regression.cause,
        "cause_note": regression.cause_note,
        "created_by_name": regression.created_by_name,
        "created_at": regression.created_at,
        "current_attempt_no": regression.current_attempt_no,
        "available": unavailable is None and ctx is not None,
        "unavailable_reason": None if unavailable is None else unavailable.reason,
        "current_run": current_read,
    }


# ---------------------------------------------------------------------------
# 准备重跑 / 当前轮导出
# ---------------------------------------------------------------------------


def _assert_live_capability(execution, mode: str) -> None:
    """旧 live CLI 不能承载固定范围/项目；在生成或导出 JSONL 前明确拒绝。"""
    if mode == "live" and execution.has_fixed_execution:
        _fail(
            409,
            "live_fixed_scope_unsupported",
            "旧 live CLI 不能承载固定文档范围或项目快照，请使用作业执行",
        )


def _prepare_existing(
    db: DBSession,
    admin: User,
    feedback: AnswerFeedback,
    regression: FeedbackRegression,
    run: FeedbackRegressionRun,
    requested_mode: str,
    execution,
) -> dict:
    if run.requested_mode != requested_mode:
        _fail(409, "prepare_mode_conflict", "该准备请求已按其他模式创建")
    if run.attempt_no != regression.current_attempt_no:
        _fail(409, "run_not_current", "该准备请求对应的轮次已不是当前轮")
    _assert_live_capability(execution, requested_mode)
    try:
        ctx = _case_context_from_execution(db, execution, cause=regression.cause)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    if not _enrollment_matches(regression, ctx):
        _fail(409, "sample_changed", "回归样例已变化，请重新纳入")
    mode_case, fingerprints, binding = _mode_case(ctx, requested_mode)
    _assert_run_binding(run, ctx, fingerprints, binding)
    return _prepare_read(
        regression,
        run,
        jsonl=_jsonl(mode_case),
        filename=_filename(feedback.id, run.attempt_no),
    )


def prepare_run(
    db: DBSession,
    admin: User,
    feedback_id: uuid.UUID,
    payload: FeedbackRegressionPrepareRequest,
    *,
    auth_token_version: int,
) -> dict:
    try:
        locked = _gate_roots(db, admin, feedback_id, auth_token_version)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    regression = _locked_regression(db, feedback_id)

    existing = db.scalar(
        select(FeedbackRegressionRun).where(
            FeedbackRegressionRun.regression_id == regression.id,
            FeedbackRegressionRun.prepare_request_id == payload.prepare_request_id,
        )
    )
    if existing is not None:
        try:
            execution = _execution_from_locked(db, locked, feedback_id)
        except _Unavailable as exc:
            _fail(exc.status_code, exc.reason, exc.message)
        return _prepare_existing(
            db, admin, feedback, regression, existing, payload.requested_mode, execution
        )

    if regression.revision != payload.expected_revision:
        _fail(409, "revision_conflict", "回归记录已被更新，请刷新")

    active_job = db.scalar(
        select(FeedbackRegressionJob.id)
        .where(
            FeedbackRegressionJob.regression_id == regression.id,
            FeedbackRegressionJob.status.in_(ACTIVE_JOB_STATUSES),
        )
        .limit(1)
    )
    if active_job is not None:
        _fail(409, "regression_job_active", "该反馈存在进行中的在线回归作业，请等待完成或取消")

    try:
        execution = _execution_from_locked(db, locked, feedback_id)
        ctx = _case_context_from_execution(db, execution, cause=regression.cause)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    _assert_live_capability(execution, payload.requested_mode)
    if not _enrollment_matches(regression, ctx):
        _fail(409, "sample_changed", "回归样例已变化，请重新纳入")

    mode_case, fingerprints, binding = _mode_case(ctx, payload.requested_mode)
    attempt_no = regression.current_attempt_no + 1
    run = FeedbackRegressionRun(
        regression_id=regression.id,
        attempt_no=attempt_no,
        prepare_request_id=payload.prepare_request_id,
        protocol_version=REGRESSION_PROTOCOL_VERSION,
        requested_mode=payload.requested_mode,
        execution_kind="manual",
        feedback_revision=feedback.revision,
        feedback_result_sha256=feedback.result_sha256,
        case_sha256=fingerprints["case_sha256"],
        dataset_sha256=fingerprints["dataset_sha256"],
        binding_sha256=binding,
        prepared_by=admin.id,
        prepared_by_name=admin.username,
    )
    db.add(run)
    regression.revision += 1
    regression.current_attempt_no = attempt_no
    regression.updated_at = utc_now()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # 并发同键重放：rollback 已释放全部根锁，必须重新鉴权/加锁。
        try:
            locked = _gate_roots(db, admin, feedback_id, auth_token_version)
        except _Unavailable as exc:
            _fail(exc.status_code, exc.reason, exc.message)
        feedback = db.get(AnswerFeedback, feedback_id)
        if feedback is None:
            _fail(404, "feedback_not_found", "反馈不存在")
        regression = _locked_regression(db, feedback_id)
        existing = db.scalar(
            select(FeedbackRegressionRun).where(
                FeedbackRegressionRun.regression_id == regression.id,
                FeedbackRegressionRun.prepare_request_id == payload.prepare_request_id,
            )
        )
        if existing is None:
            _fail(409, "prepare_conflict", "回归准备发生并发冲突，请重试")
        try:
            execution = _execution_from_locked(db, locked, feedback_id)
        except _Unavailable as exc:
            _fail(exc.status_code, exc.reason, exc.message)
        return _prepare_existing(
            db, admin, feedback, regression, existing, payload.requested_mode, execution
        )
    db.refresh(run)
    db.refresh(regression)
    return _prepare_read(
        regression,
        run,
        jsonl=_jsonl(mode_case),
        filename=_filename(feedback.id, attempt_no),
    )


def export_run(
    db: DBSession,
    admin: User,
    feedback_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    auth_token_version: int,
) -> dict:
    try:
        locked = _gate_roots(db, admin, feedback_id, auth_token_version)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    regression = _regression_for_feedback(db, feedback_id)
    if regression is None:
        _fail(404, "regression_not_found", "该反馈尚未纳入回归")
    run = db.get(FeedbackRegressionRun, run_id)
    if run is None or run.regression_id != regression.id:
        _fail(404, "run_not_found", "重跑轮次不存在")
    if getattr(run, "execution_kind", "manual") == EXECUTION_KIND_ONLINE:
        _fail(409, "online_run_not_importable")
    if run.attempt_no != regression.current_attempt_no:
        _fail(409, "run_not_current", "仅当前轮可以导出")

    try:
        execution = _execution_from_locked(db, locked, feedback_id)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    _assert_live_capability(execution, run.requested_mode)
    try:
        ctx = _case_context_from_execution(db, execution, cause=regression.cause)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    if not _enrollment_matches(regression, ctx):
        _fail(409, "sample_changed", "回归样例已变化，请重新准备")
    mode_case, fingerprints, binding = _mode_case(ctx, run.requested_mode)
    _assert_run_binding(run, ctx, fingerprints, binding)
    return _prepare_read(
        regression,
        run,
        jsonl=_jsonl(mode_case),
        filename=_filename(feedback_id, run.attempt_no),
    )


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------


def list_runs(
    db: DBSession,
    admin: User,
    feedback_id: uuid.UUID,
    *,
    page: int,
    page_size: int,
) -> tuple[int, list[dict]]:
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    regression = _regression_for_feedback(db, feedback_id)
    if regression is None:
        return 0, []

    total = db.scalar(
        select(func.count())
        .select_from(FeedbackRegressionRun)
        .where(FeedbackRegressionRun.regression_id == regression.id)
    ) or 0
    runs = list(
        db.scalars(
            select(FeedbackRegressionRun)
            .where(FeedbackRegressionRun.regression_id == regression.id)
            .order_by(
                FeedbackRegressionRun.attempt_no.desc(),
                FeedbackRegressionRun.id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    states = _run_states(db, admin, feedback, regression, runs)
    jobs = _jobs_for_runs(db, runs)
    return int(total), [
        _run_read(
            run,
            current_attempt_no=regression.current_attempt_no,
            state=states[run.id],
            job=jobs.get(run.id),
        )
        for run in runs
    ]


# ---------------------------------------------------------------------------
# 导入报告
# ---------------------------------------------------------------------------


def _validate_report(raw: bytes, *, case: dict, run: FeedbackRegressionRun) -> dict:
    adapter = _adapter()
    report_error = getattr(adapter, "RegressionReportError", None)
    try:
        return adapter.validate_regression_report(
            raw,
            case=case,
            run_id=str(run.id),
            requested_mode=run.requested_mode,
            dataset_sha256=run.dataset_sha256,
        )
    except adapter.RegressionUnavailable as exc:
        reason = str(getattr(exc, "reason", "sample_changed"))
        message = getattr(exc, "message", None)
        _fail(409, reason, message)
    except Exception as exc:  # noqa: BLE001 - 仅精确匹配适配层报告错误
        if report_error is not None and isinstance(exc, report_error):
            raise _store_report_error(exc) from None
        if isinstance(exc, EvaluationError):
            _fail(400, "report_invalid", "评测报告校验失败")
        raise


def import_report(
    db: DBSession,
    admin: User,
    feedback_id: uuid.UUID,
    run_id: uuid.UUID,
    *,
    expected_revision: int,
    raw: bytes,
    auth_token_version: int,
) -> dict:
    if len(raw) > REPORT_MAX_BYTES:
        _fail(413, "report_too_large", f"评测报告不能超过 {REPORT_MAX_BYTES} 字节")
    if not raw.strip():
        _fail(400, "report_empty", "评测报告为空")

    try:
        locked = _gate_roots(db, admin, feedback_id, auth_token_version)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)
    regression = _locked_regression(db, feedback_id)
    feedback = db.get(AnswerFeedback, feedback_id)
    if feedback is None:
        _fail(404, "feedback_not_found", "反馈不存在")
    run = db.get(FeedbackRegressionRun, run_id)
    if run is None or run.regression_id != regression.id:
        _fail(404, "run_not_found", "重跑轮次不存在")
    if getattr(run, "execution_kind", "manual") == EXECUTION_KIND_ONLINE:
        _fail(409, "online_run_not_importable")
    if run.attempt_no != regression.current_attempt_no:
        _fail(409, "run_not_current", "仅当前轮可以导入报告")
    if run.requested_mode not in _VALID_MODES:
        _fail(400, "report_invalid", "重跑轮次的执行模式不合法")

    try:
        execution = _execution_from_locked(db, locked, feedback_id)
    except _Unavailable as exc:
        _fail(exc.status_code, exc.reason, exc.message)

    def _validated_summary() -> dict:
        try:
            ctx = _case_context_from_execution(db, execution, cause=regression.cause)
        except _Unavailable as exc:
            _fail(exc.status_code, exc.reason, exc.message)
        if not _enrollment_matches(regression, ctx):
            _fail(409, "sample_changed", "回归样例已变化，请重新准备")
        mode_case, fingerprints, binding = _mode_case(ctx, run.requested_mode)
        _assert_run_binding(run, ctx, fingerprints, binding)
        summary = _validate_report(raw, case=mode_case, run=run)
        if not isinstance(summary.get("report_sha256"), str):
            _fail(400, "report_invalid", "评测报告缺少规范摘要")
        return summary

    if run.imported_at is not None:
        # 同一轮同一规范工件重复导入（含客户端超时重试）直接幂等返回；
        # 不要求调用方重新拿到已递增后的 revision。
        summary = _validated_summary()
        if summary["report_sha256"] == run.report_sha256:
            return {
                "regression_revision": regression.revision,
                "run": _run_read(
                    run,
                    current_attempt_no=regression.current_attempt_no,
                    state="imported",
                ),
            }
        _fail(409, "report_conflict", "该轮已导入不同的评测报告")

    # 新导入先在当前 revision 上 CAS，再做任何落库之外的校验。
    if regression.revision != expected_revision:
        _fail(409, "revision_conflict", "回归记录已被更新，请刷新")
    summary = _validated_summary()
    report_sha = summary["report_sha256"]

    failures = summary.get("failures")
    if not isinstance(failures, list):
        failures = []
    failures = [str(item) for item in failures if isinstance(item, str)]

    # 只有全部校验通过后才写白名单摘要；错误路径不落任何字段。
    run.imported_at = utc_now()
    run.imported_by = admin.id
    run.imported_by_name = admin.username
    run.report_sha256 = report_sha
    run.report_mode = summary.get("report_mode")
    run.validation_level = summary.get("validation_level")
    run.passed = summary.get("passed")
    run.failures = failures
    run.outcome = summary.get("outcome")
    run.http_status = summary.get("http_status")
    run.citation_count = summary.get("citation_count")
    run.dataset_report_sha256 = summary.get("dataset_report_sha256")
    run.result_sha256 = summary.get("result_sha256")
    run.machine_sha256 = summary.get("machine_sha256")
    regression.revision += 1
    regression.updated_at = utc_now()
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _fail(409, "import_conflict", "导入发生并发冲突，请刷新")
    db.refresh(run)
    db.refresh(regression)
    return {
        "regression_revision": regression.revision,
        "run": _run_read(
            run,
            current_attempt_no=regression.current_attempt_no,
            state="imported",
        ),
    }
