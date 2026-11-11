"""Q 排序指标：MRR、前 K 相关率、覆盖 cohort、阈值比较与离线 CLI。

全部使用合成 case/response 与临时目录；不访问数据库、模型或网络。
"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import evaluation as ev
from scripts.evaluate_rag import main

ROOT = Path(__file__).parents[1] / "evaluation"
RUN_ID = "00000000-0000-4000-8000-000000000020"
POSITIVE = {"filename": "doc.md", "chunk_index": 0}
OTHER = {"filename": "other.md", "chunk_index": 0}


def _source(filename="doc.md", chunk_index=0, **extra):
    return {"filename": filename, "chunk_index": chunk_index, **extra}


def _case(case_id="a", **overrides):
    case = {"id": case_id, "question": "合成问题", "expected_outcome": "answer",
            "expected_sources": [dict(POSITIVE)], "top_k": 2,
            "source_labels_complete": True, "required_phrases": []}
    case.update(overrides)
    return case


def _response(candidates=None, supplied=None, citations=None, answer="合成回答",
              trace=None, include_trace=True, http_status=None):
    response = {"answer": answer, "citations": citations if citations is not None else [dict(POSITIVE)]}
    if http_status is not None:
        response["http_status"] = http_status
    if trace is not None:
        response["trace"] = trace
    elif include_trace:
        response["trace"] = {"candidate_sources": candidates if candidates is not None else [],
                             "supplied_sources": supplied if supplied is not None else []}
    return response


def _report(cases, responses, *, run_id=RUN_ID, mode="offline"):
    results = [ev.grade_case(case, responses.get(case["id"], {"error": "missing_response"}))
               for case in cases]
    identity = ev.build_identity(cases, mode=mode, documents_dir=ROOT / "documents")
    return ev.make_report(results, run_id, identity=identity)


def _comparison(baseline, current, thresholds=None):
    return ev.compare_reports(baseline, current, thresholds)


class SourceLabelDeclarationTests(unittest.TestCase):
    def test_strict_bool_and_no_default_injection(self):
        legacy = {"id": "legacy", "question": "q"}
        validated = ev.validate_cases([legacy])
        self.assertIs(validated[0], legacy)
        self.assertNotIn("source_labels_complete", validated[0])
        for value in ("true", 1, 0, None, []):
            with self.subTest(value=value), self.assertRaises(ev.EvaluationError):
                ev.validate_cases([{"id": "x", "question": "q", "source_labels_complete": value}])
        for value in (True, False):
            with self.subTest(value=value):
                self.assertIs(ev.validate_cases(
                    [{"id": "x", "question": "q", "source_labels_complete": value}])[0]
                    ["source_labels_complete"], value)

    def test_legacy_dataset_identity_digest_is_stable(self):
        self.assertEqual(ev.dataset_identity([{"id": "legacy", "question": "q"}])["sha256"],
                         "d4bb90e0e9935b2314618a513487c00b7df43a253b16e596e137f0777f6ae5d4")

    def test_declaration_changes_dataset_identity(self):
        legacy = _case(case_id="legacy")
        legacy.pop("source_labels_complete")
        declared = _case(case_id="legacy")
        self.assertNotEqual(ev.dataset_identity([legacy])["sha256"],
                            ev.dataset_identity([declared])["sha256"])


class MrrTests(unittest.TestCase):
    def test_first_known_positive_uses_original_trace_order(self):
        case = _case()
        result = ev.grade_case(case, _response(
            candidates=[{**OTHER, "score": 0.99}, dict(POSITIVE)],
            supplied=[dict(POSITIVE), dict(OTHER)]))
        self.assertEqual(result["candidate_source_mrr"], 0.5)
        self.assertEqual(result["supplied_source_mrr"], 1.0)

    def test_missing_trace_is_null_empty_trace_is_zero_and_no_labels_is_null(self):
        case = _case()
        self.assertIsNone(ev.grade_case(case, _response(include_trace=False))["candidate_source_mrr"])
        self.assertIsNone(ev.grade_case(case, {"answer": "ok", "trace": {
            "supplied_sources": [dict(POSITIVE)]}})["candidate_source_mrr"])
        empty = _case(case_id="empty")
        result = ev.grade_case(empty, {"answer": "ok", "trace": {
            "candidate_sources": [], "supplied_sources": []}})
        self.assertEqual(result["candidate_source_mrr"], 0.0)
        self.assertEqual(result["supplied_source_mrr"], 0.0)
        no_labels = _case(case_id="none", expected_sources=[])
        self.assertIsNone(ev.grade_case(no_labels, _response())["candidate_source_mrr"])

    def test_chat_and_expected_http_negative_are_not_in_denominator(self):
        chat = _case(case_id="chat", mode="chat")
        result = ev.grade_case(chat, _response(candidates=[dict(POSITIVE)]))
        self.assertFalse(result["source_ranking_eligible"])
        self.assertIsNone(result["candidate_source_mrr"])
        negative = _case(case_id="negative", expected_http_status=403)
        denied = ev.grade_case(negative, _response(http_status=403, include_trace=False))
        self.assertFalse(denied["source_ranking_eligible"])
        self.assertIsNone(denied["candidate_source_mrr"])

    def test_chunk_range_and_index_version_matching_are_reused(self):
        case = _case(expected_sources=[{"filename": "doc.md", "chunk_index": 2}])
        merged = {"filename": "doc.md", "chunk_start": 1, "chunk_end": 3}
        result = ev.grade_case(case, _response(candidates=[merged], supplied=[merged]))
        self.assertEqual(result["candidate_source_mrr"], 1.0)
        stale = _case(case_id="stale", expected_sources=[
            {"filename": "doc.md", "chunk_index": 2, "index_version": 3}])
        wrong_generation = {"filename": "doc.md", "chunk_index": 2, "index_version": 1}
        self.assertEqual(ev.grade_case(stale, _response(
            candidates=[wrong_generation], supplied=[wrong_generation]))["candidate_source_mrr"], 0.0)


class PrecisionAtKTests(unittest.TestCase):
    def test_requires_complete_source_declaration(self):
        missing = _case(case_id="missing")
        missing.pop("source_labels_complete")
        self.assertIsNone(ev.grade_case(missing, _response())["candidate_source_precision_at_k"])
        incomplete = _case(case_id="incomplete", source_labels_complete=False)
        self.assertIsNone(ev.grade_case(incomplete, _response())["candidate_source_precision_at_k"])

    def test_fixed_denominator_and_top_k(self):
        case = _case(top_k=4)
        result = ev.grade_case(case, _response(candidates=[dict(POSITIVE), dict(OTHER)]))
        self.assertEqual(result["candidate_source_precision_at_k"], 0.25)
        self.assertEqual(ev.grade_case(case, _response(candidates=[]))["candidate_source_precision_at_k"], 0.0)
        long_list = [dict(POSITIVE), dict(POSITIVE), dict(POSITIVE), dict(POSITIVE), dict(OTHER)]
        self.assertEqual(ev.grade_case(case, _response(candidates=long_list))["candidate_source_precision_at_k"], 1.0)

    def test_default_top_k_is_six(self):
        case = _case()
        case.pop("top_k")
        result = ev.grade_case(case, _response(candidates=[dict(POSITIVE)]))
        self.assertEqual(result["top_k"], 6)
        self.assertAlmostEqual(result["candidate_source_precision_at_k"], 1 / 6)

    def test_complete_empty_expected_sources_means_zero_relevant(self):
        case = _case(expected_sources=[])
        result = ev.grade_case(case, _response(candidates=[dict(OTHER), dict(POSITIVE)]))
        self.assertEqual(result["candidate_source_precision_at_k"], 0.0)
        self.assertEqual(result["supplied_source_precision_at_k"], 0.0)
        self.assertIsNone(result["candidate_source_mrr"])

    def test_missing_trace_is_null_not_zero(self):
        case = _case()
        self.assertIsNone(ev.grade_case(case, _response(include_trace=False))["candidate_source_precision_at_k"])

    def test_misses_are_only_negatives_when_labels_are_complete(self):
        incomplete = _case(source_labels_complete=False)
        result = ev.grade_case(incomplete, _response(candidates=[dict(OTHER), dict(OTHER)]))
        self.assertIsNone(result["candidate_source_precision_at_k"])
        self.assertEqual(result["candidate_source_recall"], 0.0)


class CoverageAndReportTests(unittest.TestCase):
    def _mixed(self):
        cases = [
            _case("a"),
            _case("b"),
            _case("c", source_labels_complete=False),
            _case("d"),
            _case("e", mode="chat"),
        ]
        responses = {
            "a": _response(candidates=[dict(POSITIVE), dict(OTHER)], supplied=[dict(POSITIVE), dict(OTHER)]),
            "b": _response(candidates=[dict(OTHER), dict(OTHER), dict(POSITIVE)],
                           supplied=[dict(OTHER), dict(OTHER), dict(POSITIVE)]),
            "c": _response(candidates=[dict(POSITIVE)], supplied=[dict(POSITIVE)]),
            "d": _response(include_trace=False),
            "e": _response(candidates=[dict(POSITIVE)], supplied=[dict(POSITIVE)]),
        }
        return cases, responses

    def test_macro_average_and_stable_cohorts(self):
        cases, responses = self._mixed()
        report = _report(cases, responses)
        self.assertAlmostEqual(report["candidate_source_mrr"], (1 + 1 / 3 + 1) / 3)
        self.assertAlmostEqual(report["candidate_source_precision_at_k"], 0.25)
        mrr = report["ranking_coverage"]["candidate_source_mrr"]
        self.assertEqual((mrr["status"], mrr["eligible"], mrr["computable"]), ("partial", 4, 3))
        self.assertEqual(mrr["eligible_cohort"], ["a", "b", "c", "d"])
        self.assertEqual(mrr["cohort"], ["a", "b", "c"])
        self.assertEqual(mrr["unknown_cohort"], ["d"])
        precision = report["ranking_coverage"]["candidate_source_precision_at_k"]
        self.assertEqual((precision["eligible"], precision["computable"]), (3, 2))
        self.assertEqual(precision["eligible_cohort"], ["a", "b", "d"])
        self.assertEqual(precision["cohort"], ["a", "b"])
        for name in ev.RANKING_METRICS:
            self.assertNotIn("e", report["ranking_coverage"][name]["eligible_cohort"])

    def test_zero_eligible_is_distinct_from_unmeasured(self):
        chat_cases = [_case("chat", mode="chat")]
        chat_report = _report(chat_cases, {"chat": _response(include_trace=False)})
        empty = chat_report["ranking_coverage"]["candidate_source_mrr"]
        self.assertEqual((empty["status"], empty["eligible"], empty["computable"]),
                         ("no_eligible_cases", 0, 0))
        self.assertIsNone(chat_report["candidate_source_mrr"])

        missing = _report([_case("missing")], {"missing": _response(include_trace=False)})
        unmeasured = missing["ranking_coverage"]["candidate_source_mrr"]
        self.assertEqual((unmeasured["status"], unmeasured["eligible"], unmeasured["computable"]),
                         ("unmeasured", 1, 0))
        self.assertEqual(unmeasured["unknown_cohort"], ["missing"])
        self.assertIsNone(missing["candidate_source_mrr"])

    def test_null_is_never_filled_with_zero(self):
        report = _report([_case("missing")], {"missing": _response(include_trace=False)})
        self.assertIsNone(report["candidate_source_mrr"])
        self.assertIsNone(report["candidate_source_precision_at_k"])


class RankingComparisonTests(unittest.TestCase):
    def _pair(self):
        cases = [_case("a"), _case("b")]
        normal = {
            "a": _response(candidates=[dict(POSITIVE), dict(OTHER)], supplied=[dict(POSITIVE), dict(OTHER)]),
            "b": _response(candidates=[dict(OTHER), dict(POSITIVE)], supplied=[dict(OTHER), dict(POSITIVE)]),
        }
        regressed = {
            "a": _response(candidates=[dict(OTHER), dict(POSITIVE)], supplied=[dict(OTHER), dict(POSITIVE)]),
            "b": _response(candidates=[dict(OTHER), dict(OTHER), dict(POSITIVE)],
                           supplied=[dict(OTHER), dict(OTHER), dict(POSITIVE)]),
        }
        return cases, normal, regressed

    def test_order_regression_with_equal_recall_exits_three(self):
        cases, normal, regressed = self._pair()
        baseline, current = _report(cases, normal), _report(cases, regressed)
        self.assertEqual(baseline["candidate_source_recall"], current["candidate_source_recall"])
        self.assertEqual(baseline["supplied_source_recall"], current["supplied_source_recall"])
        rules = {name: {"max_absolute_change": 0.0} for name in ev.RANKING_METRICS}
        comparison = _comparison(baseline, current, rules)
        self.assertEqual(comparison["status"], "comparable")
        self.assertEqual(comparison["verdict"], "regression")
        for name in ev.RANKING_METRICS:
            self.assertEqual(comparison["metrics"][name]["status"], "regressed")
        self.assertEqual(comparison["metrics"]["candidate_source_recall"]["status"], "unchanged")
        self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 3)

    def test_same_counts_different_cohorts_are_unknown(self):
        cases, normal, _ = self._pair()
        baseline = _report(cases, normal)
        current = copy.deepcopy(baseline)
        for row, new_id in zip(current["results"], ("x", "y")):
            row["id"] = new_id
            row["machine_sha256"] = ev.machine_sha256(row)
        for name in ev.RANKING_METRICS:
            entry = current["ranking_coverage"][name]
            entry["eligible_cohort"] = ["x", "y"]
            entry["cohort"] = ["x", "y"]
            entry["unknown_cohort"] = []
        comparison = _comparison(baseline, current)
        for name in ev.RANKING_METRICS:
            self.assertEqual(comparison["metrics"][name]["status"], "unknown")
            self.assertEqual(comparison["metrics"][name]["detail"], "eligible_cohort_mismatch")
        self.assertEqual(comparison["verdict"], "unknown")
        self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 5)

    def test_missing_trace_expected_case_is_unknown(self):
        cases, normal, _ = self._pair()
        baseline = _report(cases, normal)
        current_responses = dict(normal)
        current_responses["b"] = _response(include_trace=False)
        current = _report(cases, current_responses)
        comparison = _comparison(baseline, current)
        metric = comparison["metrics"]["candidate_source_mrr"]
        self.assertEqual(metric["status"], "unknown")
        self.assertEqual(metric["detail"], "computable_cohort_mismatch")
        self.assertEqual(metric["current_coverage"]["unknown_cohort"], ["b"])
        self.assertEqual(comparison["verdict"], "unknown")
        self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 5)

    def test_shared_partial_coverage_is_still_unknown(self):
        cases, normal, regressed = self._pair()
        baseline_responses = dict(normal)
        current_responses = dict(regressed)
        for responses in (baseline_responses, current_responses):
            responses["b"] = _response(include_trace=False)
        baseline = _report(cases, baseline_responses)
        current = _report(cases, current_responses)
        comparison = _comparison(baseline, current)
        metric = comparison["metrics"]["candidate_source_mrr"]
        self.assertEqual(metric["status"], "unknown")
        self.assertEqual(metric["detail"], "unmeasured_cases")
        self.assertEqual(metric["baseline_coverage"]["cohort"], ["a"])
        self.assertEqual(metric["current_coverage"]["cohort"], ["a"])
        self.assertEqual(comparison["verdict"], "unknown")

    def test_zero_eligible_is_not_applicable_or_gated_unknown(self):
        cases = [_case("chat", mode="chat")]
        # 普通对话仍可报告来源召回，但排序质量分母明确为零。
        responses = {"chat": _response(candidates=[dict(POSITIVE)], supplied=[dict(POSITIVE)])}
        baseline, current = _report(cases, responses), _report(cases, responses)
        plain = _comparison(baseline, current)
        for name in ev.RANKING_METRICS:
            self.assertEqual(plain["metrics"][name]["status"], "not_applicable")
            self.assertEqual(plain["metrics"][name]["detail"], "no_eligible_cases")
        self.assertEqual(plain["verdict"], "no_regression")
        gated = _comparison(baseline, current, {"candidate_source_mrr": {"max_absolute_change": 0.0}})
        self.assertEqual(gated["metrics"]["candidate_source_mrr"]["status"], "unknown")
        self.assertEqual(gated["verdict"], "unknown")
        self.assertEqual(ev.comparison_exit_code(gated, assertions_passed=True), 5)

    def test_threshold_whitelist_accepts_ranking_metrics(self):
        rules = {name: {"max_absolute_change": 0.0} for name in ev.RANKING_METRICS}
        self.assertEqual(ev.validate_thresholds(rules), rules)
        with self.assertRaises(ev.EvaluationError):
            ev.validate_thresholds({"unknown_ranking": {"max_absolute_change": 0.0}})

    def test_coverage_summary_is_verified_against_results(self):
        cases, normal, _ = self._pair()
        baseline = _report(cases, normal)
        current = copy.deepcopy(baseline)
        entry = current["ranking_coverage"]["candidate_source_mrr"]
        entry.update(status="computed", eligible=1, computable=1,
                     eligible_cohort=["a"], cohort=["a"], unknown_cohort=[])
        comparison = _comparison(baseline, current)
        metric = comparison["metrics"]["candidate_source_mrr"]
        self.assertEqual(metric["status"], "unknown")
        self.assertEqual(metric["detail"], "coverage_summary_mismatch")
        self.assertEqual(comparison["verdict"], "unknown")

    def test_single_metric_summary_error_does_not_mask_other_regressions(self):
        cases, normal, regressed = self._pair()
        baseline = _report(cases, normal)
        current = _report(cases, regressed)
        # 只把 candidate_source_mrr 摘要改成自洽的一例，逐条结果仍完整正确。
        current["ranking_coverage"]["candidate_source_mrr"] = {
            "status": "computed", "eligible": 1, "computable": 1,
            "eligible_cohort": ["a"], "cohort": ["a"], "unknown_cohort": []}
        rules = {name: {"max_absolute_change": 0.0} for name in ev.RANKING_METRICS}
        comparison = _comparison(baseline, current, rules)
        self.assertEqual(comparison["metrics"]["candidate_source_mrr"]["status"], "unknown")
        self.assertEqual(comparison["metrics"]["candidate_source_mrr"]["detail"],
                         "coverage_summary_mismatch")
        for name in ("supplied_source_mrr", "candidate_source_precision_at_k",
                     "supplied_source_precision_at_k"):
            self.assertEqual(comparison["metrics"][name]["status"], "regressed", name)
        self.assertIn("supplied_source_mrr", comparison["regressions"])
        self.assertEqual(comparison["verdict"], "regression")
        self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 3)

    def test_computed_metrics_keep_coverage_on_every_status(self):
        cases, normal, regressed = self._pair()
        normal_report = _report(cases, normal)
        regressed_report = _report(cases, regressed)
        rules = {name: {"max_absolute_change": 0.0} for name in ev.RANKING_METRICS}
        pairs = (
            (_comparison(normal_report, normal_report, rules), "unchanged",
             normal_report, normal_report),
            (_comparison(normal_report, regressed_report, rules), "regressed",
             normal_report, regressed_report),
            (_comparison(regressed_report, normal_report, rules), "improved",
             regressed_report, normal_report),
            (_comparison(normal_report, regressed_report), "observed",
             normal_report, regressed_report),
        )
        for comparison, status, base_report, current_report in pairs:
            for name in ev.RANKING_METRICS:
                metric = comparison["metrics"][name]
                self.assertEqual(metric["status"], status, name)
                self.assertEqual(metric["baseline_coverage"],
                                 base_report["ranking_coverage"][name])
                self.assertEqual(metric["current_coverage"],
                                 current_report["ranking_coverage"][name])

    def test_global_marker_conflict_blocks_all_ranking_metrics_only(self):
        cases, normal, _ = self._pair()
        baseline = _report(cases, normal)
        current = copy.deepcopy(baseline)
        current["results"][0]["source_ranking_eligible"] = False
        comparison = _comparison(baseline, current)
        for name in ev.RANKING_METRICS:
            self.assertEqual(comparison["metrics"][name]["status"], "unknown")
            self.assertEqual(comparison["metrics"][name]["detail"], "ranking_marker_inconsistent")
        self.assertEqual(comparison["metrics"]["candidate_source_recall"]["status"], "unchanged")
        self.assertEqual(comparison["verdict"], "unknown")

    def test_malformed_coverage_and_values_raise(self):
        cases, normal, _ = self._pair()
        base = _report(cases, normal)
        mutations = [
            lambda report: report.update(candidate_source_mrr=1.5),
            lambda report: report.update(candidate_source_mrr=True),
            lambda report: report["results"][0].update(candidate_source_mrr=-0.1),
            lambda report: report["results"][0].update(candidate_source_mrr="0.5"),
            lambda report: report["ranking_coverage"]["candidate_source_mrr"].update(eligible=True),
            lambda report: report["ranking_coverage"]["candidate_source_mrr"].update(computable=-1),
            lambda report: report["ranking_coverage"]["candidate_source_mrr"].update(eligible=99),
            lambda report: report["ranking_coverage"]["candidate_source_mrr"].update(
                eligible_cohort=["a", "a"]),
            lambda report: report["results"][0].update(source_ranking_eligible="yes"),
            lambda report: report["results"][0].update(top_k=0),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                broken = copy.deepcopy(base)
                mutate(broken)
                with self.assertRaises(ev.EvaluationError):
                    ev.compare_reports(broken, base)
                with self.assertRaises(ev.EvaluationError):
                    ev.compare_reports(base, broken)

    def test_old_metrics_report_is_incomparable(self):
        cases, normal, _ = self._pair()
        current = _report(cases, normal)
        legacy = copy.deepcopy(current)
        legacy["identity"]["metrics_version"] = 1
        legacy.pop("ranking_coverage")
        for name in ev.RANKING_METRICS:
            legacy.pop(name)
        for row in legacy["results"]:
            for key in [*ev.RANKING_METRICS, "source_ranking_eligible", "source_label_count",
                        "source_labels_complete", "top_k", "mode"]:
                row.pop(key)
        comparison = _comparison(legacy, current)
        self.assertEqual(comparison["status"], "incomparable")
        self.assertIn("metrics_version_mismatch", comparison["reasons"])
        for name in ev.RANKING_METRICS:
            self.assertEqual(comparison["metrics"][name]["status"], "unknown")
        self.assertEqual(ev.comparison_exit_code(comparison, assertions_passed=True), 4)


class FingerprintInvarianceTests(unittest.TestCase):
    def test_trace_reorder_preserves_result_and_machine_fingerprints(self):
        case = _case(top_k=1)
        first = ev.grade_case(case, _response(candidates=[dict(POSITIVE), dict(OTHER)],
                                              supplied=[dict(POSITIVE), dict(OTHER)]))
        reordered = ev.grade_case(case, _response(candidates=[dict(OTHER), dict(POSITIVE)],
                                                  supplied=[dict(OTHER), dict(POSITIVE)]))
        self.assertEqual(first["result_sha256"], reordered["result_sha256"])
        self.assertEqual(first["machine_sha256"], reordered["machine_sha256"])
        self.assertEqual(first["candidate_source_recall"], reordered["candidate_source_recall"])
        self.assertGreater(first["candidate_source_mrr"], reordered["candidate_source_mrr"])
        self.assertGreater(first["candidate_source_precision_at_k"],
                           reordered["candidate_source_precision_at_k"])

    def test_shipped_samples_change_only_trace_order(self):
        cases = ev.validate_cases(ev.read_jsonl(ROOT / "cases.ranking.example.jsonl"))
        normal = {row["id"]: row for row in ev.validate_responses(
            ev.read_jsonl(ROOT / "responses.ranking.example.jsonl"))}
        regressed = {row["id"]: row for row in ev.validate_responses(
            ev.read_jsonl(ROOT / "responses.ranking.regressed.example.jsonl"))}
        decreased = False
        for case in cases:
            before = ev.grade_case(case, normal[case["id"]])
            after = ev.grade_case(case, regressed[case["id"]])
            self.assertEqual(before["result_sha256"], after["result_sha256"])
            self.assertEqual(before["machine_sha256"], after["machine_sha256"])
            self.assertEqual(before["candidate_source_recall"], after["candidate_source_recall"])
            self.assertEqual(before["supplied_source_recall"], after["supplied_source_recall"])
            decreased = decreased or after["candidate_source_mrr"] < before["candidate_source_mrr"]
        self.assertTrue(decreased)

    def test_versions_are_unchanged_except_metrics(self):
        self.assertEqual(ev.METRICS_VERSION, 2)
        self.assertEqual(ev.IDENTITY_SCHEMA, 2)
        self.assertEqual(ev.RESULT_FINGERPRINT_VERSION, 1)
        self.assertEqual(ev.MACHINE_FINGERPRINT_VERSION, 1)
        result = ev.grade_case(_case(), _response())
        self.assertEqual(result["result_fingerprint_version"], 1)
        self.assertEqual(result["machine_fingerprint_version"], 1)


def _lf_documents_copy(temp):
    target = Path(temp) / "documents-lf"
    for source in sorted((ROOT / "documents").rglob("*")):
        if not source.is_file():
            continue
        destination = target / source.relative_to(ROOT / "documents")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    return target


class RankingCliTests(unittest.TestCase):
    def _run(self, *extra, dataset="cases.ranking.example.jsonl",
             responses="responses.ranking.example.jsonl"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        documents = _lf_documents_copy(temp.name)
        report = Path(temp.name) / "report.json"
        comparison = Path(temp.name) / "comparison.json"
        with contextlib.redirect_stdout(io.StringIO()) as output, \
                patch("scripts.evaluate_rag.httpx.Client") as client:
            code = main(["--dataset", str(ROOT / dataset), "--responses", str(ROOT / responses),
                         "--documents-dir", str(documents), "--output", str(report),
                         "--comparison-output", str(comparison), *extra])
        client.assert_not_called()
        return code, report, comparison, output.getvalue()

    def test_ranking_normal_exits_zero(self):
        code, _, comparison, _ = self._run(
            "--baseline", str(ROOT / "baseline.ranking.example.json"),
            "--thresholds", str(ROOT / "thresholds.ranking.example.json"))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(comparison.read_text(encoding="utf-8"))["verdict"], "no_regression")

    def test_ranking_order_regression_exits_three(self):
        code, _, comparison, _ = self._run(
            "--baseline", str(ROOT / "baseline.ranking.example.json"),
            "--thresholds", str(ROOT / "thresholds.ranking.example.json"),
            responses="responses.ranking.regressed.example.jsonl")
        self.assertEqual(code, 3)
        body = json.loads(comparison.read_text(encoding="utf-8"))
        self.assertEqual(body["verdict"], "regression")
        for name in ev.RANKING_METRICS:
            self.assertIn(name, body["regressions"])

    def test_ranking_missing_trace_exits_five(self):
        code, _, comparison, _ = self._run(
            "--baseline", str(ROOT / "baseline.ranking.example.json"),
            responses="responses.ranking.missing-trace.example.jsonl")
        self.assertEqual(code, 5)
        body = json.loads(comparison.read_text(encoding="utf-8"))
        self.assertEqual(body["verdict"], "unknown")
        self.assertEqual(body["metrics"]["candidate_source_mrr"]["detail"], "computable_cohort_mismatch")

    def test_old_metrics_baseline_exits_four(self):
        baseline = json.loads((ROOT / "baseline.ranking.example.json").read_text(encoding="utf-8"))
        baseline["identity"]["metrics_version"] = 1
        baseline.pop("ranking_coverage")
        for name in ev.RANKING_METRICS:
            baseline.pop(name)
        for row in baseline["results"]:
            for key in [*ev.RANKING_METRICS, "source_ranking_eligible", "source_label_count",
                        "source_labels_complete", "top_k", "mode"]:
                row.pop(key)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "legacy.json"
            path.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")
            code, _, comparison, _ = self._run("--baseline", str(path))
        self.assertEqual(code, 4)
        self.assertIn("metrics_version_mismatch",
                      json.loads(comparison.read_text(encoding="utf-8"))["reasons"])

    def test_platform_normal_regressed_and_missing_metrics(self):
        cases = ("cases.platform.example.jsonl", "responses.platform.example.jsonl")
        normal = self._run("--baseline", str(ROOT / "baseline.platform.example.json"),
                           dataset=cases[0], responses=cases[1])
        self.assertEqual(normal[0], 0)
        regressed = self._run("--baseline", str(ROOT / "baseline.platform.example.json"),
                              dataset=cases[0], responses="responses.platform.regressed.example.jsonl")
        self.assertEqual(regressed[0], 1)
        missing = self._run("--baseline", str(ROOT / "baseline.platform.example.json"),
                            "--thresholds", str(ROOT / "thresholds.example.json"),
                            dataset=cases[0], responses="responses.platform.missing-metrics.example.jsonl")
        self.assertEqual(missing[0], 5)


if __name__ == "__main__":
    unittest.main()