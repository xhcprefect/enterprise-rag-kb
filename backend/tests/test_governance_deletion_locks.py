"""删除 / 项目撤销 / 取消同步 的统一锁序与"锁等待"验收（真实 PostgreSQL）。

覆盖 T3 删除路径的三条不变量：

1. **锁序**：``User -> KnowledgeBase(UUID) -> ProjectSource(UUID) ->
   ProjectSnapshot(UUID) -> Conversation(UUID) -> ChatTurn(UUID) ->
   Message(UUID) -> Document(UUID)``，与最终发布事务一致，绝不先锁自己
   会话再隐式拿 KB。
2. **锁等待可观测**：并发的行锁会让删除/撤销真实阻塞。持锁线程先拿目标行锁
   （``threading.Event`` 对齐起点），再用 ``Future.result(timeout)`` 做**有界**
   等待，并额外在 ``pg_locks`` 里确认"确实有人在等"，不靠 ``sleep`` 猜。
   释放后必须在界内完成 —— 否则判失败。
3. **受影响集合有限**：目标用户锁只让"实际会变"的行阻塞；与本次删除无关的
   行（他人独立会话等）必须能被 ``FOR UPDATE NOWAIT`` 立刻拿到。

数据库门沿用 ``tests._memory_project_test_gate``：未显式提供 owner 标记的
loopback ``*_test`` 库时整类 skip，绝不连接业务库。
"""
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import HTTPException
from tests._pg_lock_wait import wait_for_blocking
from datetime import datetime, timezone
from pathlib import Path

from unittest.mock import Mock, patch

from sqlalchemy import delete, event, select, text
from sqlalchemy.dialects import postgresql

from tests import _memory_project_test_gate as gate

from app.api.v1 import knowledge, users as users_api
from app.db import session as db_session
from app.db.init_db import init_db
from app.models import (
    AuditLog,
    ChatTurn,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    ProjectSnapshot,
    ProjectSource,
    UploadSession,
    User,
)
from app.services import document_governance, project_sources, upload, vector_store

WAIT_TIMEOUT = 20.0


