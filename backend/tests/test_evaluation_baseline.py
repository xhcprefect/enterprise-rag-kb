"""基线比较、身份、延迟分位数、阈值方向与 CLI 退出码；不访问网络、数据库或模型。"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import evaluation as ev
from scripts.evaluate_rag import main

ROOT = Path(__file__).parents[1] / "evaluation"
RUN_ID = "00000000-0000-4000-8000-000000000010"


def _cases(name="cases.platform.example.jsonl"):
    return ev.validate_cases(ev.read_jsonl(ROOT / name))


def _responses(name="responses.platform.example.jsonl"):
    return {row["id"]: row for row in ev.validate_responses(ev.read_jsonl(ROOT / name))}


def _report(cases, responses, *, mode="offline", docs=True, thresholds=None, **config):
    results = [ev.grade_case(case, responses.get(case["id"], {"error": "missing_response"})) for case in cases]
    identity = ev.build_identity(cases, mode=mode, documents_dir=ROOT / "documents" if docs else None, **config)
    return ev.make_report(results, RUN_ID, identity=identity)


class BaselineIdentityTests(unittest.TestCase):
    def test_identity_is_stable_and_changes_with_dataset_or_documents(self):
        cases = _cases()
        first = ev.build_identity(cases, mode="offline", documents_dir=ROOT / "documents")
        second = ev.build_identity(cases, mode="offline", documents_dir=ROOT / "documents")
        self.assertEqual(first["dataset"]["sha256"], second["dataset"]["sha256"])
        # 语料身份覆盖整棵声明目录：被点名 3 份 + 未点名干扰资料 3 份。
        self.assertEqual(first["dataset"]["documents"]["files"], 6)
        self.assertEqual(first["dataset"]["documents"]["kind"], "declared-local-snapshot")
        changed = [dict(case) for case in cases]
        changed[0] = {**changed[0], "required_phrases": ["绝对不存在的短语"]}
        other = ev.build_identity(changed, mode="offline", documents_dir=ROOT / "documents")
        self.assertNotEqual(first["dataset"]["sha256"], other["dataset"]["sha256"])
        no_docs = ev.build_identity(cases, mode="offline", documents_dir=None)
        self.assertIsNone(no_docs["dataset"]["documents"]["sha256"])

    def test_identity_never_contains_secrets_urls_or_content(self):
        cases = _cases()
        identity = ev.build_identity(cases, mode="offline", documents_dir=ROOT / "documents", model="demo-model")
        text = json.dumps(identity, ensure_ascii=False)
        self.assertNotIn("http", text)
        self.assertNotIn("提前3天", text)
        self.assertNotIn("重新登录", text)
        self.assertEqual(identity["config"]["model"], "demo-model")
        for bad in ({"model": "https://secret.example/api"}, {"model": "user:pass@host"}, {"model": "sk-live-secret"}):
            with self.subTest(bad=bad):
                if bad["model"].startswith("sk-"):
                    continue  # 纯符号形态不构成 URL/账号，允许作为公开模型名。
                with self.assertRaises(ev.EvaluationError):
                    ev.build_identity(cases, mode="offline", documents_dir=None, **bad)

    def test_config_validation_rejects_unknown_or_bad_values(self):
        with self.assertRaises(ev.EvaluationError):
            ev.config_identity(mode="replay")
        with self.assertRaises(ev.EvaluationError):
            ev.config_identity(mode="offline", chunk_size=800, chunk_overlap=800)
        with self.assertRaises(ev.EvaluationError):
            ev.config_identity(mode="offline", retrieval_top_k=21)
        with self.assertRaises(ev.EvaluationError):
            ev.config_identity(mode="offline", retrieval_rewrite="maybe")
        with self.assertRaises(ev.EvaluationError):
            ev.config_identity(mode="offline", chunk_size=True)

    def test_cli_rejects_config_with_unknown_or_secret_keys_without_echoing_values(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"RAG_EVAL_TOKEN": "sk-should-not-print"}):
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({"model": "m", "token": "sk-should-not-print"}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                             "--responses", str(ROOT / "responses.platform.example.jsonl"),
                             "--config-file", str(config), "--output", str(Path(temp) / "r.json")])
            self.assertEqual(code, 2)
            self.assertNotIn("sk-should-not-print", output.getvalue())


class LatencyTests(unittest.TestCase):
    def test_percentiles_interpolate_and_keep_missing_as_none(self):
        self.assertEqual(ev._percentile([10, 20], 0.5), 15.0)
        self.assertAlmostEqual(ev._percentile([1, 2, 3, 4], 0.95), 3.85)
        self.assertIsNone(ev._percentile([], 0.95))
        self.assertEqual(ev._percentile([7], 0.95), 7)

    def test_report_records_percentiles_and_sample_counts(self):
        report = _report(_cases(), _responses())
        self.assertEqual(report["first_token_ms_samples"], 8)
        self.assertEqual(report["first_token_ms_p50"], 25.0)
        self.assertIsNotNone(report["total_ms_p95"])
        missing = ev.make_report([ev.grade_case({"id": "x"}, {"answer": "ok"})])
        self.assertIsNone(missing["first_token_ms_p50"])
        self.assertIsNone(missing["total_ms_p95"])
        self.assertEqual(missing["first_token_ms_samples"], 0)


class ComparisonTests(unittest.TestCase):
    def test_identical_reports_are_no_regression(self):
        cases, responses = _cases(), _responses()
        baseline = _report(cases, responses)
        current = _report(cases, responses)
        comparison = ev.compare_reports(baseline, current)
        self.assertEqual((comparison["status"], comparison["verdict"]), ("comparable", "no_regression"))
        self.assertEqual(comparison["regressions"], [])
        self.assertEqual(comparison["metrics"]["assertion_pass_rate"]["delta"], 0)

    def test_assertion_regression_is_detected_and_reports_per_metric_delta(self):
        cases = _cases()
        baseline = _report(cases, _responses())
        current = _report(cases, _responses("responses.platform.regressed.example.jsonl"))
        comparison = ev.compare_reports(baseline, current)
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["verdict"], "regression")
        self.assertIn("assertion_pass_rate", comparison["regressions"])
        self.assertLess(comparison["metrics"]["assertion_pass_rate"]["delta"], 0)
        self.assertIn("citation_source_recall", comparison["regressions"])

    def _comparable(self, **values):
        """构造身份一致的最小报告，只改动指定指标。"""
        base = _report(_cases(), _responses())
        return {**base, **values}

    def test_lower_is_better_metric_direction_cannot_be_flipped_by_caller(self):
        baseline = self._comparable(first_token_ms_p95=100.0, total_ms_p95=100.0)
        current = {**baseline, "first_token_ms_p95": 160.0, "total_ms_p95": 160.0}
        comparison = ev.compare_reports(baseline, current, {"first_token_ms_p95": {"max_relative_change": 0.25}})
        self.assertEqual(comparison["metrics"]["first_token_ms_p95"]["direction"], "lower")
        self.assertEqual(comparison["metrics"]["first_token_ms_p95"]["status"], "regressed")
        self.assertGreater(comparison["metrics"]["first_token_ms_p95"]["delta"], 0)

    def test_absolute_and_relative_thresholds_and_zero_baseline(self):
        base = self._comparable(assertion_pass_rate=1.0)
        current = {**base, "assertion_pass_rate": 0.9}
        absolute = ev.compare_reports(base, current, {"assertion_pass_rate": {"max_absolute_change": 0.05}})
        self.assertEqual(absolute["verdict"], "regression")
        allowed = ev.compare_reports(base, current, {"assertion_pass_rate": {"max_absolute_change": 0.2}})
        self.assertEqual(allowed["metrics"]["assertion_pass_rate"]["status"], "within_tolerance")
        relative = ev.compare_reports(base, current, {"assertion_pass_rate": {"max_relative_change": 0.05}})
        self.assertEqual(relative["verdict"], "regression")
        zero_base = self._comparable(first_token_ms_p95=0.0)
        zero_current = {**zero_base, "first_token_ms_p95": 50.0}
        zero = ev.compare_reports(zero_base, zero_current, {"first_token_ms_p95": {"max_relative_change": 0.5}})
        self.assertEqual(zero["metrics"]["first_token_ms_p95"]["status"], "regressed")
        self.assertIsNone(zero["metrics"]["first_token_ms_p95"]["relative_delta"])
        self.assertEqual(zero["verdict"], "regression")

    def test_improvement_is_reported_separately_from_regression(self):
        base = self._comparable(assertion_pass_rate=0.5)
        current = {**base, "assertion_pass_rate": 1.0}
        comparison = ev.compare_reports(base, current)
        self.assertEqual(comparison["metrics"]["assertion_pass_rate"]["status"], "improved")
        self.assertIn("assertion_pass_rate", comparison["improvements"])
        self.assertEqual(comparison["verdict"], "no_regression")

    def test_missing_gated_metric_is_unknown_and_never_zero(self):
        cases = _cases()
        baseline = _report(cases, _responses())
        current = _report(cases, _responses("responses.platform.missing-metrics.example.jsonl"))
        comparison = ev.compare_reports(baseline, current, {"first_token_ms_p95": {"max_relative_change": 0.25}})
        self.assertEqual(comparison["metrics"]["first_token_ms_p95"]["status"], "unknown")
        self.assertIsNone(comparison["metrics"]["first_token_ms_p95"]["current"])
        self.assertEqual(comparison["verdict"], "unknown")
        self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 5)
        ungated = ev.compare_reports(baseline, current)
        self.assertEqual(ungated["metrics"]["first_token_ms_p95"]["status"], "unknown")
        self.assertIn("first_token_ms_p95", ungated["unknown_metrics"])
        self.assertEqual(ungated["verdict"], "unknown")
        both_missing = ev.compare_reports(
            ev.make_report([ev.grade_case({"id": "a"}, {"answer": "ok", "total_ms": 5})]),
            ev.make_report([ev.grade_case({"id": "a"}, {"answer": "ok", "total_ms": 6})]))
        self.assertEqual(both_missing["metrics"]["first_token_ms_p95"]["status"], "not_applicable")

    def test_partial_latency_coverage_is_unknown_not_a_cheap_average(self):
        baseline = _report(_cases(), _responses())
        # 只返回了部分样本时，均值会偏低；不能与全量基线比较后判为变快。
        current = {**baseline, "first_token_ms_mean": 10.0, "first_token_ms_samples": 4}
        comparison = ev.compare_reports(baseline, current, {"first_token_ms_mean": {"max_relative_change": 0.5}})
        self.assertEqual(comparison["metrics"]["first_token_ms_mean"]["status"], "unknown")
        self.assertEqual(comparison["metrics"]["first_token_ms_mean"]["detail"], "partial_samples")
        self.assertEqual(comparison["verdict"], "unknown")

    def test_dataset_config_and_legacy_identity_are_incomparable(self):
        cases = _cases()
        baseline = _report(cases, _responses())
        other = _report(_cases("cases.example.jsonl"), _responses("responses.example.jsonl"))
        mismatch = ev.compare_reports(baseline, other)
        self.assertEqual(mismatch["status"], "incomparable")
        self.assertIn("dataset_changed", mismatch["reasons"])
        legacy = {key: value for key, value in baseline.items() if key != "identity"}
        legacy_compare = ev.compare_reports(legacy, _report(cases, _responses()))
        self.assertEqual(legacy_compare["status"], "incomparable")
        self.assertIn("baseline_identity_missing", legacy_compare["reasons"])
        self.assertEqual(ev.comparison_exit_code(legacy_compare, assertions_passed=True), 4)

    def test_live_reports_without_model_identity_are_incomparable(self):
        cases = _cases()
        baseline = _report(cases, _responses(), mode="live")
        current = _report(cases, _responses(), mode="live", model="m1")
        comparison = ev.compare_reports(baseline, current)
        self.assertEqual(comparison["status"], "incomparable")
        self.assertIn("config_unverifiable:model", comparison["reasons"])

    def test_config_change_reason_names_the_field_not_its_value(self):
        cases = _cases()
        baseline = _report(cases, _responses(), model="model-a")
        current = _report(cases, _responses(), model="model-b")
        comparison = ev.compare_reports(baseline, current)
        self.assertIn("config_changed:model", comparison["reasons"])
        self.assertNotIn("model-a", json.dumps(comparison, ensure_ascii=False))

    def test_bad_baseline_structures_raise_evaluation_error_instead_of_type_error(self):
        base = _report(_cases(), _responses())
        for change in ({"assertion_pass_rate": "0.9"}, {"total_ms_p95": {"value": 1}},
                       {"cases": 3}, {"cases": True}, {"passed": 99},
                       {"first_token_ms_samples": 99}, {"first_token_ms_samples": 0, "first_token_ms_p95": 1.0},
                       {"results": "nope"}, {"identity": []},
                       {"identity": {"dataset": {"sha256": "not-a-digest"}}}):
            with self.subTest(change=change):
                broken = {**base, **change}
                with self.assertRaises(ev.EvaluationError):
                    ev.compare_reports(broken, base)
                with self.assertRaises(ev.EvaluationError):
                    ev.compare_reports(base, broken)
        # 合法但两侧都缺 dataset.sha256 时，不能因 None == None 判成同一数据集。
        left = json.loads(json.dumps(base))
        right = json.loads(json.dumps(base))
        left["identity"]["dataset"]["sha256"] = None
        right["identity"]["dataset"]["sha256"] = None
        comparison = ev.compare_reports(left, right)
        self.assertEqual(comparison["status"], "incomparable")
        self.assertIn("dataset_identity_missing", comparison["reasons"])
        self.assertNotEqual(comparison["verdict"], "no_regression")

    def test_nested_identity_config_is_validated_not_used_as_dict_key(self):
        base = _report(_cases(), _responses())
        for config in ({"mode": []}, {"mode": {"offline": True}}, {"mode": "replay"},
                       {"mode": "offline", "chunk_size": "800"},
                       {"mode": "offline", "chunk_overlap": []},
                       {"mode": "offline", "retrieval_top_k": 99},
                       {"mode": "offline", "retrieval_rewrite": ["on"]},
                       {"mode": "offline", "model": ["m"]}):
            with self.subTest(config=config):
                broken = json.loads(json.dumps(base))
                broken["identity"]["config"] = config
                with self.assertRaises(ev.EvaluationError):
                    ev.compare_reports(broken, base)
                with self.assertRaises(ev.EvaluationError):
                    ev.compare_reports(base, broken)

    def test_huge_integers_and_missing_coverage_counts_never_crash_or_pass(self):
        base = _report(_cases(), _responses())
        for name in ("total_ms_mean", "total_ms_p95", "first_token_ms_p95", "assertion_pass_rate"):
            with self.subTest(name=name):
                broken = json.loads(json.dumps(base))
                broken[name] = 10 ** 400
                with self.assertRaises(ev.EvaluationError):
                    ev.compare_reports(broken, base)
        for thresholds in ({"total_ms_p95": {"max_absolute_change": 10 ** 400}},
                           {"total_ms_p95": {"max_relative_change": 10 ** 400}}):
            with self.subTest(thresholds=thresholds), self.assertRaises(ev.EvaluationError):
                ev.validate_thresholds(thresholds)
        # 有指标值但缺样本计数：无法证明覆盖范围，必须 unknown 而不是 no_regression。
        for report_key, metric in (("first_token_ms_samples", "first_token_ms_p95"),
                                   ("total_ms_samples", "total_ms_mean"),
                                   ("generation_cost_samples", "generation_cost_estimate")):
            with self.subTest(report_key=report_key):
                missing = json.loads(json.dumps(base))
                missing.pop(report_key, None)
                missing[metric] = missing.get(metric) or 10.0
                if metric == "generation_cost_estimate":
                    missing[metric] = 0.5
                comparison = ev.compare_reports(base, missing)
                self.assertEqual(comparison["metrics"][metric]["status"], "unknown")
                self.assertEqual(comparison["metrics"][metric]["detail"], "coverage_unverifiable")
                self.assertEqual(comparison["verdict"], "unknown")
                self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 5)
                both = json.loads(json.dumps(missing))
                both["cases"] = missing["cases"]
                pair = ev.compare_reports(missing, both)
                self.assertEqual(pair["metrics"][metric]["status"], "unknown")
                self.assertNotEqual(pair["verdict"], "no_regression")

    def test_threshold_validation_rejects_unknown_metrics_bad_values_and_bools(self):
        for thresholds in ({"unknown_metric": {"max_absolute_change": 0.1}},
                           {"assertion_pass_rate": {"max_absolute_drop": 0.1}},
                           {"assertion_pass_rate": {"max_absolute_change": -1}},
                           {"assertion_pass_rate": {"max_absolute_change": True}},
                           {"assertion_pass_rate": {"max_absolute_change": float("inf")}},
                           {"assertion_pass_rate": 1}):
            with self.subTest(thresholds=thresholds), self.assertRaises(ev.EvaluationError):
                ev.validate_thresholds(thresholds)

    def test_comparison_exit_code_priority(self):
        comparable = {"status": "comparable", "verdict": "regression"}
        self.assertEqual(ev.comparison_exit_code(comparable, assertions_passed=False), 1)
        self.assertEqual(ev.comparison_exit_code(comparable, assertions_passed=True), 3)
        self.assertEqual(ev.comparison_exit_code({"status": "incomparable", "verdict": "incomparable"}, assertions_passed=True), 4)
        self.assertEqual(ev.comparison_exit_code({"status": "unknown", "verdict": "unknown"}, assertions_passed=True), 5)
        self.assertEqual(ev.comparison_exit_code({"status": "comparable", "verdict": "no_regression"}, assertions_passed=True), 0)


class ExpandedFixtureTests(unittest.TestCase):
    def test_platform_fixture_covers_multi_chunk_cross_doc_refusal_followup_and_scope(self):
        cases = _cases()
        self.assertEqual(len(cases), 8)
        by_id = {case["id"]: case for case in cases}
        self.assertTrue(by_id["platform-cross-doc"]["expected_sources"])
        self.assertEqual(len(by_id["platform-cross-doc"]["expected_sources"]), 2)
        self.assertEqual(len(by_id["platform-scope-selected"]["document_ids"]), 2)
        self.assertLessEqual(len(by_id["platform-scope-followup"]["document_ids"]), 5)
        self.assertEqual(len(by_id["platform-followup"]["turns"]), 2)
        self.assertTrue(by_id["platform-topic-switch"]["turns"][1].startswith("换个话题"))
        self.assertEqual(by_id["platform-refuse"]["expected_outcome"], "refuse")

    def test_referenced_documents_actually_produce_multiple_chunks(self):
        from app.services.structured_documents import iter_structured_chunks
        cases = _cases()
        referenced = {source["filename"] for case in cases for source in case.get("expected_sources", [])}
        self.assertGreaterEqual(len(referenced), 3)
        for name in ("合成数据平台操作手册.md", "合成安全响应预案.md"):
            self.assertIn(name, referenced)
            chunks = list(iter_structured_chunks(ROOT / "documents" / name, 800, 120))
            self.assertGreaterEqual(len(chunks), 4, name)
            indexes = sorted({source["chunk_index"] for case in cases for source in case.get("expected_sources", [])
                              if source["filename"] == name and "chunk_index" in source})
            self.assertTrue(indexes)
            self.assertLess(max(indexes), len(chunks))

    def test_old_generation_index_version_must_match(self):
        case = {"id": "old", "expected_sources": [{"filename": "doc", "chunk_index": 2, "index_version": 3}]}
        self.assertTrue(ev.grade_case(case, {"answer": "ok", "citations": [{"filename": "doc", "chunk_index": 2, "index_version": 3}]})["passed"])
        stale = ev.grade_case(case, {"answer": "ok", "citations": [{"filename": "doc", "chunk_index": 2, "index_version": 1}]})
        self.assertFalse(stale["passed"])
        self.assertIn("missing_expected_citations", stale["failures"])

    def test_document_scope_payload_only_on_first_turn_and_never_for_chat(self):
        import httpx
        import uuid as _uuid
        requests = []
        conversation = str(_uuid.uuid4())
        def handle(request):
            requests.append(json.loads(request.content))
            body = ('event: meta\ndata: ' + json.dumps({"conversation_id": conversation}) + '\n\n'
                    'event: delta\ndata: {"content":"ok"}\n\n'
                    'event: done\ndata: {"citations":[],"trace":{}}\n\n')
            return httpx.Response(200, text=body)
        scope = [str(_uuid.uuid4()), str(_uuid.uuid4())]
        case = {"id": "scope", "mode": "rag", "kb_id": str(_uuid.uuid4()), "turns": ["q1", "q2"], "document_ids": scope}
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            ev.run_live_case(client, case, base_url="https://example.test", token="secret", run_id=_uuid.uuid4())
        self.assertEqual(requests[0]["document_ids"], scope)
        self.assertNotIn("document_ids", requests[1])

    def test_dataset_validation_rejects_bad_document_scope(self):
        for extra in ({"document_ids": []}, {"document_ids": ["not-a-uuid"]},
                      {"document_ids": [str(__import__("uuid").uuid4())], "mode": "chat"},
                      {"document_ids": [str(__import__("uuid").uuid4())], "conversation_id": str(__import__("uuid").uuid4())}):
            with self.subTest(extra=extra), self.assertRaises(ev.EvaluationError):
                ev.validate_cases([{"id": "x", "question": "q", **extra}])


class CliBaselineTests(unittest.TestCase):
    def _run(self, *extra, dataset="cases.platform.example.jsonl", responses="responses.platform.example.jsonl"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        report = Path(temp.name) / "report.json"
        comparison = Path(temp.name) / "comparison.json"
        with contextlib.redirect_stdout(io.StringIO()) as output, patch("scripts.evaluate_rag.httpx.Client") as client:
            code = main(["--dataset", str(ROOT / dataset), "--responses", str(ROOT / responses),
                         "--output", str(report), "--comparison-output", str(comparison), *extra])
        client.assert_not_called()
        return code, report, comparison, output.getvalue()

    def test_offline_self_comparison_exits_zero_and_is_redacted(self):
        code, report, comparison, output = self._run("--baseline", str(ROOT / "baseline.platform.example.json"))
        self.assertEqual(code, 0)
        content = report.read_text(encoding="utf-8")
        self.assertNotIn('"content"', content)
        self.assertNotIn("提前3天", content)
        self.assertNotIn("重新登录", content)
        self.assertEqual(json.loads(comparison.read_text(encoding="utf-8"))["verdict"], "no_regression")
        self.assertIn("comparison", json.loads(content))
        self.assertNotIn("http://", output)

    def test_assertion_failure_exits_one_even_with_baseline(self):
        code, report, _, _ = self._run("--baseline", str(ROOT / "baseline.platform.example.json"),
                                       responses="responses.platform.regressed.example.jsonl")
        self.assertEqual(code, 1)
        body = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(body["passed"], 6)
        self.assertEqual(body["comparison"]["verdict"], "regression")

    def test_latency_regression_with_passing_assertions_exits_three(self):
        code, report, comparison, _ = self._run("--baseline", str(ROOT / "baseline.platform.example.json"),
                                                "--thresholds", str(ROOT / "thresholds.example.json"),
                                                responses="responses.platform.slow.example.jsonl")
        self.assertEqual(code, 3)
        verdict = json.loads(comparison.read_text(encoding="utf-8"))
        self.assertEqual(verdict["verdict"], "regression")
        self.assertIn("first_token_ms_p95", verdict["regressions"])

    def test_missing_gated_metric_exits_five(self):
        code, _, comparison, _ = self._run("--baseline", str(ROOT / "baseline.platform.example.json"),
                                           "--thresholds", str(ROOT / "thresholds.example.json"),
                                           responses="responses.platform.missing-metrics.example.jsonl")
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(comparison.read_text(encoding="utf-8"))["verdict"], "unknown")

    def test_different_dataset_and_legacy_baseline_exit_four_without_crash(self):
        code, report, comparison, _ = self._run("--baseline", str(ROOT / "baseline.platform.example.json"),
                                                dataset="cases.example.jsonl", responses="responses.example.jsonl")
        self.assertEqual(code, 4)
        self.assertIn("dataset_changed", json.loads(comparison.read_text(encoding="utf-8"))["reasons"])
        with tempfile.TemporaryDirectory() as temp:
            legacy = Path(temp) / "legacy.json"
            body = json.loads((ROOT / "baseline.platform.example.json").read_text(encoding="utf-8"))
            body.pop("identity", None)
            legacy.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            code, report, comparison, _ = self._run("--baseline", str(legacy))
            self.assertEqual(code, 4)
            verdict = json.loads(comparison.read_text(encoding="utf-8"))
            self.assertIn("baseline_identity_missing", verdict["reasons"])
            self.assertEqual(verdict["status"], "incomparable")
            self.assertNotEqual(verdict["verdict"], "no_regression")
            self.assertNotEqual(json.loads(report.read_text(encoding="utf-8"))["comparison"]["verdict"], "no_regression")

    def test_configuration_errors_still_exit_two(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "thresholds.json"
            bad.write_text(json.dumps({"not_a_metric": {"max_absolute_change": 0.1}}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                             "--responses", str(ROOT / "responses.platform.example.jsonl"),
                             "--thresholds", str(bad), "--output", str(Path(temp) / "r.json")])
            self.assertEqual(code, 2)

    def test_live_cli_never_reaches_network_without_tokens(self):
        with patch("scripts.evaluate_rag.httpx.Client") as client, \
             contextlib.redirect_stdout(io.StringIO()) as output, \
             patch.dict(os.environ, {"RAG_EVAL_TOKEN": ""}):
            code = main(["--live", "--dataset", str(ROOT / "cases.platform.example.jsonl"),
                         "--base-url", "http://127.0.0.1:8000", "--kb-id", str(__import__("uuid").uuid4()),
                         "--output", "evaluation-results/never.json"])
        self.assertEqual(code, 2)
        client.assert_not_called()
        self.assertNotIn("Traceback", output.getvalue())

    def test_live_cli_rejects_bad_config_and_baseline_before_any_http(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            bad_configs = {
                "overlap": {"chunk_size": 800, "chunk_overlap": 800},
                "url": {"model": "https://secret.example/model"},
                "type": {"retrieval_top_k": "6"},
            }
            for name, config in bad_configs.items():
                with self.subTest(name=name):
                    path = temp_path / f"{name}.json"
                    path.write_text(json.dumps(config), encoding="utf-8")
                    with patch("scripts.evaluate_rag.httpx.Client") as client, \
                         contextlib.redirect_stdout(io.StringIO()) as output, \
                         patch.dict(os.environ, {"RAG_EVAL_TOKEN": "token-value"}):
                        code = main(["--live", "--dataset", str(ROOT / "cases.platform.example.jsonl"),
                                     "--base-url", "http://127.0.0.1:8000",
                                     "--kb-id", str(__import__("uuid").uuid4()),
                                     "--config-file", str(path), "--output", str(temp_path / "r.json")])
                    self.assertEqual(code, 2)
                    client.assert_not_called()
                    self.assertNotIn("Traceback", output.getvalue())
                    self.assertNotIn("secret.example", output.getvalue())
            bad_baseline = temp_path / "baseline.json"
            bad_baseline.write_text(json.dumps({"cases": "eight", "results": []}), encoding="utf-8")
            with patch("scripts.evaluate_rag.httpx.Client") as client, \
                 contextlib.redirect_stdout(io.StringIO()) as output, \
                 patch.dict(os.environ, {"RAG_EVAL_TOKEN": "token-value"}):
                code = main(["--live", "--dataset", str(ROOT / "cases.platform.example.jsonl"),
                             "--base-url", "http://127.0.0.1:8000",
                             "--kb-id", str(__import__("uuid").uuid4()),
                             "--baseline", str(bad_baseline), "--output", str(temp_path / "r.json")])
            self.assertEqual(code, 2)
            client.assert_not_called()
            self.assertNotIn("Traceback", output.getvalue())

    def test_live_cli_rejects_nested_mode_and_huge_integer_baseline_before_http(self):
        original = json.loads((ROOT / "baseline.platform.example.json").read_text(encoding="utf-8"))
        cases = {
            "nested_mode": lambda body: body["identity"]["config"].__setitem__("mode", []),
            "huge_integer": lambda body: body.__setitem__("total_ms_mean", 10 ** 400),
            "huge_threshold": None,
        }
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            for name, mutate in cases.items():
                with self.subTest(name=name):
                    if mutate is None:
                        baseline = ROOT / "baseline.platform.example.json"
                        thresholds = temp_path / "huge-thresholds.json"
                        thresholds.write_text(json.dumps({"total_ms_p95": {"max_absolute_change": 10 ** 400}}),
                                              encoding="utf-8")
                    else:
                        body = json.loads(json.dumps(original))
                        mutate(body)
                        baseline = temp_path / f"{name}.json"
                        baseline.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
                        thresholds = None
                    extra = ["--baseline", str(baseline)]
                    if thresholds is not None:
                        extra += ["--thresholds", str(thresholds)]
                    with patch("scripts.evaluate_rag.httpx.Client") as client, \
                         contextlib.redirect_stdout(io.StringIO()) as output, \
                         patch.dict(os.environ, {"RAG_EVAL_TOKEN": "token-value"}):
                        code = main(["--live", "--dataset", str(ROOT / "cases.platform.example.jsonl"),
                                     "--base-url", "http://127.0.0.1:8000",
                                     "--kb-id", str(__import__("uuid").uuid4()),
                                     "--output", str(temp_path / "r.json"), *extra])
                    self.assertEqual(code, 2)
                    client.assert_not_called()
                    self.assertNotIn("Traceback", output.getvalue())

    def test_bad_baseline_file_exits_two_without_traceback(self):
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "baseline.json"
            bad.write_text(json.dumps({"assertion_pass_rate": "oops", "cases": 1, "results": [{}]}), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = main(["--dataset", str(ROOT / "cases.platform.example.jsonl"),
                             "--responses", str(ROOT / "responses.platform.example.jsonl"),
                             "--baseline", str(bad), "--output", str(Path(temp) / "r.json")])
            self.assertEqual(code, 2)
            self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
