"""多轮追问在检索、生成、SSE 和持久化之间的契约；无数据库/网络。"""
import asyncio
import json
import unittest
import uuid
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.fake_turns import mock_turns

from fastapi import Request
from langchain_core.documents import Document as LCDocument

from app.api.v1 import chat
from app.core.config import settings
from app.schemas.chat import ChatRequest
from app.services import followup, rag


def source(content="盗窃罪的未成年人量刑规定", **metadata):
    return (LCDocument(page_content=content, metadata={
        "document_id": str(uuid.uuid4()), "filename": "law.txt", "chunk_index": 0,
        "index_version": 2, **metadata,
    }), 0.1)


class FollowupApiTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid.uuid4(), username="context_test")
        self.kb = SimpleNamespace(id=uuid.uuid4(), collection_name="context_test", name="法律资料")
        self.request = Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": []})
        self.previous_source = source()
        citation = {**rag.to_citations([self.previous_source])[0], "source_index": 3}
        self.history = [
            SimpleNamespace(id=uuid.uuid4(), role="user", content="盗窃罪如何量刑？", citations=None),
            SimpleNamespace(id=uuid.uuid4(), role="assistant", content="量刑取决于具体条件[3]。", citations=[citation]),
        ]

    def stream(self, question, *, history=None, mode="rag", docs=None, patches=(), real_answer=False):
        conv = SimpleNamespace(id=uuid.uuid4(), kb_id=self.kb.id if mode == "rag" else None, mode=mode)
        db, writer = MagicMock(), MagicMock()
        db.scalars.return_value.all.return_value = list(reversed(self.history if history is None else history))
        writer.get.return_value = self.user

        async def consume(response):
            return [frame async for frame in response.body_iterator]

        with ExitStack() as stack:
            mock_turns(stack, conv.id, writer)
            for patcher in patches:
                stack.enter_context(patcher)
            stack.enter_context(patch.object(chat, "_get_conversation", return_value=conv))
            stack.enter_context(patch.object(chat, "_get_kb", return_value=self.kb))
            retrieve = stack.enter_context(patch.object(rag, "retrieve", return_value=[source()] if docs is None else docs))
            load_sources = stack.enter_context(patch.object(chat, "load_ready_citations", return_value=[self.previous_source]))
            answer = None if real_answer else stack.enter_context(patch.object(rag, "answer_stream", return_value=iter(["本轮说明[1]。"])))
            stack.enter_context(patch.object(chat, "SessionLocal", return_value=writer))
            audit = stack.enter_context(patch.object(chat, "write_audit"))
            stack.enter_context(patch.object(chat.logger, "exception"))
            response = chat.chat_stream(ChatRequest(question=question, conversation_id=conv.id), self.request, db, self.user)
            frames = asyncio.run(consume(response))
        events = [(frame.split("\n", 1)[0].removeprefix("event: "),
                   json.loads(frame.split("data: ", 1)[1])) for frame in frames]
        return SimpleNamespace(events=events, db=db, writer=writer, retrieve=retrieve,
                               load_sources=load_sources, answer=answer, audit=audit)

    def test_rewrite_changes_retrieval_only_and_keeps_original_user_question(self):
        query = "未成年人犯盗窃罪的量刑规定是什么？"
        with patch.object(followup, "complete_short_chat", return_value=json.dumps({"action": "search", "query": query}, ensure_ascii=False)) as rewrite:
            result = self.stream("那未成年人呢？")
        rewrite.assert_called_once()
        result.retrieve.assert_called_once_with(self.kb.collection_name, query, settings.RETRIEVAL_TOP_K)
        self.assertEqual(result.answer.call_args.args[0], "那未成年人呢？")
        self.assertEqual(result.db.add.call_args.args[0].content, "那未成年人呢？")
        prepared = result.answer.call_args.kwargs["prepared"]
        self.assertIn("【用户问题】\n那未成年人呢？", prepared.messages[-1].content)
        self.assertIn(query, prepared.messages[-1].content)
        self.assertEqual(result.events[0][1]["citations"], [])
        self.assertEqual(result.events[-1][0], "done")
        self.assertEqual(result.writer.add.call_args.args[0].citations, result.events[-1][1]["citations"])
        self.assertEqual(result.audit.call_args.args[-1]["retrieval_status"], "rewritten")

    def test_rewrite_timeout_falls_back_to_original_query_and_still_completes(self):
        with patch.object(followup, "complete_short_chat", side_effect=TimeoutError), patch.object(followup.logger, "warning"):
            result = self.stream("那未成年人呢？")
        self.assertEqual(result.retrieve.call_args.args[1], "那未成年人呢？")
        self.assertEqual(result.audit.call_args.args[-1]["retrieval_status"], "fallback")
        self.assertEqual(result.events[-1][0], "done")

    def test_ambiguous_followup_is_saved_as_clarification_without_retrieval_or_answer_model(self):
        reply = "您指的是哪一种量刑条件？"
        with patch.object(followup, "complete_short_chat", return_value=json.dumps({"action": "clarify", "question": reply}, ensure_ascii=False)):
            result = self.stream("那种情况呢？")
        result.retrieve.assert_not_called()
        result.load_sources.assert_not_called()
        result.answer.assert_not_called()
        self.assertEqual(result.writer.add.call_args.args[0].content, reply)
        self.assertIsNone(result.writer.add.call_args.args[0].citations)
        self.assertEqual(result.events[-1][1]["citations"], [])

    def test_followup_without_a_complete_turn_asks_for_context(self):
        with patch.object(followup, "complete_short_chat") as rewrite:
            result = self.stream("那未成年人呢？", history=[self.history[0]])
        rewrite.assert_not_called()
        result.retrieve.assert_not_called()
        result.answer.assert_not_called()
        self.assertEqual(result.events[-1][0], "done")

    def test_plain_chat_bypasses_both_rewrite_and_retrieval(self):
        with patch.object(followup, "plan_retrieval") as plan:
            result = self.stream("那未成年人呢？", mode="chat")
        plan.assert_not_called()
        result.retrieve.assert_not_called()
        result.load_sources.assert_not_called()
        self.assertEqual(result.answer.call_args.kwargs["prepared"].messages[-1].content, "那未成年人呢？")
        self.assertEqual(result.events[-1][1]["citations"], [])

    def test_explicit_previous_source_loads_verified_original_instead_of_vector_search(self):
        with patch.object(followup, "complete_short_chat") as rewrite:
            result = self.stream("解释刚才第3条来源")
        rewrite.assert_not_called()
        result.retrieve.assert_not_called()
        result.load_sources.assert_called_once()
        requested = result.load_sources.call_args.args[1][0]
        self.assertEqual(requested["source_index"], 3)
        self.assertEqual(requested["source_message_id"], str(self.history[1].id))
        self.assertEqual(result.events[-1][0], "done")

    def test_stale_previous_source_clarifies_without_using_an_unrelated_search_hit(self):
        plan = followup.RetrievalPlan("解释刚才第3条来源", "reference", citations=[self.history[1].citations[0], self.history[1].citations[0]])
        with patch.object(followup, "plan_retrieval", return_value=plan):
            # 模拟两条指定来源中只核验到一条；不能静默忽略另一条。
            result = self.stream("解释刚才第3条来源")
        result.retrieve.assert_not_called()
        result.answer.assert_not_called()
        self.assertIn("无法核验", result.writer.add.call_args.args[0].content)
        self.assertEqual(result.events[-1][1]["citations"], [])

    def test_documents_excluded_by_budget_cannot_become_final_citations(self):
        docs = [source("资料" * 10000, filename=f"law-{index}.txt") for index in range(6)]
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 128), \
             patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 64), patch.object(rag, "get_llm") as model:
            model.return_value.stream.return_value = [SimpleNamespace(content="内容见[6]。")]
            result = self.stream("盗窃罪如何量刑？", docs=docs, history=[], real_answer=True)
        self.assertEqual(result.events[-1][0], "done")
        self.assertEqual(result.events[-1][1]["citations"], [])
        self.assertIsNone(result.writer.add.call_args.args[0].citations)

    def test_empty_retrieval_refuses_without_calling_generation(self):
        with patch.object(rag, "get_llm") as model:
            result = self.stream("盗窃罪如何量刑？", docs=[], history=[], real_answer=True)
        model.assert_not_called()
        self.assertEqual(result.events[-1][0], "done")
        self.assertTrue(result.writer.add.call_args.args[0].content.startswith("根据现有知识库无法回答"))

    def test_oversized_current_question_returns_safe_error_before_generation(self):
        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 128):
            result = self.stream("很长的问题" * 500, mode="chat")
        result.answer.assert_not_called()
        result.writer.add.assert_not_called()
        self.assertEqual(result.events[-1][0], "error")
        self.assertIn("上下文输入预算", result.events[-1][1]["message"])
