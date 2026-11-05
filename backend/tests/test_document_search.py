"""文档列表搜索 q：字面匹配、状态组合、分页 total 与权限（真实 PostgreSQL）。"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tests._env import TEST_URL

from fastapi import HTTPException
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.engine import make_url

if TEST_URL:
    from app.db import session as db_session

    _target = make_url(TEST_URL)
    if db_session.engine.url != _target:
        db_session.engine.dispose()
        db_session.engine = create_engine(TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
        db_session.SessionLocal.configure(bind=db_session.engine)

    from app.api.v1 import knowledge
    from app.db.init_db import init_db
    from app.models import Document, KnowledgeBase, User

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DocumentSearchTests(unittest.TestCase):
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
                "TRUNCATE users, knowledge_bases, documents, conversations, upload_sessions, audit_logs, "
                "document_chunks RESTART IDENTITY CASCADE"
            ))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(knowledge.settings, "UPLOAD_DIR", self.temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        with SessionLocal() as db:
            self.admin = User(username="search_admin", role="admin", is_active=True, password_hash="x")
            self.owner = User(username="search_owner", role="user", is_active=True, password_hash="x")
            self.stranger = User(username="search_stranger", role="user", is_active=True, password_hash="x")
            db.add_all([self.admin, self.owner, self.stranger])
            db.flush()
            self.kb = KnowledgeBase(name="search-kb", collection_name="s_" + uuid.uuid4().hex,
                                    owner_id=self.owner.id, is_public=True)
            self.private = KnowledgeBase(name="private-kb", collection_name="s_" + uuid.uuid4().hex,
                                         owner_id=self.owner.id, is_public=False)
            db.add_all([self.kb, self.private])
            db.commit()
            self.kb_id, self.private_id = self.kb.id, self.private.id
            self.admin_id, self.owner_id, self.stranger_id = self.admin.id, self.owner.id, self.stranger.id

    def add(self, filename, status="ready", kb_id=None):
        with SessionLocal() as db:
            doc = Document(kb_id=kb_id or self.kb_id, filename=filename, file_ext=Path(filename).suffix,
                           file_size=1, stored_path=str(Path(self.temp.name) / f"{uuid.uuid4().hex}.txt"),
                           status=status)
            db.add(doc)
            db.commit()
            return doc.id

    def search(self, q=None, *, page=1, page_size=20, status=None, user_id=None, kb_id=None):
        with SessionLocal() as db:
            user = db.get(User, user_id or self.admin_id)
            return knowledge.list_documents(kb_id or self.kb_id, page, page_size, status, q, db, user).data

    def names(self, result):
        return [item.filename for item in result.items]

    def test_empty_or_whitespace_query_returns_everything(self):
        self.add("alpha.txt")
        self.add("beta.txt")
        self.assertEqual(self.search(None).total, 2)
        self.assertEqual(self.search("   ").total, 2)
        self.assertEqual(self.search("").total, 2)

    def test_query_matches_literally_and_is_trimmed(self):
        self.add("Policy 2024.pdf")
        self.add("policy_backup.txt")
        self.add("other.txt")
        self.assertEqual(sorted(self.names(self.search("  policy "))), ["Policy 2024.pdf", "policy_backup.txt"])
        self.assertEqual(sorted(self.names(self.search("POLICY"))), ["Policy 2024.pdf", "policy_backup.txt"])  # 大小写不敏感
        self.assertEqual(self.names(self.search("2024")), ["Policy 2024.pdf"])
        self.assertEqual(self.search("missing").total, 0)

    def test_percent_and_underscore_are_literal(self):
        self.add("100%_done.txt")
        self.add("100Xdone.txt")
        self.add("abc.txt")
        self.assertEqual(self.names(self.search("100%_")), ["100%_done.txt"])
        self.assertEqual(self.names(self.search("%")), ["100%_done.txt"])
        self.assertEqual(self.names(self.search("_")), ["100%_done.txt"])

    def test_backslash_is_escaped_not_an_escape_character(self):
        self.add("back\\slash.txt")
        self.add("backslash.txt")
        self.assertEqual(self.names(self.search("back\\slash")), ["back\\slash.txt"])

    def test_query_combines_with_status_and_total(self):
        for index in range(3):
            self.add(f"report-{index}.txt", "ready")
        for index in range(2):
            self.add(f"report-{index}.md", "failed")
        self.add("notes.txt")
        result = self.search("report", status="failed")
        self.assertEqual(result.total, 2)
        self.assertTrue(all(item.status == "failed" for item in result.items))
        self.assertEqual(self.search("report").total, 5)
        self.assertEqual(self.search("report", status="ready", page=2, page_size=2).total, 3)
        self.assertEqual(len(self.search("report", status="ready", page=2, page_size=2).items), 1)

    def test_long_query_is_truncated_not_rejected(self):
        self.add("some file.txt")
        result = self.search("x" * 500)  # 超过 200 被截断，不是 422
        self.assertEqual(result.total, 0)
        self.add("y" * 250 + ".txt")
        # 截断到 200 后仍能命中以相同前缀开头的文件名
        self.assertEqual(self.search("y" * 300).total, 1)
        # 路由参数不设 FastAPI max_length：否则真实 HTTP 会 422 而不是截断
        import inspect
        default = inspect.signature(knowledge.list_documents).parameters["q"].default
        self.assertIsNone(getattr(default, "max_length", None))

    def test_non_string_or_blank_query_is_ignored(self):
        self.add("visible.txt")
        self.assertEqual(knowledge._normalize_search(None), None)
        self.assertEqual(knowledge._normalize_search("   "), None)
        self.assertEqual(len(knowledge._normalize_search("z" * 500)), 200)

    def test_pagination_is_stable_across_pages(self):
        ids = [self.add(f"same-{index}.txt") for index in range(6)]
        first = self.search("same", page=1, page_size=3)
        second = self.search("same", page=2, page_size=3)
        self.assertEqual(first.total, 6)
        self.assertEqual(len(first.items), 3)
        self.assertEqual(len(second.items), 3)
        collected = [str(item.id) for item in first.items] + [str(item.id) for item in second.items]
        self.assertEqual(len(set(collected)), 6)
        self.assertEqual(set(collected), {str(i) for i in ids})

    def test_no_cross_knowledge_base_leak(self):
        self.add("shared.txt")
        self.add("shared.txt", kb_id=self.private_id)
        result = self.search("shared")
        self.assertEqual(result.total, 1)
        self.assertEqual(result.items[0].kb_id, self.kb_id)

    def test_permissions_unchanged_for_search(self):
        self.add("secret.txt", kb_id=self.private_id)
        # 非管理员且非拥有者：404，不泄露私有库是否存在
        with self.assertRaises(HTTPException) as error:
            self.search("secret", user_id=self.stranger_id, kb_id=self.private_id)
        self.assertEqual(error.exception.status_code, 404)
        # 拥有者可以搜索
        self.assertEqual(self.search("secret", user_id=self.owner_id, kb_id=self.private_id).total, 1)
        # 公开库普通用户可搜索
        self.add("public.txt")
        self.assertEqual(self.search("public", user_id=self.stranger_id).total, 1)
