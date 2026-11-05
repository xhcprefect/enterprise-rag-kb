"""记忆摘要：新/旧 schema init 两遍幂等、稳定 seq、发布后删会话级联（真实 PostgreSQL）。

仅在本轮 owned 的专用 *_test 库（foundation_test）运行；设置 TEST_DATABASE_URL
指向该库后这些用例真实执行，不视为跳过。
"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from tests._env import TEST_URL

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

if TEST_URL:
    from app.db import session as db_session

    _target = make_url(TEST_URL)
    if db_session.engine.url != _target:
        db_session.engine.dispose()
        db_session.engine = create_engine(TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
        db_session.SessionLocal.configure(bind=db_session.engine)

    from app.db.init_db import init_db
    from app.db.migrations import ensure_foundation_schema, plan_legacy_turn_seq, backfill_turn_seq
    from app.models import (
        ChatTurn,
        Conversation,
        ConversationMemory,
        KnowledgeBase,
        Message,
        SummaryJob,
        SummaryJobStatus,
        User,
    )

    SessionLocal, engine = db_session.SessionLocal, db_session.engine

_ALL_TABLES = (
    "users", "knowledge_bases", "documents", "document_chunks", "upload_sessions",
    "conversations", "chat_turns", "messages", "conversation_memories", "summary_jobs",
    "audit_logs",
)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class MemoryMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE " + ", ".join(_ALL_TABLES) + " RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            user = User(username="mem_admin", role="admin", is_active=True, password_hash="x")
            db.add(user)
            db.flush()
            self.user_id = user.id
            self.kb = KnowledgeBase(name="mem-kb", collection_name="m_" + uuid.uuid4().hex,
                                    owner_id=user.id, is_public=True)
            db.add(self.kb)
            db.commit()
            self.kb_id = self.kb.id

    def columns(self):
        with engine.begin() as conn:
            return {
                (row[0], row[1])
                for row in conn.execute(text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema()"
                ))
            }

    def tables(self):
        with engine.begin() as conn:
            return {
                row[0] for row in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                ))
            }

    def indexes(self):
        with engine.begin() as conn:
            return {
                row[0] for row in conn.execute(text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
                ))
            }

    def make_conversation(self):
        with SessionLocal() as db:
            conv = Conversation(user_id=self.user_id, kb_id=self.kb_id, mode="chat", title="t")
            db.add(conv)
            db.commit()
            return conv.id

    def make_completed_pair(self, conv_id, seq=None, *, user_at=None, assistant_at=None,
                            legacy=False, reply_to_id=None):
        """插入一个完整 user+assistant 对话对（可选带 turn_seq）。

        legacy=True 模拟旧聊天：纯消息、无 ChatTurn、无 turn_id（供补序路径使用）。
        """
        with SessionLocal() as db:
            if legacy:
                # user 必须严格早于同轮 assistant（真实插入顺序），避免全序邻接按
                # (created_at,id) 排序时因同时间戳按 id 交错而错配。
                _u_at = user_at or datetime.now(timezone.utc)
                _a_at = assistant_at or (_u_at + timedelta(seconds=1))
                user_msg = Message(conversation_id=conv_id, role="user", status="completed",
                                   content="u", turn_seq=seq, reply_to_id=reply_to_id,
                                   created_at=_u_at)
                assistant_msg = Message(conversation_id=conv_id, role="assistant",
                                        status="completed", content="a", turn_seq=seq,
                                        created_at=_a_at)
                db.add_all([user_msg, assistant_msg])
                db.commit()
                return None, user_msg.id, assistant_msg.id
            turn = ChatTurn(
                request_id=uuid.uuid4(), user_id=self.user_id, conversation_id=conv_id,
                user_message_id=uuid.uuid4(), payload_hash="p" * 64,
                status="completed", heartbeat_at=datetime.now(timezone.utc),
                turn_seq=seq,
            )
            db.add(turn)
            db.flush()
            user_msg = Message(conversation_id=conv_id, role="user", status="completed",
                               content="u", turn_id=turn.id, turn_seq=seq,
                               created_at=user_at or datetime.now(timezone.utc))
            assistant_msg = Message(conversation_id=conv_id, role="assistant", status="completed",
                                    content="a", turn_id=turn.id, turn_seq=seq,
                                    created_at=assistant_at or datetime.now(timezone.utc))
            db.add_all([user_msg, assistant_msg])
            db.commit()
            return turn.id, user_msg.id, assistant_msg.id

    # ---- schema 幂等 ----
    def test_double_init_creates_memory_tables_columns_indexes(self):
        init_db(create_admin=False)
        init_db(create_admin=False)
        cols = self.columns()
        self.assertIn("conversation_memories", self.tables())
        self.assertIn("summary_jobs", self.tables())
        self.assertIn(("conversations", "next_turn_seq"), cols)
        self.assertIn(("conversations", "memory_epoch"), cols)
        self.assertIn(("conversations", "memory_completed_chars"), cols)
        self.assertIn(("chat_turns", "turn_seq"), cols)
        self.assertIn(("messages", "turn_seq"), cols)
        self.assertIn("uq_chat_turns_conversation_seq", self.indexes())
        self.assertIn("uq_messages_conversation_seq_role", self.indexes())

    def test_old_schema_simulation_rebuilds_idempotently(self):
        # 模拟旧库：删掉本轮新增的表和列，再 init 两遍应恢复且旧行保留。
        self.make_conversation()
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS summary_jobs CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS conversation_memories CASCADE"))
            conn.execute(text("ALTER TABLE conversations DROP COLUMN IF EXISTS next_turn_seq"))
            conn.execute(text("ALTER TABLE messages DROP COLUMN IF EXISTS turn_seq"))
            conn.execute(text("DROP INDEX IF EXISTS uq_chat_turns_conversation_seq"))
            conn.execute(text("DROP INDEX IF EXISTS uq_messages_conversation_seq_role"))
        init_db(create_admin=False)
        init_db(create_admin=False)
        cols = self.columns()
        self.assertIn("conversation_memories", self.tables())
        self.assertIn("summary_jobs", self.tables())
        self.assertIn(("conversations", "next_turn_seq"), cols)
        self.assertIn("uq_chat_turns_conversation_seq", self.indexes())
        self.assertIn("uq_messages_conversation_seq_role", self.indexes())

    def test_conversation_schema_foundation_helper_is_idempotent(self):
        init_db(create_admin=False)
        with engine.begin() as conn:
            ensure_foundation_schema(conn)
            ensure_foundation_schema(conn)
        self.assertIn("uq_chat_turns_conversation_seq", self.indexes())
        self.assertIn("uq_messages_conversation_seq_role", self.indexes())

    # ---- 部分唯一索引 ----
    def test_null_turn_seq_rows_coexist_but_non_null_dup_rejected(self):
        conv_id = self.make_conversation()
        self.make_completed_pair(conv_id, seq=None)
        self.make_completed_pair(conv_id, seq=None)  # NULL 不受约束，能共存
        self.make_completed_pair(conv_id, seq=1)
        with SessionLocal() as db, self.assertRaises(Exception):
            db.execute(text(
                "INSERT INTO chat_turns (id, request_id, user_id, conversation_id, "
                "user_message_id, payload_hash, status, heartbeat_at, turn_seq) "
                "VALUES (:id, :rid, :uid, :cid, :mid, :p, 'processing', now(), 1)"
            ), {
                "id": str(uuid.uuid4()), "rid": str(uuid.uuid4()), "uid": str(self.user_id),
                "cid": str(conv_id), "mid": str(uuid.uuid4()), "p": "p" * 64,
            })
            db.commit()
        with SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(ChatTurn)
                              .where(ChatTurn.conversation_id == conv_id))
            # 3 对各 1 条 ChatTurn（含一条 seq=1），加上被打断的重复插入失败。
            self.assertEqual(count, 3)

    # ---- 稳定 seq：纯函数与 DB backfill ----
    def test_plan_legacy_turn_seq_is_deterministic_and_respects_existing(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        d = lambda m: base + timedelta(minutes=m)  # noqa: E731 - 严格递增时间戳，user 前于同轮 assistant
        rows = [
            {"id": "a1", "role": "assistant", "created_at": d(2), "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "u1", "role": "user", "created_at": d(1), "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "u2", "role": "user", "created_at": d(3), "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a2", "role": "assistant", "created_at": d(4), "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
        ]
        plan1 = plan_legacy_turn_seq(rows)
        plan2 = plan_legacy_turn_seq(rows)
        self.assertEqual(plan1, plan2)  # 确定性
        self.assertEqual(plan1["u1"], 1)
        self.assertEqual(plan1["a1"], 1)
        self.assertEqual(plan1["u2"], 2)
        self.assertEqual(plan1["a2"], 2)
        # 不重排已分配序号：预置完整对 seq=5 不被改，新轮从 6 开始。
        seeded = [
            {"id": "u0", "role": "user", "created_at": d(1), "reply_to_id": None,
             "turn_seq": 5, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a0", "role": "assistant", "created_at": d(2), "reply_to_id": None,
             "turn_seq": 5, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "u9", "role": "user", "created_at": d(3), "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a9", "role": "assistant", "created_at": d(4), "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
        ]
        plan = plan_legacy_turn_seq(seeded)
        self.assertNotIn("u0", plan)   # 已分配 -> 不重排、不入 plan
        self.assertNotIn("a0", plan)
        self.assertEqual(plan["u9"], 6)
        self.assertEqual(plan["a9"], 6)

    def test_plan_reply_to_single_pair_and_ambiguity_rejected(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 干净单 reply：u1 = a1 成对；u2 -> a2 成对（时间严格递增，user 早于同轮 assistant）。
        clean = [
            {"id": "u1", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a1", "role": "assistant", "created_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
             "reply_to_id": "u1", "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "u2", "role": "user", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "a2", "role": "assistant", "created_at": datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
        ]
        plan = plan_legacy_turn_seq(clean)
        self.assertEqual(plan["u1"], 1)
        self.assertEqual(plan["a1"], 1)
        self.assertEqual(plan["u2"], 2)
        self.assertEqual(plan["a2"], 2)
        # 歧义：u1 有两个 assistant 回复 -> 整个拒绝（宁可留 lag），不得挪用给邻接。
        ambiguous = [
            {"id": "u1", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "u2", "role": "user", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "a1", "role": "assistant", "created_at": base, "reply_to_id": "u1",
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a2", "role": "assistant", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
             "reply_to_id": "u1", "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
        ]
        plan = plan_legacy_turn_seq(ambiguous)
        self.assertEqual(plan, {})

    def test_plan_reply_to_non_matching_target_does_not_fall_back_to_adjacency(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 带非空 reply_to_id 的 assistant，即使 reply 目标不在本会话 completed user
        # （跨会话/空白/失败/不存在 id），也绝不 fallback 成裸邻接去配 u。
        rows = [
            {"id": "u", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a", "role": "assistant", "created_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
             "reply_to_id": "other-conversation-user", "turn_seq": None, "turn_id": None,
             "status": "completed", "content": "x"},
        ]
        plan = plan_legacy_turn_seq(rows)
        self.assertEqual(plan, {})  # u 不得被带 reply 的 a 挪用；a 留在显式/其他角色负责
        # 空白/失败目标同样不 fallback。
        rows2 = [
            {"id": "u", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a", "role": "assistant", "created_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
             "reply_to_id": "blank_user", "turn_seq": None, "turn_id": None,
             "status": "completed", "content": "x"},
        ]
        # blank_user 若作为独立行存在且为 completed 空白 user，不会进入 completed_user；
        # a 仍因带 reply 被 reserved，u 不得配 a。
        rows2.append({"id": "blank_user", "role": "user", "created_at": base,
                      "reply_to_id": None, "turn_seq": None, "turn_id": None,
                      "status": "completed", "content": ""})
        self.assertEqual(plan_legacy_turn_seq(rows2), {})

    def test_plan_legacy_adjacency_is_full_order_not_grab_next_free(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 输入 u1,u2,a2：旧逻辑会错误把 u1 与 a2 配对；正确全序邻接让 u2 与 a2 成对，
        # u1 因紧邻是 u2（user 障碍）无法配对。
        rows = [
            {"id": "u1", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "u2", "role": "user", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "a2", "role": "assistant", "created_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
        ]
        plan = plan_legacy_turn_seq(rows)
        self.assertNotIn("u1", plan)     # u1 被 user 障碍阻隔，不得配 a2
        self.assertEqual(plan["u2"], 1)
        self.assertEqual(plan["a2"], 1)

    def test_plan_does_not_pad_incomplete_or_non_completed(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 不完整（缺 assistant / failed / 空白）：不补为可摘要轮；只有完整 completed 邻接成对。
        rows = [
            {"id": "u1", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a1", "role": "assistant", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "failed", "content": "x"},
            {"id": "ublank", "role": "user", "created_at": datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "   "},  # 空白 user 不参与配对
            {"id": "ublank_a", "role": "assistant", "created_at": datetime(2026, 1, 1, 1, 1, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": ""},  # 空白 assistant 不参与
            {"id": "u2", "role": "user", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "a2", "role": "assistant", "created_at": datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "u3", "role": "user", "created_at": datetime(2026, 1, 3, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
        ]
        plan = plan_legacy_turn_seq(rows)
        self.assertNotIn("u1", plan)        # u1 后是 failed/unused a1
        self.assertNotIn("u1", plan) or True
        self.assertNotIn("ublank", plan)    # 空白 user 不给 a1 配对
        self.assertNotIn("ublank_a", plan)
        self.assertEqual(plan["u2"], 1)     # u2 -> a2 成对
        self.assertEqual(plan["a2"], 1)
        self.assertNotIn("u3", plan)        # 孤立 user 不补

    def test_plan_half_pair_joins_existing_seq(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # u1 已 seq=7、紧邻 a1 为 NULL：NULL 侧沿用 7，而非 assigned_max+index。
        # user 早于同轮 assistant（严格时序），才不会按 id 交错导致 a1 排在 u1 前。
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        rows = [
            {"id": "u1", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": 7, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a1", "role": "assistant", "created_at": base + timedelta(minutes=1),
             "reply_to_id": None, "turn_seq": None, "turn_id": None,
             "status": "completed", "content": "x"},
        ]
        plan = plan_legacy_turn_seq(rows)
        self.assertNotIn("u1", plan)   # 已分配不重排
        self.assertEqual(plan["a1"], 7)

    def test_plan_half_pair_skips_when_same_role_seq_taken(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # u1 seq=7；另有一个 assistant a_x 已占 7 -> a1 不能沿用 7（防同 role 撞唯一索引），
        # 留 NULL（滞后），否则 (conversation,turn_seq,role) 会撞唯一。
        rows = [
            {"id": "u1", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": 7, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a1", "role": "assistant", "created_at": datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "a_x", "role": "assistant", "created_at": datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": 7, "turn_id": None, "status": "completed",
             "content": "x"},
        ]
        plan = plan_legacy_turn_seq(rows)
        self.assertNotIn("a1", plan)        # 撞号 -> 不写
        self.assertNotIn("u1", plan)        # 已分配不重排

    def test_plan_leaves_lag_when_old_content_would_invert(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # 新请求已把 a_new 标成 seq=1（created 更晚）；更早的 legacy u_old 未分配。
        # 不得把旧内容 append 为 seq=2（倒挂）-> 留 lag，plan 为空。
        rows = [
            {"id": "u_old", "role": "user", "created_at": base, "reply_to_id": None,
             "turn_seq": None, "turn_id": None, "status": "completed", "content": "x"},
            {"id": "a_old", "role": "assistant", "created_at": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": None, "turn_id": None, "status": "completed",
             "content": "x"},
            {"id": "a_new", "role": "assistant", "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
             "reply_to_id": None, "turn_seq": 1, "turn_id": None, "status": "completed",
             "content": "x"},
        ]
        plan = plan_legacy_turn_seq(rows)
        self.assertEqual(plan, {})

    def test_backfill_turn_seq_is_idempotent_and_preserves_old_rows(self):
        conv_id = self.make_conversation()
        # 两对需时间严格递增且相互分离，避免全序邻接跨对交错。
        self.make_completed_pair(conv_id, seq=None, legacy=True,
                                 user_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.make_completed_pair(conv_id, seq=None, legacy=True,
                                 user_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        with SessionLocal() as db:
            written = backfill_turn_seq(db, conv_id)
            self.assertEqual(written, 4)  # 两对 legacy user+assistant
            # 同一事务内幂等：再跑一遍不重复写
            written_again = backfill_turn_seq(db, conv_id)
            self.assertEqual(written_again, 0)
            db.commit()  # backfill 只 flush；必须由调用方提交才落库
        with SessionLocal() as db:
            messages = db.execute(
                text("SELECT role, turn_seq FROM messages WHERE conversation_id = :cid "
                     "ORDER BY turn_seq, role"),
                {"cid": str(conv_id)},
            ).all()
            self.assertEqual({(m.role, m.turn_seq) for m in messages},
                             {("user", 1), ("assistant", 1), ("user", 2), ("assistant", 2)})

    def test_backfill_does_not_commit_subject_to_caller(self):
        conv_id = self.make_conversation()
        self.make_completed_pair(conv_id, seq=None, legacy=True)
        # 单独会话里补序后再回滚：证明 backfill 只 flush、不 commit。
        s = SessionLocal()
        self.assertEqual(backfill_turn_seq(s, conv_id), 2)
        s.rollback()
        s.close()
        with SessionLocal() as db:
            n = db.execute(
                text("SELECT count(*) FROM messages WHERE conversation_id = :cid "
                     "AND turn_seq IS NOT NULL"),
                {"cid": str(conv_id)},
            ).scalar()
            self.assertEqual(n, 0)  # 未提交 -> 什么都没落库

    def test_backfill_bumps_next_turn_seq_only_forward(self):
        conv_id = self.make_conversation()
        # 两对时间严格递增且分离，last-used 最大 seq = 2。
        self.make_completed_pair(conv_id, seq=None, legacy=True,
                                 user_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.make_completed_pair(conv_id, seq=None, legacy=True,
                                 user_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
        with SessionLocal() as db:
            v = db.execute(text("SELECT next_turn_seq FROM conversations WHERE id = :cid"),
                           {"cid": str(conv_id)}).scalar()
            self.assertEqual(v, 0)
        with SessionLocal() as db:
            backfill_turn_seq(db, conv_id)
            db.commit()
        with SessionLocal() as db:
            v = db.execute(text("SELECT next_turn_seq FROM conversations WHERE id = :cid"),
                           {"cid": str(conv_id)}).scalar()
            self.assertEqual(v, 2)  # last-used 最大 seq = 2；下一新请求取 3，不会撞旧索引。
            # 幂等补序不改动计数，也不回退。
            before = db.execute(text("SELECT next_turn_seq FROM conversations WHERE id = :cid"),
                                {"cid": str(conv_id)}).scalar()
            backfill_turn_seq(db, conv_id)
            db.commit()
            after = db.execute(text("SELECT next_turn_seq FROM conversations WHERE id = :cid"),
                               {"cid": str(conv_id)}).scalar()
            self.assertEqual(before, after)

    def test_backfill_empty_conversation_keeps_counter_zero(self):
        conv_id = self.make_conversation()
        with SessionLocal() as db:
            w = backfill_turn_seq(db, conv_id)
            db.commit()
            self.assertEqual(w, 0)
        with SessionLocal() as db:
            v = db.execute(text("SELECT next_turn_seq FROM conversations WHERE id = :cid"),
                           {"cid": str(conv_id)}).scalar()
            self.assertEqual(v, 0)  # 空会话不产生多余空号

    def test_backfill_syncs_chat_turn_for_reply_paired_message(self):
        conv_id = self.make_conversation()
        # user 是无 turn_id 的 legacy；assistant 带 turn_id（新格式 ChatTurn）但 turn_seq 未分配。
        # 显式 reply 配对后补序，应同步对应 ChatTurn.turn_seq（str 键不丢失）。
        with SessionLocal() as db:
            turn = ChatTurn(
                request_id=uuid.uuid4(), user_id=self.user_id, conversation_id=conv_id,
                user_message_id=uuid.uuid4(), payload_hash="p" * 64, status="completed",
                heartbeat_at=datetime.now(timezone.utc), turn_seq=None,
            )
            db.add(turn)
            db.flush()
            u_msg = Message(conversation_id=conv_id, role="user", status="completed",
                            content="u", turn_seq=None,
                            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            db.add(u_msg)
            db.flush()  # 先生成 u_msg.id，reply_to_id 才有可引用的值（UUID 默认在 flush 时才生成）
            a_msg = Message(conversation_id=conv_id, role="assistant", status="completed",
                            content="a", turn_seq=None, turn_id=turn.id, reply_to_id=u_msg.id,
                            created_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc))
            db.add(a_msg)
            db.commit()
            self.turn_id, self.a_msg_id, self.u_msg_id = turn.id, a_msg.id, u_msg.id
        with SessionLocal() as db:
            backfill_turn_seq(db, conv_id)
            db.commit()
        with SessionLocal() as db:
            a = db.get(Message, self.a_msg_id)
            self.assertIsNotNone(a.turn_seq)
            ct = db.get(ChatTurn, self.turn_id)
            self.assertIsNotNone(ct.turn_seq)          # 经 str 键同步到 ChatTurn，不丢失
            self.assertEqual(ct.turn_seq, a.turn_seq)
            u = db.get(Message, self.u_msg_id)
            self.assertEqual(u.turn_seq, 1)

    def test_backfill_syncs_turn_seq_on_messages_only_and_count_stable(self):
        conv_id = self.make_conversation()
        # 新格式（带 turn_id）消息已分配 seq=5；更早 legacy 不补（倒挂），计数只增不减。
        self.make_completed_pair(conv_id, seq=5)
        self.make_completed_pair(conv_id, seq=None, legacy=True,
                                 user_at=datetime(2025, 12, 1, tzinfo=timezone.utc))
        with SessionLocal() as db:
            backfill_turn_seq(db, conv_id)
            db.commit()
            v = db.execute(text("SELECT next_turn_seq FROM conversations WHERE id = :cid"),
                           {"cid": str(conv_id)}).scalar()
            self.assertEqual(v, 5)  # last-used 最大 seq = 5，保留既有计数，下一新请求取 6。

    # ---- 发布记忆后删除会话：DB 级联，ORM 不先置 NULL ----
    def test_backfill_then_claim_same_session_syncs_identity_map(self):
        # 同 Session 补序后立刻 claim 的场景：M 持有已加载的 conv/question/answer ORM 对象，
        # backfill 用 raw UPDATE 后这些对象必须被精确 expire/重读，否则 claim 复用旧对象
        # 取 next_turn_seq(旧0)+1 会撞已补的 seq(1)。这里验证补序后同对象值已同步。
        conv_id = self.make_conversation()
        with SessionLocal() as db:
            q = Message(conversation_id=conv_id, role="user", status="completed", content="q",
                        turn_seq=None, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            a = Message(conversation_id=conv_id, role="assistant", status="completed", content="a",
                        turn_seq=None, created_at=datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc))
            db.add_all([q, a])
            db.commit()
        with SessionLocal() as db:
            conv = db.get(Conversation, conv_id)
            question = db.scalar(select(Message).where(
                Message.conversation_id == conv_id, Message.role == "user"))
            answer = db.scalar(select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant"))
            self.assertEqual(conv.next_turn_seq, 0)
            self.assertIsNone(question.turn_seq)
            self.assertIsNone(answer.turn_seq)
            # 带会话行锁补序（同一事务，不 commit）。
            written = backfill_turn_seq(db, conv_id)
            self.assertEqual(written, 2)
            # 精确 expire 后，同一 ORM 对象重读应反映 raw UPDATE 写入的值，不再陈旧。
            self.assertEqual(conv.next_turn_seq, 1)
            self.assertEqual(question.turn_seq, 1)
            self.assertEqual(answer.turn_seq, 1)
            # claim 约定：下一条新请求在行锁下取 next_turn_seq + 1 = 2，不会撞已补的 1。
            db.rollback()  # 不落库回归痕迹

    def test_publishing_memory_then_deleting_conversation_cascades(self):
        conv_id = self.make_conversation()
        with SessionLocal() as db:
            conv = db.get(Conversation, conv_id)
            conv.memory = ConversationMemory(
                conversation_id=conv_id, version=1, epoch=0, covered_turn_seq=2,
                structured={"facts": []}, source_range={"start_seq": 1, "end_seq": 2},
            )
            db.add(SummaryJob(
                conversation_id=conv_id, epoch=0, base_memory_version=0, target_turn_seq=3,
                idempotency_key="k-" + uuid.uuid4().hex, status=SummaryJobStatus.PENDING.value,
            ))
            db.commit()
        self.assertEqual(self._memory_count(conv_id), 1)
        self.assertEqual(self._job_count(conv_id), 1)

        # ORM 删除会话：不应因 memory/job 的 NOT NULL conversation_id 报错，应 DB 级联清掉。
        with SessionLocal() as db:
            conv = db.get(Conversation, conv_id)
            db.delete(conv)
            db.commit()
        self.assertEqual(self._memory_count(conv_id), 0)
        self.assertEqual(self._job_count(conv_id), 0)

    def _memory_count(self, conv_id):
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(ConversationMemory)
                             .where(ConversationMemory.conversation_id == conv_id))

    def _job_count(self, conv_id):
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(SummaryJob)
                             .where(SummaryJob.conversation_id == conv_id))


if __name__ == "__main__":
    unittest.main()
