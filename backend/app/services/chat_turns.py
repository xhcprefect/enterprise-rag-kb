"""持久化问答认领、幂等重放和租约隔离；网络期间不持有数据库行锁。

T3 发布契约（与 ``answer_sources`` 共用同一全局锁顺序）::

    User -> KnowledgeBase -> ProjectSource -> ProjectSnapshot
    -> Conversation -> ChatTurn -> Message -> UUID 排序的上传 Document

* 认领：请求 advisory 锁可先拿；随后按根顺序锁 User/KB、项目 Source/Snapshot，再锁会话；
  幂等重放同样复核当前用户令牌版本、ACL 与项目授权。
* 发布：先严格解析 v1 来源快照（trace 的 supplied/history 两列表），再按根顺序锁全部
  实际来源所需行，最后在同一事务里落 assistant 消息；任何复核失败都不写成功回答。
* 租约：短事务、同一根顺序、锁后重新取时刻；续期复用同一检查而不是盲 UPDATE。
"""
import hashlib
import json
import logging
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import ChatTurn, Conversation, KnowledgeBase, Message, User
from app.services import answer_sources, document_scopes, kb_access, memory_service, project_chat_scope
from app.services.answer_sources import SourceSnapshotError, SourceVerificationError

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
    # 认证时刻的原始令牌版本（追加在尾部）：发布/续期/重放都必须与锁定后的 User 精确一致。
    auth_token_version: int | None = None
    # 认领时固定的会话模式与知识库，发布前与真实行比对，避免借用别的会话/库。
    mode: str | None = None
    kb_id: uuid.UUID | None = None


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


# ---------------- 令牌 / ACL 的 fail-closed 复核 ----------------

def _require_token(user: User, auth_token_version) -> None:
    """锁定后的 User 必须启用、令牌版本与认证时快照精确一致。

    缺失/``bool``/字符串/负数一律 401：绝不因“取不到原始版本”而跳过比较（fail open）。
    也不从刷新后的 ORM 实例反推原始版本。
    """
    if user is None or not user.is_active:
        raise HTTPException(401, "用户已不可用，请重新登录")
    if type(auth_token_version) is not int or auth_token_version < 0:
        raise HTTPException(401, "登录状态已失效，请重新登录")
    if auth_token_version != user.token_version:
        raise HTTPException(401, "登录状态已失效，请重新登录")


def _require_kb_acl(db: Session, kb: KnowledgeBase | None, user: User) -> None:
    """统一 ACL（当前事务状态）：admin/public/owner 兼容 + 显式成员 reader/maintainer。

    必须传当前 Session：不缓存成员角色，也不在缺库时按 admin/public 放行。
    """
    if kb is None:
        return
    if not kb_access.can_read_kb(db, kb, user):
        raise HTTPException(404, "知识库不存在")


def _lock_roots(db: Session, user_id: uuid.UUID, kb_id: uuid.UUID | None,
                auth_token_version: int | None) -> answer_sources.SourceRoot:
    """第一段：User -> KnowledgeBase 行锁；随后复核令牌与 ACL。"""
    try:
        root = answer_sources.lock_root(db, user_id, kb_id)
    except SourceVerificationError as exc:
        if "用户不存在" in str(exc):
            raise HTTPException(401, "用户不存在，请重新登录") from exc
        raise HTTPException(404, "知识库不存在") from exc
    _require_token(root.user, auth_token_version)
    _require_kb_acl(db, root.knowledge_base, root.user)
    return root


def _lock_turn_messages(db: Session, turn_id: uuid.UUID) -> list[Message]:
    """第三段后缀：ChatTurn 之后的 Message 行锁（UUID 稳定顺序）。"""
    return db.scalars(
        select(Message).where(Message.turn_id == turn_id).order_by(Message.id)
        .with_for_update().execution_options(populate_existing=True)
    ).all()


