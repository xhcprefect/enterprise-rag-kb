"""成员/令牌/项目快照在发布锁等待期间被撤销时，绝不写出成功回答。

与 ``test_publication_concurrency`` 的 race 骨架同型（真实 PG 锁 + ``wait_for_blocking``
的精确阻塞证据），但被锁对象是 A2 新增的授权根：成员行（knowledge_base_members 的
读取授权由 User 行锁后的新鲜查询复核）、令牌版本（User 行）与项目 Source/Snapshot。
所有用例使用确定性替身，不调用真实模型。
"""
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event

from sqlalchemy import select, text

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL
from tests._pg_lock_wait import wait_for_blocking

if TEST_URL:
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, engine
    from app.models import (
        ChatTurn,
        Conversation,
        KnowledgeBase,
        KnowledgeBaseMember,
        Message,
        ProjectChunk,
        ProjectFileVersion,
        ProjectSnapshot,
        ProjectSnapshotFile,
        ProjectSource,
        User,
    )
    from app.services import chat_turns
    from app.services.answer_sources import SOURCE_SNAPSHOT_VERSION

TOKEN = 3
MANIFEST = "a" * 64
CONFIG = "b" * 64
TABLES = ("users, knowledge_bases, knowledge_base_members, documents, document_chunks, "
          "conversations, chat_turns, messages, answer_feedbacks, audit_logs, model_usage, "
          "project_sources, project_snapshots, project_file_versions, project_snapshot_files, "
          "project_chunks")


def _empty_snapshot():
    return {"source_snapshot_version": SOURCE_SNAPSHOT_VERSION,
            "supplied_sources": [], "history_sources": []}


