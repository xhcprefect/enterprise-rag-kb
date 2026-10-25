"""流式协议和思考内容过滤的回归测试。"""
import unittest
from unittest.mock import MagicMock, patch

from app.services import rag
from app.services.cloud_models import CloudModelError, OpenAICompatibleChat, _Endpoint


class StreamTests(unittest.TestCase):
    def test_reasoning_tags_at_every_split_position(self):
        for text in ("<think>private</think>answer", "before<thinking>private</thinking>answer"):
            expected = "beforeanswer" if text.startswith("before") else "answer"
            for offset in range(len(text) + 1):
                self.assertEqual("".join(rag.filter_reasoning([text[:offset], text[offset:]])), expected)
            self.assertEqual("".join(rag.filter_reasoning(iter(text))), expected)

    def test_unclosed_reasoning_is_hidden(self):
        self.assertEqual("".join(rag.filter_reasoning(["before<th", "ink>private"])), "before")
        self.assertEqual("".join(rag.filter_reasoning(["ordinary <th"])), "ordinary <th")

    def test_empty_rag_does_not_call_model(self):
        with patch.object(rag, "get_llm") as llm:
            self.assertIn("无法回答", "".join(rag.answer_stream("q", [], [])))
            llm.assert_not_called()

    def read(self, lines):
        client, response = MagicMock(), MagicMock()
        response.iter_lines.return_value = lines
        llm = OpenAICompatibleChat(_Endpoint("https://example.invalid/v1", "test", 1), "test", 0)
        try:
            with patch("app.services.cloud_models._post", return_value=(client, response)):
                return list(llm.stream([]))
        finally:
            response.close.assert_called_once()
            client.close.assert_called_once()

    def test_http_200_error_event_raises(self):
        with self.assertRaises(CloudModelError):
            self.read(['data: {"error":{"message":"failed"}}'])

    def test_incomplete_stream_raises(self):
        with self.assertRaises(CloudModelError):
            self.read(['data: {"choices":[{"delta":{"content":"partial"}}]}'])

    def test_completed_stream(self):
        chunks = self.read(['data: {"choices":[{"delta":{"content":"ok"}}]}', 'data: [DONE]'])
        self.assertEqual("".join(chunk.content for chunk in chunks), "ok")

    def test_malformed_stream_raises(self):
        with self.assertRaises(CloudModelError):
            self.read(['data: not-json'])
