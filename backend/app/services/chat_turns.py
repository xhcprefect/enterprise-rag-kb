"""持久化问答认领、幂等重放和租约隔离；网络期间不持有数据库行锁。"""
import hashlib
import json
import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import ChatTurn, Conversation, KnowledgeBase, Message, User
from app.services import document_scopes, memory_service, project_chat_scope

logger = logging.getLogger(__name__)


class ChatLeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Reservation:
    turn_id: uuid.UUID
    request_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    user_id: uuid.UUID
    replay_answer: str | None = None
    replay_citations: list[dict] = field(default_factory=list)
    replay_latency_ms: int = 0
    replay_trace: dict = field(default_factory=dict)
    assistant_message_id: uuid.UUID | None = None
    document_scope: list[dict] = field(default_factory=list)
    scope_revision: int = 0
    # 项目会话绑定的快照 id（当次请求绑定并回放用；追加在尾部，不破坏旧 positional 构造）。
    project_snapshot_id: uuid.UUID | None = None


def _now():
    return datetime.now(timezone.utc)


def payload_hash(question: str, mode: str, kb_id, top_k: int, *, document_ids=None,
                 scope_revision: int | None = None, project_snapshot_id=None) -> str:
    values = [question, mode, str(kb_id) if kb_id else None, top_k]
    # 旧调用保留原摘要；只有显式提供的范围参数参与幂等身份。项目请求额外纳入项目快照
    # sid 与 scope_revision；普通上传 / 普通 chat 未显式给 sid 时摘要与旧版本逐字节一致。
    scope_request = {}
    if document_ids is not None:
        scope_request["document_ids"] = sorted(str(value) for value in document_scopes.scope_ids(document_ids))
    if project_snapshot_id is not None:
        scope_request["project_snapshot_id"] = str(project_snapshot_id)
    if scope_revision is not None:
        scope_request["scope_revision"] = scope_revision
    if scope_request:
        values.append(scope_request)
    data = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _request_lock(user_id: uuid.UUID, request_id: uuid.UUID) -> int:
    return int.from_bytes(hashlib.sha256(f"chat:{user_id}:{request_id}".encode()).digest()[:8], "big", signed=True)


def _expire_active(db: Session, conv: Conversation) -> None:
    if conv.active_turn_id is not None:
        db.execute(update(ChatTurn).where(ChatTurn.id == conv.active_turn_id, ChatTurn.status == "processing").values(status="failed"))
        db.execute(update(Message).where(Message.turn_id == conv.active_turn_id, Message.status == "processing").values(status="failed"))
    conv.active_turn_id, conv.lease_expires_at = None, None


def reclaim_expired(db: Session) -> int:
    """回收租约已过期的在途问答；返回处理的会话数。

    正常路径由 ``_expire_active`` 在**同一会话**再次认领时清理。但若进程在生成中途
    被杀（部署、OOM、竞价实例回收），那轮会永远停在 processing：没有并发续期线程，
    也就不会有人再碰这个会话，``/chat/conversations`` 会一直显示「回答中」。
    这里在启动时按租约全局兜底一次，与 ``_expire_active`` 的判定口径保持一致。
    """
    now = _now()
    conv_ids = db.scalars(select(Conversation.id).where(
        Conversation.active_turn_id.is_not(None),
        or_(Conversation.lease_expires_at.is_(None), Conversation.lease_expires_at <= now),
    )).all()
    if not conv_ids:
        return 0
    # 先记下要回收的轮次，后面只动这些行：不能顺带改到该会话里更早失败的历史记录。
    turn_ids = db.scalars(select(ChatTurn.id).where(
        ChatTurn.conversation_id.in_(conv_ids), ChatTurn.status == "processing",
    )).all()
    messages = db.execute(update(Message).where(
        Message.turn_id.in_(turn_ids), Message.status == "processing",
    ).values(status="failed")).rowcount if turn_ids else 0
    turns = db.execute(update(ChatTurn).where(
        ChatTurn.id.in_(turn_ids), ChatTurn.status == "processing",
    ).values(status="failed")).rowcount if turn_ids else 0
    # 整行批量更新，避免身份映射里留着旧的 active_turn_id。
    db.execute(update(Conversation).where(Conversation.id.in_(conv_ids))
               .values(active_turn_id=None, lease_expires_at=None))
    db.commit()
    logger.warning("回收了 %d 个租约过期的在途会话（问答 %d 条、消息 %d 条标记为失败）",
                   len(conv_ids), turns, messages)
    return len(conv_ids)


