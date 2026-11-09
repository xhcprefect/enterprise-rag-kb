"""文档内容身份相关迁移：新/旧 schema 幂等、部分唯一索引、老行不删除（真实 PostgreSQL）。

破坏性读写前先走共享门 ``tests._memory_project_test_gate``（不放宽原门）：
loopback / ``_test`` 后缀 / owner 标签格式 / 实际 engine 逐字段一致 /
``pg_database`` comment 与 owner 精确匹配，不符即硬 fail。
"""
import hashlib
import unittest
import uuid

from datetime import datetime, timezone

from tests import _memory_project_test_gate as gate
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
    from app.models import Document, KnowledgeBase, UploadSession, User

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DocumentMigrationTests(unittest.TestCase):
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
        with engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, documents, upload_sessions, audit_logs, document_chunks "
                "RESTART IDENTITY CASCADE"
            ))
        with SessionLocal() as db:
            user = User(username="migrate_admin", role="admin", is_active=True, password_hash="x")
            db.add(user)
            db.flush()
            self.user_id = user.id
            self.kb = KnowledgeBase(name="migrate-kb", collection_name="m_" + uuid.uuid4().hex,
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

    def indexes(self):
        with engine.begin() as conn:
            return {
                row[0] for row in conn.execute(text(
                    "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"
                ))
            }

    def add_legacy_document(self, filename, digest=None):
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename=filename, file_ext=".txt", file_size=1,
                           stored_path=f"/legacy/{uuid.uuid4().hex}.txt", content_sha256=digest)
            db.add(doc)
            db.commit()
            return doc.id

    def column_details(self):
        with engine.begin() as conn:
            return {
                row[0]: {"nullable": row[1], "default": row[2]}
                for row in conn.execute(text(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'documents'"
                ))
            }

    def superseded_fk(self):
        """返回 (constraint_name, delete_rule) 或 None。

        只看当前 schema 的 documents 表（按 OID 锚定），其他 schema 的
        同名表/FK 一律不算，避免跨 schema 串扰误判。
        """
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT c.conname, c.confdeltype FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = c.conkey[1] "
                "JOIN pg_class rt ON rt.oid = c.confrelid "
                "WHERE c.contype = 'f' AND t.relname = 'documents' "
                "AND n.nspname = current_schema() "
                "AND cardinality(c.conkey) = 1 "
                "AND a.attname = 'superseded_by_id' AND rt.relname = 'documents' LIMIT 1"
            )).first()
            return None if row is None else (row[0], row[1])

    def test_double_init_is_idempotent_and_creates_new_columns(self):
        init_db(create_admin=False)
        init_db(create_admin=False)
        columns = self.columns()
        self.assertIn(("documents", "content_sha256"), columns)
        self.assertIn(("upload_sessions", "deduplicated"), columns)
        self.assertIn(("conversations", "pinned_at"), columns)
        # 治理列（T3 冻结契约）也必须在新/旧库两遍 init 后齐备
        self.assertIn(("documents", "maintainer"), columns)
        self.assertIn(("documents", "enabled"), columns)
        self.assertIn(("documents", "governance_revision"), columns)
        self.assertIn(("documents", "superseded_by_id"), columns)
        self.assertIn("uq_documents_kb_content_sha256", self.indexes())

    def test_governance_server_defaults_and_self_fk(self):
        """新库也必须有真实 server default 与自引用 FK（ON DELETE SET NULL）。"""
        init_db(create_admin=False)
        details = self.column_details()
        self.assertIsNotNone(details["enabled"]["default"])
        self.assertIn("true", details["enabled"]["default"].lower())
        self.assertIsNotNone(details["governance_revision"]["default"])
        self.assertIn("0", details["governance_revision"]["default"])
        self.assertIsNotNone(details["maintainer"]["default"])
        self.assertIn("''", details["maintainer"]["default"])
        fk = self.superseded_fk()
        self.assertIsNotNone(fk, "documents.superseded_by_id 自引用外键缺失")
        self.assertEqual(fk[1], "n")  # confdeltype 'n' = SET NULL

    def test_existing_column_without_fk_gains_fk_on_init(self):
        """列已存在但 FK 缺失（上一版迁移补的裸列）也必须能补上 FK。"""
        init_db(create_admin=False)
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_superseded_by_id_documents"
            ))
            conn.execute(text(
                "ALTER TABLE documents ALTER COLUMN enabled DROP DEFAULT"
            ))
            conn.execute(text("ALTER TABLE documents ALTER COLUMN governance_revision DROP DEFAULT"))
            conn.execute(text("ALTER TABLE documents ALTER COLUMN maintainer DROP DEFAULT"))
        self.assertIsNone(self.superseded_fk())
        init_db(create_admin=False)
        init_db(create_admin=False)  # 幂等：第二遍不能重复加约束
        fk = self.superseded_fk()
        self.assertIsNotNone(fk)
        self.assertEqual(fk[1], "n")
        details = self.column_details()
        self.assertIn("true", details["enabled"]["default"].lower())
        self.assertIn("0", details["governance_revision"]["default"])
        self.assertIn("''", details["maintainer"]["default"])

    def test_missing_fk_added_despite_same_name_fk_in_other_schema(self):
        """当前表无自 FK、旁 schema 的同名 documents 有自 FK 时，init 仍必须给当前表补上。

        只修 FK 缺失，不碰旁 schema（测试结束恢复原状），两次 init 幂等。
        """
        init_db(create_admin=False)
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE documents DROP CONSTRAINT IF EXISTS fk_documents_superseded_by_id_documents"
            ))
        self.assertIsNone(self.superseded_fk())
        conn = engine.connect()
        conn.execution_options(isolation_level="AUTOCOMMIT")
        try:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS t3a1_fk_shadow"))
            conn.execute(text(
                "CREATE TABLE IF NOT EXISTS t3a1_fk_shadow.documents ("
                "id UUID PRIMARY KEY, superseded_by_id UUID)"
            ))
            conn.execute(text(
                "ALTER TABLE t3a1_fk_shadow.documents DROP CONSTRAINT IF EXISTS shadow_self_fk"
            ))
            conn.execute(text(
                "ALTER TABLE t3a1_fk_shadow.documents ADD CONSTRAINT shadow_self_fk "
                "FOREIGN KEY (superseded_by_id) REFERENCES t3a1_fk_shadow.documents(id) "
                "ON DELETE SET NULL"
            ))
        finally:
            conn.close()
        try:
            init_db(create_admin=False)
            init_db(create_admin=False)  # 幂等
            fk = self.superseded_fk()
            self.assertIsNotNone(fk, "旁 schema 有同名 FK 时当前表的自 FK 仍必须补齐")
            self.assertEqual(fk[1], "n")
        finally:
            cleanup = engine.connect()
            cleanup.execution_options(isolation_level="AUTOCOMMIT")
            try:
                cleanup.execute(text("DROP SCHEMA IF EXISTS t3a1_fk_shadow CASCADE"))
            finally:
                cleanup.close()

    def test_deleting_replacement_keeps_original_superseded(self):
        """删除替代文档只把链接置 NULL，保留 superseded_at（原文档仍不可检索）。"""
        original = self.add_legacy_document("orig.txt")
        replacement = self.add_legacy_document("repl.txt")
        with SessionLocal() as db:
            row = db.get(Document, original)
            row.superseded_at = datetime.now(timezone.utc)
            row.superseded_by_id = replacement
            db.commit()
        with SessionLocal() as db:
            db.delete(db.get(Document, replacement))
            db.commit()
        with SessionLocal() as db:
            row = db.get(Document, original)
            self.assertIsNotNone(row.superseded_at)
            self.assertIsNone(row.superseded_by_id)

    def test_old_schema_migration_preserves_null_rows_and_metadata(self):
        legacy = [self.add_legacy_document(f"old-{index}.txt") for index in range(3)]
        with engine.begin() as conn:
            # 删列会连带删除依赖它的部分唯一索引，无需再显式 DROP INDEX
            conn.execute(text("ALTER TABLE documents DROP COLUMN content_sha256"))
            conn.execute(text("ALTER TABLE upload_sessions DROP COLUMN deduplicated"))
            conn.execute(text("ALTER TABLE conversations DROP COLUMN pinned_at"))
            conn.execute(text("ALTER TABLE documents DROP COLUMN governance_revision"))
            conn.execute(text("ALTER TABLE documents DROP COLUMN superseded_by_id"))
            conn.execute(text("ALTER TABLE documents DROP COLUMN enabled"))
            conn.execute(text("DROP INDEX IF EXISTS uq_documents_kb_content_sha256"))
        init_db(create_admin=False)
        init_db(create_admin=False)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 3)
            for doc_id in legacy:
                self.assertIsNone(db.get(Document, doc_id).content_sha256)  # 老行保持 NULL
                row = db.get(Document, doc_id)
                self.assertTrue(row.enabled)  # 治理列补齐时默认有效
                self.assertEqual(row.governance_revision, 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(UploadSession)), 0)
        self.assertIn("uq_documents_kb_content_sha256", self.indexes())

    def test_partial_index_allows_many_null_but_one_digest_per_kb(self):
        digest = hashlib.sha256(b"same").hexdigest()
        self.add_legacy_document("a.txt")
        self.add_legacy_document("b.txt")
        self.add_legacy_document("c.txt", digest)
        with SessionLocal() as db, self.assertRaises(Exception):
            db.add(Document(kb_id=self.kb_id, filename="d.txt", file_ext=".txt", file_size=1,
                            stored_path="/legacy/d.txt", content_sha256=digest))
            db.commit()
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 3)
            self.assertEqual(db.scalar(select(func.count()).select_from(Document).where(
                Document.content_sha256.is_(None))), 2)

    def test_duplicate_hashes_fail_closed_without_deleting_rows(self):
        digest = hashlib.sha256(b"dup").hexdigest()
        self.add_legacy_document("dup-a.txt", digest)
        # 绕过唯一索引制造历史重复（模拟从旧库/手工导入的数据）
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_documents_kb_content_sha256"))
        self.add_legacy_document("dup-b.txt", digest)
        with self.assertRaises(RuntimeError) as error:
            init_db(create_admin=False)
        message = str(error.exception)
        self.assertIn("uq_documents_kb_content_sha256", message)
        # 诊断里给出可操作信息与具体行 id
        self.assertIn("kb=", message)
        self.assertIn("置为 NULL", message)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 2)  # 一行都没删
        self.assertNotIn("uq_documents_kb_content_sha256", self.indexes())
        # 人工确认 canonical 后把其余行置 NULL（不删文档），再次初始化即可建索引
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE documents SET content_sha256 = NULL WHERE filename = 'dup-b.txt'"
            ))
        init_db(create_admin=False)
        self.assertIn("uq_documents_kb_content_sha256", self.indexes())

    def test_upload_session_deduplicated_defaults_false(self):
        with SessionLocal() as db:
            session = UploadSession(kb_id=self.kb_id, fingerprint="f" * 64, filename="x.txt",
                                    file_ext=".txt", file_size=1, chunk_size=1, total_chunks=1,
                                    received=[], created_by=self.user_id)
            db.add(session)
            db.commit()
            self.assertFalse(db.get(UploadSession, session.id).deduplicated)
