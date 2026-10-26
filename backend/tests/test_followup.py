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


def security_history(clarification=True):
    guide = {"source_index": 1, "document_id": "00000000-0000-0000-0000-000000000001",
             "filename": "数据安全指南.pdf", "chunk_index": 4, "index_version": 2}
    manual = {"source_index": 2, "document_id": "00000000-0000-0000-0000-000000000002",
              "filename": "星海科技内部管理手册（2026版）.pdf", "chunk_index": 7, "index_version": 3}
    result = [
        {"id": "security-q1", "role": "user", "content": "数据安全分几级"},
        {"id": "security-a1", "role": "assistant", "content": "《数据安全指南》分为三级[1]。\n《星海科技内部管理手册（2026版）》分为四级[2]。", "citations": [guide, manual]},
    ]
    if clarification:
        result.extend([
            {"id": "security-q2", "role": "user", "content": "具体点呢"},
            {"id": "security-a2", "role": "assistant", "content": "您想了解哪份文件的数据安全分级具体内容：《数据安全指南》还是《星海科技内部管理手册（2026版）》？", "citations": []},
        ])
    return result


class FollowupTests(unittest.TestCase):
    def test_document_selection_after_clarification_keeps_original_topic_and_sources(self):
        original = security_history()
        before = copy.deepcopy(original)
        with patch.object(followup, "complete_short_chat") as model:
            plan = followup.plan_retrieval("星海科技内部管理手册", original)
        self.assertEqual(plan.status, "document_followup")
        self.assertIn("数据安全分几级", plan.query)
        self.assertIn("具体点呢", plan.query)
        self.assertEqual([citation["source_index"] for citation in plan.citations], [2])
        self.assertEqual(plan.citations[0]["source_message_id"], "security-a1")
        self.assertNotIn("分为四级", plan.query)  # 旧答案不是本轮事实依据。
        self.assertEqual(original, before)
        model.assert_not_called()

    def test_elaboration_uses_both_verified_sources_instead_of_forcing_a_choice(self):
        with patch.object(followup, "complete_short_chat") as model:
            plan = followup.plan_retrieval("具体点呢", security_history(clarification=False))
        self.assertEqual(plan.status, "elaboration")
        self.assertIn("数据安全分几级", plan.query)
        self.assertEqual([citation["source_index"] for citation in plan.citations], [1, 2])
        model.assert_not_called()

    def test_short_answer_to_a_clarification_is_rewritten_with_the_original_question(self):
        original = history() + [
            {"role": "user", "content": "那其他情况呢？"},
            {"role": "assistant", "content": "您想了解成年人还是未成年人的量刑？", "citations": []},
        ]
        with patch.object(followup, "complete_short_chat", return_value='{"action":"search","query":"未成年人盗窃罪的量刑规定"}') as model:
            plan = followup.plan_retrieval("未成年人", original)
        self.assertEqual(plan.query, "未成年人盗窃罪的量刑规定")
        model.assert_called_once()
        self.assertIn("盗窃罪如何量刑", model.call_args.args[0][1].content)

    def test_document_titles_wrappers_ordinals_and_versions_resolve_the_displayed_choice(self):
        for question in ["《星海科技内部管理手册（2026版）》", "选星海科技内部管理手册", "就按《星海科技内部管理手册》为准", "第二个", "第二份", "2", "后者", "2026版"]:
            with self.subTest(question=question), patch.object(followup, "complete_short_chat") as model:
                plan = followup.plan_retrieval(question, security_history())
                self.assertEqual(plan.status, "document_followup")
                self.assertEqual([citation["source_index"] for citation in plan.citations], [2])
                model.assert_not_called()
        original = security_history()
        original[-1]["content"] = "您想了解《星海科技内部管理手册（2026版）》还是《数据安全指南》？"
        self.assertEqual(followup.plan_retrieval("第二份", original).citations[0]["source_index"], 1)
        self.assertEqual(followup.plan_retrieval("前者", original).citations[0]["source_index"], 2)

    def test_same_name_versions_are_not_silently_combined(self):
        original = security_history()
        older = {**original[1]["citations"][1], "source_index": 3,
                 "document_id": "00000000-0000-0000-0000-000000000003", "filename": "星海科技内部管理手册（2025版）.pdf"}
        original[1]["citations"].append(older)
        original[1]["content"] += "\n《星海科技内部管理手册（2025版）》另有要求[3]。"
        original[-1]["content"] = "您想了解《星海科技内部管理手册（2026版）》还是《星海科技内部管理手册（2025版）》？"
        with patch.object(followup, "complete_short_chat") as model:
            ambiguous = followup.plan_retrieval("星海科技内部管理手册", original)
            selected = followup.plan_retrieval("2025版", original)
        self.assertEqual(ambiguous.status, "clarify")
        self.assertEqual(ambiguous.citations, [])
        self.assertEqual([citation["source_index"] for citation in selected.citations], [3])
        model.assert_not_called()

    def test_answer_titles_bind_to_citation_identities_even_when_upload_names_are_opaque(self):
        original = security_history()
        for index, citation in enumerate(original[1]["citations"]):
            citation["filename"] = f"2026050700000{index}.pdf"
        plan = followup.plan_retrieval("星海科技内部管理手册", original)
        self.assertEqual(plan.status, "document_followup")
        self.assertEqual(plan.citations[0]["document_id"], "00000000-0000-0000-0000-000000000002")

    def test_unrelated_topic_and_explicit_topic_switch_cannot_borrow_old_sources(self):
        for question in ["劳动合同试用期有多长？", "工资流程"]:
            original = security_history() + [
                {"role": "user", "content": question},
                {"role": "assistant", "content": "您想了解哪种合同或工资流程？", "citations": []},
            ]
            with self.subTest(question=question), patch.object(followup, "complete_short_chat", return_value='{"action":"search","query":"星海科技内部管理手册规定的劳动合同与工资流程"}'):
                plan = followup.plan_retrieval("星海科技内部管理手册", original)
            self.assertEqual(plan.citations, [])
            self.assertNotIn("数据安全", plan.query)
        with patch.object(followup, "complete_short_chat") as model:
            plan = followup.plan_retrieval("换个话题，星海科技内部管理手册的薪酬规定", security_history())
        self.assertEqual(plan.status, "original")
        self.assertEqual(plan.citations, [])
        model.assert_not_called()

    def test_malformed_legacy_source_metadata_does_not_break_an_independent_question(self):
        for citations in [None, "invalid", ["invalid"], [{"document_id": "id", "filename": "file.txt", "source_index": [1]}]]:
            original = history()
            original[-1]["citations"] = citations
            with self.subTest(citations=citations), patch.object(followup, "complete_short_chat") as model:
                plan = followup.plan_retrieval("劳动合同试用期有多长？", original)
            self.assertEqual(plan.status, "original")
            model.assert_not_called()

    def test_disabled_model_rewrite_still_resolves_explicit_document_selection(self):
        with patch.object(settings, "RETRIEVAL_REWRITE_ENABLED", False), patch.object(followup, "complete_short_chat") as model:
            plan = followup.plan_retrieval("星海科技内部管理手册", security_history())
        self.assertEqual(plan.status, "document_followup")
        model.assert_not_called()

    def test_saved_topic_survives_history_trimming_and_subsequent_elaboration(self):
        original = security_history()
        selected = followup.plan_retrieval("星海科技内部管理手册", original)
        source = {**original[1]["citations"][1], "source_index": 1}
        compact = [
            {"role": "user", "content": "星海科技内部管理手册"},
            {"role": "assistant", "content": "已核验的手册内容[1]。", "citations": [source], "followup_topic": selected.topic},
        ]
        plan = followup.plan_retrieval("再详细一点", compact)
        self.assertEqual(plan.status, "elaboration")
        self.assertIn("数据安全分几级", plan.query)
        self.assertIn("具体点呢", plan.topic)
        self.assertEqual([citation["document_id"] for citation in plan.citations], [source["document_id"]])

    def test_clarification_timeout_or_unhelpful_rewrite_keeps_topic_without_old_facts(self):
        original = history() + [
            {"role": "user", "content": "那其他情况呢？"},
            {"role": "assistant", "content": "您想了解成年人还是未成年人的量刑？", "citations": []},
        ]
        for value in [TimeoutError("private-error"), '{"action":"search","query":"未成年人"}']:
            kwargs = {"side_effect": value} if isinstance(value, Exception) else {"return_value": value}
            with self.subTest(value=type(value).__name__), patch.object(followup, "complete_short_chat", **kwargs), patch.object(followup.logger, "warning"):
                plan = followup.plan_retrieval("未成年人", original)
            self.assertIn("盗窃罪如何量刑", plan.query)
            self.assertIn("未成年人", plan.query)
            self.assertNotIn("法律规定", plan.query)
            self.assertEqual(plan.topic, plan.query)

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
