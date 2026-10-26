"""流式协议和思考内容过滤的回归测试。"""
import unittest
from unittest.mock import MagicMock, patch

from langchain_core.messages import HumanMessage

from app.core.config import settings
from app.services import rag
from app.services.cloud_models import CloudModelError, OpenAICompatibleChat, _Endpoint


class StreamTests(unittest.TestCase):
    def setUp(self):
        # 测试不能随开发者 .env 中的模型参数变化。
        for name, value in [("LLM_REASONING_EFFORT", ""), ("LLM_STREAM_INCLUDE_USAGE", False)]:
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_reasoning_effort_is_sent_only_when_configured(self):
        """DeepSeek 默认开思考且思考 token 计入 max_tokens，需能显式关掉。"""
        chat = OpenAICompatibleChat(_Endpoint("https://api.deepseek.com", "k", 5), "deepseek-flash", 0.2)
        messages = [HumanMessage(content="q")]
        with patch.object(settings, "LLM_REASONING_EFFORT", ""):
            self.assertNotIn("reasoning_effort", chat._payload(messages, True))
        with patch.object(settings, "LLM_REASONING_EFFORT", "none"):
            self.assertEqual(chat._payload(messages, True)["reasoning_effort"], "none")
            # 非流式同样生效，否则评测与补全仍会被思考拖慢。
            self.assertEqual(chat._payload(messages, False)["reasoning_effort"], "none")

    def test_deepseek_stream_sends_disabled_reasoning_to_the_transport(self):
        endpoint = _Endpoint("https://api.deepseek.com", "test-only", 5)
        chat = OpenAICompatibleChat(endpoint, "deepseek-flash", 0.2)
        client, response = MagicMock(), MagicMock()
        response.iter_lines.return_value = ['data: {"choices":[{"delta":{"content":"正文"}}]}', 'data: [DONE]']
        with patch.object(settings, "LLM_REASONING_EFFORT", "none"), \
             patch("app.services.cloud_models._post", return_value=(client, response)) as request:
            self.assertEqual("".join(chunk.content for chunk in chat.stream([HumanMessage(content="q")])), "正文")
        payload = request.call_args.args[2]
        self.assertEqual(request.call_args.args[:2], (endpoint, "/chat/completions"))
        self.assertEqual(request.call_args.kwargs, {"stream": True})
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertIs(payload["stream"], True)
        self.assertNotIn("enable_thinking", payload)
        client.close.assert_called_once()
        response.close.assert_called_once()

    def test_deepseek_non_streaming_uses_same_setting_and_only_returns_content(self):
        chat = OpenAICompatibleChat(_Endpoint("https://api.deepseek.com/v1", "test-only", 5), "deepseek-flash", 0.2)
        response = MagicMock()
        response.json.return_value = {"choices": [{"message": {"reasoning_content": "隐藏的思考", "content": "正文"}}]}
        with patch.object(settings, "LLM_REASONING_EFFORT", "none"), \
             patch.object(settings, "LLM_STREAM_INCLUDE_USAGE", True), \
             patch("app.services.cloud_models._post", return_value=response) as request:
            self.assertEqual(chat.invoke([HumanMessage(content="q")]).content, "正文")
        payload = request.call_args.args[2]
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertIs(payload["stream"], False)
        self.assertNotIn("enable_thinking", payload)
        self.assertNotIn("stream_options", payload)

    def test_unconfigured_other_services_keep_their_existing_parameters(self):
        endpoint = _Endpoint("https://example.invalid/v1", "test-only", 5)
        for model in ("generic-chat", "Qwen2.5-test", "Qwen3-test"):
            with self.subTest(model=model):
                payload = OpenAICompatibleChat(endpoint, model, 0.2)._payload([], True)
                self.assertNotIn("reasoning_effort", payload)
                self.assertNotIn("thinking", payload)
                if "Qwen3" in model:
                    self.assertIs(payload["enable_thinking"], False)
                else:
                    self.assertNotIn("enable_thinking", payload)

    def test_reasoning_content_is_not_returned_as_answer(self):
        """思考字段不能混进正文，否则前端会显示模型的内部推理。"""
        chunks = self.read(['data: {"choices":[{"delta":{"reasoning_content":"思考"}}]}',
                            'data: {"choices":[{"delta":{"content":"正文"}}]}', 'data: [DONE]'])
        self.assertEqual("".join(chunk.content for chunk in chunks), "正文")
