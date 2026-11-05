"""分片上传 API/服务对 kind=project 知识库的写入旁路封闭验收。

本文件封闭「普通分片上传 API/服务不得在项目源码库（kind=project）上落盘/合并/
创建 Document」的旁路，回溯 create/chunk/get/complete/abort 全部门以及服务层
create/write/assemble 兜底。

安全隔离验收（本文件自身即第一道门）：
- 不在导入期改任何环境变量，不硬写 URL / 凭据 / owner marker；一律取自已设好的启动环境。
- TEST_DATABASE_URL 与 TEST_DATABASE_OWNER 都缺 -> 整组 SkipTest（绝不仅凭 `_test`
  后缀授权破坏性操作）。
- 任何部分配置（只给一个）、URL 不可解析、host 非 loopback、库名非 ``_test``、实际
  应用 engine.url != 调用方 TEST_DATABASE_URL、或 PG shobj_description 库 comment !=
  TEST_DATABASE_OWNER -> 写前硬 fail（RuntimeError），绝不当 skip 掩盖。
- fixture 用「单外事务 + 每请求 savepoint + tearDown 整体 rollback」合成，不 TRUNCATE/
  drop，不给共享专属库留残留。随机 tag 避免与历史遗留行撞唯一约束。

回归方式：真 router + 真服务，只覆盖 `get_db` 与 `get_current_user`（auth），不 mock
任何业务/服务/文件系统；项目库 fixture 与上传会话都在同一个真 PG 外事务里对路由可见，
负控命中 guard 在碰磁盘 / assemble 之前就 400，绝不新增 Document / 分片 / 合并文件；
正控（普通 upload KB）继续可选手动。

运行（用本仓库 venv，先核 owner 再跑；URL/owner 由启动环境提供）：
  RAG_DISABLE_DOTENV=1 LOG_STDOUT_ONLY=true \\
  TEST_DATABASE_URL=... TEST_DATABASE_OWNER=... \\
  UPLOAD_DIR=<tmp>/uploads PROJECT_STORAGE_DIR=<tmp>/projects \\
  python -m unittest tests.test_project_upload_guard -v
"""
from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from tests import _memory_project_test_gate as gate

# 导入期绝不改 env / 不硬写 URL/owner；全部取自已设好的启动环境。
TEST_URL_RAW = gate.TEST_DATABASE_URL
TEST_OWNER_RAW = gate.TARGET_OWNER

from app.core.config import settings  # noqa: E402
from app.api.deps import get_current_user  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.models import (  # noqa: E402
    KNOWLEDGE_BASE_KIND_PROJECT,
    KNOWLEDGE_BASE_KIND_UPLOAD,
    Document,
    KnowledgeBase,
    UploadSession,
    UploadStatus,
    User,
    UserRole,
)

engine = db_session.engine


def _verify_test_env() -> None:
    """破坏性测试总开关（共享门）：双缺 -> SkipTest；部分配置/坏 URL/owner 标签格式/
    engine 不符/库 comment 不符 -> 硬失败（ConfigError）。"""
    gate.verify(TEST_URL_RAW, TEST_OWNER_RAW, db_session.engine)


def _kb(name: str, owner: User, *, kind: str, public: bool = True) -> KnowledgeBase:
    return KnowledgeBase(
        name=name,
        description="",
        collection_name=f"gu_{uuid.uuid4().hex}",
        owner_id=owner.id,
        is_public=public,
        kind=kind,
    )


