"""Conversation memory service layer for chat summary (phase 1).

Only depends on F's landed model columns (ConversationMemory / SummaryJob, the
Conversation.next_turn_seq/memory_* counters, ChatTurn.turn_seq, Message.turn_seq).
All writes run inside the caller's short transaction and this module does not
commit on its own (except marked standalone maintenance helpers). The model
network wait happens in the worker module, not here, so no DB lock is held during it.

Invariants:
- Only complete_turn-successful complete turns are counted (no full-body scan, no
  long transaction inside the session lock); enqueue uses bounded incremental
  counters plus the just-finished turn.
- Watermark covered_turn_seq = max complete-turn seq truly included in the
  published summary (not the requested target, and never crossing a still-
  processing turn).
- Publish CAS: epoch + ConversationMemory.version == base_memory_version; a
  reset or a newer summary must not be overwritten by a late result.
- Claim rebases a pending job onto the *current* published memory under the
  conv -> job locks before freezing its snapshot: a task enqueued at base N must
  never keep calling the model against base N once version N+1 has landed. If,
  after rebasing, there is no uncovered complete turn left, the task is
  superseded (cancelled) instead of being retried.
- A refused publish is never left as a stranded ``processing`` row: while the
  caller still holds its ORIGINAL claim lease it is requeued with bounded
  backoff (or cancelled when another pending already took the vehicle); when
  the lease is no longer ours it is left untouched. Neither path fenced-writes
  over another owner's lease.
- Lock order is always conv -> job (claim/publish/reset lock the conversation
  row first, then that conversation's jobs), consistent with reset so no
  deadlock.
- Legacy turns (turn_seq NULL, pairing unprovable) are never summarized; they
  surface as lag/None rather than being force-compressed.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (
    CONVERSATION_MODE_CHAT,
    ChatTurn,
    Conversation,
    ConversationMemory,
    Message,
    SummaryJob,
    SummaryJobStatus,
)
from app.schemas.chat import MemoryResetResult, MemoryStatus
from app.services import conversation_memory as cm
from app.services.context_window import estimate_message_tokens
from app.services.memory_prompt import (
    ITEMS_SECTIONS,
    summarize_into_text,
    validate_sources,
    redact_summary,
)

logger = logging.getLogger(__name__)

# Optional hard character ceiling for triggering; 0 disables it (token ratio / turn
# count only).
MEMORY_TRIGGER_CHAR_CEILING = 0

# Fixed overhead for the summary prompt (schema + instructions), subtracted from
# the input budget so old-summary + new batch still fit the model window.
PROMPT_OVERHEAD_TOKENS = 1024


def memory_enabled(conv: Conversation) -> bool:
    return bool(settings.CHAT_MEMORY_ENABLED) and conv.mode == CONVERSATION_MODE_CHAT


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _older_than_lease(moment: datetime) -> bool:
    """True when ``moment`` predates the current lease deadline.

    SQLite-backed tests hand back naive datetimes (``DateTime(timezone=True)`` loses
    the tz info there); treat those as UTC instead of raising on a mixed compare.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment < _now() - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS)


