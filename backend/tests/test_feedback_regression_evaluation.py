"""单条反馈回归评测适配层：纯函数、严格报告校验与 CLI 预检（无 DB/网络）。"""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from app.services import evaluation as ev
from app.services import feedback_regression_evaluation as fre
from scripts.evaluate_rag import main as evaluate_main

RUN_ID = uuid.UUID("00000000-0000-4000-8000-000000000042")
KB_ID = "00000000-0000-4000-8000-000000000001"
SOURCE = {
    "document_id": "00000000-0000-4000-8000-000000000101",
    "filename": "doc.md",
    "index_version": 2,
    "chunk_index": 1,
}
CASE_ID = "feedback-00000000-0000-4000-8000-0000000000aa"

SUMMARY_KEYS = {
    "report_sha256", "report_mode", "validation_level", "passed", "failures", "outcome",
    "http_status", "citation_count", "dataset_report_sha256", "result_sha256", "machine_sha256",
}


def _base_case(**overrides):
    case = {
        "id": CASE_ID,
        "turns": ["第一个问题", "第二个问题"],
        "question": "第二个问题",
        "mode": "rag",
        "expected_outcome": "answer",
        "expected_sources": [dict(SOURCE)],
        "required_phrases": ["必须出现的短语"],
        "forbidden_phrases": [],
        "actor": "admin",
        "verified_by": "admin",
        "verified_at": "2026-11-08",
        "reviewed": True,
        "unknowns": [],
        "top_k": 6,
        "kb_id": KB_ID,
    }
    case.update(overrides)
    return case


def _response():
    citation = {**SOURCE, "snippet": "原文片段"}
    return {
        "answer": "根据资料，必须出现的短语。",
        "citations": [citation],
        "trace": {"candidate_sources": [citation], "supplied_sources": [citation]},
    }


def _report(case, *, include_content=False, run_id=RUN_ID, mode="offline", extra_cases=()):
    results = [ev.grade_case(case, _response(), include_content=include_content)]
    for extra in extra_cases:
        results.append(ev.grade_case(extra, _response(), include_content=include_content))
    identity = ev.build_identity([case, *extra_cases], mode=mode, documents_dir=None)
    report = ev.make_report(results, run_id, identity=identity)
    report["mode"] = mode
    return report


def _raw(report):
    return json.dumps(report, ensure_ascii=False).encode("utf-8")


def _dataset_sha(case):
    return fre.case_fingerprints(case)["dataset_sha256"]


def _validate(report, *, case=None, run_id=RUN_ID, mode="offline", dataset_sha256=None):
    case = _base_case() if case is None else case
    return fre.validate_regression_report(
        _raw(report), case=case, run_id=run_id, requested_mode=mode,
        dataset_sha256=_dataset_sha(case) if dataset_sha256 is None else dataset_sha256)


def _resign(result):
    result["machine_sha256"] = ev.machine_sha256(result)
    return result


class _Feedback:
    id = uuid.UUID("00000000-0000-4000-8000-0000000000ff")
    revision = 3
    result_sha256 = "a" * 64


