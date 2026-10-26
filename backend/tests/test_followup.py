"""按需追问补全、结构校验、来源消歧；不调用真实模型。"""
import copy
import json
import unittest
from unittest.mock import patch

from app.core.config import settings
from app.services import followup
from app.services.context_window import estimate_message_tokens


def history(turns=1):
    result = []
    for index in range(turns):
        result.extend([
            {"id": f"q{index}", "role": "user", "content": f"盗窃罪如何量刑？（第{index}次提问）"},
            {"id": f"a{index}", "role": "assistant", "content": "法律规定见[1]和[3]。", "citations": [
                {"source_index": 1, "document_id": "first", "chunk_index": 0, "index_version": 2},
                {"source_index": 3, "document_id": "third", "chunk_index": 2, "index_version": 4},
            ]},
        ])
    return result


class FollowupTests(unittest.TestCase):
    def test_independent_questions_and_explicit_topic_switches_skip_model(self):
        for question in ["劳动合同试用期有多长？", "换个话题，什么是违约责任？", "另一个问题：税率是多少？", "new topic: annual leave"]:
            with self.subTest(question=question), patch.object(followup, "complete_short_chat") as model:
                plan = followup.plan_retrieval(question, history(3))
                self.assertEqual(plan.query, question)
                self.assertEqual(plan.status, "original")
                model.assert_not_called()

    def test_common_followups_use_bounded_rewrite_and_leave_inputs_untouched(self):
        original = history(5)
        before = copy.deepcopy(original)
        for question in ["那未成年人呢？", "为什么？", "第3点展开解释", "what about minors?"]:
            with self.subTest(question=question), patch.object(followup, "complete_short_chat", return_value='{"action":"search","query":"未成年人盗窃罪的量刑规定"}') as model:
                plan = followup.plan_retrieval(question, original)
                self.assertEqual(plan.status, "rewritten")
                self.assertEqual(plan.query, "未成年人盗窃罪的量刑规定")
                self.assertEqual(model.call_args.kwargs, {"timeout": settings.RETRIEVAL_REWRITE_TIMEOUT,
                                                         "max_tokens": settings.RETRIEVAL_REWRITE_MAX_TOKENS})
                payload = json.loads(model.call_args.args[0][1].content)
                self.assertEqual(payload["question"], question)
                self.assertEqual(len(payload["history"]), 6)
                self.assertNotIn("[3]", payload["history"][-1]["content"])
        self.assertEqual(original, before)

    def test_disabled_rewrite_skips_model_but_still_checks_explicit_previous_sources(self):
        with patch.object(settings, "RETRIEVAL_REWRITE_ENABLED", False), patch.object(followup, "complete_short_chat") as model:
            self.assertEqual(followup.plan_retrieval("那未成年人呢？", history()).status, "original")
            self.assertEqual(followup.plan_retrieval("解释刚才第3条来源", history()).status, "reference")
        model.assert_not_called()

    def test_orphans_and_empty_history_do_not_invent_a_topic(self):
        for items in [[], [{"role": "assistant", "content": "orphan"}], [{"role": "user", "content": "unanswered"}]]:
            with self.subTest(items=items), patch.object(followup, "complete_short_chat") as model:
                plan = followup.plan_retrieval("那未成年人呢？", items)
                self.assertEqual(plan.status, "clarify")
                self.assertTrue(plan.clarification)
                model.assert_not_called()

    def test_clarification_json_and_fenced_or_reasoning_wrapped_json_are_supported(self):
        search = '{"action":"search","query":"完整检索问题"}'
        for response in [search, "```json\n" + search + "\n```", "<think>推理</think>" + search]:
            with self.subTest(response=response), patch.object(followup, "complete_short_chat", return_value=response):
                self.assertEqual(followup.plan_retrieval("那未成年人呢？", history()).query, "完整检索问题")
        with patch.object(followup, "complete_short_chat", return_value='{"action":"clarify","question":"您指的是谁？"}'):
            self.assertEqual(followup.plan_retrieval("那他呢？", history()).clarification, "您指的是谁？")

    def test_invalid_results_and_transport_errors_fall_back_without_logging_response_content(self):
        invalid = ["secret-invalid-json", "[]", "null", '{}', '{"action":"search","query":12}',
                   '{"action":"search","query":" "}', '{"action":"clarify","question":null}',
                   json.dumps({"action": "search", "query": "x" * 2001}),
                   json.dumps({"action": "clarify", "question": "x" * 301})]
        for response in invalid:
            with self.subTest(response=response), patch.object(followup, "complete_short_chat", return_value=response), \
                 patch.object(followup.logger, "warning") as log:
                plan = followup.plan_retrieval("那未成年人呢？", history())
                self.assertEqual((plan.status, plan.query), ("fallback", "那未成年人呢？"))
                self.assertNotIn("secret-invalid-json", str(log.call_args))
        for error in [TimeoutError("secret"), RuntimeError("secret")]:
            with patch.object(followup, "complete_short_chat", side_effect=error), patch.object(followup.logger, "warning") as log:
                self.assertEqual(followup.plan_retrieval("那未成年人呢？", history()).status, "fallback")
                self.assertNotIn("secret", str(log.call_args))

    def test_rewrite_prompt_also_obeys_the_context_window(self):
        huge = [{"role": "user", "content": "用户问题" * 10000}, {"role": "assistant", "content": "历史说明" * 10000}]
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), \
             patch.object(followup, "complete_short_chat", return_value='{"action":"search","query":"完整问题"}') as model:
            plan = followup.plan_retrieval("那未成年人呢？", huge)
        self.assertEqual(plan.status, "rewritten")
        messages = model.call_args.args[0]
        self.assertLessEqual(estimate_message_tokens(messages) + settings.RETRIEVAL_REWRITE_MAX_TOKENS + settings.CONTEXT_TOKEN_SAFETY_MARGIN, 4096)
        payload = json.loads(messages[1].content)
        self.assertLessEqual(sum(len(item["content"]) for item in payload["history"]), settings.RETRIEVAL_REWRITE_HISTORY_CHARS + 100)

    def test_current_question_too_large_for_rewrite_falls_back_without_calling_model(self):
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 1024), patch.object(followup, "complete_short_chat") as model:
            plan = followup.plan_retrieval("那" + "很长的问题" * 2000, history())
        self.assertEqual(plan.status, "fallback")
        model.assert_not_called()

    def test_previous_source_uses_final_number_not_filtered_list_position(self):
        for question in ["解释刚才第3条来源", "解释来源3", "解释上一轮来源[3]", "说明刚才第三条引用"]:
            with self.subTest(question=question), patch.object(followup, "complete_short_chat") as model:
                plan = followup.plan_retrieval(question, history())
                self.assertEqual(plan.status, "reference")
                self.assertEqual([citation["source_index"] for citation in plan.citations], [3])
                self.assertEqual(plan.citations[0]["source_message_id"], "a0")
                self.assertEqual(plan.citations[0]["index_version"], 4)
                model.assert_not_called()

    def test_multiple_source_indices_are_all_resolved_without_silently_omitting_one(self):
        for question in ["解释刚才第1、3条来源", "比较上一轮第一条和第三条来源", "比较来源1和3", "解释刚才的来源[1][3]", "解释刚才第一条来源和第三条"]:
            with self.subTest(question=question):
                plan = followup.plan_retrieval(question, history())
                self.assertEqual(plan.status, "reference")
                self.assertEqual([citation["source_index"] for citation in plan.citations], [1, 3])

    def test_missing_sources_ambiguous_turns_and_ranges_ask_for_clarification(self):
        for question in ["解释刚才第2条来源", "解释第一轮第3条来源", "解释前面的第3条来源", "解释上上轮第3条来源", "解释上上次的第3条来源", "解释前两轮的第3条来源", "解释刚才的来源", "解释刚才来源1到3"]:
            with self.subTest(question=question), patch.object(followup, "complete_short_chat") as model:
                plan = followup.plan_retrieval(question, history(2))
                self.assertEqual(plan.status, "clarify")
                self.assertEqual(plan.citations, [])
                model.assert_not_called()
        self.assertEqual(followup.plan_retrieval("解释刚才第3条来源", []).status, "clarify")
