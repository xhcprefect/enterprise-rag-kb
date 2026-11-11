"""文档内容身份：两个上传入口的服务端摘要、去重、并发与清理（真实 PostgreSQL）。

只接受显式提供、库名以 ``_test`` 结尾的 TEST_DATABASE_URL；未提供时整体跳过。
"""
import hashlib
import io
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from fastapi import HTTPException, Request
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

if TEST_URL:
    from app.db import session as db_session

    _target = make_url(TEST_URL)
    if db_session.engine.url != _target:
        db_session.engine.dispose()
        db_session.engine = create_engine(TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
        db_session.SessionLocal.configure(bind=db_session.engine)

    from app.core.config import settings
    from app.db.init_db import init_db
    from app.models import Document, DocumentStatus, KnowledgeBase, UploadSession, User
    from app.api.v1 import knowledge
    from app.services import upload
    from app.services.document_identity import file_sha256

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


def _document_tables_reset():
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE users, knowledge_bases, documents, upload_sessions, audit_logs, document_chunks "
            "RESTART IDENTITY CASCADE"
        ))


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DocumentIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        _document_tables_reset()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for name, value in (("UPLOAD_DIR", self.temp.name), ("UPLOAD_CHUNK_SIZE", 4)):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})
        with SessionLocal() as db:
            self.admin = User(username="doc_admin", role="admin", is_active=True, password_hash="x")
            db.add(self.admin)
            db.flush()
            self.kb = KnowledgeBase(name="doc-kb", collection_name="doc_" + uuid.uuid4().hex,
                                    owner_id=self.admin.id, is_public=True)
            self.other_kb = KnowledgeBase(name="other-kb", collection_name="doc_" + uuid.uuid4().hex,
                                          owner_id=self.admin.id, is_public=True)
            db.add_all([self.kb, self.other_kb])
            db.commit()
            self.admin_id = self.admin.id
            self.kb_id, self.other_kb_id = self.kb.id, self.other_kb.id
            self.request.state.auth_token_version = self.admin.token_version

    # ---- 辅助 ----
    def small_upload(self, data: bytes, *, kb_id=None, filename="same.txt"):
        with SessionLocal() as db:
            file = knowledge.UploadFile(filename=filename, file=io.BytesIO(data))
            response = knowledge.upload_document(kb_id or self.kb_id, self.request, file, db, self.admin)
            return response.data

    def chunked_upload(self, data: bytes, *, kb_id=None, filename="same.txt", declared=None):
        with SessionLocal() as db:
            session = upload.create_or_resume(db, kb_id or self.kb_id, filename, len(data), ".txt",
                                              self.admin_id, declared)
            session_id = session.id
        chunk_size = 4
        for index in range((len(data) + chunk_size - 1) // chunk_size):
            knowledge._write_chunk(kb_id or self.kb_id, session_id, self.admin_id, index,
                                   data[index * chunk_size:(index + 1) * chunk_size],
                                   self.request.state.auth_token_version)
        with SessionLocal() as db:
            return knowledge.complete_upload(kb_id or self.kb_id, session_id, self.request, db, self.admin)

    def document_count(self) -> int:
        with SessionLocal() as db:
            return db.scalar(select(func.count()).select_from(Document)) or 0

    # ---- 小文件入口 ----
    def test_small_upload_persists_server_digest_and_deduplicates(self):
        data = b"identical content"
        first = self.small_upload(data)
        self.assertFalse(first.deduplicated)
        with SessionLocal() as db:
            row = db.get(Document, first.id)
            self.assertEqual(row.content_sha256, hashlib.sha256(data).hexdigest())
        second = self.small_upload(data, filename="renamed.txt")
        self.assertTrue(second.deduplicated)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.filename, "same.txt")  # 不改名、不改旧引用
        self.assertEqual(self.document_count(), 1)
        # 复用后只留下 canonical 文件，本次写入的临时文件已清理
        files = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)

    def test_small_upload_uses_real_bytes_not_name_or_size(self):
        first = self.small_upload(b"AAAA")
        second = self.small_upload(b"BBBB")
        self.assertNotEqual(first.id, second.id)
        self.assertFalse(second.deduplicated)

    def test_cross_knowledge_base_is_never_deduplicated(self):
        data = b"cross kb content"
        first = self.small_upload(data)
        second = self.small_upload(data, kb_id=self.other_kb_id)
        self.assertNotEqual(first.id, second.id)
        self.assertFalse(second.deduplicated)
        self.assertEqual(self.document_count(), 2)

    def test_small_upload_race_creates_one_canonical(self):
        data = b"race content"
        barrier = Barrier(2)

        def run(_):
            barrier.wait()
            with SessionLocal() as db:
                file = knowledge.UploadFile(filename="race.txt", file=io.BytesIO(data))
                return (knowledge.upload_document(self.kb_id, self.request, file, db, self.admin)).data.id

        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(run, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.document_count(), 1)

    # ---- 分片入口 ----
    def test_chunked_upload_computes_digest_without_declaration_and_dedups(self):
        data = b"chunked content"
        first = self.chunked_upload(data)  # 老客户端：不声明 hash
        self.assertFalse(first.data.deduplicated)
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, first.data.document.id).content_sha256,
                             hashlib.sha256(data).hexdigest())
        second = self.chunked_upload(data, filename="another.txt")
        self.assertTrue(second.data.deduplicated)
        self.assertEqual(second.data.document.id, first.data.document.id)
        self.assertEqual(self.document_count(), 1)
        files = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)

    def test_declared_hash_is_only_used_for_integrity(self):
        data = b"integrity content"
        wrong = hashlib.sha256(b"different").hexdigest()
        with self.assertRaises(HTTPException) as error:
            self.chunked_upload(data, declared=wrong)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.document_count(), 0)
        # 声明正确的摘要可以完成，但落库仍是服务端自己算的值
        result = self.chunked_upload(data, declared=hashlib.sha256(data).hexdigest())
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, result.data.document.id).content_sha256,
                             hashlib.sha256(data).hexdigest())

    def test_complete_replay_keeps_deduplicated_flag(self):
        data = b"replay content"
        first = self.chunked_upload(data)
        with SessionLocal() as db:
            session_id = db.scalar(select(UploadSession.id).where(UploadSession.document_id == first.data.document.id))
        with SessionLocal() as db:
            replay = knowledge.complete_upload(self.kb_id, session_id, self.request, db, self.admin)
        self.assertEqual(replay.data.document.id, first.data.document.id)
        self.assertFalse(replay.data.deduplicated)

        dedup = self.chunked_upload(data, filename="replay-2.txt")
        with SessionLocal() as db:
            session_id = db.scalar(select(UploadSession.id).where(
                UploadSession.document_id == first.data.document.id,
                UploadSession.deduplicated.is_(True)))
        with SessionLocal() as db:
            replay = knowledge.complete_upload(self.kb_id, session_id, self.request, db, self.admin)
        self.assertTrue(replay.data.deduplicated)
        self.assertEqual(replay.data.document.id, dedup.data.document.id)

    def test_small_then_chunked_same_content_reuses_small_document(self):
        data = b"cross entry content"
        first = self.small_upload(data)
        result = self.chunked_upload(data, filename="chunked-name.txt")
        self.assertTrue(result.data.deduplicated)
        self.assertEqual(result.data.document.id, first.id)
        self.assertEqual(self.document_count(), 1)

    def test_failed_and_cancelled_documents_are_reused_without_hidden_retry(self):
        data = b"failed content"
        first = self.small_upload(data)
        for status in (DocumentStatus.FAILED.value, DocumentStatus.CANCELLED.value):
            with SessionLocal() as db:
                db.get(Document, first.id).status = status
                db.commit()
            reused = self.small_upload(data, filename="retry.txt")
            self.assertTrue(reused.deduplicated)
            self.assertEqual(reused.id, first.id)
            self.assertEqual(reused.status, status)
            # 显式重新入库是唯一的重试方式
            with SessionLocal() as db:
                result = knowledge.reindex_document(self.kb_id, first.id, self.request, db, self.admin)
            self.assertEqual(result.data.status, DocumentStatus.PENDING.value)

    # ---- 治理：inactive canonical 不得被复用/复活，重索引不得重置治理 ----
    def _set_governance(self, doc_id, **fields):
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            for name, value in fields.items():
                setattr(doc, name, value)
            db.commit()

    def test_small_upload_inactive_canonical_is_409_not_revived(self):
        data = b"governed content"
        first = self.small_upload(data)
        self._set_governance(first.id, superseded_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        for governance in ({"superseded_at": datetime(2026, 1, 1, tzinfo=timezone.utc)},
                           {"enabled": False},
                           {"valid_until": datetime(2000, 1, 1, tzinfo=timezone.utc)},
                           {"valid_from": datetime(2999, 1, 1, tzinfo=timezone.utc)}):
            self._set_governance(first.id, **{"superseded_at": None, "enabled": True,
                                              "valid_from": None, "valid_until": None})
            self._set_governance(first.id, **governance)
            with self.assertRaises(HTTPException) as error:
                self.small_upload(data, filename=f"retry-{uuid.uuid4().hex}.txt")
            self.assertEqual(error.exception.status_code, 409, str(governance))
            # 不复活：行仍是原治理状态，且没有产生第二份 canonical
            self.assertEqual(self.document_count(), 1)
            doc = self.fresh_document(first.id)
            self.assertEqual(doc.governance_revision, 0)
        # 本次被拒上传写入的 tmp 文件也必须清干净
        files = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        self.assertEqual(len(files), 1)

    def test_chunked_complete_inactive_canonical_is_409(self):
        data = b"chunked governed"
        first = self.chunked_upload(data)
        self._set_governance(first.data.document.id, enabled=False)
        with self.assertRaises(HTTPException) as error:
            self.chunked_upload(data, filename="again.txt")
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.document_count(), 1)
        # 本次内容明确不可采用（inactive canonical）：不产生新 Document，本次合并
        # 文件与分片由请求自己清理，只保留 canonical 及其文件。
        canonical_path = Path(self.fresh_document(first.data.document.id).stored_path)
        remaining = {p for p in Path(self.temp.name).rglob("*") if p.is_file()}
        self.assertEqual(remaining, {canonical_path})
        # TTL/主动放弃路径也不会误删 canonical。
        with SessionLocal() as db:
            for session in db.scalars(select(UploadSession)).all():
                session.updated_at = datetime.now(timezone.utc) - timedelta(hours=48)
            db.commit()
            upload.cleanup_expired(db)
        self.assertTrue(canonical_path.is_file())
        left = {p for p in Path(self.temp.name).rglob("*") if p.is_file()}
        self.assertEqual(left, {canonical_path})

    def test_completed_session_replay_of_inactive_canonical_is_409(self):
        data = b"replay governed"
        first = self.chunked_upload(data)
        with SessionLocal() as db:
            session_id = db.scalar(select(UploadSession.id).where(
                UploadSession.document_id == first.data.document.id,
                UploadSession.deduplicated.is_(False)))
        self._set_governance(first.data.document.id,
                             superseded_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                             status=DocumentStatus.READY.value)
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                knowledge.complete_upload(self.kb_id, session_id, self.request, db, self.admin)
        self.assertEqual(error.exception.status_code, 409)
        # 重放被拒既不改治理、也不改入库状态
        doc = self.fresh_document(first.data.document.id)
        self.assertIsNotNone(doc.superseded_at)
        self.assertEqual(doc.governance_revision, 0)
        self.assertEqual(doc.status, DocumentStatus.READY.value)

    def test_reindex_keeps_governance_metadata(self):
        data = b"reindex governed"
        first = self.small_upload(data)
        valid_until = datetime(2999, 1, 1, tzinfo=timezone.utc)
        reviewed = datetime(2026, 5, 1, tzinfo=timezone.utc)
        self._set_governance(first.id, maintainer="ops", last_reviewed_at=reviewed,
                             valid_until=valid_until, index_version=4,
                             status=DocumentStatus.READY.value)
        with SessionLocal() as db:
            result = knowledge.reindex_document(self.kb_id, first.id, self.request, db, self.admin)
        self.assertEqual(result.data.status, DocumentStatus.PENDING.value)
        doc = self.fresh_document(first.id)
        self.assertEqual(doc.maintainer, "ops")
        self.assertEqual(doc.last_reviewed_at, reviewed)
        self.assertEqual(doc.valid_until, valid_until)
        self.assertEqual(doc.index_version, 5)   # 只推进代次
        self.assertEqual(doc.governance_revision, 0)

    def test_cancel_keeps_governance_metadata(self):
        data = b"cancel governed"
        first = self.small_upload(data)
        valid_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._set_governance(first.id, maintainer="ops", valid_from=valid_from,
                             status=DocumentStatus.PENDING.value)
        with SessionLocal() as db:
            knowledge.cancel_document(self.kb_id, first.id, self.request, db, self.admin)
        doc = self.fresh_document(first.id)
        self.assertEqual(doc.maintainer, "ops")
        self.assertEqual(doc.valid_from, valid_from)
        self.assertTrue(doc.cancel_requested)
        self.assertEqual(doc.governance_revision, 0)

    def fresh_document(self, doc_id):
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            db.refresh(doc)
            return doc

    def test_missing_local_file_after_dedup_cleans_only_own_copy(self):
        data = b"cleanup content"
        canonical = self.small_upload(data)
        with SessionLocal() as db:
            canonical_row = db.get(Document, canonical.id)
            canonical_path = Path(canonical_row.stored_path)
        self.assertTrue(canonical_path.is_file())
        self.small_upload(data, filename="copy.txt")
        self.assertTrue(canonical_path.is_file())
        files = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        self.assertEqual(files, [canonical_path])

    def test_delete_kb_between_slow_write_and_commit_cleans_owned_file(self):
        """慢写提交后、阶段三重锁前删除知识库：会话行消失，文件不能变孤儿。

        用 assemble 的包装器模拟"phase2 提交之后删除 KB"这一窗口，
        因为真实并发线程难以精确命中该时刻。
        """
        data = b"delete-race content"
        with SessionLocal() as db:
            session = upload.create_or_resume(db, self.kb_id, "race.txt", len(data), ".txt",
                                              self.admin_id, None)
            session_id = session.id
        for index in range((len(data) + 3) // 4):
            knowledge._write_chunk(self.kb_id, session_id, self.admin_id, index,
                                   data[index * 4:(index + 1) * 4], self.request.state.auth_token_version)
        real_assemble = upload.assemble

        def assemble_then_delete(db, session):
            result = real_assemble(db, session)
            db.commit()  # phase2 提交
            with SessionLocal() as other:
                knowledge.delete_kb(self.kb_id, self.request, other, self.admin)
            return result

        with SessionLocal() as db, patch.object(upload, "assemble", side_effect=assemble_then_delete):
            with self.assertRaises(HTTPException) as error:
                knowledge.complete_upload(self.kb_id, session_id, self.request, db, self.admin)
        self.assertEqual(error.exception.status_code, 404)
        # tmp 分片随会话删除由 TTL 清理；关键是合并后的正式文件不能成为孤儿
        remaining = [p for p in Path(self.temp.name).rglob("*")
                     if p.is_file() and "tmp" not in p.parts]
        self.assertEqual(remaining, [])  # 本次合并出的文件已清理，无孤儿

    def test_abort_between_slow_write_and_commit_cleans_owned_file(self):
        data = b"abort-race content"
        with SessionLocal() as db:
            session = upload.create_or_resume(db, self.kb_id, "abort.txt", len(data), ".txt",
                                              self.admin_id, None)
            session_id = session.id
        for index in range((len(data) + 3) // 4):
            knowledge._write_chunk(self.kb_id, session_id, self.admin_id, index,
                                   data[index * 4:(index + 1) * 4], self.request.state.auth_token_version)
        real_assemble = upload.assemble

        def assemble_then_abort(db, session):
            result = real_assemble(db, session)
            db.commit()
            with SessionLocal() as other:
                upload.abort_session(other, other.get(UploadSession, session_id))
            return result

        with SessionLocal() as db, patch.object(upload, "assemble", side_effect=assemble_then_abort):
            with self.assertRaises(HTTPException) as error:
                knowledge.complete_upload(self.kb_id, session_id, self.request, db, self.admin)
        self.assertEqual(error.exception.status_code, 409)
        remaining = [p for p in Path(self.temp.name).rglob("*") if p.is_file()]
        self.assertEqual(remaining, [])

    def test_cleanup_never_removes_file_claimed_by_another_document(self):
        """并发完成已把同一路径收编为 canonical 时，失败清理不得删除它。"""
        data = b"canonical guard"
        with SessionLocal() as db:
            session = upload.create_or_resume(db, self.kb_id, "guard.txt", len(data), ".txt",
                                              self.admin_id, None)
            session_id = session.id
        for index in range((len(data) + 3) // 4):
            knowledge._write_chunk(self.kb_id, session_id, self.admin_id, index,
                                   data[index * 4:(index + 1) * 4], self.request.state.auth_token_version)
        real_assemble = upload.assemble

        def assemble_then_abort(db, session):
            result = real_assemble(db, session)
            db.commit()
            # 模拟另一个请求已用同一路径创建了 canonical 文档
            with SessionLocal() as other:
                other.add(Document(kb_id=self.kb_id, filename="guard.txt", file_ext=".txt",
                                   file_size=len(data), stored_path=str(result.path),
                                   content_sha256=result.digest, status="ready"))
                other.commit()
                upload.abort_session(other, other.get(UploadSession, session_id))
            return result

        with SessionLocal() as db, patch.object(upload, "assemble", side_effect=assemble_then_abort):
            with self.assertRaises(HTTPException):
                knowledge.complete_upload(self.kb_id, session_id, self.request, db, self.admin)
        with SessionLocal() as db:
            canonical = db.scalar(select(Document).where(Document.filename == "guard.txt"))
        self.assertTrue(Path(canonical.stored_path).is_file())

    def test_abort_after_phase2_removes_unpublished_final(self):
        """进程在 phase2 提交后退出：之后主动放弃必须清掉已落盘的正式文件。"""
        data = b"orphan after crash"
        with SessionLocal() as db:
            session = upload.create_or_resume(db, self.kb_id, "crash.txt", len(data), ".txt",
                                              self.admin_id, None)
            session_id = session.id
        for index in range((len(data) + 3) // 4):
            knowledge._write_chunk(self.kb_id, session_id, self.admin_id, index,
                                   data[index * 4:(index + 1) * 4], self.request.state.auth_token_version)
        with SessionLocal() as db:
            session = upload.get_session(db, self.kb_id, session_id, lock=True)
            assembled = upload.assemble(db, session)
            db.commit()  # 模拟 phase2 提交后进程退出，phase3 从未执行
        self.assertTrue(assembled.path.is_file())
        with SessionLocal() as db:
            upload.abort_session(db, upload.get_session(db, self.kb_id, session_id))
        self.assertFalse(assembled.path.exists())
        remaining = [p for p in Path(self.temp.name).rglob("*")
                     if p.is_file() and "tmp" not in p.parts]
        self.assertEqual(remaining, [])

    def test_ttl_cleanup_removes_unpublished_final_but_keeps_referenced(self):
        """TTL 过期同样清理阶段二落盘的文件；已被文档收编的路径必须保留。"""
        orphan_data = b"ttl orphan"
        kept_data = b"ttl kept"
        sessions = {}
        for name, data in (("orphan.txt", orphan_data), ("kept.txt", kept_data)):
            with SessionLocal() as db:
                session = upload.create_or_resume(db, self.kb_id, name, len(data), ".txt",
                                                  self.admin_id, None)
                sessions[name] = session.id
            for index in range((len(data) + 3) // 4):
                knowledge._write_chunk(self.kb_id, sessions[name], self.admin_id, index,
                                       data[index * 4:(index + 1) * 4], self.request.state.auth_token_version)

        assembled = {}
        with SessionLocal() as db:
            for name in sessions:
                session = upload.get_session(db, self.kb_id, sessions[name], lock=True)
                assembled[name] = upload.assemble(db, session)
                session.updated_at = datetime.now(timezone.utc) - timedelta(hours=48)
                db.commit()
        # kept.txt 的路径已被一个文档引用（模拟另一请求收编）
        with SessionLocal() as db:
            db.add(Document(kb_id=self.kb_id, filename="kept.txt", file_ext=".txt",
                            file_size=len(kept_data), stored_path=str(assembled["kept.txt"].path),
                            content_sha256=assembled["kept.txt"].digest, status="ready"))
            db.commit()
            removed = upload.cleanup_expired(db)
        self.assertGreaterEqual(removed, 2)
        self.assertFalse(assembled["orphan.txt"].path.exists())
        self.assertTrue(assembled["kept.txt"].path.is_file())

    def test_partial_unique_index_ignores_null_hashes(self):
        with SessionLocal() as db:
            db.add_all([
                Document(kb_id=self.kb_id, filename="legacy-a.txt", file_ext=".txt", file_size=1,
                         stored_path=str(Path(self.temp.name) / "a.txt")),
                Document(kb_id=self.kb_id, filename="legacy-b.txt", file_ext=".txt", file_size=1,
                         stored_path=str(Path(self.temp.name) / "b.txt")),
            ])
            db.commit()
        self.assertEqual(self.document_count(), 2)  # 老 NULL 行可共存
        digest = hashlib.sha256(b"unique").hexdigest()
        with SessionLocal() as db:
            db.add(Document(kb_id=self.kb_id, filename="one.txt", file_ext=".txt", file_size=1,
                            stored_path=str(Path(self.temp.name) / "one.txt"), content_sha256=digest))
            db.commit()
        with SessionLocal() as db, self.assertRaises(Exception):
            db.add(Document(kb_id=self.kb_id, filename="two.txt", file_ext=".txt", file_size=1,
                            stored_path=str(Path(self.temp.name) / "two.txt"), content_sha256=digest))
            db.commit()


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class BackfillScriptTests(unittest.TestCase):
    """backfill 直接调用函数（dry-run/--apply/冲突/缺文件），只在专用测试库。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        script = Path(__file__).resolve().parent.parent / "scripts" / "backfill_document_hashes.py"
        spec = importlib.util.spec_from_file_location("backfill_document_hashes", script)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setUp(self):
        with engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, documents, upload_sessions, audit_logs, document_chunks "
                "RESTART IDENTITY CASCADE"
            ))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(settings, "UPLOAD_DIR", self.temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        with SessionLocal() as db:
            user = User(username="backfill_admin", role="admin", is_active=True, password_hash="x")
            db.add(user)
            db.flush()
            self.kb = KnowledgeBase(name="backfill-kb", collection_name="b_" + uuid.uuid4().hex,
                                    owner_id=user.id, is_public=True)
            db.add(self.kb)
            db.commit()
            self.kb_id = self.kb.id

    def add(self, name, data, digest=None, path=None):
        target = Path(path or (Path(self.temp.name) / f"{uuid.uuid4().hex}-{name}"))
        if data is not None and path is None:
            target.write_bytes(data)
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename=name, file_ext=".txt", file_size=1,
                           stored_path=str(target), content_sha256=digest)
            db.add(doc)
            db.commit()
            return doc.id, target

    def run_backfill(self, apply):
        with patch("builtins.print"):
            return self.module.backfill(apply)

    def test_dry_run_writes_nothing_and_apply_fills_only_canonical(self):
        data = b"duplicate bytes"
        first, first_path = self.add("a.txt", data)
        second, _ = self.add("b.txt", data)
        third, third_path = self.add("c.txt", b"unique")
        self.assertEqual(self.run_backfill(False), 0)  # dry-run 可正常打印，不触发 NameError
        with SessionLocal() as db:
            self.assertIsNone(db.get(Document, first).content_sha256)
            self.assertIsNone(db.get(Document, third).content_sha256)

        self.assertEqual(self.run_backfill(True), 0)
        digest = hashlib.sha256(data).hexdigest()
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, first).content_sha256, digest)
            self.assertIsNone(db.get(Document, second).content_sha256)  # 重复行保持 NULL
            self.assertEqual(db.get(Document, third).content_sha256,
                             hashlib.sha256(b"unique").hexdigest())
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 3)  # 不删行

    def test_existing_hash_must_match_real_file_bytes(self):
        path = Path(self.temp.name) / "wrong.txt"
        path.write_bytes(b"actual bytes")
        wrong = hashlib.sha256(b"something else").hexdigest()
        doc_id, _ = self.add("wrong.txt", None, digest=wrong, path=path)
        self.assertEqual(self.run_backfill(True), 1)  # 冲突 → 非零
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, doc_id).content_sha256, wrong)  # 不覆盖

    def test_missing_and_untrusted_paths_are_reported_not_touched(self):
        missing_id, _ = self.add("gone.txt", None, path=Path(self.temp.name) / "does-not-exist.txt")
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        stray = Path(outside.name) / "stray.txt"
        stray.write_bytes(b"x")
        stray_id, _ = self.add("stray.txt", None, path=stray)
        self.assertEqual(self.run_backfill(True), 1)
        with SessionLocal() as db:
            self.assertIsNone(db.get(Document, missing_id).content_sha256)
            self.assertIsNone(db.get(Document, stray_id).content_sha256)
        self.assertTrue(stray.is_file())  # 不可信路径绝不读取/移动/删除

    def test_existing_correct_hash_is_selected_as_canonical(self):
        data = b"canonical pick"
        digest = hashlib.sha256(data).hexdigest()
        second, _ = self.add("second.txt", data)
        first_with_hash, _ = self.add("first.txt", data, digest=digest,
                                      path=Path(self.temp.name) / "first.txt")
        Path(self.temp.name, "first.txt").write_bytes(data)
        self.assertEqual(self.run_backfill(True), 0)
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, first_with_hash).content_sha256, digest)
            self.assertIsNone(db.get(Document, second).content_sha256)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class BackfillShapeTests(unittest.TestCase):
    """回填脚本的核心判定（可信根、缺文件、组 canonical）不在测试里写业务库。"""

    def test_trusted_upload_file_rejects_outside_and_directories(self):
        from app.services.document_identity import is_trusted_upload_file

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            inside = Path(root) / "a.txt"
            inside.write_bytes(b"x")
            self.assertTrue(is_trusted_upload_file(str(inside), Path(root)))
            self.assertFalse(is_trusted_upload_file(str(Path(root)), Path(root)))
            stray = Path(outside) / "b.txt"
            stray.write_bytes(b"x")
            self.assertFalse(is_trusted_upload_file(str(stray), Path(root)))
            self.assertFalse(is_trusted_upload_file(str(Path(root) / "missing.txt"), Path(root)))

    def test_file_sha256_matches_content(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "a.bin"
            path.write_bytes(b"abcdefgh")
            self.assertEqual(file_sha256(path), hashlib.sha256(b"abcdefgh").hexdigest())
