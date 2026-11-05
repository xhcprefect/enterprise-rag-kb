"""组装后的总上下文预算、完整轮次和历史引用隔离。"""
import copy
import unittest
import uuid
from unittest.mock import patch

from langchain_core.documents import Document as LCDocument
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.config import settings
from app.services import rag
from app.services.context_window import ContextBudgetError, estimate_message_tokens, estimate_tokens
from app.services.conversation_memory import assemble_recall


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


# 跨 assemble_recall -> prepare_answer 的最终 prompt 接点回归：低信任摘要必须作为
# 普通 Human 被带回最终消息（不被 trim_history/complete_turns 当孤立 user 丢掉），
# 且当前问题/最近连续完整后缀优先、同一输入预算内、不得升高成 System 或伪造配对。
# 纯函数，无 DB / 模型 / 网络调用。
class RecallToPrepareAnswerRegressionTest(unittest.TestCase):
    def _recent_flat(self, turns=3):
        """真实形状的最近完整轮：每条消息有自己的 UUID id，assistant 通过 reply_to_id
        显式指向其所属 user（同一 turn_id），seq 单调递增。这样 assemble_recall 的
        _group_complete_turns 与 prepare_answer 的 complete_turns 都能把它们配对成完整轮。"""
        flat = []
        for seq in range(1, turns + 1):
            uid = str(uuid.uuid4())
            flat.append({"seq": seq, "role": "user", "content": f"问题{seq}",
                         "status": "completed", "id": uid, "turn_id": uid})
            flat.append({"seq": seq, "role": "assistant", "content": f"回答{seq}",
                         "status": "completed", "id": str(uuid.uuid4()),
                         "turn_id": uid, "reply_to_id": uid})
        return flat

    def test_summary_survives_into_final_prompt_as_human_and_stays_in_budget(self):
        summary_text = "用户决定长期记住：量刑细则以资料为准。"
        recent = self._recent_flat(3)
        history, truncated, _ = assemble_recall(
            summary_text=summary_text, recent_turns=recent,
            budget_tokens=settings.MEMORY_HISTORY_TOKEN_BUDGET,
            keep_recent=settings.MEMORY_KEEP_RECENT_TURNS,
        )
        self.assertFalse(truncated)
        # 前置单条低信任摘要确实在组装历史头部。
        self.assertEqual(history[0]["role"], "user")
        self.assertTrue(history[0]["content"].startswith(rag.MEMORY_SUMMARY_MARKER))
        with patch.object(settings, "HISTORY_MAX_TURNS", 10), \
             patch.object(settings, "HISTORY_MAX_CHARS", 10_000_000):
            prepared = rag.prepare_answer("当前这个问题？", [], history, mode="chat")
        kinds = [type(m).__name__ for m in prepared.messages]
        self.assertEqual(kinds[0], "SystemMessage")
        self.assertEqual(kinds[-1], "HumanMessage")
        # 摘要仍作为 Human 出现在最终消息（位于 System 之后、current 之前）。
        summary_msgs = [m for m in prepared.messages
                        if isinstance(m, HumanMessage) and m.content.startswith(rag.MEMORY_SUMMARY_MARKER)]
        self.assertEqual(len(summary_msgs), 1)
        self.assertNotIn("SystemMessage", [type(summary_msgs[0]).__name__])
        # 当前问题保留、预算内。
        self.assertEqual(prepared.messages[-1].content, "当前这个问题？")
        # 逐一断言每个近期 user/assistant 原文都按 (user, assistant) 且 seq 升序
        # 完整出现在最终 prompt（System 之后、当前问题之前）。
        non_summary = [m for m in prepared.messages
                       if not m.content.startswith(rag.MEMORY_SUMMARY_MARKER)]
        turn_msgs = non_summary[1:-1]
        self.assertEqual(len(turn_msgs) % 2, 0)
        pair_contents = [(turn_msgs[i].content, turn_msgs[i + 1].content)
                         for i in range(0, len(turn_msgs), 2)]
        self.assertEqual(pair_contents, [(f"问题{seq}", f"回答{seq}") for seq in range(1, 4)])
        self.assertEqual(prepared.input_tokens, estimate_message_tokens(prepared.messages))
        self.assertLessEqual(prepared.input_tokens, prepared.input_budget)

    def test_summary_dropped_when_budget_is_too_small_but_question_and_recent_survive(self):
        recent = self._recent_flat(3)
        history, truncated, _ = assemble_recall(
            summary_text="长摘要" * 4000, recent_turns=recent,
            budget_tokens=settings.MEMORY_HISTORY_TOKEN_BUDGET,
            keep_recent=settings.MEMORY_KEEP_RECENT_TURNS,
        )
        # 长摘要远超窗口，无法在近轮之后还有空余预算，应被丢弃并如实标记截断。
        self.assertTrue(truncated)
        # 窗口：能容纳至少一个完整近轮（问题3+回答3）但绝容纳不下 36000 字节的长摘要。
        with patch.object(settings, "HISTORY_MAX_TURNS", 10), \
             patch.object(settings, "HISTORY_MAX_CHARS", 1000), \
             patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 512), \
             patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 64), \
             patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 32):
            prepared = rag.prepare_answer("挤吗？", [], history, mode="chat")
        # 当前问题不变；摘要被丢弃，不进入最终 prompt。
        self.assertEqual(prepared.messages[-1].content, "挤吗？")
        self.assertFalse(any(isinstance(m, HumanMessage)
                             and m.content.startswith(rag.MEMORY_SUMMARY_MARKER)
                             for m in prepared.messages))
        # 实际断言保留最近完整配对（最新一轮 user/assistant 原文都在），而非仅 >=2。
        recent_pair = [m.content for m in prepared.messages
                       if not m.content.startswith(rag.MEMORY_SUMMARY_MARKER)][1:-1]
        self.assertIn("问题3", recent_pair)
        self.assertIn("回答3", recent_pair)
        self.assertLessEqual(prepared.input_tokens, prepared.input_budget)

    def test_turn_id_without_reply_to_is_not_a_complete_turn(self):
        """负控：assistant 有 turn_id 却缺 reply_to_id 的畸形消息不能当完整轮。

        即便 assemble_recall 按同 seq 把它们临时配在一起，真实配对逻辑 complete_turns
        要求显式 reply link；有 turn_id 却不指向其 user 的消息既走不了 reply-link 分支、
        也回退不到 legacy 邻接配对，必须被整对丢弃，不能带着畸形内容进入最终 prompt。
        """
        flat = [
            {"seq": 1, "role": "user", "content": "好问题1", "status": "completed",
             "id": "good-q-1", "turn_id": "good-t-1"},
            {"seq": 1, "role": "assistant", "content": "好回答1", "status": "completed",
             "id": "good-a-1", "turn_id": "good-t-1", "reply_to_id": "good-q-1"},
            # 畸形：assistant 有 turn_id，但缺 reply_to_id，无法显式指向其 user。
            {"seq": 2, "role": "user", "content": "畸形问题2", "status": "completed",
             "id": "mal-q-2", "turn_id": "mal-t-2"},
            {"seq": 2, "role": "assistant", "content": "畸形回答2", "status": "completed",
             "id": "mal-a-2", "turn_id": "mal-t-2"},
        ]
        history, _, _ = assemble_recall(
            summary_text=None, recent_turns=flat,
            budget_tokens=settings.MEMORY_HISTORY_TOKEN_BUDGET,
            keep_recent=settings.MEMORY_KEEP_RECENT_TURNS,
        )
        with patch.object(settings, "HISTORY_MAX_TURNS", 10), \
             patch.object(settings, "HISTORY_MAX_CHARS", 10_000_000):
            prepared = rag.prepare_answer("当前问题", [], history, mode="chat")
        non_summary = [m for m in prepared.messages
                       if not m.content.startswith(rag.MEMORY_SUMMARY_MARKER)]
        joined = "\n".join(m.content for m in non_summary)
        # 正常的显式 reply link 轮仍完整保留。
        self.assertIn("好问题1", joined)
        self.assertIn("好回答1", joined)
        # 有 turn_id 却缺 reply_to 的畸形轮不能当完整轮，user/assistant 都不能进入 prompt。
        self.assertNotIn("畸形问题2", joined)
        self.assertNotIn("畸形回答2", joined)