class ProjectUploadGuardPgTests(unittest.TestCase):
    """真实 router + 服务 + 专属 PG：分片上传入口对 project KB 全封闭。"""

    @classmethod
    def setUpClass(cls):
        _verify_test_env()
        Base.metadata.create_all(bind=engine)
        with patch("app.core.logging.setup_logging"):
            from app import main
        from fastapi.testclient import TestClient

        cls.app = main.app
        cls.client = TestClient(main.app)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        _verify_test_env()
        self._tag = uuid.uuid4().hex[:10]
        upload_tmp = tempfile.TemporaryDirectory(prefix="rag-upload-")
        self.addCleanup(upload_tmp.cleanup)
        self._upload_tmp = upload_tmp.name
        storage = patch.object(settings, "UPLOAD_DIR", self._upload_tmp)
        storage.start()
        self.addCleanup(storage.stop)

        # 单外事务：所有 fixture 与路由写入都在同一连接的事务里，最终整体 rollback。
        self._conn = engine.connect()
        self._outer = self._conn.begin()

        # 分片/会话内部路径（_chunk_metadata/_write_chunk）用全局 SessionLocal；
        # 复绑到同一连接，让它们在外事务里看到项目/普通 KB fixture。
        db_session.SessionLocal.configure(bind=self._conn)
        self.addCleanup(lambda: db_session.SessionLocal.configure(bind=engine))

        self._fixture_db = db_session.SessionLocal(bind=self._conn)
        self.admin = User(
            username=f"pg-admin-{self._tag}", role=UserRole.ADMIN.value,
            is_active=True, password_hash="unused-test-only",
        )
        self._fixture_db.add(self.admin)
        self._fixture_db.flush()

        self.project_kb = _kb(f"project-{self._tag}", self.admin,
                              kind=KNOWLEDGE_BASE_KIND_PROJECT, public=False)
        self.upload_kb = _kb(f"upload-{self._tag}", self.admin, kind=KNOWLEDGE_BASE_KIND_UPLOAD)
        self._fixture_db.add_all([self.project_kb, self.upload_kb])
        self._fixture_db.flush()

        self.app.dependency_overrides[get_db] = self._override_get_db
        self.app.dependency_overrides[get_current_user] = self._override_user
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.app.dependency_overrides.clear()
        self._conn.close()

    def _override_user(self):
        return self.admin

    def _override_get_db(self):
        db = db_session.SessionLocal(bind=self._conn)
        db.begin_nested()  # 每请求 savepoint：路由内 commit 只释放 savepoint，不提交外事务。
        try:
            yield db
        finally:
            db.close()

    # ---- 会话行与断言工具 ----
    def _add_session(self, kb, *, status=UploadStatus.UPLOADING.value, filename="x.txt",
                     file_size=4, document_id=None) -> UploadSession:
        session = UploadSession(
            kb_id=kb.id,
            fingerprint=f"gufp{uuid.uuid4().hex}",
            filename=filename,
            file_ext=".txt",
            file_size=file_size,
            chunk_size=4,
            total_chunks=1,
            received=[] if status == UploadStatus.UPLOADING.value else [0],
            status=status,
            document_id=document_id,
            created_by=self.admin.id,
        )
        self._fixture_db.add(session)
        self._fixture_db.flush()
        return session

    def _doc_count(self, kb_id) -> int:
        with db_session.SessionLocal(bind=self._conn) as db:
            return db.scalar(select(func.count()).select_from(Document).where(Document.kb_id == kb_id)) or 0

    def _session_count(self, kb_id) -> int:
        with db_session.SessionLocal(bind=self._conn) as db:
            return db.scalar(select(func.count()).select_from(UploadSession).where(UploadSession.kb_id == kb_id)) or 0

    def _kb_url(self, kb_id) -> str:
        return f"/api/v1/knowledge-bases/{kb_id}"

    # ---- 负控：create/get/chunk/complete/abort 全部在碰磁盘/assemble 前 400 ----
    def test_create_upload_session_on_project_kb_is_rejected_no_session(self):
        resp = self.client.post(
            f"{self._kb_url(self.project_kb.id)}/upload-sessions",
            json={"filename": "a.txt", "file_size": 4},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._session_count(self.project_kb.id), 0, "项目库不允许新建上传会话")
        self.assertEqual(self._doc_count(self.project_kb.id), 0)

    def test_get_upload_session_on_project_kb_is_rejected(self):
        session = self._add_session(self.project_kb)
        resp = self.client.get(f"{self._kb_url(self.project_kb.id)}/upload-sessions/{session.id}")
        self.assertEqual(resp.status_code, 400)

    def test_upload_chunk_on_project_kb_is_rejected_before_disk(self):
        session = self._add_session(self.project_kb)
        resp = self.client.post(
            f"{self._kb_url(self.project_kb.id)}/upload-sessions/{session.id}/chunks/0",
            content=b"abcd",
        )
        self.assertEqual(resp.status_code, 400)
        part = Path(os.environ.get("UPLOAD_DIR", "storage/uploads")) / "tmp" / str(session.id) / "0.part"
        self.assertFalse(part.exists(), "项目库分片绝不允许落 .part 文件")
        self.assertEqual(self._doc_count(self.project_kb.id), 0)

    def test_complete_upload_on_project_kb_rejects_replay_of_completed_session(self):
        # 已有 completed 会话（重放路径）：不得借它产出项目 Document。
        session = self._add_session(self.project_kb, status=UploadStatus.COMPLETED.value,
                                    filename="y.txt", file_size=2, document_id=uuid.uuid4())
        resp = self.client.post(f"{self._kb_url(self.project_kb.id)}/upload-sessions/{session.id}/complete")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._doc_count(self.project_kb.id), 0, "重放完成上传不得产出项目 Document")

    def test_abort_upload_on_project_kb_is_rejected(self):
        session = self._add_session(self.project_kb)
        resp = self.client.delete(f"{self._kb_url(self.project_kb.id)}/upload-sessions/{session.id}")
        self.assertEqual(resp.status_code, 400)

    # ---- 正控：普通 upload KB 仍可创建分片会话（不触碰项目库）----
    def test_normal_upload_kb_can_still_create_session(self):
        resp = self.client.post(
            f"{self._kb_url(self.upload_kb.id)}/upload-sessions",
            json={"filename": "b.txt", "file_size": 4},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._session_count(self.upload_kb.id), 1, "普通 upload 库会话照常创建")
        self.assertEqual(self._session_count(self.project_kb.id), 0)
        self.assertEqual(self._doc_count(self.project_kb.id), 0)
