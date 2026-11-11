"""A3：知识库成员读取/维护能力的真实 PostgreSQL 验收。

覆盖：

1. ``list_kbs`` 的 ``membership_revision`` / ``can_manage_documents`` /
   ``can_manage_members`` 由服务端按"当前用户 + 当前库"计算，owner/public
   不自动获得维护，项目库维护仍属项目配置。
2. 文档写入口（upload/reindex/cancel/delete/governance/supersede/reenable/
   quality/chunks/comparison 与所有分片 create/status/chunk/complete/abort）
   按当前库维护能力授权；纯读取（列表/详情/原文）保持读取 ACL。
3. 跨库隔离与会话创建者隔离：同库其他维护者不得读取/续传/放弃他人会话。
4. 撤销维护权或令牌在慢速写盘、慢速分片请求体、慢速合并、已完成会话重放、
   唯一冲突回滚复用等窗口内发生时，写提交必须重新根门禁并拒绝，绝不产生
   额外文档或错误复用。
5. 失败清理与同会话收编串行：迟到的失败清理不得删除已提交的 canonical。

数据库门沿用 ``tests._memory_project_test_gate``（无配置 skip、坏配置硬失败），
只在本任务独占的一次性库内 TRUNCATE 业务表。
"""
import hashlib
import io
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from fastapi import HTTPException, Request
from sqlalchemy import delete, func, select, text

if TEST_URL:
    from app.db import session as db_session
    from app.db.init_db import init_db
    from app.models import (
        KB_MEMBER_ROLE_MAINTAINER,
        KB_MEMBER_ROLE_READER,
        Document,
        DocumentChunk,
        DocumentStatus,
        KnowledgeBase,
        KnowledgeBaseMember,
        UploadSession,
        UploadStatus,
        User,
    )
    from app.api.v1 import knowledge
    from app.services import upload

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


def _reset():
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE users, knowledge_bases, documents, upload_sessions, "
            "document_chunks, audit_logs RESTART IDENTITY CASCADE"
        ))


