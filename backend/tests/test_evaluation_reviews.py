"""业务结果人工核验：结果指纹、模板、绑定、fail-closed 与无正文输出。"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import evaluation as ev
from app.services import evaluation_reviews as er
from scripts import review_evaluation
from scripts.evaluate_rag import main as evaluate_main

ROOT = Path(__file__).parents[1] / "evaluation"
RUN_ID = "00000000-0000-4000-8000-000000000042"


def _load_cases(name="cases.example.jsonl"):
    return ev.validate_cases(ev.read_jsonl(ROOT / name))


def _load_responses(name="responses.example.jsonl"):
    return {row["id"]: row for row in ev.validate_responses(ev.read_jsonl(ROOT / name))}


def _build_report(cases=None, responses=None, *, include_content=False, run_id=RUN_ID, mode="offline"):
    cases = _load_cases() if cases is None else cases
    responses = _load_responses() if responses is None else responses
    results = [ev.grade_case(case, responses.get(case["id"], {"error": "missing_response"}),
                             include_content=include_content) for case in cases]
    identity = ev.build_identity(cases, mode=mode, documents_dir=None)
    report = ev.make_report(results, run_id, identity=identity)
    report["mode"] = mode
    return report


def _filled_reviews(report, template=None):
    template = template or er.create_template(report)
    document = json.loads(json.dumps(template))
    results = {row["id"]: row for row in report["results"]}
    for review in document["reviews"]:
        result = results[review["id"]]
        if result.get("expected_http_status") is not None:
            values = (None, None, None)
        elif result.get("expected_outcome") in {"refuse", "clarify"}:
            values = (None, None, True)
        else:
            values = (True, True, True)
        review["answer_correct"], review["citation_supported"], review["outcome_correct"] = values
        review["reviewer"] = "rev-1"
        review["reviewed_at"] = "2026-11-06"
    return document


def _write(path, payload):
    path = Path(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_escaped(path, payload):
    """把代理对/孤立代理写成 \\uXXXX 转义，模拟外部 JSON 文件。"""
    path = Path(path)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return path


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_covers_answer_citations_generation_status_not_timing(self):
        case = {"id": "fp", "question": "q", "expected_sources": [{"filename": "doc.md", "chunk_index": 2}]}
        base = {"answer": "完整答案", "citations": [
            {"filename": "doc.md", "chunk_index": 2, "index_version": 3, "snippet": "原文片段", "score": 0.9}],
            "http_status": 200, "total_ms": 10, "first_token_ms": 2}
        first = ev.grade_case(case, dict(base))
        second = ev.grade_case(case, dict(base))
        self.assertEqual(first["result_sha256"], second["result_sha256"])
        self.assertEqual(first["result_fingerprint_version"], 1)
        self.assertEqual(first["machine_sha256"], second["machine_sha256"])
        self.assertEqual(first["machine_fingerprint_version"], 1)
        self.assertEqual(first["machine_sha256"], ev.machine_sha256(first))
        self.assertEqual((first["citation_count"], first["expected_http_status"], first["http_status"]), (1, None, 200))
        variants = {
            "answer": {**base, "answer": "被改动的答案"},
            "snippet": {**base, "citations": [{**base["citations"][0], "snippet": "篡改片段"}]},
            "index_version": {**base, "citations": [{**base["citations"][0], "index_version": 4}]},
            "http_status": {**base, "http_status": 500, "answer": ""},
        }
        for name, response in variants.items():
            with self.subTest(name=name):
                self.assertNotEqual(first["result_sha256"], ev.grade_case(case, response)["result_sha256"])
        timing_only = {**base, "total_ms": 999, "first_token_ms": 888,
                       "citations": [{**base["citations"][0], "score": 0.1}],
                       "trace": {"rewrite_ms": 10, "retrieve_ms": 20, "generation_ms": 30}}
        timed = ev.grade_case(case, timing_only)
        self.assertEqual(first["result_sha256"], timed["result_sha256"])
        self.assertEqual(first["machine_sha256"], timed["machine_sha256"])
        with_content = ev.grade_case(case, base, include_content=True)
        self.assertEqual(first["machine_sha256"], with_content["machine_sha256"])

    def test_default_report_has_fingerprint_without_content(self):
        report = _build_report()
        self.assertNotIn("content", json.dumps(report["results"], ensure_ascii=False))
        self.assertTrue(all(len(row["result_sha256"]) == 64 for row in report["results"]))
        self.assertTrue(all(row["machine_fingerprint_version"] == 1
                            and row["machine_sha256"] == ev.machine_sha256(row) for row in report["results"]))
        self.assertNotIn("提前3天", json.dumps(report, ensure_ascii=False))

    def test_machine_fingerprint_binds_context_without_changing_result_v1(self):
        case = {"id": "fp", "question": "q"}
        response = {"answer": "ok"}
        first = ev.grade_case(case, response)
        for change in ({"id": "other-case"}, {"expected_outcome": "refuse"}, {"expected_http_status": 403}):
            with self.subTest(change=change):
                other = ev.grade_case({**case, **change}, response)
                self.assertEqual(first["result_sha256"], other["result_sha256"])
                self.assertNotEqual(first["machine_sha256"], other["machine_sha256"])
        other_answer = ev.grade_case(case, {"answer": "different"})
        self.assertNotEqual(first["result_sha256"], other_answer["result_sha256"])
        self.assertNotEqual(first["machine_sha256"], other_answer["machine_sha256"])

    def test_machine_fingerprint_canonicalizes_key_order_and_absent_error_type(self):
        result = ev.grade_case({"id": "fp", "question": "q"}, {"answer": "ok"})
        self.assertNotIn("error_type", result)
        self.assertEqual(result["machine_sha256"], ev.machine_sha256({**result, "error_type": None}))
        self.assertEqual(result["machine_sha256"], ev.machine_sha256(dict(reversed(list(result.items())))))
        for version in (None, True, 1.0, "1", 0, 2):
            with self.subTest(version=version), self.assertRaises(ev.EvaluationError):
                ev.machine_fingerprint_payload(result, version=version)

    def test_changed_http_status_changes_fingerprint(self):
        case = {"id": "status", "question": "q", "expected_http_status": 403}
        denied = ev.grade_case(case, {"http_status": 403, "error": "http_error"})
        unauthorized = ev.grade_case(case, {"http_status": 401, "error": "http_error"})
        self.assertNotEqual(denied["result_sha256"], unauthorized["result_sha256"])
        self.assertNotEqual(denied["machine_sha256"], unauthorized["machine_sha256"])
        self.assertEqual(denied["machine_sha256"], ev.machine_sha256(denied))
        self.assertEqual(denied["error_type"], "http_error")
        self.assertEqual((denied["expected_http_status"], denied["http_status"]), (403, 403))


class TemplateTests(unittest.TestCase):
    def test_template_keeps_run_and_identity_and_never_marks_pass(self):
        report = _build_report()
        template = er.create_template(report)
        self.assertEqual(template["schema"], er.SCHEMA)
        self.assertEqual(template["run_id"], RUN_ID)
        self.assertEqual(template["identity_sha256"], er.identity_sha256(report["identity"]))
        self.assertEqual(len(template["reviews"]), report["cases"])
        for review in template["reviews"]:
            self.assertIsNone(review["answer_correct"])
            self.assertIsNone(review["citation_supported"])
            self.assertIsNone(review["outcome_correct"])
            self.assertIsNone(review["reviewer"])
            self.assertIsNone(review["reviewed_at"])
            self.assertEqual(review["unknowns"], [])
            self.assertNotIn("content", review)
        self.assertNotIn("提前3天", json.dumps(template, ensure_ascii=False))

    def test_content_is_only_attached_from_an_include_content_report(self):
        with self.assertRaises(er.ReviewError) as error:
            er.create_template(_build_report(), include_content=True)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_content_missing", 2))
        report = _build_report(include_content=True)
        template = er.create_template(report, include_content=True)
        results = {row["id"]: row for row in report["results"]}
        for review in template["reviews"]:
            self.assertEqual(review["content"], results[review["id"]]["content"])
        self.assertIn("answer", template["reviews"][0]["content"])


class ReviewBindingTests(unittest.TestCase):
    def test_valid_reviews_are_called_reviewed_pass_not_accuracy(self):
        report = _build_report()
        document = _filled_reviews(report)
        summary = er.evaluate_reviews(report, document)
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("reviewed_pass", 0))
        self.assertEqual(summary["boundary"]["declaration"], "human_declared_not_automatic_semantic_proof")
        self.assertFalse(summary["boundary"]["real_business_acceptance"])
        self.assertFalse(summary["boundary"]["production_acceptance"])
        self.assertEqual(summary["human_review"]["answer_correct"]["passed"], 3)
        self.assertEqual(summary["human_review"]["answer_correct"]["not_applicable"], 3)
        self.assertEqual(summary["human_review"]["outcome_correct"]["passed"], 6)
        self.assertEqual(summary["annotation_review_state"]["unreviewed"], 6)
        self.assertIn("annotation_review_state", summary["notes"][1])

    def test_empty_template_is_unknown_and_never_counts_as_correct(self):
        report = _build_report()
        summary = er.evaluate_reviews(report, er.create_template(report))
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("unknown", 5))
        human = summary["human_review"]
        self.assertEqual(human["cases_verified"], 0)
        self.assertEqual(human["cases_unverified"], report["cases"])
        self.assertEqual(human["answer_correct"]["passed"], 0)
        self.assertGreater(human["answer_correct"]["unverified"], 0)
        self.assertEqual(summary["machine_assertions"]["failed"], 0)

    def test_machine_assertion_failure_cannot_be_overridden_by_manual_true(self):
        cases = _load_cases("cases.platform.example.jsonl")
        responses = _load_responses("responses.platform.regressed.example.jsonl")
        report = _build_report(cases, responses)
        summary = er.evaluate_reviews(report, _filled_reviews(report))
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("failed", 1))
        self.assertGreater(summary["machine_assertions"]["failed"], 0)
        self.assertIn("machine_assertion_failed", summary["issue_counts"])

    def test_refusal_with_citations_fails_even_with_manual_true(self):
        case = {"id": "refuse", "question": "q", "expected_outcome": "refuse"}
        response = {"answer": "根据现有知识库无法回答", "citations": [{"filename": "other.md", "chunk_index": 0}]}
        report = _build_report([case], {case["id"]: response})
        document = _filled_reviews(report)
        summary = er.evaluate_reviews(report, document)
        self.assertEqual(summary["review_status"], "failed")
        self.assertIn("refusal_with_citations", summary["issue_counts"])
        self.assertTrue(report["results"][0]["passed"])
        self.assertNotIn("content", report["results"][0])
        tampered = json.loads(json.dumps(report))
        tampered["results"][0]["citation_count"] = 0
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(tampered, document)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_result_tampered", 4))

    def test_http_negative_uses_exact_machine_status_and_semantic_na(self):
        cases = _load_cases("cases.boundaries.example.jsonl")
        responses = _load_responses("responses.boundaries.example.jsonl")
        report = _build_report(cases, responses)
        summary = er.evaluate_reviews(report, _filled_reviews(report))
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("reviewed_pass", 0))
        self.assertEqual(summary["human_review"]["cases_not_applicable"], 1)
        self.assertGreaterEqual(summary["human_review"]["answer_correct"]["not_applicable"], 2)
        changed = {key: dict(value) for key, value in responses.items()}
        changed["private-kb-denied"] = {"http_status": 401, "error": "http_error"}
        tampered = _build_report(cases, changed)
        failed = er.evaluate_reviews(tampered, _filled_reviews(tampered))
        self.assertEqual((failed["review_status"], failed["exit_code"]), ("failed", 1))


class FailClosedTests(unittest.TestCase):
    def test_content_free_machine_fields_and_context_cannot_be_tampered(self):
        report = _build_report()
        document = _filled_reviews(report)
        target = report["results"][0]
        mutations = {"passed": not target["passed"], "failures": ["request_failed"],
                     "outcome": "refuse", "expected_outcome": "refuse", "expected_http_status": 403,
                     "http_status": 403, "error_type": "http_error", "citation_count": target["citation_count"] + 1,
                     "id": "rebound-case", "result_sha256": "0" * 64, "machine_sha256": "0" * 64}
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = json.loads(json.dumps(report))
                changed["results"][0][field] = value
                self.assertNotIn("content", changed["results"][0])
                if field != "result_sha256":
                    self.assertEqual(target["result_sha256"], changed["results"][0]["result_sha256"])
                with self.assertRaises(er.ReviewError) as error:
                    er.evaluate_reviews(changed, document)
                self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_result_tampered", 4))

    def test_failing_content_free_report_cannot_pass_by_flipping_passed(self):
        case = {"id": "failed", "question": "q", "expected_outcome": "answer", "required_phrases": ["required"]}
        report = _build_report([case], {case["id"]: {"answer": "different"}})
        document = _filled_reviews(report)
        self.assertFalse(report["results"][0]["passed"])
        self.assertEqual(er.evaluate_reviews(report, document)["exit_code"], 1)
        self.assertTrue(all(document["reviews"][0][key] is True for key in er._JUDGMENTS))
        for clear_failures in (False, True):
            with self.subTest(clear_failures=clear_failures):
                changed = json.loads(json.dumps(report))
                changed["results"][0]["passed"] = True
                if clear_failures:
                    changed["results"][0]["failures"] = []
                changed["passed"] = 1
                changed["assertion_pass_rate"] = 1.0
                self.assertNotIn("content", changed["results"][0])
                self.assertEqual(report["results"][0]["result_sha256"], changed["results"][0]["result_sha256"])
                with self.assertRaises(er.ReviewError) as error:
                    er.evaluate_reviews(changed, document)
                self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_result_tampered", 4))

    def test_missing_or_invalid_machine_digest_and_version_are_rejected(self):
        report = _build_report()
        document = _filled_reviews(report)
        missing = object()
        invalid_values = {
            "machine_fingerprint_version": (missing, None, True, False, "1", 1.0, 0, 2, [], {}),
            "machine_sha256": (missing, None, True, 1, "", "not-a-digest", "a" * 63, "a" * 65, "A" * 64, [], {}),
        }
        for field, values in invalid_values.items():
            for value in values:
                with self.subTest(field=field, value="missing" if value is missing else value):
                    changed = json.loads(json.dumps(report))
                    if value is missing:
                        changed["results"][0].pop(field)
                    else:
                        changed["results"][0][field] = value
                    for check in (er.create_template, lambda report: er.evaluate_reviews(report, document)):
                        with self.assertRaises(er.ReviewError) as error:
                            check(changed)
                        self.assertEqual((error.exception.reason, error.exception.exit_code),
                                         ("report_result_fingerprint_missing", 4))

    def test_content_free_timing_changes_and_null_error_type_preserve_reviews(self):
        report = _build_report()
        document = _filled_reviews(report)
        changed = json.loads(json.dumps(report))
        for result in changed["results"]:
            self.assertNotIn("content", result)
            result.update(total_ms=999, first_token_ms=888, rewrite_ms=10, retrieve_ms=20, generation_ms=30)
            if "error_type" not in result:
                result["error_type"] = None
        self.assertEqual([row["machine_sha256"] for row in report["results"]],
                         [ev.machine_sha256(row) for row in changed["results"]])
        summary = er.evaluate_reviews(changed, document)
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("reviewed_pass", 0))
        self.assertEqual(report["result_fingerprint_version"], 1)

    def test_answer_citation_or_status_change_invalidates_old_review(self):
        report = _build_report(include_content=True)
        document = _filled_reviews(report)
        changed = json.loads(json.dumps(report))
        changed["results"][0]["content"]["answer"] += "篡改"
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(changed, document)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_result_tampered", 4))

        changed = json.loads(json.dumps(report))
        changed["results"][0]["content"]["citations"][0]["snippet"] = "篡改片段"
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(changed, document)
        self.assertEqual(error.exception.reason, "report_result_tampered")

        changed = json.loads(json.dumps(report))
        changed["results"][0]["content"]["citations"][0]["index_version"] = 999
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(changed, document)
        self.assertEqual(error.exception.reason, "report_result_tampered")

        changed = json.loads(json.dumps(report))
        changed["results"][0]["content"].pop("citations")
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(changed, document)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_result_tampered", 4))

        changed = json.loads(json.dumps(report))
        changed["results"][0]["citation_count"] += 1
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(changed, document)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_result_tampered", 4))

        responses = _load_responses()
        responses["fact"] = {**responses["fact"], "http_status": 500}
        other = _build_report(responses=responses)
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(other, document)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("result_sha256_mismatch", 4))

    def test_run_identity_mode_and_case_binding_fail_closed(self):
        report = _build_report()
        document = _filled_reviews(report)
        for change, reason in (({"run_id": "00000000-0000-4000-8000-000000000043"}, "run_id_mismatch"),
                               ({"identity_sha256": "0" * 64}, "identity_mismatch"),
                               ({"mode": "live"}, "mode_mismatch")):
            with self.subTest(reason=reason):
                broken = {**json.loads(json.dumps(document)), **change}
                with self.assertRaises(er.ReviewError) as error:
                    er.evaluate_reviews(report, broken)
                self.assertEqual((error.exception.reason, error.exception.exit_code), (reason, 4))
        duplicate = json.loads(json.dumps(document))
        duplicate["reviews"].append(dict(duplicate["reviews"][0]))
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(report, duplicate)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("reviews_duplicate_case", 2))
        extra = json.loads(json.dumps(document))
        extra["reviews"].append({**extra["reviews"][0], "id": "extra-case"})
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(report, extra)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("unexpected_review_case", 4))
        missing = json.loads(json.dumps(document))
        missing["reviews"].pop()
        summary = er.evaluate_reviews(report, missing)
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("unknown", 5))
        self.assertEqual(summary["human_review"]["cases_without_review"], 1)

    def test_old_report_without_fingerprint_and_tampered_structure_are_rejected(self):
        report = _build_report()
        old = json.loads(json.dumps(report))
        old["results"][0].pop("result_sha256")
        with self.assertRaises(er.ReviewError) as error:
            er.create_template(old)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("report_result_fingerprint_missing", 4))
        duplicated = json.loads(json.dumps(report))
        duplicated["results"].append(json.loads(json.dumps(duplicated["results"][0])))
        duplicated["cases"] = len(duplicated["results"])
        duplicated["identity"]["dataset"]["case_count"] = duplicated["cases"]
        duplicated["identity"]["dataset"]["sha256"] = "b" * 64
        with self.assertRaises(er.ReviewError) as error:
            er.create_template(duplicated)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("duplicate_report_case", 4))

    def test_null_missing_open_unknowns_and_bad_types_cannot_pass(self):
        report = _build_report()
        document = _filled_reviews(report)
        answer_review = next(review for review in document["reviews"]
                             if next(row for row in report["results"] if row["id"] == review["id"])["expected_outcome"] == "answer")
        unverified = json.loads(json.dumps(document))
        next(review for review in unverified["reviews"] if review["id"] == answer_review["id"])["answer_correct"] = None
        summary = er.evaluate_reviews(report, unverified)
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("unknown", 5))
        open_unknown = json.loads(json.dumps(document))
        next(review for review in open_unknown["reviews"] if review["id"] == answer_review["id"])["unknowns"] = ["待确认"]
        self.assertEqual(er.evaluate_reviews(report, open_unknown)["exit_code"], 5)
        no_reviewer = json.loads(json.dumps(document))
        next(review for review in no_reviewer["reviews"] if review["id"] == answer_review["id"])["reviewer"] = None
        self.assertEqual(er.evaluate_reviews(report, no_reviewer)["exit_code"], 5)
        for change in ({"reviewed_at": "2026-13-01"}, {"answer_correct": 1}, {"unknowns": "open"}):
            with self.subTest(change=change):
                broken = json.loads(json.dumps(document))
                next(review for review in broken["reviews"] if review["id"] == answer_review["id"]).update(change)
                with self.assertRaises(er.ReviewError) as error:
                    er.evaluate_reviews(report, broken)
                self.assertEqual(error.exception.exit_code, 2)

    def test_incomplete_metadata_never_counts_true_as_verified(self):
        report = _build_report()
        document = _filled_reviews(report)
        answer_ids = [row["id"] for row in report["results"] if row.get("expected_outcome") == "answer"]
        target_id = answer_ids[0]
        for change in ({"reviewer": None}, {"unknowns": ["待确认"]}):
            with self.subTest(change=change):
                broken = json.loads(json.dumps(document))
                next(review for review in broken["reviews"] if review["id"] == target_id).update(change)
                summary = er.evaluate_reviews(report, broken)
                human = summary["human_review"]
                self.assertEqual((summary["review_status"], summary["exit_code"]), ("unknown", 5))
                self.assertEqual(human["cases_verified"], report["cases"] - 1)
                self.assertEqual(human["cases_unverified"], 1)
                # 该条仍填 true，但缺少核验元数据时不能计入 verified/passed。
                self.assertEqual(human["answer_correct"]["passed"], len(answer_ids) - 1)
                self.assertEqual(human["answer_correct"]["verified"], len(answer_ids) - 1)
                self.assertGreaterEqual(human["answer_correct"]["unverified"], 1)
                self.assertEqual(human["outcome_correct"]["passed"], report["cases"] - 1)

        explicit_false = json.loads(json.dumps(document))
        target = next(review for review in explicit_false["reviews"] if review["id"] == target_id)
        target["reviewer"] = None
        target["outcome_correct"] = False
        failed = er.evaluate_reviews(report, explicit_false)
        self.assertEqual((failed["review_status"], failed["exit_code"]), ("failed", 1))
        self.assertGreaterEqual(failed["human_review"]["outcome_correct"]["failed"], 1)
        # 元数据不完整时，false 仍阻断通过，但也不应计为“已核验”判断。
        self.assertEqual(failed["human_review"]["outcome_correct"]["verified"], report["cases"] - 1)

    def test_review_content_must_match_report_content(self):
        report = _build_report(include_content=True)
        document = _filled_reviews(report, er.create_template(report, include_content=True))
        target = next(review for review in document["reviews"] if "content" in review)
        target["content"]["answer"] += "被替换"
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(report, document)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("review_content_mismatch", 4))

        # 核验文件带正文，但报告未导出正文时也不能通过。
        report_without_content = _build_report()
        okay = _filled_reviews(report, er.create_template(report, include_content=True))
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(report_without_content, okay)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("review_content_mismatch", 4))

    def test_report_mode_must_match_identity_mode_and_zero_cases_is_unknown(self):
        report = _build_report()
        mislabeled = json.loads(json.dumps(report))
        mislabeled["mode"] = "live"
        with self.assertRaises(er.ReviewError) as error:
            er.create_template(mislabeled)
        self.assertEqual((error.exception.reason, error.exception.exit_code),
                         ("report_mode_identity_mismatch", 4))

        empty = _build_report([], {})
        empty_template = er.create_template(empty)
        self.assertEqual(empty_template["reviews"], [])
        summary = er.evaluate_reviews(empty, empty_template)
        self.assertEqual((summary["review_status"], summary["exit_code"]), ("unknown", 5))
        self.assertEqual(summary["issue_counts"]["no_cases"], 1)
        self.assertEqual(summary["human_review"]["cases_verified"], 0)

    def test_version_values_must_be_integers_not_bools(self):
        report = _build_report()
        broken_report = json.loads(json.dumps(report))
        broken_report["result_fingerprint_version"] = True
        with self.assertRaises(er.ReviewError) as error:
            er.create_template(broken_report)
        self.assertEqual((error.exception.reason, error.exception.exit_code),
                         ("report_result_fingerprint_missing", 4))

        broken_entry = json.loads(json.dumps(report))
        broken_entry["results"][0]["result_fingerprint_version"] = True
        with self.assertRaises(er.ReviewError) as error:
            er.create_template(broken_entry)
        self.assertEqual((error.exception.reason, error.exception.exit_code),
                         ("report_result_fingerprint_missing", 4))

        document = er.create_template(report)
        document["result_fingerprint_version"] = True
        with self.assertRaises(er.ReviewError) as error:
            er.evaluate_reviews(report, document)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("reviews_schema_invalid", 2))

    def test_summary_never_exports_content_or_reviewer(self):
        report = _build_report(include_content=True)
        document = _filled_reviews(report, er.create_template(report))
        summary = er.evaluate_reviews(report, document)
        text = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("rev-1", text)
        self.assertNotIn("提前3天", text)
        self.assertNotIn("完整答案", text)
        self.assertNotIn('"content"', text)


class StrictJsonTests(unittest.TestCase):
    def test_duplicate_keys_nan_and_invalid_documents_are_input_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dup.json"
            path.write_text('{"run_id":"a","run_id":"b"}', encoding="utf-8")
            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(path)
            self.assertEqual((error.exception.reason, error.exception.exit_code), ("json_duplicate_key", 2))
            path.write_text('{"cases":NaN}', encoding="utf-8")
            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(path)
            self.assertEqual((error.exception.reason, error.exception.exit_code), ("json_non_finite", 2))
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(er.ReviewError):
                er.load_json_file(path)
        report = _build_report()
        document = er.create_template(report)
        for broken in ({**document, "schema": "wrong"}, {**document, "result_fingerprint_version": 2},
                       {**document, "identity_sha256": "not-a-digest"}, {**document, "extra": True},
                       {**document, "reviews": [{"id": "x"}]}):
            with self.subTest(broken=sorted(broken)):
                with self.assertRaises(er.ReviewError) as error:
                    er.evaluate_reviews(report, broken)
                self.assertEqual(error.exception.exit_code, 2)

    def test_overflowing_numbers_bounded_reads_and_deep_json_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            path.write_text('{"value":1e999}', encoding="utf-8")
            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(path)
            self.assertEqual((error.exception.reason, error.exception.exit_code), ("json_non_finite", 2))

            path.write_text('{"value":-1e999}', encoding="utf-8")
            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(path)
            self.assertEqual((error.exception.reason, error.exception.exit_code), ("json_non_finite", 2))

            path.write_bytes(b'{"value":"0123456789"}')
            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(path, max_bytes=10)
            self.assertEqual((error.exception.reason, error.exception.exit_code), ("file_too_large", 2))

            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(Path(temp))
            self.assertEqual(error.exception.exit_code, 2)

            path.write_text('{"value":' + "[" * 100 + "0" + "]" * 100 + "}", encoding="utf-8")
            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(path)
            self.assertEqual((error.exception.reason, error.exception.exit_code), ("json_too_deep", 2))


class StrictJsonBytesTests(unittest.TestCase):
    """load_json_bytes 与原 load_json_file 共用严格解析，错误码与上限保持一致。"""

    def test_bytes_loader_rejects_duplicate_nan_encoding_depth_and_oversize(self):
        cases = [
            (b'{"value":1,"value":2}', "json_duplicate_key"),
            (b'{"value":NaN}', "json_non_finite"),
            (b'{"value":1e999}', "json_non_finite"),
            (b'{"value":"\xff"}', "json_invalid"),
            (b"[]", "json_invalid"),
            (b'{"value":' + b"[" * 100 + b"0" + b"]" * 100 + b"}", "json_too_deep"),
        ]
        for raw, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaises(er.ReviewError) as error:
                    er.load_json_bytes(raw)
                self.assertEqual((error.exception.reason, error.exception.exit_code), (reason, 2))

        with self.assertRaises(er.ReviewError) as error:
            er.load_json_bytes(b'{"value":"0123456789"}', max_bytes=10)
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("file_too_large", 2))

    def test_bytes_loader_accepts_bom_and_matches_file_loader(self):
        raw = b'{"run_id":"a","value":"ok"}'
        self.assertEqual(er.load_json_bytes(raw), {"run_id": "a", "value": "ok"})
        self.assertEqual(er.load_json_bytes(b"\xef\xbb\xbf" + raw), {"run_id": "a", "value": "ok"})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            path.write_bytes(b"\xef\xbb\xbf" + raw)
            self.assertEqual(er.load_json_file(path), er.load_json_bytes(b"\xef\xbb\xbf" + raw))
            path.write_bytes(b'{"value":1,"value":2}')
            with self.assertRaises(er.ReviewError) as error:
                er.load_json_file(path)
            self.assertEqual((error.exception.reason, error.exception.exit_code),
                             ("json_duplicate_key", 2))


class ReviewCliTests(unittest.TestCase):
    def test_template_then_empty_reviews_is_unknown_and_no_content_is_written(self):
        report = _build_report(include_content=True)
        with tempfile.TemporaryDirectory() as temp:
            report_path = _write(Path(temp) / "report.json", report)
            template_path = Path(temp) / "template.json"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path),
                                               "--create-template", str(template_path)])
            self.assertEqual(code, 0)
            self.assertNotIn("提前3天", output.getvalue())
            template = json.loads(template_path.read_text(encoding="utf-8"))
            self.assertEqual(template["run_id"], report["run_id"])
            self.assertTrue(all(review["answer_correct"] is None for review in template["reviews"]))
            self.assertNotIn('"content"', template_path.read_text(encoding="utf-8"))
            summary_path = Path(temp) / "summary.json"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path), "--reviews", str(template_path),
                                               "--output", str(summary_path)])
            self.assertEqual(code, 5)
            self.assertNotIn("提前3天", output.getvalue())
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["review_status"], "unknown")
            self.assertNotIn('"content"', summary_path.read_text(encoding="utf-8"))
            filled = _filled_reviews(report, template)
            filled_path = _write(Path(temp) / "filled.json", filled)
            with contextlib.redirect_stdout(io.StringIO()):
                code = review_evaluation.main(["--report", str(report_path), "--reviews", str(filled_path),
                                               "--output", str(summary_path), "--overwrite"])
            self.assertEqual(code, 0)

    def test_cli_refuses_overwrite_collision_and_content_without_report_body(self):
        report = _build_report()
        with tempfile.TemporaryDirectory() as temp:
            report_path = _write(Path(temp) / "report.json", report)
            template_path = Path(temp) / "template.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(review_evaluation.main(["--report", str(report_path),
                                                          "--create-template", str(template_path)]), 0)
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path),
                                               "--create-template", str(template_path)])
            self.assertEqual(code, 2)
            self.assertIn("output_exists", output.getvalue())
            with contextlib.redirect_stdout(io.StringIO()):
                code = review_evaluation.main(["--report", str(report_path),
                                               "--create-template", str(template_path), "--overwrite"])
            self.assertEqual(code, 0)
            before = report_path.read_text(encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path), "--create-template", str(report_path)])
            self.assertEqual(code, 2)
            self.assertIn("input_output_collision", output.getvalue())
            self.assertEqual(before, report_path.read_text(encoding="utf-8"))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path), "--create-template",
                                               str(Path(temp) / "t2.json"), "--template-include-content"])
            self.assertEqual(code, 2)
            self.assertIn("report_content_missing", output.getvalue())

    def test_cli_refuses_hardlink_and_symlink_aliases_even_with_overwrite(self):
        report = _build_report()
        with tempfile.TemporaryDirectory() as temp:
            report_path = _write(Path(temp) / "report.json", report)
            before = report_path.read_bytes()
            hardlink = Path(temp) / "hardlink.json"
            try:
                os.link(report_path, hardlink)
            except OSError as exc:
                self.skipTest(f"hardlink unavailable: {exc}")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path),
                                               "--create-template", str(hardlink), "--overwrite"])
            self.assertEqual(code, 2)
            self.assertIn("input_output_collision", output.getvalue())
            self.assertEqual(before, report_path.read_bytes())

            symlink = Path(temp) / "symlink.json"
            try:
                symlink.symlink_to(report_path)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path),
                                               "--create-template", str(symlink), "--overwrite"])
            self.assertEqual(code, 2)
            self.assertIn("input_output_collision", output.getvalue())
            self.assertEqual(before, report_path.read_bytes())

    def test_review_output_hardlink_to_review_file_is_rejected(self):
        report = _build_report()
        document = _filled_reviews(report)
        with tempfile.TemporaryDirectory() as temp:
            report_path = _write(Path(temp) / "report.json", report)
            review_path = _write(Path(temp) / "reviews.json", document)
            before = review_path.read_bytes()
            hardlink = Path(temp) / "summary.json"
            try:
                os.link(review_path, hardlink)
            except OSError as exc:
                self.skipTest(f"hardlink unavailable: {exc}")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path), "--reviews", str(review_path),
                                               "--output", str(hardlink), "--overwrite"])
            self.assertEqual(code, 2)
            self.assertIn("input_output_collision", output.getvalue())
            self.assertEqual(before, review_path.read_bytes())

    def test_review_cli_does_not_import_or_create_http_clients(self):
        source = Path(review_evaluation.__file__).read_text(encoding="utf-8")
        self.assertNotIn("httpx", source)
        self.assertNotIn("requests", source)
        self.assertNotIn("urllib.request", source)


class UnicodeSafetyTests(unittest.TestCase):
    def test_loader_rejects_lone_surrogates_in_values_and_keys(self):
        for bad in ("\ud800", "\udc00", "\ud800x", "x\udc00"):
            payloads = [{"value": bad}, {bad: "value"}, {"nested": [{"value": bad}]}]
            for payload in payloads:
                with tempfile.TemporaryDirectory() as temp:
                    path = _write_escaped(Path(temp) / "input.json", payload)
                    with self.assertRaises(er.ReviewError) as error:
                        er.load_json_file(path)
                    self.assertEqual((error.exception.reason, error.exception.exit_code),
                                     ("json_invalid", 2))
        with self.assertRaises(er.ReviewError) as error:
            er._check_json_value({"value": "\ud800"})
        self.assertEqual((error.exception.reason, error.exception.exit_code), ("json_invalid", 2))

    def test_direct_service_rejects_lone_surrogates_before_digesting(self):
        report = _build_report(include_content=True)
        report["results"][0]["content"]["answer"] = "\ud800"
        with self.assertRaises(er.ReviewError) as error:
            er.create_template(report)
        self.assertEqual((error.exception.reason, error.exception.exit_code),
                         ("report_structure_invalid", 2))

        report = _build_report()
        report["results"][0]["content"] = {"answer": "\ud800", "citations": []}
        with self.assertRaises(er.ReviewError) as error:
            er.inspect_report(report)
        self.assertEqual((error.exception.reason, error.exception.exit_code),
                         ("report_structure_invalid", 2))

        report = _build_report()
        mutations = []
        for key in ("unknowns", "content"):
            changed = _filled_reviews(report)
            changed["reviews"][0][key] = ["\ud800"] if key == "unknowns" else {
                "answer": "\ud800", "citations": []}
            mutations.append(changed)
        changed = _filled_reviews(report)
        changed["reviews"][0]["\ud800"] = "value"
        mutations.append(changed)
        changed = _filled_reviews(report)
        changed["\ud800"] = "value"
        mutations.append(changed)
        for changed in mutations:
            with self.assertRaises(er.ReviewError) as error:
                er.validate_review_document(changed)
            self.assertEqual((error.exception.reason, error.exception.exit_code),
                             ("reviews_schema_invalid", 2))
            with self.assertRaises(er.ReviewError) as error:
                er.evaluate_reviews(report, changed)
            self.assertEqual((error.exception.reason, error.exception.exit_code),
                             ("reviews_schema_invalid", 2))

    def test_cli_rejects_lone_surrogates_without_traceback_or_output(self):
        report = _build_report(include_content=True)
        report["results"][0]["content"]["answer"] = "\ud800"
        with tempfile.TemporaryDirectory() as temp:
            report_path = _write_escaped(Path(temp) / "report.json", report)
            template_path = Path(temp) / "template.json"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path),
                                               "--create-template", str(template_path)])
            self.assertEqual(code, 2)
            self.assertIn("json_invalid", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            self.assertFalse(template_path.exists())

        report = _build_report()
        document = _filled_reviews(report)
        document["reviews"][0]["unknowns"] = ["\ud800"]
        with tempfile.TemporaryDirectory() as temp:
            report_path = _write(Path(temp) / "report.json", report)
            review_path = _write_escaped(Path(temp) / "reviews.json", document)
            summary_path = Path(temp) / "summary.json"
            with contextlib.redirect_stdout(io.StringIO()) as output:
                code = review_evaluation.main(["--report", str(report_path), "--reviews", str(review_path),
                                               "--output", str(summary_path)])
            self.assertEqual(code, 2)
            self.assertIn("json_invalid", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
            self.assertFalse(summary_path.exists())

    def test_valid_paired_surrogate_emoji_is_accepted(self):
        emoji = "\U0001F600"
        report = _build_report(include_content=True)
        report["results"][0]["content"]["answer"] = emoji
        report["results"][0]["result_sha256"] = ev.result_sha256(report["results"][0])
        report["results"][0]["machine_sha256"] = ev.machine_sha256(report["results"][0])
        template = er.create_template(report, include_content=True)
        self.assertEqual(template["reviews"][0]["content"]["answer"], emoji)

        with tempfile.TemporaryDirectory() as temp:
            report_path = _write_escaped(Path(temp) / "report.json", report)
            self.assertIn("\\ud83d\\ude00", report_path.read_text(encoding="utf-8"))
            loaded = er.load_json_file(report_path)
            self.assertEqual(loaded["results"][0]["content"]["answer"], emoji)
            loaded_template = er.create_template(loaded, include_content=True)
            self.assertEqual(loaded_template["reviews"][0]["content"]["answer"], emoji)

        document = _filled_reviews(report)
        document["reviews"][0]["unknowns"] = ["😀"]
        with tempfile.TemporaryDirectory() as temp:
            review_path = _write_escaped(Path(temp) / "reviews.json", document)
            loaded_document = er.load_json_file(review_path)
            validated = er.validate_review_document(loaded_document)
            self.assertEqual(validated["reviews"][0]["unknowns"], [emoji])
            summary = er.evaluate_reviews(report, validated)
            # 合法 emoji 被接受；但 unknowns 非空仍是未决，不能算通过。
            self.assertEqual((summary["review_status"], summary["exit_code"]), ("unknown", 5))


if __name__ == "__main__":
    unittest.main()