def claim_turn(db: Session, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None,
               kb_id: uuid.UUID | None, mode: str, question: str, top_k: int,
               request_id: uuid.UUID | None = None, document_ids=None,
               scope_revision: int | None = None, reset_scope: bool = False,
               project_snapshot_id: uuid.UUID | None = None) -> Reservation:
    """只在短事务内认领；同键重放不新建会话/消息，也不再次调用模型。

    项目会话（kind=project 的 KB）绑定项目快照：新会话无显式 sid 选该源 active ready，
    显式 sid 同源同 KB 校验；已有会话保持已绑 sid 不随 active 漂移。mode=chat 任何项目
    范围均拒绝且从不项目检索；项目与 document_ids 任何显式互斥。
    """
    if mode != "rag" and (document_ids is not None or scope_revision is not None
                          or project_snapshot_id is not None):
        raise HTTPException(400, "普通对话不支持文档范围")
    key = request_id or uuid.uuid4()
    digest = payload_hash(question, mode, kb_id, top_k, document_ids=document_ids,
                          scope_revision=scope_revision, project_snapshot_id=project_snapshot_id)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _request_lock(user_id, key)})
    previous = db.scalar(select(ChatTurn).where(ChatTurn.user_id == user_id, ChatTurn.request_id == key))
    if previous is not None:
        if previous.conversation_id is None:
            # 删除会话后保留去重记录：迟到的重试不能重新建会话或再次调用模型。
            raise HTTPException(410, "原会话已删除，该 request_id 不可复用，请使用新的 request_id")
        if previous.payload_hash != digest or (conversation_id and previous.conversation_id != conversation_id):
            raise HTTPException(409, "同一 request_id 不能用于不同的问题、参数或会话")
        if previous.status != "completed":
            message = "该请求仍在处理中，请等待或刷新会话" if previous.status == "processing" else "该请求已结束且未完成，请使用新的 request_id 重试"
            raise HTTPException(409, message)
        answer = db.scalar(select(Message).where(Message.turn_id == previous.id, Message.role == "assistant", Message.status == "completed"))
        if answer is None:
            raise HTTPException(409, "原请求结果已不可用，请使用新的 request_id")
        # 项目请求重放：固定已存 turn 的 sid（不借会话当前 scope/active），并复核当前授权
        # （ACL/revoked/ready）——只读重放也需当前权限，源撤销/失去 ACL 不得继续读取。
        if previous.project_snapshot_id is not None:
            project_chat_scope.replay_project_check(db, user_id, previous.project_snapshot_id)
        result = Reservation(previous.id, key, previous.conversation_id, previous.user_message_id, user_id,
                             answer.content, answer.citations or [], answer.latency_ms, previous.trace or {}, answer.id,
                             project_snapshot_id=previous.project_snapshot_id)
        db.commit()
        return result

    if conversation_id is None:
        conv = Conversation(id=uuid.uuid4(), user_id=user_id, kb_id=kb_id, mode=mode,
                            title=question.strip()[:30] or "新会话", document_scope=[], scope_revision=0)
        db.add(conv)
        db.flush()
    else:
        conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                         .with_for_update().execution_options(populate_existing=True))
        if conv is None or conv.user_id != user_id:
            raise HTTPException(404, "会话不存在")
        if conv.mode != mode or conv.kb_id != kb_id:
            raise HTTPException(409, "会话配置已改变，请刷新后重试")
    now = _now()
    if conv.active_turn_id is not None:
        if conv.lease_expires_at and conv.lease_expires_at > now:
            raise HTTPException(409, "该会话已有回答正在生成，请等待完成或停止后重试")
        _expire_active(db, conv)

    # 项目快照绑定解析（仅 kind=project 的 KB 使用；mode=chat 任何项目范围已在入口拒绝）。
    project_sid = None
    kb_kind_project = project_chat_scope.is_project_kb(db, conv.kb_id)
    if mode == "rag" and (project_snapshot_id is not None or kb_kind_project):
        project_sid = project_chat_scope.resolve_claim_project(
            db, conv, user_id, document_ids, project_snapshot_id,
            new_conv=conversation_id is None)
    elif project_snapshot_id is not None:
        # 普通上传库（或未绑定项目的会话）不得塞入项目快照。
        raise HTTPException(400, "非项目知识库不能绑定项目快照")

    current_scope = getattr(conv, "document_scope", [])
    current_revision = getattr(conv, "scope_revision", None) or 0
    if scope_revision is not None and scope_revision != current_revision:
        raise HTTPException(409, "文档范围已在其他页面改变，请刷新会话后重试")
    if project_sid is None:
        if document_ids is not None:
            if conversation_id is not None:
                raise HTTPException(400, "已有会话请先通过范围选择器更新文档范围")
            current_scope = document_scopes.resolve_scope(db, kb_id, document_ids, lock=True)
            conv.document_scope = current_scope
            current_revision = conv.scope_revision = int(bool(current_scope))
        elif not reset_scope and (current_scope or not isinstance(current_scope, list)):
            current_scope = document_scopes.validate_scope(db, kb_id, current_scope, lock=True)
    else:
        # 项目会话不维护文档范围（恒为 []）。
        current_scope, conv.document_scope = [], []

    # 新会话/已有项目会话都落会话绑定（已有项目会话值相同，幂等）；turn 记当次 sid 供重放。
    if project_sid is not None:
        conv.project_snapshot_id = project_sid
    identifier, message_id = uuid.uuid4(), uuid.uuid4()
    # 会话内稳定轮次序号：普通对话（启用记忆时）在会话行锁下分配，user/assistant 同一 seq，
    # request_id 重放不重复分配，失败/取消轮留空号不重排。仅分配序号，不触发摘要。
    # 旧完整历史一次性统计接入（幂等，holder 为 memory 服务）：F 的补序不初始化
    # memory_completed_* 计数，这里在分配新轮前（会话行锁内）把 legacy 完整轮的真实
    # 计数/字数/token 播种一次；纯新会话无 legacy 轮则空操作，不会对在途轮重复计数。
    if memory_service.memory_enabled(conv):
        memory_service.reconcile_legacy_stats(db, conv)
    turn_seq = None
    if memory_service.memory_enabled(conv):
        turn_seq = (conv.next_turn_seq or 0) + 1
        conv.next_turn_seq = turn_seq
    db.add(ChatTurn(id=identifier, request_id=key, user_id=user_id, conversation_id=conv.id,
                    user_message_id=message_id, payload_hash=digest, status="processing", heartbeat_at=now,
                    turn_seq=turn_seq, project_snapshot_id=project_sid))
    db.flush()
    db.add(Message(id=message_id, conversation_id=conv.id, turn_id=identifier, role="user",
                   content=question, status="processing", turn_seq=turn_seq))
    conv.active_turn_id = identifier
    conv.lease_expires_at = now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)
    conv.updated_at = now
    db.commit()
    return Reservation(identifier, key, conv.id, message_id, user_id,
                       document_scope=current_scope, scope_revision=current_revision,
                       project_snapshot_id=project_sid)


