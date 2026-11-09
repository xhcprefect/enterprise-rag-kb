"""按需追问补全、结构校验、来源消歧；不调用真实模型。"""
import copy
import json
import unittest
from unittest.mock import patch

from app.core.config import settings
from app.services import answer_sources, followup
from app.services.context_window import estimate_message_tokens


def snapshot(supplied=(), historical=()):
    return {"source_snapshot_version": 1, "supplied_sources": list(supplied), "history_sources": list(historical)}


def history(turns=1):
    result = []
    for index in range(turns):
        citations = [
            {"source_index": 1, "document_id": "00000000-0000-0000-0000-00000000000c", "chunk_index": 0, "index_version": 2},
            {"source_index": 3, "document_id": "00000000-0000-0000-0000-00000000000d", "chunk_index": 2, "index_version": 4},
        ]
        result.extend([
            {"id": f"q{index}", "role": "user", "content": f"盗窃罪如何量刑？（第{index}次提问）"},
            {"id": f"a{index}", "role": "assistant", "content": "法律规定见[1]和[3]。", "citations": citations,
             "source_provenance": snapshot([answer_sources.parse_source(citation) for citation in citations])},
        ])
    return result


def security_history(clarification=True):
    guide = {"source_index": 1, "document_id": "00000000-0000-0000-0000-000000000001",
             "filename": "数据安全指南.pdf", "chunk_index": 4, "index_version": 2}
    manual = {"source_index": 2, "document_id": "00000000-0000-0000-0000-000000000002",
              "filename": "星海科技内部管理手册（2026版）.pdf", "chunk_index": 7, "index_version": 3}
    citations = [guide, manual]
    result = [
        {"id": "security-q1", "role": "user", "content": "数据安全分几级"},
        {"id": "security-a1", "role": "assistant", "content": "《数据安全指南》分为三级[1]。\n《星海科技内部管理手册（2026版）》分为四级[2]。", "citations": citations,
         "source_provenance": snapshot([answer_sources.parse_source(citation) for citation in citations])},
    ]
    if clarification:
        result.extend([
            {"id": "security-q2", "role": "user", "content": "具体点呢"},
            {"id": "security-a2", "role": "assistant", "content": "您想了解哪份文件的数据安全分级具体内容：《数据安全指南》还是《星海科技内部管理手册（2026版）》？", "citations": [],
             "source_provenance": snapshot()},
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
            {"role": "assistant", "content": "您想了解成年人还是未成年人的量刑？", "citations": [],
             "source_provenance": snapshot()},
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
        original[1]["source_provenance"]["supplied_sources"].append(answer_sources.parse_source(older))
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
                {"role": "assistant", "content": "您想了解哪种合同或工资流程？", "citations": [], "source_provenance": snapshot()},
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
            original[-1]["source_provenance"] = snapshot()
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
            {"role": "assistant", "content": "已核验的手册内容[1]。", "citations": [source],
             "followup_topic": selected.topic,
             "source_provenance": snapshot([answer_sources.parse_source(source)])},
        ]
        plan = followup.plan_retrieval("再详细一点", compact)
        self.assertEqual(plan.status, "elaboration")
        self.assertIn("数据安全分几级", plan.query)
        self.assertIn("具体点呢", plan.topic)
        self.assertEqual([citation["document_id"] for citation in plan.citations], [source["document_id"]])

    def test_clarification_timeout_or_unhelpful_rewrite_keeps_topic_without_old_facts(self):
        original = history() + [
            {"role": "user", "content": "那其他情况呢？"},
            {"role": "assistant", "content": "您想了解成年人还是未成年人的量刑？", "citations": [],
             "source_provenance": snapshot()},
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
        huge = [{"role": "user", "content": "用户问题" * 10000},
                {"role": "assistant", "content": "历史说明" * 10000, "citations": [], "source_provenance": snapshot()}]
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), \
             patch.object(followup, "complete_short_chat", return_value='{"action":"search","query":"完整问题"}') as model:
            plan = followup.plan_retrieval("那未成年人呢？", huge)
        self.assertEqual(plan.status, "rewritten")
        messages = model.call_args.args[0]
        self.assertLessEqual(estimate_message_tokens(messages) + settings.RETRIEVAL_REWRITE_MAX_TOKENS + settings.CONTEXT_TOKEN_SAFETY_MARGIN, 4096)
        payload = json.loads(messages[1].content)
        self.assertLessEqual(sum(len(item["content"]) for item in payload["history"]), settings.RETRIEVAL_REWRITE_HISTORY_CHARS + 100)

    def test_rewrite_receives_current_scope_so_old_files_do_not_force_another_choice(self):
        scope = [{key: security_history()[1]["citations"][1][key]
                  for key in ("document_id", "filename", "index_version")}]
        with patch.object(followup, "complete_short_chat", return_value='{"action":"search","query":"手册中内部级外发审批的要求"}') as model:
            plan = followup.plan_retrieval("那审批要求呢？", security_history(clarification=False), document_scope=scope)
        self.assertEqual(plan.status, "rewritten")
        payload = json.loads(model.call_args.args[0][1].content)
        self.assertEqual(payload["document_scope"], scope)
        self.assertIn("已选定的文件范围", model.call_args.args[0][0].content)

    def test_long_scope_names_and_json_escaping_share_the_rewrite_input_budget(self):
        scope = [{"document_id": f"00000000-0000-0000-0000-{index:012d}",
                  "filename": "长文件名" * 70 + f"-{index}.pdf", "index_version": 1} for index in range(1, 6)]
        dialogue = [{"role": "user", "content": '规则\\\\"' * 1000},
                    {"role": "assistant", "content": '历史说明[3]\\\\"' * 1000, "citations": [],
                     "source_provenance": snapshot()}]
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 12000), \
             patch.object(followup, "complete_short_chat", return_value='{"action":"search","query":"审批要求"}') as model:
            plan = followup.plan_retrieval("那审批要求呢？", dialogue, document_scope=scope)
        self.assertEqual(plan.status, "rewritten")
        messages = model.call_args.args[0]
        self.assertEqual(json.loads(messages[1].content)["document_scope"], scope)
        self.assertLessEqual(estimate_message_tokens(messages) + settings.RETRIEVAL_REWRITE_MAX_TOKENS
                             + settings.CONTEXT_TOKEN_SAFETY_MARGIN, 12000)

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


