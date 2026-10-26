"""历史来源按身份重新读取原文；查询过滤条件用 PostgreSQL 方言离线验证。"""
import copy
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import Column, MetaData, Table, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.services import vector_store


# 只声明查询所需列，避免初始化 PGVector 或连接任何数据库。
_rows = Table("context_test_vectors", MetaData(), Column("collection_id", UUID),
              Column("document", Text), Column("cmetadata", JSONB))


class HistoricalSourceTests(unittest.TestCase):
    def setUp(self):
        self.document_id = str(uuid.uuid4())
        self.citation = {"document_id": self.document_id, "chunk_index": 2, "index_version": 3,
                         "source_index": 5, "source_message_id": "previous-message", "snippet": "旧片段不是证据"}
        self.metadata = {"document_id": self.document_id, "chunk_index": 2, "index_version": 3, "filename": "actual.txt"}
        self.store = SimpleNamespace(EmbeddingStore=SimpleNamespace(document=_rows.c.document,
                                    cmetadata=_rows.c.cmetadata, collection_id=_rows.c.collection_id),
                                    get_collection=MagicMock(return_value=SimpleNamespace(uuid=uuid.uuid4())))

    def load(self, citations, fetched):
        db = MagicMock()
        db.execute.return_value.all.return_value = fetched
        with patch.object(vector_store, "get_vector_store", return_value=self.store), \
             patch("app.db.session.SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            result = vector_store.load_ready_citations("kb_current", citations)
        return result, db

    def test_loads_database_original_and_preserves_previous_number_without_fake_similarity(self):
        before = copy.deepcopy(self.citation)
        result, _ = self.load([self.citation], [("数据库中的完整原文", self.metadata)])
        self.assertEqual(len(result), 1)
        doc, _ = result[0]
        self.assertEqual(doc.page_content, "数据库中的完整原文")
        self.assertEqual(doc.metadata["historical_source_index"], 5)
        self.assertEqual(doc.metadata["historical_message_id"], "previous-message")
        self.assertEqual(doc.metadata["index_version"], 3)
        self.assertIsNone(doc.metadata["citation_score"])
        self.assertEqual(self.citation, before)

    def test_sql_enforces_collection_knowledge_base_ready_cancel_and_both_generations(self):
        _, db = self.load([self.citation], [])
        query = db.execute.call_args.args[0]
        sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for condition in ["collection_id =", "knowledge_bases.collection_name = 'kb_current'",
                          "documents.status = 'ready'", "documents.cancel_requested IS false",
                          "CAST(documents.index_version AS VARCHAR)", "documents.index_version = 3",
                          self.document_id, "chunk_index", "index_version", "coalesce"]:
            with self.subTest(condition=condition):
                self.assertIn(condition, sql)

    def test_legacy_generation_zero_requires_an_exact_nonempty_original_prefix(self):
        legacy = {key: value for key, value in self.citation.items() if key != "index_version"}
        metadata = {key: value for key, value in self.metadata.items() if key != "index_version"}
        for snippet, expected in [("完整", 1), ("旧内容", 0), ("", 0), (None, 0), (12, 0)]:
            with self.subTest(snippet=snippet):
                result, _ = self.load([{**legacy, "snippet": snippet}], [("完整原文", metadata)])
                self.assertEqual(len(result), expected)
        result, _ = self.load([{**legacy, "snippet": "完整"}], [("完整原文", self.metadata)])
        self.assertEqual(result, [])  # 相同文档和分块在更高代次不能复活旧引用。

    def test_different_document_chunk_or_generation_never_matches(self):
        for metadata in [{**self.metadata, "document_id": str(uuid.uuid4())},
                         {**self.metadata, "chunk_index": 1}, {**self.metadata, "index_version": 4}]:
            with self.subTest(metadata=metadata):
                result, _ = self.load([self.citation], [("新原文", metadata)])
                self.assertEqual(result, [])

    def test_invalid_id_chunk_and_generation_do_not_open_a_store(self):
        invalid = [{**self.citation, "document_id": "invalid"}, {**self.citation, "chunk_index": -1},
                   {**self.citation, "chunk_index": True}, {**self.citation, "chunk_index": "2"},
                   {**self.citation, "index_version": -1}, {**self.citation, "index_version": True},
                   {**self.citation, "index_version": "3"}]
        with patch.object(vector_store, "get_vector_store") as store:
            self.assertEqual(vector_store.load_ready_citations("kb_current", invalid), [])
        store.assert_not_called()

    def test_missing_collection_and_missing_original_do_not_return_old_snippets(self):
        self.store.get_collection.return_value = None
        result, db = self.load([self.citation], [])
        self.assertEqual(result, [])
        db.execute.assert_not_called()
        self.store.get_collection.return_value = SimpleNamespace(uuid=uuid.uuid4())
        for content in [None, "", "  "]:
            result, _ = self.load([self.citation], [(content, self.metadata)])
            self.assertEqual(result, [])

    def test_historical_merged_source_requires_every_piece_in_its_original_range(self):
        citation = {**self.citation, "chunk_start": 1, "chunk_end": 3}
        fetched = [(f"piece {index}", {**self.metadata, "chunk_index": index, "page_start": index, "page_end": index})
                   for index in (1, 2, 3)]
        result, _ = self.load([citation], fetched)
        self.assertEqual(result[0][0].page_content, "piece 1\n\npiece 2\n\npiece 3")
        self.assertEqual(result[0][0].metadata["chunk_index"], 2)
        self.assertEqual(result[0][0].metadata["page_start"], 1)
        self.assertEqual(result[0][0].metadata["page_end"], 3)
        result, _ = self.load([citation], fetched[:2])
        self.assertEqual(result, [])

    def test_requested_order_is_preserved_even_when_database_returns_another_order(self):
        other = {**self.citation, "chunk_index": 4, "source_index": 8}
        result, _ = self.load([self.citation, other], [("第二段", {**self.metadata, "chunk_index": 4}),
                                                       ("第一段", self.metadata)])
        self.assertEqual([doc.page_content for doc, _ in result], ["第一段", "第二段"])
        self.assertEqual([doc.metadata["historical_source_index"] for doc, _ in result], [5, 8])