def set_document_scope(db: Session, conversation_id: uuid.UUID, document_ids,
                       expected_revision: int | None = None) -> tuple[list[dict], int]:
    """调用方先检查会话和知识库权限；与认领/完成共用会话行锁。"""
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        raise HTTPException(404, "会话不存在")
    if conv.mode != "rag":
        raise HTTPException(400, "普通对话不支持文档范围")
    if conv.active_turn_id is not None:
        if conv.lease_expires_at and conv.lease_expires_at > _now():
            raise HTTPException(409, "回答正在生成，请停止或等待完成后再调整文档范围")
        _expire_active(db, conv)
    revision = conv.scope_revision or 0
    if expected_revision is not None and revision != expected_revision:
        raise HTTPException(409, "文档范围已在其他页面改变，请刷新会话后重试")
    selected = document_scopes.resolve_scope(db, conv.kb_id, document_ids, lock=True)
    if selected != (conv.document_scope or []):
        conv.document_scope, conv.scope_revision, conv.updated_at = selected, revision + 1, _now()
        revision += 1
    db.commit()
    return selected, revision


def set_project_snapshot(db: Session, conversation_id: uuid.UUID, user: User,
                         snapshot_id: uuid.UUID,
                         expected_revision: int) -> tuple[uuid.UUID, int]:
    """CAS 切换会话绑定的项目快照。

    仅本人、仅项目知识库会话、仅同源同 KB；有效生成租约或 stale revision → 409；
    无变化相同快照幂等，不递增版本。
    """
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        raise HTTPException(404, "会话不存在")
    if conv.mode != "rag":
        raise HTTPException(400, "普通对话不支持项目快照")
    if conv.user_id != user.id:
        raise HTTPException(403, "只能切换自己的会话项目快照")
    if not project_chat_scope.is_project_kb(db, conv.kb_id):
        raise HTTPException(400, "仅项目知识库会话支持项目快照")
    if conv.active_turn_id is not None:
        if conv.lease_expires_at and conv.lease_expires_at > _now():
            raise HTTPException(409, "回答正在生成，请停止或等待完成后再切换项目快照")
        _expire_active(db, conv)
    revision = conv.scope_revision or 0
    if expected_revision != revision:
        raise HTTPException(409, "项目快照版本已在其他页面改变，请刷新后重试")
    # 同源同 KB + ACL/revoked/ready，DB 权威。
    try:
        project_chat_scope.resolve_new_project_snapshot(db, user, conv.kb_id, snapshot_id)
    except HTTPException:
        raise
    if conv.project_snapshot_id != snapshot_id:
        conv.project_snapshot_id, conv.scope_revision, conv.updated_at = snapshot_id, revision + 1, _now()
        revision += 1
    db.commit()
    return snapshot_id, revision


