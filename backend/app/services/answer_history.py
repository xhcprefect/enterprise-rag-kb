"""仅为模型装配当前仍可核验的完整历史轮；不改动用于展示的原消息。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ChatTurn, Conversation, KnowledgeBase, Message, User
from app.services import answer_sources, kb_access
from app.services.answer_sources import SourceSnapshotError, SourceVerificationError
from app.services.chat_turns import ChatLeaseLost, Reservation


def load_rag_history(db: Session, reservation: Reservation) -> list[dict]:
    """有界、无行锁的短读取；数据失效整轮排除，数据库故障仍向调用方传播。"""
    with db.no_autoflush:
        user = db.scalar(select(User).where(User.id == reservation.user_id)
                         .execution_options(populate_existing=True))
        if (user is None or user.is_active is not True
                or type(reservation.auth_token_version) is not int
                or reservation.auth_token_version < 0 or user.token_version != reservation.auth_token_version):
            raise ChatLeaseLost("登录状态已改变，请重新登录")
        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == reservation.kb_id)
                       .execution_options(populate_existing=True))
        if kb is None or not kb_access.can_read_kb(db, kb, user):
            raise ChatLeaseLost("知识库权限已改变，请刷新会话")
        conv = db.scalar(select(Conversation).where(Conversation.id == reservation.conversation_id)
                         .execution_options(populate_existing=True))
        if (conv is None or reservation.mode != "rag" or conv.user_id != user.id or conv.mode != "rag"
                or conv.kb_id != kb.id or conv.scope_revision != reservation.scope_revision
                or conv.project_snapshot_id != reservation.project_snapshot_id):
            raise ChatLeaseLost("会话配置已改变，请刷新会话")
        if kb.kind == "project":
            if reservation.project_snapshot_id is None:
                raise ChatLeaseLost("项目会话缺少固定快照，请刷新会话")
            try:
                answer_sources.read_project_binding(db, [reservation.project_snapshot_id], kb=kb, user=user)
            except SourceVerificationError as exc:
                raise ChatLeaseLost("项目快照不再可用，请刷新会话") from exc
        elif reservation.project_snapshot_id is not None:
            raise ChatLeaseLost("会话来源类型已改变，请刷新会话")
        rows = db.scalars(select(Message).where(
            Message.conversation_id == conv.id, Message.status == "completed",
        ).order_by(Message.created_at.desc(), Message.id.desc())
          .limit(settings.HISTORY_MAX_TURNS * 2).execution_options(populate_existing=True)).all()
        turn_ids = {row.turn_id for row in rows if row.turn_id is not None}
        turns = {row.id: row for row in db.scalars(select(ChatTurn).where(
            ChatTurn.id.in_(turn_ids), ChatTurn.conversation_id == conv.id,
            ChatTurn.user_id == user.id, ChatTurn.status == "completed",
        ).execution_options(populate_existing=True)).all()} if turn_ids else {}
        grouped: dict = {}
        for row in rows:
            grouped.setdefault(row.turn_id, []).append(row)
        accepted = {}
        for turn_id, messages in grouped.items():
            turn = turns.get(turn_id)
            if turn is None or len(messages) != 2 or turn.project_snapshot_id != reservation.project_snapshot_id:
                continue
            question = next((row for row in messages if row.role == "user" and row.id == turn.user_message_id), None)
            answer = next((row for row in messages if row.role == "assistant"), None)
            if (question is None or answer is None or answer.reply_to_id != question.id
                    or not isinstance(question.content, str) or not question.content.strip()
                    or not isinstance(answer.content, str) or not answer.content.strip()):
                continue
            try:
                sources = answer_sources.sources_from_trace(turn.trace, mode="rag")
                if any(source.get("source_kind") == "project"
                       and source["snapshot_id"] != str(reservation.project_snapshot_id) for source in sources):
                    continue
                answer_sources.validate_sources(db, kb, sources, user=user, lock=False)
            except (SourceSnapshotError, SourceVerificationError):
                continue
            trace = turn.trace
            provenance = {"source_snapshot_version": answer_sources.SOURCE_SNAPSHOT_VERSION,
                          "supplied_sources": [dict(item) for item in sources], "history_sources": []}
            topic = trace.get("followup_topic")
            if not isinstance(topic, str) or not 0 < len(topic) <= settings.HISTORY_MAX_CHARS:
                topic = None
            accepted[turn_id] = (question, answer, provenance, topic)

        # 按问题在有界消息窗口内的位置排序；同时间戳的 UUID 排序不影响显式配对。
        positions = {row.id: index for index, row in enumerate(reversed(rows))}
        history = []
        for question, answer, provenance, topic in sorted(accepted.values(), key=lambda pair: positions[pair[0].id]):
            for row in (question, answer):
                item = {"id": str(row.id), "role": row.role, "content": row.content,
                        "turn_id": str(row.turn_id), "status": "completed",
                        "reply_to_id": str(row.reply_to_id) if row.reply_to_id else None,
                        "citations": (row.citations or []) if row.role == "assistant" else []}
                if row.role == "assistant":
                    item[answer_sources.SOURCE_PROVENANCE_FIELD] = provenance
                    item["followup_topic"] = topic
                history.append(item)
    return history
