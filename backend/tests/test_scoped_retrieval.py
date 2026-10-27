"""检索范围必须同时约束两路 SQL、降级、邻居和补充证据。"""
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document as LCDocument
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, MetaData, Table, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.core.config import settings
from app.services import document_scopes, rag, retrieval_candidates as candidates, vector_store

_rows = Table("scope_test_vectors", MetaData(), Column("collection_id", UUID),
              Column("document", Text), Column("cmetadata", JSONB), Column("embedding", Vector(1024)))


class ScopedRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.identifier = uuid.uuid4()
        self.scope = [{"document_id": str(self.identifier), "filename": "手册.pdf", "index_version": 3}]
        self.anchor = self.pair("分为四级", 1)
        self.fresh = self.pair("内部级文件外发需要审批并脱敏", 20)
        self.other = self.pair("其他指南可直接公开", 1, document_id=str(uuid.uuid4()))

    def pair(self, text, index, **meta):
        return LCDocument(page_content=text, metadata={**self.scope[0], "chunk_index": index, **meta}), 0.1

    def test_vector_filter_is_before_order_and_limit_and_binds_document_version(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        store = SimpleNamespace(collection_name="kb_scope", EmbeddingStore=SimpleNamespace(**{
            key: _rows.c[key] for key in ("document", "cmetadata", "collection_id", "embedding")}),
            get_collection=lambda _db: SimpleNamespace(uuid=uuid.uuid4()))
        with patch("app.db.session.SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            vector_store.search_ready_vectors(store, [1.0] + [0.0] * 1023, 1, document_scope=self.scope)
        query = db.execute.call_args.args[0].compile(dialect=postgresql.dialect())
        sql = str(query)
        self.assertIn(self.identifier, query.params.values())
        self.assertIn(3, query.params.values())
        self.assertLess(sql.index("documents.index_version ="), sql.index("ORDER BY"))
        self.assertIn("knowledge_bases.collection_name =", sql)
        self.assertIn("documents.cancel_requested IS false", sql)

    def test_keyword_filter_is_before_order_and_limit(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = []
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            candidates.keyword_candidates("kb_scope", "内部级外发审批", 1, document_scope=self.scope)
        sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        self.assertIn(str(self.identifier), sql)
        self.assertLess(sql.index("documents.index_version = 3"), sql.index("ORDER BY"))

    def test_both_channels_receive_scope_and_outside_candidates_cannot_reach_the_model(self):
        store = MagicMock()
        with patch.object(rag, "get_vector_store", return_value=store), \
             patch.object(rag, "search_ready_vectors", return_value=[self.other, self.fresh]) as vector, \
             patch.object(candidates, "keyword_candidates", return_value=[self.other, self.fresh]) as keyword, \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: [self.other, *pairs]), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), patch.object(settings, "RERANK_ENABLED", False):
            result = rag.retrieve("kb_scope", "内部级外发", 2, document_scope=self.scope)
        self.assertEqual(vector.call_args.kwargs["document_scope"], self.scope)
        self.assertEqual(keyword.call_args.kwargs["document_scope"], self.scope)
        self.assertEqual(len(result), 1)
        self.assertTrue(document_scopes.contains(self.scope, result[0][0].metadata))

    def test_vector_failure_keeps_keyword_fallback_scoped(self):
        with patch.object(rag, "get_vector_store", side_effect=RuntimeError("embedding unavailable")), \
             patch.object(candidates, "keyword_candidates", return_value=[self.other, self.fresh]) as keyword, \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: pairs), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), patch.object(settings, "RERANK_ENABLED", False), \
             patch.object(rag.logger, "warning"):
            result = rag.retrieve("kb_scope", "内部级外发", 6, document_scope=self.scope)
        self.assertEqual(keyword.call_args.kwargs["document_scope"], self.scope)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0].page_content, self.fresh[0].page_content)

    def test_explicit_empty_or_corrupt_scope_never_starts_unrestricted_retrieval(self):
        with patch.object(rag, "get_vector_store") as store, patch.object(candidates, "keyword_candidates") as keyword:
            self.assertEqual(rag.retrieve("kb", "q", 6, document_scope=[]), [])
            for scope in [{}, [{**self.scope[0], "index_version": True}]]:
                with self.subTest(scope=scope), self.assertRaises(ValueError):
                    rag.retrieve("kb", "q", 6, document_scope=scope)
        store.assert_not_called()
        keyword.assert_not_called()

    def test_supplement_preserves_verified_anchor_and_adds_distant_body_not_other_file(self):
        with patch.object(rag, "retrieve", return_value=[self.anchor, self.other, self.fresh]) as retrieve:
            result = rag.supplement_sources("kb", "内部级能否外发", [self.anchor], 6, self.scope)
        retrieve.assert_called_once_with("kb", "内部级能否外发", 6, document_scope=self.scope)
        self.assertEqual([doc.page_content for doc, _ in result], [self.anchor[0].page_content, self.fresh[0].page_content])

    def test_supplement_deduplicates_previously_covered_chunk_ranges(self):
        merged = self.pair("片段1到3", 2, chunk_start=1, chunk_end=3)
        contained = self.pair("片段3", 3)
        stale = self.pair("旧代次原文", 22, index_version=2)
        with patch.object(rag, "retrieve", return_value=[contained, stale, self.fresh]):
            result = rag.supplement_sources("kb", "q", [merged], 6, self.scope)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1][0].page_content, self.fresh[0].page_content)
