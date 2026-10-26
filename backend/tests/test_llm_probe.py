"""模型诊断只发送固定合成文本，显式授权后才联网，且不输出推理或密钥。"""
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import httpx

from app.core.config import settings
from scripts import _probe_llm


class LlmProbeTests(unittest.TestCase):
    def setUp(self):
        self.client_class = httpx.Client
        self.requests = []
        self.output = io.StringIO()
        for name, value in [("LLM_PROVIDER", "cloud"), ("LLM_REASONING_EFFORT", "none"),
                            ("LLM_MODEL", "deepseek-flash"), ("LLM_API_KEY", "never-print-this-key"),
                            ("LLM_BASE_URL", "https://example.invalid/v1")]:
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_probe(self, events, status=200):
        def handle(request):
            self.requests.append(request)
            return httpx.Response(status, headers={"content-type": "text/event-stream"}, text=events)
        def create(**kwargs):
            return self.client_class(transport=httpx.MockTransport(handle), **kwargs)
        with patch.object(httpx, "Client", side_effect=create), redirect_stdout(self.output):
            result = _probe_llm.main(["--live", "--timeout", "1"])
        return result, json.loads(self.output.getvalue())

    def test_requires_explicit_live_opt_in_without_opening_a_client(self):
        with patch.object(httpx, "Client") as client, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                _probe_llm.main([])
        self.assertEqual(error.exception.code, 2)
        client.assert_not_called()

    def test_reports_zero_reasoning_using_the_application_payload(self):
        status, report = self.run_probe(
            'data: {"choices":[{"delta":{"content":"PUBLIC-ANSWER"}}]}\n\n'
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":'
            '{"prompt_tokens":12,"completion_tokens":2,"completion_tokens_details":{"reasoning_tokens":0}}}\n\n'
            'data: [DONE]\n\n')
        self.assertEqual(status, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["reasoning_chars"], 0)
        self.assertEqual(report["reasoning_tokens"], 0)
        self.assertEqual(report["content_chars"], len("PUBLIC-ANSWER"))
        self.assertEqual(len(self.requests), 1)
        payload = json.loads(self.requests[0].content)
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertLessEqual(payload["max_tokens"], 64)
        self.assertIs(payload["stream"], True)
        self.assertNotIn("PUBLIC-ANSWER", self.output.getvalue())
        self.assertNotIn("never-print-this-key", self.output.getvalue())

    def test_reasoning_is_a_failure_and_is_never_printed(self):
        status, report = self.run_probe(
            'data: {"choices":[{"delta":{"reasoning_content":"PRIVATE-REASONING"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}\n\n')
        self.assertEqual(status, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["reasoning_chars"], len("PRIVATE-REASONING"))
        self.assertNotIn("PRIVATE-REASONING", self.output.getvalue())

    def test_reasoning_tokens_without_visible_reasoning_are_also_a_failure(self):
        status, report = self.run_probe(
            'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}],'
            '"usage":{"completion_tokens_details":{"reasoning_tokens":9}}}\n\n')
        self.assertEqual(status, 1)
        self.assertEqual(report["reasoning_tokens"], 9)

    def test_empty_or_incomplete_responses_cannot_pass(self):
        for events in ['data: [DONE]\n\n', 'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n']:
            with self.subTest(events=events):
                self.output = io.StringIO()
                status, report = self.run_probe(events)
                self.assertEqual(status, 1)
                self.assertFalse(report["passed"])

    def test_upstream_error_body_is_redacted(self):
        status, report = self.run_probe("never-print-this-key PRIVATE-UPSTREAM-ERROR", status=401)
        self.assertEqual(status, 1)
        self.assertEqual(report["http_status"], 401)
        self.assertEqual(report["error_type"], "HTTPStatusError")
        self.assertNotIn("never-print-this-key", self.output.getvalue())
        self.assertNotIn("PRIVATE-UPSTREAM-ERROR", self.output.getvalue())