def latest_memory(db: Session, conversation_id: uuid.UUID) -> ConversationMemory | None:
    return db.scalar(
        select(ConversationMemory)
        .where(ConversationMemory.conversation_id == conversation_id)
        .order_by(ConversationMemory.version.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


def _lock_conversation(db: Session, conversation_id) -> Conversation | None:
    """Lock the conversation row and force a refresh from the locked read.

    ``SessionLocal`` is ``expire_on_commit=False`` and a ``SELECT ... FOR UPDATE``
    does **not** overwrite attributes already present in the Session's identity map.
    A caller that read the conversation earlier in the same Session (e.g. before a
    reset committed in another Session) would therefore keep seeing the old
    ``memory_epoch`` even though the row lock is held. ``populate_existing`` makes the
    locked read authoritative, so epoch/base decisions are never made against a
    stale object. This is the single lock-order entry (conv -> job) for claim,
    publish, fail, recover and shutdown-release.
    """
    return db.scalar(
        select(Conversation)
        .where(Conversation.id == conversation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _job_counts(db: Session, conversation_id: uuid.UUID) -> tuple[bool, bool, bool]:
    """Return (pending, processing, failed) with LIMIT 1 targeted queries, no full scan."""
    def _exists(status: str) -> bool:
        return db.scalar(
            select(SummaryJob.id)
            .where(SummaryJob.conversation_id == conversation_id,
                   SummaryJob.status == status)
            .limit(1)
        ) is not None

    return (_exists(SummaryJobStatus.PENDING.value),
            _exists(SummaryJobStatus.PROCESSING.value),
            _exists(SummaryJobStatus.FAILED.value))


# ---------- incremental counters (inside complete_turn transaction) ----------


def _turn_metrics(user_msg: Message, assistant_msg: Message) -> tuple[int, int, int]:
    chars = len(user_msg.content) + len(assistant_msg.content)
    tokens = estimate_message_tokens([_as_dict(user_msg), _as_dict(assistant_msg)])
    return 1, chars, tokens


def _as_dict(message: Message) -> dict:
    return {"role": message.role, "content": message.content, "status": message.status}


def add_completed_turn(db: Session, conv: Conversation, turn: ChatTurn,
                       user_msg: Message, assistant_msg: Message) -> None:
    """Add one just-completed turn to the conversation's incremental counters."""
    if turn.turn_seq is None:
        return
    turns, chars, tokens = _turn_metrics(user_msg, assistant_msg)
    conv.memory_completed_turns += turns
    conv.memory_completed_chars += chars
    conv.memory_completed_tokens += tokens
    conv.updated_at = _now()


def reconcile_legacy_stats(db: Session, conv: Conversation) -> bool:
    """One-time bounded seeding of legacy complete-turn stats into the counters.

    I (memory service) own ``memory_completed_*``; F's turn_seq backfill does NOT
    initialize them, so a pre-memory conversation starts at 0 despite having complete
    turns and would never trigger a summary. This seeds the real count/chars/tokens
    **once**, and is idempotent:

    - Gated on the new explicit ``memory_stats_initialized`` marker (NOT on
      ``memory_completed_turns == 0``): after the first attempt the conversation is
      marked initialized and never re-scans on every claim. A purely-new conversation
      is trivially complete (0 complete turns) and also marked so.
    - Runs under the conversation row lock held by the caller (claim_turn), sharing
      the same transaction domain as backfill; must be invoked BEFORE a new turn is
      allocated so the in-flight turn is never counted.
    - Reuses F's ``backfill_turn_seq`` (read-only, unmodified) to first provably pair
      old all-NULL legacy messages before counting, so old history never permanently
      misses stats. ``backfill_turn_seq`` only flushes; the caller commits.
    - Uses a real complete-turn pair count (never a seq-diff) with a bounded result,
      processed in continuation batches rather than a full-history scan per claim.
    - If the bounded read is truncated / the count is uncertain (can't prove all old
      turns), it sets ``memory_stats_initialized=True`` but ``memory_stats_complete=
      False`` and DOES NOT seed a partial number: the status reads the count as
      unknown (None) rather than claiming a partial count as the full truth.
    """
    if not memory_enabled(conv) or (conv.memory_stats_initialized or False):
        return False
    # Backfill legacy turn_seq once so provable old complete turns become countable.
    from app.db.migrations import backfill_turn_seq
    backfill_turn_seq(db, conv.id)

    counted, truncated = _count_complete_turns_bounded(db, conv.id)
    conv.memory_stats_initialized = True
    conv.memory_stats_complete = not truncated
    if truncated:
        # 不确定/读截断：不把 partial 当全量。计数保持未知；complete=false 让状态端
        # 把 uncovered 显示为 None，也不基于它触发摘要。
        logger.warning("legacy stats init truncated conversation=%s (count unknown)",
                       conv.id)
    elif counted:
        turns, chars, tokens = counted
        conv.memory_completed_turns = (conv.memory_completed_turns or 0) + turns
        conv.memory_completed_chars = (conv.memory_completed_chars or 0) + chars
        conv.memory_completed_tokens = (conv.memory_completed_tokens or 0) + tokens
        logger.info("reconciled legacy stats conversation=%s turns=%d chars=%d tokens=%d",
                    conv.id, turns, chars, tokens)
    conv.updated_at = _now()
    return True


def _count_complete_turns_bounded(db: Session, conversation_id: uuid.UUID):
    """Count proven complete-turn pairs in one bounded, complete-or-truncated read.

    Loads all completed messages with turn_seq NOT NULL (after backfill) up to a hard
    cap and groups them into proven pairs. Returns ( (turns, chars, tokens) | None,
    truncated ). ``truncated=True`` when the read hit the cap before exhausting —
    the true count is unknown (never claim a partial count as full). The cap is far
    above any real per-session batch and only guards against unbounded scans; a
    conversation under the cap is fully counted and marked complete.
    """
    cap = settings.MEMORY_MAX_INPUT_TURNS * 10 + 2000
    rows = db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id,
               Message.turn_seq.is_not(None),
               Message.status == "completed")
        .order_by(Message.turn_seq.asc(), Message.role.asc())
        .limit(cap)
    ).scalars().all()
    if len(rows) >= cap:
        return None, True
    if not rows:
        return None, False
    messages = [{
        "seq": m.turn_seq, "role": m.role, "content": m.content, "status": m.status,
        "id": str(m.id),
        "turn_id": str(m.turn_id) if m.turn_id is not None else None,
        "reply_to_id": str(m.reply_to_id) if m.reply_to_id is not None else None,
    } for m in rows]
    turns = cm.completed_turns_after(messages, 0)
    if not turns:
        return None, False
    chars = sum(len(t.user["content"]) + len(t.assistant["content"]) for t in turns)
    tokens = sum(estimate_message_tokens([t.user, t.assistant]) for t in turns)
    return (len(turns), chars, tokens), False


def _covered_metrics(memory: ConversationMemory | None) -> tuple[int, int, int]:
    if memory is None:
        return 0, 0, 0
    return (
        int(memory.covered_turns or 0),
        int(memory.covered_chars or 0),
        int(memory.covered_tokens or 0),
    )


def _uncovered_counts(conv: Conversation, memory: ConversationMemory | None) -> tuple[int | None, int | None, int | None]:
    """Real uncovered complete-turn count/chars/tokens.

    Covered counters are treated as 0 when no memory exists; only an unknown
    incremental counter yields None. When legacy init was truncated/uncertain
    (``memory_stats_complete=False``) the counters are NOT relied upon — the count
    is unknown (None), never a partial claimed as full. After reset the memory row
    is deleted so this returns the full historical count again, letting a fresh
    summary rebuild.
    """
    if not (conv.memory_stats_complete or False):
        return None, None, None
    if conv.memory_completed_turns is None:
        return None, None, None
    covered_t, covered_c, covered_k = _covered_metrics(memory)
    return (
        max(0, int(conv.memory_completed_turns) - covered_t),
        max(0, int(conv.memory_completed_chars) - covered_c),
        max(0, int(conv.memory_completed_tokens) - covered_k),
    )


def maybe_enqueue_summary(db: Session, conv: Conversation, turn: ChatTurn) -> bool:
    """Decide whether to enqueue/merge a summary job during the complete_turn txn.

    Trigger is threshold-only (token ratio / turn count / char ceiling). Whether to
    create or merge a pending job is borne by the DB partial unique constraints
    (at most 1 pending + 1 processing per conversation): merge into an existing
    pending; otherwise create one even if a job is processing (so targets that
    arrive while processing never get dropped; the pending is the vehicle that
    covers post-claim turns and the re-evaluation after a publish).
    """
    if not memory_enabled(conv) or turn.turn_seq is None:
        return False
    # 初始化未完整/不确定（旧史读截断）：增量计数未知，不能基于 partial 触发摘要。
    if not (conv.memory_stats_complete or False):
        return False
    memory = latest_memory(db, conv.id)
    covered_turns, covered_chars, covered_tokens = _covered_metrics(memory)
    unarchived = int(conv.memory_completed_turns or 0) - covered_turns
    history_chars = int(conv.memory_completed_chars or 0) - covered_chars
    history_tokens = int(conv.memory_completed_tokens or 0) - covered_tokens
    trigger = cm.should_summarize(
        memory=cm.MemoryState(
            mode="chat", enabled=True,
            epoch=conv.memory_epoch or 0,
            base_memory_version=memory.version if memory else 0,
            covered_turn_seq=memory.covered_turn_seq if memory else 0,
            next_turn_seq=conv.next_turn_seq or 0,
            memory_completed_turns=conv.memory_completed_turns or 0,
            chars=conv.memory_completed_chars or 0,
            tokens=conv.memory_completed_tokens or 0,
        ),
        history_tokens=history_tokens, history_chars=history_chars,
        unarchived_turns=unarchived,
        history_token_budget=settings.MEMORY_HISTORY_TOKEN_BUDGET,
        trigger_ratio=settings.MEMORY_TRIGGER_RATIO,
        unarchived_trigger=settings.MEMORY_TRIGGER_TURNS,
        char_ceiling=MEMORY_TRIGGER_CHAR_CEILING,
    )
    if not trigger.should:
        return False

    epoch = conv.memory_epoch or 0
    base_version = memory.version if memory else 0
    target = turn.turn_seq
    existing_pending = db.scalar(
        select(SummaryJob).where(
            SummaryJob.conversation_id == conv.id,
            SummaryJob.status == SummaryJobStatus.PENDING.value,
        ).order_by(SummaryJob.created_at.desc()).limit(1)
    )
    if existing_pending is not None:
        if target > (existing_pending.target_turn_seq or 0):
            existing_pending.target_turn_seq = target
        existing_pending.epoch = epoch
        existing_pending.base_memory_version = base_version
        existing_pending.idempotency_key = _idempotency_key(conv.id, epoch, base_version, target)
        existing_pending.available_at = None
        return True
    # No pending: create one even if a job is processing (DB allows 1+1).
    db.add(SummaryJob(
        conversation_id=conv.id, epoch=epoch, base_memory_version=base_version,
        target_turn_seq=target,
        idempotency_key=_idempotency_key(conv.id, epoch, base_version, target),
        status=SummaryJobStatus.PENDING.value,
    ))
    logger.info("summary enqueued conversation=%s target_seq=%d epoch=%d base=%d",
                conv.id, target, epoch, base_version)
    return True


def _idempotency_key(conversation_id, epoch: int, base_version: int, target_seq: int) -> str:
    raw = f"{conversation_id}:{epoch}:{base_version}:{target_seq}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


# ---------- status read ----------


def _format_structured(memory: ConversationMemory | None) -> dict | None:
    if memory is None or not isinstance(memory.structured, dict):
        return None
    return redact_summary(memory.structured)


def _latest_failed_job(db: Session, conversation_id) -> SummaryJob | None:
    return db.scalar(
        select(SummaryJob)
        .where(SummaryJob.conversation_id == conversation_id,
               SummaryJob.status == SummaryJobStatus.FAILED.value)
        .order_by(SummaryJob.finished_at.desc().nullslast(), SummaryJob.created_at.desc())
        .limit(1)
    )


def read_memory_status(db: Session, conv: Conversation) -> MemoryStatus:
    """Reflect persisted facts; a fresh failure is not masked forever by an old ready."""
    if not memory_enabled(conv):
        return MemoryStatus(enabled=False, state="disabled",
                            next_turn_seq=conv.next_turn_seq or 0,
                            epoch=conv.memory_epoch or 0)
    memory = latest_memory(db, conv.id)
    covered = memory.covered_turn_seq if memory else 0
    pending, processing, _failed = _job_counts(db, conv.id)
    failed_job = _latest_failed_job(db, conv.id)
    fresh_failed = failed_job is not None and (failed_job.epoch or 0) == (conv.memory_epoch or 0)

    if processing:
        state = "processing"
    elif pending:
        state = "queued"
    elif fresh_failed:
        state = "failed"
    elif memory is not None:
        state = "ready"
    else:
        state = "idle"

    uncovered_t, _, _ = _uncovered_counts(conv, memory)
    uncovered = None if uncovered_t is None else max(0, uncovered_t)
    structured = _format_structured(memory)
    truncated = bool(structured and structured.get("unfinished"))
    keep_recent = settings.MEMORY_KEEP_RECENT_TURNS
    lag = bool(
        processing
        or (fresh_failed and not (pending or processing))
        or (uncovered is not None and uncovered > keep_recent and not pending and not processing)
    )

    return MemoryStatus(
        enabled=True, state=state, epoch=conv.memory_epoch or 0,
        version=memory.version if memory else None, covered_turn_seq=covered,
        next_turn_seq=conv.next_turn_seq or 0, uncovered_completed_turns=uncovered,
        structured=structured, lag=lag, truncated=truncated,
        error_code=failed_job.error_code if failed_job else None,
        error_message=failed_job.error_message if failed_job else None,
        updated_at=memory.updated_at if memory else None,
    )


# ---------- reset ----------


def reset_memory(db: Session, conv: Conversation, expected_version: int) -> MemoryResetResult:
    """Void conversation memory: CAS on published version, revoke jobs, allow rebuild.

    Only callable when no chat answer is generating (the API checks for processing
    ChatTurn / valid active lease under the conversation lock before calling).
    Background summary jobs do not block this. It:
    - CAS-checks expected_version == current published version (or 0 if none);
    - cancels this conversation's pending/processing summary jobs (voiding leases);
    - deletes the published ConversationMemory row (truly voids it, so the old
      summary is no longer injected);
    - bumps memory_epoch; KEEPS the original messages and the incremental
      completed-turn counters so a subsequent complete turn can re-trigger a fresh
      summary from the oldest uncovered turn (rebuild stats).
    """
    memory = latest_memory(db, conv.id)
    current_version = memory.version if memory else 0
    if current_version != expected_version:
        raise ValueError("memory_version_mismatch")
    # void all "work product" jobs (pending/processing/ready): the memory they stand
    # for is being deleted, so a READY job must not linger as if its summary is live.
    db.execute(
        update(SummaryJob)
        .where(SummaryJob.conversation_id == conv.id,
               SummaryJob.status.in_([SummaryJobStatus.PENDING.value,
                                      SummaryJobStatus.PROCESSING.value,
                                      SummaryJobStatus.READY.value]))
        .values(status=SummaryJobStatus.CANCELLED.value, lease_id=None)
    )
    conv.memory_epoch = (conv.memory_epoch or 0) + 1
    if memory is not None:
        db.delete(memory)
    conv.updated_at = _now()
    return MemoryResetResult(epoch=conv.memory_epoch, version=None, covered_turn_seq=0, reset=True)


# ---------- history read (called by chat_stream) ----------


def _complete_turn_seqs(db: Session, conversation_id, covered_seq: int, *,
                        desc: bool, limit: int, below_seq: int | None = None) -> list[int]:
    """Pick a bounded set of *complete-turn candidate* seq identities > covered_seq.

    A ``turn_seq`` is a candidate only when it has exactly one completed user message
    and exactly one completed assistant message (grouped in SQL, so a LIMIT never
    slices a single pair apart, and no message *body* is read just to choose
    identities). This is a *candidate* filter, NOT the full pairing resolve:
    empty-content or reply_to-mismatched "two completed messages" still pass here and
    are only rejected later in Python by :func:`completed_turns_after`. Callers that
    need a genuine count of real pairs must page past such candidates (see
    ``_load_latest_completed_turns``). Returns identities oldest-first (desc=False) or
    newest-first (desc=True); ``below_seq`` (strictly lower seq bound) pages the next
    DESC batch.
    """
    order_col = Message.turn_seq.desc() if desc else Message.turn_seq.asc()
    conditions = [
        Message.conversation_id == conversation_id,
        Message.status == "completed",
        Message.turn_seq.is_not(None),
        Message.turn_seq > covered_seq,
    ]
    if below_seq is not None:
        conditions.append(Message.turn_seq < below_seq)
    rows = db.execute(
        select(Message.turn_seq)
        .where(*conditions)
        .group_by(Message.turn_seq)
        .having(func.count(1).filter(Message.role == "user") == 1,
                func.count(1).filter(Message.role == "assistant") == 1)
        .order_by(order_col)
        .limit(limit)
    ).scalars().all()
    return [int(s) for s in rows]


def _load_turn_messages(db: Session, conversation_id, seqs) -> list[dict]:
    """Load message dicts (ASC by turn_seq) for the given complete-turn seqs."""
    if not seqs:
        return []
    rows = db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id,
               Message.turn_seq.in_(seqs),
               Message.status == "completed")
        .order_by(Message.turn_seq.asc(), Message.role.asc())
        .execution_options(populate_existing=True)
    ).scalars().all()
    return [{
        "seq": m.turn_seq, "role": m.role, "content": m.content,
        "status": m.status, "id": str(m.id),
        "turn_id": str(m.turn_id) if m.turn_id is not None else None,
        "reply_to_id": str(m.reply_to_id) if m.reply_to_id is not None else None,
    } for m in rows]


