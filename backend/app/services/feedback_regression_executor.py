"""反馈回归在线作业执行器（R2）。

本模块只消费 R1b 冻结的作业契约返回的 ``LoadedExecution``：

* 不创建/修改 Conversation、ChatTurn、Message，也不调用 ``chat_stream`` 或旧
  live CLI；只有本作业自己的新问题/新回答进入临时历史。
* 上传执行始终使用 ``ExecutionContext.upload_manifest`` 的**固定清单**：``()``
  表示永久空范围，任何检索、锚点复核与补充检索都显式携带它；topic switch 不能
  把它放宽为全库，更窄的追问范围只能是清单的子集。
* 项目执行始终使用原会话固定的 snapshot/source/manifest/config 身份；即使源上
  的 ``active_snapshot_id`` 已切换，也不跟随漂移；追问可选文件范围固定在同一
  快照内。
* 每个临时 assistant 都带严格 v1 ``answer_sources.build_source_snapshot``；
  candidate 身份来自实际返回的 pairs 顺序，supplied 身份来自
  ``prepare_answer`` 最终 ``prepared.pairs``，规划依赖与历史依赖都随快照传递。
* 所有改写/向量/重排/生成调用（含重试）都包在该作业从 ``load_execution`` 得到
  的 ``UsageContext`` 内；模型等待期间不持有任何数据库连接或根/作业锁。

等待与授权
==========

生成期间有一个独立的观察线程，用真实授权路径 ``load_execution``（重新按根锁
复核原管理员 token、原所有者 ACL、冻结清单/项目绑定）并续租；它只在短事务内
运行，绝不与模型等待共用连接。生成协程按 ``StreamState`` 的首字/停顿/总时限
推进，并至少每个观察周期醒来检查停止、取消、租约与授权状态。同步检索/改写的
HTTP 超时仍由既有配置约束；本模块不承诺立即中断同步请求，也不会为取消创建无界
线程队列。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import anyio
import httpx
from langchain_core.documents import Document as LCDocument

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import KnowledgeBase, User
from app.services import (
    answer_sources,
    document_scopes,
    followup,
    project_retrieval,
    project_sources,
    rag,
    usage_tracking,
    vector_store,
)
from app.services import feedback_regression_jobs as jobs
from app.services.cloud_models import CloudModelError
from app.services.context_window import ContextBudgetError
from app.services.evaluation import EvaluationError
from app.services.feedback_regression_context import ExecutionContext
from app.services.feedback_regression_jobs import JobError, LoadedExecution
from app.services.stream_control import StreamEvent, StreamFault, StreamState

logger = logging.getLogger(__name__)

# 单次作业最多重放的问题数（执行上下文自身已按 10 轮导出，这里再作硬界）。
MAX_EXECUTION_TURNS = 10
# 生成/等待期间检查停止、租约、授权的基础周期（秒）。测试可缩短该模块常量。
WATCH_INTERVAL_SECONDS = 1.0
# 生成协程每次等待上游的清醒周期；与观察周期一致，保证停止/撤权能及时生效。
STREAM_POLL_SECONDS = 1.0
# 观察线程停止时的有界 join。
WATCH_STOP_JOIN_SECONDS = 5.0

_ANCHOR_STATUSES = frozenset({"reference", "document_followup", "elaboration"})
_UPLOAD_EXECUTION = "upload"
_PROJECT_EXECUTION = "project"
_CHAT_EXECUTION = "chat"

# StreamFault -> (作业白名单错误码, 是否显式可恢复)。
_STREAM_FAULTS: dict[str, tuple[str, bool]] = {
    "first_token_timeout": ("model_timeout", True),
    "idle_timeout": ("model_timeout", True),
    "total_timeout": ("deadline_exceeded", False),
    "stream_limit": ("answer_too_large", False),
    "invalid_stream": ("stream_interrupted", True),
    "stream_interrupted": ("stream_interrupted", True),
}

# 上下文/授权类失败：不重试，错误码落在冻结白名单内。
_SOURCE_REASONS = frozenset({
    "owner_inactive",
    "owner_access_revoked",
    "source_invalid",
    "source_verification_failed",
    "manifest_invalid",
    "manifest_overflow",
    "scope_invalid",
    "project_snapshot_invalid",
    "project_snapshot_changed",
    "sample_changed",
    "root_plan_changed",
    "feedback_stale",
    "feedback_not_verified",
    "version_mismatch",
    "turn_not_found",
    "turns_overflow",
    "annotation_incomplete",
    "source_ambiguous",
    "chunk_missing",
})


class ExecutionStopped(Exception):
    """协作停止：作业应通过 ``release`` 交回租约，绝不发布结果。"""


class ExecutionLeaseLost(Exception):
    """租约/取消/期限已不可用；由作业服务回收或另一个 worker 接手。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ExecutionAborted(Exception):
    """确定性失败：``error_code`` 必须是冻结白名单码。"""

    def __init__(self, error_code: str, retryable: bool = False):
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = bool(retryable)