def _all_waits(engine) -> list[tuple]:
    """诊断用：当前所有未授予的锁（关系名/模式），只在断言失败时读取。"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT coalesce(c.relname, l.locktype), l.mode FROM pg_locks l "
            "LEFT JOIN pg_class c ON c.oid = l.relation WHERE NOT l.granted"
        )).all()
        conn.rollback()
    return [tuple(row) for row in rows]


class DocumentDeletionLockSqlTests(unittest.TestCase):
    """不依赖数据库也必须验证真实路由发出的 PostgreSQL 锁序。"""

    def test_delete_document_locks_kb_before_document_and_cleans_file(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "replacement.txt"
            path.write_text("payload", encoding="utf-8")
            kb_id, doc_id = uuid.uuid4(), uuid.uuid4()
            kb = SimpleNamespace(kind="upload", collection_name="offline", name="kb")
            doc = SimpleNamespace(id=doc_id, filename=path.name, stored_path=str(path))
            db = Mock()
            db.scalar.side_effect = [kb, doc]
            with patch.object(knowledge, "delete_document_vectors") as vectors, \
                    patch.object(knowledge, "write_audit"):
                knowledge.delete_document(kb_id, doc_id, _Request(), db, SimpleNamespace())
            statements = [call.args[0] for call in db.scalar.call_args_list]
            self.assertEqual([stmt.get_final_froms()[0].name for stmt in statements],
                             ["knowledge_bases", "documents"])
            for statement in statements:
                sql = str(statement.compile(dialect=postgresql.dialect()))
                self.assertTrue(sql.endswith("FOR UPDATE"), sql)
                self.assertTrue(statement.get_execution_options().get("populate_existing"))
            vectors.assert_called_once_with("offline", str(doc_id), db=db)
            db.delete.assert_called_once_with(doc)
            db.commit.assert_called_once()
            self.assertFalse(path.exists())

    def test_delete_document_still_rejects_project_before_document_lookup(self):
        db = Mock()
        db.scalar.return_value = SimpleNamespace(kind="project")
        with self.assertRaises(HTTPException) as error:
            knowledge.delete_document(uuid.uuid4(), uuid.uuid4(), _Request(), db, SimpleNamespace())
        self.assertEqual(error.exception.status_code, 400)
        db.scalar.assert_called_once()
        db.delete.assert_not_called()
        db.commit.assert_not_called()


@unittest.skipIf(
    gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
    "需要显式 owned PostgreSQL 测试库",
)
class DeletionLockOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        init_db(create_admin=False)

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        self.engine = db_session.engine
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.created_kbs: list[uuid.UUID] = []
        self.created_users: list[uuid.UUID] = []
        self.addCleanup(self._cleanup)

    # ---------------- fixture ----------------

    def _cleanup(self):
        with db_session.SessionLocal() as db:
            if self.created_kbs:
                db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(self.created_kbs)))
            if self.created_users:
                db.execute(delete(User).where(User.id.in_(self.created_users)))
            db.commit()

    def _user(self, db, *, admin=False) -> User:
        user = User(
            username="lock_" + uuid.uuid4().hex,
            role="admin" if admin else "user",
            is_active=True,
            password_hash="test-only-no-login",
        )
        db.add(user)
        db.flush()
        self.created_users.append(user.id)
        return user

    def _kb(self, db, owner: User, *, kind: str = "upload") -> KnowledgeBase:
        kb = KnowledgeBase(
            name="lockkb_" + uuid.uuid4().hex,
            description="",
            collection_name="lockcol_" + uuid.uuid4().hex,
            owner_id=owner.id,
            is_public=False,
            kind=kind,
        )
        db.add(kb)
        db.flush()
        self.created_kbs.append(kb.id)
        return kb

    def _conversation(self, db, user: User, kb: KnowledgeBase) -> Conversation:
        conv = Conversation(user_id=user.id, kb_id=kb.id, mode="rag", title="t",
                            document_scope=[], scope_revision=0)
        db.add(conv)
        db.flush()
        return conv

    def _turn_with_message(self, db, user: User, conv: Conversation) -> ChatTurn:
        turn = ChatTurn(
            request_id=uuid.uuid4(), user_id=user.id, conversation_id=conv.id,
            user_message_id=uuid.uuid4(), payload_hash="0" * 64, status="completed",
            heartbeat_at=datetime.now(timezone.utc),
        )
        db.add(turn)
        db.flush()
        db.add(Message(conversation_id=conv.id, turn_id=turn.id, role="user",
                       content="q", status="completed"))
        db.flush()
        return turn

    def _document(self, db, kb: KnowledgeBase, uploader: User, path: Path) -> Document:
        doc = Document(
            kb_id=kb.id, filename=path.name, file_ext=path.suffix,
            file_size=path.stat().st_size, stored_path=str(path),
            status="ready", uploaded_by=uploader.id,
        )
        db.add(doc)
        db.flush()
        return doc

    def _project_source(self, db, kb: KnowledgeBase, creator: User) -> ProjectSource:
        source = ProjectSource(
            kb_id=kb.id, name="src_" + uuid.uuid4().hex, root_key="fixture",
            relative_dir="", root_fingerprint="0" * 64, created_by=creator.id,
        )
        db.add(source)
        db.flush()
        return source

    # ---------------- helpers ----------------

    def _run_while_held(self, acquire, act, *, relation: str, probe=None, commit_holder=False,
                        on_release=None):
        """确认 actor 等待 holder 后才释放；on_release 可让持锁方继续真实业务操作。"""
        armed, acting_ready, release = threading.Event(), threading.Event(), threading.Event()
        pids = {}
        pool = ThreadPoolExecutor(max_workers=2)
        acting = None

        def limit_statements(_session, _transaction, connection):
            # complete_upload 分阶段 commit；每个新事务都设超时，不能只保护阶段一。
            connection.execute(text("SET LOCAL statement_timeout = '15s'"))

        def hold():
            with self.engine.connect() as connection, db_session.SessionLocal(bind=connection) as holder:
                event.listen(holder, "after_begin", limit_statements)
                pids["holder"] = holder.scalar(text("SELECT pg_backend_pid()"))
                acquire(holder)
                armed.set()
                if not release.wait(timeout=WAIT_TIMEOUT):
                    raise AssertionError("调用方未在限定时间内释放行锁")
                if on_release is not None:
                    on_release(holder)
                if commit_holder:
                    holder.commit()
                else:
                    holder.rollback()

        def run_action():
            # 固定物理连接：阶段间 commit 后 actor PID 仍与观测到的 PID 一致。
            with self.engine.connect() as connection, db_session.SessionLocal(bind=connection) as db:
                event.listen(db, "after_begin", limit_statements)
                pids["actor"] = db.scalar(text("SELECT pg_backend_pid()"))
                acting_ready.set()
                return act(db)

        holder_future = pool.submit(hold)
        try:
            self.assertTrue(armed.wait(timeout=10), "持锁线程未能取得目标行锁")
            acting = pool.submit(run_action)
            self.assertTrue(acting_ready.wait(timeout=10), "删除连接未就绪")
            self.assertTrue(wait_for_blocking(self.engine, pids["holder"], pids["actor"]),
                            f"未观察到本次 {relation} 锁等待；当前等待: {_all_waits(self.engine)}")
            self.assertFalse(acting.done())
            if probe is not None:
                probe()
        finally:
            release.set()
            try:
                holder_future.result(timeout=WAIT_TIMEOUT)
                if acting is not None:
                    acting.result(timeout=WAIT_TIMEOUT)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        return acting.result()

    # ---------------- 分片完成 vs 用户删除 ----------------

    def test_complete_upload_waits_for_deleted_uploader_in_own_kb(self):
        self._complete_upload_racing_user_delete(owned=True)

    def test_complete_upload_waits_for_deleted_uploader_in_other_owners_kb(self):
        self._complete_upload_racing_user_delete(owned=False)

    def _complete_upload_racing_user_delete(self, *, owned):
        with patch.object(knowledge.settings, "UPLOAD_DIR", self.temp.name):
            with db_session.SessionLocal() as db:
                admin = self._user(db, admin=True)
                uploader = self._user(db, admin=True)
                owner = uploader if owned else self._user(db)
                kb = self._kb(db, owner)
                session = UploadSession(
                    kb_id=kb.id, created_by=uploader.id, fingerprint=uuid.uuid4().hex,
                    filename="payload.txt", file_ext=".txt", file_size=7,
                    chunk_size=7, total_chunks=1, received=[0], status="uploading",
                )
                db.add(session)
                db.flush()
                upload.save_chunk(session, 0, b"payload")
                claimed_id = upload.claimed_document_id(session)
                ids = dict(admin=admin.id, uploader=uploader.id, owner=owner.id,
                           kb=kb.id, session=session.id, document=claimed_id)
                db.commit()

            def acquire(holder):
                holder.scalar(select(User.id).where(User.id == ids["uploader"]).with_for_update())

            def complete(db):
                # 也覆盖同一 Session 中的认证对象被阶段提交过期的情形。
                db.expire_on_commit = True
                user = db.get(User, ids["uploader"])
                try:
                    knowledge.complete_upload(ids["kb"], ids["session"], _Request(), db, user)
                except HTTPException as exc:
                    return exc.status_code
                return 200

            def delete_after_wait(holder):
                users_api.delete_user(ids["uploader"], _Request(), holder,
                                      holder.get(User, ids["admin"]))

            def probe():
                # 等 User 时不可先拿下层锁；否则删除会在 KB 或 FK SET NULL 会话上成环。
                with db_session.SessionLocal() as db:
                    db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == ids["kb"])
                              .with_for_update(nowait=True))
                    db.scalar(select(UploadSession.id).where(UploadSession.id == ids["session"])
                              .with_for_update(nowait=True))
                    db.rollback()

            status = self._run_while_held(
                acquire, complete, relation="users", probe=probe,
                on_release=delete_after_wait, commit_holder=True,
            )
            self.assertEqual(status, 401)
            with db_session.SessionLocal() as db:
                self.assertIsNone(db.get(User, ids["uploader"]))
                kb = db.get(KnowledgeBase, ids["kb"])
                self.assertEqual(kb.owner_id, None if owned else ids["owner"])
                session = db.get(UploadSession, ids["session"])
                self.assertIsNone(session.created_by)
                self.assertEqual(session.status, "uploading")
                self.assertIsNone(db.scalar(select(Document.id).where(Document.kb_id == ids["kb"])))
            self.assertFalse(upload.final_path(ids["kb"], ids["document"], ".txt").exists())
            self.assertFalse(upload.tmp_dir(ids["session"]).exists())
            self.assertFalse([path for path in Path(self.temp.name).rglob("*") if path.is_file()])

    # ---------------- 用户删除 ----------------

    def test_delete_user_waits_on_target_conversation_and_leaves_unrelated_rows_free(self):
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            target = self._user(db)
            other = self._user(db)
            target_kb = self._kb(db, target)
            other_kb = self._kb(db, other)
            # 目标在他人 KB 里的会话（会随用户 CASCADE 删除）。
            target_conv = self._conversation(db, target, other_kb)
            self._turn_with_message(db, target, target_conv)
            # 目标 KB 下他人的会话（保留，但 kb 属主变更需与发布锁互斥）。
            foreign_conv = self._conversation(db, other, target_kb)
            self._turn_with_message(db, other, foreign_conv)
            # 完全无关的行：他人、他库、他人会话。
            unrelated_kb = self._kb(db, other)
            unrelated_conv = self._conversation(db, other, unrelated_kb)
            db.commit()
            ids = dict(target=target.id, target_conv=target_conv.id,
                       foreign_conv=foreign_conv.id, unrelated_conv=unrelated_conv.id,
                       target_kb=target_kb.id, admin=admin.id)

        def acquire(holder):
            holder.execute(
                select(Conversation).where(Conversation.id == ids["target_conv"])
                .with_for_update()
            ).scalar_one()

        def do_delete(db):
            admin_row = db.get(User, ids["admin"])
            users_api.delete_user(ids["target"], _Request(), db, admin_row)

        def probe():
            # 有限集合：与本次删除无关的行必须能立刻加锁（NOWAIT 不阻塞）。
            with db_session.SessionLocal() as session:
                session.execute(
                    select(Conversation).where(Conversation.id == ids["unrelated_conv"])
                    .with_for_update(nowait=True)
                ).scalar_one()
                session.rollback()

        self._run_while_held(acquire, do_delete, relation="conversations", probe=probe)

        with db_session.SessionLocal() as db:
            self.assertIsNone(db.get(User, ids["target"]))
            # 他人会话保留，且在目标 KB 里（kb 属主被置空，库本身保留）。
            self.assertIsNotNone(db.get(Conversation, ids["foreign_conv"]))
            self.assertIsNotNone(db.get(KnowledgeBase, ids["target_kb"]))
            self.assertIsNone(db.get(KnowledgeBase, ids["target_kb"]).owner_id)
            # 目标的会话与其中消息已随 CASCADE 删除。
            self.assertIsNone(db.get(Conversation, ids["target_conv"]))

    def test_delete_user_releases_fk_references_of_documents_and_sources(self):
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            target = self._user(db)
            other = self._user(db)
            other_kb = self._kb(db, other)
            path = Path(self.temp.name) / "doc.txt"
            path.write_text("payload", encoding="utf-8")
            doc = self._document(db, other_kb, target, path)          # 目标在他人库上传
            project_kb = self._kb(db, other, kind="project")
            source = self._project_source(db, project_kb, target)      # 目标创建的源
            snap = ProjectSnapshot(source_id=source.id, status="ready")
            db.add(snap)
            db.commit()
            ids = dict(admin=admin.id, target=target.id, doc=doc.id,
                       source=source.id, snap=snap.id, kb=other_kb.id, owner=other.id)

        with db_session.SessionLocal() as db:
            users_api.delete_user(ids["target"], _Request(), db, db.get(User, ids["admin"]))

        with db_session.SessionLocal() as db:
            self.assertIsNone(db.get(Document, ids["doc"]).uploaded_by)
            self.assertIsNone(db.get(ProjectSource, ids["source"]).created_by)
            self.assertIsNotNone(db.get(ProjectSnapshot, ids["snap"]))
            # 目标不拥有的库，属主不变。
            self.assertEqual(db.get(KnowledgeBase, ids["kb"]).owner_id, ids["owner"])

    def test_deletion_rechecks_admin_and_original_token_after_waiting_on_user(self):
        for operation in ("user", "kb"):
            for field, value, expected in (("role", "user", 403), ("is_active", False, 403),
                                           ("token_version", 1, 401)):
                with self.subTest(operation=operation, field=field):
                    with db_session.SessionLocal() as db:
                        admin = self._user(db, admin=True)
                        target = self._user(db)
                        kb = self._kb(db, target)
                        db.commit()
                        admin_id, target_id, kb_id = admin.id, target.id, kb.id
                        db.expunge(admin)

                    def acquire(holder):
                        fresh = holder.scalar(select(User).where(User.id == admin_id).with_for_update())
                        setattr(fresh, field, value)
                        holder.flush()

                    def act(db):
                        try:
                            if operation == "user":
                                users_api.delete_user(target_id, _Request(0), db, admin)
                            else:
                                knowledge.delete_kb(kb_id, _Request(0), db, admin)
                        except HTTPException as exc:
                            db.rollback()
                            return exc.status_code
                        return 200

                    status = self._run_while_held(acquire, act, relation="users", commit_holder=True)
                    self.assertEqual(status, expected)
                    with db_session.SessionLocal() as db:
                        self.assertIsNotNone(db.get(User, target_id))
                        self.assertIsNotNone(db.get(KnowledgeBase, kb_id))

    def test_two_admins_deleting_each_other_finish_without_reverse_user_locks(self):
        with db_session.SessionLocal() as db:
            first, second = self._user(db, admin=True), self._user(db, admin=True)
            db.commit()
            ids = first.id, second.id
            db.expunge_all()
        start = threading.Event()

        def act(actor, target):
            with db_session.SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                self.assertTrue(start.wait(timeout=10))
                try:
                    users_api.delete_user(target.id, _Request(), db, actor)
                    return 200
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act, first, second), pool.submit(act, second, first)]
            start.set()
            outcomes = [future.result(timeout=WAIT_TIMEOUT) for future in futures]
        self.assertEqual(sorted(outcomes), [200, 403])
        with db_session.SessionLocal() as db:
            self.assertEqual(len(db.scalars(select(User).where(User.id.in_(ids))).all()), 1)

    # ---------------- 项目源撤销 / 取消 ----------------

    def test_revoke_source_waits_for_source_lock_and_keeps_locked_truth(self):
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            project_kb = self._kb(db, admin, kind="project")
            source = self._project_source(db, project_kb, admin)
            snap = ProjectSnapshot(source_id=source.id, status="pending")
            db.add(snap)
            db.commit()
            ids = dict(admin=admin.id, source=source.id, kb=project_kb.id, snap=snap.id)
            # 调用方手里是**事务外**的旧对象（revoked_at 为 None）。
            stale = db.get(ProjectSource, source.id)
            db.expunge(stale)

        # 并发撤销已经提交（时刻早于本次调用）。
        with db_session.SessionLocal() as writer:
            writer.execute(
                text("UPDATE project_sources SET revoked_at = :ts WHERE id = :id"),
                {"ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "id": ids["source"]},
            )
            writer.commit()

        def acquire(holder):
            holder.execute(
                select(ProjectSource).where(ProjectSource.id == ids["source"])
                .with_for_update()
            ).scalar_one()

        def act(db):
            project_sources.revoke_source(db, stale)
            db.commit()

        self._run_while_held(acquire, act, relation="project_sources")

        with db_session.SessionLocal() as db:
            source = db.get(ProjectSource, ids["source"])
            # 旧 ORM 数据绝不覆盖已提交的撤销时刻；快照被真实取消。
            self.assertEqual(source.revoked_at, datetime(2026, 1, 1, tzinfo=timezone.utc))
            self.assertEqual(db.get(ProjectSnapshot, ids["snap"]).status, "cancelled")

    def test_revoke_source_never_overwrites_committed_revocation_with_stale_object(self):
        """事务外持有的 ``ProjectSource`` 只是 id 载体：不得拿它的旧时刻覆盖库值。"""
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            project_kb = self._kb(db, admin, kind="project")
            source = self._project_source(db, project_kb, admin)
            db.commit()
            ids = dict(admin=admin.id, source=source.id)
            stale = db.get(ProjectSource, source.id)
            db.expunge(stale)

        with db_session.SessionLocal() as writer:
            writer.execute(text("UPDATE project_sources SET revoked_at = :ts WHERE id = :id"),
                           {"ts": datetime(2026, 1, 1, tzinfo=timezone.utc), "id": ids["source"]})
            writer.commit()

        _revoke_in_session(ids["source"], stale)

        with db_session.SessionLocal() as db:
            self.assertEqual(db.get(ProjectSource, ids["source"]).revoked_at,
                             datetime(2026, 1, 1, tzinfo=timezone.utc))

    def test_cancel_on_revoked_source_uses_fresh_row(self):
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            project_kb = self._kb(db, admin, kind="project")
            source = self._project_source(db, project_kb, admin)
            db.commit()
            ids = dict(admin=admin.id, source=source.id)
            stale = db.get(ProjectSource, source.id)
            db.expunge(stale)

        with db_session.SessionLocal() as writer:
            writer.execute(text("UPDATE project_sources SET revoked_at = :ts WHERE id = :id"),
                           {"ts": datetime.now(timezone.utc), "id": ids["source"]})
            writer.commit()

        with db_session.SessionLocal() as db:
            target = project_sources.cancel_active_snapshot(db, stale)
            db.commit()
            # 锁后新鲜行显示已撤销：返回 None，绝不按旧对象写入任何东西。
            self.assertIsNone(target)

    def test_delete_kb_project_path_waits_for_source_lock(self):
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            project_kb = self._kb(db, admin, kind="project")
            source = self._project_source(db, project_kb, admin)
            db.commit()
            ids = dict(admin=admin.id, source=source.id, kb=project_kb.id)

        def acquire(holder):
            holder.execute(
                select(ProjectSource).where(ProjectSource.id == ids["source"])
                .with_for_update()
            ).scalar_one()

        def do_delete(db):
            knowledge.delete_kb(ids["kb"], _Request(), db, db.get(User, ids["admin"]))

        self._run_while_held(acquire, do_delete, relation="project_sources")

        with db_session.SessionLocal() as db:
            self.assertIsNotNone(db.get(ProjectSource, ids["source"]).revoked_at)

    # ---------------- 删除替代文档 vs 治理 ----------------

    def test_delete_replacement_waits_for_supersede_kb_before_locking_any_document(self):
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            kb = self._kb(db, admin)
            paths = [Path(self.temp.name) / name for name in ("first.txt", "second.txt")]
            for path in paths:
                path.write_text("payload", encoding="utf-8")
            docs = [self._document(db, kb, admin, path) for path in paths]
            original, replacement = sorted(docs, key=lambda doc: str(doc.id))
            ids = dict(admin=admin.id, kb=kb.id, original=original.id, replacement=replacement.id)
            original_path, replacement_path = Path(original.stored_path), Path(replacement.stored_path)
            db.commit()
        held = {}

        def acquire(holder):
            knowledge._lock_governance_admin(holder, _Request(), holder.get(User, ids["admin"]))
            held["kb"] = document_governance.require_governance_kb(holder, ids["kb"])
            # 模拟 supersede 已取得 UUID 较小的第一行、尚未取得 replacement 的时刻。
            holder.scalar(select(Document.id).where(Document.id == ids["original"]).with_for_update())

        def finish_supersede(holder):
            document_governance.supersede_document(
                holder, held["kb"], ids["original"], ids["replacement"], 0,
            )

        def do_delete(db):
            knowledge.delete_document(ids["kb"], ids["replacement"], _Request(), db,
                                      db.get(User, ids["admin"]))

        def probe():
            # 删除必须等在 KB 上，而不是先拿 replacement 再让 SET NULL 反锁 original。
            with db_session.SessionLocal() as db:
                db.scalar(select(Document.id).where(Document.id == ids["replacement"])
                          .with_for_update(nowait=True))
                db.rollback()

        # 不访问向量服务；仍执行真实 PG DELETE、自引用 FK SET NULL 和源文件清理。
        with patch.object(knowledge, "delete_document_vectors") as vectors:
            self._run_while_held(
                acquire, do_delete, relation="knowledge_bases", probe=probe,
                on_release=finish_supersede, commit_holder=True,
            )
        vectors.assert_called_once()
        with db_session.SessionLocal() as db:
            self.assertIsNone(db.get(Document, ids["replacement"]))
            original = db.get(Document, ids["original"])
            self.assertIsNone(original.superseded_by_id)
            self.assertIsNotNone(original.superseded_at)
            self.assertEqual(original.governance_revision, 1)
            self.assertEqual(original.lifecycle_state, "superseded")
        self.assertTrue(original_path.exists())
        self.assertFalse(replacement_path.exists())

    # ---------------- 上传库硬删 ----------------

    def test_delete_kb_locks_conversation_before_document_and_cleans_files(self):
        # 只删向量集合、不做检索：把向量引擎固定到测试库，避免任何外部连接。
        with patch.object(vector_store, "_vector_engine", return_value=self.engine):
            vector_store._store.cache_clear()
            self.addCleanup(vector_store._store.cache_clear)
            # 显式建集合表（删除路径会按集合名清理），不依赖其它测试的副作用。
            vector_store.get_vector_store(
                "lockcol_probe_" + uuid.uuid4().hex).create_tables_if_not_exists()
            self._delete_kb_with_conversation_lock()

    def _delete_kb_with_conversation_lock(self):
        with db_session.SessionLocal() as db:
            admin = self._user(db, admin=True)
            kb = self._kb(db, admin)
            conv = self._conversation(db, admin, kb)
            self._turn_with_message(db, admin, conv)
            path = Path(self.temp.name) / "deleted.txt"
            path.write_text("payload", encoding="utf-8")
            doc = self._document(db, kb, admin, path)
            db.commit()
            ids = dict(admin=admin.id, kb=kb.id, conv=conv.id, doc=doc.id, name=kb.name)

        def acquire(holder):
            holder.execute(
                select(Conversation).where(Conversation.id == ids["conv"])
                .with_for_update()
            ).scalar_one()

        def do_delete(db):
            knowledge.delete_kb(ids["kb"], _Request(), db, db.get(User, ids["admin"]))

        self._run_while_held(acquire, do_delete, relation="conversations")

        with db_session.SessionLocal() as db:
            self.assertIsNone(db.get(KnowledgeBase, ids["kb"]))
            self.assertIsNone(db.get(Conversation, ids["conv"]))
            self.assertIsNone(db.get(Document, ids["doc"]))
            self.assertEqual(
                db.scalar(select(Message).where(Message.conversation_id == ids["conv"])), None
            )
            # 保留删除行为：轮次作为去重墓碑保留，但会话绑定与诊断被清除。
            turn = db.scalar(select(ChatTurn).where(ChatTurn.conversation_id.is_(None),
                                                     ChatTurn.user_id == ids["admin"]))
            self.assertIsNotNone(turn)
            self.assertIsNone(turn.trace)
            # 审计仍然落盘（删除行为不变）。
            audit = db.scalar(select(AuditLog).where(AuditLog.action == "kb_delete",
                                                    AuditLog.target == ids["name"]))
            self.assertIsNotNone(audit)
        self.assertFalse(path.exists())


class _Request:
    """直接调用仍显式携带认证时保存的原始令牌版本。"""

    def __init__(self, version=0):
        self.client = None
        self.headers = {}
        self.state = SimpleNamespace(auth_token_version=version)


def _revoke_in_session(source_id, stale):
    with db_session.SessionLocal() as db:
        project_sources.revoke_source(db, stale)
        db.commit()


if __name__ == "__main__":
    unittest.main()