def _active_locked(db: Session, reservation: Reservation):
    conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None or conv.active_turn_id != reservation.turn_id or not conv.lease_expires_at or conv.lease_expires_at <= _now():
        raise ChatLeaseLost("该回答已取消或处理租约已失效，请刷新会话后重试")
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == reservation.turn_id)
                     .with_for_update().execution_options(populate_existing=True))
    if turn is None or turn.status != "processing":
        raise ChatLeaseLost("该回答已结束，请刷新会话")
    return conv, turn


def complete_turn(db: Session, reservation: Reservation, answer: str, citations: list[dict],
                  latency_ms: int, trace: dict, *, scope_update: list[dict] | None = None) -> uuid.UUID:
    conv, turn = _active_locked(db, reservation)
    if (getattr(conv, "scope_revision", None) or 0) != reservation.scope_revision:
        raise ChatLeaseLost("文档范围已改变，请刷新会话后重试")
    # 项目会话：新鲜复核会话/轮次 sid 未漂移 + 项目仍 ready / 未 revoke / 有 ACL，绝不把
    # 失效项目结果写成成功。
    if reservation.project_snapshot_id is not None:
        try:
            project_chat_scope.verify_active_project(db, conv, reservation)
        except project_chat_scope.ProjectBindingLost as exc:
            raise ChatLeaseLost(str(exc)) from exc
    selected = reservation.document_scope if scope_update is None else scope_update
    if selected:
        # 生成期间发生删除/取消/重建时，禁止将旧代次结果写成成功回答。
        selected = document_scopes.validate_scope(db, conv.kb_id, selected, lock=True)
    if scope_update is not None and selected != (getattr(conv, "document_scope", None) or []):
        conv.document_scope = selected
        conv.scope_revision = reservation.scope_revision + 1
    identifier = uuid.uuid4()
    # 真实 ChatTurn 总有 turn_seq（普通对话启用记忆时分配，否则 None）；用 getattr 兜底
    # 兼容内存 mock / 旧测试中的简化 turn，保持真实生产行为不变。
    turn_seq = getattr(turn, "turn_seq", None)
    assistant_msg = Message(id=identifier, conversation_id=conv.id, turn_id=turn.id,
                            reply_to_id=reservation.user_message_id, status="completed", role="assistant",
                            content=answer, citations=citations or None, latency_ms=latency_ms,
                            turn_seq=turn_seq)
    db.add(assistant_msg)
    user_msg = db.get(Message, reservation.user_message_id)
    db.execute(update(Message).where(Message.id == reservation.user_message_id,
                                     Message.turn_id == turn.id).values(status="completed",
                                                                        turn_seq=turn_seq))
    turn.status, turn.trace = "completed", trace
    conv.active_turn_id, conv.lease_expires_at, conv.updated_at = None, None, _now()
    # 记忆：仅普通对话且启用时，在成功完成事务内做增量计数 + 触发入队（不自行 commit，
    # 不持网络等待，无正文扫描）；失败/取消轮不计数。重放不会再次进到这里。
    if memory_service.memory_enabled(conv) and turn_seq is not None:
        if user_msg is not None:
            user_msg.status = "completed"
        memory_service.add_completed_turn(db, conv, turn, user_msg, assistant_msg)
        memory_service.maybe_enqueue_summary(db, conv, turn)
    db.commit()
    return identifier