def provenance_turn(question, answer, sources, historical=(), citations=()):
    """带严格 v1 来源快照的完整轮；sources 是 supplied、historical 是继承来源。"""
    assistant = {"role": "assistant", "content": answer, "citations": list(citations), "source_provenance": {
        "source_snapshot_version": 1, "supplied_sources": list(sources), "history_sources": list(historical)}}
    return [{"role": "user", "content": question}, assistant]


class PlanningSourcesTests(unittest.TestCase):
    OLD = {"source_kind": "document", "document_id": "00000000-0000-0000-0000-00000000000a",
           "filename": "old.txt", "index_version": 1, "chunk_index": 0}
    NEW = {"source_kind": "document", "document_id": "00000000-0000-0000-0000-00000000000b",
           "filename": "new.txt", "index_version": 2, "chunk_index": 1}

    def rewritten(self, question, turns, response='{"action":"search","query":"继承旧来源的检索问题"}'):
        # 单一模型 mock：返回计划与 mock，调用方直接验证模型确实被调用及收到的内容。
        with patch.object(settings, "RETRIEVAL_REWRITE_HISTORY_TURNS", 2), \
             patch.object(followup, "complete_short_chat", return_value=response) as model:
            plan = followup.plan_retrieval(question, turns)
        return plan, model

    def test_rewritten_plan_records_provenance_of_the_trimmed_recent_only(self):
        # 旧轮带着未引用的传递来源 OLD；近轮带 NEW；更早轮被改写窗口裁掉，不得虚加。
        oldest = provenance_turn("最早问题", "最早回答", [self.NEW])
        older = provenance_turn("旧问题", "旧回答", [self.OLD], historical=[])
        newer = provenance_turn("补充条件", "近回答", [])
        turns = [*oldest, *older, *newer]
        plan, model = self.rewritten("那种情况呢？", turns)
        self.assertEqual(plan.status, "rewritten")
        self.assertEqual([item["document_id"] for item in plan.planning_sources],
                         [self.OLD["document_id"]])
        self.assertNotIn(self.NEW["document_id"], [item["document_id"] for item in plan.planning_sources])
        self.assertIn("旧回答", model.call_args.args[0][1].content)

    def test_transitive_uncited_source_follows_the_rewrite_even_when_cited_one_is_selected(self):
        # supplied 含引用 A 与未引用 B；改写真正依赖的是该轮完整快照，B 必须跟随。
        a = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000c"}
        b = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000d"}
        turns = provenance_turn("旧问题", "旧回答引用A[1]。", [a, b])
        plan, _ = self.rewritten("那种情况呢？", turns)
        self.assertEqual(plan.status, "rewritten")
        self.assertEqual({item["document_id"] for item in plan.planning_sources},
                         {a["document_id"], b["document_id"]})

    def test_model_repeating_the_original_question_adds_no_planning_dependency(self):
        turns = provenance_turn("旧问题", "旧回答", [self.OLD])
        plan, model = self.rewritten("那未成年人呢？", turns, '{"action":"search","query":"那未成年人呢？"}')
        self.assertEqual(plan.status, "original")
        self.assertEqual(plan.planning_sources, [])
        self.assertEqual(model.call_count, 1)

    def test_independent_question_and_topic_switch_have_no_planning_dependency(self):
        turns = provenance_turn("旧问题", "旧回答", [self.OLD])
        with patch.object(followup, "complete_short_chat") as model:
            independent = followup.plan_retrieval("劳动合同试用期有多长？", turns)
            switch = followup.plan_retrieval("换个话题，什么是违约责任？", turns)
        self.assertEqual((independent.status, independent.planning_sources), ("original", []))
        self.assertEqual((switch.status, switch.planning_sources), ("original", []))
        model.assert_not_called()

    def test_bad_provenance_fails_closed_before_the_model_is_called(self):
        turns = provenance_turn("旧问题", "旧回答", [{"document_id": "not-a-uuid", "index_version": 1, "chunk_index": 0}])
        # 严格来源快照在模型调用之前整轮解析；坏源直接抛失败，模型永远不被调用。
        with patch.object(followup, "complete_short_chat") as model:
            # 坏 UUID 抛的是严格收集器基类；无论哪种坏源都必须在模型前整轮失败。
            with self.assertRaises(answer_sources.SourceSnapshotError):
                followup.plan_retrieval("那未成年人呢？", turns)
        model.assert_not_called()

    def test_missing_provenance_on_a_retained_assistant_fails_closed(self):
        turns = [{"role": "user", "content": "旧问题"},
                 {"role": "assistant", "content": "旧回答", "citations": []}]
        with patch.object(followup, "complete_short_chat") as model:
            with self.assertRaises(answer_sources.SourceSnapshotUnavailable):
                followup.plan_retrieval("那未成年人呢？", turns)
        model.assert_not_called()

    def test_oversized_planning_sources_fail_instead_of_being_trimmed(self):
        many = [{"source_kind": "document", "document_id": f"00000000-0000-0000-0000-{index:012d}",
                 "filename": f"d{index}.txt", "index_version": 1, "chunk_index": 0}
                for index in range(1, 258)]
        turns = provenance_turn("旧问题", "旧回答", many)
        with patch.object(followup, "complete_short_chat") as model:
            with self.assertRaises(answer_sources.SourceSnapshotOverflow):
                followup.plan_retrieval("那种情况呢？", turns)
        model.assert_not_called()

    def test_clarification_option_selection_keeps_transitive_source_of_clarification_turn(self):
        # 澄清轮快照带未引用 C；用户按“前者”选文件时实际读取了澄清轮选项正文，C 必须跟随。
        a = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000c", "filename": "甲手册.pdf"}
        b = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000d", "filename": "乙手册.pdf"}
        c = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000e", "filename": "丙手册.pdf"}
        cited = [{"source_index": 1, "document_id": a["document_id"], "chunk_index": 0, "index_version": 1, "filename": "甲手册.pdf"},
                 {"source_index": 2, "document_id": b["document_id"], "chunk_index": 0, "index_version": 1, "filename": "乙手册.pdf"}]
        anchor = provenance_turn("有哪些手册", "共有两份手册，见[1][2]。", [*cited, c], citations=cited)
        clarify = provenance_turn("那份呢", "您想了解《甲手册》还是《乙手册》？", [])
        plan = followup.plan_retrieval("前者", [*anchor, *clarify])
        self.assertEqual(plan.status, "document_followup")
        self.assertEqual({item["document_id"] for item in plan.citations}, {a["document_id"]})
        # 读取澄清轮选项顺序解析“前者”→甲；甲轮完整快照（含未引用 C）必须随计划。
        self.assertEqual({item["document_id"] for item in plan.planning_sources},
                         {a["document_id"], b["document_id"], c["document_id"]})

    def test_explicit_filename_choice_does_not_collect_unused_clarification_turn(self):
        # 显式文件名走 _named_choices，不读澄清轮正文；澄清轮的快照不被虚加。
        a = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000c", "filename": "甲手册.pdf"}
        b = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000d", "filename": "乙手册.pdf"}
        cited = [{"source_index": 1, "document_id": a["document_id"], "chunk_index": 0, "index_version": 1, "filename": "甲手册.pdf"},
                 {"source_index": 2, "document_id": b["document_id"], "chunk_index": 0, "index_version": 1, "filename": "乙手册.pdf"}]
        anchor = provenance_turn("有哪些手册", "共有两份手册，见[1][2]。", cited, citations=cited)
        clarify = provenance_turn("那份呢", "您想了解《甲手册》还是《乙手册》？", [b])
        plan = followup.plan_retrieval("选甲手册", [*anchor, *clarify])
        self.assertEqual(plan.status, "document_followup")
        self.assertEqual({item["document_id"] for item in plan.planning_sources},
                         {a["document_id"], b["document_id"]})

    def test_bad_provenance_of_used_clarification_turn_propagates(self):
        # “前者”需要解析澄清轮正文，该轮坏源必须传播；锚定轮自身合法不掩盖。
        a = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000c", "filename": "甲手册.pdf"}
        b = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000d", "filename": "乙手册.pdf"}
        cited = [{"source_index": 1, "document_id": a["document_id"], "chunk_index": 0, "index_version": 1, "filename": "甲手册.pdf"},
                 {"source_index": 2, "document_id": b["document_id"], "chunk_index": 0, "index_version": 1, "filename": "乙手册.pdf"}]
        anchor = provenance_turn("有哪些手册", "共有两份手册，见[1][2]。", cited, citations=cited)
        clarify = provenance_turn("那份呢", "您想了解《甲手册》还是《乙手册》？",
                                  [{"document_id": "not-a-uuid", "index_version": 1, "chunk_index": 0}])
        with self.assertRaises(answer_sources.SourceSnapshotError):
            followup.plan_retrieval("前者", [*anchor, *clarify])

    def test_reference_early_exit_clarify_carries_no_source_dependency(self):
        # 更早轮次/范围引用早退 clarify 没有采用上一轮内容，依赖必须为空。
        for question in ["第2轮的来源是什么", "来源1到3都解释一下"]:
            with self.subTest(question=question):
                plan = followup.plan_retrieval(question, history())
                self.assertEqual(plan.status, "clarify")
                self.assertEqual(plan.planning_sources, [])

    def test_reference_success_and_missing_index_collect_the_last_turn(self):
        # 成功核验与缺失编号分支实际读取了上一轮引用，两者都收该轮完整快照。
        plan = followup.plan_retrieval("解释刚才第3条来源", history())
        self.assertEqual(plan.status, "reference")
        self.assertEqual({item["document_id"] for item in plan.planning_sources},
                         {citation["document_id"] for citation in history()[1]["citations"]})
        cited = [{"source_index": 9, "document_id": "00000000-0000-0000-0000-00000000000c",
                  "chunk_index": 0, "index_version": 1, "filename": "孤本.pdf"}]
        single = provenance_turn("旧问题", "旧回答见[9]。", cited, citations=cited)
        plan = followup.plan_retrieval("解释刚才第1条来源", single)
        self.assertEqual(plan.status, "clarify")
        self.assertEqual([item["document_id"] for item in plan.planning_sources],
                         [single[1]["citations"][0]["document_id"]])

    def test_saved_topic_derivation_carries_its_real_dependency_beyond_recent(self):
        # 澄清轮回复原问题：最终 query 由 saved topic 派生，超出 recent 的旧来源必须随计划。
        older = provenance_turn("旧主题是什么", "旧回答", [self.OLD], historical=())
        recent = [{"role": "user", "content": "再说说吧"},
                  {"role": "assistant", "content": "您想了解成年人还是未成年人的量刑？", "citations": [],
                   "source_provenance": snapshot(),
                   "followup_topic": "未成年人盗窃罪的量刑规定"}]
        turns = [*older, *recent]
        plan, model = self.rewritten("成年人", turns, '{"action":"search","query":"成年人"}')
        # 派生 query 不同于原问题 → rewritten；真正被采用的 saved topic 锚定轮依赖被传递。
        self.assertEqual(plan.status, "rewritten")
        self.assertIn("未成年人盗窃罪的量刑规定", plan.query)
        self.assertEqual([item["document_id"] for item in plan.planning_sources],
                         [self.OLD["document_id"]])

    def test_reference_and_document_followup_carry_full_used_turn_provenance(self):
        # reference 的规划依赖是被核验那一轮的完整快照（含未引用的传递来源），不只是 citations。
        plan = followup.plan_retrieval("解释刚才第3条来源", history())
        self.assertEqual(plan.status, "reference")
        self.assertEqual(plan.citations[0]["source_index"], 3)
        self.assertEqual({item["document_id"] for item in plan.planning_sources},
                         {citation["document_id"] for citation in history()[1]["citations"]})
        selection = followup.plan_retrieval("星海科技内部管理手册", security_history())
        self.assertEqual(selection.status, "document_followup")
        self.assertEqual([item["document_id"] for item in selection.citations],
                         ["00000000-0000-0000-0000-000000000002"])
        # document_followup 的规划依赖覆盖完整锚定轮（含未被选择的另一份文件）。
        self.assertEqual({item["document_id"] for item in selection.planning_sources},
                         {"00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"})

    def test_elaboration_keeps_transitive_uncited_source_of_the_anchor_turn(self):
        # 主题锚定轮的快照带未引用 B；elaboration 成功后 B 必须随 planning_sources 传递。
        a = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000c"}
        b = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000d"}
        cited = [{**a, "source_index": 1}]
        turns = provenance_turn("旧问题", "旧回答引用A[1]。", [a, b], citations=cited)
        with patch.object(followup, "complete_short_chat") as model:
            plan = followup.plan_retrieval("具体点呢", turns)
        self.assertEqual(plan.status, "elaboration")
        self.assertEqual({item["document_id"] for item in plan.planning_sources},
                         {a["document_id"], b["document_id"]})
        model.assert_not_called()

    def test_with_fields_rejects_unknown_fields_and_never_touches_sources(self):
        base = followup.RetrievalPlan("解释刚才第3条来源", "reference", citations=[{"document_id": "x"}],
                                      planning_sources=[self.OLD])
        rebuilt = base.with_fields(status="clarify", clarification="请确认来源")
        self.assertEqual(rebuilt.status, "clarify")
        self.assertEqual(rebuilt.clarification, "请确认来源")
        self.assertEqual(rebuilt.query, base.query)
        self.assertEqual(rebuilt.citations, base.citations)
        self.assertEqual(rebuilt.planning_sources, base.planning_sources)
        with self.assertRaises(ValueError):
            base.with_fields(nonexistent_field=1)
        # 规划依赖只能由严格收集器增补，重建不得覆盖（防止 chat.py 检索重建丢依赖）。
        with self.assertRaises(ValueError):
            base.with_fields(planning_sources=[])
        with self.assertRaises(ValueError):
            base.with_fields(planning_sources=[self.NEW])
        # 传回同一列表（不改变集合）是允许的幂等操作。
        self.assertEqual(base.with_fields(planning_sources=base.planning_sources).planning_sources,
                         base.planning_sources)

    def test_unused_window_turn_with_bad_provenance_does_not_break_search(self):
        # 窗口内被预算裁出 recent 的旧轮未被本轮采用：其坏快照不影响 search，也不虚加依赖。
        bad = provenance_turn("无关旧问题", "无关旧回答" + "长" * 2000,
                              [{"document_id": "not-a-uuid", "index_version": 1, "chunk_index": 0}])
        good = provenance_turn("旧问题", "旧回答引用A[1]。", [self.OLD], citations=[{**self.OLD, "source_index": 1}])
        turns = [*bad, *good]
        with patch.object(settings, "RETRIEVAL_REWRITE_HISTORY_TURNS", 2), \
             patch.object(settings, "RETRIEVAL_REWRITE_HISTORY_CHARS", 400), \
             patch.object(followup, "complete_short_chat",
                          return_value='{"action":"search","query":"未成年人盗窃罪的量刑规定"}') as model:
            plan = followup.plan_retrieval("那种情况呢？", turns)
        self.assertEqual(plan.status, "rewritten")
        model.assert_called_once()
        # 模型改写只用了实际送入的 recent（好轮）；坏轮既未失败也未进入依赖。
        self.assertEqual([item["document_id"] for item in plan.planning_sources],
                         [self.OLD["document_id"]])

    def test_window_older_unrelated_turn_is_never_added_to_planning_dependency(self):
        # 旧轮来源超出改写窗口：source_followup 的依赖只取窗口内实际锚定轮，不收更早旧轮。
        oldest = provenance_turn("最早问题", "最早回答", [self.NEW])
        a = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000c"}
        b = {**self.OLD, "document_id": "00000000-0000-0000-0000-00000000000d"}
        window_turn = provenance_turn("旧问题", "旧回答引用A[1]。", [a, b], citations=[{**a, "source_index": 1}])
        turns = [*oldest, *window_turn]
        plan = followup.plan_retrieval("具体点呢", turns)
        self.assertEqual(plan.status, "elaboration")
        self.assertEqual({item["document_id"] for item in plan.planning_sources}, {a["document_id"], b["document_id"]})
        self.assertNotIn(self.NEW["document_id"], [item["document_id"] for item in plan.planning_sources])

    def test_with_sources_merges_and_normalizes_without_duplicating(self):
        base = followup.RetrievalPlan("q", "rewritten", planning_sources=[self.OLD])
        merged = base.with_sources([self.OLD, self.NEW])
        self.assertEqual([item["document_id"] for item in merged.planning_sources],
                         [self.OLD["document_id"], self.NEW["document_id"]])
        self.assertEqual(merged.query, base.query)
