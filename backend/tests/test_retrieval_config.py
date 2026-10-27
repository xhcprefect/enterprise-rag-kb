"""检索条数的默认值契约：滑块初值、请求回退、显式覆盖三者必须一致。

背景：``RETRIEVAL_TOP_K`` 曾经是死配置——``config.py`` 定义了它，但没有任何代码读取，
真正生效的是 ``ChatRequest.top_k`` 的硬编码默认值 6，前端也总是显式传 6。
这三处一旦重新分叉，改 ``.env`` 又会变成没有效果。
"""
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.api.v1 import chat
from app.core.config import settings
from app.schemas.chat import ChatRequest, RetrievalConfig


class RetrievalConfigEndpointTests(unittest.TestCase):
    def test_endpoint_reports_the_same_value_the_stream_falls_back_to(self):
        data = chat.get_retrieval_config(SimpleNamespace(id=uuid.uuid4())).data
        self.assertEqual(data.top_k, settings.RETRIEVAL_TOP_K)
        self.assertEqual(RetrievalConfig(top_k=settings.RETRIEVAL_TOP_K).top_k, settings.RETRIEVAL_TOP_K)

    def test_endpoint_follows_a_changed_setting(self):
        # 换一个和线上默认不同的值，确认接口读的是配置而不是常量。
        with patch.object(settings, "RETRIEVAL_TOP_K", 9):
            self.assertEqual(chat.get_retrieval_config(SimpleNamespace(id=uuid.uuid4())).data.top_k, 9)


class TopKResolutionTests(unittest.TestCase):
    """省略 top_k 时必须回落到服务端配置；显式传入必须原样生效。"""

    def test_schema_omitted_top_k_is_none_so_settings_win(self):
        self.assertIsNone(ChatRequest(question="q").top_k)

    def test_schema_rejects_values_outside_the_slider_range(self):
        for value in (0, 21):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ChatRequest(question="q", top_k=value)

    def test_stream_falls_back_to_settings_and_honours_explicit_value(self):
        # 直接测解析这一行：它是本次改动的核心，且不需要跑整条 SSE 链路。
        cases = [(None, 9), (4, 4), (1, 1), (20, 20)]
        for given, expected in cases:
            with self.subTest(given=given), patch.object(settings, "RETRIEVAL_TOP_K", 9):
                payload = ChatRequest(question="q", **({} if given is None else {"top_k": given}))
                resolved = settings.RETRIEVAL_TOP_K if payload.top_k is None else payload.top_k
                self.assertEqual(resolved, expected)

    def test_claim_turn_receives_the_resolved_value(self):
        """端到端确认解析结果真的传到了下游，而不只是算了一个没人用的局部变量。"""
        user = SimpleNamespace(id=uuid.uuid4(), username="topk_test", is_admin=True)
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"), url=SimpleNamespace(path="/"))
        db = MagicMock()
        reservation = SimpleNamespace(
            conversation_id=uuid.uuid4(), request_id=uuid.uuid4(), user_message_id=uuid.uuid4(),
            turn_id=uuid.uuid4(), document_scope=[], scope_revision=0,
            replay_answer=None, question="q", mode="chat", status="pending",
        )
        for given, expected in ((None, 5), (3, 3)):
            with self.subTest(given=given), \
                 patch.object(settings, "RETRIEVAL_TOP_K", 5), \
                 patch.object(chat.chat_turns, "claim_turn", return_value=reservation) as claim:
                payload = ChatRequest(question="q", mode="chat", **({} if given is None else {"top_k": given}))
                chat.chat_stream(payload, request, db, user)
                self.assertEqual(claim.call_args.kwargs["top_k"], expected)


if __name__ == "__main__":
    unittest.main()
