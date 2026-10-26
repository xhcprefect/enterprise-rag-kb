"""评测固定断言、隐私默认值、计费边界与内存 HTTP 流，无真实网络。"""
import contextlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import httpx

from app.services import evaluation as ev
from scripts.evaluate_rag import main


class EvaluationTests(unittest.TestCase):
    def test_public_fixture_passes_without_claiming_semantic_accuracy_or_exporting_text(self):
        root = Path(__file__).parents[1] / "evaluation"
        cases = ev.validate_cases(ev.read_jsonl(root / "cases.example.jsonl"))
        responses = {row["id"]: row for row in ev.read_jsonl(root / "responses.example.jsonl")}
        report = ev.make_report([ev.grade_case(case, responses[case["id"]]) for case in cases])
        self.assertEqual((report["passed"], report["cases"]), (6, 6))
        self.assertEqual(report["candidate_source_recall"], 1)
        self.assertIsNone(report["generation_cost_estimate"])
        self.assertNotIn("提前3天", json.dumps(report, ensure_ascii=False))
        self.assertIn("不是语义事实正确率", report["notes"][0])

    def test_required_forbidden_outcome_and_source_assertions_fail_independently(self):
        case = {"id": "x", "expected_outcome": "answer", "required_phrases": ["must"], "forbidden_phrases": ["bad"],
                "expected_sources": [{"filename": "right.txt"}]}
        result = ev.grade_case(case, {"answer": "根据现有知识库无法回答，bad", "citations": [{"filename": "wrong.txt"}],
                                      "trace": {"candidate_sources": []}})
        self.assertFalse(result["passed"])
        self.assertTrue({"outcome_mismatch", "required_phrase_missing", "forbidden_phrase_present", "missing_expected_candidates", "missing_expected_citations"}.issubset(result["failures"]))

    def test_merged_source_range_matches_only_included_chunks(self):
        response = {"answer": "ok", "citations": [{"filename": "doc", "chunk_index": 2, "chunk_start": 1, "chunk_end": 3}]}
        self.assertTrue(ev.grade_case({"id": "x", "expected_sources": [{"filename": "doc", "chunk_index": 3}]}, response)["passed"])
        self.assertFalse(ev.grade_case({"id": "x", "expected_sources": [{"filename": "doc", "chunk_index": 4}]}, response)["passed"])

    def test_actual_usage_is_required_and_prices_only_estimate_generation_cost(self):
        case = {"id": "cost"}
        known = ev.grade_case(case, {"answer": "ok", "trace": {"usage": {"input_tokens": 100, "output_tokens": 20}}}, input_price=2, output_price=4)
        self.assertAlmostEqual(known["generation_cost_estimate"], 0.00028)
        unknown = ev.grade_case(case, {"answer": "ok", "trace": {"input_tokens_estimate": 9000}}, input_price=2, output_price=4)
        self.assertIsNone(unknown["generation_cost_estimate"])
        self.assertIsNone(ev.make_report([known, unknown])["generation_cost_estimate"])

    def test_missing_response_is_a_failure_not_an_empty_success(self):
        result = ev.grade_case({"id": "x"}, {"error": "missing_response"})
        self.assertFalse(result["passed"])
        self.assertIn("empty_answer", result["failures"])
        self.assertIsNone(result["first_token_ms"])

    def test_dataset_validation_rejects_duplicate_ids_or_unbounded_turns(self):
        for cases in [[{"id": "x", "question": "q"}] * 2, [{"id": "x", "turns": ["q"] * 11}],
                      [{"id": "x", "question": " "}], [{"id": "x", "question": "q", "mode": "wrong"}]]:
            with self.subTest(cases=cases), self.assertRaises(ev.EvaluationError):
                ev.validate_cases(cases)

    def test_sse_requires_done_and_rejects_error_and_invalid_json(self):
        for lines in [["event: delta", 'data: {"content":"partial"}', ""],
                      ["event: error", 'data: {"message":"secret"}', ""], ["event: done", "data: not-json", ""]]:
            with self.subTest(lines=lines), self.assertRaises(ev.EvaluationError) as error:
                ev.consume_sse(lines, 0)
            self.assertNotIn("secret", str(error.exception))

    def test_live_requests_use_stable_ids_and_preserve_conversation_across_turns(self):
        requests = []
        conversation = str(uuid.uuid4())
        class FragmentedBody(httpx.SyncByteStream):
            def __iter__(self):
                text = ('event: meta\r\ndata: ' + json.dumps({"conversation_id": conversation}) + '\r\n\r\n'
                        'event: delta\r\ndata: {"content":"answer"}\r\n\r\n'
                        'event: done\r\ndata: {"citations":[],"trace":{"usage":{"input_tokens":10,"output_tokens":2}}}\r\n\r\n').encode()
                for offset in range(0, len(text), 3):
                    yield text[offset:offset + 3]
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, stream=FragmentedBody())
        case, run_id = {"id": "multi", "mode": "chat", "turns": ["first", "followup"]}, uuid.uuid4()
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            first = ev.run_live_case(client, case, base_url="https://example.test", token="secret", run_id=run_id)
            ev.run_live_case(client, case, base_url="https://example.test", token="secret", run_id=run_id)
        self.assertNotIn("conversation_id", requests[0])
        self.assertEqual(requests[1]["conversation_id"], conversation)
        self.assertEqual(requests[0]["request_id"], requests[2]["request_id"])
        self.assertNotEqual(requests[0]["request_id"], requests[1]["request_id"])
        self.assertEqual(first["generation_usage"], {"input_tokens": 20, "output_tokens": 4})
        self.assertEqual(first["answer"], "answer")

    def test_offline_cli_never_creates_http_client_and_defaults_to_redacted_report(self):
        root = Path(__file__).parents[1] / "evaluation"
        with tempfile.TemporaryDirectory() as temp, patch("scripts.evaluate_rag.httpx.Client") as client, \
             patch.dict(os.environ, {"RAG_EVAL_TOKEN": "never-print-this"}), contextlib.redirect_stdout(io.StringIO()) as output:
            report = Path(temp) / "report.json"
            code = main(["--dataset", str(root / "cases.example.jsonl"), "--responses", str(root / "responses.example.jsonl"), "--output", str(report)])
            content = report.read_text(encoding="utf-8")
        client.assert_not_called()
        self.assertEqual(code, 0)
        self.assertNotIn("never-print-this", output.getvalue())
        self.assertNotIn('"content"', content)

    def test_supplied_source_recall_distinguishes_budget_exclusion_from_retrieval(self):
        source = {"filename": "policy.md", "chunk_index": 0}
        case = {"id": "budget", "expected_sources": [source]}
        result = ev.grade_case(case, {"answer": "answer", "citations": [],
                                      "trace": {"candidate_sources": [source], "supplied_sources": []}})
        self.assertEqual(result["candidate_source_recall"], 1)
        self.assertEqual(result["supplied_source_recall"], 0)
        self.assertIn("missing_expected_supplied_sources", result["failures"])

    def test_boundary_fixture_checks_stale_sources_and_actual_http_denial(self):
        root = Path(__file__).parents[1] / "evaluation"
        cases = ev.validate_cases(ev.read_jsonl(root / "cases.boundaries.example.jsonl"))
        responses = {row["id"]: row for row in ev.validate_responses(ev.read_jsonl(root / "responses.boundaries.example.jsonl"))}
        report = ev.make_report([ev.grade_case(case, responses[case["id"]]) for case in cases])
        self.assertEqual((report["passed"], report["cases"]), (2, 2))
        permission = cases[1]
        self.assertFalse(ev.grade_case(permission, {"answer": "private data", "citations": []})["passed"])
        self.assertFalse(ev.grade_case(permission, {"http_status": 401, "error": "http_error"})["passed"])

    def test_permission_case_disables_admin_trace_and_does_not_read_error_body(self):
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(403, json={"message": "never-export-this-body"})
        case = {"id": "permission", "actor": "member", "question": "private question", "mode": "chat",
                "expected_http_status": 403}
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            response = ev.run_live_case(client, case, base_url="https://example.test", token="member-secret", run_id=uuid.uuid4())
        self.assertFalse(requests[0]["include_trace"])
        self.assertEqual(response["http_status"], 403)
        self.assertNotIn("never-export-this-body", str(response))
        self.assertTrue(ev.grade_case(case, response)["passed"])

    def test_preseeded_conversation_is_used_to_exercise_stale_history(self):
        conversation = str(uuid.uuid4())
        request_body = []
        def handle(request):
            request_body.append(json.loads(request.content))
            body = ('event: meta\ndata: ' + json.dumps({"conversation_id": conversation}) + '\n\n'
                    'event: delta\ndata: {"content":"请确认来源。"}\n\n'
                    'event: done\ndata: {"citations":[],"trace":{"status":"clarify"}}\n\n')
            return httpx.Response(200, text=body)
        case = {"id": "stale", "question": "解释上一轮来源1", "mode": "chat", "conversation_id": conversation}
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            ev.run_live_case(client, case, base_url="https://example.test", token="secret", run_id=uuid.uuid4())
        self.assertEqual(request_body[0]["conversation_id"], conversation)

    def test_malformed_replays_and_nonfinite_json_fail_as_configuration_errors(self):
        for row in [{"id": []}, {"id": "x", "answer": []}, {"id": "x", "citations": ["not-an-object"]},
                    {"id": "x", "trace": {"candidate_sources": "wrong"}}]:
            with self.subTest(row=row), self.assertRaises(ev.EvaluationError):
                ev.validate_responses([row])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nan.jsonl"
            path.write_text('{"id":"x","total_ms":NaN}', encoding="utf-8")
            with self.assertRaises(ev.EvaluationError):
                ev.read_jsonl(path)
        for payload in ['{"citations":["bad"]}', '{"trace":{"supplied_sources":2}}', '{"latency":NaN}']:
            with self.subTest(payload=payload), self.assertRaises(ev.EvaluationError):
                ev.consume_sse(["event: done", "data: " + payload, ""], 0)

    def test_case_validation_rejects_malformed_boundary_and_boolean_assertions(self):
        for extra in [{"expect_no_citations": "false"}, {"actor": "unknown"}, {"mode": []},
                      {"conversation_id": "not-a-uuid"}, {"expected_http_status": True},
                      {"expected_http_status": 403, "expected_outcome": "answer"}]:
            with self.subTest(extra=extra), self.assertRaises(ev.EvaluationError):
                ev.validate_cases([{"id": "x", "question": "q", **extra}])

    def test_live_cli_rejects_remote_plaintext_before_sending_bearer_token(self):
        with patch.dict(os.environ, {"RAG_EVAL_TOKEN": "secret"}), patch("scripts.evaluate_rag.httpx.Client") as client, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = main(["--live", "--base-url", "http://untrusted.test"])
        self.assertEqual(code, 2)
        self.assertNotIn("secret", output.getvalue())
        client.assert_not_called()