@dataclass(frozen=True)
class ExecutionOutcome:
    """一次作业最终发布所需的真实新结果（最后一条问题）。"""

    answer: str
    citations: tuple[dict, ...]
    trace: dict
    supplied_sources: tuple[dict, ...]
    history_sources: tuple[dict, ...]


@dataclass(frozen=True)
class _TurnResult:
    outcome: ExecutionOutcome
    history_items: tuple[dict, ...]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _deadline_passed(deadline_at: datetime | None) -> bool:
    deadline = _aware(deadline_at)
    return deadline is not None and datetime.now(timezone.utc) >= deadline


def _questions(execution: ExecutionContext) -> tuple[str, ...]:
    values = tuple(
        item for item in (execution.turns or ()) if isinstance(item, str) and item.strip()
    )
    if not values:
        question = execution.case.get("question") if isinstance(execution.case, dict) else None
        if isinstance(question, str) and question.strip():
            values = (question,)
    if not values:
        raise ExecutionAborted("invalid_result", False)
    return values[-MAX_EXECUTION_TURNS:]


def _top_k(execution: ExecutionContext) -> int:
    case = execution.case if isinstance(execution.case, dict) else {}
    value = case.get("top_k")
    if type(value) is int and 1 <= value <= 20:
        return value
    return max(1, min(20, int(settings.RETRIEVAL_TOP_K)))


def _kb_snapshot(execution: ExecutionContext) -> dict | None:
    """读取执行目标知识库的稳定列；不在模型等待期间持有连接。"""
    if execution.kb_id is None:
        return None
    try:
        kb_id = uuid.UUID(execution.kb_id)
    except (ValueError, TypeError, AttributeError):
        raise ExecutionAborted("source_verification_failed", False) from None
    with SessionLocal() as db:
        kb = db.get(KnowledgeBase, kb_id)
        if kb is None:
            raise ExecutionAborted("source_verification_failed", False)
        return {
            "id": kb.id,
            "kind": kb.kind,
            "collection_name": kb.collection_name,
        }


def _manifest_scope(execution: ExecutionContext) -> list[dict]:
    """上传执行必须在每次检索/锚点/补充决策中使用冻结清单。

    ``None`` 只可能出现在旧 manual 检查上下文；在线 upload 执行缺失清单一律失败
    关闭。显式 ``()`` 原样返回为空列表，绝不退化成全库。
    """
    manifest = execution.upload_manifest
    if not isinstance(manifest, tuple):
        raise ExecutionAborted("source_verification_failed", False)
    result: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for entry in manifest:
        if not isinstance(entry, Mapping):
            raise ExecutionAborted("source_verification_failed", False)
        try:
            document_id = str(uuid.UUID(str(entry.get("document_id"))))
        except (ValueError, TypeError, AttributeError):
            raise ExecutionAborted("source_verification_failed", False) from None
        version = entry.get("index_version")
        if type(version) is not int or version < 0:
            raise ExecutionAborted("source_verification_failed", False)
        key = (document_id, version)
        if key in seen:
            raise ExecutionAborted("source_verification_failed", False)
        seen.add(key)
        result.append({"document_id": document_id, "index_version": version})
    return result


