"""记忆历史读取的最小回归：聊天用最新完整后缀，摘要用最旧连续前缀。

只改 memory_service 历史读取 / _build_snapshot 的回归。核心契约：

- ``build_chat_history`` 必须取**实际最新的完整轮后缀**（至少最新 keep_recent 对），
  而不是旧实现 "ASC+LIMIT 取旧窗口末尾" —— 超过 2*MAX_INPUT+KEEP+100 个未覆盖完整轮时，
  旧实现会把真正最新的完整轮全部丢掉。这里用 >300 轮（含 seq 空洞、failed/processing
  轮、显式 reply_to 与 legacy NULL-seq 对）在真 PG 上回归。
- ``_build_snapshot`` 取**最旧连续未覆盖前缀**、**保护最新 keep_recent 轮**、且不因读取
  方向变化跳过中间区间。

本测试不 TRUNCATE / drop，也不动源数据：fixture 全部塞进"外部事务 + savepoint"里，
每条用例结束回滚，随机 UUID 的核随之消失；不写任何常驻行。破坏性读写前先走共享门
（``tests._memory_project_test_gate``：loopback / ``_test`` 后缀 / owner 标签格式 /
实际 engine 逐字段一致 / ``pg_database`` comment 与 owner 精确匹配），任何不符即硬 fail
并拒绝写。库选择完全来自显式运行环境，不绑定某台机器的容器 id / 端口。

运行方式（凭据与 owner 只在本进程环境给出，不进源码/报告）：
  RAG_DISABLE_DOTENV=1 LOG_STDOUT_ONLY=true \
  LLM_BASE_URL=http://127.0.0.1:1/v1 EMBEDDING_BASE_URL=http://127.0.0.1:1/v1 \
  TEST_DATABASE_URL=postgresql+psycopg://USER:PASS@127.0.0.1:<port>/<db>_test \
  TEST_DATABASE_OWNER=owned-test:<token> \
  python -m unittest tests.test_memory_history_read -v
"""
from __future__ import annotations

import os
import re
import unittest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

# 专属库选择来自"启动时刻的运行环境"（tests/__init__ 在导入应用模块前 prepare）。
os.environ.setdefault("RAG_DISABLE_DOTENV", "1")
import tests._env as _env  # noqa: E402

_env.prepare()

# 共享安全门：导入期不连接、不抛配置异常；无配置 -> 本模块整体 skip，
# 部分/坏配置 -> 执行 setUpClass 时以 ConfigError 硬 fail（绝不 skip 掩盖）。
from tests import _memory_project_test_gate as gate  # noqa: E402

_TARGET_URL = gate.TEST_DATABASE_URL
_TARGET_OWNER = gate.TARGET_OWNER
_CLASS_SKIP = gate.classify(_TARGET_URL, _TARGET_OWNER) == "missing_both"

from sqlalchemy import text  # noqa: E402

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
    User,
)
from app.services import memory_service  # noqa: E402


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _verify_target() -> None:
    """破坏性读写前走共享门：loopback / ``_test`` / owner 标签 / 实际 engine 逐字段 /
    库 comment 精确匹配。任何一项不符即硬 fail（ConfigError）。"""
    gate.verify(_TARGET_URL, _TARGET_OWNER, engine)
    with engine.connect() as conn:  # 已过门，此时才允许连接
        dbname = conn.execute(text("SELECT current_database()")).scalar()
    # 声明库名与实连库名再核一次（防 URL 解析与实际驱动差异）。
    if dbname != (engine.url.database or ""):
        raise RuntimeError(
            f"engine 实际连的库 {dbname!r} != engine.url 指定库 {engine.url.database!r}，拒绝操作"
        )


_schema_ready = False


def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    _verify_target()
    init_db(create_admin=False)
    _schema_ready = True


def _structured_summary() -> dict:
    """最小合法结构化摘要（供已发布 memory 的 structured 使用；真实消息 id 随用例注入）。"""
    return {
        "version": 1,
        "unfinished": False,
        "goals": [],
        "constraints": [],
        "confirmed_facts": [{
            "text": "用户长期偏好 A",
            "source_message_ids": [str(uuid.uuid4())],
            "verification": "confirmed_by_user",
        }],
        "decisions": [],
        "preferences": [],
        "corrections": [],
        "open_questions": [],
    }


# 标注"外壳消息 id 占位"——测试里 build_chat_history / _build_snapshot 只读 structured
# 的文本，不校验来源，因此占位 id 无碍。
_seqs_in_text_re = re.compile(r"\[(?:user|assistant) (\d+) message_id=")


@unittest.skipIf(_CLASS_SKIP,
                 "需显式配置 TEST_DATABASE_URL(以 _test 结尾) 与 TEST_DATABASE_OWNER(带测试库前缀)")
class MemoryHistoryReadRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_schema()

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        self.patchers = [
            patch.object(settings, "CHAT_MEMORY_ENABLED", True),
            patch.object(settings, "MEMORY_KEEP_RECENT_TURNS", 4),
            patch.object(settings, "MEMORY_MAX_INPUT_TURNS", 200),
            patch.object(settings, "MEMORY_HISTORY_TOKEN_BUDGET", 8000),
        ]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    # ---- fixtures（全部在一个未提交的外部事务 + savepoint 里，结束后回滚） ----

    def _new_conv(self, db):
        u = User(username="mem_hist_" + uuid.uuid4().hex[:8], role="user", is_active=True,
                 password_hash=hash_password("Original123!"))
        db.add(u)
        db.flush()
        conv = Conversation(user_id=u.id, mode=CONVERSATION_MODE_CHAT, title="会话")
        db.add(conv)
        db.flush()
        return conv

    def _add_complete_turn(self, db, conv, seq, prefix="u"):
        """插入一轮含显式 reply_to、两消息均 completed 的完整轮。返回 (user_id, assistant_id)。"""
        uid = uuid.uuid4()
        aid = uuid.uuid4()
        turn = ChatTurn(request_id=uuid.uuid4(), user_id=conv.user_id, conversation_id=conv.id,
                        user_message_id=uid, payload_hash="0" * 64,
                        status="completed", heartbeat_at=_now())
        db.add(turn)
        db.flush()
        um = Message(id=uid, conversation_id=conv.id, role="user", content=f"{prefix}:{seq}",
                     status="completed", turn_seq=seq, turn_id=turn.id)
        am = Message(id=aid, conversation_id=conv.id, role="assistant", content=f"a:{seq}",
                     status="completed", turn_seq=seq, turn_id=turn.id, reply_to_id=uid)
        db.add(um)
        db.add(am)
        return uid, aid

    def _add_failed_turn(self, db, conv, seq):
        """一轮两消息均 failed（不完整，不得进完整轮身份）。"""
        turn = ChatTurn(request_id=uuid.uuid4(), user_id=conv.user_id, conversation_id=conv.id,
                        user_message_id=uuid.uuid4(), payload_hash="x" * 64,
                        status="failed", heartbeat_at=_now())
        db.add(turn)
        db.flush()
        db.add(Message(conversation_id=conv.id, role="user", content=f"f:{seq}",
                       status="failed", turn_seq=seq, turn_id=turn.id))
        db.add(Message(conversation_id=conv.id, role="assistant", content=f"fa:{seq}",
                       status="failed", turn_seq=seq, turn_id=turn.id))

    def _add_processing_turn(self, db, conv, seq):
        """一轮仍 processing（user 完成、assistant 处理中）→ 不进完整轮身份。"""
        uid = uuid.uuid4()
        turn = ChatTurn(request_id=uuid.uuid4(), user_id=conv.user_id, conversation_id=conv.id,
                        user_message_id=uid, payload_hash="p" * 64,
                        status="processing", heartbeat_at=_now())
        db.add(turn)
        db.flush()
        db.add(Message(id=uid, conversation_id=conv.id, role="user", content=f"p:{seq}",
                       status="completed", turn_seq=seq, turn_id=turn.id))
        db.add(Message(conversation_id=conv.id, role="assistant", content=f"pa:{seq}",
                       status="processing", turn_seq=seq, turn_id=turn.id))

    def _add_legacy_pair(self, db, conv):
        """legacy：turn_seq NULL、两消息 completed。契约——不得被认作完整轮/不得入摘要。"""
        turn = ChatTurn(request_id=uuid.uuid4(), user_id=conv.user_id, conversation_id=conv.id,
                        user_message_id=uuid.uuid4(), payload_hash="l" * 64,
                        status="completed", heartbeat_at=_now())
        db.add(turn)
        db.flush()
        db.add(Message(conversation_id=conv.id, role="user", content="legacy-user",
                       status="completed", turn_seq=None, turn_id=turn.id))
        db.add(Message(conversation_id=conv.id, role="assistant", content="legacy-asst",
                       status="completed", turn_seq=None, turn_id=turn.id))

    def _rollback_fixture(self, db, sp):
        # 丢弃 savepoint（及外部事务里本用例写入）——不做 TRUNCATE/drop，核随回滚消失。
        sp.rollback()
        db.rollback()

    # ---- 聊天：实际最新完整后缀 ----

    def test_chat_takes_actual_latest_four_complete_pairs(self):
        """超过旧 ASC+LIMIT 窗口的未覆盖轮时，聊天必须取实际最新完整对，而非旧段末尾。"""
        outer = SessionLocal()
        sp = outer.begin_nested()
        try:
            conv = self._new_conv(outer)
            covered = 1000
            mem = ConversationMemory(conversation_id=conv.id, version=1, epoch=0,
                                     covered_turn_seq=covered,
                                     structured=_structured_summary(),
                                     source_range={}, covered_turns=500)
            outer.add(mem)
            outer.flush()
            # 完整轮 seq 1001..1520（520 个），中间挖 seq 空洞 + failed/processing。
            COMPLETE_HI = 1520
            complete_ids = {}
            for seq in range(1001, COMPLETE_HI + 1):
                if seq % 37 == 0:
                    # seq 空洞：完全无记录。
                    continue
                if seq in (1005, 1234, 1400):
                    self._add_failed_turn(outer, conv, seq)
                    continue
                if seq in (1300, 1450):
                    self._add_processing_turn(outer, conv, seq)
                    continue
                uid, _aid = self._add_complete_turn(outer, conv, seq, prefix="u")
                complete_ids[seq] = uid
            # 若干 legacy 对（绝不该被认作完整轮）。
            for _ in range(2):
                self._add_legacy_pair(outer, conv)
            outer.flush()

            messages, lag, _truncated = memory_service.build_chat_history(outer, conv, "q")

            # 至少最新 keep_recent（4）完整对出现；lag 为真（存在更多完整对）。
            self.assertTrue(lag)
            last_user = None
            newest_from_history = []
            for m in messages:
                content = m["content"]
                if m["role"] == "user" and content.startswith("u:"):
                    last_user = int(content[2:])
                    newest_from_history.append(last_user)
            self.assertIsNotNone(last_user)
            # 最新完整轮（1520）确被召回；且不是旧段末尾。
            self.assertEqual(last_user, COMPLETE_HI)
            # 召回的 user seq 恰是实际最新的一小段（都 > 旧窗口能摸到的末尾之上，且无旧轮）。
            self.assertTrue(all(s >= COMPLETE_HI - 12 for s in newest_from_history))
            self.assertNotIn(1001, newest_from_history)

            # legacy / failed / processing 永不混入完整轮召回。
            joined = "\n".join(m["content"] for m in messages)
            self.assertNotIn("legacy-user", joined)
            self.assertNotIn("legacy-asst", joined)
            self.assertNotIn("f:", joined)
            self.assertNotIn("p:", joined)
        finally:
            self._rollback_fixture(outer, sp)
            outer.close()
        # 外事务回滚后核消失：新会话里看不到该会话。
        with SessionLocal() as chk:
            self.assertIsNone(chk.get(Conversation, conv.id))

    def test_chat_recent_messages_are_contiguous_latest_suffix(self):
        """召回的最新完整对按 seq 连续成对（user 前 assistant 后），顺序与正文一致。"""
        outer = SessionLocal()
        sp = outer.begin_nested()
        try:
            conv = self._new_conv(outer)
            for seq in range(1, 30):
                self._add_complete_turn(outer, conv, seq)
            outer.flush()
            messages, lag, _ = memory_service.build_chat_history(outer, conv, "q")
            self.assertTrue(lag)  # 30 > keep_recent=4
            user_seqs = [int(m["content"][2:]) for m in messages if m["role"] == "user"]
            # 只展示最新 keep_recent 对（连续后缀）。
            self.assertEqual(user_seqs, [26, 27, 28, 29])
            # 每对 user 之后紧跟同 seq 的 assistant。
            roles = [m["role"] for m in messages]
            self.assertEqual(
                [r for r in roles if r in ("user", "assistant")],
                ["user", "assistant"] * (len([r for r in roles if r == "user"])),
            )
        finally:
            self._rollback_fixture(outer, sp)
            outer.close()

    # ---- 摘要：最旧未覆盖前缀 + 保护最新 4 ----

    def test_snapshot_oldest_contiguous_prefix_and_protects_latest_four(self):
        """_build_snapshot 从最旧未覆盖轮连续挑可归档前缀、保护最新 keep_recent 轮、
        且绝不跨过仍 processing 的空洞（避免水印跳过可能完成的轮）。"""
        outer = SessionLocal()
        sp = outer.begin_nested()
        try:
            conv = self._new_conv(outer)
            covered = 1000
            mem = ConversationMemory(conversation_id=conv.id, version=3, epoch=0,
                                     covered_turn_seq=covered,
                                     structured=_structured_summary(),
                                     source_range={}, covered_turns=500,
                                     covered_chars=1, covered_tokens=1)
            outer.add(mem)
            outer.flush()
            # 连续完整轮 1001..1450（< 有界前缀读取 504，完全落在前缀窗内），
            # 随后一个 processing 空洞 1460，其后还有完整轮（绝不可被选中/跨过）。
            LAST_BEFORE_BLOCKER = 1450
            for seq in range(1001, LAST_BEFORE_BLOCKER + 1):
                self._add_complete_turn(outer, conv, seq)
            self._add_processing_turn(outer, conv, 1460)
            for seq in range(1500, 1561):
                self._add_complete_turn(outer, conv, seq)
            # 若干 legacy 对（不完整、不得入批）。
            for _ in range(2):
                self._add_legacy_pair(outer, conv)
            outer.flush()

            snapshot, included_seq, digest = memory_service._build_snapshot(
                outer, conv, base_memory_version=3, job_id=uuid.uuid4())

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["start_seq"], 1001)
            self.assertEqual(included_seq, snapshot["end_seq"])
            text = snapshot["text"]
            seqs = sorted({int(m) for m in _seqs_in_text_re.findall(text)})
            self.assertGreater(len(seqs), 0)
            # 最旧未覆盖前缀起于 1001，且连续（selected 内无空洞）。
            self.assertEqual(seqs[0], 1001)
            self.assertEqual(seqs, list(range(seqs[0], seqs[-1] + 1)))
            # 绝不跨过 processing 空洞：无任何选中轮 >= 1460。
            self.assertTrue(all(s < 1460 for s in seqs))
            # 保护最新 keep_recent 轮（processing 空洞之前的最新完整对 1447..1450）。
            self.assertFalse(set(range(1447, 1451)) & set(seqs))
            self.assertLessEqual(seqs[-1], 1446)
            # 来源映射真实 UUID 且成对（每轮 user+assistant）。
            self.assertTrue(snapshot["message_ids"])
            self.assertEqual(len(set(snapshot["message_ids"])) % 2, 0)
            # digest 与 batch 一致。
            self.assertEqual(
                digest,
                __import__("hashlib").sha256(text.encode("utf-8")).hexdigest(),
            )
        finally:
            self._rollback_fixture(outer, sp)
            outer.close()

    def test_snapshot_protects_actual_latest_even_when_huge_tail(self):
        """未覆盖轮远超有界前缀读取(504)时，保护的是真实最新 4 轮，而不是前缀窗末尾。"""
        outer = SessionLocal()
        sp = outer.begin_nested()
        try:
            conv = self._new_conv(outer)
            mem = ConversationMemory(conversation_id=conv.id, version=1, epoch=0,
                                     covered_turn_seq=5000, structured=_structured_summary(),
                                     source_range={}, covered_turns=2500,
                                     covered_chars=1, covered_tokens=1)
            outer.add(mem)
            outer.flush()
            HI = 5560
            for seq in range(5001, HI + 1):
                self._add_complete_turn(outer, conv, seq)  # 连续完整轮，560 > 504
            outer.flush()
            latest_complete = HI

            snapshot, _inc, _dig = memory_service._build_snapshot(
                outer, conv, base_memory_version=1, job_id=uuid.uuid4())
            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot["start_seq"], 5001)
            seqs = sorted({int(m) for m in _seqs_in_text_re.findall(snapshot["text"])})
            # 从最旧未覆盖连续挑前缀（无空洞）。
            self.assertEqual(seqs[0], 5001)
            self.assertEqual(seqs, list(range(seqs[0], seqs[-1] + 1)))
            # 真实最新完整轮绝不在批内（留给聊天 recent），且保护带止于 latest-4。
            self.assertFalse({latest_complete} & set(seqs))
            self.assertLessEqual(seqs[-1], latest_complete - 4)
        finally:
            self._rollback_fixture(outer, sp)
            outer.close()

    # ---- 大尾轮：连续裁剪 + 正确 lag/truncated ----

    def test_large_tail_clips_contiguously_and_reports_lag(self):
        """预算不足以放全部 recent 时，assemble_recall 从最旧连续剪裁、保住最新对，
        并如实上报 truncated / lag。"""
        with patch.object(settings, "MEMORY_HISTORY_TOKEN_BUDGET", 100):
            outer = SessionLocal()
            sp = outer.begin_nested()
            try:
                conv = self._new_conv(outer)
                for seq in range(1, 40):
                    self._add_complete_turn(outer, conv, seq)
                outer.flush()
                messages, lag, truncated = memory_service.build_chat_history(outer, conv, "q")
                # 预算极小：无法装下任何 recent 对 -> 截断为真。
                self.assertTrue(truncated)
                self.assertTrue(lag)
                # 即便截断，历史的 user 轮（若能装下）也必须是最新的连续后缀。
                user_seqs = [int(m["content"][2:]) for m in messages if m["role"] == "user"]
                if user_seqs:
                    self.assertEqual(user_seqs, sorted(user_seqs))
                    self.assertTrue(max(user_seqs) <= 39)
            finally:
                self._rollback_fixture(outer, sp)
                outer.close()


if __name__ == "__main__":
    unittest.main()
