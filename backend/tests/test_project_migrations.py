"""项目索引：新/旧 schema init 两遍幂等、FK 删除顺序、活动同步唯一、文件版本身份（真实 PG）。

仅在本轮 owned 的专用 *_test 库（foundation_test）运行；设置 TEST_DATABASE_URL
指向该库后这些用例真实执行，不视为跳过。
"""
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
    from app.models import (
        KnowledgeBase,
        ProjectChunk,
        ProjectFileVersion,
        ProjectSnapshot,
        ProjectSnapshotFile,
        ProjectSnapshotStatus,
        ProjectSource,
        User,
    )

    SessionLocal, engine = db_session.SessionLocal, db_session.engine

_ALL_TABLES = (
    "users", "knowledge_bases", "documents", "document_chunks", "upload_sessions",
    "conversations", "chat_turns", "messages", "audit_logs",
    "project_sources", "project_snapshots", "project_file_versions",
    "project_snapshot_files", "project_chunks",
)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class ProjectMigrationTests(unittest.TestCase):
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
            conn.execute(text("TRUNCATE " + ", ".join(_ALL_TABLES) + " RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            user = User(username="proj_admin", role="admin", is_active=True, password_hash="x")
            db.add(user)
            db.flush()
            self.user_id = user.id
            self.kb = KnowledgeBase(name="proj-kb", collection_name="p_" + uuid.uuid4().hex,
                                    owner_id=user.id, is_public=True, kind="project")
            db.add(self.kb)
            db.commit()
            self.kb_id = self.kb.id

    def tables(self):
        with engine.begin() as conn:
            return {
                row[0] for row in conn.execute(text(
                    "SELECT table_name FROM information_schema.tables "
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

    def make_source(self, **kw):
        with SessionLocal() as db:
            src = ProjectSource(
                kb_id=self.kb_id, name="src", root_key="lib",
                relative_dir=kw.get("relative_dir", ""), root_fingerprint="fp-x",
                created_by=self.user_id,
            )
            db.add(src)
            db.commit()
            return src.id

    def make_snapshot(self, source_id, status="pending"):
        with SessionLocal() as db:
            snap = ProjectSnapshot(source_id=source_id, status=status)
            db.add(snap)
            db.commit()
            return snap.id

    # ---- schema 幂等与 FK 顺序 ----
    def test_double_init_creates_project_tables_and_kind_column(self):
        init_db(create_admin=False)
        init_db(create_admin=False)
        tables = self.tables()
        self.assertIn("project_sources", tables)
        self.assertIn("project_snapshots", tables)
        self.assertIn("project_file_versions", tables)
        self.assertIn("project_snapshot_files", tables)
        self.assertIn("project_chunks", tables)
        with engine.connect() as conn:
            kind_type = conn.execute(text(
                "SELECT data_type FROM information_schema.columns WHERE table_name='knowledge_bases' "
                "AND column_name='kind'"
            )).scalar()
            self.assertIsNotNone(kind_type)

    def test_old_schema_simulation_rebuilds_project_tables_and_kind(self):
        # 模拟旧库：删掉整张 project 表 + kind 列（这些是本次新增，老库不存在）。
        # init 两遍应把表与列全部重建，且未被删的表（knowledge_bases）上的既有行保留。
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS project_chunks CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS project_snapshot_files CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS project_file_versions CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS project_snapshots CASCADE"))
            conn.execute(text("DROP TABLE IF EXISTS project_sources CASCADE"))
            conn.execute(text("ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS kind"))
        init_db(create_admin=False)
        init_db(create_admin=False)
        tables = self.tables()
        for name in ("project_sources", "project_snapshots", "project_file_versions",
                     "project_snapshot_files", "project_chunks"):
            self.assertIn(name, tables)
        with engine.connect() as conn:
            kind_type = conn.execute(text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='knowledge_bases' AND column_name='kind'"
            )).scalar()
            self.assertIsNotNone(kind_type)
        # knowledge_bases 未删：既有 KB 行应保留（表未被 drop，行数据不被波及）。
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(KnowledgeBase, self.kb_id))

    # ---- 知识库 kind 默认值与类型 ----
    def test_knowledge_base_kind_defaults_upload_and_holds_project(self):
        with SessionLocal() as db:
            kb = KnowledgeBase(name="u-" + uuid.uuid4().hex, collection_name="q_" + uuid.uuid4().hex,
                               owner_id=self.user_id, is_public=True)
            db.add(kb)
            db.commit()
            self.assertEqual(db.get(KnowledgeBase, kb.id).kind, "upload")
            proj = db.get(KnowledgeBase, self.kb_id)
            self.assertEqual(proj.kind, "project")

    # ---- 每源至多一个活动同步 ----
    def test_only_one_active_sync_per_source(self):
        src_id = self.make_source()
        self.make_snapshot(src_id, status="pending")
        # 第二个活动（processing）应被部分唯一索引拒绝。
        with self.assertRaises(Exception):
            self.make_snapshot(src_id, status="processing")
        # 终态不冲突：可以重复建 ready/failed。
        self.make_snapshot(src_id, status="ready")
        self.make_snapshot(src_id, status="failed")

    def test_snapshot_status_enum_values(self):
        self.assertEqual(ProjectSnapshotStatus.PENDING.value, "pending")
        self.assertEqual(ProjectSnapshotStatus.PROCESSING.value, "processing")
        self.assertEqual(ProjectSnapshotStatus.READY.value, "ready")
        self.assertEqual(ProjectSnapshotStatus.FAILED.value, "failed")
        self.assertEqual(ProjectSnapshotStatus.CANCELLED.value, "cancelled")

    # ---- 文件版本身份：同内容同路径同配置唯一；改内容可以多版本 ----
    def test_file_version_identity_rules(self):
        src_id = self.make_source()
        with SessionLocal() as db:
            v1 = ProjectFileVersion(source_id=src_id, relative_path="a.js",
                                    content_sha256="c1" * 32, config_fingerprint="cfg-1",
                                    blob_path="/blobs/b1", size=10, language="javascript")
            db.add(v1)
            db.commit()
            self.v1 = v1.id
        # 完全相同 -> 拒绝
        with SessionLocal() as db, self.assertRaises(Exception):
            db.add(ProjectFileVersion(source_id=src_id, relative_path="a.js",
                                      content_sha256="c1" * 32, config_fingerprint="cfg-1",
                                      blob_path="/blobs/b2", size=10, language="javascript"))
            db.commit()
        # 同路径不同内容/配置 -> 允许（同字节不同路径也保留两个身份）
        with SessionLocal() as db:
            v2 = ProjectFileVersion(source_id=src_id, relative_path="a.js",
                                    content_sha256="c2" * 32, config_fingerprint="cfg-1",
                                    blob_path="/blobs/b3", size=20, language="javascript")
            db.add(v2)
            db.commit()
            self.v2 = v2.id
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ProjectFileVersion)
                                       .where(ProjectFileVersion.source_id == src_id)), 2)

    # ---- FK 删除顺序：删源级联到快照/文件版本/分块/映射 ----
    def test_delete_source_cascades_through_snapshot_and_versions(self):
        src_id = self.make_source()
        snap_id = self.make_snapshot(src_id, status="ready")
        with SessionLocal() as db:
            version = ProjectFileVersion(source_id=src_id, relative_path="a.py",
                                         content_sha256="d1" * 32, config_fingerprint="cfg",
                                         blob_path="/blobs/b", size=5, language="python")
            db.add(version)
            db.flush()
            db.add(ProjectSnapshotFile(snapshot_id=snap_id, relative_path="a.py",
                                       file_version_id=version.id))
            db.add(ProjectChunk(file_version_id=version.id, chunk_index=0, content="x"))
            db.commit()
            self.version_id = version.id
        with SessionLocal() as db:
            source = db.get(ProjectSource, src_id)
            db.delete(source)
            db.commit()
        with SessionLocal() as db:
            self.assertIsNone(db.get(ProjectSource, src_id))
            self.assertIsNone(db.get(ProjectSnapshot, snap_id))
            self.assertIsNone(db.get(ProjectFileVersion, self.version_id))
            self.assertEqual(db.scalar(select(func.count()).select_from(ProjectChunk)), 0)
            self.assertEqual(db.scalar(select(func.count()).select_from(ProjectSnapshotFile)), 0)

    # ---- active_snapshot 可空（SET NULL）且不构成 create_all 循环 ----
    def test_source_active_snapshot_roundtrip(self):
        src_id = self.make_source()
        snap_id = self.make_snapshot(src_id, status="ready")
        with SessionLocal() as db:
            src = db.get(ProjectSource, src_id)
            src.active_snapshot_id = snap_id
            db.commit()
            self.assertEqual(db.get(ProjectSource, src_id).active_snapshot_id, snap_id)


if __name__ == "__main__":
    unittest.main()