def _load_latest_completed_turns(db: Session, conversation_id, covered_seq: int,
                                 min_pairs: int):
    """Latest genuine complete-turn pairs (> covered), guaranteed up to ``min_pairs``.

    The SQL identity pick only guarantees "two completed messages"; empty-content or
    reply_to-mismatched candidates can still sneak in and would be rejected by
    ``completed_turns_after``. A fixed slack (e.g. keep_recent + N) therefore cannot
    guarantee the *actual* latest keep_recent pairs if the tail holds more than N such
    degenerate candidates. So instead of trusting a slack, we page the newest identity
    batches (bounded ``below_seq`` walks) newest-first, loading and resolving genuine
    pairs as we accumulate, and stop only once we hold ``min_pairs`` *real* pairs (or
    the post-watermark set is exhausted). ``min_pairs`` is a floor on real pairs, not a
    fetched-row budget; a healthy conversation resolves in one batch.
    """
    chunk = max(min_pairs, 8)
    below = None
    aggregated: list[dict] = []
    scanned = 0
    _HARD_CAP = 8192  # guard against pathologically poisoned "two completed" tails
    while True:
        seqs = _complete_turn_seqs(db, conversation_id, covered_seq,
                                   desc=True, limit=chunk, below_seq=below)
        if not seqs:
            break
        below = min(seqs)  # strictly older than this batch for the next walk
        aggregated.extend(_load_turn_messages(db, conversation_id, seqs))
        scanned += len(seqs)
        if len(cm.completed_turns_after(aggregated, covered_seq)) >= min_pairs:
            break
        if scanned >= _HARD_CAP:
            break
    return cm.completed_turns_after(aggregated, covered_seq)


