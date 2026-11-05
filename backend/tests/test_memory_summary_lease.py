"""内存摘要 worker 的租约安全边界：真实 PG 上的双会话反例回归。

不把连接串/端口/凭据/owner 常量硬编码进源码；由显式运行环境提供
``TEST_DATABASE_URL`` 与 ``TEST_DATABASE_OWNER``（owner 是目标库 comment 里的归属
标记，用于防止清到其它 owner 的库）。核验顺序写死在代码里，任何 TRUNCATE 前必验：

- 未配置 TEST_DATABASE_URL + TEST_DATABASE_OWNER -> 显式 skip（不碰任何库）；
- 配置了但缺一个、engine 实际连的库与 TEST_DATABASE_URL 库名不符、或库 comment 与
  TEST_DATABASE_OWNER 不符 -> 硬 fail（绝不静默跳过、绝不清其它 owner 的库）。

所有断言走真实 ``claim_next_summary`` / ``publish_summary`` / ``mark_failed`` /
``heartbeat_job`` / ``recover_stale_summary_jobs``，用真消息/真批次/真
``parse_summary`` 产物，不用空 {} 绕过来源校验。覆盖锁内 fencing 与过期拒续/拒写：

  1. fresh 合法批次 -> 成功发布（正控）；
  2. 过期未 recover -> 拒发布（expiry guard）、拒续期；
  3. reset（epoch+1）-> 迟到发布/迟到 mark_failed 均拒（epoch/base guard）；
  4. recover 后同一行换新租约 -> 旧 owner 迟到发布拒写（原 claim token fence）；
  5. 来源被改动 -> 拒发布（重新计算真实来源 digest 不符）；
  6. nil/过期 heartbeat -> 拒续期（与发布拒 nil 一致）。

运行示例（环境在启动时就要给出，供 tests/__init__ 在引擎建好前 apply）：
  RAG_DISABLE_DOTENV=1 \
  TEST_DATABASE_URL=postgresql+psycopg://USER:PASS@HOST:PORT/memory_test \
  TEST_DATABASE_OWNER=owned-test:<owner-hex> \
  python -m unittest tests.test_memory_summary_lease -v
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

# ---- 专属库选择来自"启动时刻的运行环境"，这里不硬编码 ----
# ``tests/__init__.py`` 在导入时执行 ``tests._env.prepare()``，而 ``_env.TEST_URL`` 在
# 那次导入时一次性捕获环境变量：若在本模块体内才设置 TEST_DATABASE_URL 就太晚，引擎会
# 绑定到默认开发库。所以 URL 必须在启动命令里给出；本模块只读取并据此核验。
os.environ.setdefault("RAG_DISABLE_DOTENV", "1")
import tests._env as _env  # noqa: E402

_env.prepare()

from tests import _memory_project_test_gate as gate  # noqa: E402

_TARGET_URL = gate.TEST_DATABASE_URL
_TARGET_OWNER = gate.TARGET_OWNER
# 只有“两者都缺”才整体 skip；部分/坏配置必须进入用例并在 setUpClass 硬 fail，
# 绝不用 skip 掩盖配置错误。
_MISSING = gate.classify(_TARGET_URL, _TARGET_OWNER) == "missing_both"

from sqlalchemy import select, text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.init_db import init_db  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    CONVERSATION_MODE_CHAT,
    ChatTurn,
    Conversation,
    ConversationMemory,
    Message,
    SummaryJob,
    SummaryJobStatus,
    User,
)
from app.services import memory_service  # noqa: E402
from app.services.memory_prompt import SummaryFormatError, parse_summary  # noqa: E402


def _verify_owner() -> None:
    """破坏性读写前走共享门：URL 配置合法、实际 engine 逐字段一致、库 comment 与
    TEST_DATABASE_OWNER 精确匹配。任何一项不符即硬 fail（ConfigError），
    绝不静默跳过、绝不清其它 owner 的库。"""
    gate.verify(_TARGET_URL, _TARGET_OWNER, engine)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    _verify_owner()
    init_db(create_admin=False)
    _schema_ready = True


@unittest.skipIf(_MISSING,
                 "需显式设置 TEST_DATABASE_URL(以 _test 结尾) 与 TEST_DATABASE_OWNER(带测试库前缀)")
class MemorySummaryLeaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_schema()

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        with engine.begin() as conn:  # 双重核 owner 后清空业务表
            _verify_owner()
            conn.execute(text(
                "TRUNCATE users, conversations, chat_turns, messages, summary_jobs, "
                "conversation_memories RESTART IDENTITY CASCADE"
            ))
        self.patchers = [
            patch.object(settings, "CHAT_MEMORY_ENABLED", True),
            patch.object(settings, "MEMORY_JOB_LEASE_SECONDS", 300),
            # 只放 1 个完整轮时，默认 keep_recent=4 会把唯一那轮全留作 recent、无可归档，
            # claim 会因"无输入的选不了"返回 None。0 使 select_input_window 选中最旧轮，
            # 得到真实可摘批次的同时不影响本测试关心的租约语义。
            patch.object(settings, "MEMORY_KEEP_RECENT_TURNS", 0),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    # ---- fixtures ----

    def _make_user_conv(self) -> uuid.UUID:
        with SessionLocal() as db:
            u = User(username="mem_lease_user", role="user", is_active=True,
                     password_hash=hash_password("Original123"))
            db.add(u)
            db.flush()
            conv = Conversation(user_id=u.id, mode=CONVERSATION_MODE_CHAT, title="会话")
            db.add(conv)
            db.flush()
            cid = conv.id
            turn = ChatTurn(request_id=uuid.uuid4(), user_id=u.id, conversation_id=cid,
                            user_message_id=uuid.uuid4(), payload_hash="0" * 64,
                            status="completed", heartbeat_at=_now())
            db.add(turn)
            db.flush()
            um = Message(conversation_id=cid, role="user", content="我想长期记住这个决定",
                         status="completed", turn_seq=1, turn_id=turn.id)
            am = Message(conversation_id=cid, role="assistant", content="好的，我会记住",
                         status="completed", turn_seq=1, turn_id=turn.id)
            db.add(um)
            db.add(am)
            db.commit()
            self.user_msg_id = um.id
            self.assistant_msg_id = am.id
            return cid

    def _enqueue_pending(self, cid: uuid.UUID, target: int = 1) -> uuid.UUID:
        with SessionLocal() as db:
            job = SummaryJob(
                conversation_id=cid, epoch=0, base_memory_version=0,
                target_turn_seq=target,
                idempotency_key="lease-test-" + uuid.uuid4().hex,
                status=SummaryJobStatus.PENDING.value,
            )
            db.add(job)
            db.commit()
            return job.id

    def _claim(self) -> SummaryJob:
        """真实领取：返回 detached processing job（含冻结 lease/快照/digest）。

        ``expire_on_commit=False`` 让 claim 内部 commit 后已加载属性仍可访问，否则
        detached + expired 的对象一读属性就抛 DetachedInstanceError。
        """
        with SessionLocal(expire_on_commit=False) as db:
            job = memory_service.claim_next_summary(db)
            self.assertIsNotNone(job)
            db.expunge(job)
            return job

    def _real_parsed(self, message_ids: list) -> dict:
        """用真实消息 id 构 JSON 并走真实 parse_summary 归一化，来源不空。"""
        raw = {
            "version": 1,
            "unfinished": False,
            "goals": [],
            "constraints": [],
            "confirmed_facts": [{
                "text": "用户决定长期记住一件事",
                "source_message_ids": [str(message_ids[0])],
                "verification": "confirmed_by_user",
            }],
            "decisions": [],
            "preferences": [],
            "corrections": [],
            "open_questions": [],
        }
        return parse_summary(json.dumps(raw, ensure_ascii=False))

    def _memory_for(self, cid) -> ConversationMemory | None:
        with SessionLocal() as db:
            return db.scalar(select(ConversationMemory)
                             .where(ConversationMemory.conversation_id == cid))

    # ---- tests ----

    def test_fresh_real_claim_publishes(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        parsed = self._real_parsed([self.user_msg_id])
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            memory = memory_service.publish_summary(db, row, parsed)
            self.assertIsNotNone(memory)
            db.rollback()
        with SessionLocal() as db:
            self.assertEqual(db.get(SummaryJob, job.id).status,
                             SummaryJobStatus.READY.value)
        self.assertIsNotNone(self._memory_for(cid))

    def test_expired_unrecovered_publish_and_heartbeat_refused(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        # 老化 heartbeat（保持同一 lease/status，尚未 recover）。
        stale = _now() - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS + 30)
        with engine.begin() as conn:
            conn.execute(text("UPDATE summary_jobs SET heartbeat_at = :h WHERE id = :id"),
                         {"h": stale, "id": job.id})
        parsed = self._real_parsed([self.user_msg_id])
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            self.assertIsNone(memory_service.publish_summary(db, row, parsed))
            db.rollback()
        self.assertIsNone(self._memory_for(cid))
        # 过期拒续期。
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            self.assertFalse(memory_service.heartbeat_job(db, row))
            db.rollback()

    def test_nil_heartbeat_refused_renewal(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        with engine.begin() as conn:
            conn.execute(text("UPDATE summary_jobs SET heartbeat_at = NULL WHERE id = :id"),
                         {"id": job.id})
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            self.assertFalse(memory_service.heartbeat_job(db, row))
            db.rollback()

    def test_reset_epoch_bump_refuses_late_publish_and_late_fail(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        # reset：epoch+1（真实 reset 语义），快照/digest/lease 保持旧值。
        with engine.begin() as conn:
            conn.execute(text("UPDATE conversations SET memory_epoch = memory_epoch + 1 "
                              "WHERE id = :id"), {"id": cid})
        parsed = self._real_parsed([self.user_msg_id])
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            self.assertIsNone(memory_service.publish_summary(db, row, parsed))
            db.rollback()
        # 迟到失败也不得把(早期)任务标成 FAILED。
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            memory_service.mark_failed(db, row, "model_error", "late")
            db.rollback()
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            self.assertNotEqual(row.status, SummaryJobStatus.FAILED.value)
        self.assertIsNone(self._memory_for(cid))

    def test_recover_then_reclaim_refuses_old_owner_late_publish(self):
        """最危险竞态：同一行 recover 后又被新 claim 拿走新租约，旧 owner 迟到发布须拒。"""
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        old = self._claim()
        old_lease = old.lease_id
        old_digest = old.input_digest
        # 第二会话：老化 heartbeat -> recover 作废旧租约 -> 重新 claim 换新租约。
        with SessionLocal() as db:
            db.execute(text("UPDATE summary_jobs SET heartbeat_at = :h WHERE id = :id"),
                       {"h": _now() - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS + 30),
                        "id": old.id})
            db.commit()
            recovered = memory_service.recover_stale_summary_jobs(db)
            self.assertEqual(recovered, 1)
            new_job = memory_service.claim_next_summary(db)
            self.assertIsNotNone(new_job)
            # 同一批来源 re-claim 的 digest 自然相同（快照确定性）；真正的 fence 是
            # 新 claim 换发了新租约，旧 owner 必须用它 claim 时那份旧租约才被拒。
            self.assertNotEqual(str(new_job.lease_id), str(old_lease))
            db.rollback()
        # 旧 owner 仍用其 OLD detached job 迟到发布 -> 原 claim fence 拒绝。
        parsed = self._real_parsed([self.user_msg_id])
        with SessionLocal() as db:
            db.expunge_all()
            memory = memory_service.publish_summary(db, old, parsed)
            self.assertIsNone(memory)
            db.rollback()
        self.assertIsNone(self._memory_for(cid))
        with SessionLocal() as db:
            row = db.get(SummaryJob, old.id)
            # 仍归新租约所有，旧结果未落地、错误终态未被旧 owner 改写。
            self.assertEqual(row.status, SummaryJobStatus.PROCESSING.value)

    def test_source_changed_refuses_publish(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()  # 冻结旧 digest
        # 第二会话：改动一条真实来源消息内容（完成状态不变）。
        with engine.begin() as conn:
            conn.execute(text("UPDATE messages SET content = :c WHERE id = :id"),
                         {"c": "被改动，不再是 claim 时的原文", "id": self.user_msg_id})
        parsed = self._real_parsed([self.user_msg_id])
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            memory = memory_service.publish_summary(db, row, parsed)
            self.assertIsNone(memory)
            db.rollback()
        self.assertIsNone(self._memory_for(cid))

    def test_fresh_heartbeat_renews(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        with SessionLocal() as db:
            row = db.get(SummaryJob, job.id)
            self.assertTrue(memory_service.heartbeat_job(db, row))
            db.rollback()

    def _add_turn(self, cid, seq):
        with SessionLocal() as db:
            user_id = db.get(Conversation, cid).user_id
            turn = ChatTurn(request_id=uuid.uuid4(), user_id=user_id, conversation_id=cid,
                            user_message_id=uuid.uuid4(), payload_hash="0" * 64,
                            status="completed", heartbeat_at=_now())
            db.add(turn)
            db.flush()
            user = Message(conversation_id=cid, role="user", content=f"决定 {seq}",
                           status="completed", turn_seq=seq, turn_id=turn.id)
            answer = Message(conversation_id=cid, role="assistant", content=f"记住 {seq}",
                             status="completed", turn_seq=seq, turn_id=turn.id)
            db.add_all([user, answer])
            db.commit()
            return user.id

    def test_pending_successor_rebases_after_first_publish(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        first = self._claim()
        new_message = self._add_turn(cid, 2)
        successor_id = self._enqueue_pending(cid, target=2)
        with SessionLocal() as db:
            self.assertEqual(db.get(SummaryJob, successor_id).base_memory_version, 0)
            first_memory = memory_service.publish_summary(db, first, self._real_parsed([self.user_msg_id]))
            self.assertEqual(first_memory.version, 1)
        successor = self._claim()
        self.assertEqual(successor.id, successor_id)
        self.assertEqual(successor.base_memory_version, 1)
        self.assertEqual(successor.input_snapshot["base_version"], 1)
        self.assertEqual(successor.input_snapshot["previous"], first_memory.structured)
        self.assertIn(str(new_message), successor.input_snapshot["message_ids"])
        self.assertNotIn(str(self.user_msg_id), successor.input_snapshot["message_ids"])
        with SessionLocal() as db:
            result = memory_service.publish_summary(db, successor, self._real_parsed([new_message]))
            self.assertEqual(result.version, 2)
            self.assertEqual(result.covered_turn_seq, 2)
            self.assertEqual(result.covered_turns, 2)
            self.assertEqual(db.get(SummaryJob, successor_id).status, "ready")

    def test_already_absorbed_successor_retires_without_another_claim(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        first = self._claim()
        successor_id = self._enqueue_pending(cid)
        with SessionLocal() as db:
            self.assertIsNotNone(memory_service.publish_summary(db, first, self._real_parsed([self.user_msg_id])))
        with SessionLocal() as db:
            self.assertIsNone(memory_service.claim_next_summary(db))
            row = db.get(SummaryJob, successor_id)
            self.assertEqual(row.status, "cancelled")
            self.assertIsNone(row.lease_id)
            self.assertEqual(row.attempts, 0)

    def test_rejected_publication_resolves_processing_and_preserves_successor(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        first = self._claim()
        successor_id = self._enqueue_pending(cid)
        with engine.begin() as conn:
            conn.execute(text("UPDATE messages SET content = :content WHERE id = :id"),
                         {"content": "来源已被修改", "id": self.user_msg_id})
        with SessionLocal() as db:
            self.assertIsNone(memory_service.publish_summary(db, first, self._real_parsed([self.user_msg_id])))
            row = db.get(SummaryJob, first.id)
            self.assertEqual(row.status, "cancelled")
            self.assertIsNone(row.lease_id)
            self.assertEqual(db.get(SummaryJob, successor_id).status, "pending")

    def test_recover_rechecks_heartbeat_after_candidate_read(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        stale = _now() - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS + 30)
        with engine.begin() as conn:
            conn.execute(text("UPDATE summary_jobs SET heartbeat_at = :h WHERE id = :id"), {"h": stale, "id": job.id})
        read_candidates = memory_service._stale_summary_job_ids

        def renew_after_read(db, deadline):
            candidates = read_candidates(db, deadline)
            self.assertIn(job.id, candidates)
            with engine.begin() as conn:
                conn.execute(text("UPDATE summary_jobs SET heartbeat_at = :h WHERE id = :id"),
                             {"h": _now(), "id": job.id})
            return candidates

        with SessionLocal() as db:
            cached = db.get(SummaryJob, job.id)
            with patch.object(memory_service, "_stale_summary_job_ids", side_effect=renew_after_read):
                self.assertEqual(memory_service.recover_stale_summary_jobs(db), 0)
            db.refresh(cached)
            self.assertEqual(cached.status, "processing")
            self.assertEqual(cached.lease_id, job.lease_id)

    def test_shutdown_cannot_return_a_reclaimed_lease(self):
        from app.services import memory_worker
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        old = self._claim()
        lock = memory_service._lock_conversation
        new_lease = []

        def reclaim_before_lock(db, conversation_id):
            with engine.begin() as conn:
                conn.execute(text("UPDATE summary_jobs SET heartbeat_at = :h WHERE id = :id"), {
                    "h": _now() - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS + 30), "id": old.id})
            with patch.object(memory_service, "_lock_conversation", side_effect=lock):
                with SessionLocal() as other:
                    self.assertEqual(memory_service.recover_stale_summary_jobs(other), 1)
                    current = memory_service.claim_next_summary(other)
                    new_lease.append(current.lease_id)
            return lock(db, conversation_id)

        with patch.object(memory_service, "_lock_conversation", side_effect=reclaim_before_lock):
            memory_worker._release_on_shutdown(old)
        with SessionLocal() as db:
            current = db.get(SummaryJob, old.id)
            self.assertEqual(current.status, "processing")
            self.assertEqual(current.lease_id, new_lease[0])
            self.assertNotEqual(current.lease_id, old.lease_id)

    def test_shutdown_returns_owned_job_or_retires_it_when_pending_exists(self):
        from app.services import memory_worker
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        memory_worker._release_on_shutdown(job)
        with SessionLocal() as db:
            current = db.get(SummaryJob, job.id)
            self.assertEqual(current.status, "pending")
            self.assertIsNone(current.lease_id)
        reclaimed = self._claim()
        successor_id = self._enqueue_pending(cid)
        memory_worker._release_on_shutdown(reclaimed)
        with SessionLocal() as db:
            self.assertEqual(db.get(SummaryJob, reclaimed.id).status, "cancelled")
            self.assertEqual(db.get(SummaryJob, successor_id).status, "pending")

    def test_cached_conversation_cannot_hide_reset_epoch(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        with SessionLocal(expire_on_commit=False) as db:
            cached = db.get(Conversation, cid)
            db.commit()
            with engine.begin() as conn:
                conn.execute(text("UPDATE conversations SET memory_epoch = memory_epoch + 1 WHERE id = :id"), {"id": cid})
            self.assertEqual(cached.memory_epoch, 0)
            self.assertIsNone(memory_service.publish_summary(db, job, self._real_parsed([self.user_msg_id])))
            self.assertEqual(cached.memory_epoch, 1)
            self.assertEqual(db.get(SummaryJob, job.id).status, "cancelled")

    def test_cached_source_cannot_hide_a_changed_message(self):
        cid = self._make_user_conv()
        self._enqueue_pending(cid)
        job = self._claim()
        with SessionLocal(expire_on_commit=False) as db:
            cached = db.get(Message, self.user_msg_id)
            original = cached.content
            db.commit()
            with engine.begin() as conn:
                conn.execute(text("UPDATE messages SET content = :content WHERE id = :id"),
                             {"content": "另一事务已修改来源", "id": self.user_msg_id})
            self.assertEqual(cached.content, original)
            self.assertIsNone(memory_service.publish_summary(db, job, self._real_parsed([self.user_msg_id])))
            self.assertEqual(cached.content, "另一事务已修改来源")
            self.assertNotEqual(db.get(SummaryJob, job.id).status, "processing")

    def test_claim_refreshes_cached_pending_job_after_epoch_change(self):
        cid = self._make_user_conv()
        job_id = self._enqueue_pending(cid)
        with SessionLocal(expire_on_commit=False) as db:
            cached = db.get(SummaryJob, job_id)
            db.commit()
            with engine.begin() as conn:
                conn.execute(text("UPDATE conversations SET memory_epoch = 1 WHERE id = :id"), {"id": cid})
                conn.execute(text("UPDATE summary_jobs SET epoch = 1 WHERE id = :id"), {"id": job_id})
            self.assertEqual(cached.epoch, 0)
            claimed = memory_service.claim_next_summary(db)
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed.id, job_id)
            self.assertEqual(claimed.epoch, 1)
            self.assertEqual(claimed.input_snapshot["epoch"], 1)

    def test_oversized_input_is_failed_instead_of_reported_as_absorbed(self):
        cid = self._make_user_conv()
        job_id = self._enqueue_pending(cid)
        with engine.begin() as conn:
            conn.execute(text("UPDATE messages SET content = :content WHERE id = :id"),
                         {"content": "超长输入" * 20000, "id": self.user_msg_id})
        with SessionLocal() as db:
            self.assertIsNone(memory_service.claim_next_summary(db))
            row = db.get(SummaryJob, job_id)
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.error_code, "no_claimable_input")


if __name__ == "__main__":
    unittest.main()