def _manifest_keys(scope: Sequence[Mapping]) -> set[tuple[str, int]]:
    return {(str(item["document_id"]), int(item["index_version"])) for item in scope}


def _pair_identity(metadata: Any) -> tuple[str, int] | None:
    if not isinstance(metadata, Mapping):
        return None
    try:
        document_id = str(uuid.UUID(str(metadata.get("document_id"))))
    except (ValueError, TypeError, AttributeError):
        return None
    version = metadata.get("index_version")
    if type(version) is not int or version < 0:
        return None
    return document_id, version


def _filter_scope(
    pairs: Sequence[tuple[LCDocument, float]], keys: set[tuple[str, int]]
) -> list[tuple[LCDocument, float]]:
    result = []
    for pair in pairs:
        doc = pair[0] if isinstance(pair, (tuple, list)) and pair else None
        if _pair_identity(getattr(doc, "metadata", None)) in keys:
            result.append(pair)
    return result


def _narrow_scope(
    anchors: Sequence[tuple[LCDocument, float]], keys: set[tuple[str, int]]
) -> list[dict]:
    """从已核验锚点生成固定清单的子集；绝不引入清单外文档。"""
    result: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for doc, _score in anchors:
        identity = _pair_identity(getattr(doc, "metadata", None))
        if identity is None or identity not in keys or identity in seen:
            continue
        seen.add(identity)
        result.append({"document_id": identity[0], "index_version": identity[1]})
    return result


def _clarify(plan: followup.RetrievalPlan, message: str) -> followup.RetrievalPlan:
    return plan.with_fields(status="clarify", clarification=message)


def _plan_turn(
    question: str,
    history: list[dict],
    scope: list[dict] | None,
) -> followup.RetrievalPlan:
    options = {"document_scope": scope} if scope is not None else {}
    return followup.plan_retrieval(question, history, **options)


def _retrieve_upload(
    execution: ExecutionContext,
    kb: Mapping,
    plan: followup.RetrievalPlan,
    top_k: int,
) -> tuple[list[tuple[LCDocument, float]], followup.RetrievalPlan]:
    """上传库检索：固定清单是最终硬边界，追问只能收窄。"""
    base = _manifest_scope(execution)
    keys = _manifest_keys(base)
    collection = str(kb["collection_name"])
    if plan.status in _ANCHOR_STATUSES:
        requested = list(plan.citations)
        if plan.status == "elaboration":
            # 与在线 chat 的“有活动范围”分支一致：只在清单内补充新证据。
            requested = [item for item in requested if document_scopes.contains(base, item)]
        if any(not document_scopes.contains(base, item) for item in requested):
            return [], _clarify(
                plan, "该历史来源不在本次固定执行范围内，请重新提交作业。"
            )
        anchors = vector_store.load_ready_citations(collection, requested) if requested else []
        if len(anchors) != len(requested):
            return [], _clarify(
                plan, "您指定的历史来源已不可用、被更新或无法核验，请重新提交作业。"
            )
        anchors = _filter_scope(anchors, keys)
        if len(anchors) != len(requested):
            return [], _clarify(
                plan, "历史来源已不在本次固定执行范围内，请重新提交作业。"
            )
        if plan.status == "reference":
            return anchors, plan
        if plan.status == "document_followup":
            selected = _narrow_scope(anchors, keys)
        else:
            selected = base
        docs = rag.supplement_sources(
            collection, plan.topic or plan.query, anchors, top_k, selected
        )
        return _filter_scope(docs, keys), plan
    docs = rag.retrieve(collection, plan.query, top_k, document_scope=base)
    return _filter_scope(docs, keys), plan


def _project_identity(execution: ExecutionContext) -> tuple[str, str, str, str]:
    project = execution.project if isinstance(execution.project, Mapping) else None
    if not project:
        raise ExecutionAborted("source_verification_failed", False)
    values = (
        str(project.get("snapshot_id") or ""),
        str(project.get("source_id") or ""),
        str(project.get("manifest_fingerprint") or ""),
        str(project.get("config_fingerprint") or ""),
    )
    if not all(values):
        raise ExecutionAborted("source_verification_failed", False)
    return values  # type: ignore[return-value]