def _load_oldest_completed_turns(db: Session, conversation_id, covered_seq: int,
                                 limit: int):
    """Oldest contiguous uncovered complete-turn prefix (> covered), for summary."""
    seqs = _complete_turn_seqs(db, conversation_id, covered_seq, desc=False, limit=limit)
    return cm.completed_turns_after(_load_turn_messages(db, conversation_id, seqs),
                                    covered_seq)


def build_chat_history(db: Session, conv: Conversation,
                       question: str) -> tuple[list[dict], bool, bool]:
    """Assemble the chat prompt history: published summary (low-trust Human) + recent turns.

    Returns (history, lag, truncated). history is role/content dicts for
    rag.prepare_answer; the summary goes first as an ordinary user message (NOT a
    system instruction). lag = real lag, truncated = budget clipping. When not
    enabled the caller uses the original HISTORY_MAX_TURNS path.
    """
    if not memory_enabled(conv):
        return [], False, False
    memory = latest_memory(db, conv.id)
    covered = memory.covered_turn_seq if memory else 0
    summary_text = None
    if memory is not None and isinstance(memory.structured, dict):
        summary_text = summarize_into_text(redact_summary(memory.structured))
    # Read the *latest* genuine complete-turn suffix, not an oldest ASC+LIMIT window,
    # so the most-recent keep_recent pairs are never dropped when the conversation has
    # grown large. ``_load_latest_completed_turns`` guarantees the actual latest
    # keep_recent real pairs by paging past empty/malformed candidates, never by a
    # fixed slack.
    keep_recent = settings.MEMORY_KEEP_RECENT_TURNS
    post_turns = _load_latest_completed_turns(db, conv.id, covered, max(keep_recent, 1))
    recent_flat = [m for t in post_turns for m in (t.user, t.assistant)]
    gap = len(post_turns) > keep_recent
    messages, truncated, _ = cm.assemble_recall(
        summary_text=summary_text,
        recent_turns=recent_flat,
        budget_tokens=settings.MEMORY_HISTORY_TOKEN_BUDGET,
        keep_recent=keep_recent,
    )
    return messages, (gap or truncated), truncated


# ---------- worker: claim / lease / publish ----------


def claim_next_summary(db: Session) -> SummaryJob | None:
    """SKIP LOCKED claim a claimable pending job; lock order conv -> job.

    Only pending jobs with available_at NULL or already reached are eligible, and
    only if that conversation has no processing job. It locks the conversation row
    first, then locks/claims the job row (consistent with reset/publish to avoid
    deadlock). On claim it freezes input_snapshot/input_digest, marks PROCESSING,
    and issues a fresh lease. Returns the claimed job or None.
    """
    now = _now()
    candidate_ids = db.scalars(
        select(SummaryJob.id)
        .where(SummaryJob.status == SummaryJobStatus.PENDING.value,
               (SummaryJob.available_at.is_(None)) | (SummaryJob.available_at <= now))
        .order_by(SummaryJob.created_at)
        .limit(50)
    ).all()
    if not candidate_ids:
        db.rollback()
        return None
    for job_id in candidate_ids:
        cid = db.scalar(select(SummaryJob.conversation_id).where(SummaryJob.id == job_id))
        if cid is None:
            continue
        conv = _lock_conversation(db, cid)
        if conv is None or not memory_enabled(conv):
            db.execute(update(SummaryJob).where(SummaryJob.id == job_id)
                       .values(status=SummaryJobStatus.CANCELLED.value))
            db.commit()
            continue
        if db.scalar(select(SummaryJob.id).where(
                SummaryJob.conversation_id == cid,
                SummaryJob.status == SummaryJobStatus.PROCESSING.value,
        ).limit(1)) is not None:
            continue
        job = db.scalar(select(SummaryJob).where(SummaryJob.id == job_id)
                        .with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True))
        if job is None or job.status != SummaryJobStatus.PENDING.value:
            continue
        if (conv.memory_epoch or 0) != (job.epoch or 0):
            job.status = SummaryJobStatus.CANCELLED.value
            db.commit()
            continue
        # 领料时把任务的 base 重挂到**当前同代次**已发布摘要上（conv -> job 锁内）：
        # 入队/回收时登记的 base 可能已过期（另一个 worker 已发布 v+1）。若继续按旧
        # base 冻结快照，模型会反复为一个 CAS 必然拒绝的版本生成摘要并卡在 processing。
        # 重挂只影响本次快照与发布 CAS 的基准，历史摘要内容本身不变；epoch 仍须一致。
        memory = latest_memory(db, conv.id)
        base_version = memory.version if memory is not None else 0
        job.base_memory_version = base_version
        snapshot, included_seq, digest, outcome = _build_snapshot_detailed(
            db, conv, base_version, job.id,
        )
        if snapshot is None:
            if outcome in ("nothing_to_archive", "stale_base"):
                # 重挂后已无可归档内容（pending 目标轮已被刚发布的摘要覆盖、或剩余轮
                # 全部落在 keep_recent 保护带内），或重挂期间又发布了新版本：这不是
                # 错误，任务被新摘要吸收，作废即可，绝不标 failed 也绝不留在 processing。
                job.status = SummaryJobStatus.CANCELLED.value
                job.lease_id = None
                job.error_code = "nothing_to_archive"
                job.finished_at = _now()
                db.commit()
                continue
            job.status = SummaryJobStatus.FAILED.value
            job.error_code = "no_claimable_input"
            job.finished_at = _now()
            db.commit()
            continue
        # 不可变 claim token：快照携带 claim 时的 job_id/lease_id，供发布/失败在
        # 一致的 conv->job 锁内重读并核对原租约、有效期与源 digest，杜绝迟到覆写。
        job.lease_id = uuid.uuid4()
        snapshot["claim_job_id"] = str(job.id)
        snapshot["claim_lease_id"] = str(job.lease_id)
        job.input_snapshot = snapshot
        job.input_digest = digest
        job.target_turn_seq = included_seq
        job.status = SummaryJobStatus.PROCESSING.value
        job.heartbeat_at = _now()
        job.started_at = _now()
        job.attempts = (job.attempts or 0) + 1
        job.available_at = None
        db.commit()
        return job
    db.rollback()
    return None


