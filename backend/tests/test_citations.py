"""最终引用与检索候选分离；不依赖数据库或真实模型。"""
import asyncio
import json
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.fake_turns import mock_turns

from fastapi import Request
from langchain_core.documents import Document as LCDocument

from app.api.v1 import chat
from app.schemas.chat import ChatRequest
from app.services import chat_turns, rag
from app.services.cloud_models import CloudModelError


def documents():
    return [
        (LCDocument(page_content=f"刑法资料 {index}", metadata={
            "document_id": str(index), "filename": f"law-{index}.txt", "chunk_index": index - 1,
        }), 0.1 * index)
        for index in range(1, 7)
    ]


class CitationSelectionTests(unittest.TestCase):
    def setUp(self):
        self.candidates = rag.to_citations(documents())

    def test_candidate_numbers_match_context(self):
        self.assertEqual([item["source_index"] for item in self.candidates], list(range(1, 7)))
        self.assertEqual(self.candidates[0]["score"], 0.9)
        self.assertIn("[3] 来源：law-3.txt", rag.build_context(documents()))

    def test_screenshot_refusal_does_not_publish_candidates(self):
        answer = "根据现有知识库无法回答。现有资料均为法律条文内容，未包含百度网址相关信息。"
        self.assertEqual(rag.finalize_citations(answer, self.candidates), [])

    def test_explicit_refusal_overrides_numbers_mentioned_in_its_explanation(self):
        for prefix in ["", "抱歉，", "**", "> "]:
            with self.subTest(prefix=prefix):
                answer = prefix + "根据现有知识库无法回答。资料[1]和[3]只涉及法律条文。"
                self.assertEqual(rag.finalize_citations(answer, self.candidates), [])

    def test_only_used_candidates_are_returned_without_renumbering(self):
        result = rag.finalize_citations("第一条依据[3]，另一条依据[1]，再次引用[3]。", self.candidates)
        self.assertEqual([item["source_index"] for item in result], [1, 3])
        self.assertEqual([item["filename"] for item in result], ["law-1.txt", "law-3.txt"])
        self.assertEqual(len(self.candidates), 6)

    def test_uncited_empty_and_unknown_sources_are_not_evidence(self):
        for answer in ["", " ", "这是不带来源的回答。", "来源[0][7][9999]不存在。"]:
            with self.subTest(answer=answer):
                self.assertEqual(rag.finalize_citations(answer, self.candidates), [])
        self.assertEqual(rag.finalize_citations("回答[1]", []), [])

    def test_thinking_code_links_and_escaped_brackets_are_not_citations(self):
        answer = (
            "<think>内部推理[1]</think>"
            "示例 `[2]`，链接 [3](https://example.com)，转义 \\[4]。\n"
            "```python\narray = [5]\n```\n"
            "~~~text\n[6]\n~~~~\n"
            "[1]: https://example.com\n"
            "正文没有采用资料。"
        )
        self.assertEqual(rag.finalize_citations(answer, self.candidates), [])
        self.assertEqual(rag.finalize_citations("```text\n[1]\n未闭合代码块[2]", self.candidates), [])

    def test_citations_after_code_are_preserved(self):
        result = rag.finalize_citations("```python\nprint([1])\n```\n实际依据[3]。", self.candidates)
        self.assertEqual([item["source_index"] for item in result], [3])

    def test_legacy_candidates_and_filtered_history_are_idempotent(self):
        legacy = [{key: value for key, value in item.items() if key != "source_index"} for item in self.candidates]
        result = rag.finalize_citations("回答[3]。", legacy)
        self.assertEqual(result[0]["source_index"], 3)
        self.assertEqual(rag.finalize_citations("回答[3]。", result), result)
        self.assertNotIn("source_index", legacy[2])


