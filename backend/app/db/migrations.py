"""本轮新增的两阶段 schema 辅助：记忆摘要与项目索引的增量列/索引与 seq 补序。

划分：旧的 users/upload/documents/conversations/messages 增量列与文档去重等全部
DDL 仍由 ``app.db.init_db`` 负责；本模块只承载本轮（F 基础）新增的部分，由
``init_db`` 在 create_all 之后调用，保证新旧 schema 两遍初始化结果一致：

- ``ensure_foundation_schema(conn)``：本轮新增列 + 部分唯一索引（幂等）。
- ``plan_legacy_turn_seq(rows)``：为旧 chat 消息确定性补序的纯函数（不重排已分配
  序号；reply_to 显式优先、歧义拒绝、legacy 按完整全序邻接、半对沿用、不倒挂）。M
  可在会话事务里应用它，或直接用 ``backfill_turn_seq``。
- ``backfill_turn_seq(session, conversation_id)``：在会话行锁的单事务内补序并同步
  对应 ``chat_turns.turn_seq`` 与 ``conversations.next_turn_seq``。**只 flush 不
  commit**——提交权归调用方，避免提前提交 M 持有的 claim/complete 事务。

补充约定：
- 新请求的稳定轮次序号由 ``Conversation.next_turn_seq`` 在会话行锁下分配（见 M
  chat_turns 保留逻辑）；本模块只在补序后把该计数器提升到不高于已分配序号之上，
  且只增不减，使下一新请求不会与旧 Message 的部分唯一索引撞号。
- 已分配非空 turn_seq 的 ChatTurn/Message 永远不重排；补序只填 NULL 行。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models import ChatTurn, Conversation, Message

logger = logging.getLogger(__name__)

_MIN_DT = datetime(1970, 1, 1)

# 本轮新增列的 ALTER 段（幂等；表不存在时跳过，交给 create_all）。
_FOUNDATION_COLUMNS: dict[str, dict[str, str]] = {
    "conversations": {
        "next_turn_seq": "INTEGER NOT NULL DEFAULT 0",
        "memory_epoch": "INTEGER NOT NULL DEFAULT 0",
        "memory_completed_turns": "INTEGER NOT NULL DEFAULT 0",
        "memory_completed_chars": "BIGINT NOT NULL DEFAULT 0",
        "memory_completed_tokens": "BIGINT NOT NULL DEFAULT 0",
        "memory_stats_initialized": "BOOLEAN NOT NULL DEFAULT FALSE",
        "memory_stats_complete": "BOOLEAN NOT NULL DEFAULT FALSE",
        "project_snapshot_id": "UUID",
    },
    "chat_turns": {
        "turn_seq": "INTEGER",
        "project_snapshot_id": "UUID",
    },
    "messages": {
        "turn_seq": "INTEGER",
    },
    "knowledge_bases": {
        "kind": "VARCHAR(16) NOT NULL DEFAULT 'upload'",
    },
}


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    )
    return {row[0] for row in rows}


def ensure_foundation_schema(conn) -> None:
    """幂等地补齐本轮新增列与部分唯一索引（新库与模拟旧库两遍一致）。"""
    table_names = {
        row[0]
        for row in conn.execute(
            text("SELECT table_name FROM information_schema.tables "
                 "WHERE table_schema = current_schema()")
        )
    }
    for table, columns in _FOUNDATION_COLUMNS.items():
        if table not in table_names:
            continue
        existing = _existing_columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    # 会话内稳定轮次序号：ChatTurn (conversation, turn_seq) 非空唯一。
    if "chat_turns" in table_names:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_chat_turns_conversation_seq "
            "ON chat_turns (conversation_id, turn_seq) WHERE turn_seq IS NOT NULL"
        ))
    # Message (conversation, turn_seq, role) 非空唯一。
    if "messages" in table_names:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_conversation_seq_role "
            "ON messages (conversation_id, turn_seq, role) WHERE turn_seq IS NOT NULL"
        ))


def plan_legacy_turn_seq(rows) -> dict[str, int]:
    """为旧 chat 完整轮确定性补序，返回 {message_id: turn_seq}（只含本次应写 NULL 行）。

    ``rows``：每项为 dict，至少含 ``id``、``role``（'user'/'assistant'）、
    ``created_at``（可比对象）、``reply_to_id``、``turn_seq``、``turn_id``、``status``、
    ``content``。

    规则（与 M/P 契约一致，经主审修正）：
    1. 已分配非空 turn_seq 的行绝不被重排/清除，结果亦不与新序号冲突；
    2. 只处理 status=='completed' 且 ``content`` 非空白的完整轮；空白/不完整/
       failed/processing 不补为可摘要轮；
    3. 显式 ``reply_to`` 配对优先：assistant.reply_to_id -> 本会话 user；一个 user 有多个
       assistant 回复、两边已绑不同 ChatTurn（turn_id 相斥）或已分配 seq 不一致属歧义——
       整个拒绝该 user，其与相邻 assistant 一律不进邻接、不互相挪用；
    4. legacy（无 turn_id）邻接按“完整全序”配对：user 的紧邻下一条必须是可用的 completed
       legacy assistant，非 completed / 空白 / 其它 user / 已占用 assistant 都是配对障碍；
    5. 半对沿用：一对中一侧已带 seq=S、另一侧为 NULL 时，NULL 侧写 S；写前须确认同 role
       没有其它消息已占 S（否则撞 (conversation,turn_seq,role) 唯一索引 -> 留 NULL 滞后）；
    6. 新序号从现有最大已分配 turn_seq 之后连续分配，按 user.created_at,id 稳定顺序；仅当
       该 user 不早于所有已分配内容（不产生时序倒挂）才分配，否则宁可留 legacy 滞后不补；
    7. 只返回应写 NULL 行；未配对/被拒/已分配/滞后/撞号的行一律不出现在结果里。
    """
    return _plan_legacy_turn_seq(rows)


def _plan_legacy_turn_seq(rows) -> dict[str, int]:
    by_id: dict[str, dict] = {}
    for r in rows:
        by_id[str(r["id"])] = r

    def seq_of(r) -> int | None:
        s = r.get("turn_seq")
        return int(s) if s is not None else None

    def sortkey(r):
        # 全序：先 created_at 后 id；缺 created_at 时用最旧兜底（DB 列非空）。
        dt = r.get("created_at")
        return (dt if dt is not None else _MIN_DT, str(r["id"]))

    def is_legacy(r) -> bool:
        return not r.get("turn_id")

    def is_blank(r) -> bool:
        c = r.get("content")
        return c is None or not str(c).strip()

    # 只考虑 completed 且非空白的消息作为可摘要轮成员。
    completed_user = {
        str(r["id"]): r for r in by_id.values()
        if r.get("status") == "completed" and r["role"] == "user" and not is_blank(r)
    }
    completed_assistants = [
        r for r in by_id.values()
        if r.get("status") == "completed" and r["role"] == "assistant" and not is_blank(r)
    ]
    if not completed_user or not completed_assistants:
        return {}

    # 已分配行的最大 seq、最新已分配内容的全序键、以及 (role -> 已占用 seq 集合)。
    assigned_max = 0
    existing_max_key = None
    used_seq_by_role: dict[str, set[int]] = {}
    for r in by_id.values():
        s = seq_of(r)
        if s is not None:
            assigned_max = max(assigned_max, s)
            used_seq_by_role.setdefault(r["role"], set()).add(s)
            key = sortkey(r)
            if existing_max_key is None or key > existing_max_key:
                existing_max_key = key

    # ---- A. 显式 reply_to 配对（歧义/互斥拒绝）----
    reply_edges: dict[str, list[str]] = {}
    for a in completed_assistants:
        rid = a.get("reply_to_id")
        if rid and str(rid) in completed_user:
            reply_edges.setdefault(str(rid), []).append(str(a["id"]))
    # 凡携带非空 reply_to_id 的 assistant 一律只走显式路径，禁止被 legacy 邻接挪用——
    # 即使 reply 目标不在本会话 completed user（跨会话/空白/failed/不存在 id）也如此：
    # 它已不是“裸 legacy 邻接”可以自由配对的对象，不能 fallback 成邻接 user。
    reserved_assistants: set[str] = {
        str(a["id"]) for a in completed_assistants if a.get("reply_to_id")
    }

    pairs: list[tuple[dict, dict]] = []      # (user, assistant)
    joined_seq: dict[str, int] = {}          # user_id -> 半对已有 seq（NULL 侧沿用）
    consumed: set[str] = set()
    rejected_users: set[str] = set()
    for uid, aid_list in reply_edges.items():
        u = completed_user[uid]
        if len(aid_list) != 1:
            rejected_users.add(uid)          # 多个 assistant 回复 -> 歧义，拒绝
            continue
        aid = aid_list[0]
        a = by_id[aid]
        us, as_ = seq_of(u), seq_of(a)
        # turn_id 相斥（两边已绑不同 ChatTurn）或已分配但 seq 不一致 -> 拒绝。
        if (u.get("turn_id") and a.get("turn_id")
                and str(u["turn_id"]) != str(a["turn_id"])):
            rejected_users.add(uid)
            continue
        if us is not None and as_ is not None and us != as_:
            rejected_users.add(uid)
            continue
        pairs.append((u, a))
        consumed.add(uid)
        consumed.add(aid)
        if us is not None or as_ is not None:
            joined_seq[uid] = us if us is not None else as_

    # ---- B. 全序邻接（仅 legacy 非空白完整 user；被拒/已配对的排除）----
    total_order = sorted(by_id.values(), key=sortkey)
    pos = {str(r["id"]): i for i, r in enumerate(total_order)}
    available_assistant_ids = {
        str(a["id"]) for a in completed_assistants
        if str(a["id"]) not in consumed
        and is_legacy(a)
        and str(a["id"]) not in reserved_assistants
    }
    pending_users = [
        u for u in completed_user.values()
        if str(u["id"]) not in consumed
        and str(u["id"]) not in rejected_users
        and is_legacy(u)
    ]
    for u in sorted(pending_users, key=sortkey):
        i = pos.get(str(u["id"]))
        if i is None or i + 1 >= len(total_order):
            continue
        nxt = total_order[i + 1]
        if str(nxt["id"]) not in available_assistant_ids:
            continue  # 紧邻不是可用 completed legacy assistant（障碍）-> 不配对
        a = by_id[str(nxt["id"])]
        us, as_ = seq_of(u), seq_of(a)
        if us is not None and as_ is not None:
            continue  # 两边都已分配 -> 无需补序；不占用。
        pairs.append((u, a))
        if us is not None:
            joined_seq[str(u["id"])] = us
            consumed.add(str(nxt["id"]))          # 仅 assistant 被占用
        elif as_ is not None:
            joined_seq[str(u["id"])] = as_
            consumed.add(str(u["id"]))            # 仅 user 被占用
        else:
            consumed.add(str(u["id"]))
            consumed.add(str(nxt["id"]))
        available_assistant_ids.discard(str(nxt["id"]))

    # ---- C. 分配新序号（延续 assigned_max；防时序倒挂 + 防同 role 撞 seq）----
    def acceptable_fresh(u) -> bool:
        if existing_max_key is None:
            return True
        return sortkey(u) >= existing_max_key

    plan: dict[str, int] = {}
    seq_counter = assigned_max
    for u, a in sorted(pairs, key=lambda p: sortkey(p[0])):
        uid, aid = str(u["id"]), str(a["id"])
        us, as_ = seq_of(u), seq_of(a)
        if us is None and as_ is None:
            if not acceptable_fresh(u):
                continue  # 旧内容不得 append 为更新的轮次 -> 留 lag
            seq_counter += 1
            plan[uid] = seq_counter
            plan[aid] = seq_counter
            continue
        # 半对：NULL 侧沿用已分配 seq；写前防同 role 其它消息已占 S。
        s = joined_seq.get(uid) or us or as_
        if us is None and s not in used_seq_by_role.get("user", set()):
            plan[uid] = s
        if as_ is None and s not in used_seq_by_role.get("assistant", set()):
            plan[aid] = s
    return plan


def backfill_turn_seq(session: Session, conversation_id) -> int:
    """在单事务（会话行锁）内为该会话 legacy 消息补序，并同步推进分配计数器。

    只写入仍未分配的 NULL 行；返回写入的 ``messages.turn_seq`` 条数（幂等）。本函数
    **只 flush、不 commit**——必须由调用方在合适的事务边界提交，避免提前提交 M 持有的
    claim/complete 事务。

    与补序一起在同一会话行锁内完成三件事，保证下一次新请求不会与旧 Message 的部分唯一
    索引 (conversation, turn_seq, role) 撞号：

    1. 写 ``messages.turn_seq``（NULL -> seq）；
    2. 对带 ``turn_id`` 的消息同步对应 ``chat_turns.turn_seq``（仅填 NULL，绝不重排）；
    3. 把 ``conversations.next_turn_seq`` 提升到不高于现有序号的范围之上，且只增不减
       （保留已有计数器不回退）。

    统计边界：本函数只做 legacy 补序，**不** 计算/回写 ``memory_completed_turns`` /
    ``memory_completed_chars`` / ``memory_completed_tokens``。这三者由 M 的
    memory_worker 在同一个会话行锁事务的 complete 流程里一次性累加（B 有界统计，供
    触发判定），避免本模块与 M 双写造成重复累加；本函数返回后、同一事务内 M 再落
    统计即可。raw UPDATE 会绕过 ORM identity map，故本函数对**实际写到的** Message /
    ChatTurn / Conversation 实例做精确 expire，保证同事务内 M 复用的 ORM 对象读到新值
    （而非整站 expire_all 冲掉别处未 flush 状态）。
    """
    conv = session.execute(
        text("SELECT id, next_turn_seq FROM conversations WHERE id = :cid FOR UPDATE"),
        {"cid": conversation_id},
    ).mappings().first()
    if conv is None:
        return 0

    messages = session.execute(
        text(
            "SELECT id, role, created_at, reply_to_id, turn_seq, turn_id, status, content "
            "FROM messages WHERE conversation_id = :cid ORDER BY created_at, id"
        ),
        {"cid": conversation_id},
    ).mappings().all()
    rows = [dict(m) for m in messages]
    plan = plan_legacy_turn_seq(rows)
    # plan 用 str 键；这里统一 str(id) 以便 .get(message_id) 命中并同步 ChatTurn。
    rows_by_id = {str(r["id"]): r for r in rows}

    written = 0
    written_message_uuids: list = []        # 实际被写到的 Message 主键（原生 UUID，用于精确 expire）
    synced_turn_uuids: list = []            # 实际被同步到的 ChatTurn 主键（原生 UUID）
    turn_id_to_seq: dict[str, int] = {}
    for message_id, seq in plan.items():
        res = session.execute(
            text("UPDATE messages SET turn_seq = :seq WHERE id = :id AND turn_seq IS NULL"),
            {"seq": seq, "id": message_id},
        )
        if res.rowcount:
            written += res.rowcount
            row = rows_by_id.get(message_id)
            if row is not None:
                written_message_uuids.append(row["id"])
        turn_id = (rows_by_id.get(message_id) or {}).get("turn_id")
        if turn_id:
            turn_id_to_seq.setdefault(str(turn_id), seq)

    # 同步 ChatTurn（仅填 NULL，保留已分配序号，绝不重排）。记录实际写到的 turn 以便精确 expire。
    for turn_key, seq in turn_id_to_seq.items():
        res = session.execute(
            text("UPDATE chat_turns SET turn_seq = :seq WHERE id = :tid AND turn_seq IS NULL"),
            {"seq": seq, "tid": turn_key},
        )
        if res.rowcount:
            # 从 rows 里找回该 turn 的原生 UUID（turn_key 是 str 形式）。
            for r in rows:
                tr = r.get("turn_id")
                if tr is not None and str(tr) == turn_key:
                    synced_turn_uuids.append(tr)
                    break

    # next_turn_seq 约定 = 该会话“已分配最大 turn_seq”（last-used；见 conversation.py）。
    # 新请求在行锁下取 next_turn_seq + 1。这里把它提升到不低于现有最大已分配 seq，
    # 且只增不减：空会话保持 0（不产生多余空号），幂等二次补序不改动计数。
    max_existing = max(
        (int(r["turn_seq"]) for r in rows if r.get("turn_seq") is not None),
        default=0,
    )
    max_plan = max(plan.values(), default=0)
    current = int(conv["next_turn_seq"] or 0)
    target = max(current, max_existing, max_plan)
    bumped_counter = target > current
    if bumped_counter:
        session.execute(
            text("UPDATE conversations SET next_turn_seq = :n WHERE id = :cid "
                 "AND next_turn_seq < :n"),
            {"n": target, "cid": conversation_id},
        )

    # 只 flush；提交权交给调用方（不提前提交 M 的事务）。
    session.flush()

    # 精确同步 identity map：raw text UPDATE 不会自动更新已加载的 ORM 实例，否则同事务里 M
    # 持有的 conv/question/answer 对象仍是旧值，claim 取 next_turn_seq+1 会撞到刚补的 seq。
    # 只对“实际被本函数写到的”实例 expire，不整站 expire_all（避免冲掉别处未 flush 状态）。
    for mid in written_message_uuids:
        obj = session.get(Message, mid)
        if obj is not None:
            session.expire(obj)
    for tid in synced_turn_uuids:
        obj = session.get(ChatTurn, tid)
        if obj is not None:
            session.expire(obj)
    if bumped_counter:
        obj = session.get(Conversation, conv["id"])
        if obj is not None:
            session.expire(obj)

    logger.info("backfill_turn_seq: 为会话 %s 补序 %d 条消息", conversation_id, written)
    return written