def _build_snapshot(db: Session, conv: Conversation, base_memory_version: int, job_id) -> tuple[dict | None, int, str]:
    """Compatibility wrapper: ``(snapshot, included_seq, digest)``."""
    snapshot, included_seq, digest, _outcome = _build_snapshot_detailed(
        db, conv, base_memory_version, job_id,
    )
    return snapshot, included_seq, digest


def _build_snapshot_detailed(db: Session, conv: Conversation, base_memory_version: int,
                             job_id) -> tuple[dict | None, int, str, str]:
    """Freeze this claim's input window against ``base_memory_version``.

    Returns ``(snapshot, included_seq, digest, outcome)``. ``outcome`` is
    ``"ok"``, ``"nothing_to_archive"`` (there is genuinely no complete turn left
    to archive after ``covered``, or all of them are inside the keep_recent
    protection band — the task has been absorbed by a newer summary) or
    ``"no_claimable_input"`` (uncovered turns exist but none can be selected,
    e.g. a single turn exceeds the input budget).
    """
    memory = latest_memory(db, conv.id)
    if memory is not None and memory.version != base_memory_version:
        # The caller must rebase first; summarising against another base would be
        # CAS-rejected at publish and could double-count the covered counters.
        return None, 0, "", "stale_base"
    covered = memory.covered_turn_seq if memory is not None else 0
    max_input = settings.MEMORY_MAX_INPUT_TURNS
    keep_recent = settings.MEMORY_KEEP_RECENT_TURNS
    # Oldest contiguous uncovered prefix (bounded identity pick) that the window can
    # select from WITHOUT skipping a middle interval. Merge in the *true* latest tail
    # as well: select_input_window protects the last keep_recent turns from being
    # summarized, and that must be the actual latest complete pairs even when the
    # uncovered tail far exceeds the bounded prefix read (else a huge tail would
    # protect some middle turns instead of the newest ones).
    oldest = _load_oldest_completed_turns(db, conv.id, covered,
                                          2 * max_input + keep_recent + 100)
    latest = _load_latest_completed_turns(db, conv.id, covered, max(keep_recent, 1))
    if latest:
        by_seq = {t.seq: t for t in oldest}
        for t in latest:
            by_seq.setdefault(t.seq, t)
        ordered = [by_seq[s] for s in sorted(by_seq)]
    else:
        ordered = sorted(oldest, key=lambda t: t.seq)
    # Never archiving across a still-processing turn: it may yet complete and, if the
    # summary watermark skipped it, would be lost despite the incremental counters.
    # select_input_window can't see processing holes (they are filtered out of the
    # complete-turn identity), so close the archivable set before the first blocking
    # processing turn (> covered) explicitly.
    blocker = db.scalar(
        select(func.min(Message.turn_seq))
        .where(Message.conversation_id == conv.id,
               Message.turn_seq.is_not(None),
               Message.turn_seq > covered,
               Message.status == "processing")
    )
    if blocker is not None:
        ordered = [t for t in ordered if t.seq < blocker]
    previous = memory.structured if (memory and isinstance(memory.structured, dict)) else None
    # Budget for the new batch must account for old-summary + prompt overhead.
    overhead = 0
    if previous:
        overhead += estimate_message_tokens([{"role": "user", "content": summarize_into_text(redact_summary(previous))}])
    input_budget = max(512, settings.MEMORY_HISTORY_TOKEN_BUDGET - overhead - PROMPT_OVERHEAD_TOKENS)
    selected, _included, _lagging = cm.select_input_window(
        ordered, budget_tokens=input_budget,
        keep_recent=settings.MEMORY_KEEP_RECENT_TURNS,
    )
    selected = selected[:max_input]
    if not selected:
        # 区分"已被新摘要吸收/可归档窗口为空"与"确有可归档轮但第一轮就超预算"：
        # keep_recent 保护带里的轮次本就不该进摘要（select 时被整段丢弃），不算可重试
        # 的输入问题；只有保护带之外仍有轮却因预算一个都放不下，才是 input-too-large。
        archivable = ordered[:-keep_recent] if keep_recent > 0 else ordered
        if not archivable:
            return None, covered, "", "nothing_to_archive"
        return None, covered, "", "no_claimable_input"
    # Watermark = true max seq among the actually-included turns (recompute after the
    # max_input slice so it never crosses un-included turns).
    included_seq = selected[-1].seq
    # Text carries message UUIDs so the model can cite source_message_ids.
    lines = []
    for t in selected:
        lines.append(f"[user {t.seq} message_id={t.user['id']}]\n{t.user['content']}")
        lines.append(f"[assistant {t.seq} message_id={t.assistant['id']}]\n{t.assistant['content']}")
    batch = "\n\n".join(lines)
    message_ids = [str(m["id"]) for t in selected for m in (t.user, t.assistant)]
    # Real fixed-batch metrics in the same口径 as add_completed_turn (raw content
    # only, not the formatting labels), so the covered deduction stays consistent.
    covered_chars = sum(len(t.user["content"]) + len(t.assistant["content"]) for t in selected)
    covered_tokens = sum(estimate_message_tokens([t.user, t.assistant]) for t in selected)
    snapshot = {
        "start_seq": covered + 1 if covered else selected[0].seq,
        "end_seq": included_seq,
        "message_ids": message_ids,
        "text": batch,
        "previous": previous,
        "base_version": base_memory_version,
        "epoch": conv.memory_epoch or 0,
        "conversation_id": str(conv.id),
        "user_id": str(conv.user_id) if conv.user_id is not None else None,
        "turn_count": len(selected),
        "covered_chars": covered_chars,
        "covered_tokens": covered_tokens,
    }
    digest = hashlib.sha256(batch.encode("utf-8")).hexdigest()
    return snapshot, included_seq, digest, "ok"