class CitationApiTests(unittest.TestCase):
    def setUp(self):
        self.user = SimpleNamespace(id=uuid.uuid4(), username="citation_test")
        self.kb = SimpleNamespace(id=uuid.uuid4(), collection_name="citation_test", name="法律资料")
        self.request = Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": []})
        self.candidates = rag.to_citations(documents())

    def stream(self, parts, mode="rag"):
        conv = SimpleNamespace(id=uuid.uuid4(), kb_id=self.kb.id if mode == "rag" else None, mode=mode)
        db, writer = MagicMock(), MagicMock()
        db.scalars.return_value.all.return_value = []
        writer.get.return_value = self.user
        async def consume(response):
            return [frame async for frame in response.body_iterator]
        with ExitStack() as stack:
            mock_turns(stack, conv.id, writer)
            stack.enter_context(patch.object(chat, "_get_conversation", return_value=conv))
            stack.enter_context(patch.object(chat, "_get_kb", return_value=self.kb))
            retrieval = stack.enter_context(patch.object(rag, "retrieve", return_value=documents()))
            stack.enter_context(patch.object(rag, "answer_stream", return_value=iter(parts)))
            stack.enter_context(patch.object(chat, "SessionLocal", return_value=writer))
            stack.enter_context(patch.object(chat, "write_audit"))
            stack.enter_context(patch.object(chat.logger, "exception"))
            response = chat.chat_stream(ChatRequest(question="百度网址是啥", conversation_id=conv.id), self.request, db, self.user)
            frames = asyncio.run(consume(response))
        events = []
        for frame in frames:
            event, data = frame.strip().split("\n", 1)
            events.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
        return events, writer, retrieval

    def test_refusal_sends_and_persists_no_citations(self):
        events, writer, _ = self.stream(["根据现有知识库", "无法回答，资料[1]不是网址信息。"])
        self.assertEqual(events[0][0], "meta")
        self.assertEqual(events[0][1]["citations"], [])
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["citations"], [])
        self.assertIsNone(writer.add.call_args.args[0].citations)
        writer.commit.assert_called_once()

    def test_final_event_and_saved_message_have_the_same_used_sources(self):
        events, writer, _ = self.stream(["法律规定", "见[3]。"])
        self.assertEqual(events[0][1]["citations"], [])
        final = events[-1][1]["citations"]
        self.assertEqual([item["source_index"] for item in final], [3])
        self.assertEqual(final, writer.add.call_args.args[0].citations)
        self.assertEqual(writer.add.call_args.args[0].content, "法律规定见[3]。")

    def test_plain_chat_never_retrieves_or_publishes_sources(self):
        events, writer, retrieval = self.stream(["普通对话中的数字[1]不是知识库引用。"], mode="chat")
        retrieval.assert_not_called()
        self.assertEqual(events[-1][1]["citations"], [])
        self.assertIsNone(writer.add.call_args.args[0].citations)

    def test_interrupted_generation_does_not_publish_or_save_candidates(self):
        def broken():
            yield "部分回答[1]"
            raise CloudModelError("simulated interruption")
        events, writer, _ = self.stream(broken())
        self.assertEqual(events[0][1]["citations"], [])
        self.assertEqual(events[-1][0], "error")
        self.assertNotIn("done", [event for event, _ in events])
        writer.add.assert_not_called()

    def test_user_cancellation_is_logged_without_a_failure_traceback(self):
        """点「停止」后模型才返回是预期路径；不能每次都留下 ERROR 级 traceback。"""
        def cancelled(*_args, **_kwargs):
            raise chat_turns.ChatLeaseLost("该回答已取消或处理租约已失效，请刷新会话后重试")

        conv = SimpleNamespace(id=uuid.uuid4(), kb_id=self.kb.id, mode="rag")
        db, writer = MagicMock(), MagicMock()
        db.scalars.return_value.all.return_value = []
        writer.get.return_value = self.user

        async def consume(response):
            return [frame async for frame in response.body_iterator]

        with ExitStack() as stack:
            mock_turns(stack, conv.id, writer)
            stack.enter_context(patch.object(chat_turns, "complete_turn", side_effect=cancelled))
            stack.enter_context(patch.object(chat, "_get_conversation", return_value=conv))
            stack.enter_context(patch.object(chat, "_get_kb", return_value=self.kb))
            stack.enter_context(patch.object(rag, "retrieve", return_value=documents()))
            stack.enter_context(patch.object(rag, "answer_stream", return_value=iter(["回答[1]"])))
            stack.enter_context(patch.object(chat, "SessionLocal", return_value=writer))
            stack.enter_context(patch.object(chat, "write_audit"))
            failure = stack.enter_context(patch.object(chat.logger, "exception"))
            info = stack.enter_context(patch.object(chat.logger, "info"))
            response = chat.chat_stream(ChatRequest(question="百度网址是啥", conversation_id=conv.id), self.request, db, self.user)
            frames = asyncio.run(consume(response))

        failure.assert_not_called()
        self.assertTrue(any("问答已结束" in str(call.args[0]) for call in info.call_args_list))
        self.assertIn("event: error", "\n".join(frames))

    def history(self, answer, candidates, mode="rag"):
        now = datetime.now(timezone.utc)
        original = SimpleNamespace(
            id=uuid.uuid4(), kb_id=self.kb.id, mode=mode, title="history", created_at=now, updated_at=now,
            messages=[SimpleNamespace(id=uuid.uuid4(), role="assistant", content=answer, citations=candidates,
                                      latency_ms=1, created_at=now)],
        )
        with patch.object(chat, "_get_conversation", return_value=original):
            result = chat.get_conversation(original.id, MagicMock(), self.user)
        return result.data.messages[0], original.messages[0]

    def test_old_refusal_history_is_normalized_without_overwriting_the_database(self):
        result, original = self.history("根据现有知识库无法回答。", self.candidates)
        self.assertIsNone(result.citations)
        self.assertEqual(len(original.citations), 6)

    def test_old_and_new_history_keep_the_original_source_number(self):
        legacy = [{key: value for key, value in item.items() if key != "source_index"} for item in self.candidates]
        result, _ = self.history("回答[3]。", legacy)
        self.assertEqual([item.source_index for item in result.citations], [3])
        result, _ = self.history("回答[3]。", [self.candidates[2]])
        self.assertEqual([item.source_index for item in result.citations], [3])

    def test_plain_chat_history_cannot_inherit_knowledge_sources(self):
        result, _ = self.history("数字[1]。", self.candidates, mode="chat")
        self.assertIsNone(result.citations)
