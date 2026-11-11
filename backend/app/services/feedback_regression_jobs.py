"""反馈回归在线作业服务：提交/幂等、状态机、租约、比较与人工核验。

R1b 的范围只有持久作业基础与公共 worker 契约；**本模块不调用任何模型、不启动
worker、不创建用户会话/轮次消息**。R2 的独立 worker 通过 ``claim`` /
``recover`` / ``heartbeat`` / ``load_execution`` / ``check_lease`` / ``complete``
/ ``fail`` / ``release`` 驱动作业，所有模型等待都在这些短事务之外进行。

事务责任（冻结契约）：

* 提交、取消、人工核验：一次事务内完成全部校验与写入；批量任一条失败则零写入。
* ``claim`` / ``recover`` / ``heartbeat`` / ``fail`` / ``release``：只锁作业行的
  短事务，函数自行 commit/rollback，绝不持根锁等待外部工作。
* ``load_execution`` / ``complete``：先按冻结顺序锁定全部根
  （User→KB→ProjectSource→Snapshot→Conversation→ChatTurn→Message→Document→
  Feedback），随后才锁 Regression→Run→Job；函数在返回/提交前释放连接，绝不
  在模型等待期间持有数据库锁。
* 发布只信服务端对真实新内容重算的 ``grade_case(..., include_content=False)``；
  调用方传入的 passed/指纹不被信任。

错误/日志边界：作业表只保存白名单错误码；正文只在作业 body 表且随反馈/作业级联
删除。所有面向列表/详情/审计的响应都不含答案、引用、trace 或原问题。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from fastapi import HTTPException
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.core.config import settings
from app.models import (
    AnswerFeedback,
    FeedbackRegression,
    FeedbackRegressionBatch,
    FeedbackRegressionJob,
    FeedbackRegressionJobBody,
    FeedbackRegressionRun,
    ModelUsage,
    User,
)
from app.models.feedback_regression import REGRESSION_PROTOCOL_VERSION
from app.models.feedback_regression_job import (
    ACTIVE_JOB_STATUSES,
    EXECUTION_KIND_ONLINE,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    JOB_STATUSES,
    REVIEW_STATUSES,
    REVIEW_STATUS_UNKNOWN,
    TERMINAL_JOB_STATUSES,
)
from app.services import answer_feedback, answer_sources
from app.services.evaluation import EvaluationError, grade_case
from app.services.feedback_regression import reasoning_detail
from app.services.usage_tracking import UsageContext

# 新结果正文的硬上限：白名单 JSON 的 UTF-8 字节数。
MAX_RESULT_BODY_BYTES = 1024 * 1024
MAX_RESULT_ANSWER_CHARS = 131072
MAX_RESULT_CITATIONS = 512
MAX_RESULT_FAILURES = 64
MAX_RECOVERY_BATCH = 50
# 列表/批量一次最多聚合的作业数（API 分页上限 100、批量上限 20）。
MAX_USAGE_JOB_IDS = 100

# 稳定错误码白名单；未知异常/码一律收敛为 execution_failed，绝不落原异常消息。
ERROR_CODES = frozenset({
    "answer_too_large",
    "attempts_exhausted",
    "cancelled",
    "deadline_exceeded",
    "execution_failed",
    "generation_empty",
    "internal_error",
    "invalid_result",
    "lease_expired",
    "model_error",
    "model_timeout",
    "model_unavailable",
    "retrieval_failed",
    "source_verification_failed",
    "stream_interrupted",
})
# 只有显式标记为可恢复且属于该集合的错误才允许自动重试。
RETRYABLE_ERROR_CODES = frozenset({
    "internal_error",
    "model_error",
    "model_timeout",
    "model_unavailable",
    "retrieval_failed",
    "stream_interrupted",
})

_MESSAGES = {
    "admin_required": "需要管理员权限",
    "auth_invalid": "登录状态已失效，请重新登录",
    "batch_not_found": "回归批次不存在",
    "feedback_not_found": "反馈不存在",
    "invalid_batch": "批量回归请求不合法",
    "invalid_request": "回归作业请求不合法",
    "job_active": "该反馈已有进行中的在线回归作业",
    "job_conflict": "回归作业发生并发冲突，请刷新",
    "job_not_cancellable": "当前作业状态不能取消",
    "job_not_found": "回归作业不存在",
    "job_not_succeeded": "仅成功的回归作业可以比较或核验",
    "lease_invalid": "作业租约已失效",
    "revision_conflict": "回归记录已被更新，请刷新",
    "regression_job_active": "该反馈存在进行中的在线回归作业，请等待完成或取消",
    "regression_not_found": "该反馈尚未纳入回归",
    "request_id_conflict": "相同请求标识已绑定不同的请求内容或路由",
    "request_not_found": "未找到该请求标识对应的提交结果",
    "result_invalid": "回归结果不满足白名单结构",
    "result_missing": "回归结果正文不存在",
    "result_too_large": "回归结果正文超过大小上限",
    "review_conflict": "作业已被更新，请刷新",
    "sample_changed": "回归样例已变化，请重新纳入或准备",
    "source_verification_failed": "实际来源复核失败",
}

_IDENTITY_KEYS = (
    "feedback_id",
    "feedback_revision",
    "feedback_result_sha256",
    "owner_id",
    "conversation_id",
    "message_id",
    "turn_id",
    "kb_id",
    "mode",
    "execution_kind",
)
_PROJECT_KEYS = (
    "snapshot_id",
    "source_id",
    "manifest_fingerprint",
    "config_fingerprint",
)
_TRACE_SOURCE_KEYS = (
    "source_kind",
    "document_id",
    "filename",
    "index_version",
    "chunk_index",
    "chunk_start",
    "chunk_end",
    "file_version_id",
    "snapshot_id",
    "source_id",
    "relative_path",
    "manifest_fingerprint",
    "config_fingerprint",
    "score",
)
_CITATION_KEYS = (
    "source_index",
    "source_kind",
    "document_id",
    "filename",
    "index_version",
    "chunk_index",
    "chunk_start",
    "chunk_end",
    "file_version_id",
    "snapshot_id",
    "source_id",
    "relative_path",
    "manifest_fingerprint",
    "config_fingerprint",
    "score",
    "snippet",
    "language",
    "symbol",
    "symbol_kind",
)
_TRACE_NUMBER_KEYS = (
    "rewrite_ms",
    "retrieve_ms",
    "generation_ms",
    "first_token_ms",
    "total_ms",
)
_TOP_K_ADVISORY_NS = 741_912_006


class JobError(Exception):
    """稳定的作业/API 失败；``status_code`` 供 HTTP 层映射。"""

    def __init__(self, reason: str, message: str | None = None, status_code: int = 409):
        super().__init__(reason)
        self.reason = reason
        self.message = message or _MESSAGES.get(reason) or "回归作业操作被阻断"
        self.status_code = status_code


class JobResultError(JobError):
    """执行结果不满足白名单结构；worker 应调用 ``fail`` 记录白名单错误码。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_http(exc: JobError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=reasoning_detail(exc.reason, exc.message),
    )


def _fail(status_code: int, reason: str, message: str | None = None):
    raise JobError(reason, message, status_code)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _advisory_lock(db: DBSession, admin_id: uuid.UUID, request_id: uuid.UUID) -> None:
    """同一管理员 request_id 的跨路由/跨进程提交串行化。

    键作用域含管理员 UUID：不同管理员的相同 UUID 不互相阻塞，也不共享幂等绑定。
    """
    digest = hashlib.sha256(b"admin:" + admin_id.bytes + b":request:" + request_id.bytes).digest()
    key = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _TOP_K_ADVISORY_NS, "key": key},
    )


def _request_sha(kind: str, payload: dict) -> str:
    """路由种类 + 规范请求摘要；批量条目按反馈 UUID 升序归一。"""
    return _sha256({"kind": kind, "payload": payload})


# ---------------------------------------------------------------------------
# 上下文/绑定
# ---------------------------------------------------------------------------


def _context_service():
    from app.services import feedback_regression_context

    return feedback_regression_context


def _adapter():
    from app.services import feedback_regression_evaluation

    return feedback_regression_evaluation


def _context_error(exc: Exception) -> JobError:
    return JobError(
        str(getattr(exc, "reason", "invalid_request")),
        getattr(exc, "message", None),
        int(getattr(exc, "status_code", 409)),
    )


def _build_locked(
    db: DBSession,
    *,
    admin_id: uuid.UUID,
    auth_token_version: int,
    feedback_ids: Sequence[uuid.UUID],
    fixed_manifests: Mapping[uuid.UUID, Sequence | None] | None = None,
    fixed_projects: Mapping[uuid.UUID, Mapping] | None = None,
):
    """规划 -> 加锁 -> 构建上下文；不 commit，不在锁内等待外部工作。"""
    service = _context_service()
    try:
        plan = service.plan_roots(db, feedback_ids)
        locked = service.lock_roots(
            db,
            plan,
            admin_id=admin_id,
            auth_token_version=auth_token_version,
            fixed_manifests=fixed_manifests,
        )
        contexts = service.build_contexts(
            db, locked, fixed_projects=fixed_projects
        )
    except service.RegressionContextError as exc:
        raise _context_error(exc) from None
    return locked, contexts.by_feedback()