def heartbeat_job(db: Session, job: SummaryJob) -> bool:
    """Extend the lease only while the job still holds its ORIGINAL claim lease, is
    processing, and is not already expired.

    ``job.lease_id`` is the frozen claim lease captured when the worker claimed; the
    WHERE clause compares the current row's lease to that original token, so a recover
    that voided or re-issued the lease refuses renewal (the latest row's lease is never
    substituted for the original claim). A missing heartbeat (NULL) is treated as stale
    and refused, consistent with publish's NULL refusal. Returns True only when the
    renewal actually landed, so the worker can stop publishing once the lease is lost.
    """
    deadline = _now() - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS)
    res = db.execute(
        update(SummaryJob)
        .where(SummaryJob.id == job.id,
               SummaryJob.lease_id == job.lease_id,
               SummaryJob.status == SummaryJobStatus.PROCESSING.value,
               SummaryJob.heartbeat_at.is_not(None),
               SummaryJob.heartbeat_at >= deadline)
        .values(heartbeat_at=_now())
    )
    db.commit()
    return res.rowcount > 0


def _stale_summary_job_ids(db: Session, deadline: datetime) -> list[uuid.UUID]:
    """Bounded lock-free candidate read for :func:`recover_stale_summary_jobs`.

    Kept as a separate (overridable) step because this read is only a *hint*: the
    row can be renewed between it and the locked re-read. Tests substitute this
    function to replay exactly that window without touching the locked logic.
    """
    return list(db.scalars(
        select(SummaryJob.id)
        .where(SummaryJob.status == SummaryJobStatus.PROCESSING.value,
               (SummaryJob.heartbeat_at.is_(None)) | (SummaryJob.heartbeat_at < deadline))
        .order_by(SummaryJob.heartbeat_at)
        .limit(100)
    ).all())


def recover_stale_summary_jobs(db: Session) -> int:
    """Return stale/expired processing jobs back to claimable, voiding stale leases.

    Each stale processing job is handled under its conversation lock to respect the
    "one pending per conversation" unique constraint: if that conversation already
    has a pending job, the stale one is superseded (cancelled); otherwise it is
    moved back to pending. Either way its lease is voided.

    The stale candidate list is only a *hint* read outside the lock. Waiting for the
    conversation row lock can take long enough for the live owner to renew its
    heartbeat, so the official decision is made on a ``populate_existing`` re-read
    inside the lock: a job that renewed past the deadline (or is no longer
    processing) is left untouched instead of revoking a lease that is still valid.
    """
    deadline = _now() - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS)
    stale_ids = _stale_summary_job_ids(db, deadline)
    recovered = 0
    for job_id in stale_ids:
        cid = db.scalar(select(SummaryJob.conversation_id).where(SummaryJob.id == job_id))
        if cid is None:
            continue
        conv = _lock_conversation(db, cid)
        if conv is None:
            continue
        job = db.get(SummaryJob, job_id, with_for_update=True, populate_existing=True)
        if job is None or job.status != SummaryJobStatus.PROCESSING.value:
            continue
        # 关键：候选是在锁外挑的。等锁期间 owner 可能已续期（heartbeat 越过 deadline）
        # 或该行已被换个租约重新领取；锁内以当前行为准复核，刚续期的任务绝不收回。
        hb = job.heartbeat_at
        if hb is not None:
            if hb.tzinfo is None:
                hb = hb.replace(tzinfo=timezone.utc)
            if hb >= deadline:
                continue
        other_pending = db.scalar(select(SummaryJob.id).where(
            SummaryJob.conversation_id == cid,
            SummaryJob.status == SummaryJobStatus.PENDING.value,
            SummaryJob.id != job_id,
        ).limit(1))
        if other_pending is not None:
            job.status = SummaryJobStatus.CANCELLED.value
            job.lease_id = None
            job.finished_at = _now()
        else:
            job.status = SummaryJobStatus.PENDING.value
            job.lease_id = None
            job.heartbeat_at = None
            # Respect backoff if the job had already been rate-limited.
            job.available_at = job.available_at or None
            recovered += 1
    db.commit()
    return recovered


def _fencing_job(db: Session, job: SummaryJob) -> SummaryJob | None:
    """Re-read the job row under the conversation lock and verify the ORIGINAL claim.

    ``populate_existing`` defeats the caller Session's stale cache so ``live`` reflects
    the latest committed row. The claim token is taken from the *caller's* detached
    ``job`` (the frozen lease + frozen claim_lease_id the worker captured at claim),
    **never re-derived from the latest row**——re-deriving would let a recover-and-reclaim
    on the same row present a fresh internally-consistent token to an old owner and let
    its late publish/fail slip through. We require the current row to still:
      - be the same processing job id,
      - hold a lease, and
      - hold exactly the lease this specific old owner was issued at claim time.
    Any recover (lease -> None) or re-claim (lease -> new value) therefore fails fencing.
    """
    orig_lease = getattr(job, "lease_id", None)
    orig_claim_lease = ((job.input_snapshot or {}).get("claim_lease_id"))
    if orig_lease is None or orig_claim_lease is None:
        return None
    if str(orig_lease) != str(orig_claim_lease):
        # Caller not carrying a self-consistent claim token: refuse defensively.
        return None
    live = db.get(SummaryJob, job.id, with_for_update=True, populate_existing=True)
    if live is None:
        return None
    if live.status != SummaryJobStatus.PROCESSING.value or live.lease_id is None:
        return None
    if live.lease_id != orig_lease:
        # The row now holds a different (or voided-then-new) lease than we claimed.
        return None
    return live


