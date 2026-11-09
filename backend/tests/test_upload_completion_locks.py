"""分片完成发布的离线锁序回归；不连接任何数据库。"""
import hashlib
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import knowledge
from app.models import UploadSession, UploadStatus
from app.services import upload


class _UploaderIdentity:
    """模拟可能过期的认证对象：持有下层锁时重新读属性必须失败。"""

    def __init__(self, user_id, locks):
        self.user_id = user_id
        self.locks = locks
        self.id_reads = 0

    @property
    def id(self):
        if self.locks:
            raise AssertionError("取得 KB/会话锁后不得刷新认证 User")
        self.id_reads += 1
        return self.user_id


class UploadCompletionLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        storage = patch.object(knowledge.settings, "UPLOAD_DIR", self.temp.name)
        storage.start()
        self.addCleanup(storage.stop)
        self.kb_id, self.session_id, self.uploader_id = (uuid.uuid4() for _ in range(3))
        self.kb = SimpleNamespace(id=self.kb_id, kind="upload", name="own-kb")
        self.session = UploadSession(
            id=self.session_id, kb_id=self.kb_id, created_by=self.uploader_id,
            filename="payload.txt", file_ext=".txt", file_size=7, chunk_size=7,
            total_chunks=1, received=[0], status=UploadStatus.UPLOADING.value,
            deduplicated=False,
        )
        self.assembled = upload.AssemblyResult(
            upload.claimed_document_id(self.session),
            upload.final_path(self.kb_id, upload.claimed_document_id(self.session), ".txt"),
            hashlib.sha256(b"payload").hexdigest(),
        )
        chunks = upload.tmp_dir(self.session_id)
        chunks.mkdir(parents=True)
        (chunks / "0.part").write_bytes(b"payload")
        self.phase = 0
        self.queries = []
        self.locks = []
        self.uploader = _UploaderIdentity(self.uploader_id, self.locks)
        self.uploader_exists = True
        self.publication_session_exists = True
        self.reference_id = None
        self.db = Mock()
        self.db.scalar.side_effect = self._scalar
        self.db.commit.side_effect = self._end_transaction
        self.db.rollback.side_effect = self._end_transaction
        self.db.add.side_effect = self._add
        assembly = patch.object(upload, "assemble", side_effect=self._assemble)
        self.assemble = assembly.start()
        self.addCleanup(assembly.stop)
        audit = patch.object(knowledge, "write_audit")
        self.audit = audit.start()
        self.addCleanup(audit.stop)

    def _scalar(self, statement):
        table = statement.get_final_froms()[0].name
        sql = str(statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
        ))
        self.queries.append((self.phase, table, sql))
        if " FOR " in sql:
            self.locks.append((table, sql))
        if table == "knowledge_bases":
            return self.kb
        if table == "upload_sessions":
            if self.phase == 2 and not self.publication_session_exists:
                return None
            return self.session
        if table == "users":
            return self.uploader_id if self.uploader_exists else None
        if table == "documents":
            # canonical 查询返回空；清理的单列 id 查询可模拟并发收编的文件。
            return self.reference_id if len(statement.selected_columns) == 1 else None
        raise AssertionError(f"意外的表查询: {table}")

    def _end_transaction(self):
        self.phase += 1
        self.locks.clear()

    def _assemble(self, db, session):
        self.assertIs(db, self.db)
        self.assertIs(session, self.session)
        self.assertEqual(self.phase, 1)
        # 阶段一已释放 KB，User 锁必须等合并提交后才取。
        self.assertEqual([table for table, _ in self.locks], ["upload_sessions"])
        self.assembled.path.parent.mkdir(parents=True)
        self.assembled.path.write_bytes(b"payload")
        session.document_id = self.assembled.claimed_doc_id
        return self.assembled

    def _add(self, document):
        # 离线 DB 替身补齐真正 INSERT 时才应用的 ORM 默认值，保留真实响应校验。
        for name, value in dict(
            created_at=datetime.now(timezone.utc), chunk_count=0, char_count=0,
            progress=0, cancel_requested=False, index_version=0, maintainer="",
            enabled=True, governance_revision=0,
        ).items():
            setattr(document, name, value)

    def complete(self):
        return knowledge.complete_upload(
            self.kb_id, self.session_id, SimpleNamespace(), self.db, self.uploader,
        )

    def test_publication_uses_real_key_share_before_kb_and_owned_session_locks(self):
        result = self.complete()
        publication_locks = [(table, sql) for phase, table, sql in self.queries
                             if phase == 2 and " FOR " in sql]
        self.assertEqual([table for table, _ in publication_locks],
                         ["users", "knowledge_bases", "upload_sessions"])
        user_sql, kb_sql, session_sql = [sql for _, sql in publication_locks]
        self.assertTrue(user_sql.startswith("SELECT users.id \nFROM users"), user_sql)
        self.assertTrue(user_sql.endswith("FOR KEY SHARE"), user_sql)
        self.assertNotIn("FOR NO KEY UPDATE", user_sql)
        self.assertTrue(kb_sql.endswith("FOR UPDATE"), kb_sql)
        self.assertTrue(session_sql.endswith("FOR UPDATE"), session_sql)
        for _, table, sql in self.queries:
            if table == "upload_sessions":
                self.assertIn(f"upload_sessions.created_by = '{self.uploader_id}'", sql)
        self.assertEqual(self.uploader.id_reads, 1)
        document = self.db.add.call_args.args[0]
        self.assertEqual(document.uploaded_by, self.uploader_id)
        self.assertEqual(result.data.document.id, self.assembled.claimed_doc_id)
        self.assertFalse(result.data.deduplicated)
        self.assertEqual(self.session.status, UploadStatus.COMPLETED.value)
        self.assertEqual(self.db.commit.call_count, 3)
        self.db.rollback.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).rglob("*.txt")), [self.assembled.path])
        self.assertFalse(upload.tmp_dir(self.session_id).exists())

    def test_deleted_uploader_rolls_back_and_cleans_before_lower_publication_locks(self):
        self.uploader_exists = False
        with self.assertRaises(HTTPException) as error:
            self.complete()
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(error.exception.detail, "用户不存在")
        self.assertEqual([table for phase, table, _ in self.queries if phase == 2], ["users"])
        self.assertEqual(self.db.commit.call_count, 2)
        self.db.rollback.assert_called_once()
        self.db.add.assert_not_called()
        self.audit.assert_not_called()
        self.assertEqual(self.uploader.id_reads, 1)
        self.assertFalse(self.assembled.path.exists())
        self.assertFalse(upload.tmp_dir(self.session_id).exists())
        self.assertFalse([path for path in Path(self.temp.name).rglob("*") if path.is_file()])

    def test_deleted_uploader_does_not_remove_a_concurrently_referenced_assembly(self):
        self.uploader_exists = False
        self.reference_id = uuid.uuid4()
        with self.assertRaises(HTTPException) as error:
            self.complete()
        self.assertEqual(error.exception.status_code, 401)
        self.db.rollback.assert_called_once()
        self.db.add.assert_not_called()
        self.assertEqual(self.assembled.path.read_bytes(), b"payload")
        self.assertFalse(upload.tmp_dir(self.session_id).exists())

    def test_deleted_upload_session_still_uses_existing_rollback_and_cleanup(self):
        self.publication_session_exists = False
        with self.assertRaises(HTTPException) as error:
            self.complete()
        self.assertEqual(error.exception.status_code, 404)
        self.db.rollback.assert_called_once()
        self.db.add.assert_not_called()
        self.assertFalse(self.assembled.path.exists())
        self.assertFalse(upload.tmp_dir(self.session_id).exists())


if __name__ == "__main__":
    unittest.main()
