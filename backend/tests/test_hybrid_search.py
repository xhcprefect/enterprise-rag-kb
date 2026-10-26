"""中文关键词、排名融合和单次有截止时间的重排；HTTP 用内存 transport。"""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import httpx
from langchain_core.documents import Document

from app.services import hybrid_search as hybrid


class HybridSearchTests(unittest.TestCase):
    def setUp(self):
        self.pairs = [(Document(page_content=f"content {index}", metadata={"document_id": str(index), "chunk_index": 0,
                                                                        "index_version": 2}), 0.1 * index)
                      for index in range(1, 4)]
        self.client_class = httpx.AsyncClient

    def transport(self, handler):
        return lambda **kwargs: self.client_class(transport=httpx.MockTransport(handler), **kwargs)

    def rerank(self, handler, **kwargs):
        with patch.object(httpx, "AsyncClient", side_effect=self.transport(handler)):
            return hybrid.rerank_results("question", self.pairs, base_url="https://example.test/v1/", api_key="test-key",
                                         model="reranker", **kwargs)

    def test_chinese_articles_normalize_and_exact_codes_survive_fulltext_tokenization(self):
        self.assertIn("article143", hybrid.keyword_terms("第一百四十三条"))
        self.assertIn("article143", hybrid.keyword_terms("第143条"))
        code = "x" + "err_auth_401".encode().hex()
        self.assertIn(code, hybrid.keyword_terms("ERR_AUTH_401 是什么"))
        self.assertIn(code, hybrid.build_search_text("发生 ERR_AUTH_401 错误"))
        self.assertTrue(set(hybrid.keyword_terms("未成年人盗窃罪")).intersection(hybrid.keyword_terms("盗窃罪与未成年人")))
        self.assertLessEqual(len(hybrid.keyword_terms("中文" * 100, max_terms=5)), 5)

    def test_search_text_includes_filename_and_section_without_mutating_metadata(self):
        metadata = {"filename": "DEVICE-X9.txt", "section": "第一章 审批"}
        before = copy.deepcopy(metadata)
        text = hybrid.build_search_text("正文", metadata)
        self.assertIn("审批", text)
        self.assertIn("device", text)
        self.assertEqual(metadata, before)

    def test_fusion_deduplicates_identity_and_keeps_vector_similarity_separate_from_rank(self):
        keyword = [self.pairs[1], (Document(page_content="only keyword", metadata={"document_id": "extra", "chunk_index": 0}), 0)]
        before = copy.deepcopy(self.pairs)
        result = hybrid.fuse_results(self.pairs, keyword)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0][0].metadata["document_id"], "2")
        self.assertEqual(result[0][0].metadata["retrieval_method"], "hybrid")
        self.assertEqual(result[0][1], 0.2)
        self.assertNotIn("citation_score", result[0][0].metadata)
        only = next(doc for doc, _ in result if doc.metadata["document_id"] == "extra")
        self.assertIsNone(only.metadata["citation_score"])
        self.assertEqual(self.pairs, before)

    def test_generation_identity_and_duplicate_leg_entries_are_not_collapsed_incorrectly(self):
        old = self.pairs[0]
        new = (Document(page_content="new", metadata={**old[0].metadata, "index_version": 3}), 0.3)
        self.assertEqual(len(hybrid.fuse_results([old, old, new], [])), 2)

    def test_rerank_payload_is_bounded_and_unreturned_candidates_are_retained(self):
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(200, json={"results": [{"index": 2, "relevance_score": 0.99}]})
        before = copy.deepcopy(self.pairs)
        result = self.rerank(handle, max_chars=4)
        self.assertEqual([doc.metadata["document_id"] for doc, _ in result], ["3", "1", "2"])
        self.assertEqual(self.pairs, before)
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["documents"], ["cont"] * 3)
        self.assertEqual(str(requests[0].url), "https://example.test/v1/rerank")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer test-key")

    def test_invalid_rerank_results_fall_back_as_a_whole_without_logging_response(self):
        for results in [[], [{"index": 99, "relevance_score": 1}], [{"index": True, "relevance_score": 1}],
                        [{"index": 1, "relevance_score": float("nan")}], [{"index": 1, "relevance_score": 1}] * 2]:
            with self.subTest(results=results), patch.object(hybrid.logger, "warning") as log:
                def handle(request):
                    return httpx.Response(200, text=json.dumps({"results": results}))
                self.assertIs(self.rerank(handle), self.pairs)
                self.assertNotIn("test-key", str(log.call_args))

    def test_timeout_cancels_single_request_and_error_status_has_no_retry(self):
        calls = []
        async def stalled(request):
            calls.append(request)
            await asyncio.Event().wait()
        with patch.object(hybrid.logger, "warning"):
            self.assertIs(self.rerank(stalled, timeout=0.03), self.pairs)
        self.assertEqual(len(calls), 1)
        calls.clear()
        def unavailable(request):
            calls.append(request)
            return httpx.Response(503)
        with patch.object(hybrid.logger, "warning"):
            self.assertIs(self.rerank(unavailable), self.pairs)
        self.assertEqual(len(calls), 1)

    def test_unconfigured_reranking_never_opens_http_client(self):
        with patch.object(httpx, "AsyncClient") as client:
            self.assertIs(hybrid.rerank_results("q", self.pairs, base_url="", api_key="", model=""), self.pairs)
        client.assert_not_called()