def _terminate(db: Session, turn_id: uuid.UUID, conversation_id: uuid.UUID, status: str, *,
               error_code: str | None = None, error_message: str | None = None) -> bool:
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None or conv.active_turn_id != turn_id:
        return False
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == turn_id)
                     .with_for_update().execution_options(populate_existing=True))
    if turn is None or turn.status != "processing":
        return False
    turn.status = status
    if error_code:
        turn.trace = {"error_code": error_code, "error_message": (error_message or "请求已结束")[:500]}
    db.execute(update(Message).where(Message.turn_id == turn_id, Message.status == "processing").values(status=status))
    conv.active_turn_id, conv.lease_expires_at, conv.updated_at = None, None, _now()
    return True


def end_turn(reservation: Reservation, status: str = "cancelled", *,
             error_code: str | None = None, error_message: str | None = None) -> None:
    """完成后的清理是空操作；断连、异常和旧工作线程都不能覆盖更新的认领。"""
    try:
        with SessionLocal() as db:
            _terminate(db, reservation.turn_id, reservation.conversation_id, status,
                       error_code=error_code, error_message=error_message)
            db.commit()
    except Exception as exc:
        logger.warning("问答状态清理失败，将由租约到期恢复: %s", type(exc).__name__)


def clear_conversation_requests(db: Session, conversation_ids) -> None:
    """调用方锁住会话后，在删除事务内清除诊断、终止活跃请求，保留去重身份。"""
    ids = tuple(conversation_ids)
    if not ids:
        return
    db.execute(update(ChatTurn).where(ChatTurn.conversation_id.in_(ids)).values(trace=None))
    db.execute(update(ChatTurn).where(ChatTurn.conversation_id.in_(ids),
                                     ChatTurn.status == "processing").values(status="cancelled"))