def _expire_active(db: Session, conv: Conversation) -> None:
    if conv.active_turn_id is not None:
        turn = db.scalar(select(ChatTurn).where(ChatTurn.id == conv.active_turn_id)
                         .with_for_update().execution_options(populate_existing=True))
        messages = _lock_turn_messages(db, conv.active_turn_id)
        if turn is not None and turn.status == "processing":
            turn.status = "failed"
        for message in messages:
            if message.status == "processing":
                message.status = "failed"
    conv.active_turn_id, conv.lease_expires_at = None, None


def reclaim_expired(db: Session, *, limit: int = 200,
                    expired_before: datetime | None = None) -> int:
    """回收租约已过期的在途问答；返回从 processing 改为 failed 的轮次数（有限批次）。

    正常路径由 ``_expire_active`` 在**同一会话**再次认领时清理。但若进程在生成中途
    被杀（部署、OOM、竞价实例回收），那轮会永远停在 processing：没有并发续期线程，
    也就不会有人再碰这个会话，``/chat/conversations`` 会一直显示「回答中」。

    这里按 Conversation -> ChatTurn -> Message 的行锁顺序逐会话处理：锁后再取新鲜
    UTC 并复核“仍是同一有效轮次且确实已过期”，绝不先收集旧 id 再批量盲改，从而保留
    锁等待期间发生的新续期/新认领。``expired_before`` 可将启动回收限定在固定时刻之前
    已过期的租约；锁后也复核该边界，不追赶启动后才到期或已续期的租约。
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit 必须是 1 到 1000 的整数")
    cutoff = expired_before if expired_before is not None else _now()
    candidates = db.scalars(select(Conversation.id).where(
        Conversation.active_turn_id.is_not(None),
        or_(Conversation.lease_expires_at.is_(None), Conversation.lease_expires_at <= cutoff),
    ).order_by(Conversation.id).limit(limit)).all()
    if not candidates:
        return 0
    processed = 0
    for conversation_id in candidates:
        # 每个会话一个小事务：锁定后立即提交，避免在一个事务里累积整批行锁。
        conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                         .with_for_update().execution_options(populate_existing=True))
        if conv is None or conv.active_turn_id is None:
            db.rollback()
            continue
        now = _now()
        if expired_before is not None:
            now = min(now, expired_before)
        if conv.lease_expires_at is not None and conv.lease_expires_at > now:
            db.rollback()  # 锁等待期间被续期了
            continue
        turn = db.scalar(select(ChatTurn).where(ChatTurn.id == conv.active_turn_id)
                         .with_for_update().execution_options(populate_existing=True))
        _lock_turn_messages(db, conv.active_turn_id)
        now = _now()  # 最后一把锁之后再取时刻，过期判定不用陈旧时间
        if expired_before is not None:
            now = min(now, expired_before)
        if conv.lease_expires_at is not None and conv.lease_expires_at > now:
            db.rollback()
            continue
        if turn is None or turn.status != "processing":
            conv.active_turn_id, conv.lease_expires_at = None, None
            db.commit()
            continue
        turn.status = "failed"
        turn.trace = {"error_code": "lease_lost", "error_message": "处理租约已失效，请使用新请求重试"}
        db.execute(update(Message).where(Message.turn_id == turn.id, Message.status == "processing")
                   .values(status="failed"))
        conv.active_turn_id, conv.lease_expires_at = None, None
        db.commit()
        processed += 1
    if processed:
        logger.warning("回收了 %d 个租约过期的在途会话", processed)
    return processed


def reclaim_expired_on_startup(db: Session, *, limit: int = 200) -> int:
    """按启动时的过期会话数排空积压；每批仍由逐会话短事务回收。

    固定截止时刻与初始计数，批次数只取决于启动积压，不追赶后来到期的新租约。
    不能按 reclaim_expired 的返回值提前停止：整批悬空/已结束轮次或锁后续期都可能
    返回 0，但后面仍有未扫描的过期会话。
    """
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit 必须是 1 到 1000 的整数")
    cutoff = _now()
    eligible = db.scalar(select(func.count()).select_from(Conversation).where(
        Conversation.active_turn_id.is_not(None),
        or_(Conversation.lease_expires_at.is_(None), Conversation.lease_expires_at <= cutoff),
    )) or 0
    processed = 0
    for _ in range((eligible + limit - 1) // limit):
        processed += reclaim_expired(db, limit=limit, expired_before=cutoff)
    return processed


def _replay(db: Session, previous: ChatTurn, *, key: uuid.UUID, digest: str,
            conversation_id: uuid.UUID | None, user_id: uuid.UUID, kb_id: uuid.UUID | None,
            mode: str, root: answer_sources.SourceRoot,
            auth_token_version: int) -> Reservation:
    """在当前授权下重放原结果；固定原快照，不借用后来切换的会话范围。"""
    stored_conversation = previous.conversation_id
    stored_snapshot = previous.project_snapshot_id
    stored_message = previous.user_message_id
    if stored_conversation is None:
        raise HTTPException(410, "原会话已删除，该 request_id 不可复用，请使用新的 request_id")
    if previous.payload_hash != digest or (conversation_id and stored_conversation != conversation_id):
        raise HTTPException(409, "同一 request_id 不能用于不同的问题、参数或会话")
    if root.knowledge_base is not None and root.knowledge_base.kind == "project":
        if stored_snapshot is None:
            raise HTTPException(409, "原请求缺少项目快照，请使用新的 request_id")
        project_chat_scope.lock_claim_binding(db, kb=root.knowledge_base, user=root.user,
                                              snapshot_id=stored_snapshot)
    elif stored_snapshot is not None:
        raise HTTPException(409, "原请求的项目绑定与知识库不一致")
    conv = db.scalar(select(Conversation).where(Conversation.id == stored_conversation)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None:
        raise HTTPException(410, "原会话已删除，该 request_id 不可复用，请使用新的 request_id")
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == previous.id)
                     .with_for_update().execution_options(populate_existing=True))
    if (turn is None or turn.user_id != user_id or turn.request_id != key
            or turn.conversation_id != stored_conversation or turn.payload_hash != digest
            or turn.user_message_id != stored_message or turn.project_snapshot_id != stored_snapshot
            or conv.user_id != user_id or conv.mode != mode or conv.kb_id != kb_id):
        raise HTTPException(409, "原会话配置已改变，请刷新后重试")
    if turn.status != "completed":
        message = "该请求仍在处理中，请等待或刷新会话" if turn.status == "processing" else "该请求已结束且未完成，请使用新的 request_id 重试"
        raise HTTPException(409, message)
    messages = _lock_turn_messages(db, turn.id)
    question = next((row for row in messages if row.id == stored_message and row.role == "user"), None)
    answer = next((row for row in messages if row.role == "assistant"), None)
    if (question is None or answer is None or question.status != "completed"
            or answer.status != "completed" or answer.reply_to_id != stored_message
            or question.conversation_id != conv.id or answer.conversation_id != conv.id):
        raise HTTPException(409, "原请求结果已不可用，请使用新的 request_id")
    _require_token(root.user, auth_token_version)
    _require_kb_acl(db, root.knowledge_base, root.user)
    result = Reservation(turn.id, key, conv.id, stored_message, user_id,
                         answer.content, answer.citations or [], answer.latency_ms, turn.trace or {}, answer.id,
                         project_snapshot_id=stored_snapshot,
                         auth_token_version=auth_token_version, mode=mode, kb_id=kb_id)
    db.commit()
    return result


def claim_turn(db: Session, *, user_id: uuid.UUID, conversation_id: uuid.UUID | None,
               kb_id: uuid.UUID | None, mode: str, question: str, top_k: int,
               request_id: uuid.UUID | None = None, document_ids=None,
               scope_revision: int | None = None, reset_scope: bool = False,
               project_snapshot_id: uuid.UUID | None = None,
               auth_token_version: int | None = None) -> Reservation:
    """只在短事务内认领；同键重放不新建会话/消息，也不再次调用模型。

    项目会话（kind=project 的 KB）绑定项目快照：新会话无显式 sid 选该源 active ready，
    显式 sid 同源同 KB 校验；已有会话保持已绑 sid 不随 active 漂移。mode=chat 任何项目
    范围均拒绝且从不项目检索；项目与 document_ids 任何显式互斥。

    ``auth_token_version`` 是 ``get_current_user`` 在认证成功时写入的原始令牌版本；
    缺失/非法/已失效一律 401，绝不从刷新后的 User 反推。
    """
    if mode not in ("rag", "chat"):
        raise HTTPException(400, "未知会话模式")
    if type(auth_token_version) is not int or auth_token_version < 0:
        raise HTTPException(401, "登录状态已失效，请重新登录")
    if mode == "chat" and kb_id is not None:
        raise HTTPException(400, "普通对话不绑定知识库")
    if mode != "rag" and (document_ids is not None or scope_revision is not None
                          or project_snapshot_id is not None):
        raise HTTPException(400, "普通对话不支持文档范围")
    key = request_id or uuid.uuid4()
    digest = payload_hash(question, mode, kb_id, top_k, document_ids=document_ids,
                          scope_revision=scope_revision, project_snapshot_id=project_snapshot_id)
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _request_lock(user_id, key)})
    previous = db.scalar(select(ChatTurn).where(ChatTurn.user_id == user_id, ChatTurn.request_id == key))
    # 墓碑只报告该用户自己的去重记录；知识库已删也不得重新认领。
    if previous is not None and previous.conversation_id is None:
        _lock_roots(db, user_id, None, auth_token_version)
        raise HTTPException(410, "原会话已删除，该 request_id 不可复用，请使用新的 request_id")
    # 根锁必须先于会话/轮次/文档；重放同样复核当前权限。
    root = _lock_roots(db, user_id, kb_id, auth_token_version)
    kb = root.knowledge_base
    if previous is not None:
        return _replay(db, previous, key=key, digest=digest, conversation_id=conversation_id,
                       user_id=user_id, kb_id=kb_id, mode=mode, root=root,
                       auth_token_version=auth_token_version)

    kb_is_project = kb is not None and kb.kind == "project"
    if project_snapshot_id is not None and not kb_is_project:
        # 普通上传库（或未绑定项目的会话）不得塞入项目快照。
        raise HTTPException(400, "非项目知识库不能绑定项目快照")

    # 项目绑定锁（ProjectSource -> ProjectSnapshot）必须在会话锁之前。为决定锁哪个
    # 快照，需要对会话做一次**无锁**探读（已有会话保持已绑 sid），锁会话后再复核未漂移。
    project_sid = None
    binding = None
    peek_conv = None
    peek_snapshot_id = None
    if mode == "rag" and kb_is_project:
        if conversation_id is not None:
            peek_conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id))
            if peek_conv is None or peek_conv.user_id != user_id:
                raise HTTPException(404, "会话不存在")
            if peek_conv.mode != mode or peek_conv.kb_id != kb_id:
                raise HTTPException(409, "会话配置已改变，请刷新后重试")
            peek_snapshot_id = peek_conv.project_snapshot_id
        project_sid = project_chat_scope.plan_claim_project(
            db, kb=kb, user=root.user, peek_conv=peek_conv, explicit_sid=project_snapshot_id,
            document_ids=document_ids, new_conv=conversation_id is None)
        binding = project_chat_scope.lock_claim_binding(db, kb=kb, user=root.user, snapshot_id=project_sid)
    if conversation_id is None:
        conv = Conversation(id=uuid.uuid4(), user_id=user_id, kb_id=kb_id, mode=mode,
                            title=question.strip()[:30] or "新会话", document_scope=[], scope_revision=0,
                            project_snapshot_id=project_sid)
        db.add(conv)
        db.flush()
    else:
        conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                         .with_for_update().execution_options(populate_existing=True))
        if conv is None or conv.user_id != user_id:
            raise HTTPException(404, "会话不存在")
        if conv.mode != mode or conv.kb_id != kb_id:
            raise HTTPException(409, "会话配置已改变，请刷新后重试")
        if project_sid is not None and conv.project_snapshot_id != peek_snapshot_id:
            raise HTTPException(409, "会话项目快照在认领期间发生变化，请刷新后重试")
    if project_sid is not None:
        project_chat_scope.verify_claim_binding(db, conv=conv, binding=binding, snapshot_id=project_sid,
                                                new_conv=conversation_id is None,
                                                require_active=conversation_id is None and project_snapshot_id is None)
    now = _now()
    if conv.active_turn_id is not None:
        if conv.lease_expires_at and conv.lease_expires_at > now:
            raise HTTPException(409, "该会话已有回答正在生成，请等待完成或停止后重试")
        _expire_active(db, conv)

    current_scope = getattr(conv, "document_scope", [])
    current_revision = getattr(conv, "scope_revision", None) or 0
    if scope_revision is not None and scope_revision != current_revision:
        raise HTTPException(409, "文档范围已在其他页面改变，请刷新会话后重试")
    if project_sid is None and document_ids is not None and conversation_id is not None:
        raise HTTPException(400, "已有会话请先通过范围选择器更新文档范围")
    if project_sid is not None:
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
    turn = ChatTurn(id=identifier, request_id=key, user_id=user_id, conversation_id=conv.id,
                    user_message_id=message_id, payload_hash=digest, status="processing", heartbeat_at=now,
                    turn_seq=turn_seq, project_snapshot_id=project_sid)
    db.add(turn)
    db.flush()
    db.add(Message(id=message_id, conversation_id=conv.id, turn_id=identifier, role="user",
                   content=question, status="processing", turn_seq=turn_seq))
    db.flush()
    # 新轮次及消息的 FK 先写入，再取最后一组 Document 锁；失败由整个认领事务回滚。
    if project_sid is None:
        if document_ids is not None:
            current_scope = document_scopes.resolve_scope(db, kb_id, document_ids, lock=True)
            conv.document_scope = current_scope
            current_revision = conv.scope_revision = int(bool(current_scope))
        elif not reset_scope and (current_scope or not isinstance(current_scope, list)):
            current_scope = document_scopes.validate_scope(db, kb_id, current_scope, lock=True)
    now = _now()
    turn.heartbeat_at = now
    conv.active_turn_id = identifier
    conv.lease_expires_at = now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)
    conv.updated_at = now
    db.commit()
    return Reservation(identifier, key, conv.id, message_id, user_id,
                       document_scope=current_scope, scope_revision=current_revision,
                       project_snapshot_id=project_sid,
                       auth_token_version=auth_token_version, mode=mode, kb_id=kb_id)


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


def _active_locked(db: Session, reservation: Reservation, conv: Conversation | None = None):
    """会话/轮次行锁与租约复核（调用方已按根顺序锁好 User/KB/项目绑定）。"""
    if conv is None:
        conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id)
                         .with_for_update().execution_options(populate_existing=True))
    if conv is None or conv.active_turn_id != reservation.turn_id or not conv.lease_expires_at or conv.lease_expires_at <= _now():
        raise ChatLeaseLost("该回答已取消或处理租约已失效，请刷新会话后重试")
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == reservation.turn_id)
                     .with_for_update().execution_options(populate_existing=True))
    if turn is None or turn.status != "processing":
        raise ChatLeaseLost("该回答已结束，请刷新会话")
    return conv, turn


def _verify_reservation(reservation: Reservation, conv: Conversation, turn: ChatTurn) -> None:
    if (conv.user_id != reservation.user_id or turn.user_id != reservation.user_id
            or turn.conversation_id != reservation.conversation_id
            or conv.mode != reservation.mode or conv.kb_id != reservation.kb_id
            or turn.request_id != reservation.request_id
            or turn.user_message_id != reservation.user_message_id
            or conv.project_snapshot_id != reservation.project_snapshot_id
            or turn.project_snapshot_id != reservation.project_snapshot_id
            or conv.scope_revision != reservation.scope_revision):
        raise ChatLeaseLost("请求与当前会话、轮次或范围不一致，请刷新会话")


def _scope_document_ids(scope) -> list[uuid.UUID]:
    if not scope:
        return []
    return [identifier for identifier, _version in document_scopes.scope_pairs(scope)]


def complete_turn(db: Session, reservation: Reservation, answer: str, citations: list[dict],
                  latency_ms: int, trace: dict, *, scope_update: list[dict] | None = None) -> uuid.UUID:
    """发布一轮回答：严格来源快照 -> 根锁/项目绑定锁 -> 会话/轮次/消息 -> 文档 -> 复核。

    任何来源缺失、漂移、过期、越权或代次变化都让本轮失败（不写成功 assistant 消息），
    包括最终 citations 为空 / 明确拒答的轮次。事务内不做网络访问、不提前提交。
    """
    mode = reservation.mode
    if mode not in ("rag", "chat"):
        raise ChatLeaseLost("请求缺少固定的会话模式，无法发布回答")
    # 认证快照必须存在且是严格非负整数；缺失/非法一律在取任何锁之前 fail closed。
    if type(reservation.auth_token_version) is not int or reservation.auth_token_version < 0:
        raise ChatLeaseLost("请求缺少有效的登录令牌版本，回答未保存，请重新登录后重试")
    try:
        # 1) 先严格解析 trace 的 v1 快照（supplied/history 必须是两个真列表）。
        combined = answer_sources.parse_source_snapshot(trace, mode=mode)
        supplied = answer_sources.parse_sources(trace["supplied_sources"], default_kind=answer_sources.SOURCE_KIND_DOCUMENT)
        historical = answer_sources.parse_sources(trace["history_sources"], default_kind=answer_sources.SOURCE_KIND_DOCUMENT)
    except SourceSnapshotError as exc:
        raise ChatLeaseLost("本轮来源快照缺失或损坏，无法发布回答，请重新提问") from exc

    # 2) 根锁：User -> KB（普通对话只锁 User），并复核令牌版本 / 启用状态 / ACL。
    try:
        root = _lock_roots(db, reservation.user_id, reservation.kb_id, reservation.auth_token_version)
    except HTTPException as exc:
        raise ChatLeaseLost("用户或知识库权限已改变，请刷新会话") from exc
    kb = root.knowledge_base

    # 3) 项目绑定锁在会话锁之前；上传路径绝不查项目表，项目路径绝不锁上传文档。
    binding = None
    if reservation.project_snapshot_id is not None or any(
            source.get("source_kind") == answer_sources.SOURCE_KIND_PROJECT for source in combined):
        if kb is None or kb.kind != "project":
            raise ChatLeaseLost("项目来源与知识库类型不一致，请刷新会话")
        try:
            binding = project_chat_scope.lock_publication_binding(
                db, kb=kb, user=root.user, reservation=reservation, sources=combined)
        except SourceVerificationError as exc:
            raise ChatLeaseLost("项目快照不再可用或被撤销，请刷新会话") from exc
    elif combined and kb is not None and kb.kind == "project":
        raise ChatLeaseLost("项目知识库缺少项目绑定，请刷新会话")

    # 4) 会话 -> 轮次 -> 消息；随后按 UUID 排序一次性锁 scope+provenance 全部上传文档。
    conv, turn = _active_locked(db, reservation)
    _verify_reservation(reservation, conv, turn)
    messages = _lock_turn_messages(db, turn.id)
    user_msg = next((row for row in messages if row.id == reservation.user_message_id), None)
    if (user_msg is None or user_msg.role != "user" or user_msg.status != "processing"
            or user_msg.turn_id != turn.id or user_msg.conversation_id != conv.id):
        raise ChatLeaseLost("原始问题消息不存在或身份已改变，请刷新会话")

    selected = reservation.document_scope if scope_update is None else scope_update
    if reservation.project_snapshot_id is not None:
        try:
            project_chat_scope.verify_active_project(db, conv, reservation)
        except project_chat_scope.ProjectBindingLost as exc:
            raise ChatLeaseLost(str(exc)) from exc
    try:
        scope_documents = _scope_document_ids(selected)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ChatLeaseLost("文档范围已失效，请重新选择或取消范围") from exc
    provenance_documents = [uuid.UUID(source["document_id"]) for source in combined
                            if source.get("source_kind") == answer_sources.SOURCE_KIND_DOCUMENT]
    lock_ids = [*scope_documents, *provenance_documents]
    if kb is not None and kb.kind == "upload" and lock_ids:
        try:
            answer_sources.lock_document_ids(db, kb, lock_ids)
        except SourceVerificationError as exc:
            raise ChatLeaseLost("本轮涉及的文档已删除或不再属于当前知识库，请重新提问") from exc

    # 5) 最后一把锁之后再取新鲜时刻，做最终复核（不早于锁等待）：租约/活跃轮次、
    #    令牌版本、ACL、范围版本、项目绑定、代次与生命周期都要用这个时刻重判。
    now = _now()
    if (conv.active_turn_id != reservation.turn_id or not conv.lease_expires_at
            or conv.lease_expires_at <= now or turn.status != "processing"):
        raise ChatLeaseLost("该回答已取消或处理租约已失效，请刷新会话后重试")
    _require_token(root.user, reservation.auth_token_version)
    _require_kb_acl(db, kb, root.user)
    if (getattr(conv, "scope_revision", None) or 0) != reservation.scope_revision:
        raise ChatLeaseLost("文档范围已改变，请刷新会话后重试")
    if selected:
        # 生成期间发生删除/取消/重建时，禁止将旧代次结果写成成功回答（HTTP 409）。
        selected = document_scopes.validate_scope(db, conv.kb_id, selected, lock=False)
    if combined:
        try:
            answer_sources.validate_sources(db, kb, combined, user=root.user, lock=False,
                                            binding=binding, now=now)
        except SourceVerificationError as exc:
            raise ChatLeaseLost("本轮来源已失效、改代次或不再可读，回答未保存，请重新提问") from exc

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
    user_msg.status, user_msg.turn_seq = "completed", turn_seq
    # 发布后的 trace 只保留已严格解析的来源形状（不把未校验的原始字段带进历史）。
    published = {key: value for key, value in dict(trace).items()
                 if key not in {"source_snapshot_version", "supplied_sources", "history_sources"}}
    published["source_snapshot_version"] = answer_sources.SOURCE_SNAPSHOT_VERSION
    published["supplied_sources"] = [dict(item) for item in supplied]
    published["history_sources"] = [dict(item) for item in historical]
    turn.status, turn.trace = "completed", published
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
    """Conv -> Turn -> Message 同一后缀顺序；只结束仍有效的同一轮次。"""
    conv = db.scalar(select(Conversation).where(Conversation.id == conversation_id)
                     .with_for_update().execution_options(populate_existing=True))
    if conv is None or conv.active_turn_id != turn_id:
        return False
    turn = db.scalar(select(ChatTurn).where(ChatTurn.id == turn_id)
                     .with_for_update().execution_options(populate_existing=True))
    if turn is None or turn.status != "processing":
        return False
    for message in _lock_turn_messages(db, turn_id):
        if message.status == "processing":
            message.status = status
    turn.status = status
    if error_code:
        turn.trace = {"error_code": error_code, "error_message": (error_message or "请求已结束")[:500]}
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
    db.execute(update(Message).where(Message.turn_id.in_(
        select(ChatTurn.id).where(ChatTurn.conversation_id.in_(ids))), Message.status == "processing")
        .values(status="cancelled"))


def cancel_request(db: Session, request_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    turn = db.scalar(select(ChatTurn).where(ChatTurn.request_id == request_id, ChatTurn.user_id == user_id))
    if turn is None:
        raise HTTPException(404, "请求不存在")
    changed = _terminate(db, turn.id, turn.conversation_id, "cancelled") if turn.conversation_id else False
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
    now = _now()
    if conv.active_turn_id == turn.id and conv.lease_expires_at and conv.lease_expires_at > now:
        db.commit()
        return
    active = conv.active_turn_id == turn.id
    current = db.scalar(select(ChatTurn).where(ChatTurn.id == turn.id)
                        .with_for_update().execution_options(populate_existing=True))
    _lock_turn_messages(db, turn.id)
    now = _now()
    if active and conv.lease_expires_at and conv.lease_expires_at > now:
        db.commit()
        return
    if current is not None and current.status == "processing":
        current.status = "failed"
        current.trace = {"error_code": "lease_lost", "error_message": "处理租约已失效，请使用新请求重试"}
        db.execute(update(Message).where(Message.turn_id == turn.id, Message.status == "processing")
                   .values(status="failed"))
        if active:
            conv.active_turn_id, conv.lease_expires_at = None, None
    db.commit()


def check_lease(reservation: Reservation, renew: bool = False) -> bool:
    """短事务检查/续期；跨进程取消以数据库为准，不为每路问答创建常驻线程。

    与发布相同的根顺序（User -> KB -> 项目 Source/Snapshot -> Conversation -> ChatTurn），
    锁后再取新鲜时刻；续期复用同一复核结果而不是单独盲 UPDATE。
    """
    if (reservation.mode not in ("rag", "chat")
            or type(reservation.auth_token_version) is not int or reservation.auth_token_version < 0):
        return False
    try:
        with SessionLocal() as db:
            root = _lock_roots(db, reservation.user_id, reservation.kb_id, reservation.auth_token_version)
            if reservation.project_snapshot_id is not None:
                try:
                    project_chat_scope.lock_lease_binding(db, kb=root.knowledge_base,
                                                          user=root.user, snapshot_id=reservation.project_snapshot_id)
                except (SourceVerificationError, HTTPException):
                    return False
            conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id)
                             .with_for_update().execution_options(populate_existing=True))
            turn = db.scalar(select(ChatTurn).where(ChatTurn.id == reservation.turn_id)
                             .with_for_update().execution_options(populate_existing=True))
            now = _now()  # 锁等待之后重新取时刻
            if (conv is None or conv.active_turn_id != reservation.turn_id
                    or not conv.lease_expires_at or conv.lease_expires_at <= now
                    or turn is None or turn.status != "processing"):
                return False
            try:
                _verify_reservation(reservation, conv, turn)
            except ChatLeaseLost:
                return False
            if reservation.project_snapshot_id is not None:
                conv_check = conv
                try:
                    project_chat_scope.verify_active_project(db, conv_check, reservation)
                except project_chat_scope.ProjectBindingLost:
                    return False
            if renew:
                conv.lease_expires_at = now + timedelta(seconds=settings.CHAT_LEASE_SECONDS)
                turn.heartbeat_at = now
                db.commit()
            else:
                db.rollback()
            return True
    except HTTPException:
        return False
    except Exception as exc:  # noqa: BLE001 - 租约检查失败按失效处理
        logger.warning("问答租约检查失败: %s", type(exc).__name__)
        return False


@contextmanager
def keepalive(reservation: Reservation):
    stopped, lost = threading.Event(), threading.Event()
    interval = max(1.0, min(30.0, settings.CHAT_LEASE_SECONDS / 3))

    def renew():
        while not stopped.wait(interval):
            try:
                # 复用 auth-aware 的检查+续期，而不是绕开令牌/ACL 的盲 UPDATE。
                if not check_lease(reservation, renew=True):
                    lost.set()
                    return
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