class CaseHelperTests(unittest.TestCase):
    def test_mark_mode_returns_copy_and_does_not_mutate_base_case(self):
        base = _base_case()
        marked = fre.mark_mode(base, "offline")
        self.assertIsNot(marked, base)
        self.assertNotIn("regression_protocol", base)
        self.assertNotIn("regression_mode", base)
        self.assertEqual(marked["regression_protocol"], 1)
        self.assertEqual(marked["regression_mode"], "offline")
        self.assertEqual(set(marked) - set(base), {"regression_protocol", "regression_mode"})
        for invalid in ("", "hybrid", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                fre.mark_mode(base, invalid)

    def test_case_fingerprints_cover_full_case_and_dataset_identity(self):
        case = _base_case()
        fingerprints = fre.case_fingerprints(case)
        canonical = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(fingerprints["case_sha256"],
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertEqual(fingerprints["dataset_sha256"], ev.dataset_identity([case])["sha256"])
        self.assertEqual(fingerprints, fre.case_fingerprints(json.loads(json.dumps(case))))

        changed = {**case, "required_phrases": ["另一个短语"]}
        self.assertNotEqual(fingerprints["case_sha256"], fre.case_fingerprints(changed)["case_sha256"])
        self.assertNotEqual(fingerprints["dataset_sha256"], fre.case_fingerprints(changed)["dataset_sha256"])

        annotated = {**case, "stratum": "fact"}
        self.assertNotEqual(fingerprints["case_sha256"], fre.case_fingerprints(annotated)["case_sha256"])
        self.assertEqual(fingerprints["dataset_sha256"], fre.case_fingerprints(annotated)["dataset_sha256"])

        marked = fre.mark_mode(case, "offline")
        self.assertNotEqual(fingerprints["dataset_sha256"], fre.case_fingerprints(marked)["dataset_sha256"])
        self.assertNotEqual(fre.case_fingerprints(marked)["dataset_sha256"],
                            fre.case_fingerprints(fre.mark_mode(case, "live"))["dataset_sha256"])

    def test_binding_is_stable_and_excludes_regression_round_fields(self):
        case = _base_case()
        payload = fre.base_binding_payload(
            case, feedback=_Feedback, resolved_sources=[dict(SOURCE)], kb_id=KB_ID,
            mode="rag", cause="retrieval")
        self.assertEqual(payload["protocol_version"], 1)
        self.assertEqual(payload["feedback_id"], str(_Feedback.id))
        self.assertEqual(payload["feedback_revision"], 3)
        self.assertEqual(payload["feedback_result_sha256"], "a" * 64)
        self.assertEqual(payload["cause"], "retrieval")
        self.assertEqual(payload["kb_id"], KB_ID)
        self.assertEqual(payload["mode"], "rag")
        self.assertEqual(payload["case_sha256"], fre.case_fingerprints(case)["case_sha256"])
        self.assertEqual(payload["dataset_sha256"], fre.case_fingerprints(case)["dataset_sha256"])
        self.assertEqual(payload["sources"], [{"document_id": SOURCE["document_id"],
                                                "index_version": 2, "chunk_index": 1}])
        for forbidden in ("run_id", "attempt_no", "regression_revision", "revision", "binding_sha256"):
            self.assertNotIn(forbidden, payload)
        digest = fre.binding_sha256(payload)
        self.assertEqual(digest, fre.binding_sha256(fre.base_binding_payload(
            case, feedback=_Feedback, resolved_sources=[dict(SOURCE)], kb_id=KB_ID,
            mode="rag", cause="retrieval")))
        self.assertEqual(len(digest), 64)

        variants = [
            {"cause": "generation"},
            {"mode": "chat"},
            {"kb_id": str(uuid.UUID(KB_ID))[:-1] + "2"},
            {"resolved_sources": [{**SOURCE, "chunk_index": 2}]},
        ]
        other_feedback = type("OtherFeedback", (), {
            "id": _Feedback.id, "revision": 4, "result_sha256": _Feedback.result_sha256})
        baseline = dict(feedback=_Feedback, resolved_sources=[dict(SOURCE)], kb_id=KB_ID,
                        mode="rag", cause="retrieval")
        for variant in variants:
            with self.subTest(variant=sorted(variant)):
                changed = {**baseline, **variant}
                self.assertNotEqual(digest, fre.binding_sha256(fre.base_binding_payload(case, **changed)))
        self.assertNotEqual(digest, fre.binding_sha256(fre.base_binding_payload(
            case, feedback=other_feedback, resolved_sources=[dict(SOURCE)], kb_id=KB_ID,
            mode="rag", cause="retrieval")))


class ReportValidationTests(unittest.TestCase):
    def test_offline_report_without_content_is_consistency_only(self):
        case = fre.mark_mode(_base_case(), "offline")
        report = _report(case)
        raw = _raw(report)
        summary = _validate(report, case=case)
        self.assertEqual(set(summary), SUMMARY_KEYS)
        self.assertEqual(summary["validation_level"], "consistency_only")
        self.assertEqual(summary["report_mode"], "offline")
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["failures"], [])
        self.assertEqual(summary["outcome"], "answer")
        self.assertEqual(summary["http_status"], 200)
        self.assertEqual(summary["citation_count"], 1)
        self.assertEqual(summary["result_sha256"], report["results"][0]["result_sha256"])
        self.assertEqual(summary["machine_sha256"], report["results"][0]["machine_sha256"])
        self.assertEqual(summary["dataset_report_sha256"], report["identity"]["dataset"]["sha256"])
        parsed = json.loads(raw.decode("utf-8"))
        canonical = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(summary["report_sha256"],
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest())
        self.assertNotIn("必须出现的短语", json.dumps(summary, ensure_ascii=False))

    def test_report_with_content_is_regraded_and_rejects_inconsistent_content(self):
        case = fre.mark_mode(_base_case(), "offline")
        report = _report(case, include_content=True)
        summary = _validate(report, case=case)
        self.assertEqual(summary["validation_level"], "content_regraded")
        self.assertTrue(summary["passed"])

        questions = _report(case, include_content=True)
        questions["results"][0]["content"]["questions"] = list(
            reversed(questions["results"][0]["content"]["questions"]))
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(questions, case=case)
        self.assertEqual(error.exception.reason, "report_content_mismatch")

        answer = _report(case, include_content=True)
        result = answer["results"][0]
        result["content"]["answer"] = "完全错误的回答"
        result["result_sha256"] = ev.result_sha256(result)
        _resign(result)
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(answer, case=case)
        self.assertEqual(error.exception.reason, "report_content_mismatch")

    def test_run_mode_case_and_dataset_binding_errors(self):
        case = fre.mark_mode(_base_case(), "offline")
        wrong_run = _report(case, run_id=uuid.UUID("00000000-0000-4000-8000-000000000043"))
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(wrong_run, case=case)
        self.assertEqual(error.exception.reason, "report_run_mismatch")

        wrong_mode = _report(case, mode="live")
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(wrong_mode, case=case, mode="offline")
        self.assertEqual(error.exception.reason, "report_mode_mismatch")

        wrong_case = _report(case)
        wrong_case["results"][0]["id"] = "other-case"
        _resign(wrong_case["results"][0])
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(wrong_case, case=case)
        self.assertEqual(error.exception.reason, "report_case_mismatch")

        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(_report(case, extra_cases=[_base_case(id="other-case")]), case=case)
        self.assertEqual(error.exception.reason, "report_case_count_mismatch")

        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(_report(case), case=case, dataset_sha256="0" * 64)
        self.assertEqual(error.exception.reason, "report_dataset_mismatch")

        for mutate in (lambda dataset: dataset.__setitem__("sha256", "0" * 64),
                       lambda dataset: dataset.__setitem__("schema", 1),
                       lambda dataset: dataset.__setitem__("case_count", None)):
            changed = _report(case)
            mutate(changed["identity"]["dataset"])
            with self.subTest(), self.assertRaises(fre.RegressionReportError) as error:
                _validate(changed, case=case)
            self.assertEqual(error.exception.reason, "report_dataset_mismatch")

    def test_malformed_results_and_structural_failures_are_rejected(self):
        case = fre.mark_mode(_base_case(), "offline")

        duplicate = _report(case)
        duplicate["results"] = [duplicate["results"][0], duplicate["results"][0]]
        duplicate["cases"] = 2
        duplicate["passed"] = 2
        duplicate["assertion_pass_rate"] = 1.0
        duplicate["identity"]["dataset"]["case_count"] = 2
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(duplicate, case=case)
        self.assertEqual(error.exception.reason, "report_invalid")

        missing = _report(case)
        missing["results"] = []
        missing["cases"] = 0
        missing["passed"] = 0
        missing["assertion_pass_rate"] = None
        missing["identity"]["dataset"]["case_count"] = 0
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(missing, case=case)
        self.assertEqual(error.exception.reason, "report_case_count_mismatch")

        bad_machine = _report(case)
        bad_machine["results"][0]["machine_sha256"] = "0" * 64
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(bad_machine, case=case)
        self.assertEqual(error.exception.reason, "report_invalid")

        bad_failures = _report(case)
        bad_failures["results"][0]["failures"] = ["bad failure!"]
        _resign(bad_failures["results"][0])
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(bad_failures, case=case)
        self.assertEqual(error.exception.reason, "report_invalid")

        too_many = _report(case)
        too_many["results"][0]["failures"] = ["failure"] * 65
        _resign(too_many["results"][0])
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(too_many, case=case)
        self.assertEqual(error.exception.reason, "report_invalid")

        bad_outcome = _report(case)
        bad_outcome["results"][0]["outcome"] = "bad outcome"
        _resign(bad_outcome["results"][0])
        with self.assertRaises(fre.RegressionReportError) as error:
            _validate(bad_outcome, case=case)
        self.assertEqual(error.exception.reason, "report_invalid")

    def test_strict_bytes_parsing_and_unsupported_modes(self):
        case = fre.mark_mode(_base_case(), "offline")
        with self.assertRaises(fre.RegressionReportError) as error:
            fre.validate_regression_report(b"{}", case=case, run_id=RUN_ID,
                                           requested_mode="hybrid", dataset_sha256=_dataset_sha(case))
        self.assertEqual(error.exception.reason, "unsupported_validation")

        payloads = [
            (b'{"a":1,"a":2}', "json_invalid"),
            (b'{"a":NaN}', "json_invalid"),
            (b'{"a":"\xff"}', "json_invalid"),
            (b"[]", "json_invalid"),
            (b'{"padding":"' + b"a" * (2 * 1024 * 1024) + b'"}', "json_invalid"),
        ]
        for raw, reason in payloads:
            with self.subTest(reason=reason):
                with self.assertRaises(fre.RegressionReportError) as error:
                    fre.validate_regression_report(raw, case=case, run_id=RUN_ID,
                                                   requested_mode="offline",
                                                   dataset_sha256=_dataset_sha(case))
                self.assertEqual(error.exception.reason, reason)
                self.assertNotIn("padding", error.exception.message)


class RegressionCliTests(unittest.TestCase):
    def _regression_case(self, *, mode="offline", protocol=1, **overrides):
        case = {
            "id": CASE_ID,
            "regression_protocol": protocol,
            "regression_mode": mode,
            "turns": ["需要回归的问题"],
            "question": "需要回归的问题",
            "mode": "rag",
            "expected_outcome": "answer",
            "expected_sources": [{"filename": "doc.md"}],
            "required_phrases": [],
            "forbidden_phrases": [],
            "actor": "admin",
            "verified_by": "admin",
            "verified_at": "2026-11-08",
            "reviewed": True,
            "unknowns": [],
            "top_k": 6,
            "kb_id": KB_ID,
        }
        case.update(overrides)
        return case

    def _write_dataset(self, temp, cases):
        path = Path(temp) / "cases.jsonl"
        path.write_text("".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
                        encoding="utf-8")
        return path

    def _expect_precheck_error(self, cases, *extra):
        with tempfile.TemporaryDirectory() as temp:
            dataset = self._write_dataset(temp, cases)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                # 响应文件不存在：若预检没有先于读取响应执行，会返回 2 而不是 SystemExit。
                evaluate_main(["--dataset", str(dataset), "--run-id", str(RUN_ID),
                               "--responses", str(Path(temp) / "missing.jsonl"), *extra])
            self.assertEqual(raised.exception.code, 2)

    def test_regression_precheck_fails_before_reading_responses(self):
        case = self._regression_case()
        with tempfile.TemporaryDirectory() as temp:
            dataset = self._write_dataset(temp, [case])
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                evaluate_main(["--dataset", str(dataset),
                               "--responses", str(Path(temp) / "missing.jsonl")])
            self.assertEqual(raised.exception.code, 2)

        self._expect_precheck_error([case], "--kb-id", "00000000-0000-4000-8000-000000000001")
        self._expect_precheck_error([self._regression_case(mode="live")])
        self._expect_precheck_error([self._regression_case(protocol=2)])
        self._expect_precheck_error([case, {"id": "legacy", "question": "旧问题", "mode": "chat"}])
        self._expect_precheck_error([
            self._regression_case(conversation_id="00000000-0000-4000-8000-000000000002")])

        with tempfile.TemporaryDirectory() as temp:
            dataset = self._write_dataset(temp, [case])
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                evaluate_main(["--dataset", str(dataset), "--run-id", str(RUN_ID), "--live"])
            self.assertEqual(raised.exception.code, 2)

    def test_legacy_cases_and_valid_regression_offline_run_are_unaffected(self):
        legacy = {"id": "legacy", "question": "旧问题", "mode": "chat", "expected_outcome": "answer"}
        with tempfile.TemporaryDirectory() as temp:
            dataset = self._write_dataset(temp, [legacy])
            responses = Path(temp) / "responses.jsonl"
            responses.write_text(json.dumps({"id": "legacy", "answer": "回答"}, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
            output = Path(temp) / "legacy-report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(evaluate_main(["--dataset", str(dataset), "--responses", str(responses),
                                                "--output", str(output)]), 0)
            self.assertTrue(output.exists())

        case = self._regression_case()
        with tempfile.TemporaryDirectory() as temp:
            dataset = self._write_dataset(temp, [case])
            responses = Path(temp) / "responses.jsonl"
            responses.write_text(json.dumps({
                "id": CASE_ID, "answer": "回归回答",
                "citations": [{"filename": "doc.md", "chunk_index": 1, "index_version": 2}],
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            output = Path(temp) / "regression-report.json"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(evaluate_main(["--dataset", str(dataset), "--run-id", str(RUN_ID),
                                                "--responses", str(responses), "--output", str(output)]), 0)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["run_id"], str(RUN_ID))
            self.assertEqual(report["mode"], "offline")


if __name__ == "__main__":
    unittest.main()