def _project_snapshot_trace(supplied):
    return {"source_snapshot_version": SOURCE_SNAPSHOT_VERSION,
            "supplied_sources": list(supplied), "history_sources": []}


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离发布门禁测试")
class KbMemberPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            self.owner = User(username="pub_owner", role="user", is_active=True,
                              password_hash="x")
            self.member = User(username="pub_member", role="user", is_active=True,
                               password_hash="x", token_version=TOKEN)
            db.add_all([self.owner, self.member])
            db.flush()
            # 私有上传库 A（member 为 reader）与私有项目库 P（member 为 reader）。
            self.kb_a = KnowledgeBase(name="pub-a", collection_name="puba_" + uuid.uuid4().hex,
                                      owner_id=self.owner.id, is_public=False, kind="upload")
            self.project_kb = KnowledgeBase(name="pub-p", collection_name="pubp_" + uuid.uuid4().hex,
                                            owner_id=self.owner.id, is_public=False, kind="project")
            db.add_all([self.kb_a, self.project_kb])
            db.flush()
            db.add(KnowledgeBaseMember(kb_id=self.kb_a.id, user_id=self.member.id, role="reader"))
            db.add(KnowledgeBaseMember(kb_id=self.project_kb.id, user_id=self.member.id, role="reader"))
            db.commit()
            self.member_id, self.owner_id = self.member.id, self.owner.id
            self.kb_a_id, self.project_kb_id = self.kb_a.id, self.project_kb.id

    # ---------- helpers ----------

    def claim(self, *, kb_id=None, snapshot_id=None):
        with SessionLocal() as db:
            return chat_turns.claim_turn(
                db, user_id=self.member_id, conversation_id=None,
                kb_id=kb_id if kb_id is not None else self.kb_a_id, mode="rag",
                question="问题", top_k=6, request_id=uuid.uuid4(),
                auth_token_version=TOKEN, project_snapshot_id=snapshot_id)

    def complete_in(self, db, reservation, trace=None):
        return chat_turns.complete_turn(db, reservation, "回答", [], 1,
                                        _empty_snapshot() if trace is None else trace)

    def add_project(self):
        """可发布的最小项目来源：ready 快照 + 映射文件版本 + embedded chunk。"""
        with SessionLocal() as db:
            source = ProjectSource(kb_id=self.project_kb_id, name="src", root_key="root",
                                   relative_dir="", root_fingerprint="f" * 64,
                                   created_by=self.owner_id)
            db.add(source)
            db.flush()
            snapshot = ProjectSnapshot(source_id=source.id, status="ready",
                                       manifest_fingerprint=MANIFEST,
                                       config_fingerprint=CONFIG)
            db.add(snapshot)
            db.flush()
            version = ProjectFileVersion(source_id=source.id, relative_path="src/main.py",
                                         content_sha256="c" * 64, blob_path="/tmp/blob",
                                         parse_done=True, parse_status="ready",
                                         config_fingerprint=CONFIG)
            db.add(version)
            db.flush()
            db.add(ProjectSnapshotFile(snapshot_id=snapshot.id, relative_path="src/main.py",
                                       file_version_id=version.id))
            db.add(ProjectChunk(file_version_id=version.id, chunk_index=0, content="代码",
                                embedded=True))
            source.active_snapshot_id = snapshot.id
            db.commit()
            return {"source_id": source.id, "snapshot_id": snapshot.id,
                    "file_version_id": version.id}

    def project_citation(self, project):
        return {"source_kind": "project", "document_id": str(project["file_version_id"]),
                "file_version_id": str(project["file_version_id"]),
                "snapshot_id": str(project["snapshot_id"]),
                "source_id": str(project["source_id"]),
                "relative_path": "src/main.py", "manifest_fingerprint": MANIFEST,
                "config_fingerprint": CONFIG, "chunk_index": 0}

    def revoke_member(self, kb_id=None):
        with SessionLocal() as db:
            db.execute(text("DELETE FROM knowledge_base_members WHERE kb_id = :kb "
                            "AND user_id = :user"),
                       {"kb": kb_id or self.kb_a_id, "user": self.member_id})
            db.commit()

    def revoke_project_source(self, project):
        with SessionLocal() as db:
            db.execute(text("UPDATE project_sources SET revoked_at = now(), "
                            "active_snapshot_id = NULL WHERE id = :sid"),
                       {"sid": project["source_id"]})
            db.commit()

    def assert_no_answer(self, reservation):
        """发布失败后：无成功 assistant 消息、轮次仍 processing，最后清租约。"""
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant")))
            self.assertEqual(db.get(ChatTurn, reservation.turn_id).status, "processing")
            chat_turns.cancel_request(db, reservation.request_id, self.member_id)

    def assert_published(self, reservation, answer="回答"):
        with SessionLocal() as db:
            row = db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant"))
            self.assertIsNotNone(row)
            self.assertEqual(row.content, answer)
            self.assertEqual(db.get(ChatTurn, reservation.turn_id).status, "completed")

    def race(self, hold, act, *, while_blocked=None):
        """与 test_publication_concurrency 相同的骨架：精确阻塞后注入撤销。"""
        armed, actor_ready, release = Event(), Event(), Event()
        pids = {}
        pool = ThreadPoolExecutor(max_workers=2)
        actor = None

        def holder():
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["holder"] = db.scalar(text("SELECT pg_backend_pid()"))
                hold(db)
                armed.set()
                self.assertTrue(release.wait(20), "测试必须有界释放持锁事务")
                db.commit()

        def action():
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["actor"] = db.scalar(text("SELECT pg_backend_pid()"))
                actor_ready.set()
                try:
                    return act(db)
                except chat_turns.ChatLeaseLost as exc:
                    db.rollback()
                    return exc

        held = pool.submit(holder)
        try:
            self.assertTrue(armed.wait(8), "holder 未就绪")
            actor = pool.submit(action)
            self.assertTrue(actor_ready.wait(8), "actor 未就绪")
            self.assertTrue(wait_for_blocking(engine, pids["holder"], pids["actor"]),
                            "必须观察到本次两个 PID 的真实阻塞关系")
            self.assertFalse(actor.done())
            if while_blocked:
                while_blocked()
        finally:
            release.set()
            try:
                held.result(timeout=20)
                if actor is not None:
                    actor.result(timeout=20)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        return actor.result()

    # ---------- 成员撤权 × 发布 ----------

    def test_member_revoked_while_publication_waits_never_publishes(self):
        """发布等 User 行锁时成员被撤权：锁后新鲜复核必须拒绝。"""
        reservation = self.claim()

        def hold(db):
            db.scalar(select(User.id).where(User.id == self.member_id).with_for_update())

        result = self.race(hold, lambda db: self.complete_in(db, reservation),
                           while_blocked=self.revoke_member)
        self.assertIsInstance(result, chat_turns.ChatLeaseLost)
        self.assert_no_answer(reservation)

    def test_member_can_publish_while_membership_still_valid(self):
        """未撤权的成员正常发布成功（同一套锁路径的阳性对照）。"""
        reservation = self.claim()
        with SessionLocal() as db:
            self.complete_in(db, reservation)
        self.assert_published(reservation)

    # ---------- 令牌失效 × 发布 ----------

    def test_token_invalidated_while_publication_waits_never_publishes(self):
        """发布等 User 行锁时 token_version 被刷新：锁后核对认证快照失败。"""
        reservation = self.claim()

        def hold(db):
            user = db.scalar(select(User).where(User.id == self.member_id).with_for_update())
            user.token_version += 1
            db.flush()

        result = self.race(hold, lambda db: self.complete_in(db, reservation))
        self.assertIsInstance(result, chat_turns.ChatLeaseLost)
        self.assert_no_answer(reservation)

    def test_token_bump_before_publication_is_rejected_and_lease_stops(self):
        """发布前已被刷新的 token：check_lease 先失效，complete_turn 拒绝。"""
        reservation = self.claim()
        with SessionLocal() as db:
            db.get(User, self.member_id).token_version += 1
            db.commit()
        self.assertFalse(chat_turns.check_lease(reservation, renew=False))
        with SessionLocal() as db:
            with self.assertRaises(chat_turns.ChatLeaseLost):
                self.complete_in(db, reservation)
        self.assert_no_answer(reservation)

    # ---------- 项目快照撤销 × 发布 ----------

    def test_project_source_revoked_while_publication_waits_never_publishes(self):
        """发布等 ProjectSource 行锁时成员被撤权：锁后新鲜 ACL 复核必须失败。

        发布按 Source -> Snapshot 顺序取锁，holder 先锁 Source 才能真实阻塞发布。
        撤销动作不能碰 holder 持有的 Source 行（否则撤销自己会被阻塞 15s），改为
        撤掉 KB 成员授权：发布锁后的 ``visible_kb_predicate`` 复核一样会拒绝。
        """
        project = self.add_project()
        reservation = self.claim(kb_id=self.project_kb_id, snapshot_id=project["snapshot_id"])
        citation = self.project_citation(project)
        trace = _project_snapshot_trace([citation])

        def hold(db):
            db.scalar(select(ProjectSource.id).where(
                ProjectSource.id == project["source_id"]).with_for_update())

        result = self.race(hold, lambda db: self.complete_in(db, reservation, trace),
                           while_blocked=lambda: self.revoke_member(self.project_kb_id))
        self.assertIsInstance(result, chat_turns.ChatLeaseLost)
        self.assert_no_answer(reservation)

    def test_project_source_revoked_before_publication_is_rejected(self):
        """发布前已撤销的项目源：check_lease 先失效，complete_turn 拒绝。"""
        project = self.add_project()
        reservation = self.claim(kb_id=self.project_kb_id, snapshot_id=project["snapshot_id"])
        citation = self.project_citation(project)
        self.revoke_project_source(project)
        self.assertFalse(chat_turns.check_lease(reservation, renew=False))
        with SessionLocal() as db:
            with self.assertRaises(chat_turns.ChatLeaseLost):
                self.complete_in(db, reservation, _project_snapshot_trace([citation]))
        self.assert_no_answer(reservation)

    def test_project_member_publishes_on_fixed_snapshot_while_authorized(self):
        """有授权的成员按固定快照发布成功（阳性对照，含项目来源校验链）。"""
        project = self.add_project()
        reservation = self.claim(kb_id=self.project_kb_id, snapshot_id=project["snapshot_id"])
        citation = self.project_citation(project)
        with SessionLocal() as db:
            self.complete_in(db, reservation, _project_snapshot_trace([citation]))
        self.assert_published(reservation, "回答")

    def test_member_revocation_also_blocks_project_publication(self):
        """项目库成员身份被撤（非源撤销）同样让发布失败。"""
        project = self.add_project()
        reservation = self.claim(kb_id=self.project_kb_id, snapshot_id=project["snapshot_id"])
        self.revoke_member(kb_id=self.project_kb_id)
        with SessionLocal() as db:
            with self.assertRaises(chat_turns.ChatLeaseLost):
                self.complete_in(db, reservation,
                                 _project_snapshot_trace([self.project_citation(project)]))
        self.assert_no_answer(reservation)

    def test_source_revoked_by_holder_transaction_itself_never_publishes(self):
        """持锁事务在释放前把源标记为撤销：发布的锁后 ready/撤销复核失败。

        holder 锁住 Source 行后直接在同一事务里写 revoked_at 再提交——发布被
        Source 行锁阻塞、锁后读到的是已撤销的行（不是先撤销再发布的窄窗口）。
        """
        project = self.add_project()
        reservation = self.claim(kb_id=self.project_kb_id, snapshot_id=project["snapshot_id"])
        citation = self.project_citation(project)
        trace = _project_snapshot_trace([citation])

        def hold(db):
            source = db.scalar(select(ProjectSource).where(
                ProjectSource.id == project["source_id"]).with_for_update())
            source.revoked_at = datetime.now(timezone.utc)
            source.active_snapshot_id = None
            db.flush()

        result = self.race(hold, lambda db: self.complete_in(db, reservation, trace))
        self.assertIsInstance(result, chat_turns.ChatLeaseLost)
        self.assert_no_answer(reservation)

    # ---------- 租约检查对真实并发撤销的反应 ----------

    def test_check_lease_reads_fresh_acl_state_after_holder_commit(self):
        """holder 在锁内提交撤权后，check_lease 的独立事务必须读到新状态。

        本用例只覆盖“提交后的新鲜读取”：check_lease 在 holder 释放锁并提交
        之后才调用，不构造 check_lease 自身的锁等待。生成等待中的实时中断由
        ``test_kb_member_reading.test_member_revoked_while_model_waits_stops_stream_with_lease_lost``
        的真实 SSE 链路覆盖。
        """
        reservation = self.claim()
        self.assertTrue(chat_turns.check_lease(reservation, renew=True))

        holder_release = Event()
        holder_ready = Event()
        pool = ThreadPoolExecutor(max_workers=1)

        def holder():
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                db.scalar(select(User.id).where(User.id == self.member_id).with_for_update())
                holder_ready.set()
                self.assertTrue(holder_release.wait(20), "测试必须有界释放持锁事务")
                db.commit()

        held = pool.submit(holder)
        try:
            self.assertTrue(holder_ready.wait(8), "holder 未就绪")
            # 撤权事务不触碰 User/KB 行锁，因此不会与 holder 行锁互相阻塞。
            self.revoke_member()
        finally:
            holder_release.set()
            held.result(timeout=20)
            pool.shutdown(wait=True, cancel_futures=True)
        self.assertFalse(chat_turns.check_lease(reservation, renew=False))
        self.assert_no_answer(reservation)


if __name__ == "__main__":
    unittest.main()
