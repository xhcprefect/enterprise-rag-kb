"""组装后的总上下文预算、完整轮次和历史引用隔离。"""
import copy
import unittest
from unittest.mock import patch

from langchain_core.documents import Document as LCDocument
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.config import settings
from app.services import rag
from app.services.context_window import ContextBudgetError, estimate_message_tokens, estimate_tokens


def history(turns=4):
    result = []
    for index in range(turns):
        result.extend([
            {"role": "user", "content": f"第{index}轮：盗窃罪如何量刑？", "id": f"q{index}"},
            {"role": "assistant", "content": f"第{index}轮历史说明[3]。" * 20, "id": f"a{index}", "citations": [{"source_index": 3}]},
        ])
    return result


class ContextPromptTests(unittest.TestCase):
    def test_complete_recent_turns_share_one_budget_with_system_question_and_documents(self):
        original_history = history(10)
        pairs = [(LCDocument(page_content=(f"资料{index}：" + "量刑说明" * 1500), metadata={
            "filename": f"law-{index}.txt", "document_id": str(index), "chunk_index": 0,
        }), 0.1) for index in range(6)]
        before = [doc.page_content for doc, _ in pairs]
        for window in [4096, 8192, 16384]:
            with self.subTest(window=window), patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", window), \
                 patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 128), patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 64):
                prepared = rag.prepare_answer("那未成年人呢？", pairs, original_history, retrieval_query="未成年人盗窃罪的量刑规定是什么？")
                self.assertEqual(prepared.input_tokens, estimate_message_tokens(prepared.messages))
                self.assertLessEqual(prepared.input_tokens + 128 + 64, window)
                self.assertIsInstance(prepared.messages[0], SystemMessage)
                self.assertIsInstance(prepared.messages[-1], HumanMessage)
                middle = prepared.messages[1:-1]
                self.assertEqual(len(middle) % 2, 0)
                self.assertTrue(all(isinstance(message, HumanMessage if index % 2 == 0 else AIMessage) for index, message in enumerate(middle)))
                self.assertIn("【用户问题】\n那未成年人呢？", prepared.messages[-1].content)
                self.assertIn("仅用于明确指代，不是事实依据", prepared.messages[-1].content)
                self.assertLess(len(prepared.pairs), len(pairs))
                self.assertTrue(prepared.pairs)
        self.assertEqual(before, [doc.page_content for doc, _ in pairs])
        self.assertEqual(original_history, history(10))

    def test_old_numbers_are_namespaced_without_changing_inline_or_fenced_code(self):
        original = [{"role": "user", "content": "旧问题"}, {"role": "assistant", "content":
            "旧依据[3]，代码 `[1]`，链接 [2](https://example.com)。\n```python\nitems = [4]\n```\n还有依据[5]。"}]
        before = copy.deepcopy(original)
        isolated = rag.isolate_history(original)
        self.assertIn("〔历史轮1来源3〕", isolated[1]["content"])
        self.assertIn("〔历史轮1来源5〕", isolated[1]["content"])
        self.assertIn("`[1]`", isolated[1]["content"])
        self.assertIn("items = [4]", isolated[1]["content"])
        self.assertIn("[2](https://example.com)", isolated[1]["content"])
        self.assertIn("不是本轮证据", isolated[1]["content"])
        self.assertEqual(original, before)

    def test_old_citations_do_not_appear_as_current_source_numbers_in_history(self):
        pairs = [(LCDocument(page_content="本轮原文", metadata={"filename": "current.txt"}), 0.2)]
        prepared = rag.prepare_answer("继续解释", pairs, history(1))
        assistant = next(message for message in prepared.messages if isinstance(message, AIMessage))
        self.assertNotIn("[3]", assistant.content)
        self.assertIn("[1] 来源：current.txt", prepared.messages[-1].content)
        self.assertNotIn("历史说明", prepared.messages[-1].content)

    def test_newest_long_turn_keeps_a_bounded_pair_instead_of_only_an_assistant(self):
        previous = [{"role": "user", "content": "盗窃罪的未成年人条件"}, {"role": "assistant", "content": "历史说明" * 20000}]
        with patch.object(settings, "HISTORY_MAX_CHARS", 200), patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), \
             patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 128), patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 64):
            prepared = rag.prepare_answer("继续", [], previous, mode="chat")
        self.assertEqual(len(prepared.messages), 4)
        self.assertIsInstance(prepared.messages[1], HumanMessage)
        self.assertIsInstance(prepared.messages[2], AIMessage)
        self.assertLessEqual(sum(len(item.content) for item in prepared.messages[1:-1]), 200)
        self.assertLessEqual(prepared.input_tokens, prepared.input_budget)

    def test_original_question_is_rejected_instead_of_silently_truncated(self):
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 1024), patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 64), \
             patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 64), patch.object(rag, "get_llm") as model:
            with self.assertRaises(ContextBudgetError):
                list(rag.answer_stream("很长的问题" * 1000, [], [], mode="chat"))
            model.assert_not_called()

    def test_zero_history_configuration_disables_history_but_keeps_current_question(self):
        with patch.object(settings, "HISTORY_MAX_TURNS", 0):
            prepared = rag.prepare_answer("本轮问题", [], history(), mode="chat")
        self.assertEqual(len(prepared.messages), 2)
        self.assertEqual(prepared.messages[-1].content, "本轮问题")

    def test_sources_that_do_not_fit_are_not_citation_candidates(self):
        pairs = [(LCDocument(page_content="资料" * 10000, metadata={"filename": f"{index}.txt"}), 0.2)
                 for index in range(6)]
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 128), \
             patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 64):
            prepared = rag.prepare_answer("问题", pairs, [])
        candidates = rag.to_citations(prepared.pairs)
        self.assertEqual([item["source_index"] for item in candidates], [1])
        self.assertEqual(rag.finalize_citations("内容见[6]", candidates), [])
        self.assertLess(estimate_tokens(prepared.pairs[0][0].page_content), estimate_tokens(pairs[0][0].page_content))

    def test_neighbor_context_over_budget_falls_back_to_actual_hit_and_narrows_source_range(self):
        metadata = {"filename": "law.txt", "document_id": "doc", "chunk_index": 1,
                    "chunk_start": 0, "chunk_end": 2, "page_start": 1, "page_end": 3,
                    "_anchor_content": "中心命中片段的原文", "_anchor_location": {"page_start": 2, "page_end": 2}}
        doc = LCDocument(page_content="前邻居原文" * 10000 + "\n中心命中片段的原文\n后邻居", metadata=metadata)
        before = copy.deepcopy(doc)
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 128), \
             patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 64):
            prepared = rag.prepare_answer("本轮问题", [(doc, 0.1)], [])
        self.assertEqual(prepared.pairs[0][0].page_content, "中心命中片段的原文")
        citation = rag.to_citations(prepared.pairs)[0]
        self.assertEqual((citation["chunk_index"], citation["chunk_start"], citation["chunk_end"]), (1, 1, 1))
        self.assertEqual((citation["page_start"], citation["page_end"]), (2, 2))
        self.assertNotIn("_anchor_content", citation)
        self.assertEqual(doc, before)
        self.assertLessEqual(prepared.input_tokens, prepared.input_budget)

    def test_verified_previous_source_has_a_current_number_and_no_fake_similarity(self):
        pairs = [(LCDocument(page_content="核验后的原文", metadata={
            "filename": "law.txt", "index_version": 2, "historical_source_index": 3, "citation_score": None,
        }), 0.0)]
        prepared = rag.prepare_answer("解释刚才第3条来源", pairs, history(1))
        self.assertIn("上一轮来源3，本轮使用编号1", prepared.messages[-1].content)
        citation = rag.to_citations(prepared.pairs)[0]
        self.assertEqual(citation["source_index"], 1)
        self.assertEqual(citation["index_version"], 2)
        self.assertIsNone(citation["score"])

    def test_ollama_uses_the_same_configured_context_window_as_the_budget(self):
        with patch.object(settings, "LLM_PROVIDER", "ollama"), patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 16384), \
             patch.object(rag, "ChatOllama") as model:
            rag.get_llm()
        self.assertEqual(model.call_args.kwargs["num_ctx"], 16384)
