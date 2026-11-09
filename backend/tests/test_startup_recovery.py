"""启动问答回收的离线批次、租约边界与 lifespan 回归；不连接数据库。"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

from app.models import ChatTurn, Conversation, Message
from app.services import chat_turns

with patch("app.core.logging.setup_logging"):
    from app import main


START = datetime(2026, 1, 1, tzinfo=timezone.utc)
PAST = START - timedelta(hours=1)


class RecoverySession:
    """只解释回收所需查询的内存替身；同时校验有限选择和逐会话锁顺序。"""

    def __init__(self, count=0):
        self.conversations, self.turns, self.messages = {}, {}, {}
        self.batches, self.selection_cutoffs, self.events = [], [], []
        self.count_cutoffs = []
        self.commits = self.rollbacks = 0
        self.on_conversation_lock = self.on_messages_lock = self.on_commit = None
        for index in range(1, count + 1):
            self.add(index)

    def add(self, index, *, deadline=PAST, active=True):
        conversation_id, turn_id = uuid.UUID(int=index), uuid.UUID(int=100_000 + index)
        conv = SimpleNamespace(id=conversation_id, active_turn_id=turn_id if active else None,
                               lease_expires_at=deadline)
        self.conversations[conversation_id] = conv
        self.turns[turn_id] = SimpleNamespace(id=turn_id, status="processing", trace=None)
        self.messages[turn_id] = [SimpleNamespace(status="processing")]
        return conv

    def eligible(self, statement):
        sql = str(statement)
        assert "conversations.active_turn_id IS NOT NULL" in sql
        assert "conversations.lease_expires_at IS NULL" in sql
        assert "conversations.lease_expires_at <=" in sql
        assert "FOR UPDATE" not in sql
        cutoff, = [value for value in statement.compile().params.values()
                   if isinstance(value, datetime)]
        rows = (conv for conv in self.conversations.values()
                if conv.active_turn_id is not None
                and (conv.lease_expires_at is None or conv.lease_expires_at <= cutoff))
        return cutoff, rows

    @staticmethod
    def locked_id(statement):
        assert "FOR UPDATE" in str(statement)
        assert statement.get_execution_options()["populate_existing"] is True
        identifier, = [value for value in statement.compile().params.values()
                       if isinstance(value, uuid.UUID)]
        return identifier

    def scalars(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is Conversation:
            assert statement.column_descriptions[0]["name"] == "id"
            assert "ORDER BY conversations.id" in str(statement)
            limit = statement._limit_clause.value
            assert 1 <= limit <= 1000
            cutoff, eligible = self.eligible(statement)
            ids = sorted(conv.id for conv in eligible)[:limit]
            self.selection_cutoffs.append(cutoff)
            self.batches.append(ids)
            return SimpleNamespace(all=lambda: ids)
        assert entity is Message
        turn_id = self.locked_id(statement)
        self.events.append(("messages", turn_id))
        if self.on_messages_lock:
            self.on_messages_lock(turn_id)
        return SimpleNamespace(all=lambda: self.messages[turn_id])

    def scalar(self, statement):
        if str(statement).startswith("SELECT count("):
            cutoff, eligible = self.eligible(statement)
            self.count_cutoffs.append(cutoff)
            return sum(1 for _ in eligible)
        entity = statement.column_descriptions[0]["entity"]
        identifier = self.locked_id(statement)
        if entity is Conversation:
            self.events.append(("conversation", identifier))
            conv = self.conversations[identifier]
            if self.on_conversation_lock:
                self.on_conversation_lock(conv)
            return conv
        assert entity is ChatTurn
        self.events.append(("turn", identifier))
        return self.turns.get(identifier)

    def execute(self, statement):
        assert str(statement).startswith("UPDATE messages SET status=")
        params = statement.compile().params
        turn_id, = [value for value in params.values() if isinstance(value, uuid.UUID)]
        assert params["status"] == "failed"
        assert params["status_1"] == "processing"
        self.events.append(("update", turn_id))
        for message in self.messages[turn_id]:
            if message.status == "processing":
                message.status = "failed"

    def commit(self):
        self.events.append(("commit",))
        self.commits += 1
        if self.on_commit:
            self.on_commit()

    def rollback(self):
        self.events.append(("rollback",))
        self.rollbacks += 1


class StartupRecoveryTests(unittest.TestCase):
    def test_drains_401_rows_in_bounded_batches_and_per_conversation_transactions(self):
        db = RecoverySession(401)
        db.conversations[uuid.UUID(int=1)].lease_expires_at = None
        with patch.object(chat_turns, "_now", return_value=START):
            self.assertEqual(chat_turns.reclaim_expired_on_startup(db), 401)
        self.assertEqual([len(batch) for batch in db.batches], [200, 200, 1])
        self.assertEqual(db.count_cutoffs, [START])
        self.assertEqual(db.selection_cutoffs, [START] * 3)
        self.assertEqual((db.commits, db.rollbacks), (401, 0))
        for index, conv in enumerate(db.conversations.values()):
            turn_id = uuid.UUID(int=100_001 + index)
            self.assertEqual(db.events[index * 5:(index + 1) * 5], [
                ("conversation", conv.id), ("turn", turn_id), ("messages", turn_id),
                ("update", turn_id), ("commit",),
            ])
            self.assertIsNone(conv.active_turn_id)
            self.assertIsNone(conv.lease_expires_at)
            self.assertEqual(db.turns[turn_id].status, "failed")
            self.assertEqual(db.turns[turn_id].trace["error_code"], "lease_lost")
            self.assertEqual(db.messages[turn_id][0].status, "failed")

    def test_zero_processed_first_full_batch_does_not_hide_later_expired_rows(self):
        for reason in ("dangling", "renewed"):
            with self.subTest(reason=reason):
                db = RecoverySession(401)
                if reason == "dangling":
                    for index in range(1, 201):
                        turn_id = uuid.UUID(int=100_000 + index)
                        if index <= 100:
                            del db.turns[turn_id]
                        else:
                            db.turns[turn_id].status = "completed"
                else:
                    def renew(conv):
                        if conv.id.int <= 200:
                            conv.lease_expires_at = START + timedelta(hours=1)
                    db.on_conversation_lock = renew
                with patch.object(chat_turns, "_now", return_value=START), \
                     patch.object(chat_turns, "reclaim_expired", wraps=chat_turns.reclaim_expired) as batch:
                    self.assertEqual(chat_turns.reclaim_expired_on_startup(db), 201)
                self.assertEqual(batch.call_count, 3)
                self.assertEqual([len(ids) for ids in db.batches], [200, 200, 1])
                self.assertTrue(all(conv.active_turn_id is None
                                    for conv in list(db.conversations.values())[200:]))
                if reason == "renewed":
                    self.assertEqual((db.commits, db.rollbacks), (201, 200))
                    self.assertTrue(all(db.turns[conv.active_turn_id].status == "processing"
                                        for conv in list(db.conversations.values())[:200]))
                else:
                    self.assertEqual((db.commits, db.rollbacks), (401, 0))

    def test_fixed_cutoff_does_not_chase_new_or_newly_expired_leases(self):
        db, clock = RecoverySession(3), [START]
        initially_live = db.add(4, deadline=START + timedelta(seconds=1))

        def new_work():
            clock[0] += timedelta(minutes=10)
            db.add(100 + db.commits, deadline=clock[0] + timedelta(seconds=1))

        db.on_commit = new_work
        with patch.object(chat_turns, "_now", side_effect=lambda: clock[0]):
            self.assertEqual(chat_turns.reclaim_expired_on_startup(db, limit=1), 3)
        self.assertEqual(db.selection_cutoffs, [START] * 3)
        self.assertEqual(len(db.batches), 3)
        self.assertIsNotNone(initially_live.active_turn_id)
        self.assertEqual(len(db.conversations), 7)
        self.assertTrue(all(db.turns[conv.active_turn_id].status == "processing"
                            for conv in db.conversations.values() if conv.active_turn_id is not None))

    def test_initial_count_bounds_calls_even_if_batches_keep_reporting_zero(self):
        db = MagicMock()
        db.scalar.return_value = 10_001
        with patch.object(chat_turns, "_now", return_value=START) as now, \
             patch.object(chat_turns, "reclaim_expired", return_value=0) as batch:
            self.assertEqual(chat_turns.reclaim_expired_on_startup(db), 0)
        now.assert_called_once_with()
        db.scalar.assert_called_once()
        self.assertEqual(batch.call_args_list,
                         [call(db, limit=200, expired_before=START)] * 51)

    def test_cutoff_is_rechecked_after_each_lock_wait_even_if_renewal_has_since_expired(self):
        for lock in ("conversation", "messages"):
            with self.subTest(lock=lock):
                db, clock = RecoverySession(1), [START]
                conv = db.conversations[uuid.UUID(int=1)]
                turn_id = conv.active_turn_id
                renewed = START + timedelta(hours=1)

                def renew(_row):
                    conv.lease_expires_at = renewed
                    clock[0] = START + timedelta(hours=2)

                setattr(db, "on_" + lock + "_lock", renew)
                with patch.object(chat_turns, "_now", side_effect=lambda: clock[0]) as now:
                    self.assertEqual(chat_turns.reclaim_expired_on_startup(db), 0)
                self.assertEqual(now.call_count, 2 if lock == "conversation" else 3)
                self.assertEqual((db.commits, db.rollbacks), (0, 1))
                self.assertEqual(conv.active_turn_id, turn_id)
                self.assertEqual(conv.lease_expires_at, renewed)
                self.assertEqual(db.turns[turn_id].status, "processing")
                self.assertEqual(db.messages[turn_id][0].status, "processing")
                expected = [("conversation", conv.id)]
                if lock == "messages":
                    expected += [("turn", turn_id), ("messages", turn_id)]
                self.assertEqual(db.events, expected + [("rollback",)])

    def test_default_reclaim_api_remains_one_batch_and_counts_only_failed_turns(self):
        db = RecoverySession(201)
        db.turns[uuid.UUID(int=100_001)].status = "completed"
        with patch.object(chat_turns, "_now", return_value=START):
            self.assertEqual(chat_turns.reclaim_expired(db), 199)
        self.assertEqual([len(batch) for batch in db.batches], [200])
        self.assertEqual(db.count_cutoffs, [])
        self.assertEqual(db.commits, 200)
        self.assertIsNotNone(db.conversations[uuid.UUID(int=201)].active_turn_id)

    def test_no_eligible_rows_does_not_run_a_batch(self):
        db = RecoverySession()
        db.add(1, deadline=START + timedelta(seconds=1))
        db.add(2, active=False)
        with patch.object(chat_turns, "_now", return_value=START):
            self.assertEqual(chat_turns.reclaim_expired_on_startup(db), 0)
        self.assertEqual(db.count_cutoffs, [START])
        self.assertEqual(db.batches, [])
        self.assertEqual(db.events, [])

    def test_invalid_batch_limits_are_rejected_before_querying(self):
        for recover in (chat_turns.reclaim_expired, chat_turns.reclaim_expired_on_startup):
            for limit in (0, -1, 1001, True, False, "200", 200.0, None):
                with self.subTest(recover=recover.__name__, limit=limit):
                    db = MagicMock()
                    with self.assertRaises(ValueError):
                        recover(db, limit=limit)
                    self.assertEqual(db.mock_calls, [])

    def test_valid_batch_limit_boundaries(self):
        for limit in (1, 1000):
            with self.subTest(limit=limit):
                db = RecoverySession(3)
                with patch.object(chat_turns, "_now", return_value=START):
                    self.assertEqual(chat_turns.reclaim_expired_on_startup(db, limit=limit), 3)
                self.assertEqual(len(db.batches), (3 + limit - 1) // limit)


class StartupLifespanTests(unittest.IsolatedAsyncioTestCase):
    async def check_lifespan(self, *, failure=None):
        db = MagicMock()
        db.__enter__.return_value = db
        db.scalar.return_value = 0
        with patch.object(main, "init_db") as init, \
             patch.object(main, "SessionLocal", return_value=db), \
             patch.object(chat_turns, "reclaim_expired_on_startup", side_effect=failure) as recover, \
             patch.object(chat_turns, "reclaim_expired") as single_batch, \
             patch.object(main.anyio.to_thread, "current_default_thread_limiter",
                          return_value=SimpleNamespace(total_tokens=0)), \
             patch.object(main.cloud_models, "close_async_clients", new_callable=AsyncMock) as close, \
             patch.object(main.logger, "warning") as warning:
            async with main.lifespan(main.app):
                init.assert_called_once_with()
                recover.assert_called_once_with(db)
                single_batch.assert_not_called()
                db.scalar.assert_called_once()
                if failure:
                    db.rollback.assert_called_once_with()
                    warning.assert_called_once_with(
                        "回收过期问答失败，将由租约续期逻辑处理: %s", type(failure).__name__)
                else:
                    db.rollback.assert_not_called()
                    warning.assert_not_called()
            close.assert_awaited_once_with()
        db.__exit__.assert_called_once()

    async def test_startup_uses_full_backlog_helper(self):
        await self.check_lifespan()

    async def test_startup_rolls_back_and_warns_but_continues_on_recovery_failure(self):
        await self.check_lifespan(failure=RuntimeError("offline recovery failure"))