def reload_owned_job(db: Session, job: SummaryJob) -> SummaryJob | None:
    """Lock the conversation, then re-read the job and verify this caller still owns it.

    This is the entry point for an owner that wants to hand work back (shutdown
    release) but has NOT been holding a transaction since the claim. The two steps
    must not be split:

    - the conversation row lock is taken **first** (same conv -> job order as
      claim/publish/reset/recover), and
    - the job is re-read after that lock with ``populate_existing`` and compared
      against the caller's ORIGINAL claim lease (both the detached ``lease_id`` and
      the frozen ``input_snapshot['claim_lease_id']``), plus a still-valid heartbeat.

    Reading the live row *before* taking the lock (as the old shutdown path did) is a
    lost-update race: while we wait for the lock, a recover can void the lease and a
    new claim can install a fresh one, and the stale pre-lock read would still say
    "ours", letting an old process overwrite the new owner's lease/state.
    """
    orig_lease = getattr(job, "lease_id", None)
    orig_claim_lease = (job.input_snapshot or {}).get("claim_lease_id")
    if orig_lease is None or orig_claim_lease is None:
        return None
    if str(orig_lease) != str(orig_claim_lease):
        return None
    conv = _lock_conversation(db, job.conversation_id)
    if conv is None:
        return None
    live = db.get(SummaryJob, job.id, with_for_update=True, populate_existing=True)
    if live is None or live.status != SummaryJobStatus.PROCESSING.value:
        return None
    if live.lease_id is None or live.lease_id != orig_lease:
        return None
    hb = live.heartbeat_at
    if hb is None or _older_than_lease(hb):
        # The lease already lapsed; recover owns the row from here. Never hand back
        # work from a lease we can no longer prove is alive.
        return None
    return live


def _sources_unchanged(db: Session, job: SummaryJob) -> bool:
    """Re-read the claim-frozen source messages and confirm they are still the same.

    (a) every frozen message id still exists, (b) its status is still ``completed``,
    (c) the batch re-rendered from those rows yields the same ``input_digest`` that the
    claim froze. This refuses to land a summary over sources whose content or
    completion state changed after the claim (source-changed refusal). No frozen digest
    means nothing was verifiable -> returns True (no claim token to compare).
    """
    snapshot = job.input_snapshot or {}
    digest = job.input_digest
    if not digest:
        return True
    raw_ids = snapshot.get("message_ids") or []
    ids = [uuid.UUID(str(x)) for x in raw_ids]
    if not ids:
        return False
    rows = db.scalars(select(Message).where(Message.id.in_(ids))
                      .execution_options(populate_existing=True)).all()
    by_id = {str(r.id): r for r in rows}
    if len(by_id) != len(ids):
        return False  # a source message was deleted since the claim froze the snapshot
    ordered = [by_id[str(i)] for i in ids]
    if any(r.status != "completed" for r in ordered):
        return False  # a source message is no longer completed
    grouped: dict[int, list] = {}
    for r in ordered:
        grouped.setdefault(r.turn_seq, []).append(r)
    lines: list[str] = []
    # 必须与 _build_snapshot 的逐轮渲染一致：每轮先 user 后 assistant（按 role 排序
    # 会让 "assistant" < "user" 而排反，导致 digest 永远不等）。用 user-first 显式排序。
    for seq in sorted(grouped):
        for r in sorted(grouped[seq], key=lambda m: (m.role != "user", m.role)):
            lines.append(f"[{r.role} {seq} message_id={r.id}]\n{r.content}")
    batch = "\n\n".join(lines)
    return hashlib.sha256(batch.encode("utf-8")).hexdigest() == digest


def _resolve_claim(db: Session, conv: Conversation, live: SummaryJob, *,
                   error_code: str, error_message: str,
                   retryable: bool, immediate: bool = False) -> str:
    """Resolve a job we still own after its own publish/failure was refused.

    MUST only be called while holding the conversation row lock and a ``live`` job
    re-read by :func:`_fencing_job` (i.e. the row still carries this owner's
    original claim lease). Under those locks it either:

    - retires the job (terminal FAILED after ``MEMORY_MAX_ATTEMPTS``, or CANCELLED
      when it is not retryable / another pending already absorbed the work), or
    - returns it to PENDING with the lease voided and a bounded backoff (or
      immediately when ``immediate``). Its stale ``base_memory_version`` is
      harmless: the next claim rebases onto the then-current published memory.

    This is what keeps a refused publish from stranding the row in ``processing``
    forever while still never writing over another owner's lease: when the
    claim token no longer matches, callers do not reach this helper at all.
    """
    now = _now()
    if not retryable:
        live.status = SummaryJobStatus.CANCELLED.value
        live.lease_id = None
        live.error_code = error_code[:64] if error_code else None
        live.error_message = (error_message or "")[:2000]
        live.finished_at = now
        db.commit()
        return live.status
    attempts = int(live.attempts or 0)
    if attempts >= settings.MEMORY_MAX_ATTEMPTS:
        live.status = SummaryJobStatus.FAILED.value
        live.lease_id = None
        live.error_code = error_code[:64] if error_code else None
        live.error_message = (error_message or "")[:2000]
        live.finished_at = now
        db.commit()
        return live.status
    other_pending = db.scalar(select(SummaryJob.id).where(
        SummaryJob.conversation_id == conv.id,
        SummaryJob.status == SummaryJobStatus.PENDING.value,
        SummaryJob.id != live.id,
    ).limit(1))
    if other_pending is not None:
        # The pending job is the vehicle for whatever is still uncovered; this
        # claim's work is absorbed there instead of becoming a second pending.
        live.status = SummaryJobStatus.CANCELLED.value
        live.lease_id = None
        live.error_code = error_code[:64] if error_code else None
        live.error_message = (error_message or "")[:2000]
        live.finished_at = now
        db.commit()
        return live.status
    backoff = 0 if immediate else min(300, (2 ** max(0, attempts - 1)) * 10)
    live.status = SummaryJobStatus.PENDING.value
    live.lease_id = None
    live.heartbeat_at = None
    live.available_at = (now + timedelta(seconds=backoff)) if backoff else None
    live.error_code = error_code[:64] if error_code else None
    live.error_message = (error_message or "")[:2000]
    db.commit()
    return live.status


