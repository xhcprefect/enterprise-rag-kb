"""两路检索集成、过滤条件与结构相邻片段合并；不连接数据库。"""
import copy
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document as LCDocument
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.services import rag, retrieval_candidates as candidates


class RetrievalCandidateTests(unittest.TestCase):
    def setUp(self):
        self.identifier = uuid.uuid4()
        self.metadata = {"document_id": str(self.identifier), "filename": "law.txt", "index_version": 2,
                         "chunk_index": 2, "section": "第一条", "section_key": "one", "line_start": 20, "line_end": 29}
        self.pair = (LCDocument(page_content="原始片段2", metadata=self.metadata), 0.2)

    def rows(self):
        return [(SimpleNamespace(document_id=self.identifier, chunk_index=index, content=f"片段{index}", source_metadata={
            "section_key": "one" if index < 4 else "two", "section": "第一条" if index < 4 else "第二条",
            "line_start": index * 10, "line_end": index * 10 + 9,
        }), 2) for index in (1, 2, 3, 4)]

    def test_neighbor_context_stops_at_structure_boundary_and_deduplicates_selected_hits(self):
        before = copy.deepcopy(self.pair[0].metadata)
        db = MagicMock()
        db.execute.return_value.all.return_value = self.rows()
        neighbor = (LCDocument(page_content="片段3", metadata={**self.metadata, "chunk_index": 3}), 0.3)
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            result = candidates.expand_neighbors("kb_current", [self.pair, neighbor], 2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0].page_content, "片段1\n\n片段2\n\n片段3")
        self.assertEqual(result[0][0].metadata["chunk_start"], 1)
        self.assertEqual(result[0][0].metadata["chunk_end"], 3)
        self.assertEqual(result[0][0].metadata["line_start"], 10)
        self.assertEqual(result[0][0].metadata["line_end"], 39)
        self.assertEqual(self.pair[0].metadata, before)
        sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for clause in ["knowledge_bases.collection_name = 'kb_current'", "documents.status = 'ready'",
                       "documents.index_version = 2", "documents.cancel_requested IS false", "document_chunks.embedded IS true"]:
            self.assertIn(clause, sql)

    def test_neighbor_expansion_always_requires_current_governed_base_rows(self):
        """radius 0 也必须回查同库当前代次 base；无权威 base 就丢弃。"""
        for radius in (0, 1):
            db = MagicMock()
            with patch.object(candidates, "SessionLocal") as sessions:
                sessions.return_value.__enter__.return_value = db
                db.execute.return_value.all.return_value = []
                self.assertEqual(candidates.expand_neighbors("kb", [self.pair], radius), [])
                db.execute.assert_called_once()
            sql = str(db.execute.call_args.args[0].compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            for clause in ["knowledge_bases.collection_name = 'kb'", "documents.enabled IS true",
                           "documents.superseded_at IS NULL", "documents.superseded_by_id IS NULL",
                           "documents.valid_from IS NULL OR", "documents.valid_until IS NULL OR",
                           "documents.index_version >= 0", "document_chunks.embedded IS true",
                           "documents.index_version = 2"]:
                self.assertIn(clause, sql)

    def test_missing_generation_source_kind_or_identity_is_never_upgraded(self):
        """缺失/None version 不补 0；project/显式非 document 不能碰撞上传 ID。"""
        base = {"document_id": str(self.identifier), "chunk_index": 1}
        bad = [(LCDocument(page_content="x", metadata=meta), 0.1) for meta in [
            base,  # 缺 index_version
            {**base, "index_version": None},
            {**base, "source_kind": "project", "index_version": 2},
            {**base, "source_kind": "unknown", "index_version": 2},
            {**base, "source_kind": None, "index_version": 2},
            {**base, "index_version": 2, "document_id": "not-a-uuid"},
            {**base, "index_version": 2, "chunk_index": True},
            {**base, "index_version": 2, "chunk_index": 1.0},
            {**base, "index_version": True},
            {**base, "index_version": 2, "chunk_index": -1},
            {**base, "index_version": -1},
        ]]
        with patch.object(candidates, "SessionLocal") as sessions:
            self.assertEqual(candidates.expand_neighbors("kb", bad, 1), [])
        sessions.assert_not_called()

    def test_single_base_rewrites_identity_owned_position_and_anchor_metadata(self):
        """旧候选自带的多片段范围/正文不能冒充本次 base。"""
        db = MagicMock()
        db.execute.return_value.all.return_value = [
            (SimpleNamespace(document_id=self.identifier, chunk_index=2, content="当前库原文2",
                             source_metadata={"section": "第一条", "line_start": 20, "line_end": 29}), 2)]
        stale = {**self.metadata, "chunk_start": 0, "chunk_end": 9,
                 "_anchor_content": "旧合并正文", "_anchor_location": {"page_start": 9},
                 "keyword_rank": 0.9}
        candidate = (LCDocument(page_content="候选缓存正文", metadata=stale), 0.2)
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            result = candidates.expand_neighbors("kb", [candidate], 0)
        doc, _ = result[0]
        self.assertEqual(doc.page_content, "当前库原文2")
        self.assertEqual((doc.metadata["chunk_start"], doc.metadata["chunk_end"]), (2, 2))
        self.assertEqual(doc.metadata["_anchor_content"], "当前库原文2")
        self.assertEqual(doc.metadata["_anchor_location"], {"section": "第一条", "line_start": 20, "line_end": 29})
        self.assertNotIn("page_start", doc.metadata)  # 旧位置不保留
        self.assertEqual(doc.metadata["keyword_rank"], 0.9)  # 排名标量可保留
        self.assertEqual(stale["chunk_start"], 0)  # 入参未被就地修改

    def test_output_identity_and_chunk_index_come_from_the_authoritative_row(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = [
            (SimpleNamespace(document_id=self.identifier, chunk_index=2, content="片段2",
                             source_metadata={"section": "第一条"}), 2)]
        # document_id 合法但 chunk_index 标错：SQL 按它查，输出必须用实际行的值。
        candidate = (LCDocument(page_content="旧", metadata={**self.metadata, "chunk_index": 2}), 0.2)
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            doc, _ = candidates.expand_neighbors("kb", [candidate], 0)[0]
        self.assertEqual(doc.metadata["document_id"], str(self.identifier))
        self.assertEqual(doc.metadata["index_version"], 2)
        self.assertEqual(doc.metadata["chunk_index"], 2)
        self.assertEqual(doc.metadata["chunk_start"], 2)

    def test_stale_candidate_range_cannot_cover_chunks_it_did_not_read(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = [
            (SimpleNamespace(document_id=self.identifier, chunk_index=2, content="片段2",
                             source_metadata={"section_key": "one", "section": "第一条"}), 2),
            (SimpleNamespace(document_id=self.identifier, chunk_index=3, content="片段3",
                             source_metadata={"section_key": "one", "section": "第一条"}), 2)]
        stale = {**self.metadata, "chunk_start": 0, "chunk_end": 3}
        candidate = (LCDocument(page_content="旧范围", metadata=stale), 0.2)
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            result = candidates.expand_neighbors("kb", [candidate], 1)
        doc, _ = result[0]
        self.assertEqual(doc.page_content, "片段2\n\n片段3")
        self.assertEqual((doc.metadata["chunk_start"], doc.metadata["chunk_end"]), (2, 3))

    def test_keyword_query_scopes_ready_embedded_rows_and_never_trusts_metadata_identity(self):
        db = MagicMock()
        db.execute.return_value.all.return_value = [("ERR_AUTH_401", {"document_id": "wrong"}, 4,
                                                     self.identifier, "actual.txt", 2, 0.7)]
        with patch.object(candidates, "SessionLocal") as sessions:
            sessions.return_value.__enter__.return_value = db
            result = candidates.keyword_candidates("kb_current", "ERR_AUTH_401")
        self.assertEqual(result[0][0].metadata["document_id"], str(self.identifier))
        self.assertIsNone(rag.to_citations(result)[0]["score"])
        sql = str(db.execute.call_args.args[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for clause in ["knowledge_bases.collection_name = 'kb_current'", "documents.status = 'ready'",
                       "documents.cancel_requested IS false", "document_chunks.embedded IS true", "to_tsvector('simple'", "@@"]:
            self.assertIn(clause, sql)

    def test_vector_query_is_embedded_once_when_candidate_pool_expands(self):
        docs = [(LCDocument(page_content=str(index), metadata={"document_id": "same", "chunk_index": index}), index / 1000)
                for index in range(80)]
        store = MagicMock()
        with patch.object(rag, "get_vector_store", return_value=store), \
             patch.object(rag, "search_ready_vectors", side_effect=[docs[:60], docs]) as search, \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False), patch.object(settings, "RERANK_ENABLED", False), \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _collection, pairs, _radius: pairs):
            result = rag.retrieve("kb", "question", 6)
        store.embeddings.embed_query.assert_called_once_with("question")
        self.assertEqual(search.call_count, 2)
        self.assertEqual(len(result), 6)

    def test_hybrid_uses_keyword_candidates_when_embedding_service_fails(self):
        store = MagicMock()
        store.embeddings.embed_query.side_effect = RuntimeError("upstream unavailable")
        with patch.object(rag, "get_vector_store", return_value=store), \
             patch.object(candidates, "keyword_candidates", return_value=[self.pair]), \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _collection, pairs, _radius: pairs), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), patch.object(settings, "RERANK_ENABLED", False), \
             patch.object(rag.logger, "warning"):
            self.assertEqual(len(rag.retrieve("kb", "code", 6)), 1)

    def test_keyword_fallback_also_handles_vector_store_initialization_failure(self):
        with patch.object(rag, "get_vector_store", side_effect=RuntimeError("vector tables unavailable")), \
             patch.object(candidates, "keyword_candidates", return_value=[self.pair]) as keyword, \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _collection, pairs, _radius: pairs), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), patch.object(settings, "RERANK_ENABLED", False), \
             patch.object(rag.logger, "warning"):
            result = rag.retrieve("kb", "code", 6)
        keyword.assert_called_once_with("kb", "code", settings.KEYWORD_CANDIDATES)
        self.assertEqual(len(result), 1)

    def test_hybrid_failure_is_not_misreported_as_an_empty_knowledge_base(self):
        store = MagicMock()
        store.embeddings.embed_query.side_effect = RuntimeError("upstream unavailable")
        with patch.object(rag, "get_vector_store", return_value=store), \
             patch.object(candidates, "keyword_candidates", return_value=[]), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), patch.object(rag.logger, "warning"):
            with self.assertRaises(RuntimeError):
                rag.retrieve("kb", "code", 6)