def _lock_regressions(
    db: DBSession, feedback_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, FeedbackRegression]:
    identifiers = list(feedback_ids)
    rows = db.scalars(
        select(FeedbackRegression)
        .where(FeedbackRegression.feedback_id.in_(identifiers))
        .order_by(FeedbackRegression.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    by_feedback = {row.feedback_id: row for row in rows}
    if set(by_feedback) != set(identifiers):
        _fail(404, "regression_not_found")
    return by_feedback


def _enrollment_values(execution, feedback: AnswerFeedback, cause: str) -> tuple[str, str, str]:
    """与旧 manual 纳入完全相同的基准 case 指纹与绑定摘要。"""
    adapter = _adapter()
    try:
        fingerprints = adapter.case_fingerprints(execution.case)
        binding = adapter.binding_sha256(
            adapter.base_binding_payload(
                execution.case,
                feedback=feedback,
                resolved_sources=list(execution.resolved_sources),
                kb_id=execution.kb_id,
                mode=execution.mode,
                cause=cause,
            )
        )
    except adapter.RegressionUnavailable as exc:
        raise JobError(str(exc.reason), str(exc.message)) from None
    return fingerprints["case_sha256"], fingerprints["dataset_sha256"], binding


def _online_run_values(execution, feedback: AnswerFeedback, cause: str) -> tuple[dict, str]:
    """在线 live 轮次的 mode 化 case 指纹与绑定摘要（用于 run/job 冻结）。"""
    adapter = _adapter()
    try:
        case = adapter.mark_mode(execution.case, "live")
        fingerprints = adapter.case_fingerprints(case)
        binding = adapter.binding_sha256(
            adapter.base_binding_payload(
                case,
                feedback=feedback,
                resolved_sources=list(execution.resolved_sources),
                kb_id=execution.kb_id,
                mode=execution.mode,
                cause=cause,
            )
        )
    except (adapter.RegressionUnavailable, ValueError) as exc:
        raise JobError(
            str(getattr(exc, "reason", "sample_changed")),
            getattr(exc, "message", None),
        ) from None
    return fingerprints, binding


def _assert_enrollment(
    regression: FeedbackRegression,
    execution,
    feedback: AnswerFeedback,
) -> None:
    case_sha, dataset_sha, binding = _enrollment_values(
        execution, feedback, regression.cause
    )
    if (
        regression.case_sha256 != case_sha
        or regression.dataset_sha256 != dataset_sha
        or regression.binding_sha256 != binding
    ):
        _fail(409, "sample_changed")


def _assert_run_binding(
    run: FeedbackRegressionRun,
    execution,
    feedback: AnswerFeedback,
    cause: str,
) -> None:
    fingerprints, binding = _online_run_values(execution, feedback, cause)
    if (
        run.case_sha256 != fingerprints["case_sha256"]
        or run.dataset_sha256 != fingerprints["dataset_sha256"]
        or run.binding_sha256 != binding
    ):
        _fail(409, "sample_changed")


def _summary_fixed_bindings(job: FeedbackRegressionJob):
    """从作业摘要恢复冻结身份；绝不接受缺失/漂移的摘要。"""
    summary = job.context_summary if isinstance(job.context_summary, dict) else {}
    kind = summary.get("execution_kind")
    if kind not in ("upload", "project", "chat"):
        _fail(409, "invalid_request", "作业执行上下文摘要不合法")
    manifest = summary.get("upload_manifest")
    project = summary.get("project")
    if kind == "upload":
        if not isinstance(manifest, list):
            _fail(409, "manifest_invalid", "作业执行清单已丢失")
    elif kind == "project":
        if not isinstance(project, dict) or not all(
            isinstance(project.get(key), str) and project.get(key) for key in _PROJECT_KEYS
        ):
            _fail(409, "project_snapshot_changed", "作业项目绑定已丢失")
        if manifest is not None:
            _fail(409, "manifest_invalid", "项目作业不能携带上传执行清单")
    else:
        if manifest is not None or project is not None:
            _fail(409, "invalid_request", "普通对话作业摘要不合法")
    fixed_manifests = (
        {job.feedback_id: manifest} if manifest is not None else None
    )
    fixed_projects = (
        {job.feedback_id: {key: project[key] for key in _PROJECT_KEYS}}
        if isinstance(project, dict)
        else None
    )
    return fixed_manifests, fixed_projects


@dataclass(frozen=True)
class _FeedbackSnapshot:
    """绑定计算所需的反馈身份快照；不持有 Session/ORM 状态。"""

    id: uuid.UUID
    revision: int
    result_sha256: str


def _assert_execution_matches_job(
    execution,
    regression: FeedbackRegression,
    run: FeedbackRegressionRun,
    job: FeedbackRegressionJob,
) -> None:
    summary = job.context_summary if isinstance(job.context_summary, dict) else {}
    for key in _IDENTITY_KEYS:
        if summary.get(key) != getattr(execution, key):
            _fail(409, "root_plan_changed", "作业执行上下文与冻结摘要不一致")
    stored_manifest = summary.get("upload_manifest")
    current_manifest = (
        None
        if execution.upload_manifest is None
        else [dict(entry) for entry in execution.upload_manifest]
    )
    if stored_manifest != current_manifest:
        _fail(409, "manifest_invalid", "执行清单与冻结摘要不一致")
    stored_project = summary.get("project")
    current_project = (
        None
        if execution.project is None
        else {key: execution.project.get(key) for key in _PROJECT_KEYS}
    )
    if stored_project != current_project:
        _fail(409, "project_snapshot_changed", "项目绑定与冻结摘要不一致")
    if execution.root_sha256 != job.root_sha256:
        _fail(409, "root_plan_changed", "执行根身份与作业冻结值不一致")
    if run.execution_kind != "online" or run.regression_id != regression.id:
        _fail(409, "invalid_request", "作业关联的轮次不合法")
    if job.regression_id != regression.id or run.id != job.run_id:
        _fail(409, "invalid_request", "作业与轮次关联不一致")
    if job.feedback_id != uuid.UUID(execution.feedback_id):
        _fail(409, "invalid_request", "作业与反馈关联不一致")
    snapshot = regression_feedback(execution)
    _assert_enrollment(regression, execution, snapshot)
    _assert_run_binding(run, execution, snapshot, regression.cause)
    if (
        job.case_sha256 != run.case_sha256
        or job.dataset_sha256 != run.dataset_sha256
        or job.binding_sha256 != run.binding_sha256
    ):
        _fail(409, "sample_changed", "作业结果绑定与轮次不一致")


def regression_feedback(execution) -> _FeedbackSnapshot:
    """构造只读反馈身份快照供绑定计算；不触发 Session 查询。"""
    return _FeedbackSnapshot(
        id=uuid.UUID(execution.feedback_id),
        revision=execution.feedback_revision,
        result_sha256=execution.feedback_result_sha256,
    )


def _execution_for_feedback(contexts, feedback_id: uuid.UUID):
    execution = contexts.get(feedback_id)
    if execution is None:
        _fail(409, "root_plan_changed", "执行上下文缺少目标反馈")
    return execution


def _canonicalize_frozen_manifests(
    db: DBSession,
    *,
    admin_id: uuid.UUID,
    auth_token_version: int,
    feedback_ids: Sequence[uuid.UUID],
    contexts: Mapping,
) -> Mapping:
    """把首次冻结的上传清单按上下文服务同样的 UUID 顺序规范化。

    ``lock_roots`` 对显式清单会排序；首次冻结（尤其限定范围）可能保留会话顺序。
    重新以显式 ``fixed_manifests`` 构建一次，确保入队时写入的 root_sha 与后续
    worker/比较重建完全一致，且不会把新文档混入。
    """
    manifests = {
        feedback_id: [dict(entry) for entry in execution.upload_manifest]
        for feedback_id, execution in contexts.items()
        if execution.upload_manifest is not None
    }
    if not manifests:
        return contexts
    _locked, rebuilt = _build_locked(
        db,
        admin_id=admin_id,
        auth_token_version=auth_token_version,
        feedback_ids=feedback_ids,
        fixed_manifests=manifests,
    )
    return rebuilt


# ---------------------------------------------------------------------------
# 读取视图（无正文）
# ---------------------------------------------------------------------------


def _result_read(job: FeedbackRegressionJob) -> dict | None:
    if job.result_sha256 is None and job.result_captured_at is None:
        return None
    failures = job.result_failures if isinstance(job.result_failures, list) else []
    return {
        "passed": job.result_passed,
        "failures": [str(item) for item in failures if isinstance(item, str)][
            :MAX_RESULT_FAILURES
        ],
        "outcome": job.result_outcome,
        "http_status": job.result_http_status,
        "citation_count": job.citation_count,
        "result_sha256": job.result_sha256,
        "machine_sha256": job.machine_sha256,
        "captured_at": job.result_captured_at,
    }


def _review_read(job: FeedbackRegressionJob) -> dict:
    status = job.review_status if job.review_status in REVIEW_STATUSES else REVIEW_STATUS_UNKNOWN
    # 核验结果与当前结果指纹不一致时不得混用旧核验（正常写入路径不会发生）。
    if job.review_result_sha256 not in (None, job.result_sha256):
        status = REVIEW_STATUS_UNKNOWN
    return {
        "status": status,
        "notes": job.review_notes or "",
        "reviewed_by_name": job.reviewed_by_name,
        "reviewed_at": job.reviewed_at,
        "result_sha256": job.review_result_sha256,
    }


def _binding_read(job: FeedbackRegressionJob) -> dict:
    return {
        "root_sha256": job.root_sha256,
        "case_sha256": job.case_sha256,
        "dataset_sha256": job.dataset_sha256,
        "binding_sha256": job.binding_sha256,
    }


def _money(value: Decimal | None) -> str | None:
    return format(value, ".10f") if value is not None else None


def _empty_usage() -> dict:
    return {
        "attempts": 0,
        "succeeded_attempts": 0,
        "failed_attempts": 0,
        "cancelled_attempts": 0,
        "interrupted_attempts": 0,
        "started_attempts": 0,
        "other_attempts": 0,
        "input_tokens": None,
        "output_tokens": None,
        "cached_input_tokens": None,
        "total_tokens": None,
        "known_input_tokens": None,
        "known_output_tokens": None,
        "usage_known_attempts": 0,
        "usage_missing_attempts": 0,
        "priced_attempts": 0,
        "unpriced_attempts": 0,
        "unfinished_attempts": 0,
        "currency": None,
        "currency_mismatch": False,
        "known_cost_subtotal": None,
        "total_cost": None,
    }


def job_usage(db: DBSession, job_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """一次分组查询聚合多个作业的实际用量，避免列表/批量 N+1。

    只读 ``ModelUsage``，包含全部 operation 与所有重试尝试；缺测/未定价/多币种
    一律保持 ``None``，不把未知补成 0。单次最多 ``MAX_USAGE_JOB_IDS`` 个作业。
    """
    identifiers: list[uuid.UUID] = []
    for value in job_ids:
        identifier = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        if identifier not in identifiers:
            identifiers.append(identifier)
    if not identifiers:
        return {}
    if len(identifiers) > MAX_USAGE_JOB_IDS:
        _fail(422, "invalid_request")
    rows = db.execute(
        select(
            ModelUsage.regression_job_id.label("job_id"),
            ModelUsage.currency.label("currency"),
            func.count().label("attempts"),
            func.sum(
                case((ModelUsage.status == "succeeded", 1), else_=0)
            ).label("succeeded_attempts"),
            func.sum(
                case((ModelUsage.status == "failed", 1), else_=0)
            ).label("failed_attempts"),
            func.sum(
                case((ModelUsage.status == "cancelled", 1), else_=0)
            ).label("cancelled_attempts"),
            func.sum(
                case((ModelUsage.status == "interrupted", 1), else_=0)
            ).label("interrupted_attempts"),
            func.sum(
                case((ModelUsage.status == "started", 1), else_=0)
            ).label("started_attempts"),
            func.sum(ModelUsage.input_tokens).label("input_tokens"),
            func.sum(ModelUsage.output_tokens).label("output_tokens"),
            func.sum(ModelUsage.cached_input_tokens).label("cached_input_tokens"),
            # 已知 token 小计逐列累计实际测到的值：部分缺测时仍可展示已测部分，
            # 而不是只给一个 null 总量。该列一次都没测到时才收敛为 null。
            func.sum(ModelUsage.input_tokens).label("known_input_tokens"),
            func.sum(ModelUsage.output_tokens).label("known_output_tokens"),
            func.sum(
                case((ModelUsage.input_tokens.is_not(None), 1), else_=0)
            ).label("known_input_attempts"),
            func.sum(
                case((ModelUsage.output_tokens.is_not(None), 1), else_=0)
            ).label("known_output_attempts"),
            func.sum(
                case(
                    (
                        or_(
                            ModelUsage.input_tokens.is_(None),
                            ModelUsage.output_tokens.is_(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("usage_missing_attempts"),
            func.sum(
                case((ModelUsage.estimated_cost.is_not(None), 1), else_=0)
            ).label("priced_attempts"),
            func.sum(
                case(
                    (
                        or_(
                            ModelUsage.finished_at.is_(None),
                            ModelUsage.status == "started",
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("unfinished_attempts"),
            func.sum(ModelUsage.estimated_cost).label("known_cost"),
        )
        .where(ModelUsage.regression_job_id.in_(identifiers))
        .group_by(ModelUsage.regression_job_id, ModelUsage.currency)
    ).all()
    states: dict[uuid.UUID, dict] = {}
    for row in rows:
        state = states.setdefault(
            row.job_id,
            {
                "attempts": 0,
                "succeeded_attempts": 0,
                "failed_attempts": 0,
                "cancelled_attempts": 0,
                "interrupted_attempts": 0,
                "started_attempts": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "known_input_tokens": 0,
                "known_output_tokens": 0,
                "known_input_attempts": 0,
                "known_output_attempts": 0,
                "usage_missing_attempts": 0,
                "priced_attempts": 0,
                "unfinished_attempts": 0,
                "currencies": set(),
                "currency_missing": False,
                "known_cost": Decimal("0"),
            },
        )
        attempts = int(row.attempts or 0)
        state["attempts"] += attempts
        for key in (
            "succeeded_attempts",
            "failed_attempts",
            "cancelled_attempts",
            "interrupted_attempts",
            "started_attempts",
            "usage_missing_attempts",
            "priced_attempts",
            "unfinished_attempts",
            "known_input_attempts",
            "known_output_attempts",
        ):
            state[key] += int(getattr(row, key) or 0)
        state["input_tokens"] += int(row.input_tokens or 0)
        state["output_tokens"] += int(row.output_tokens or 0)
        state["cached_input_tokens"] += int(row.cached_input_tokens or 0)
        state["known_input_tokens"] += int(row.known_input_tokens or 0)
        state["known_output_tokens"] += int(row.known_output_tokens or 0)
        if row.known_cost is not None:
            state["known_cost"] += Decimal(row.known_cost)
        currency = row.currency if isinstance(row.currency, str) else ""
        if currency:
            state["currencies"].add(currency)
        else:
            state["currency_missing"] = True
    result: dict[uuid.UUID, dict] = {}
    for job_id in identifiers:
        state = states.get(job_id)
        if state is None:
            result[job_id] = _empty_usage()
            continue
        attempts = state["attempts"]
        missing = state["usage_missing_attempts"]
        known = max(0, attempts - missing)
        currencies = state["currencies"]
        currency_mismatch = state["currency_missing"] or len(currencies) > 1
        currency = (
            next(iter(currencies))
            if len(currencies) == 1 and not state["currency_missing"]
            else None
        )
        if attempts and missing == 0:
            input_tokens = state["input_tokens"]
            output_tokens = state["output_tokens"]
            cached_tokens = state["cached_input_tokens"]
            total_tokens = input_tokens + output_tokens
        else:
            input_tokens = output_tokens = cached_tokens = total_tokens = None
        # 部分调用缺测时仍保留实际已测部分的小计；该列一次都没测到时为 null。
        known_input_tokens = (
            state["known_input_tokens"] if state["known_input_attempts"] > 0 else None
        )
        known_output_tokens = (
            state["known_output_tokens"] if state["known_output_attempts"] > 0 else None
        )
        known_cost_subtotal = None
        if state["priced_attempts"] and not currency_mismatch and len(currencies) == 1:
            known_cost_subtotal = _money(state["known_cost"])
        total_cost = None
        if (
            attempts
            and missing == 0
            and state["priced_attempts"] == attempts
            and state["unfinished_attempts"] == 0
            and not currency_mismatch
            and len(currencies) == 1
        ):
            total_cost = _money(state["known_cost"])
        result[job_id] = {
            "attempts": attempts,
            "succeeded_attempts": state["succeeded_attempts"],
            "failed_attempts": state["failed_attempts"],
            "cancelled_attempts": state["cancelled_attempts"],
            "interrupted_attempts": state["interrupted_attempts"],
            "started_attempts": state["started_attempts"],
            "other_attempts": max(
                0,
                attempts
                - state["succeeded_attempts"]
                - state["failed_attempts"]
                - state["cancelled_attempts"]
                - state["interrupted_attempts"]
                - state["started_attempts"],
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_tokens,
            "total_tokens": total_tokens,
            "known_input_tokens": known_input_tokens,
            "known_output_tokens": known_output_tokens,
            "usage_known_attempts": known,
            "usage_missing_attempts": missing,
            "priced_attempts": state["priced_attempts"],
            "unpriced_attempts": max(0, attempts - state["priced_attempts"]),
            "unfinished_attempts": state["unfinished_attempts"],
            "currency": currency,
            "currency_mismatch": currency_mismatch,
            "known_cost_subtotal": known_cost_subtotal,
            "total_cost": total_cost,
        }
    return result


def job_read(job: FeedbackRegressionJob, usage: Mapping | None) -> dict:
    return {
        "job_id": job.id,
        "feedback_id": job.feedback_id,
        "regression_id": job.regression_id,
        "batch_id": job.batch_id,
        "run_id": job.run_id,
        "run_attempt_no": job.run_attempt_no,
        "status": job.status,
        "revision": job.revision,
        "attempt_count": job.attempt_count,
        "execution_kind": "online",
        "requested_mode": "live",
        "can_import": False,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "available_at": job.available_at,
        "started_at": job.started_at,
        "deadline_at": job.deadline_at,
        "completed_at": job.completed_at,
        "cancelled_at": job.cancelled_at,
        "error_code": job.error_code,
        "binding": _binding_read(job),
        "result": _result_read(job),
        "review": _review_read(job),
        "usage": dict(usage) if usage is not None else _empty_usage(),
    }


def _batch_counts(db: DBSession, batch_id: uuid.UUID) -> dict:
    counts = {status: 0 for status in JOB_STATUSES}
    rows = db.execute(
        select(FeedbackRegressionJob.status, func.count())
        .where(FeedbackRegressionJob.batch_id == batch_id)
        .group_by(FeedbackRegressionJob.status)
    ).all()
    for status, count in rows:
        if status in counts:
            counts[status] = int(count)
    return {
        "total": sum(counts.values()),
        "queued": counts[JOB_STATUS_QUEUED],
        "running": counts[JOB_STATUS_RUNNING],
        "succeeded": counts[JOB_STATUS_SUCCEEDED],
        "failed": counts[JOB_STATUS_FAILED],
        "cancelled": counts[JOB_STATUS_CANCELLED],
    }


def _batch_jobs(db: DBSession, batch_id: uuid.UUID) -> list[FeedbackRegressionJob]:
    return list(
        db.scalars(
            select(FeedbackRegressionJob)
            .where(FeedbackRegressionJob.batch_id == batch_id)
            .order_by(FeedbackRegressionJob.created_at, FeedbackRegressionJob.id)
        ).all()
    )


def batch_read(db: DBSession, batch: FeedbackRegressionBatch) -> dict:
    jobs = _batch_jobs(db, batch.id)
    usage = job_usage(db, [job.id for job in jobs])
    return {
        "batch_id": batch.id,
        "request_id": batch.request_id,
        "revision": batch.revision,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
        "counts": _batch_counts(db, batch.id),
        "jobs": [job_read(job, usage.get(job.id)) for job in jobs],
    }


def get_job(db: DBSession, job_id: uuid.UUID) -> dict:
    job = db.get(FeedbackRegressionJob, job_id)
    if job is None:
        _fail(404, "job_not_found")
    return job_read(job, job_usage(db, [job.id]).get(job.id))


def list_jobs(
    db: DBSession,
    admin: User,
    *,
    feedback_id: uuid.UUID | None = None,
    batch_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[int, list[dict]]:
    conditions = []
    if feedback_id is not None:
        conditions.append(FeedbackRegressionJob.feedback_id == feedback_id)
    if batch_id is not None:
        conditions.append(FeedbackRegressionJob.batch_id == batch_id)
    if request_id is not None:
        # request_id 只属于单条提交；同时绑定发起管理员，避免跨管理员枚举。
        conditions.append(FeedbackRegressionJob.request_id == request_id)
        conditions.append(FeedbackRegressionJob.admin_id == admin.id)
    total = int(
        db.scalar(
            select(func.count())
            .select_from(FeedbackRegressionJob)
            .where(*conditions)
        )
        or 0
    )
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), pages)
    rows = db.scalars(
        select(FeedbackRegressionJob)
        .where(*conditions)
        .order_by(
            FeedbackRegressionJob.created_at.desc(),
            FeedbackRegressionJob.id.desc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    # 一次有界分组查询聚合整页用量，绝不按 job 逐条扫表。
    usage = job_usage(db, [row.id for row in rows])
    return total, [job_read(row, usage.get(row.id)) for row in rows]


def get_batch(db: DBSession, batch_id: uuid.UUID) -> dict:
    batch = db.get(FeedbackRegressionBatch, batch_id)
    if batch is None:
        _fail(404, "batch_not_found")
    return batch_read(db, batch)


def lookup_request(db: DBSession, admin: User, request_id: uuid.UUID) -> dict:
    """按原 request_id 只读找回提交结果；不返回正文，也不跨管理员。"""
    job = db.scalar(
        select(FeedbackRegressionJob).where(
            FeedbackRegressionJob.admin_id == admin.id,
            FeedbackRegressionJob.request_id == request_id,
        )
    )
    batch = db.scalar(
        select(FeedbackRegressionBatch).where(
            FeedbackRegressionBatch.admin_id == admin.id,
            FeedbackRegressionBatch.request_id == request_id,
        )
    )
    if job is not None and batch is not None:
        _fail(409, "request_id_conflict")
    if job is not None:
        return {
            "request_id": request_id,
            "kind": "job",
            "job": job_read(job, job_usage(db, [job.id]).get(job.id)),
            "batch": None,
        }
    if batch is not None:
        return {
            "request_id": request_id,
            "kind": "batch",
            "job": None,
            "batch": batch_read(db, batch),
        }
    _fail(404, "request_not_found")


# ---------------------------------------------------------------------------
# 提交（幂等）
# ---------------------------------------------------------------------------


def _normalized_batch_items(items: Sequence) -> list[tuple[uuid.UUID, int]]:
    try:
        values = list(items)
    except TypeError:
        _fail(422, "invalid_batch")
    if not 1 <= len(values) <= 20:
        _fail(422, "invalid_batch")
    result: list[tuple[uuid.UUID, int]] = []
    seen: set[uuid.UUID] = set()
    for item in values:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            _fail(422, "invalid_batch")
        identifier, revision = item
        if not isinstance(identifier, uuid.UUID) or identifier in seen:
            _fail(422, "invalid_batch")
        if type(revision) is not int or revision < 1:
            _fail(422, "invalid_batch")
        seen.add(identifier)
        result.append((identifier, revision))
    result.sort(key=lambda entry: str(entry[0]))
    return result


def _existing_request(db: DBSession, admin: User, request_id: uuid.UUID):
    """按“管理员 + UUID”查既有提交；其他管理员的同 UUID 不是冲突。"""
    job = db.scalar(
        select(FeedbackRegressionJob).where(
            FeedbackRegressionJob.admin_id == admin.id,
            FeedbackRegressionJob.request_id == request_id,
        )
    )
    batch = db.scalar(
        select(FeedbackRegressionBatch).where(
            FeedbackRegressionBatch.admin_id == admin.id,
            FeedbackRegressionBatch.request_id == request_id,
        )
    )
    if job is not None and batch is not None:
        _fail(409, "request_id_conflict")
    return job, batch


def _create_jobs(
    db: DBSession,
    *,
    admin: User,
    auth_token_version: int,
    executions: list,
    regressions: dict[uuid.UUID, FeedbackRegression],
    batch_id: uuid.UUID | None,
    single_request_id: uuid.UUID | None = None,
    single_request_sha: str | None = None,
) -> list[FeedbackRegressionJob]:
    now = utc_now()
    runs: list[FeedbackRegressionRun] = []
    jobs: list[FeedbackRegressionJob] = []
    for execution in executions:
        feedback_id = uuid.UUID(execution.feedback_id)
        feedback = regression_feedback(execution)
        regression = regressions[feedback_id]
        fingerprints, binding = _online_run_values(
            execution, feedback, regression.cause
        )
        attempt_no = int(regression.current_attempt_no) + 1
        run_id = uuid.uuid4()
        job_id = uuid.uuid4()
        run = FeedbackRegressionRun(
            id=run_id,
            regression_id=regression.id,
            attempt_no=attempt_no,
            # 在线轮次没有旧 prepare 重放语义；使用 job_id 作为该轮稳定键。
            prepare_request_id=job_id,
            protocol_version=REGRESSION_PROTOCOL_VERSION,
            requested_mode="live",
            execution_kind=EXECUTION_KIND_ONLINE,
            feedback_revision=execution.feedback_revision,
            feedback_result_sha256=execution.feedback_result_sha256,
            case_sha256=fingerprints["case_sha256"],
            dataset_sha256=fingerprints["dataset_sha256"],
            binding_sha256=binding,
            prepared_by=admin.id,
            prepared_by_name=admin.username,
        )
        job = FeedbackRegressionJob(
            id=job_id,
            regression_id=regression.id,
            feedback_id=feedback_id,
            batch_id=batch_id,
            run_id=run_id,
            run_attempt_no=attempt_no,
            request_id=single_request_id,
            request_sha256=single_request_sha,
            status=JOB_STATUS_QUEUED,
            attempt_count=0,
            revision=1,
            execution_kind=EXECUTION_KIND_ONLINE,
            requested_mode="live",
            admin_id=admin.id,
            admin_name=admin.username,
            admin_token_version=auth_token_version,
            context_summary=execution.summary(),
            root_sha256=execution.root_sha256,
            case_sha256=fingerprints["case_sha256"],
            dataset_sha256=fingerprints["dataset_sha256"],
            binding_sha256=binding,
            available_at=now,
            review_status=REVIEW_STATUS_UNKNOWN,
        )
        regression.current_attempt_no = attempt_no
        regression.revision = int(regression.revision) + 1
        regression.updated_at = now
        runs.append(run)
        jobs.append(job)
    db.add_all(runs)
    db.flush()
    db.add_all(jobs)
    return jobs


def _integrity_error(exc: IntegrityError) -> None:
    constraint = getattr(
        getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None
    )
    if constraint == "uq_feedback_regression_jobs_active":
        _fail(409, "job_active")
    if constraint in {
        "uq_feedback_regression_jobs_admin_request",
        "uq_feedback_regression_batches_admin_request",
    }:
        _fail(409, "request_id_conflict")
    if constraint in {"uq_feedback_regression_runs_attempt"}:
        _fail(409, "revision_conflict")
    _fail(409, "job_conflict")


def _commit_or_conflict(db: DBSession) -> None:
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        _integrity_error(exc)


def _replay_job(
    db: DBSession,
    admin: User,
    auth_token_version: int,
    job: FeedbackRegressionJob,
) -> dict:
    """同键重放：仍复核当前 admin 权限、固定清单/项目与当前样例绑定。"""
    fixed_manifests, fixed_projects = _summary_fixed_bindings(job)
    _locked, contexts = _build_locked(
        db,
        admin_id=admin.id,
        auth_token_version=auth_token_version,
        feedback_ids=[job.feedback_id],
        fixed_manifests=fixed_manifests,
        fixed_projects=fixed_projects,
    )
    execution = _execution_for_feedback(contexts, job.feedback_id)
    regression = db.get(FeedbackRegression, job.regression_id)
    run = db.get(FeedbackRegressionRun, job.run_id)
    if regression is None or run is None:
        _fail(409, "sample_changed")
    _assert_execution_matches_job(execution, regression, run, job)
    data = job_read(job, job_usage(db, [job.id]).get(job.id))
    db.rollback()
    return data


def enqueue_job(
    db: DBSession,
    admin: User,
    feedback_id: uuid.UUID,
    expected_revision: int,
    request_id: uuid.UUID,
    *,
    auth_token_version: int,
) -> dict:
    """单条提交；request_id 绑定管理员+路由+规范请求内容。"""
    if type(expected_revision) is not int or expected_revision < 1:
        _fail(422, "invalid_request")
    payload = {"feedback_id": str(feedback_id), "expected_revision": expected_revision}
    request_sha = _request_sha("job", payload)
    _advisory_lock(db, admin.id, request_id)
    existing_job, existing_batch = _existing_request(db, admin, request_id)
    if existing_batch is not None:
        _fail(409, "request_id_conflict")
    if existing_job is not None:
        if existing_job.request_sha256 != request_sha:
            _fail(409, "request_id_conflict")
        return _replay_job(db, admin, auth_token_version, existing_job)

    try:
        _locked, contexts = _build_locked(
            db,
            admin_id=admin.id,
            auth_token_version=auth_token_version,
            feedback_ids=[feedback_id],
        )
        contexts = _canonicalize_frozen_manifests(
            db,
            admin_id=admin.id,
            auth_token_version=auth_token_version,
            feedback_ids=[feedback_id],
            contexts=contexts,
        )
        execution = _execution_for_feedback(contexts, feedback_id)
        regressions = _lock_regressions(db, [feedback_id])
        regression = regressions[feedback_id]
        if regression.revision != expected_revision:
            _fail(409, "revision_conflict")
        _assert_enrollment(regression, execution, regression_feedback(execution))
        active = db.scalar(
            select(FeedbackRegressionJob.id)
            .where(
                FeedbackRegressionJob.regression_id == regression.id,
                FeedbackRegressionJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .limit(1)
        )
        if active is not None:
            _fail(409, "regression_job_active")
        jobs = _create_jobs(
            db,
            admin=admin,
            auth_token_version=auth_token_version,
            executions=[execution],
            regressions=regressions,
            batch_id=None,
            single_request_id=request_id,
            single_request_sha=request_sha,
        )
        _commit_or_conflict(db)
    except JobError:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        _integrity_error(exc)
    # 提交事务已提交，重新读回同一作业（含新建时的空用量汇总）。
    return get_job(db, jobs[0].id)


def _replay_batch(
    db: DBSession,
    admin: User,
    auth_token_version: int,
    batch: FeedbackRegressionBatch,
) -> dict:
    jobs = _batch_jobs(db, batch.id)
    if not jobs:
        _fail(409, "sample_changed", "批次成员已不可用")
    feedback_ids = [job.feedback_id for job in jobs]
    fixed_manifests = {}
    fixed_projects = {}
    for job in jobs:
        manifests, projects = _summary_fixed_bindings(job)
        if manifests:
            fixed_manifests.update(manifests)
        if projects:
            fixed_projects.update(projects)
    _locked, contexts = _build_locked(
        db,
        admin_id=admin.id,
        auth_token_version=auth_token_version,
        feedback_ids=feedback_ids,
        fixed_manifests=fixed_manifests or None,
        fixed_projects=fixed_projects or None,
    )
    for job in jobs:
        execution = _execution_for_feedback(contexts, job.feedback_id)
        regression = db.get(FeedbackRegression, job.regression_id)
        run = db.get(FeedbackRegressionRun, job.run_id)
        if regression is None or run is None:
            _fail(409, "sample_changed")
        _assert_execution_matches_job(execution, regression, run, job)
    data = batch_read(db, batch)
    db.rollback()
    return data


def enqueue_batch(
    db: DBSession,
    admin: User,
    request_id: uuid.UUID,
    items: Sequence,
    *,
    auth_token_version: int,
) -> dict:
    """批量提交（1–20 条唯一反馈）：全部验证通过后一次提交，否则零写入。"""
    normalized = _normalized_batch_items(items)
    payload = {
        "items": [
            {"feedback_id": str(feedback_id), "expected_revision": revision}
            for feedback_id, revision in normalized
        ]
    }
    request_sha = _request_sha("batch", payload)
    _advisory_lock(db, admin.id, request_id)
    existing_job, existing_batch = _existing_request(db, admin, request_id)
    if existing_job is not None:
        _fail(409, "request_id_conflict")
    if existing_batch is not None:
        if existing_batch.request_sha256 != request_sha:
            _fail(409, "request_id_conflict")
        return _replay_batch(db, admin, auth_token_version, existing_batch)

    feedback_ids = [feedback_id for feedback_id, _ in normalized]
    expected = dict(normalized)
    try:
        _locked, contexts = _build_locked(
            db,
            admin_id=admin.id,
            auth_token_version=auth_token_version,
            feedback_ids=feedback_ids,
        )
        contexts = _canonicalize_frozen_manifests(
            db,
            admin_id=admin.id,
            auth_token_version=auth_token_version,
            feedback_ids=feedback_ids,
            contexts=contexts,
        )
        executions = [
            _execution_for_feedback(contexts, feedback_id)
            for feedback_id in feedback_ids
        ]
        regressions = _lock_regressions(db, feedback_ids)
        for feedback_id in feedback_ids:
            if regressions[feedback_id].revision != expected[feedback_id]:
                _fail(409, "revision_conflict")
        for execution in executions:
            _assert_enrollment(
                regressions[uuid.UUID(execution.feedback_id)],
                execution,
                regression_feedback(execution),
            )
        active = db.scalars(
            select(FeedbackRegressionJob.regression_id)
            .where(
                FeedbackRegressionJob.regression_id.in_(
                    [regression.id for regression in regressions.values()]
                ),
                FeedbackRegressionJob.status.in_(ACTIVE_JOB_STATUSES),
            )
            .limit(1)
        ).first()
        if active is not None:
            _fail(409, "regression_job_active")

        batch = FeedbackRegressionBatch(
            id=uuid.uuid4(),
            request_id=request_id,
            request_sha256=request_sha,
            admin_id=admin.id,
            admin_name=admin.username,
            revision=1,
        )
        db.add(batch)
        db.flush()
        _create_jobs(
            db,
            admin=admin,
            auth_token_version=auth_token_version,
            executions=executions,
            regressions=regressions,
            batch_id=batch.id,
        )
        _commit_or_conflict(db)
    except JobError:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        _integrity_error(exc)
    return batch_read(db, batch)


# ---------------------------------------------------------------------------
# 取消
# ---------------------------------------------------------------------------


def _lock_admin(db: DBSession, admin: User, auth_token_version: int) -> User:
    """仅 User→Job 子集锁：取消不依赖原反馈所有者当前 ACL。"""
    row = db.scalar(
        select(User)
        .where(User.id == admin.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        row is None
        or not row.is_active
        or type(auth_token_version) is not int
        or auth_token_version < 0
        or row.token_version != auth_token_version
    ):
        _fail(401, "auth_invalid")
    if row.role != "admin":
        _fail(403, "admin_required")
    return row


def cancel_job(
    db: DBSession,
    admin: User,
    job_id: uuid.UUID,
    expected_revision: int,
    *,
    auth_token_version: int,
) -> dict:
    now = utc_now()
    try:
        _lock_admin(db, admin, auth_token_version)
        job = db.scalar(
            select(FeedbackRegressionJob)
            .where(FeedbackRegressionJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            _fail(404, "job_not_found")
        if job.revision != expected_revision:
            _fail(409, "review_conflict")
        if job.status == JOB_STATUS_CANCELLED:
            data = job_read(job, job_usage(db, [job.id]).get(job.id))
            db.rollback()
            return data
        if job.status in TERMINAL_JOB_STATUSES:
            _fail(409, "job_not_cancellable")
        job.status = JOB_STATUS_CANCELLED
        job.revision += 1
        job.cancelled_at = now
        job.cancelled_by = admin.id
        job.cancelled_by_name = admin.username
        job.lease_id = None
        job.lease_expires_at = None
        job.updated_at = now
        db.commit()
    except JobError:
        db.rollback()
        raise
    # 取消已提交；重新读取同一作业，响应仍带聚合用量（含取消前已发生调用）。
    return get_job(db, job.id)


def cancel_batch(
    db: DBSession,
    admin: User,
    batch_id: uuid.UUID,
    expected_revision: int,
    *,
    auth_token_version: int,
) -> dict:
    now = utc_now()
    try:
        _lock_admin(db, admin, auth_token_version)
        batch = db.scalar(
            select(FeedbackRegressionBatch)
            .where(FeedbackRegressionBatch.id == batch_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if batch is None:
            _fail(404, "batch_not_found")
        if batch.revision != expected_revision:
            _fail(409, "review_conflict")
        jobs = db.scalars(
            select(FeedbackRegressionJob)
            .where(FeedbackRegressionJob.batch_id == batch.id)
            .order_by(FeedbackRegressionJob.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()
        for job in jobs:
            if job.status in (JOB_STATUS_QUEUED, JOB_STATUS_RUNNING):
                job.status = JOB_STATUS_CANCELLED
                job.revision += 1
                job.cancelled_at = now
                job.cancelled_by = admin.id
                job.cancelled_by_name = admin.username
                job.lease_id = None
                job.lease_expires_at = None
                job.updated_at = now
        batch.revision += 1
        batch.updated_at = now
        db.commit()
    except JobError:
        db.rollback()
        raise
    return batch_read(db, batch)


# ---------------------------------------------------------------------------
# 人工核验 / 比较
# ---------------------------------------------------------------------------


def _canonical_source_lists(
    *groups: Sequence | None,
) -> tuple[dict, ...]:
    """把已经白名单化的来源列表严格解析成身份集合。

    与 ``build_source_snapshot`` 不同，这里每一项都是**来源元数据**，不是 assistant
    历史条目；用于持久 body 的重新校验，绝不把已保存的 ``history_sources`` 误当历史消息。
    """
    items: list = []
    for group in groups:
        if group is None:
            continue
        items.extend(list(group))
    try:
        return answer_sources.parse_sources(items)
    except (answer_sources.SourceSnapshotError, ValueError, TypeError):
        _fail(409, "source_verification_failed")


def _canonical_result_sources(
    mode: str,
    supplied_sources: Sequence | None,
    history_sources: Sequence | None,
    citations: Sequence | None,
    candidate_sources: Sequence | None = None,
) -> tuple[dict, dict, list[dict], tuple[dict, ...]]:
    """规范化一份实际执行结果，返回唯一用于验证/评分/持久化的白名单元数据。

    调用方显式传入的 ``supplied_sources``/``history_sources`` 是权威实际来源；原始
    ``trace.supplied_sources`` 不参与覆盖。``candidate_sources`` 同样先严格解析身份，
    不能把未核验的字段直接写进 body。引用先裁剪/白名单化再评分，保证结果指纹对应
    持久内容。
    """
    try:
        # ``build_source_snapshot`` 的第二个参数是 assistant 历史消息，而不是扁平
        # 来源元数据。把显式 history_sources 包成一个严格 v1 provenance 快照，
        # 让 helper 统一执行 parse_source_snapshot/parse_sources 的校验与去重。
        history_snapshot = {
            "source_snapshot_version": answer_sources.SOURCE_SNAPSHOT_VERSION,
            "supplied_sources": [],
            "history_sources": list(history_sources or []),
        }
        assistant_history = [{
            "role": "assistant",
            answer_sources.SOURCE_PROVENANCE_FIELD: history_snapshot,
        }]
        snapshot = answer_sources.build_source_snapshot(
            list(supplied_sources or []),
            assistant_history,
            mode=mode,
        )
        # 未提供 candidate_sources 时保持“未测量”，不要伪造成空列表（空列表是
        # 已测量但零命中，会改变评测语义）。
        canonical_candidates = (
            [
                dict(item)
                for item in answer_sources.parse_sources(list(candidate_sources))
            ]
            if candidate_sources is not None
            else None
        )
        safe_citations = _safe_citations(list(citations or []))
        parsed_citations = answer_sources.parse_sources(safe_citations)
    except (answer_sources.SourceSnapshotError, ValueError, TypeError):
        raise JobResultError("result_invalid") from None
    combined = _canonical_source_lists(
        snapshot["supplied_sources"],
        snapshot["history_sources"],
        canonical_candidates,
        parsed_citations,
    )
    trace_sources = {
        "supplied_sources": [dict(item) for item in snapshot["supplied_sources"]],
        "history_sources": [dict(item) for item in snapshot["history_sources"]],
    }
    if canonical_candidates is not None:
        trace_sources["candidate_sources"] = canonical_candidates
    return snapshot, trace_sources, safe_citations, combined


def _canonical_actual_sources(
    mode: str,
    supplied_sources: Sequence | None,
    history_sources: Sequence | None,
    citations: Sequence | None,
    candidate_sources: Sequence | None = None,
) -> tuple[dict, ...]:
    """重新校验已持久 body 的来源：传入项均为来源元数据，不再当作历史消息。"""
    return _canonical_source_lists(
        supplied_sources, history_sources, citations, candidate_sources
    )


def _validate_actual_sources(
    db: DBSession,
    locked,
    execution,
    sources: Sequence[Mapping],
    *,
    now: datetime,
    enforce_manifest: bool = True,
) -> None:
    if not sources:
        return
    if execution.mode == "chat":
        _fail(409, "source_verification_failed")
    kb = locked.knowledge_bases.get(uuid.UUID(execution.kb_id))
    if kb is None:
        _fail(409, "source_verification_failed")
    if kb.kind == "upload":
        manifest = execution.upload_manifest
        if enforce_manifest and manifest is None:
            _fail(409, "manifest_invalid")
        allowed = {
            (entry["document_id"], entry["index_version"])
            for entry in (manifest or [])
        }
        for source in sources:
            if source.get("source_kind") != "document":
                _fail(409, "source_verification_failed")
            if enforce_manifest and (
                source["document_id"], source["index_version"]
            ) not in allowed:
                _fail(409, "source_verification_failed")
    elif kb.kind == "project":
        project = execution.project or {}
        for source in sources:
            if (
                source.get("source_kind") != "project"
                or source.get("snapshot_id") != project.get("snapshot_id")
                or source.get("source_id") != project.get("source_id")
                or source.get("manifest_fingerprint") != project.get("manifest_fingerprint")
                or source.get("config_fingerprint") != project.get("config_fingerprint")
            ):
                _fail(409, "source_verification_failed")
    else:
        _fail(409, "source_verification_failed")
    owner = locked.users.get(uuid.UUID(execution.owner_id))
    if owner is None:
        _fail(409, "owner_inactive")
    try:
        answer_sources.validate_sources(
            db,
            kb,
            list(sources),
            user=owner,
            lock=False,
            binding=locked.project_binding(),
            now=now,
        )
    except answer_sources.SourceSnapshotError:
        _fail(409, "source_verification_failed")


def _stored_body_sources(
    body: FeedbackRegressionJobBody, mode: str
) -> tuple[dict, ...]:
    trace = body.trace if isinstance(body.trace, dict) else {}
    supplied = trace.get("supplied_sources")
    history = trace.get("history_sources")
    candidates = trace.get("candidate_sources")
    citations = body.citations if isinstance(body.citations, list) else []
    if not isinstance(supplied, list):
        supplied = []
    if not isinstance(history, list):
        history = []
    if not isinstance(candidates, list):
        candidates = []
    return _canonical_actual_sources(
        mode, supplied, history, citations, candidates
    )


def _old_answer_sources(locked, execution) -> tuple[dict, ...]:
    """原回答当时实际 supplied+history 来源与引用（严格 v1 快照）。

    普通 chat 沿用既有“完全无快照痕迹”的豁免；RAG 缺失/损坏快照绝不当作空来源。
    """
    turn = None
    if execution.turn_id:
        for row in locked.turns_by_conversation.get(
            uuid.UUID(execution.conversation_id), ()
        ):
            if str(row.id) == execution.turn_id:
                turn = row
                break
    trace = turn.trace if turn is not None and isinstance(turn.trace, dict) else {}
    message = locked.messages.get(uuid.UUID(execution.message_id))
    citations = message.citations if message is not None else None
    try:
        actual = answer_sources.sources_from_trace(trace, mode=execution.mode)
        parsed_citations = answer_sources.parse_sources(
            citations if isinstance(citations, list) else []
        )
    except (answer_sources.SourceSnapshotError, ValueError, TypeError):
        _fail(409, "old_answer_stale", "原回答的来源快照已失效")
    return _canonical_source_lists(actual, parsed_citations)


def _rebuild_for_read(
    db: DBSession,
    admin: User,
    auth_token_version: int,
    job: FeedbackRegressionJob,
):
    """当前 admin 权限下重建固定上下文并复核绑定；返回锁定的根。"""
    fixed_manifests, fixed_projects = _summary_fixed_bindings(job)
    locked, contexts = _build_locked(
        db,
        admin_id=admin.id,
        auth_token_version=auth_token_version,
        feedback_ids=[job.feedback_id],
        fixed_manifests=fixed_manifests,
        fixed_projects=fixed_projects,
    )
    execution = _execution_for_feedback(contexts, job.feedback_id)
    regression = db.get(FeedbackRegression, job.regression_id)
    run = db.get(FeedbackRegressionRun, job.run_id)
    if regression is None or run is None:
        _fail(409, "sample_changed")
    _assert_execution_matches_job(execution, regression, run, job)
    return locked, execution, regression, run


def comparison(
    db: DBSession,
    admin: User,
    job_id: uuid.UUID,
    *,
    auth_token_version: int,
) -> dict:
    job = db.get(FeedbackRegressionJob, job_id)
    if job is None:
        _fail(404, "job_not_found")
    if job.status != JOB_STATUS_SUCCEEDED or not job.result_sha256:
        _fail(409, "job_not_succeeded")
    try:
        locked, execution, regression, run = _rebuild_for_read(
            db, admin, auth_token_version, job
        )
        body = db.get(FeedbackRegressionJobBody, job.id)
        if body is None:
            _fail(409, "result_missing")
        now = utc_now()
        sources = _stored_body_sources(body, execution.mode)
        _validate_actual_sources(db, locked, execution, sources, now=now)
        old_sources = _old_answer_sources(locked, execution)
        # 旧答案的来源不在本次执行清单内也可比较，但必须按当前原 owner ACL/治理
        # 和实际引用重新核验；失效即拒绝返回任何 old/new 正文。
        _validate_actual_sources(
            db, locked, execution, old_sources, now=now, enforce_manifest=False
        )
        message = locked.messages.get(uuid.UUID(execution.message_id))
        if message is None:
            _fail(409, "feedback_stale")
        data = {
            "job": job_read(job, job_usage(db, [job.id]).get(job.id)),
            "run": {
                "run_id": run.id,
                "attempt_no": run.attempt_no,
                "execution_kind": "online",
                "requested_mode": "live",
            },
            "binding": _binding_read(job),
            "old": {
                "message_id": message.id,
                "conversation_id": message.conversation_id,
                "answer": message.content or "",
                "citations": (
                    message.citations if isinstance(message.citations, list) else []
                ),
                "result_sha256": execution.feedback_result_sha256,
            },
            "new": {
                "answer": body.answer,
                "citations": body.citations if isinstance(body.citations, list) else [],
                "trace": body.trace if isinstance(body.trace, dict) else {},
                "passed": bool(job.result_passed),
                "failures": _result_read(job)["failures"],
                "outcome": job.result_outcome,
                "http_status": job.result_http_status,
                "citation_count": int(job.citation_count or 0),
                "result_sha256": job.result_sha256,
                "machine_sha256": job.machine_sha256 or "",
            },
        }
        db.rollback()
        return data
    except JobError:
        db.rollback()
        raise


def review_job(
    db: DBSession,
    admin: User,
    job_id: uuid.UUID,
    *,
    expected_revision: int,
    run_id: uuid.UUID,
    result_sha256: str,
    status: str,
    notes: str,
    auth_token_version: int,
) -> dict:
    if status not in REVIEW_STATUSES:
        _fail(422, "invalid_request")
    if type(expected_revision) is not int or expected_revision < 1:
        _fail(422, "invalid_request")
    if not isinstance(result_sha256, str) or len(result_sha256) != 64:
        _fail(422, "invalid_request")
    if not isinstance(notes, str):
        _fail(422, "invalid_request")
    notes = notes.strip()
    if len(notes) > 1000:
        _fail(422, "invalid_request")
    if any(ord(char) < 32 and char not in "\n\t" for char in notes):
        _fail(422, "invalid_request")
    job = db.get(FeedbackRegressionJob, job_id)
    if job is None:
        _fail(404, "job_not_found")
    if job.status != JOB_STATUS_SUCCEEDED or not job.result_sha256:
        _fail(409, "job_not_succeeded")
    try:
        _locked, execution, regression, run = _rebuild_for_read(
            db, admin, auth_token_version, job
        )
        body = db.get(FeedbackRegressionJobBody, job.id)
        if body is None:
            _fail(409, "result_missing")
        sources = _stored_body_sources(body, execution.mode)
        review_now = utc_now()
        _validate_actual_sources(db, _locked, execution, sources, now=review_now)
        old_sources = _old_answer_sources(_locked, execution)
        _validate_actual_sources(
            db, _locked, execution, old_sources,
            now=review_now, enforce_manifest=False,
        )
        job = db.scalar(
            select(FeedbackRegressionJob)
            .where(FeedbackRegressionJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            _fail(404, "job_not_found")
        if job.status != JOB_STATUS_SUCCEEDED or not job.result_sha256:
            _fail(409, "job_not_succeeded")
        if job.revision != expected_revision:
            _fail(409, "review_conflict")
        if job.run_id != run_id or job.result_sha256 != result_sha256:
            _fail(409, "review_conflict", "核验结果与当前作业结果不一致")
        now = utc_now()
        job.review_status = status
        job.review_notes = notes
        job.reviewed_by = admin.id
        job.reviewed_by_name = admin.username
        job.reviewed_at = now
        job.review_result_sha256 = job.result_sha256 if status != REVIEW_STATUS_UNKNOWN else None
        job.revision += 1
        job.updated_at = now
        db.commit()
    except JobError:
        db.rollback()
        raise
    return get_job(db, job.id)


# ---------------------------------------------------------------------------
# R2 公共 worker 契约（冻结）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobClaim:
    job_id: uuid.UUID
    run_id: uuid.UUID
    feedback_id: uuid.UUID
    regression_id: uuid.UUID
    attempt_count: int
    lease_id: uuid.UUID
    lease_expires_at: datetime
    deadline_at: datetime
    revision: int


@dataclass(frozen=True)
class JobRecovery:
    job_id: uuid.UUID
    status: str
    reason: str
    attempt_count: int
    revision: int


@dataclass(frozen=True)
class LeaseCheck:
    valid: bool
    status: str | None
    reason: str | None
    deadline_at: datetime | None
    lease_expires_at: datetime | None
    attempt_count: int | None
    revision: int | None


@dataclass(frozen=True)
class LoadedExecution:
    job_id: uuid.UUID
    run_id: uuid.UUID
    feedback_id: uuid.UUID
    attempt_count: int
    revision: int
    lease_id: uuid.UUID
    lease_expires_at: datetime
    deadline_at: datetime
    execution: Any
    usage_context: UsageContext


@dataclass(frozen=True)
class JobTransition:
    applied: bool
    status: str
    reason: str | None
    attempt_count: int
    revision: int


@dataclass(frozen=True)
class JobCompletion:
    job_id: uuid.UUID
    run_id: uuid.UUID
    status: str
    attempt_count: int
    revision: int
    result_sha256: str
    machine_sha256: str
    passed: bool
    failures: tuple[str, ...]
    outcome: str | None
    http_status: int | None
    citation_count: int
    body_sha256: str


def _lease_reason(
    job: FeedbackRegressionJob,
    lease_id: uuid.UUID,
    now: datetime,
    *,
    check_deadline: bool = True,
) -> str | None:
    if job.status == JOB_STATUS_CANCELLED:
        return "cancelled"
    if job.status != JOB_STATUS_RUNNING:
        return "not_running"
    if job.lease_id != lease_id:
        return "lease_mismatch"
    if job.lease_expires_at is None or job.lease_expires_at <= now:
        return "lease_expired"
    if check_deadline and (job.deadline_at is None or job.deadline_at <= now):
        return "deadline_exceeded"
    return None


def _is_deadline_passed(job: FeedbackRegressionJob, now: datetime) -> bool:
    return job.deadline_at is not None and job.deadline_at <= now


def _transition(job: FeedbackRegressionJob, status: str, now: datetime) -> JobTransition:
    job.status = status
    job.revision += 1
    job.updated_at = now
    if status in TERMINAL_JOB_STATUSES:
        job.completed_at = now
        job.lease_id = None
        job.lease_expires_at = None
    return JobTransition(
        applied=True,
        status=job.status,
        reason=None,
        attempt_count=int(job.attempt_count),
        revision=int(job.revision),
    )


def claim(db: DBSession) -> JobClaim | None:
    """领取一个可运行作业；SKIP LOCKED + 短事务，绝不等待根锁。"""
    now = utc_now()
    total_timeout = timedelta(seconds=settings.FEEDBACK_REGRESSION_JOB_TOTAL_TIMEOUT_SECONDS)
    lease_seconds = timedelta(seconds=settings.FEEDBACK_REGRESSION_JOB_LEASE_SECONDS)
    try:
        job = db.scalar(
            select(FeedbackRegressionJob)
            .where(
                FeedbackRegressionJob.status == JOB_STATUS_QUEUED,
                FeedbackRegressionJob.available_at <= now,
                FeedbackRegressionJob.attempt_count
                < settings.FEEDBACK_REGRESSION_JOB_MAX_ATTEMPTS,
                or_(
                    FeedbackRegressionJob.deadline_at.is_(None),
                    FeedbackRegressionJob.deadline_at > now,
                ),
            )
            .order_by(
                FeedbackRegressionJob.available_at,
                FeedbackRegressionJob.created_at,
                FeedbackRegressionJob.id,
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            db.rollback()
            return None
        if job.started_at is None or job.deadline_at is None:
            job.started_at = job.started_at or now
            job.deadline_at = job.deadline_at or (now + total_timeout)
        lease_id = uuid.uuid4()
        job.status = JOB_STATUS_RUNNING
        job.attempt_count = int(job.attempt_count) + 1
        job.revision += 1
        job.lease_id = lease_id
        job.lease_expires_at = min(now + lease_seconds, job.deadline_at)
        job.heartbeat_at = now
        job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise
    return JobClaim(
        job_id=job.id,
        run_id=job.run_id,
        feedback_id=job.feedback_id,
        regression_id=job.regression_id,
        attempt_count=int(job.attempt_count),
        lease_id=lease_id,
        lease_expires_at=job.lease_expires_at,
        deadline_at=job.deadline_at,
        revision=int(job.revision),
    )


def recover(db: DBSession, *, limit: int = MAX_RECOVERY_BATCH) -> list[JobRecovery]:
    """回收过期租约/超时排队作业；重试与终态都在短事务内推进。"""
    if type(limit) is not int or not 1 <= limit <= MAX_RECOVERY_BATCH:
        limit = MAX_RECOVERY_BATCH
    now = utc_now()
    retry_delay = timedelta(seconds=settings.FEEDBACK_REGRESSION_JOB_RETRY_DELAY_SECONDS)
    max_attempts = settings.FEEDBACK_REGRESSION_JOB_MAX_ATTEMPTS
    results: list[JobRecovery] = []
    try:
        rows = db.scalars(
            select(FeedbackRegressionJob)
            .where(
                or_(
                    (
                        (FeedbackRegressionJob.status == JOB_STATUS_RUNNING)
                        & (
                            FeedbackRegressionJob.lease_expires_at.is_(None)
                            | (FeedbackRegressionJob.lease_expires_at <= now)
                            | FeedbackRegressionJob.deadline_at.is_(None)
                            | (FeedbackRegressionJob.deadline_at <= now)
                        )
                    ),
                    (
                        (FeedbackRegressionJob.status == JOB_STATUS_QUEUED)
                        & (
                            (
                                FeedbackRegressionJob.deadline_at.is_not(None)
                                & (FeedbackRegressionJob.deadline_at <= now)
                            )
                            | (
                                FeedbackRegressionJob.attempt_count
                                >= max_attempts
                            )
                        )
                    ),
                )
            )
            .order_by(FeedbackRegressionJob.id)
            .with_for_update(skip_locked=True)
            .limit(limit)
        ).all()
        for job in rows:
            if _is_deadline_passed(job, now):
                job.error_code = "deadline_exceeded"
                transition = _transition(job, JOB_STATUS_FAILED, now)
                results.append(
                    JobRecovery(
                        job_id=job.id,
                        status=transition.status,
                        reason="deadline_exceeded",
                        attempt_count=transition.attempt_count,
                        revision=transition.revision,
                    )
                )
                continue
            if int(job.attempt_count) >= max_attempts:
                job.error_code = "attempts_exhausted"
                transition = _transition(job, JOB_STATUS_FAILED, now)
                results.append(
                    JobRecovery(
                        job_id=job.id,
                        status=transition.status,
                        reason="attempts_exhausted",
                        attempt_count=transition.attempt_count,
                        revision=transition.revision,
                    )
                )
                continue
            job.status = JOB_STATUS_QUEUED
            job.available_at = now + retry_delay
            job.lease_id = None
            job.lease_expires_at = None
            job.revision += 1
            job.updated_at = now
            results.append(
                JobRecovery(
                    job_id=job.id,
                    status=job.status,
                    reason="lease_expired",
                    attempt_count=int(job.attempt_count),
                    revision=int(job.revision),
                )
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return results


def heartbeat(db: DBSession, *, job_id: uuid.UUID, lease_id: uuid.UUID) -> bool:
    """续租约；不推进 revision、不延长整个作业 deadline。"""
    now = utc_now()
    lease_seconds = timedelta(seconds=settings.FEEDBACK_REGRESSION_JOB_LEASE_SECONDS)
    try:
        job = db.scalar(
            select(FeedbackRegressionJob)
            .where(FeedbackRegressionJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None or _lease_reason(job, lease_id, now) is not None:
            db.rollback()
            return False
        if job.deadline_at is None:
            db.rollback()
            return False
        job.lease_expires_at = min(now + lease_seconds, job.deadline_at)
        job.heartbeat_at = now
        job.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise
    return True


def check_lease(db: DBSession, *, job_id: uuid.UUID, lease_id: uuid.UUID) -> LeaseCheck:
    job = db.get(FeedbackRegressionJob, job_id)
    if job is None:
        return LeaseCheck(
            valid=False,
            status=None,
            reason="not_found",
            deadline_at=None,
            lease_expires_at=None,
            attempt_count=None,
            revision=None,
        )
    now = utc_now()
    reason = _lease_reason(job, lease_id, now)
    return LeaseCheck(
        valid=reason is None,
        status=job.status,
        reason=reason,
        deadline_at=job.deadline_at,
        lease_expires_at=job.lease_expires_at,
        attempt_count=int(job.attempt_count),
        revision=int(job.revision),
    )


def _locked_worker_context(
    db: DBSession,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
) -> tuple[Any, Any, FeedbackRegression, FeedbackRegressionRun, FeedbackRegressionJob]:
    """根 -> Regression -> Run -> Job；等锁后复核 lease/cancel/deadline。"""
    job = db.get(FeedbackRegressionJob, job_id)
    if job is None:
        _fail(404, "job_not_found")
    now = utc_now()
    reason = _lease_reason(job, lease_id, now)
    if reason is not None:
        _fail(409, "lease_invalid", f"作业租约不可用（{reason}）")
    if job.admin_id is None:
        _fail(401, "auth_invalid")
    fixed_manifests, fixed_projects = _summary_fixed_bindings(job)
    locked, contexts = _build_locked(
        db,
        admin_id=job.admin_id,
        auth_token_version=int(job.admin_token_version),
        feedback_ids=[job.feedback_id],
        fixed_manifests=fixed_manifests,
        fixed_projects=fixed_projects,
    )
    execution = _execution_for_feedback(contexts, job.feedback_id)
    regression = db.scalar(
        select(FeedbackRegression)
        .where(FeedbackRegression.id == job.regression_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    run = db.scalar(
        select(FeedbackRegressionRun)
        .where(FeedbackRegressionRun.id == job.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    locked_job = db.scalar(
        select(FeedbackRegressionJob)
        .where(FeedbackRegressionJob.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if regression is None or run is None or locked_job is None:
        _fail(409, "sample_changed")
    now = utc_now()
    reason = _lease_reason(locked_job, lease_id, now)
    if reason is not None:
        _fail(409, "lease_invalid", f"作业租约不可用（{reason}）")
    if locked_job.lease_id != lease_id:
        _fail(409, "lease_invalid")
    if int(regression.current_attempt_no) != int(run.attempt_no):
        _fail(409, "run_not_current", "该作业对应的轮次已不是当前轮")
    _assert_execution_matches_job(execution, regression, run, locked_job)
    return locked, execution, regression, run, locked_job


def load_execution(
    db: DBSession,
    *,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
) -> LoadedExecution:
    """构建可脱离 Session 的临时执行上下文并释放全部数据库锁。"""
    try:
        locked, execution, _regression, _run, job = _locked_worker_context(
            db, job_id, lease_id
        )
        usage_context = UsageContext(
            user_id=job.admin_id,
            kb_id=uuid.UUID(execution.kb_id) if execution.kb_id else None,
            conversation_id=None,
            turn_id=None,
            document_id=None,
            operation="generation",
            regression_job_id=job.id,
        )
        data = LoadedExecution(
            job_id=job.id,
            run_id=job.run_id,
            feedback_id=job.feedback_id,
            attempt_count=int(job.attempt_count),
            revision=int(job.revision),
            lease_id=lease_id,
            lease_expires_at=job.lease_expires_at,
            deadline_at=job.deadline_at,
            execution=execution,
            usage_context=usage_context,
        )
        db.commit()
        return data
    except JobError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def _safe_text(value: Any, *, maximum: int) -> str | None:
    return value[:maximum] if isinstance(value, str) else None


def _safe_number(value: Any) -> int | float | None:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and value >= 0:
        return value
    return None


def _safe_source(item: Any) -> dict | None:
    if not isinstance(item, Mapping):
        return None
    result: dict = {}
    for key in _TRACE_SOURCE_KEYS:
        value = item.get(key)
        if value is None:
            continue
        if key in ("index_version", "chunk_index", "chunk_start", "chunk_end"):
            if type(value) is int and value >= 0:
                result[key] = value
        elif key == "score":
            number = _safe_number(value)
            if number is not None:
                result[key] = number
        else:
            text = _safe_text(value, maximum=2048)
            if text is not None:
                result[key] = text
    return result or None


def _safe_sources(value: Any) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for item in list(value)[:MAX_RESULT_CITATIONS]:
        safe = _safe_source(item)
        if safe is not None:
            result.append(safe)
    return result


def _safe_citation(item: Any) -> dict | None:
    if not isinstance(item, Mapping):
        return None
    result: dict = {}
    for key in _CITATION_KEYS:
        value = item.get(key)
        if value is None:
            continue
        if key == "source_index":
            if type(value) is int and value >= 1:
                result[key] = value
        elif key in ("index_version", "chunk_index", "chunk_start", "chunk_end"):
            if type(value) is int and value >= 0:
                result[key] = value
        elif key == "score":
            number = _safe_number(value)
            if number is not None:
                result[key] = number
        elif key == "snippet":
            text = _safe_text(value, maximum=300)
            if text is not None:
                result[key] = text
        else:
            text = _safe_text(value, maximum=2048)
            if text is not None:
                result[key] = text
    return result or None


def _safe_citations(value: Any) -> list[dict]:
    if not isinstance(value, list):
        _fail(422, "result_invalid")
    result = []
    for item in value[:MAX_RESULT_CITATIONS]:
        safe = _safe_citation(item)
        if safe is not None:
            result.append(safe)
    if len(value) > MAX_RESULT_CITATIONS:
        _fail(422, "result_invalid")
    return result


def _sanitize_trace(trace: Any) -> dict:
    """只复制白名单标量/trace 结构；来源列表由规范化结果显式覆盖。"""
    raw = trace if isinstance(trace, dict) else {}
    result: dict = {}
    for key in _TRACE_NUMBER_KEYS:
        number = _safe_number(raw.get(key))
        if number is not None:
            result[key] = number
    status = _safe_text(raw.get("status"), maximum=32)
    if status is not None:
        result["status"] = status
    finish_reason = _safe_text(raw.get("finish_reason"), maximum=64)
    if finish_reason is not None:
        result["finish_reason"] = finish_reason
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else None
    if usage is None and isinstance(raw.get("generation_usage"), dict):
        usage = raw["generation_usage"]
    if isinstance(usage, dict):
        safe_usage = {
            key: usage[key]
            for key in ("input_tokens", "output_tokens", "cached_input_tokens")
            if type(usage.get(key)) is int and usage.get(key) >= 0
        }
        if safe_usage:
            result["usage"] = safe_usage
    return result


def complete(
    db: DBSession,
    *,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
    answer: str,
    citations: Sequence[Mapping],
    trace: Mapping | None = None,
    supplied_sources: Sequence[Mapping] | None = None,
    history_sources: Sequence[Mapping] | None = None,
) -> JobCompletion:
    """发布真实新结果：重算断言、复核全部实际来源并按原权限/租约写入。

    规范化后的白名单结果同时用于来源复核、``grade_case`` 与持久 body，结果指纹
    永远能由保存内容复算。发布提交前重新读取当前时间并复核 deadline/lease。
    """
    if not isinstance(answer, str):
        _fail(422, "result_invalid")
    if len(answer) > MAX_RESULT_ANSWER_CHARS:
        _fail(422, "result_too_large")
    if not isinstance(citations, list) or any(
        not isinstance(item, Mapping) for item in citations
    ):
        _fail(422, "result_invalid")
    raw_trace = trace if isinstance(trace, Mapping) else {}
    if supplied_sources is not None and not isinstance(supplied_sources, (list, tuple)):
        _fail(422, "result_invalid")
    if history_sources is not None and not isinstance(history_sources, (list, tuple)):
        _fail(422, "result_invalid")

    try:
        locked, execution, _regression, _run, job = _locked_worker_context(
            db, job_id, lease_id
        )
        now = utc_now()
        snapshot, trace_sources, safe_citations, sources = _canonical_result_sources(
            execution.mode,
            list(supplied_sources or []),
            list(history_sources or []),
            list(citations),
            raw_trace.get("candidate_sources"),
        )
        _validate_actual_sources(db, locked, execution, sources, now=now)

        # 唯一用于评分与保存的规范 trace：显式 supplied/history 为权威来源，
        # 调用方 raw trace 中的同名键不得覆盖。
        safe_trace = _sanitize_trace(raw_trace)
        safe_trace["supplied_sources"] = trace_sources["supplied_sources"]
        safe_trace["history_sources"] = trace_sources["history_sources"]
        if "candidate_sources" in trace_sources:
            safe_trace["candidate_sources"] = trace_sources["candidate_sources"]
        response = {
            "answer": answer,
            "citations": safe_citations,
            "trace": safe_trace,
            "http_status": 200,
        }
        try:
            result = grade_case(execution.case, response, include_content=False)
        except EvaluationError:
            raise JobResultError("result_invalid") from None
        if type(result.get("passed")) is not bool:
            raise JobResultError("result_invalid")

        body_payload = {
            "answer": answer,
            "citations": safe_citations,
            "trace": safe_trace,
        }
        body_bytes = _canonical_json(body_payload).encode("utf-8")
        if len(body_bytes) > MAX_RESULT_BODY_BYTES:
            raise JobResultError("result_too_large")
        body_sha = hashlib.sha256(body_bytes).hexdigest()
        failures = [
            str(item)
            for item in (result.get("failures") or [])
            if isinstance(item, str)
        ][:MAX_RESULT_FAILURES]

        # 来源校验/评分/编码可能耗时；写入前用新的完成时间重新核对期限与租约。
        finished = utc_now()
        reason = _lease_reason(job, lease_id, finished)
        if reason is not None:
            raise JobError("lease_invalid", f"作业租约不可用（{reason}）")

        job.status = JOB_STATUS_SUCCEEDED
        job.revision += 1
        job.completed_at = finished
        job.updated_at = finished
        job.lease_id = None
        job.lease_expires_at = None
        job.heartbeat_at = finished
        job.error_code = None
        job.result_sha256 = result["result_sha256"]
        job.machine_sha256 = result["machine_sha256"]
        job.result_passed = bool(result["passed"])
        job.result_failures = failures
        job.result_outcome = result.get("outcome")
        job.result_http_status = result.get("http_status")
        job.citation_count = int(result.get("citation_count") or 0)
        job.result_captured_at = finished
        db.add(
            FeedbackRegressionJobBody(
                job_id=job.id,
                answer=answer,
                citations=safe_citations,
                trace=safe_trace,
                body_sha256=body_sha,
                body_bytes=len(body_bytes),
            )
        )
        db.commit()
    except JobError:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        _integrity_error(exc)
    except Exception:
        db.rollback()
        raise
    return JobCompletion(
        job_id=job.id,
        run_id=job.run_id,
        status=job.status,
        attempt_count=int(job.attempt_count),
        revision=int(job.revision),
        result_sha256=job.result_sha256,
        machine_sha256=job.machine_sha256,
        passed=bool(job.result_passed),
        failures=tuple(failures),
        outcome=job.result_outcome,
        http_status=job.result_http_status,
        citation_count=int(job.citation_count or 0),
        body_sha256=body_sha,
    )


def fail(
    db: DBSession,
    *,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
    error_code: str,
    retryable: bool = False,
) -> JobTransition:
    """显式失败：仅白名单码；仅显式可恢复错误在有界配额内重排队。"""
    code = error_code if error_code in ERROR_CODES else "execution_failed"
    now = utc_now()
    retry_delay = timedelta(seconds=settings.FEEDBACK_REGRESSION_JOB_RETRY_DELAY_SECONDS)
    try:
        job = db.scalar(
            select(FeedbackRegressionJob)
            .where(FeedbackRegressionJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            db.rollback()
            return JobTransition(False, "", "not_found", 0, 0)
        reason = _lease_reason(job, lease_id, now, check_deadline=False)
        if reason is not None:
            db.rollback()
            return JobTransition(
                False,
                job.status,
                reason,
                int(job.attempt_count),
                int(job.revision),
            )
        if _is_deadline_passed(job, now):
            job.error_code = "deadline_exceeded"
            transition = _transition(job, JOB_STATUS_FAILED, now)
        elif (
            retryable
            and code in RETRYABLE_ERROR_CODES
            and int(job.attempt_count)
            < settings.FEEDBACK_REGRESSION_JOB_MAX_ATTEMPTS
        ):
            job.status = JOB_STATUS_QUEUED
            job.available_at = now + retry_delay
            job.lease_id = None
            job.lease_expires_at = None
            job.error_code = code
            job.revision += 1
            job.updated_at = now
            transition = JobTransition(
                True,
                job.status,
                "retry_scheduled",
                int(job.attempt_count),
                int(job.revision),
            )
        else:
            job.error_code = code
            transition = _transition(job, JOB_STATUS_FAILED, now)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return transition


def release(
    db: DBSession,
    *,
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
) -> JobTransition:
    """协作停止：交回租约；有界重试，超限/超时则终态失败。"""
    now = utc_now()
    retry_delay = timedelta(seconds=settings.FEEDBACK_REGRESSION_JOB_RETRY_DELAY_SECONDS)
    try:
        job = db.scalar(
            select(FeedbackRegressionJob)
            .where(FeedbackRegressionJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            db.rollback()
            return JobTransition(False, "", "not_found", 0, 0)
        reason = _lease_reason(job, lease_id, now, check_deadline=False)
        if reason is not None:
            db.rollback()
            return JobTransition(
                False,
                job.status,
                reason,
                int(job.attempt_count),
                int(job.revision),
            )
        if _is_deadline_passed(job, now):
            job.error_code = "deadline_exceeded"
            transition = _transition(job, JOB_STATUS_FAILED, now)
        elif int(job.attempt_count) >= settings.FEEDBACK_REGRESSION_JOB_MAX_ATTEMPTS:
            job.error_code = "attempts_exhausted"
            transition = _transition(job, JOB_STATUS_FAILED, now)
        else:
            job.status = JOB_STATUS_QUEUED
            job.available_at = now + retry_delay
            job.lease_id = None
            job.lease_expires_at = None
            job.revision += 1
            job.updated_at = now
            transition = JobTransition(
                True,
                job.status,
                "requeued",
                int(job.attempt_count),
                int(job.revision),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return transition


__all__ = [
    "ERROR_CODES",
    "MAX_RESULT_BODY_BYTES",
    "RETRYABLE_ERROR_CODES",
    "JobClaim",
    "JobCompletion",
    "JobError",
    "JobRecovery",
    "JobResultError",
    "JobTransition",
    "LeaseCheck",
    "LoadedExecution",
    "batch_read",
    "cancel_batch",
    "cancel_job",
    "check_lease",
    "claim",
    "comparison",
    "complete",
    "enqueue_batch",
    "enqueue_job",
    "fail",
    "get_batch",
    "get_job",
    "heartbeat",
    "job_read",
    "list_jobs",
    "load_execution",
    "lookup_request",
    "recover",
    "release",
    "review_job",
    "to_http",
]