def publish_summary(db: Session, job: SummaryJob, parsed: dict) -> ConversationMemory | None:
    """CAS publish: epoch + base_version + live ownershipledge must match to land.

    ``job`` is only used for ``id``/``conversation_id``; the authoritative row is
    re-read under the conversation lock with ``populate_existing`` so a stale Session
    cache cannot falsely fence. Returns None (do not publish) when the conversation is
    gone, the epoch moved, a newer memory was published, the job is no longer processing
    / no longer owns the original claim lease / its heartbeat expired, any source is not
    in the trusted union (new window ids + previous summary sources), or the source
    messages changed since the claim. ``parsed`` is structurally validated up front by
    memory_prompt and redacted at the commit boundary.

    A refusal NEVER strands the job in ``processing`` while this caller still holds
    the original lease: see :func:`_resolve_claim` (requeue with backoff, or retire).
    """
    conv = _lock_conversation(db, job.conversation_id)
    if conv is None:
        return None
    live = _fencing_job(db, job)
    if live is None:
        # Not our claim any more (recovered/re-claimed/reset): the current owner or
        # recover is responsible for this row; never mutate another lease.
        return None
    if (conv.memory_epoch or 0) != (live.epoch or 0):
        # The whole memory generation was reset; this result can never land and
        # retrying it is pointless.
        _resolve_claim(db, conv, live, error_code="epoch_changed",
                       error_message="摘要代次已重置，结果作废", retryable=False)
        return None
    memory = latest_memory(db, live.conversation_id)
    snapshot = live.input_snapshot or {}
    current_version = memory.version if memory is not None else 0
    if memory is not None and memory.version != (live.base_memory_version or 0):
        # A newer summary landed while we generated, so this result can never land.
        # Requeue immediately: the next claim rebases onto that version and builds a
        # fresh snapshot from the still-uncovered turns (or retires itself when the
        # new summary already absorbed the target). Leaving the row processing would
        # keep it model-called forever against a CAS that must reject.
        _resolve_claim(db, conv, live, error_code="base_version_moved",
                       error_message="已有更新的摘要发布，任务重新排期",
                       retryable=True, immediate=True)
        return None
    # The base the snapshot was built against must still be the live one. The row's
    # ``base_memory_version`` alone is not enough: claim rebases it, so the frozen
    # snapshot's ``base_version`` is the authoritative input identity. A mismatch
    # means the input this result was generated from is no longer the head.
    if int(snapshot.get("base_version") or 0) != current_version:
        _resolve_claim(db, conv, live, error_code="base_version_moved",
                       error_message="摘要输入基准已变化，任务重新排期",
                       retryable=True, immediate=True)
        return None
    # Expiry guard: an expired-but-not-yet-recovered lease must refuse the write. Even
    # if the row still carries the original lease and is processing, a stale owner
    # that failed to heartbeat past the deadline cannot land its result. It is safe to
    # put it back in the claimable set here (we hold conv+job locks and still hold the
    # exact claim lease, so no other owner exists); leaving it processing would strand
    # it until the next recover scan.
    hb = live.heartbeat_at
    if hb is None or _older_than_lease(hb):
        _resolve_claim(db, conv, live, error_code="lease_expired",
                       error_message="摘要租约已过期，任务重新排期", retryable=True)
        return None
    # Real-source check: the messages frozen at claim must be unchanged and completed.
    if not _sources_unchanged(db, live):
        logger.warning("summary publish source changed conversation=%s job=%s",
                       live.conversation_id, live.id)
        _resolve_claim(db, conv, live, error_code="source_changed",
                       error_message="摘要来源消息在领取后发生变化", retryable=True)
        return None
    snapshot = live.input_snapshot or {}
    message_ids = snapshot.get("message_ids") or []
    previous_sources = _collect_sources(snapshot.get("previous"))
    _used, invalid = validate_sources(parsed, allowed_message_ids=list(message_ids),
                                      previous_summary_sources=previous_sources)
    if invalid:
        logger.warning("summary publish source validation failed conversation=%s invalid=%d",
                       live.conversation_id, len(invalid))
        _resolve_claim(db, conv, live, error_code="invalid_output",
                       error_message="摘要输出校验失败", retryable=True)
        return None
    structured = redact_summary(parsed)
    end_seq = snapshot.get("end_seq") or 0
    covered_turns = int(snapshot.get("turn_count") or 0)
    covered_chars = int(snapshot.get("covered_chars") or 0)
    covered_tokens = int(snapshot.get("covered_tokens") or 0)
    # The counters are cumulative deductions: start from the counters of the base
    # this snapshot was built against (== the live memory, enforced by the CAS above)
    # and add this batch. A missing memory means base 0, so nothing is carried over.
    base_turns = int(memory.covered_turns or 0) if memory is not None else 0
    base_chars = int(memory.covered_chars or 0) if memory is not None else 0
    base_tokens = int(memory.covered_tokens or 0) if memory is not None else 0
    # A claim rebased onto this base must publish as the next version of the same
    # base; a claim that somehow carries an older base is stale and is refused.
    if (memory.version if memory else 0) != int(snapshot.get("base_version") or 0):
        _resolve_claim(db, conv, live, error_code="base_version_moved",
                       error_message="快照基准版本与当前摘要不符，任务重新排期",
                       retryable=True, immediate=True)
        return None
    new_version = (memory.version if memory else 0) + 1
    now = _now()
    if memory is None:
        memory = ConversationMemory(conversation_id=live.conversation_id, version=new_version)
    memory.version = new_version
    memory.epoch = live.epoch or 0
    memory.structured = structured
    memory.source_range = {
        "start_seq": snapshot.get("start_seq", 1),
        "end_seq": end_seq,
        "message_ids": message_ids,
    }
    memory.source_digest = live.input_digest
    memory.prompt_version = "memory_prompt_v1"
    memory.model_fingerprint = _model_fingerprint()
    memory.estimated_input_tokens = estimate_message_tokens([{"role": "user", "content": snapshot.get("text", "")}])
    memory.estimated_output_tokens = 0
    memory.actual_input_tokens = None
    memory.actual_output_tokens = None
    memory.covered_turn_seq = end_seq
    memory.covered_turns = base_turns + covered_turns
    memory.covered_chars = base_chars + covered_chars
    memory.covered_tokens = base_tokens + covered_tokens
    if memory.id is None:
        db.add(memory)
    live.status = SummaryJobStatus.READY.value
    live.finished_at = now
    db.commit()
    logger.info("summary published conversation=%s version=%d covered_seq=%d epoch=%d",
                live.conversation_id, new_version, end_seq, conv.memory_epoch or 0)
    return memory


def _collect_sources(structured) -> set[str]:
    if not isinstance(structured, dict):
        return set()
    out: set[str] = set()
    for section in ITEMS_SECTIONS:
        for item in structured.get(section, []) or []:
            if isinstance(item, dict):
                for src in item.get("source_message_ids", []) or []:
                    out.add(str(src))
    return out


def _model_fingerprint() -> str | None:
    try:
        return f"{settings.LLM_PROVIDER}:{settings.LLM_MODEL}"
    except Exception:
        return None


def mark_failed(db: Session, job: SummaryJob, error_code: str, error_message: str) -> None:
    """Fail a summary job: no publish, bounded retry backoff, then terminal failed.

    ``job`` is only used for ``id``/``conversation_id``; the authoritative row is
    re-read under the conversation lock (``_fencing_job``) so a stale owner that no
    longer holds the original claim lease cannot write a terminal state over a newer
    claim. Under the conversation lock, a retryable failure moves the job back to
    pending with a future available_at and a voided lease; if that conversation already
    has another pending job, this one is superseded (cancelled) to respect the
    one-pending unique constraint. After MEMORY_MAX_ATTEMPTS it becomes terminal
    FAILED and stays out of the claim set.
    """
    conv = _lock_conversation(db, job.conversation_id)
    if conv is None:
        return
    live = _fencing_job(db, job)
    if live is None:
        return
    _resolve_claim(db, conv, live, error_code=error_code, error_message=error_message,
                   retryable=True)