def _claimed_document_id(session_id) -> uuid.UUID:
    """与 upload.claimed_document_id 相同的确定性认领 ID（不依赖 ORM 实例）。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"rag-upload:{session_id}")


def _request(version: int = 0) -> Request:
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                       "client": ("127.0.0.1", 1)})
    request.state.auth_token_version = version
    return request


def _upload_file(data: bytes, filename="m.txt", *, reader=None):
    source = reader or io.BytesIO(data)
    return knowledge.UploadFile(filename=filename, file=source)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class KbDocumentMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        _reset()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        storage = patch.object(knowledge.settings, "UPLOAD_DIR", self.temp.name)
        storage.start()
        self.addCleanup(storage.stop)
        chunk = patch.object(knowledge.settings, "UPLOAD_CHUNK_SIZE", 4)
        chunk.start()
        self.addCleanup(chunk.stop)
        with SessionLocal() as db:
            self.admin = self._user(db, admin=True)
            self.owner = self._user(db)
            self.maintainer = self._user(db)
            self.reader = self._user(db)
            self.stranger = self._user(db)
            self.disabled = self._user(db, active=False)
            self.kb = self._kb(db, self.owner)
            self.other_kb = self._kb(db, self.owner)
            self.public_kb = self._kb(db, self.owner, public=True)
            self.project_kb = self._kb(db, self.owner, kind="project")
            self._member(db, self.kb, self.maintainer, KB_MEMBER_ROLE_MAINTAINER)
            self._member(db, self.kb, self.reader, KB_MEMBER_ROLE_READER)
            self._member(db, self.other_kb, self.reader, KB_MEMBER_ROLE_READER)
            self._member(db, self.kb, self.disabled, KB_MEMBER_ROLE_READER)
            db.commit()
            self.ids = SimpleNamespace(
                admin=self.admin.id, owner=self.owner.id, maintainer=self.maintainer.id,
                reader=self.reader.id, stranger=self.stranger.id, disabled=self.disabled.id,
                kb=self.kb.id, other_kb=self.other_kb.id, public_kb=self.public_kb.id,
                project_kb=self.project_kb.id,
            )
            self.membership_revision = self.kb.membership_revision

    # ---------------- fixtures ----------------

    @staticmethod
    def _user(db, *, admin=False, active=True) -> User:
        user = User(username="maint_" + uuid.uuid4().hex[:12], display_name="u",
                    role="admin" if admin else "user", is_active=active,
                    password_hash="test-only-no-login")
        db.add(user)
        db.flush()
        return user

    @staticmethod
    def _kb(db, owner, *, public=False, kind="upload") -> KnowledgeBase:
        kb = KnowledgeBase(name="maintkb_" + uuid.uuid4().hex[:12], description="",
                           collection_name="maintcol_" + uuid.uuid4().hex[:16],
                           owner_id=owner.id, is_public=public, kind=kind)
        db.add(kb)
        db.flush()
        return kb

    @staticmethod
    def _member(db, kb, user, role):
        db.add(KnowledgeBaseMember(kb_id=kb.id, user_id=user.id, role=role))
        db.flush()

    def _actor(self, user_id):
        with SessionLocal() as db:
            return db.get(User, user_id)

    def _add_document(self, kb_id=None, *, status="ready", **overrides):
        with SessionLocal() as db:
            doc_id = uuid.uuid4()
            path = Path(self.temp.name) / str(kb_id or self.ids.kb) / f"{doc_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("body", encoding="utf-8")
            values = dict(id=doc_id, kb_id=kb_id or self.ids.kb, filename="doc.txt",
                          file_ext=".txt", file_size=4, stored_path=str(path),
                          status=status, chunk_count=1, char_count=4, **overrides)
            db.add(Document(**values))
            db.add(DocumentChunk(document_id=doc_id, chunk_index=0, content="body",
                                 embedded=True, search_text=""))
            db.commit()
            return doc_id

    def _document_count(self, kb_id=None) -> int:
        with SessionLocal() as db:
            stmt = select(func.count()).select_from(Document)
            if kb_id is not None:
                stmt = stmt.where(Document.kb_id == kb_id)
            return db.scalar(stmt) or 0

    def _expect(self, status, fn, *args, **kwargs):
        with self.assertRaises(HTTPException) as caught:
            fn(*args, **kwargs)
        self.assertEqual(caught.exception.status_code, status,
                         getattr(caught.exception, "detail", ""))
        return caught.exception

    # ---------------- 1. 列表能力矩阵 ----------------

    def test_list_kbs_reports_server_side_capabilities(self):
        def listing(user_id):
            with SessionLocal() as db:
                user = db.get(User, user_id)
                rows = knowledge.list_kbs(db, user).data
                return {row.id: row for row in rows}

        admin = listing(self.ids.admin)
        self.assertIn(self.ids.project_kb, admin)
        self.assertEqual(admin[self.ids.kb].can_manage_documents, True)
        self.assertEqual(admin[self.ids.kb].can_manage_members, True)
        self.assertEqual(admin[self.ids.project_kb].can_manage_documents, False,
                         "项目库文档维护不走普通上传维护权")
        self.assertEqual(admin[self.ids.project_kb].can_manage_members, True)
        self.assertEqual(admin[self.ids.kb].membership_revision,
                         self.membership_revision)

        maintainer = listing(self.ids.maintainer)
        self.assertEqual(maintainer[self.ids.kb].can_manage_documents, True)
        self.assertEqual(maintainer[self.ids.kb].can_manage_members, False,
                         "maintainer 不获得成员管理权")
        self.assertNotIn(self.ids.other_kb, maintainer, "跨库成员关系不得外溢")

        reader = listing(self.ids.reader)
        self.assertEqual(reader[self.ids.kb].can_manage_documents, False)
        self.assertEqual(reader[self.ids.kb].can_manage_members, False)
        self.assertIn(self.ids.public_kb, reader, "public 保持旧读取语义")

        stranger = listing(self.ids.stranger)
        self.assertIn(self.ids.public_kb, stranger)
        self.assertNotIn(self.ids.kb, stranger)

        owner = listing(self.ids.owner)
        self.assertEqual(owner[self.ids.kb].can_manage_documents, False,
                         "owner 只保留读取，不自动获得维护")
        self.assertEqual(owner[self.ids.kb].can_manage_members, False)

        disabled = listing(self.ids.disabled)
        self.assertNotIn(self.ids.kb, disabled, "disabled 成员不可见也不可维护")
        self.assertFalse(any(row.can_manage_documents for row in disabled.values()))

    # ---------------- 2/3. 维护端点矩阵 + 只读区分 ----------------

    def _maintenance_calls(self, doc_id, replacement_id):
        """每个维护写/预览入口的最小真实调用；返回 (label, callable)。"""
        return [
            ("governance", lambda db, u: knowledge.update_document_governance(
                self.ids.kb, doc_id,
                knowledge.DocumentGovernanceUpdate(expected_revision=0, maintainer="ops"),
                _request(u.token_version), db, u)),
            ("supersede", lambda db, u: knowledge.supersede_document(
                self.ids.kb, doc_id,
                knowledge.DocumentSupersedeRequest(expected_revision=0,
                                                   replacement_document_id=replacement_id),
                _request(u.token_version), db, u)),
            ("reenable", lambda db, u: knowledge.reenable_document(
                self.ids.kb, doc_id,
                knowledge.DocumentReenableRequest(expected_revision=0),
                _request(u.token_version), db, u)),
            ("reindex", lambda db, u: knowledge.reindex_document(
                self.ids.kb, doc_id, _request(u.token_version), db, u)),
            ("cancel", lambda db, u: knowledge.cancel_document(
                self.ids.kb, doc_id, _request(u.token_version), db, u)),
            ("delete", lambda db, u: knowledge.delete_document(
                self.ids.kb, doc_id, _request(u.token_version), db, u)),
            ("quality", lambda db, u: knowledge.get_document_quality(
                self.ids.kb, doc_id, db, u)),
            ("chunks", lambda db, u: knowledge.get_document_chunks(
                self.ids.kb, doc_id, 0, 20, db, u)),
            ("comparison", lambda db, u: knowledge.compare_document(
                self.ids.kb, doc_id, knowledge.Response(),
                replacement_id, db, u)),
        ]

    def test_reader_stranger_owner_public_are_denied_every_maintenance_endpoint(self):
        doc_id = self._add_document(status="failed")
        replacement = self._add_document()
        with patch.object(knowledge, "delete_document_vectors"):
            for user_id in (self.ids.reader, self.ids.owner, self.ids.stranger):
                user = self._actor(user_id)
                for label, call in self._maintenance_calls(doc_id, replacement):
                    with self.subTest(user=user.username, endpoint=label):
                        with SessionLocal() as db:
                            actor = db.get(User, user.id)
                            expected = 404 if user_id == self.ids.stranger else 403
                            error = self._expect(expected, call, db, actor)
                            if expected == 404:
                                self.assertEqual(error.detail, "知识库不存在")
                            else:
                                self.assertEqual(error.detail, "无权维护该知识库的文档")
                            db.rollback()
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, doc_id).governance_revision, 0)
            self.assertIsNotNone(db.get(Document, doc_id))
            self.assertFalse(db.get(Document, doc_id).cancel_requested)

    def test_public_member_does_not_gain_maintenance(self):
        doc_id = self._add_document(kb_id=self.ids.public_kb, status="failed")
        with SessionLocal() as db:
            user = db.get(User, self.ids.stranger)
            self._expect(403, knowledge.get_document_quality,
                         self.ids.public_kb, doc_id, db, user)
            self._expect(403, knowledge.reindex_document, self.ids.public_kb, doc_id,
                         _request(0), db, user)

    def test_maintainer_and_admin_can_run_all_maintenance_actions(self):
        for user_id in (self.ids.admin, self.ids.maintainer):
            doc_id = self._add_document()
            replacement = self._add_document()
            with SessionLocal() as db:
                user = db.get(User, user_id)
                data = knowledge.get_document_quality(self.ids.kb, doc_id, db, user)
                self.assertEqual(data.data["document_id"], doc_id)
                page = knowledge.get_document_chunks(self.ids.kb, doc_id, 0, 20, db, user)
                self.assertEqual(page.data["total"], 1)
                comparison = knowledge.compare_document(
                    self.ids.kb, doc_id, knowledge.Response(), replacement, db, user)
                self.assertTrue(comparison.data["comparable"])
                patched = knowledge.update_document_governance(
                    self.ids.kb, doc_id,
                    knowledge.DocumentGovernanceUpdate(expected_revision=0, maintainer="ops"),
                    _request(user.token_version), db, user)
                self.assertEqual(patched.data.governance_revision, 1)
                superseded = knowledge.supersede_document(
                    self.ids.kb, doc_id,
                    knowledge.DocumentSupersedeRequest(expected_revision=1,
                                                       replacement_document_id=replacement),
                    _request(user.token_version), db, user)
                self.assertEqual(superseded.data.lifecycle_state, "superseded")
                reenabled = knowledge.reenable_document(
                    self.ids.kb, doc_id, knowledge.DocumentReenableRequest(expected_revision=2),
                    _request(user.token_version), db, user)
                self.assertEqual(reenabled.data.lifecycle_state, "active")
            with patch.object(knowledge, "delete_document_vectors"):
                with SessionLocal() as db:
                    user = db.get(User, user_id)
                    knowledge.reindex_document(self.ids.kb, doc_id, _request(0), db, user)
                with SessionLocal() as db:
                    user = db.get(User, user_id)
                    knowledge.delete_document(self.ids.kb, doc_id, _request(0), db, user)

    def test_reads_stay_available_to_reader_but_not_stranger(self):
        doc_id = self._add_document()
        with SessionLocal() as db:
            reader = db.get(User, self.ids.reader)
            self.assertEqual(knowledge.list_documents(self.ids.kb, db=db, user=reader)
                             .data.total, 1)
            self.assertEqual(knowledge.get_document(self.ids.kb, doc_id, db, reader).data.id,
                             doc_id)
            stranger = db.get(User, self.ids.stranger)
            self._expect(404, knowledge.list_documents, self.ids.kb, db=db, user=stranger)
            self._expect(404, knowledge.get_document, self.ids.kb, doc_id, db, stranger)

    def test_project_kb_maintenance_is_rejected_and_cannot_manage_documents(self):
        doc_id = self._add_document(kb_id=self.ids.project_kb)
        with SessionLocal() as db:
            admin = db.get(User, self.ids.admin)
            for label, call in (
                ("quality", lambda: knowledge.get_document_quality(
                    self.ids.project_kb, doc_id, db, admin)),
                ("chunks", lambda: knowledge.get_document_chunks(
                    self.ids.project_kb, doc_id, 0, 20, db, admin)),
                ("delete", lambda: knowledge.delete_document(
                    self.ids.project_kb, doc_id, _request(0), db, admin)),
                ("upload", lambda: knowledge.upload_document(
                    self.ids.project_kb, _request(0),
                    _upload_file(b"data"), db, admin)),
                ("session", lambda: knowledge.create_upload_session(
                    self.ids.project_kb,
                    knowledge.UploadSessionCreate(filename="p.txt", file_size=4),
                    _request(0), db, admin)),
            ):
                with self.subTest(endpoint=label):
                    self._expect(400, call)
                    db.rollback()

    # ---------------- 分片会话：创建者隔离 ----------------

    def _start_session(self, kb_id=None, data=b"abcdefgh", user_id=None):
        with SessionLocal() as db:
            session = upload.create_or_resume(
                db, kb_id or self.ids.kb, "chunked.txt", len(data), ".txt",
                user_id or self.ids.admin)
            return session.id

    def _write_all(self, session_id, data=b"abcdefgh", user_id=None, token=0):
        for index in range((len(data) + 3) // 4):
            knowledge._write_chunk(self.ids.kb, session_id, user_id or self.ids.admin,
                                   index, data[index * 4:(index + 1) * 4], token)

    def test_upload_session_is_creator_only_even_for_other_maintainers(self):
        session_id = self._start_session()
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            admin = db.get(User, self.ids.admin)
            # 同库 maintainer 可以创建自己的会话，但不得读/写/放弃管理员的会话。
            own = knowledge.create_upload_session(
                self.ids.kb, knowledge.UploadSessionCreate(filename="own.txt", file_size=4),
                _request(maintainer.token_version), db, maintainer)
            self.assertNotEqual(own.data.upload_id, session_id)
            self._expect(404, knowledge.get_upload_session, self.ids.kb, session_id, db,
                         maintainer)
            self._expect(404, knowledge._chunk_metadata, self.ids.kb, session_id,
                         maintainer.id, 0, maintainer.token_version)
            self._expect(404, knowledge._write_chunk, self.ids.kb, session_id,
                         maintainer.id, 0, b"abcd", maintainer.token_version)
            self._expect(404, knowledge.abort_upload, self.ids.kb, session_id,
                         _request(maintainer.token_version), db, maintainer)
            self._expect(404, knowledge.complete_upload, self.ids.kb, session_id,
                         _request(maintainer.token_version), db, maintainer)
            # admin 的会话对 admin 本人可见。
            self.assertEqual(knowledge.get_upload_session(
                self.ids.kb, session_id, db, admin).data.upload_id, session_id)

    def test_cross_kb_session_access_is_rejected(self):
        session_id = self._start_session()
        with SessionLocal() as db:
            admin = db.get(User, self.ids.admin)
            self._expect(404, knowledge.get_upload_session, self.ids.other_kb, session_id,
                         db, admin)
            self._expect(404, knowledge._chunk_metadata, self.ids.other_kb, session_id,
                         admin.id, 0, admin.token_version)

    def test_reader_cannot_create_or_abort_upload_session(self):
        session_id = self._start_session()
        with SessionLocal() as db:
            reader = db.get(User, self.ids.reader)
            self._expect(403, knowledge.create_upload_session, self.ids.kb,
                         knowledge.UploadSessionCreate(filename="r.txt", file_size=4),
                         _request(reader.token_version), db, reader)
            db.rollback()  # 先释放本次拒绝事务持有的根锁，避免独立 Session 等待自己
            self._expect(403, knowledge.abort_upload, self.ids.kb, session_id,
                         _request(reader.token_version), db, reader)
            db.rollback()
        # 分片写入口使用同一维护门禁（独立 Session，不得与上面的锁重叠）。
        self._expect(403, knowledge._write_chunk, self.ids.kb, session_id,
                     self.ids.reader, 0, b"abcd", 0)

    # ---------------- 4. 撤销 / 旧 token 窗口 ----------------

    def test_revocation_during_slow_small_upload_write_is_rejected_and_cleans(self):
        class RevokingReader(io.BytesIO):
            """第二次 read 时撤销维护权（真实并发窗口的确定性注入）。"""

            def __init__(self, data, revoke):
                super().__init__(data)
                self.reads = 0
                self.revoke = revoke

            def read(self, size=-1):
                self.reads += 1
                if self.reads == 2:
                    self.revoke()
                return super().read(size)

        def revoke_membership():
            # 降为 reader：保留读取（可见），只撤销维护权 -> 403 而不是 404。
            with SessionLocal() as db:
                db.execute(text(
                    "UPDATE knowledge_base_members SET role='reader' "
                    "WHERE kb_id=:kb AND user_id=:uid"),
                    {"kb": self.ids.kb, "uid": self.ids.maintainer})
                db.commit()
        data = b"x" * (2 * 1024 * 1024)
        reader = RevokingReader(data, revoke_membership)
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            self._expect(403, knowledge.upload_document, self.ids.kb, _request(0),
                         _upload_file(data, reader=reader), db, maintainer)
            db.rollback()
        self.assertEqual(self._document_count(), 0)
        self.assertEqual([p for p in Path(self.temp.name).rglob("*") if p.is_file()], [])

    def test_old_token_during_slow_small_upload_write_is_rejected(self):
        class RevokingReader(io.BytesIO):
            def __init__(self, data, revoke):
                super().__init__(data)
                self.reads = 0
                self.revoke = revoke

            def read(self, size=-1):
                self.reads += 1
                if self.reads == 2:
                    self.revoke()
                return super().read(size)

        def rotate_token():
            with SessionLocal() as db:
                user = db.get(User, self.ids.maintainer)
                user.token_version += 1
                db.commit()

        data = b"y" * (2 * 1024 * 1024)
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            self._expect(401, knowledge.upload_document, self.ids.kb, _request(0),
                         _upload_file(data, reader=RevokingReader(data, rotate_token)),
                         db, maintainer)
            db.rollback()
        self.assertEqual(self._document_count(), 0)
        self.assertEqual([p for p in Path(self.temp.name).rglob("*") if p.is_file()], [])

    def test_revocation_after_chunk_metadata_blocks_disk_write(self):
        session_id = self._start_session(user_id=self.ids.maintainer)
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            self.assertEqual(knowledge._chunk_metadata(self.ids.kb, session_id,
                                                       maintainer.id, 0, 0), 4)
            db.rollback()
        # 请求体读取期间被撤销维护权：写盘前的新鲜根门禁必须拒绝。
        with SessionLocal() as db:
            db.execute(text(
                "UPDATE knowledge_base_members SET role='reader' "
                "WHERE kb_id=:kb AND user_id=:uid"),
                {"kb": self.ids.kb, "uid": self.ids.maintainer})
            db.commit()
        self._expect(403, knowledge._write_chunk, self.ids.kb, session_id,
                     self.ids.maintainer, 0, b"abcd", 0)
        self.assertFalse((upload.tmp_dir(session_id) / "0.part").exists())

    def test_revocation_during_slow_merge_blocks_publication_then_new_token_completes(self):
        data = b"slow merge payload"
        session_id = self._start_session(data=data, user_id=self.ids.maintainer)
        self._write_all(session_id, data=data, user_id=self.ids.maintainer)
        real_assemble = upload.assemble

        def assemble_then_revoke(db, session):
            result = real_assemble(db, session)
            db.commit()  # 阶段二已落盘并提交
            with SessionLocal() as other:  # 合并窗口内降权 + 轮换令牌（独立事务提交）
                other.execute(text(
                    "UPDATE knowledge_base_members SET role='reader' "
                    "WHERE kb_id=:kb AND user_id=:uid"),
                    {"kb": self.ids.kb, "uid": self.ids.maintainer})
                other.execute(text(
                    "UPDATE users SET token_version = token_version + 1 WHERE id = :uid"),
                    {"uid": self.ids.maintainer})
                other.commit()
            return result

        with SessionLocal() as db, patch.object(upload, "assemble",
                                                side_effect=assemble_then_revoke):
            maintainer = db.get(User, self.ids.maintainer)
            # 阶段三根门禁复核：原 token 已失效 -> 401，且不得发布文档。
            self._expect(401, knowledge.complete_upload, self.ids.kb, session_id,
                         _request(0), db, maintainer)
        self.assertEqual(self._document_count(), 0)
        # 迟到的失败清理不得删除尚可由新令牌合法完成的上传。
        merged = upload.final_path(self.ids.kb, _claimed_document_id(session_id), ".txt")
        self.assertTrue(merged.is_file(), "仍可恢复的合并文件不能被失败清理删除")

        # 新令牌但维护权仍未恢复：重新授权失败，仍不得发布。
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            self._expect(403, knowledge.complete_upload, self.ids.kb, session_id,
                         _request(1), db, maintainer)
            db.rollback()
        self.assertEqual(self._document_count(), 0)

        # 恢复维护权后用新令牌合法完成：复用同一认领路径，只产生一份文档。
        with SessionLocal() as db:
            db.execute(text(
                "UPDATE knowledge_base_members SET role='maintainer' "
                "WHERE kb_id=:kb AND user_id=:uid"), {"kb": self.ids.kb,
                                                     "uid": self.ids.maintainer})
            db.commit()
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            result = knowledge.complete_upload(self.ids.kb, session_id, _request(1), db,
                                               maintainer)
        self.assertEqual(self._document_count(), 1)
        self.assertEqual(result.data.document.id, _claimed_document_id(session_id))
        self.assertTrue(upload.final_path(self.ids.kb, _claimed_document_id(session_id),
                                          ".txt").is_file())

    def test_phase2_completed_replay_requires_fresh_permission(self):
        data = b"completed replay"
        session_id = self._start_session(data=data, user_id=self.ids.maintainer)
        self._write_all(session_id, data=data, user_id=self.ids.maintainer)
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            knowledge.complete_upload(self.ids.kb, session_id, _request(0), db, maintainer)
        # 已完成会话重放：先释放会话锁，再按 User -> KB 复核（降为 reader -> 403）。
        with SessionLocal() as db:
            db.execute(text(
                "UPDATE knowledge_base_members SET role='reader' "
                "WHERE kb_id=:kb AND user_id=:uid"),
                {"kb": self.ids.kb, "uid": self.ids.maintainer})
            db.commit()
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            self._expect(403, knowledge.complete_upload, self.ids.kb, session_id,
                         _request(0), db, maintainer)
        with SessionLocal() as db:
            db.execute(text(
                "UPDATE users SET token_version = token_version + 1 WHERE id = :uid"),
                {"uid": self.ids.maintainer})
            db.execute(text(
                "UPDATE knowledge_base_members SET role='maintainer' "
                "WHERE kb_id=:kb AND user_id=:uid"), {"kb": self.ids.kb,
                                                     "uid": self.ids.maintainer})
            db.commit()
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            self._expect(401, knowledge.complete_upload, self.ids.kb, session_id,
                         _request(0), db, maintainer)
        # 新 token 重放成功，返回同一文档且不新增行。
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            replay = knowledge.complete_upload(self.ids.kb, session_id, _request(1), db,
                                               maintainer)
        self.assertEqual(replay.data.document.id, _claimed_document_id(session_id))
        self.assertEqual(self._document_count(), 1)

    def test_unique_conflict_rollback_reauthorizes_before_canonical_reuse(self):
        data = b"conflict content"
        digest = hashlib.sha256(data).hexdigest()
        # 预置 canonical：唯一索引将在 INSERT 时冲突。
        canonical = self._add_document(content_sha256=digest)
        real_canonical = knowledge.document_identity.canonical_document
        calls = {"count": 0}

        def hide_canonical_and_revoke(db, kb_id, sha):
            """在"根门禁通过后、INSERT 之前"的窗口里撤权并假装暂无 canonical。

            第 1 次查询返回 None（模拟并发 canonical 在门禁与插入之间才出现），
            同窗口把 maintainer 降为 reader；插入真实撞唯一索引，冲突回滚后的
            重新授权必须看到新的成员角色并拒绝复用。
            """
            calls["count"] += 1
            if calls["count"] == 1:
                with SessionLocal() as other:
                    other.execute(text(
                        "UPDATE knowledge_base_members SET role='reader' "
                        "WHERE kb_id=:kb AND user_id=:uid"),
                        {"kb": self.ids.kb, "uid": self.ids.maintainer})
                    other.commit()
                return None
            return real_canonical(db, kb_id, sha)

        with SessionLocal() as db, patch.object(
                knowledge.document_identity, "canonical_document",
                side_effect=hide_canonical_and_revoke):
            maintainer = db.get(User, self.ids.maintainer)
            # 唯一冲突回滚后重新授权失败：403，且绝不复用 canonical。
            self._expect(403, knowledge.upload_document, self.ids.kb, _request(0),
                         _upload_file(data), db, maintainer)
            db.rollback()
        self.assertEqual(calls["count"], 1,
                         "重新授权失败时不得继续查询/复用 canonical")
        self.assertEqual(self._document_count(), 1, "被拒的冲突上传不得新增文档")
        self.assertIsNotNone(canonical)
        # 本次写入的副本由本请求清理：只保留预置 canonical 自己的文件。
        with SessionLocal() as db:
            canonical_file = db.get(Document, canonical).stored_path
        self.assertEqual([str(p) for p in Path(self.temp.name).rglob("*") if p.is_file()],
                         [canonical_file])

    def test_unique_conflict_rollback_reuses_canonical_after_reauthorization(self):
        data = b"conflict reuse"
        digest = hashlib.sha256(data).hexdigest()
        canonical = self._add_document(content_sha256=digest)
        real_canonical = knowledge.document_identity.canonical_document
        calls = {"count": 0}

        def hide_canonical_first(db, kb_id, sha):
            calls["count"] += 1
            if calls["count"] == 1:
                return None  # 门禁通过时还看不到并发 canonical
            return real_canonical(db, kb_id, sha)

        with SessionLocal() as db, patch.object(
                knowledge.document_identity, "canonical_document",
                side_effect=hide_canonical_first):
            maintainer = db.get(User, self.ids.maintainer)
            result = knowledge.upload_document(self.ids.kb, _request(0),
                                               _upload_file(data), db, maintainer)
        self.assertEqual(calls["count"], 2, "冲突回滚后必须重新查询 canonical")
        self.assertTrue(result.data.deduplicated)
        self.assertEqual(result.data.id, canonical)
        self.assertEqual(self._document_count(), 1)
        # 只保留 canonical 文件，本次写入的副本已清理。
        files = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1, files)

    # ---------------- 5. 失败清理 vs 同会话收编（TOCTOU） ----------------

    def test_delayed_failed_cleanup_keeps_canonical_adopted_by_new_token(self):
        data = b"adoption race"
        session_id = self._start_session(data=data, user_id=self.ids.maintainer)
        self._write_all(session_id, data=data, user_id=self.ids.maintainer)
        real_assemble = upload.assemble
        stolen = {}

        def assemble_then_revoke(db, session):
            result = real_assemble(db, session)
            db.commit()
            stolen["assembled"] = result
            with SessionLocal() as other:
                other.execute(text(
                    "UPDATE users SET token_version = token_version + 1 WHERE id = :uid"),
                    {"uid": self.ids.maintainer})
                other.commit()
            return result

        with SessionLocal() as db, patch.object(upload, "assemble",
                                                side_effect=assemble_then_revoke):
            maintainer = db.get(User, self.ids.maintainer)
            self._expect(401, knowledge.complete_upload, self.ids.kb, session_id,
                         _request(0), db, maintainer)
        # 同一用户用新 token 合法完成并收编同一路径。
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            result = knowledge.complete_upload(self.ids.kb, session_id, _request(1), db,
                                               maintainer)
        canonical_path = upload.final_path(self.ids.kb, _claimed_document_id(session_id), ".txt")
        self.assertTrue(canonical_path.is_file())
        # 迟到的失败清理（旧请求延迟至此才执行）不得删除 canonical 或其分片引用。
        with SessionLocal() as db:
            knowledge._cleanup_owned_assembly(db, stolen["assembled"], session_id)
        self.assertTrue(canonical_path.is_file(), "迟到清理删除了已提交的 canonical")
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(Document, result.data.document.id))
        self.assertEqual(self._document_count(), 1)

    def test_cleanup_takes_session_lock_and_waits_for_holder(self):
        """有界真实竞争：清理在会话行锁上真实阻塞，而不是仅做无锁 SELECT。"""
        data = b"lock wait"
        session_id = self._start_session(data=data, user_id=self.ids.maintainer)
        self._write_all(session_id, data=data, user_id=self.ids.maintainer)
        with SessionLocal() as db:
            assembled = upload.assemble(db, upload.get_session(
                db, self.ids.kb, session_id, lock=True))
            db.commit()
        armed, release, done = threading.Event(), threading.Event(), threading.Event()
        pids = {}

        def holder():
            with engine.connect() as conn, SessionLocal(bind=conn) as db:
                pids["holder"] = db.scalar(text("SELECT pg_backend_pid()"))
                db.execute(select(UploadSession.id).where(UploadSession.id == session_id)
                           .with_for_update()).scalar_one()
                armed.set()
                release.wait(timeout=20)
                db.rollback()

        def cleaner():
            with engine.connect() as conn, SessionLocal(bind=conn) as db:
                pids["cleaner"] = db.scalar(text("SELECT pg_backend_pid()"))
                knowledge._cleanup_owned_assembly(db, assembled, session_id)
                done.set()

        holder_thread = threading.Thread(target=holder, daemon=True)
        cleaner_thread = threading.Thread(target=cleaner, daemon=True)
        holder_thread.start()
        try:
            self.assertTrue(armed.wait(10))
            cleaner_thread.start()
            self.assertFalse(done.wait(0.5), "清理未在会话行锁上等待")
            from tests._pg_lock_wait import wait_for_blocking
            self.assertTrue(wait_for_blocking(engine, pids["holder"], pids["cleaner"]),
                            "未观察到会话行锁等待")
        finally:
            release.set()
            holder_thread.join(20)
            cleaner_thread.join(20)
        self.assertTrue(done.is_set())
        # 会话仍 uploading 且 created_by 非空：保留可恢复状态（由新 token 完成）。
        self.assertTrue(assembled.path.is_file())
        self.assertTrue(upload.tmp_dir(session_id).exists())


    def test_write_chunk_blocked_on_session_lock_reauthorizes_after_revocation(self):
            """根门禁释放后若在会话锁上等待，放行后仍必须做最终新鲜授权。

            真实竞争：holder 占住 UploadSession 行，写分片线程在阶段二真实等待
            （pg_blocking_pids）。等待期间用已提交事务把 maintainer 降为 reader；
            放行后允许写盘，但最终 User -> KB 复核必须 403，绝不能提交成功元数据。
            """
            data = b"abcd"
            session_id = self._start_session(data=data, user_id=self.ids.maintainer)
            armed, release = threading.Event(), threading.Event()
            errors, pids = [], {}
            real_get = upload.get_session

            def record_writer_pid(db, kb_id, sid, *, user_id=None, lock=False):
                if lock and "writer" not in pids:
                    pids["writer"] = db.scalar(text("SELECT pg_backend_pid()"))
                return real_get(db, kb_id, sid, user_id=user_id, lock=lock)

            def holder():
                with engine.connect() as conn, SessionLocal(bind=conn) as db:
                    pids["holder"] = db.scalar(text("SELECT pg_backend_pid()"))
                    db.execute(select(UploadSession.id).where(UploadSession.id == session_id)
                               .with_for_update()).scalar_one()
                    armed.set()
                    release.wait(timeout=20)
                    db.rollback()

            def writer():
                try:
                    knowledge._write_chunk(self.ids.kb, session_id, self.ids.maintainer,
                                           0, data, 0)
                except HTTPException as exc:
                    errors.append(exc)

            holder_thread = threading.Thread(target=holder, daemon=True)
            writer_thread = threading.Thread(target=writer, daemon=True)
            holder_thread.start()
            try:
                self.assertTrue(armed.wait(10))
                with patch.object(upload, "get_session", side_effect=record_writer_pid):
                    writer_thread.start()
                    self.assertFalse(writer_thread.join(0.4), "分片写未在会话行锁上等待")
                    from tests._pg_lock_wait import wait_for_blocking
                    self.assertTrue(wait_for_blocking(engine, pids["holder"], pids["writer"]),
                                    "未观察到 UploadSession 行锁等待")
                    # 等待会话锁期间不得持有 User/KB 根锁（否则撤权/删除会成环）。
                    with engine.connect() as observer:
                        held = observer.execute(text(
                            "SELECT count(*) FROM pg_locks l JOIN pg_class c "
                            "ON c.oid = l.relation WHERE l.pid = :pid AND l.granted "
                            "AND c.relname IN ('users', 'knowledge_bases')"),
                            {"pid": pids["writer"]}).scalar_one()
                    self.assertEqual(held, 0, "等待会话锁时仍持有根锁")
                    with SessionLocal() as other:
                        other.execute(text(
                            "UPDATE knowledge_base_members SET role='reader' "
                            "WHERE kb_id=:kb AND user_id=:uid"),
                            {"kb": self.ids.kb, "uid": self.ids.maintainer})
                        other.commit()
            finally:
                release.set()
                holder_thread.join(20)
                writer_thread.join(20)
            self.assertEqual(len(errors), 1, "最终复核未拒绝被撤权的分片写")
            self.assertEqual(errors[0].status_code, 403)
            # 分片已写盘但不得提交成功元数据；保留给合法新 token 的创建者续传。
            self.assertTrue((upload.tmp_dir(session_id) / "0.part").is_file())
            with SessionLocal() as db:
                session = db.get(UploadSession, session_id)
                self.assertEqual(session.status, UploadStatus.UPLOADING.value)
                self.assertEqual(session.received, [])
            self.assertEqual(self._document_count(), 0)

    def test_write_chunk_slow_disk_write_holds_no_root_locks_and_rechecks(self):
        """磁盘慢写只持会话锁；写盘期间撤权必须在最终复核被拒绝。"""
        data = b"abcd"
        session_id = self._start_session(data=data, user_id=self.ids.maintainer)
        armed, release = threading.Event(), threading.Event()
        errors, pids = [], {}
        real_save = upload.save_chunk

        def slow_save(session, index, data):
            created = real_save(session, index, data)
            from sqlalchemy.orm import object_session
            db = object_session(session)
            pids["writer"] = db.scalar(text("SELECT pg_backend_pid()"))
            armed.set()
            release.wait(timeout=20)
            return created

        def writer():
            try:
                knowledge._write_chunk(self.ids.kb, session_id, self.ids.maintainer,
                                       0, data, 0)
            except HTTPException as exc:
                errors.append(exc)

        writer_thread = threading.Thread(target=writer, daemon=True)
        with patch.object(upload, "save_chunk", side_effect=slow_save):
            writer_thread.start()
            try:
                self.assertTrue(armed.wait(10))
                with engine.connect() as observer:
                    held = observer.execute(text(
                        "SELECT count(*) FROM pg_locks l JOIN pg_class c "
                        "ON c.oid = l.relation WHERE l.pid = :pid AND l.granted "
                        "AND c.relname IN ('users', 'knowledge_bases')"),
                        {"pid": pids["writer"]}).scalar_one()
                self.assertEqual(held, 0, "慢写期间仍持有 User/KB 根锁")
                # 反向证明：其他事务能立刻锁住用户与知识库行（NOWAIT 不阻塞）。
                with engine.connect() as other:
                    other.execute(text("SELECT id FROM users WHERE id=:uid FOR UPDATE NOWAIT"),
                                  {"uid": self.ids.maintainer})
                    other.execute(text("SELECT id FROM knowledge_bases WHERE id=:kb "
                                       "FOR UPDATE NOWAIT"), {"kb": self.ids.kb})
                    other.rollback()
                with SessionLocal() as other:
                    other.execute(text(
                        "UPDATE knowledge_base_members SET role='reader' "
                        "WHERE kb_id=:kb AND user_id=:uid"),
                        {"kb": self.ids.kb, "uid": self.ids.maintainer})
                    other.commit()
            finally:
                release.set()
                writer_thread.join(20)
        self.assertEqual(len(errors), 1, "写盘后撤权未被最终复核拒绝")
        self.assertEqual(errors[0].status_code, 403)
        self.assertTrue((upload.tmp_dir(session_id) / "0.part").is_file())
        with SessionLocal() as db:
            session = db.get(UploadSession, session_id)
            self.assertEqual(session.status, UploadStatus.UPLOADING.value)
            self.assertEqual(session.received, [])

    def test_write_chunk_replay_read_holds_no_root_locks(self):
        """重放同片需要读取/哈希已有分片时，也只持会话锁，User/KB 可 NOWAIT。"""
        data = b"abcd"
        session_id = self._start_session(data=data, user_id=self.ids.maintainer)
        knowledge._write_chunk(self.ids.kb, session_id, self.ids.maintainer, 0, data, 0)
        armed, release = threading.Event(), threading.Event()
        errors, pids = [], {}
        real_get, real_digest = upload.get_session, upload.hashlib.file_digest

        def record_writer_pid(db, kb_id, sid, *, user_id=None, lock=False):
            if lock and "writer" not in pids:
                pids["writer"] = db.scalar(text("SELECT pg_backend_pid()"))
            return real_get(db, kb_id, sid, user_id=user_id, lock=lock)

        def slow_read(*args, **kwargs):
            armed.set()
            release.wait(timeout=20)
            return real_digest(*args, **kwargs)

        def writer():
            try:
                pids["result"] = knowledge._write_chunk(
                    self.ids.kb, session_id, self.ids.maintainer, 0, data, 0)
            except HTTPException as exc:
                errors.append(exc)

        writer_thread = threading.Thread(target=writer, daemon=True)
        with patch.object(upload, "get_session", side_effect=record_writer_pid), \
                patch.object(upload.hashlib, "file_digest", side_effect=slow_read):
            writer_thread.start()
            try:
                self.assertTrue(armed.wait(10))
                with engine.connect() as observer:
                    held = observer.execute(text(
                        "SELECT count(*) FROM pg_locks l JOIN pg_class c "
                        "ON c.oid = l.relation WHERE l.pid = :pid AND l.granted "
                        "AND c.relname IN ('users', 'knowledge_bases')"),
                        {"pid": pids["writer"]}).scalar_one()
                self.assertEqual(held, 0, "分片读取/校验期间仍持有 User/KB 根锁")
                with engine.connect() as other:
                    other.execute(text("SELECT id FROM users WHERE id=:uid FOR UPDATE NOWAIT"),
                                  {"uid": self.ids.maintainer})
                    other.execute(text("SELECT id FROM knowledge_bases WHERE id=:kb "
                                       "FOR UPDATE NOWAIT"), {"kb": self.ids.kb})
                    other.rollback()
            finally:
                release.set()
                writer_thread.join(20)
        self.assertEqual(errors, [])
        self.assertFalse(pids["result"].created, "同片重放应识别为已存在")
        self.assertEqual(pids["result"].received, 1)

    def test_phase2_completed_replay_reauthorizes_after_phase_one_release(self):
        """阶段一提交释放后，阶段二读到 completed 时改变权限也必须重新授权。"""
        data = b"completed replay"
        session_id = self._start_session(data=data, user_id=self.ids.maintainer)
        self._write_all(session_id, data=data, user_id=self.ids.maintainer)
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            knowledge.complete_upload(self.ids.kb, session_id, _request(0), db, maintainer)
        self.assertEqual(self._document_count(), 1)

        real_get = upload.get_session

        def revoke_on_locked_read(db, kb_id, sid, *, user_id=None, lock=False):
            # 阶段一短事务已提交；阶段二首次带锁读会话前完成真实成员写。
            if lock:
                with SessionLocal() as other:
                    other.execute(text(
                        "UPDATE knowledge_base_members SET role='reader' "
                        "WHERE kb_id=:kb AND user_id=:uid"),
                        {"kb": self.ids.kb, "uid": self.ids.maintainer})
                    other.commit()
            return real_get(db, kb_id, sid, user_id=user_id, lock=lock)

        with SessionLocal() as db, patch.object(upload, "get_session",
                                                side_effect=revoke_on_locked_read):
            maintainer = db.get(User, self.ids.maintainer)
            # 阶段二 completed -> 先释放会话锁 -> _replay_completed_upload 新鲜门禁 403。
            self._expect(403, knowledge.complete_upload, self.ids.kb, session_id,
                         _request(0), db, maintainer)
        # 拒绝不破坏已完成的会话：恢复维护权后重放仍返回同一文档，不新增行。
        with SessionLocal() as db:
            db.execute(text(
                "UPDATE knowledge_base_members SET role='maintainer' "
                "WHERE kb_id=:kb AND user_id=:uid"),
                {"kb": self.ids.kb, "uid": self.ids.maintainer})
            db.commit()
        with SessionLocal() as db:
            maintainer = db.get(User, self.ids.maintainer)
            replay = knowledge.complete_upload(self.ids.kb, session_id, _request(0), db,
                                               maintainer)
        self.assertEqual(replay.data.document.id, _claimed_document_id(session_id))
        self.assertEqual(self._document_count(), 1)


if __name__ == "__main__":
    unittest.main()
