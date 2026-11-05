"""文档内容身份相关迁移：新/旧 schema 幂等、部分唯一索引、老行不删除（真实 PostgreSQL）。"""
import hashlib
import unittest
import uuid

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

    def test_double_init_is_idempotent_and_creates_new_columns(self):
        init_db(create_admin=False)
        init_db(create_admin=False)
        columns = self.columns()
        self.assertIn(("documents", "content_sha256"), columns)
        self.assertIn(("upload_sessions", "deduplicated"), columns)
        self.assertIn(("conversations", "pinned_at"), columns)
        self.assertIn("uq_documents_kb_content_sha256", self.indexes())

    def test_old_schema_migration_preserves_null_rows_and_metadata(self):
        legacy = [self.add_legacy_document(f"old-{index}.txt") for index in range(3)]
        with engine.begin() as conn:
            # 删列会连带删除依赖它的部分唯一索引，无需再显式 DROP INDEX
            conn.execute(text("ALTER TABLE documents DROP COLUMN content_sha256"))
            conn.execute(text("ALTER TABLE upload_sessions DROP COLUMN deduplicated"))
            conn.execute(text("ALTER TABLE conversations DROP COLUMN pinned_at"))
            conn.execute(text("DROP INDEX IF EXISTS uq_documents_kb_content_sha256"))
        init_db(create_admin=False)
        init_db(create_admin=False)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 3)
            for doc_id in legacy:
                self.assertIsNone(db.get(Document, doc_id).content_sha256)  # 老行保持 NULL
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