def _project_doc_matches(execution: ExecutionContext, doc: LCDocument) -> bool:
    """固定快照身份复核：任何字段漂移都失败关闭，绝不静默采信。"""
    snapshot_id, source_id, manifest, config = _project_identity(execution)
    meta = doc.metadata if isinstance(doc.metadata, Mapping) else {}
    return (
        str(meta.get("snapshot_id") or "") == snapshot_id
        and str(meta.get("source_id") or "") == source_id
        and str(meta.get("snapshot_manifest_fingerprint") or "") == manifest
        and str(meta.get("snapshot_config_fingerprint") or "") == config
    )


def _verify_project_docs(execution: ExecutionContext, docs: Sequence) -> None:
    for doc in docs:
        if not _project_doc_matches(execution, doc):
            raise ExecutionAborted("source_verification_failed", False)


def _combine_project_pairs(
    anchors: Sequence[tuple[LCDocument, float]],
    fresh: Sequence[LCDocument],
    top_k: int,
) -> list[tuple[LCDocument, float]]:
    """与在线 chat 的项目检索组合一致：锚点在前、补充新原文、按 chunk 去重。"""
    pairs = [(doc, 0.0) for doc in fresh]
    for doc, _score in pairs:
        doc.metadata.setdefault("citation_score", None)
    anchor_limit = min(5, max(1, top_k // 2))
    result: list[tuple[LCDocument, float]] = []
    covered: set[tuple[str, int]] = set()
    for doc, score in [*anchors[:anchor_limit], *pairs, *anchors[anchor_limit:]]:
        meta = doc.metadata or {}
        try:
            first = meta.get("chunk_start", meta["chunk_index"])
            last = meta.get("chunk_end", meta["chunk_index"])
            file_version_id = str(meta["file_version_id"])
            if any(type(value) is not int for value in (first, last)) or not 0 <= first <= last:
                continue
        except (KeyError, TypeError):
            continue
        keys = {(file_version_id, index) for index in range(first, last + 1)}
        if keys <= covered:
            continue
        result.append((doc, score))
        covered.update(keys)
        if len(result) >= top_k:
            break
    return result


def _retrieve_project(
    execution: ExecutionContext,
    plan: followup.RetrievalPlan,
    top_k: int,
) -> tuple[list[tuple[LCDocument, float]], followup.RetrievalPlan]:
    """项目库检索：固定原快照与可选文件范围；不跟随 active_snapshot_id。"""
    project = execution.project if isinstance(execution.project, Mapping) else None
    if not project or not project.get("snapshot_id"):
        return [], _clarify(plan, "项目快照已不可用，请重新提交作业。")
    snapshot_id, _source_id, _manifest, _config = _project_identity(execution)
    try:
        snapshot_uuid = uuid.UUID(snapshot_id)
        owner_id = uuid.UUID(execution.owner_id)
    except (ValueError, TypeError, AttributeError):
        raise ExecutionAborted("source_verification_failed", False) from None
    anchors: list[tuple[LCDocument, float]] = []
    try:
        with SessionLocal() as db:
            user = db.get(User, owner_id)
            if user is None or not user.is_active:
                raise ExecutionAborted("source_verification_failed", False)
            file_ids: list[str] | None = None
            if plan.status in _ANCHOR_STATUSES:
                requested = list(plan.citations)
                if any(
                    item.get("source_kind") != "project"
                    or item.get("snapshot_id") != str(snapshot_uuid)
                    for item in requested
                ):
                    return [], _clarify(
                        plan, "该历史来源不属于当前固定项目快照，请重新提交作业。"
                    )
                anchors = project_retrieval.load_project_citations(
                    db, snapshot_uuid, requested, user
                )
                if len(anchors) != len(requested):
                    return [], _clarify(
                        plan, "您指定的项目来源已不可用或无法核验，请重新提交作业。"
                    )
                for (doc, _score), citation in zip(anchors, requested):
                    doc.metadata["historical_source_index"] = citation.get("source_index")
                _verify_project_docs(execution, [doc for doc, _score in anchors])
                if plan.status == "reference":
                    return anchors, plan
                file_ids = list(
                    dict.fromkeys(doc.metadata["file_version_id"] for doc, _score in anchors)
                )
            fresh = project_retrieval.retrieve_project(
                db,
                snapshot_uuid,
                user,
                plan.topic or plan.query,
                top_k,
                file_version_ids=file_ids,
            )
            _verify_project_docs(execution, fresh)
    except (project_retrieval.ProjectSnapshotNotReady,
            project_sources.ProjectSourceNotFound,
            project_sources.ProjectSourceRevoked):
        raise ExecutionAborted("source_verification_failed", False) from None
    return _combine_project_pairs(anchors, fresh, top_k), plan


def _retrieve_turn(
    execution: ExecutionContext,
    kb: Mapping | None,
    plan: followup.RetrievalPlan,
    top_k: int,
) -> tuple[list[tuple[LCDocument, float]], followup.RetrievalPlan]:
    kind = execution.execution_kind
    if kind == _UPLOAD_EXECUTION:
        if kb is None or kb.get("kind") != "upload":
            raise ExecutionAborted("source_verification_failed", False)
        return _retrieve_upload(execution, kb, plan, top_k)
    if kind == _PROJECT_EXECUTION:
        if kb is None or kb.get("kind") != "project":
            raise ExecutionAborted("source_verification_failed", False)
        return _retrieve_project(execution, plan, top_k)
    if kind == _CHAT_EXECUTION:
        return [], plan
    raise ExecutionAborted("invalid_result", False)


def _prepare_turn(
    question: str,
    docs: list[tuple[LCDocument, float]],
    history: list[dict],
    mode: str,
    plan: followup.RetrievalPlan,
):
    try:
        return rag.prepare_answer(
            question, docs, history, mode=mode, retrieval_query=plan.query
        )
    except ContextBudgetError:
        raise ExecutionAborted("execution_failed", False) from None


def _strict_supplied(pairs: Sequence) -> tuple[dict, ...]:
    try:
        return answer_sources.sources_from_pairs(pairs)
    except (answer_sources.SourceSnapshotError, ValueError, TypeError):
        raise ExecutionAborted("source_verification_failed", False) from None


def _sources(items: Sequence[Mapping]) -> tuple[dict, ...]:
    try:
        return answer_sources.parse_sources(
            list(items), default_kind=answer_sources.SOURCE_KIND_DOCUMENT
        )
    except (answer_sources.SourceSnapshotError, ValueError, TypeError):
        raise ExecutionAborted("source_verification_failed", False) from None


def _snapshot(
    supplied: Sequence[Mapping], history: Sequence[Mapping], mode: str
) -> dict:
    try:
        # 第二参数是 assistant 历史消息；这里收到的是已经提取出的来源身份，
        # 先严格解析成 v1 消息，避免 sources_from_history 将无 role 的来源静默跳过。
        inherited = answer_sources.parse_sources(list(history))
        provenance = {
            "source_snapshot_version": answer_sources.SOURCE_SNAPSHOT_VERSION,
            "supplied_sources": [],
            "history_sources": [dict(item) for item in inherited],
        }
        answer_sources.parse_source_snapshot(provenance, mode=mode)
        messages = [{"role": "assistant", "source_provenance": provenance}]
        return answer_sources.build_source_snapshot(list(supplied), messages, mode=mode)
    except (answer_sources.SourceSnapshotError, ValueError, TypeError):
        raise ExecutionAborted("source_verification_failed", False) from None


def _usage_input_output(metrics: Mapping | None) -> dict:
    result = {}
    if isinstance(metrics, Mapping):
        for key in ("input_tokens", "output_tokens"):
            value = metrics.get(key)
            if type(value) is int and value >= 0:
                result[key] = value
    return result


def _raise_for_watch(stop_event: threading.Event, watch: "_LeaseWatch") -> None:
    if stop_event.is_set():
        raise ExecutionStopped()
    if watch.lease_reason is not None:
        if watch.lease_reason == "deadline_exceeded":
            raise ExecutionAborted("deadline_exceeded", False)
        raise ExecutionLeaseLost(watch.lease_reason)
    if watch.error_code is not None:
        raise ExecutionAborted(watch.error_code, watch.retryable)


def _raise_for_deadline(loaded: LoadedExecution) -> None:
    if _deadline_passed(loaded.deadline_at):
        raise ExecutionAborted("deadline_exceeded", False)


async def _generate(
    loaded: LoadedExecution,
    question: str,
    docs: list[tuple[LCDocument, float]],
    history: list[dict],
    prepared,
    mode: str,
    stop_event: threading.Event,
    watch: "_LeaseWatch",
) -> tuple[str, dict, int | None, float]:
    """有界消费原生异步生成流；停止/撤权/期限会关闭上游生成器。

    同步检索/改写不在本函数内；这里只处理异步生成。等待期间每
    ``STREAM_POLL_SECONDS`` 至少醒来一次，检查观察线程发现的状态。
    """
    state = StreamState()
    metrics: dict = {}
    started = time.monotonic()
    tokens = rag.answer_astream(
        question,
        docs,
        history,
        mode=mode,
        prepared=prepared,
        metrics=metrics,
        usage_context=loaded.usage_context,
    )
    parts: list[str] = []
    pending: asyncio.Task | None = None
    try:
        while True:
            _raise_for_watch(stop_event, watch)
            remaining = state.remaining()
            if pending is None:
                pending = asyncio.create_task(anext(tokens))
            timeout = min(remaining, max(0.01, STREAM_POLL_SECONDS))
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            _raise_for_watch(stop_event, watch)
            state.remaining()
            if pending not in done:
                continue
            try:
                token = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = None
            if not isinstance(token, str):
                raise StreamFault("invalid_stream", "模型返回的正文格式异常")
            parts.append(token)
            state.observe(StreamEvent("delta", {"content": token}))
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await pending
        with anyio.CancelScope(shield=True):
            with suppress(asyncio.CancelledError, Exception):
                await tokens.aclose()
    reason = metrics.get("finish_reason")
    if reason == "length":
        raise ExecutionAborted("answer_too_large", False)
    answer = "".join(parts).strip()
    if not answer:
        raise ExecutionAborted("generation_empty", False)
    return answer, metrics, state.first_token_ms, (time.monotonic() - started) * 1000


async def _run_turn(
    loaded: LoadedExecution,
    execution: ExecutionContext,
    kb: Mapping | None,
    question: str,
    history: list[dict],
    stop_event: threading.Event,
    watch: "_LeaseWatch",
) -> _TurnResult:
    mode = execution.mode
    top_k = _top_k(execution)
    started = time.monotonic()
    rewrite_ms = retrieve_ms = generation_ms = 0.0
    generation_metrics: dict = {}
    first_token_ms: int | None = None
    plan = followup.RetrievalPlan(question)
    docs: list[tuple[LCDocument, float]] = []
    candidate_sources: tuple[dict, ...] = ()
    supplied: tuple[dict, ...] = ()
    history_sources: tuple[dict, ...] = ()
    citations: tuple[dict, ...] = ()

    scope = _manifest_scope(execution) if execution.execution_kind == _UPLOAD_EXECUTION else None
    if mode == "rag":
        t = time.monotonic()

        def plan_turn():
            with usage_tracking.usage_scope(
                loaded.usage_context.for_operation("rewrite")
            ):
                return _plan_turn(question, history, scope)

        plan = await anyio.to_thread.run_sync(plan_turn)
        rewrite_ms = (time.monotonic() - t) * 1000
        if not plan.clarification:
            t = time.monotonic()

            def retrieve_turn():
                with usage_tracking.usage_scope(
                    loaded.usage_context.for_operation("query_embedding")
                ):
                    return _retrieve_turn(execution, kb, plan, top_k)

            docs, plan = await anyio.to_thread.run_sync(retrieve_turn)
            retrieve_ms = (time.monotonic() - t) * 1000

    if plan.clarification:
        answer = plan.clarification
        history_sources = _sources(plan.planning_sources)
    else:
        candidate_sources = _strict_supplied(docs) if docs else ()
        prepared = await anyio.to_thread.run_sync(
            lambda: _prepare_turn(question, docs, history, mode, plan)
        )
        docs = list(prepared.pairs)
        supplied = _strict_supplied(docs) if docs else ()
        history_sources = _sources(
            [*plan.planning_sources, *prepared.history_sources]
        )
        t = time.monotonic()
        answer, generation_metrics, first_token_ms, generation_ms = await _generate(
            loaded, question, docs, history, prepared, mode, stop_event, watch
        )
        citations = tuple(rag.finalize_citations(answer, rag.to_citations(docs)))

    snapshot = _snapshot(supplied, history_sources, mode)
    trace = {
        "status": plan.status,
        "candidate_sources": [dict(item) for item in candidate_sources],
        "rewrite_ms": round(rewrite_ms, 2),
        "retrieve_ms": round(retrieve_ms, 2),
        "generation_ms": round(generation_ms, 2),
        "total_ms": round((time.monotonic() - started) * 1000, 2),
        "first_token_ms": first_token_ms,
        "finish_reason": generation_metrics.get("finish_reason"),
        "usage": _usage_input_output(generation_metrics) or None,
    }
    outcome = ExecutionOutcome(
        answer=answer,
        citations=citations,
        trace=trace,
        supplied_sources=supplied,
        history_sources=history_sources,
    )
    history_items = (
        {"role": "user", "content": question},
        {
            "role": "assistant",
            "content": answer,
            "citations": [dict(item) for item in citations],
            answer_sources.SOURCE_PROVENANCE_FIELD: snapshot,
            "followup_topic": plan.topic if isinstance(plan.topic, str) else None,
        },
    )
    return _TurnResult(outcome=outcome, history_items=history_items)


async def execute(
    loaded: LoadedExecution, *, stop_event: threading.Event
) -> ExecutionOutcome:
    """执行一个已领取的在线作业，返回最后一条问题的真实新结果。

    本函数不调用 ``complete``/``fail``/``release``；调用方在短事务内按结果发布或
    交回。所有模型等待都不持有任何数据库连接或根/作业锁。
    """
    execution = loaded.execution
    if not isinstance(execution, ExecutionContext):
        raise ExecutionAborted("invalid_result", False)
    if execution.execution_kind == _UPLOAD_EXECUTION:
        # 在任何检索之前显式校验冻结清单结构（包括显式空清单）。
        _manifest_scope(execution)
    if execution.execution_kind == _PROJECT_EXECUTION:
        _project_identity(execution)
    kb = await anyio.to_thread.run_sync(_kb_snapshot, execution)
    questions = _questions(execution)
    watch = _LeaseWatch(loaded, stop_event, interval=WATCH_INTERVAL_SECONDS)
    watch.start()
    try:
        history: list[dict] = []
        result: _TurnResult | None = None
        for question in questions:
            _raise_for_watch(stop_event, watch)
            _raise_for_deadline(loaded)
            result = await _run_turn(
                loaded, execution, kb, question, history, stop_event, watch
            )
            history.extend(result.history_items)
        assert result is not None
        return result.outcome
    finally:
        watch.stop()


# ---------------------------------------------------------------------------
# 真实授权观察（短事务；不参与 complete 的最终校验）
# ---------------------------------------------------------------------------


def _failure_for_job_error(reason: str) -> tuple[str | None, bool]:
    """load_execution/complete 的稳定 JobError -> 白名单失败码。

    ``lease_invalid`` 不是执行失败：租约/取消/期限由 recover 或其它 worker 处理。
    """
    if reason == "lease_invalid":
        return None, False
    if reason in {"auth_invalid", "admin_required", "job_not_found"}:
        return "execution_failed", False
    if reason in _SOURCE_REASONS:
        return "source_verification_failed", False
    if reason == "result_invalid":
        return "invalid_result", False
    if reason == "result_too_large":
        return "answer_too_large", False
    return "execution_failed", False


def classify_failure(exc: BaseException) -> tuple[str | None, bool]:
    """把执行异常映射到冻结白名单码；``None`` 表示交给租约/恢复流程。"""
    if isinstance(exc, ExecutionAborted):
        return exc.error_code, exc.retryable
    if isinstance(exc, (ExecutionStopped, ExecutionLeaseLost)):
        return None, False
    if isinstance(exc, StreamFault):
        return _STREAM_FAULTS.get(exc.code, ("stream_interrupted", True))
    if isinstance(exc, ContextBudgetError):
        return "execution_failed", False
    if isinstance(exc, answer_sources.SourceSnapshotError):
        return "source_verification_failed", False
    if isinstance(exc, jobs.JobResultError):
        return "invalid_result", False
    if isinstance(exc, EvaluationError):
        return "invalid_result", False
    if isinstance(exc, CloudModelError):
        return "model_error", True
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return "model_timeout", True
    if isinstance(exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)):
        return "model_unavailable", True
    if isinstance(exc, jobs.JobError):
        return _failure_for_job_error(exc.reason)
    return "internal_error", True


class _LeaseWatch:
    """生成期间的真实授权/租约观察线程。

    每个周期用短事务依次执行 ``check_lease``（精确原因）、``heartbeat``（续租约）
    与 ``load_execution``（按根锁重新授权并复核冻结绑定）。任何 DB/连接都不跨
    模型等待保留。观察失败只让下一周期重试，绝不伪造成功或失败。
    """

    def __init__(
        self,
        loaded: LoadedExecution,
        stop_event: threading.Event,
        *,
        interval: float = WATCH_INTERVAL_SECONDS,
    ):
        self._loaded = loaded
        self._stop_event = stop_event
        self._interval = max(0.05, float(interval))
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self.lease_reason: str | None = None
        self.error_code: str | None = None
        self.retryable = False

    def start(self) -> None:
        if self._thread is not None:
            return
        thread = threading.Thread(
            target=self._loop,
            name=f"regression-watch-{self._loaded.job_id}",
            daemon=True,
        )
        thread.start()
        self._thread = thread

    def stop(self) -> None:
        self._stopped.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=WATCH_STOP_JOIN_SECONDS)
            if thread.is_alive():
                logger.warning(
                    "回归作业观察线程未在时限内退出 job=%s", self._loaded.job_id
                )

    def _loop(self) -> None:
        try:
            while not self._stopped.wait(self._interval):
                if self._stop_event.is_set():
                    return
                try:
                    self._tick()
                except Exception as exc:  # noqa: BLE001 - 观测失败只重试，不改变作业
                    logger.warning(
                        "回归作业观察失败 job=%s err=%s",
                        self._loaded.job_id,
                        type(exc).__name__,
                    )
                if self.lease_reason is not None or self.error_code is not None:
                    return
        except Exception as exc:  # noqa: BLE001 - 守护线程不得外溢
            logger.warning(
                "回归作业观察线程异常退出 job=%s err=%s",
                self._loaded.job_id,
                type(exc).__name__,
            )

    def _tick(self) -> None:
        job_id, lease_id = self._loaded.job_id, self._loaded.lease_id
        with SessionLocal() as db:
            check = jobs.check_lease(db, job_id=job_id, lease_id=lease_id)
            if not check.valid:
                self.lease_reason = check.reason or "not_running"
                return
            if not jobs.heartbeat(db, job_id=job_id, lease_id=lease_id):
                again = jobs.check_lease(db, job_id=job_id, lease_id=lease_id)
                self.lease_reason = again.reason or "lease_expired"
                return
            try:
                jobs.load_execution(db, job_id=job_id, lease_id=lease_id)
            except JobError as exc:
                if exc.reason == "lease_invalid":
                    again = jobs.check_lease(db, job_id=job_id, lease_id=lease_id)
                    if not again.valid:
                        self.lease_reason = again.reason or "lease_expired"
                    return
                code, retryable = _failure_for_job_error(exc.reason)
                if code is not None:
                    self.error_code, self.retryable = code, retryable


__all__ = [
    "ExecutionAborted",
    "ExecutionLeaseLost",
    "ExecutionOutcome",
    "ExecutionStopped",
    "MAX_EXECUTION_TURNS",
    "STREAM_POLL_SECONDS",
    "WATCH_INTERVAL_SECONDS",
    "classify_failure",
    "execute",
]
