"""统一治理谓词接入所有检索/原文读取路径；无真实数据库，纯 SQL 方言编译。"""
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from langchain_core.documents import Document as LCDocument
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, MetaData, Table, Text, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.services import (
    document_lifecycle,
    document_scopes,
    retrieval_candidates as candidates,
    source_documents as sources,
    vector_store,
)

_rows = Table("governance_test_vectors", MetaData(), Column("collection_id", UUID),
              Column("document", Text), Column("cmetadata", JSONB), Column("embedding", Vector(1024)))

GOVERNANCE_CLAUSES = (
    "documents.status = 'ready'",
    "documents.cancel_requested IS false",
    "documents.index_version >= 0",
    "documents.superseded_at IS NULL",
    "documents.superseded_by_id IS NULL",
    "documents.enabled IS true",
)


def compiled_sql(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


class GovernancePredicateWiringTests(unittest.TestCase):
    """每条检索/读取路径都必须带同一套治理谓词，MagicMock 结果不能绕过。"""

    def setUp(self):
        self.document_id = uuid.uuid4()
        self.kb_id = uuid.uuid4()

    def assert_governed(self, sql):
        for clause in GOVERNANCE_CLAUSES:
            with self.subTest(clause=clause):
                self.assertIn(clause, sql)
        # 生效窗口是半开区间，两侧都必须允许 NULL
        self.assertIn("documents.valid_from IS NULL", sql)
        self.assertIn("documents.valid_until IS NULL", sql)

    def test_vector_ranking_applies_governance_before_order_and_limit(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        store = SimpleNamespace(collection_name="kb_gov", EmbeddingStore=SimpleNamespace(**{
            key: _rows.c[key] for key in ("document", "cmetadata", "collection_id", "embedding")}),
            get_collection=lambda _db: SimpleNamespace(uuid=uuid.uuid4()))
        with patch("app.db.session.SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            vector_store.search_ready_vectors(store, [1.0] + [0.0] * 1023, 5)
        sql = compiled_sql(db.execute.call_args.args[0])
        self.assert_governed(sql)
        self.assertLess(sql.index("documents.enabled IS true"), sql.index("ORDER BY"))

    def test_legacy_vector_metadata_gets_the_verified_current_generation(self):
        """旧向量 cmetadata 缺 index_version 时用已核对的文档行补齐，绝不猜 0。"""
        db = MagicMock()
        db.execute.return_value.all.return_value = [
            ("原文", {"document_id": str(self.document_id), "chunk_index": 0}, 0.1, 4),
        ]
        store = SimpleNamespace(collection_name="kb_gov", EmbeddingStore=SimpleNamespace(**{
            key: _rows.c[key] for key in ("document", "cmetadata", "collection_id", "embedding")}),
            get_collection=lambda _db: SimpleNamespace(uuid=uuid.uuid4()))
        with patch("app.db.session.SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            result = vector_store.search_ready_vectors(store, [1.0] + [0.0] * 1023, 1)
        self.assertEqual(result[0][0].metadata["index_version"], 4)

    def test_keyword_ranking_applies_governance_before_order_and_limit(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            candidates.keyword_candidates("kb_gov", "内网传输", 5)
        sql = compiled_sql(db.execute.call_args.args[0])
        self.assert_governed(sql)
        self.assertLess(sql.index("documents.enabled IS true"), sql.index("ORDER BY"))

    def test_neighbor_expansion_applies_governance_even_at_radius_zero(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        pair = (LCDocument(page_content="x", metadata={
            "document_id": str(self.document_id), "chunk_index": 3, "index_version": 1}), 0.1)
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            self.assertEqual(candidates.expand_neighbors("kb_gov", [pair], 0), [])
        sql = compiled_sql(db.execute.call_args.args[0])
        self.assert_governed(sql)

    def test_historical_citation_reader_applies_governance(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        store = SimpleNamespace(EmbeddingStore=SimpleNamespace(document=_rows.c.document,
                                    cmetadata=_rows.c.cmetadata, collection_id=_rows.c.collection_id),
                                get_collection=MagicMock(return_value=SimpleNamespace(uuid=uuid.uuid4())))
        citation = {"document_id": str(self.document_id), "chunk_index": 0, "index_version": 0,
                    "snippet": "prefix"}
        with patch.object(vector_store, "get_vector_store", return_value=store), \
             patch("app.db.session.SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            self.assertEqual(vector_store.load_ready_citations("kb_gov", [citation]), [])
        self.assert_governed(compiled_sql(db.execute.call_args.args[0]))

    def test_historical_citation_never_treats_unknown_or_project_kind_as_uploaded_document(self):
        for kind in ("project", "unknown", "", None):
            with self.subTest(kind=kind), patch.object(vector_store, "get_vector_store") as store:
                citation = {"document_id": str(self.document_id), "chunk_index": 0, "index_version": 0,
                            "snippet": "prefix", "source_kind": kind}
                self.assertEqual(vector_store.load_ready_citations("kb_gov", [citation]), [])
            store.assert_not_called()

    def test_document_scope_resolution_filters_governed_documents(self):
        row = SimpleNamespace(id=self.document_id, kb_id=self.kb_id, filename="a.txt",
                              status="ready", cancel_requested=False, index_version=1,
                              enabled=True, superseded_at=None, superseded_by_id=None,
                              valid_from=None, valid_until=None)
        db = MagicMock()
        db.scalars.return_value.all.return_value = [row]
        result = document_scopes.resolve_scope(db, self.kb_id, [self.document_id], lock=True)
        self.assertEqual(result[0]["document_id"], str(self.document_id))
        sql = compiled_sql(db.scalars.call_args.args[0])
        self.assert_governed(sql)
        self.assertIn("FOR UPDATE", str(db.scalars.call_args.args[0]))

    def test_scope_resolution_rechecks_lifecycle_after_locks_with_fresh_utc(self):
        """SQL 谓词的 now 在等锁前取；锁返回后必须用新 UTC 逐行复核。"""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        row = SimpleNamespace(id=self.document_id, kb_id=self.kb_id, filename="a.txt",
                              status="ready", cancel_requested=False, index_version=1,
                              enabled=True, superseded_at=None, superseded_by_id=None,
                              valid_from=None, valid_until=now - timedelta(seconds=1))
        db = MagicMock()
        db.scalars.return_value.all.return_value = [row]
        with self.assertRaises(HTTPException) as error:
            document_scopes.resolve_scope(db, self.kb_id, [self.document_id], lock=True)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("FOR UPDATE", str(db.scalars.call_args.args[0]))
        row.valid_until = now + timedelta(minutes=5)
        self.assertTrue(document_scopes.resolve_scope(db, self.kb_id, [self.document_id]))

    def test_scope_row_with_bad_governance_field_types_fails_closed(self):
        from datetime import datetime
        for changes in [dict(enabled=False), dict(enabled=None), dict(superseded_at=object()),
                        dict(valid_until=datetime(2030, 1, 1)), dict(index_version=True)]:
            with self.subTest(changes=changes):
                row = SimpleNamespace(id=self.document_id, kb_id=self.kb_id, filename="a.txt",
                                      status="ready", cancel_requested=False, index_version=1,
                                      enabled=True, superseded_at=None, superseded_by_id=None,
                                      valid_from=None, valid_until=None)
                for key, value in changes.items():
                    setattr(row, key, value)
                db = MagicMock()
                db.scalars.return_value.all.return_value = [row]
                with self.assertRaises(HTTPException):
                    document_scopes.resolve_scope(db, self.kb_id, [self.document_id])

    def test_scope_row_missing_governance_fields_fails_closed(self):
        """MagicMock 返回缺治理字段的行不能成为生产旁路。"""
        row = SimpleNamespace(id=self.document_id, kb_id=self.kb_id, filename="a.txt",
                              status="ready", cancel_requested=False, index_version=1)
        db = MagicMock()
        db.scalars.return_value.all.return_value = [row]
        with self.assertRaises(HTTPException) as error:
            document_scopes.resolve_scope(db, self.kb_id, [self.document_id])
        self.assertEqual(error.exception.status_code, 409)

    def test_scope_of_governed_row_fails_closed(self):
        db = MagicMock()
        db.scalars.return_value.all.return_value = []  # SQL 谓词已排除
        with self.assertRaises(HTTPException) as error:
            document_scopes.resolve_scope(db, self.kb_id, [self.document_id])
        self.assertEqual(error.exception.status_code, 404)

    def test_read_source_applies_governed_conditions_in_query(self):
        doc = SimpleNamespace(id=self.document_id, kb_id=self.kb_id, status="ready", cancel_requested=False,
                              index_version=1, filename="a.txt", file_ext=".txt", stored_path="unused",
                              enabled=True, superseded_at=None, superseded_by_id=None,
                              valid_from=None, valid_until=None)
        db = MagicMock()
        db.scalar.return_value = doc
        db.scalars.return_value.all.return_value = [
            SimpleNamespace(chunk_index=0, content="原文", source_metadata={})]
        sources.read_source(db, self.kb_id, self.document_id, 0, 1)
        self.assert_governed(compiled_sql(db.scalars.call_args.args[0]))


class DatabaseGovernanceRetrievalTests(unittest.TestCase):
    """真实 PG 行级验收：治理谓词必须过滤实际行，而不是只在 SQL 文本里出现。

    破坏性读写前先走共享门 ``tests._memory_project_test_gate``；不放宽原门。
    """

    @classmethod
    def setUpClass(cls):
        from sqlalchemy import make_url
        from app.core.config import settings
        from app.db.session import engine
        from tests import _memory_project_test_gate as gate

        if not gate.TEST_DATABASE_URL:
            raise unittest.SkipTest("未配置 TEST_DATABASE_URL")
        if engine.url != make_url(gate.TEST_DATABASE_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝清理")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        from app.db.init_db import init_db
        init_db(create_admin=False)
        cls.engine, cls.gate = engine, gate

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from sqlalchemy import text
        self.gate.verify(self.gate.TEST_DATABASE_URL, self.gate.TARGET_OWNER, self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("TRUNCATE documents, document_chunks, knowledge_bases RESTART IDENTITY CASCADE"))
        from app.db.session import SessionLocal
        from app.models import Document, DocumentChunk, KnowledgeBase
        self.SessionLocal, self.Document, self.DocumentChunk = SessionLocal, Document, DocumentChunk
        self.kb_id = uuid.uuid4()
        with SessionLocal() as db:
            # id 显式给定，collection_name 唯一，避免依赖自增/UUID 默认。
            kb = KnowledgeBase(id=self.kb_id, name="gov-retrieval-" + uuid.uuid4().hex[:12],
                               collection_name="gov_" + uuid.uuid4().hex, owner_id=None, is_public=True)
            db.add(kb)
            db.commit()

    def _document(self, *, chunks=("only chunk",), **values):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        defaults = dict(status="ready", cancel_requested=False, index_version=1, parse_done=True)
        defaults.update(values)
        with self.SessionLocal() as db:
            doc = self.Document(kb_id=self.kb_id, filename="gov.txt", file_ext=".txt", file_size=1,
                                stored_path="/tmp/gov.txt", **defaults)
            db.add(doc)
            db.flush()
            for index, content in enumerate(chunks):
                db.add(self.DocumentChunk(document_id=doc.id, chunk_index=index, content=content,
                                          search_text=content, embedded=True,
                                          source_metadata={"section": "s"}))
            db.commit()
            return doc.id

    def _supersede(self, identifier, replacement):
        """superseded_by_id 有真实外键：必须指向同库已存在的行。"""
        with self.SessionLocal() as db:
            doc = db.get(self.Document, identifier)
            doc.superseded_by_id = replacement
            db.commit()

    def _collection(self):
        from app.models import KnowledgeBase
        with self.SessionLocal() as db:
            return db.get(KnowledgeBase, self.kb_id).collection_name

    def _keyword_ids(self, question="hidden marker"):
        from app.services.retrieval_candidates import keyword_candidates
        return {doc.metadata["document_id"]
                for doc, _ in keyword_candidates(self._collection(), question, 10)}

    def test_keyword_retrieval_excludes_each_non_retrievable_lifecycle(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        active = self._document(chunks=("hidden marker active",))
        replacement = self._document(chunks=("hidden marker replacement",))
        legacy_linked = self._document(chunks=("hidden marker replaced",))
        self._supersede(legacy_linked, replacement)
        excluded = [
            self._document(chunks=("hidden marker disabled",), enabled=False),
            self._document(chunks=("hidden marker superseded",), superseded_at=now),
            legacy_linked,
            self._document(chunks=("hidden marker scheduled",), valid_from=now + timedelta(days=1)),
            self._document(chunks=("hidden marker expired",), valid_until=now - timedelta(seconds=1)),
            self._document(chunks=("hidden marker processing",), status="processing"),
            self._document(chunks=("hidden marker cancelled",), cancel_requested=True),
            self._document(chunks=("hidden marker negative gen",), index_version=-1),
        ]
        found = self._keyword_ids()
        self.assertIn(str(active), found)
        for identifier in excluded:
            with self.subTest(document=str(identifier)):
                self.assertNotIn(str(identifier), found)

    def test_neighbor_expansion_discards_candidate_whose_document_is_disabled(self):
        from datetime import datetime, timezone
        from langchain_core.documents import Document as LCDocument
        from app.services.retrieval_candidates import expand_neighbors
        disabled = self._document(chunks=("fragment",), enabled=False)
        pairs = [(LCDocument(page_content="cached", metadata={
            "document_id": str(disabled), "chunk_index": 0, "index_version": 1}), 0.1)]
        self.assertEqual(expand_neighbors(self._collection(), pairs, 0), [])

    def test_read_source_rejects_expired_and_superseded_documents(self):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        for values in (dict(valid_until=now - timedelta(seconds=1)), dict(superseded_at=now),
                       dict(enabled=False)):
            identifier = self._document(**values)
            with self.SessionLocal() as db, self.assertRaises(HTTPException) as error:
                sources.read_source(db, self.kb_id, identifier, 0, 1)
            self.assertEqual(error.exception.status_code, 409)

    def test_document_scope_resolution_rejects_governed_rows(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        replacement = self._document(chunks=("replacement",))
        linked = self._document(chunks=("linked",))
        self._supersede(linked, replacement)
        for identifier in (self._document(enabled=False), linked, self._document(valid_until=now)):
            with self.SessionLocal() as db, self.assertRaises(HTTPException):
                document_scopes.resolve_scope(db, self.kb_id, [identifier])

    def test_scope_resolution_rechecks_expiry_after_a_real_lock_wait(self):
        """锁等待跨过 valid_until 时，等锁前取的 SQL now 已经不是当前时刻。

        文档在等锁期间按时间自然过期（行本身未被 UPDATE，PG 不会重算已绑定的
        字面时刻），只有锁返回后用新 UTC 逐行复核才能拒绝。
        """
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime, timedelta, timezone
        from threading import Event
        from tests._pg_lock_wait import wait_for_blocking

        expiry = datetime.now(timezone.utc) + timedelta(seconds=4)
        identifier = self._document(valid_until=expiry)
        holding, reading, release = Event(), Event(), Event()
        pids = {}

        def hold_lock():
            with self.SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["holder"] = db.scalar(text("SELECT pg_backend_pid()"))
                db.execute(text("SELECT id FROM documents WHERE id = :id FOR UPDATE"), {"id": identifier})
                holding.set()
                self.assertTrue(release.wait(15))
                db.rollback()

        def read_scope():
            with self.SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["reader"] = db.scalar(text("SELECT pg_backend_pid()"))
                reading.set()
                try:
                    return document_scopes.resolve_scope(db, self.kb_id, [identifier], lock=True)
                except HTTPException as exc:
                    return exc

        pool = ThreadPoolExecutor(max_workers=2)
        reader = None
        holder = pool.submit(hold_lock)
        try:
            self.assertTrue(holding.wait(8), "未能取得行锁")
            reader = pool.submit(read_scope)
            self.assertTrue(reading.wait(8))
            self.assertTrue(wait_for_blocking(self.engine, pids["holder"], pids["reader"]))
            self.assertLess(datetime.now(timezone.utc), expiry, "必须在到期前观察到实际行锁等待")
            Event().wait(max(0, (expiry - datetime.now(timezone.utc)).total_seconds()) + 0.05)
        finally:
            release.set()
            try:
                holder.result(20)
                if reader is not None:
                    reader.result(20)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        result = reader.result()
        self.assertIsInstance(result, HTTPException)
        self.assertEqual(result.status_code, 409)

    def test_document_scope_resolution_accepts_active_row(self):
        identifier = self._document()
        with self.SessionLocal() as db:
            result = document_scopes.resolve_scope(db, self.kb_id, [identifier])
        self.assertEqual(result[0]["document_id"], str(identifier))


class LifecycleFailClosedTests(unittest.TestCase):
    """对象级判定与 SQL 谓词必须同义，坏类型一律 fail closed。"""

    def base(self, **changes):
        values = dict(status="ready", cancel_requested=False, index_version=0, enabled=True,
                      superseded_at=None, superseded_by_id=None, valid_from=None, valid_until=None)
        values.update(changes)
        return SimpleNamespace(**values)

    def test_retrievable_only_when_active_and_index_ready(self):
        self.assertTrue(document_lifecycle.is_retrievable(self.base()))
        for changes in [dict(enabled=False), dict(superseded_at=document_lifecycle.utc_now()),
                        dict(superseded_by_id=uuid.uuid4()), dict(status="processing"),
                        dict(cancel_requested=True), dict(index_version=-1), dict(index_version=True),
                        dict(index_version="0")]:
            with self.subTest(changes=changes):
                self.assertFalse(document_lifecycle.is_retrievable(self.base(**changes)))


if __name__ == "__main__":
    unittest.main()