def cancel_request(db: Session, request_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    turn = db.scalar(select(ChatTurn).where(ChatTurn.request_id == request_id, ChatTurn.user_id == user_id))
    if turn is None:
        raise HTTPException(404, "请求不存在")
    changed = _terminate(db, turn.id, turn.conversation_id, "cancelled")
    db.commit()
    return changed


def expire_request(db: Session, turn: ChatTurn) -> None:
    """读取请求状态时回收崩溃遗留的 processing，不影响同会话的新租约。"""
    if turn.status != "processing" or turn.conversation_id is None:
        return
    conv = db.scalar(select(Conversation).where(Conversation.id == turn.conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        return
    if conv.active_turn_id == turn.id and conv.lease_expires_at and conv.lease_expires_at > _now():
        db.commit()
        return
    active = conv.active_turn_id == turn.id
    current = db.scalar(select(ChatTurn).where(ChatTurn.id == turn.id)
                        .with_for_update().execution_options(populate_existing=True))
    if current is not None and current.status == "processing":
        current.status = "failed"
        current.trace = {"error_code": "lease_lost", "error_message": "处理租约已失效，请使用新请求重试"}
        db.execute(update(Message).where(Message.turn_id == turn.id, Message.status == "processing")
                   .values(status="failed"))
        if active:
            conv.active_turn_id, conv.lease_expires_at = None, None
    db.commit()


def check_lease(reservation: Reservation, renew: bool = False) -> bool:
    """短事务检查/续期；跨进程取消以数据库为准，不为每路问答创建常驻线程。"""
    with SessionLocal() as db:
        now = _now()
        valid = db.scalar(select(ChatTurn.id).join(
            Conversation, Conversation.id == ChatTurn.conversation_id,
        ).join(User, User.id == ChatTurn.user_id).outerjoin(
            KnowledgeBase, KnowledgeBase.id == Conversation.kb_id,
        ).where(
            Conversation.id == reservation.conversation_id,
            Conversation.active_turn_id == reservation.turn_id,
            Conversation.lease_expires_at > now,
            ChatTurn.id == reservation.turn_id, ChatTurn.status == "processing",
            User.id == reservation.user_id, User.is_active.is_(True),
            or_(Conversation.mode == "chat", User.role == "admin",
                KnowledgeBase.is_public.is_(True), KnowledgeBase.owner_id == reservation.user_id),
        ))
        if valid is None:
            return False
        # 项目会话短事务复核：会话/轮次 sid 未漂移 + 项目 ACL/revoked/ready，失效即判租约失去。
        if reservation.project_snapshot_id is not None:
            conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id))
            if conv is None:
                return False
            try:
                project_chat_scope.verify_active_project(db, conv, reservation)
            except project_chat_scope.ProjectBindingLost:
                return False
        if renew:
            updated = db.execute(update(Conversation).where(
                Conversation.id == reservation.conversation_id,
                Conversation.active_turn_id == reservation.turn_id,
                Conversation.lease_expires_at > now,
            ).values(lease_expires_at=now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)))
            turn = db.execute(update(ChatTurn).where(
                ChatTurn.id == reservation.turn_id, ChatTurn.status == "processing",
            ).values(heartbeat_at=now))
            if not updated.rowcount or not turn.rowcount:
                return False  # context manager rolls back a partial renewal
            db.commit()
        return True


@contextmanager
def keepalive(reservation: Reservation):
    stopped, lost = threading.Event(), threading.Event()
    interval = max(1.0, min(30.0, settings.CHAT_LEASE_SECONDS / 3))

    def renew():
        while not stopped.wait(interval):
            try:
                with SessionLocal() as db:
                    now = _now()
                    updated = db.execute(update(Conversation).where(
                        Conversation.id == reservation.conversation_id,
                        Conversation.active_turn_id == reservation.turn_id,
                        Conversation.lease_expires_at > now,
                    ).values(lease_expires_at=now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)))
                    if not updated.rowcount:
                        lost.set()
                        return
                    turn = db.execute(update(ChatTurn).where(ChatTurn.id == reservation.turn_id,
                                                             ChatTurn.status == "processing").values(heartbeat_at=now))
                    if not turn.rowcount:
                        lost.set()
                        return
                    db.commit()
            except Exception as exc:
                logger.warning("问答租约续期失败: %s", type(exc).__name__)

    thread = threading.Thread(target=renew, name=f"chat-heartbeat-{reservation.turn_id}", daemon=True)
    thread.start()
    try:
        def ensure_active():
            if lost.is_set():
                raise ChatLeaseLost("该回答已取消或已被新的请求替代")
        yield ensure_active
    finally:
        stopped.set()
        thread.join(timeout=2)
