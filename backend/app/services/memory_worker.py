"""普通对话摘要 worker：独立角色 `--role summary` 的 supervisor 循环。

由 ``worker.py`` 在选中 summary 角色时动态 import 并调用 ``run(stop_event)``；本模块
只被该角色加载，因此缺依赖不会影响 ingest/project 角色。所有数据库访问都是短事务，
**模型网络等待期间不持有数据库连接或行锁**：领取后立刻提交释放，等待结果时用
``stop_event`` 协作停机，heartbeat 周期短事务续租。

约定（与 F 契约一致）：
- 领取采用 ``SKIP LOCKED``，天然支持多 worker 并发；同一会话最多一个 processing。
- 水位以"真正纳入摘要的最大完整轮次 seq"为准（不越过仍 processing 的轮）。
- 发布 CAS：epoch + base_memory_version；reset 或新摘要先到则旧结果不得覆盖。
- 固定结构化 JSON 由 ``memory_prompt`` 校验（含 source 成员/长度/凭据），
  非法/超时/取消/超长一律不发布，有限退避后终态 failed。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import SummaryJob, SummaryJobStatus
from app.services import memory_service, usage_tracking
from app.services.cloud_models import CloudModelError, complete_short_chat
from app.services.context_window import estimate_message_tokens
from app.services.memory_prompt import (
    SummaryFormatError,
    SummaryValidationError,
    build_summary_human,
    build_summary_system,
    parse_summary,
)
from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger("memory_worker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _job_messages(snapshot: dict) -> list:
    """System 只放受信任规则；旧摘要与本批正文都只在低信任 Human 数据消息。

    旧摘要（``snapshot['previous']``）与本次正文（``snapshot['text']``，带 message_id
    标签）分别经 ``build_summary_human`` 做凭据清洗后合入同一条 ``HumanMessage``；
    **绝不**把旧摘要序列化进 System。旧 summary 的来源 id 与内容仍在 Human 完整提供给
    模型以合法延续，正文里已有的 message_id 标签原样保留供模型引用。不伪造 AI 配对。
    """
    previous = snapshot.get("previous")
    rules = build_summary_system(
        previous_summary=previous,
        model_window_tokens=settings.LLM_CONTEXT_WINDOW_TOKENS,
        output_max_tokens=settings.MEMORY_SUMMARY_MAX_TOKENS,
        use_ascii_json=True,
    )
    human = build_summary_human(
        previous_summary=previous,
        batch_text=snapshot.get("text", "") or "",
        use_ascii_json=True,
    )
    return [SystemMessage(content=rules), HumanMessage(content=human)]


def _parse_or_none(raw: str, job: SummaryJob) -> dict | None:
    snapshot = job.input_snapshot or {}
    allowed = snapshot.get("message_ids") or []
    previous = snapshot.get("previous") or {}

    from app.services.memory_service import _collect_sources
    previous_sources = _collect_sources(previous)
    try:
        parsed = parse_summary(
            raw,
            allowed_message_ids=allowed,
            previous_summary_sources=previous_sources,
            max_total_tokens=settings.MEMORY_SUMMARY_MAX_TOKENS,
            max_item_tokens=400,
            max_sources_per_item=8,
            max_items_per_section=50,
        )
    except (SummaryFormatError, SummaryValidationError) as exc:
        logger.warning("摘要输出校验失败 job=%s: %s", job.id, exc)
        return None
    return parsed


def _claim() -> tuple[SummaryJob | None, dict | None]:
    """短事务领取；返回 (job, snapshot)。无任务返回 (None, None)。"""
    with SessionLocal() as db:
        job = memory_service.claim_next_summary(db)
        if job is None:
            return None, None
        snapshot = dict(job.input_snapshot or {})
        return job, snapshot


def _context_over_budget(messages: list, output_max_tokens: int) -> bool:
    """用``context_window.estimate_message_tokens``保守估算最终 messages 总窗口。

    可用输入预算 = 模型窗口 - 输出预留 - CONTEXT_TOKEN_SAFETY_MARGIN。这里用的是真实
    模型窗口（``LLM_CONTEXT_WINDOW_TOKENS``），**不是**历史的 ``MEMORY_HISTORY_TOKEN_BUDGET``
    （那是 memory_service 的历史裁剪预算，由另一 owner 负责，不在此缩输入）。超预算
    返回 True，调用方须在触碰模型前拒绝，不能静默截短持久化快照导致来源 digest 与
    实际输入不一致。
    """
    available_input = (
        int(settings.LLM_CONTEXT_WINDOW_TOKENS)
        - int(output_max_tokens)
        - int(settings.CONTEXT_TOKEN_SAFETY_MARGIN)
    )
    if available_input < 512:
        return True
    return estimate_message_tokens(messages) > available_input


def _process_job(job: SummaryJob, snapshot: dict) -> str | None:
    """调用模型生成结构化摘要 JSON；失败/取消/超预算返回 None。网络期间不持锁。

    usage_scope 包裹真正的模型网络调用（而非发布），用 operation='summary' 独立计量，
    并带上 conversation_id 与 user_id（来自快照），不借 rewrite/生成计费。
    """
    messages = _job_messages(snapshot)
    if _context_over_budget(messages, output_max_tokens=settings.MEMORY_SUMMARY_MAX_TOKENS):
        logger.warning("摘要输入窗口超预算 job=%s estimated=%d available=%d",
                       job.id, estimate_message_tokens(messages),
                       int(settings.LLM_CONTEXT_WINDOW_TOKENS)
                       - int(settings.MEMORY_SUMMARY_MAX_TOKENS)
                       - int(settings.CONTEXT_TOKEN_SAFETY_MARGIN))
        return None
    user_id = snapshot.get("user_id")
    context = usage_tracking.UsageContext(
        user_id=user_id, conversation_id=snapshot.get("conversation_id") or job.conversation_id,
        operation="summary",
    )
    try:
        with usage_tracking.usage_scope(context):
            raw = complete_short_chat(
                messages,
                timeout=settings.MEMORY_SUMMARY_TIMEOUT_SECONDS,
                max_tokens=settings.MEMORY_SUMMARY_MAX_TOKENS,
            )
    except CloudModelError as exc:
        logger.warning("摘要模型调用失败 job=%s: %s", job.id, exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 网络/超时等一律按失败退避
        logger.warning("摘要模型调用异常 job=%s: %s", job.id, type(exc).__name__)
        return None
    return raw


def _reattach(db: Session, job: SummaryJob) -> SummaryJob | None:
    """把跨会话的 job 重新绑定到新会话（按主键取回，杜绝 detached 写不落地）。"""
    return db.get(SummaryJob, job.id)


def _publish(job: SummaryJob, parsed: dict) -> bool:
    """短事务发布；False 表示不该发布（reset/新摘要/来源不符/租约失效）。

    ``publish_summary`` 自己具备权威 fence：在一致的 conv -> job 锁序内、
    ``populate_existing`` 重读当前行、核对不可变 claim token（claim_job_id +
    claim_lease_id）、epoch/base、心跳有效期与真实来源 digest/完成状态。这里不
    再在锁外预读 live 当 fence（旧模型结果由锁内守卫拒绝，不靠调用方自查）。
    """
    with SessionLocal() as db:
        memory = memory_service.publish_summary(db, job, parsed)
        return memory is not None


def _fail(job: SummaryJob, error_code: str, error_message: str) -> None:
    """短事务失败落地；租约已变/被回收则由 ``mark_failed`` 的锁内 fence 拒绝改写，
    旧进程不会把新租约或已回收任务标成错误终态。"""
    with SessionLocal() as db:
        memory_service.mark_failed(db, job, error_code, error_message)


def _release_on_shutdown(job: SummaryJob) -> None:
    """协作停机时把已领取任务归还为可领取（void 租约、不计尝试/退避）。

    shutdown 与 user-cancel 不同：在这里把 processing 归还为 pending，但它不能撞
    "每会话至多一个 pending" 的部分唯一约束——若该会话已有一个 pending，则本任务被
    吸收/作废（合并语义），而不是再插入第二个 pending。

    **锁序与归属必须一次完成**：先锁会话行、再锁任务行（与 claim/publish/reset 一致），
    锁内用 ``populate_existing`` 重读任务并核对本进程的原 claim token（``lease_id`` 与其
    冻结的 ``claim_lease_id`` 相同）及心跳仍在有效期内。旧实现先在锁外读 live、比较租约，
    再等会话锁；等锁期间该任务可能被 recover 回收并被新 owner 重新领取，锁后不重读就会
    让旧进程把新租约/新状态覆写回 pending。任何一项不满足即放手，由当前 owner 或 recover
    负责那一行。
    """
    with SessionLocal() as db:
        live = memory_service.reload_owned_job(db, job)
        if live is None:
            return
        other_pending = db.scalar(
            select(SummaryJob.id).where(
                SummaryJob.conversation_id == live.conversation_id,
                SummaryJob.status == SummaryJobStatus.PENDING.value,
                SummaryJob.id != live.id,
            ).limit(1)
        )
        if other_pending is not None:
            live.status = SummaryJobStatus.CANCELLED.value
            live.lease_id = None
            live.finished_at = _now()
        else:
            live.status = SummaryJobStatus.PENDING.value
            live.lease_id = None
            live.heartbeat_at = None
            live.available_at = None
        db.commit()


_HB_THREADS: list[threading.Thread] = []


def _heartbeat(job: SummaryJob, stop_event: threading.Event):
    """后台续期线程；返回可 stop/join/lost 的句柄。

    ``heartbeat_job`` 返回续期是否真正落地：一旦它返回 False（租约被回收/换租/过期
    拒续），工作线程即把 ``lost`` 置位，主流程在发布前检查它，避免把结果落到已不再
    持有的租约上。停机时显式 stop+join 等待续期线程退出，再回交任务。
    """
    stopped = threading.Event()
    lost = threading.Event()
    interval = max(1.0, min(30.0, settings.MEMORY_JOB_LEASE_SECONDS / 3))

    def loop():
        while not stopped.wait(interval) and not stop_event.is_set():
            try:
                with SessionLocal() as db:
                    if not memory_service.heartbeat_job(db, job):
                        lost.set()
            except Exception:  # noqa: BLE001 - 单次续期失败先不判 lost，继续重试
                logger.warning("摘要租约续期异常 job=%s", job.id)

    thread = threading.Thread(target=loop, name=f"memory-hb-{str(job.id)[:8]}", daemon=True)
    thread.start()
    _HB_THREADS.append(thread)

    class _Handle:
        def stop(self):
            stopped.set()

        def join(self):
            thread.join(timeout=max(2.0, interval * 2))
            if thread.is_alive():
                logger.warning("摘要续期线程未按时退出 job=%s", job.id)

        @property
        def lost(self):
            return lost.is_set()

    return _Handle()


def run(stop_event: threading.Event) -> None:
    """summary 角色 supervisor 循环：恢复 -> 领取 -> 模型 -> 发布，协作停机。"""
    logger.info("memory worker 启动：budget=%d ratio=%s turns=%d keep_recent=%d",
                settings.MEMORY_HISTORY_TOKEN_BUDGET, settings.MEMORY_TRIGGER_RATIO,
                settings.MEMORY_TRIGGER_TURNS, settings.MEMORY_KEEP_RECENT_TURNS)
    last_recover = 0.0
    poll = _poll_seconds()
    while not stop_event.is_set():
        now = time.monotonic()
        if now - last_recover > 30:
            last_recover = now
            try:
                with SessionLocal() as db:
                    memory_service.recover_stale_summary_jobs(db)
            except Exception:  # noqa: BLE001
                logger.exception("摘要任务恢复扫描失败")
        try:
            job, snapshot = _claim()
        except Exception:  # noqa: BLE001 - claim 异常不得杀死 supervisor
            logger.exception("摘要任务领取异常")
            if stop_event.wait(1.0):
                break
            continue
        if job is None:
            if stop_event.wait(poll):
                break
            continue
        if stop_event.is_set():
            _release_on_shutdown(job)
            break
        stopped_hb = _heartbeat(job, stop_event)
        try:
            # 单个任务处理绝不允许异常杀死 supervisor：内部再包一层 try/except，
            # 任何未知异常都按失败退避，循环继续。
            try:
                raw = _process_job(job, snapshot)
            except Exception:  # noqa: BLE001 - 防 supervisor 被单任务异常拖垮
                logger.exception("摘要任务处理异常 job=%s", job.id)
                _fail(job, "worker_error", "摘要处理异常")
                continue
            # 停机发生在模型网络等待期间：不带结果落地，干净归还租约即可。先真正停掉并
            # join 心跳线程，确认续期线程已退出，再回交任务，避免旧续期与回交竞争。
            if stop_event.is_set():
                stopped_hb.stop()
                stopped_hb.join()
                _release_on_shutdown(job)
                break
            # 租约已丢失（recover 回收/换租/过期拒续）：我们已不再持有该任务，既不能
            # 发布也不能写终态——recover 已接管那行，落笔只会被锁内 fence 拒绝。直接跳过。
            if stopped_hb.lost:
                logger.info("摘要租约已丢失 job=%s，跳过发布/失败落地", job.id)
                continue
            if raw is None:
                _fail(job, "model_error", "摘要生成未成功（超时/取消/非法）")
                continue
            parsed = _parse_or_none(raw, job)
            if parsed is None:
                _fail(job, "invalid_output", "摘要输出校验失败")
                continue
            if not _publish(job, parsed):
                logger.info("摘要发布被拒绝 job=%s（reset/新摘要/来源不符/租约失效）", job.id)
                continue
        finally:
            stopped_hb.stop()
            stopped_hb.join()
    logger.info("memory worker 已退出")


def _poll_seconds() -> float:
    try:
        return max(0.5, float(settings.MEMORY_POLL_SECONDS))
    except Exception:  # noqa: BLE001
        return 2.0
