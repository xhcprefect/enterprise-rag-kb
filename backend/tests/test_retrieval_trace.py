"""rag.retrieve 的可选 trace：阶段计数/耗时、失败降级与零影响。

全部使用现有检索测试的替身方式；不连接数据库、不发起真实模型/网络调用。
"""
import unittest
import uuid
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document as LCDocument

from app.core.config import settings
from app.services import rag, retrieval_candidates as candidates


class RetrievalTraceTests(unittest.TestCase):
    def setUp(self):
        self.identifier = uuid.uuid4()
        self.base_metadata = {
            "document_id": str(self.identifier), "filename": "law.txt",
            "index_version": 2, "chunk_index": 1,
        }

    def pair(self, text, index=1, distance=0.1, **meta):
        return LCDocument(page_content=text, metadata={
            **self.base_metadata, "chunk_index": index, **meta}), distance

    def test_trace_records_six_stages_counts_and_non_negative_ms(self):
        vector_pair = self.pair("向量候选", 1)
        keyword_pair = self.pair("关键词候选", 2)
        neighbor_pair = self.pair("邻居合并", 1)
        trace = {}
        with patch.object(rag, "get_vector_store", return_value=MagicMock()), \
             patch.object(rag, "search_ready_vectors", return_value=[vector_pair]), \
             patch.object(candidates, "keyword_candidates", return_value=[keyword_pair]), \
             patch.object(candidates, "expand_neighbors", return_value=[neighbor_pair]), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), \
             patch.object(settings, "RERANK_ENABLED", False):
            result = rag.retrieve("kb", "question", 6, trace=trace)

        self.assertEqual(result, [neighbor_pair])
        self.assertEqual(
            set(trace),
            {"vector", "keyword", "fusion", "rerank", "select", "neighbors", "total_ms"},
        )
        for name in ("vector", "keyword", "fusion", "rerank", "select", "neighbors"):
            self.assertIsInstance(trace[name]["ms"], int)
            self.assertGreaterEqual(trace[name]["ms"], 0)
            self.assertIsInstance(trace[name]["candidates"], int)
            self.assertGreaterEqual(trace[name]["candidates"], 0)
        self.assertEqual(trace["vector"]["status"], "ok")
        self.assertIsNone(trace["vector"]["error_type"])
        self.assertEqual(trace["vector"]["candidates"], 1)
        self.assertEqual(trace["keyword"]["status"], "ok")
        self.assertEqual(trace["keyword"]["candidates"], 1)
        self.assertEqual(trace["fusion"]["status"], "ok")
        self.assertEqual(trace["fusion"]["candidates"], 2)  # 两路身份不同，融合后两条
        self.assertEqual(trace["rerank"]["status"], "skipped")
        self.assertFalse(trace["rerank"]["applied"])
        self.assertEqual(trace["rerank"]["reason"], "disabled")
        self.assertEqual(trace["select"]["candidates"], 2)
        self.assertEqual(trace["neighbors"]["candidates"], 1)
        self.assertEqual(trace["neighbors"]["dropped"], 1)
        self.assertIsInstance(trace["total_ms"], int)
        self.assertGreaterEqual(trace["total_ms"], 0)

    def test_trace_none_and_trace_dict_return_identical_results(self):
        def execute(trace):
            with patch.object(rag, "get_vector_store", return_value=MagicMock()), \
                 patch.object(rag, "search_ready_vectors", return_value=[self.pair("向量", 1)]), \
                 patch.object(candidates, "keyword_candidates", return_value=[self.pair("关键词", 2)]), \
                 patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: pairs), \
                 patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), \
                 patch.object(settings, "RERANK_ENABLED", False):
                return rag.retrieve("kb", "question", 6, trace=trace)

        without_trace = execute(None)
        with_trace = execute({})
        self.assertEqual(
            [(doc.page_content, doc.metadata, distance) for doc, distance in without_trace],
            [(doc.page_content, doc.metadata, distance) for doc, distance in with_trace],
        )

    def test_vector_failure_falls_back_to_keyword_and_trace_has_only_class_name(self):
        store = MagicMock()
        store.embeddings.embed_query.side_effect = RuntimeError("embedding-secret")
        trace = {}
        keyword_pair = self.pair("关键词候选", 2)
        with patch.object(rag, "get_vector_store", return_value=store), \
             patch.object(candidates, "keyword_candidates", return_value=[keyword_pair]), \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: pairs), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), \
             patch.object(settings, "RERANK_ENABLED", False), \
             patch.object(rag.logger, "warning"):
            result = rag.retrieve("kb", "question", 6, trace=trace)

        self.assertEqual(len(result), 1)
        self.assertEqual(trace["vector"]["status"], "error")
        self.assertEqual(trace["vector"]["error_type"], "RuntimeError")
        self.assertEqual(trace["keyword"]["status"], "ok")
        self.assertEqual(trace["fusion"]["status"], "ok")
        self.assertNotIn("embedding-secret", str(trace))

    def test_both_channels_failing_reraises_vector_error_without_trace_messages(self):
        store = MagicMock()
        store.embeddings.embed_query.side_effect = RuntimeError("vector-secret")
        trace = {}
        with patch.object(rag, "get_vector_store", return_value=store), \
             patch.object(candidates, "keyword_candidates", side_effect=ValueError("keyword-secret")), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", True), \
             patch.object(settings, "RERANK_ENABLED", False), \
             patch.object(rag.logger, "warning"):
            with self.assertRaises(RuntimeError) as caught:
                rag.retrieve("kb", "question", 6, trace=trace)

        self.assertEqual(str(caught.exception), "vector-secret")
        self.assertEqual(trace["vector"]["error_type"], "RuntimeError")
        self.assertEqual(trace["keyword"]["error_type"], "ValueError")
        self.assertNotIn("secret", str(trace))
        self.assertIn("total_ms", trace)

    def test_hybrid_disabled_skips_keyword_and_fusion(self):
        trace = {}
        with patch.object(rag, "get_vector_store", return_value=MagicMock()), \
             patch.object(rag, "search_ready_vectors", return_value=[self.pair("向量", 1)]), \
             patch.object(candidates, "keyword_candidates") as keyword, \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: pairs), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False), \
             patch.object(settings, "RERANK_ENABLED", False):
            result = rag.retrieve("kb", "question", 6, trace=trace)

        keyword.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertEqual(trace["keyword"], {"status": "skipped", "ms": 0, "candidates": 0, "error_type": None})
        self.assertEqual(trace["fusion"], {"status": "skipped", "ms": 0, "candidates": 1})

    def test_vector_failure_with_hybrid_disabled_rethrows_and_marks_vector_error(self):
        store = MagicMock()
        store.embeddings.embed_query.side_effect = RuntimeError("hidden")
        trace = {}
        with patch.object(rag, "get_vector_store", return_value=store), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False), \
             patch.object(settings, "RERANK_ENABLED", False):
            with self.assertRaises(RuntimeError):
                rag.retrieve("kb", "question", 6, trace=trace)
        self.assertEqual(trace["vector"]["status"], "error")
        self.assertEqual(trace["vector"]["error_type"], "RuntimeError")
        self.assertNotIn("keyword", trace)
        self.assertIn("total_ms", trace)

    def test_rerank_skipped_when_not_configured(self):
        trace = {}
        with patch.object(rag, "get_vector_store", return_value=MagicMock()), \
             patch.object(rag, "search_ready_vectors", return_value=[self.pair("向量", 1)]), \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: pairs), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False), \
             patch.object(settings, "RERANK_ENABLED", True), \
             patch.object(settings, "RERANK_BASE_URL", ""), \
             patch.object(settings, "RERANK_MODEL", ""):
            rag.retrieve("kb", "question", 6, trace=trace)
        self.assertEqual(trace["rerank"]["status"], "skipped")
        self.assertFalse(trace["rerank"]["applied"])
        self.assertEqual(trace["rerank"]["reason"], "not_configured")

    def test_rerank_fallback_when_enabled_but_no_scores_are_produced(self):
        pair = self.pair("向量", 1)
        trace = {}
        with patch.object(rag, "get_vector_store", return_value=MagicMock()), \
             patch.object(rag, "search_ready_vectors", return_value=[pair]), \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: pairs), \
             patch("app.services.hybrid_search.rerank_results", return_value=[pair]), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False), \
             patch.object(settings, "RERANK_ENABLED", True), \
             patch.object(settings, "RERANK_BASE_URL", "http://rerank.invalid"), \
             patch.object(settings, "RERANK_MODEL", "rerank-model"):
            rag.retrieve("kb", "question", 6, trace=trace)
        self.assertEqual(trace["rerank"]["status"], "fallback")
        self.assertFalse(trace["rerank"]["applied"])
        self.assertIsNone(trace["rerank"]["reason"])

    def test_rerank_ok_when_scores_are_applied(self):
        pair = self.pair("向量", 1)
        reranked = LCDocument(page_content="向量", metadata={
            **pair[0].metadata, "rerank_score": 0.9})
        trace = {}
        with patch.object(rag, "get_vector_store", return_value=MagicMock()), \
             patch.object(rag, "search_ready_vectors", return_value=[pair]), \
             patch.object(candidates, "expand_neighbors", side_effect=lambda _c, pairs, _r: pairs), \
             patch("app.services.hybrid_search.rerank_results", return_value=[(reranked, pair[1])]), \
             patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False), \
             patch.object(settings, "RERANK_ENABLED", True), \
             patch.object(settings, "RERANK_BASE_URL", "http://rerank.invalid"), \
             patch.object(settings, "RERANK_MODEL", "rerank-model"):
            rag.retrieve("kb", "question", 6, trace=trace)
        self.assertEqual(trace["rerank"]["status"], "ok")
        self.assertTrue(trace["rerank"]["applied"])
        self.assertIsNone(trace["rerank"]["reason"])
        self.assertEqual(trace["rerank"]["candidates"], 1)

    def test_empty_scope_returns_early_and_still_closes_trace(self):
        trace = {}
        with patch.object(rag, "get_vector_store") as store:
            self.assertEqual(rag.retrieve("kb", "question", 6, document_scope=[], trace=trace), [])
        store.assert_not_called()
        self.assertIn("total_ms", trace)


if __name__ == "__main__":
    unittest.main()
