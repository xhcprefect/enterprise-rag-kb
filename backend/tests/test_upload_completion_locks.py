"""分片完成发布的离线锁序回归；不连接任何数据库。

A3 契约：完成上传仍是三段式短事务，但根锁复核从"User 存在性 KEY SHARE"
升级为"新鲜 User FOR UPDATE（active + 认证期原 token）+ KB + 当前维护权"。

- 阶段一：User -> KB 根门禁（维护权 + 原 token），提交即释放；
- 阶段二：只持上传会话行锁合并慢写；
- 阶段三：重新按 User -> KB -> 会话取锁发布；已完成会话重放也不得沿用旧权限。

离线替身必须同时提供 ``is_active``/``role``/``token_version``，并在请求上放
认证期 ``auth_token_version`` 快照；缺失快照按生产契约 fail closed（401）。
"""
import hashlib
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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

    @property
    def username(self):
        if self.locks:
            raise AssertionError("取得 KB/会话锁后不得刷新认证 User")
        return "uploader"


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
        # 新根门禁锁的是新鲜 User 行：需要真实的 active/role/token 字段。
        self.fresh_user = SimpleNamespace(
            id=self.uploader_id, username="uploader", is_active=True,
            role="admin", token_version=0,
        )
        # 从该阶段起用户被删除/被撤权，用于验证发布前复核而不是只信任阶段一。
        self.uploader_missing_from_phase = None
        self.fresh_user_token_version = 0
        self.publication_session_exists = True
        self.reference_id = None
        self.db = MagicMock()
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
            if self.phase >= 2 and not self.publication_session_exists:
                return None
            return self.session
        if table == "users":
            if (self.uploader_missing_from_phase is not None
                    and self.phase >= self.uploader_missing_from_phase):
                return None  # 删除/禁用：FOR UPDATE 行不存在 -> 401
            return SimpleNamespace(
                id=self.fresh_user.id, username=self.fresh_user.username,
                is_active=self.fresh_user.is_active, role=self.fresh_user.role,
                token_version=self.fresh_user_token_version,
            )
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
        # 阶段一根锁已释放，合并期间只能持有会话行锁。
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
            # 失败重试三列的 ORM 默认值（真实 INSERT 时由 SQLAlchemy 填）。
            ingest_attempts=0, next_attempt_at=None, last_error_code=None,
        ).items():
            setattr(document, name, value)

    def complete(self):
        request = SimpleNamespace(state=SimpleNamespace(auth_token_version=0))
        return knowledge.complete_upload(
            self.kb_id, self.session_id, request, self.db, self.uploader,
        )

    def test_publication_relocks_user_for_update_then_kb_and_owned_session(self):
        result = self.complete()
        publication_locks = [(table, sql) for phase, table, sql in self.queries
                             if phase == 2 and " FOR " in sql]
        self.assertEqual([table for table, _ in publication_locks],
                         ["users", "knowledge_bases", "upload_sessions"])
        user_sql, kb_sql, session_sql = [sql for _, sql in publication_locks]
        # 认证期原 token / active / 维护权必须在锁后复核：不再是仅 EXISTS 的 KEY SHARE。
        self.assertIn("FROM users", user_sql)
        self.assertTrue(user_sql.endswith("FOR UPDATE"), user_sql)
        self.assertNotIn("FOR KEY SHARE", user_sql)
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

    def test_deleted_uploader_at_root_gate_cleans_only_its_own_orphaned_tmp(self):
        # 阶段一等锁期间用户被删除：FK 已把会话 created_by 置 NULL，任何用户都
        # 不再能合法完成/续传该会话，本请求清理其 tmp 分片。
        self.uploader_missing_from_phase = 0
        self.session.created_by = None
        with self.assertRaises(HTTPException) as error:
            self.complete()
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(error.exception.detail, "登录状态已失效，请重新登录")
        self.db.add.assert_not_called()
        self.audit.assert_not_called()
        self.assertEqual(self.uploader.id_reads, 1)
        self.assertFalse(self.assembled.path.exists())
        self.assertFalse(upload.tmp_dir(self.session_id).exists())
        self.assertFalse([path for path in Path(self.temp.name).rglob("*") if path.is_file()])

    def test_deleted_uploader_does_not_remove_a_concurrently_referenced_assembly(self):
        # 该路径已由并发完成收编为 canonical 文档（合并文件已存在并被引用）。
        self.assembled.path.parent.mkdir(parents=True, exist_ok=True)
        self.assembled.path.write_bytes(b"payload")
        self.uploader_missing_from_phase = 0
        self.session.created_by = None
        self.reference_id = uuid.uuid4()
        with self.assertRaises(HTTPException) as error:
            self.complete()
        self.assertEqual(error.exception.status_code, 401)
        self.db.add.assert_not_called()
        # 清理不得删除被引用的 canonical 文件，只清本次会话的 tmp 分片。
        self.assertEqual(self.assembled.path.read_bytes(), b"payload")
        self.assertFalse(upload.tmp_dir(self.session_id).exists())

    def test_revoked_token_at_root_gate_keeps_recoverable_upload(self):
        # 仅令牌轮换/撤权而账号仍活跃：会话仍可由所有者用新 token 完成，
        # 不得为了清理而破坏可恢复的分片与合并文件。
        self.fresh_user_token_version = 1
        with self.assertRaises(HTTPException) as error:
            self.complete()
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual([table for phase, table, _ in self.queries if phase == 0], ["users"])
        self.db.add.assert_not_called()
        final_files = [path for path in Path(self.temp.name).rglob("*")
                       if path.is_file() and "tmp" not in path.parts]
        self.assertEqual(final_files, [])
        self.assertTrue((upload.tmp_dir(self.session_id) / "0.part").exists())

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
