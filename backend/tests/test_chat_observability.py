"""chat 生成生命周期与进程内指标的**真实链路**回归。

断言的是 `app.api.v1.chat.chat_stream` 里真实的 `start_chat` / `finish` 调用，
不是手工构造观测对象：

- 已完成请求的重放不新开一次生成（不增加 started/in_flight）；
- 历史消息加载失败 → 恰好一次 failed；
- 响应体从未被消费就收口 → 恰好一次 cancelled；
- 客户端断连 → 恰好一次收口，gauge 归零；
- 模型停滞时显式取消 → cancelled，且不会写回回答；
- 正常完成 → completed；首字只由真正的非空白正文触发；
- 阶段标签只来自固定集合，`trace` 里的其他键不会变成阶段。
"""
import asyncio
import json
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Request
from langchain_core.documents import Document as LCDocument
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from tests._env import TEST_URL
from tests.test_database import _bind_test_engine

_bind_test_engine()

from app.api.v1 import chat
from app.core.config import settings
from app.core.observability import CHAT_STAGES, metrics
from app.core.security import create_access_token
from app.db.init_db import init_db
from app.db.session import SessionLocal, engine
from app.models import ChatTurn, Conversation, KnowledgeBase, Message, User
from app.schemas.chat import ChatRequest
from app.services import chat_turns, followup, rag


def _series(text: str) -> dict[str, float]:
    """把 render() 输出拆成 {序列名: 值}；直方图桶也算普通序列。"""
    values: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        values[name] = float(value)
    return values


def _chat_counts() -> tuple[dict, dict]:
    snapshot = metrics.snapshot()["chat"]
    return snapshot["started"], snapshot["finished"]


def _finished_total() -> int:
    return sum(metrics.snapshot()["chat"]["finished"].values())


class _HistoryBrokenSession:
    """代理真实会话，只在读取历史消息时失败。"""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def scalars(self, *_args, **_kwargs):
        raise RuntimeError("synthetic history failure")


def _source(content="合成资料正文", **metadata):
    return (LCDocument(page_content=content, metadata={
        "document_id": str(uuid.uuid4()), "filename": "observability.md", "chunk_index": 0,
        "index_version": 1, **metadata,
    }), 0.1)


class _Generator:
    """测试用异步生成器：记录是否被关闭、吐了几个 token，便于断言上游没有泄漏。"""

    def __init__(self, tokens, *, closes, gate=None, delay=0.0):
        self.tokens = tokens
        self.closes = closes
        self.gate = gate
        self.delay = delay
        self.produced = 0

    async def generate(self, *_args, **_kwargs):
        try:
            if self.gate is not None:
                self.gate.set()
                await asyncio.sleep(10)
            for token in self.tokens:
                self.produced += 1
                yield token
                if self.delay:
                    # 断连要发生在**推流中间**：首块之前就断开的话，上游根本没启动，
                    # "生成器被关闭"这件事就无从谈起。
                    await asyncio.sleep(self.delay)
        finally:
            self.closes.append(True)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离的生成生命周期指标测试")
class ChatObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL) or not engine.url.database.endswith("_test"):
            raise RuntimeError("拒绝在业务库运行生成指标回归")
        init_db(create_admin=False)
        with patch("app.core.logging.setup_logging"):
            from app.main import app

        cls.app = app

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        metrics.reset()
        with engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, conversations, chat_turns, messages, audit_logs, model_usage "
                "RESTART IDENTITY CASCADE"
            ))
        with SessionLocal() as db:
            self.user = User(username="obs_user", role="user", is_active=True, password_hash="test-only")
            db.add(self.user)
            db.flush()
            self.kb = KnowledgeBase(name="obs_kb", collection_name="obs_" + uuid.uuid4().hex,
                                    is_public=True, owner_id=self.user.id)
            db.add(self.kb)
            db.commit()
            self.user_id, self.kb_id = self.user.id, self.kb.id
        self.headers = {"Authorization": "Bearer " + create_access_token(str(self.user_id), "user")}
        self.request = Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": []})
        self.request_id = uuid.uuid4()

    def tearDown(self):
        metrics.reset()

    # ---------- helpers ----------

    def payload(self, **overrides):
        body = {"question": "合成问题", "mode": "chat", "request_id": str(self.request_id)}
        body.update(overrides)
        return body

    def post(self, **overrides):
        from fastapi.testclient import TestClient

        return TestClient(self.app).post("/api/v1/chat/stream", headers=self.headers, json=self.payload(**overrides))

    def run_stream(self, payload):
        """在真实会话里调用路由并消费整个响应体。"""
        async def consume():
            with SessionLocal() as db:
                response = chat.chat_stream(ChatRequest(**payload), self.request, db, self.user)
                return "".join([frame async for frame in response.body_iterator])

        return asyncio.run(consume())

    def turns(self):
        with SessionLocal() as db:
            return db.scalars(select(ChatTurn)).all()

    # ---------- 重放 ----------

    def test_a_completed_replay_does_not_start_another_generation(self):
        async def generate(*_args, **_kwargs):
            yield "合成回答正文"

        with patch.object(rag, "answer_astream", side_effect=generate), \
             patch.object(rag, "prepare_answer", return_value=SimpleNamespace(pairs=[], input_tokens=1, input_budget=10)):
            first = self.post()
            self.assertIn("event: done", first.text)
            started_after_generation = dict(metrics.snapshot()["chat"]["started"])
            finished_after_generation = dict(metrics.snapshot()["chat"]["finished"])
            # 同一个 request_id 再来一次：走重放分支，不能再调用模型
            with patch.object(rag, "answer_astream") as model:
                replay = self.post()
        self.assertIn('"replayed": true', replay.text)
        model.assert_not_called()
        # 重放不是一次新生成：started/in_flight/finished 都不变
        self.assertEqual(metrics.snapshot()["chat"]["started"], started_after_generation)
        self.assertEqual(metrics.snapshot()["chat"]["finished"], finished_after_generation)
        self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"chat": 0})
        self.assertEqual(started_after_generation, {"chat": 1})
        self.assertEqual(finished_after_generation, {"chat:completed": 1})

    # ---------- 失败 ----------

    def test_history_load_failure_finishes_failed_exactly_once(self):
        with SessionLocal() as real:
            wrapped = _HistoryBrokenSession(real)
            with self.assertRaises(RuntimeError):
                chat.chat_stream(ChatRequest(**self.payload()), self.request, wrapped, self.user)
        self.assertEqual(metrics.snapshot()["chat"]["started"], {"chat": 1})
        self.assertEqual(metrics.snapshot()["chat"]["finished"], {"chat:failed": 1})
        self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"chat": 0})
        self.assertEqual(_finished_total(), 1)
        turns = self.turns()
        self.assertEqual([turn.status for turn in turns], ["failed"])
        with SessionLocal() as db:
            self.assertIsNone(db.get(Conversation, turns[0].conversation_id).active_turn_id)

    # ---------- 未消费 ----------

    def test_an_unconsumed_response_body_finishes_cancelled_exactly_once(self):
        with SessionLocal() as db:
            response = chat.chat_stream(ChatRequest(**self.payload()), self.request, db, self.user)
            # 观测在流式主体之前创建：还没消费任何事件时 gauge 已经打开。
            self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"chat": 1})
            self.assertEqual(_finished_total(), 0)

            async def close_without_consuming():
                # 与 ManagedChatResponse 的收尾一致：先关掉从未启动的迭代器，再跑清理。
                await response.body_iterator.aclose()

            asyncio.run(close_without_consuming())
            response.cleanup()
            # 重复收尾（例如 finally 再跑一次）不会再记一次
            response.cleanup()
        self.assertEqual(metrics.snapshot()["chat"]["started"], {"chat": 1})
        self.assertEqual(metrics.snapshot()["chat"]["finished"], {"chat:cancelled": 1})
        self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"chat": 0})
        self.assertEqual(_finished_total(), 1)

    def test_client_disconnect_closes_the_generation_once(self):
        closes: list[bool] = []
        # 多吐几个带延迟的 token：断连必须发生在推流**中间**，才能证明上游被关闭。
        generator = _Generator(["合成", "回答", "正文", "尾部"], closes=closes, delay=0.02)
        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        received = 0

        async def receive():
            nonlocal received
            received += 1
            if received == 1:
                return {"type": "http.request", "body": b"", "more_body": False}
            # 先让几块 SSE 真正发出去，再由 ASGI 2.0 的 receive 送断开。
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        async def disconnect():
            with SessionLocal() as db, patch.object(rag, "answer_astream", side_effect=generator.generate), \
                 patch.object(rag, "prepare_answer",
                              return_value=SimpleNamespace(pairs=[], input_tokens=1, input_budget=10)):
                response = chat.chat_stream(ChatRequest(**self.payload()), self.request, db, self.user)
                # 默认 scope 没有 asgi 扩展 → spec_version 2.0 的断连检测路径。
                await response({"type": "http", "method": "POST", "path": "/api/v1/chat/stream",
                                "headers": [], "query_string": b"", "root_path": ""}, receive, send)

        asyncio.run(disconnect())
        self.assertEqual(metrics.snapshot()["chat"]["started"], {"chat": 1})
        self.assertEqual(metrics.snapshot()["chat"]["finished"], {"chat:cancelled": 1})
        self.assertEqual(_finished_total(), 1, "断连必须只收口一次")
        self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"chat": 0})
        # 断开前确实推流过；上游生成器被关闭；且没有把剩下的 token 全部产完。
        body_frames = [m for m in sent if m["type"] == "http.response.body"]
        self.assertTrue(body_frames, "断连前应当已经推出过响应体")
        self.assertEqual(closes, [True], "断连必须关闭上游模型生成器")
        self.assertLess(generator.produced, len(generator.tokens),
                        "断连后不应继续把整段回答生成完")
        self.assertEqual(self.turns()[0].status, "cancelled")

    # ---------- 取消 ----------

    def test_explicit_cancel_while_the_model_is_stalled_finishes_cancelled_once(self):
        closes: list[bool] = []
        gate = asyncio.Event()
        generator = _Generator(["late answer"], closes=closes, gate=gate)

        async def exercise():
            with SessionLocal() as db, patch.object(rag, "answer_astream", side_effect=generator.generate), \
                 patch.object(rag, "prepare_answer",
                              return_value=SimpleNamespace(pairs=[], input_tokens=1, input_budget=10)), \
                 patch.object(settings, "CHAT_STREAM_CANCEL_POLL_SECONDS", 0.01):
                response = chat.chat_stream(ChatRequest(**self.payload()), self.request, db, self.user)

                async def consume():
                    return "".join([frame async for frame in response.body_iterator])

                task = asyncio.create_task(consume())
                await asyncio.wait_for(gate.wait(), 2)

                def cancel_from_another_process():
                    with SessionLocal() as other_db:
                        chat_turns.cancel_request(other_db, self.request_id, self.user_id)

                await asyncio.to_thread(cancel_from_another_process)
                return await asyncio.wait_for(task, 5)

        output = asyncio.run(exercise())
        self.assertIn("event: error", output)
        self.assertNotIn("event: done", output)
        self.assertEqual(closes, [True], "模型生成器必须被关闭")
        self.assertEqual(metrics.snapshot()["chat"]["started"], {"chat": 1})
        self.assertEqual(metrics.snapshot()["chat"]["finished"], {"chat:cancelled": 1})
        self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"chat": 0})
        self.assertEqual(_finished_total(), 1)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Message)
                                       .where(Message.role == "assistant")), 0)
            self.assertEqual(self.turns()[0].status, "cancelled")

    # ---------- 完成 / 首字 / 阶段 ----------

    def test_normal_done_finishes_completed_and_only_real_text_counts_as_first_token(self):
        async def generate(*_args, **_kwargs):
            # 第一个 token 只有空白：不能算首字；真正的首字只记一次。
            yield "   \n"
            yield "合成回答正文"

        with patch.object(rag, "answer_astream", side_effect=generate), \
             patch.object(rag, "prepare_answer", return_value=SimpleNamespace(pairs=[], input_tokens=3, input_budget=10)):
            result = self.post()
        self.assertIn("event: done", result.text)
        self.assertEqual(metrics.snapshot()["chat"]["started"], {"chat": 1})
        self.assertEqual(metrics.snapshot()["chat"]["finished"], {"chat:completed": 1})
        self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"chat": 0})
        text = metrics.render()
        values = _series(text)
        self.assertEqual(values['ragkb_chat_first_token_seconds_count{mode="chat"}'], 1.0)
        self.assertEqual(values['ragkb_chat_total_seconds_count{mode="chat"}'], 1.0)
        # chat 模式没有检索/改写阶段，且阶段标签必须都在固定集合内
        stages = {name.split('stage="', 1)[1].split('"', 1)[0]
                  for name in values if name.startswith("ragkb_chat_stage_seconds_count")}
        self.assertTrue(stages <= set(CHAT_STAGES), stages)
        self.assertNotIn("retrieval", stages)
        self.assertEqual(metrics.snapshot()["chat"]["finished"], {"chat:completed": 1})

    def test_rag_stages_use_the_fixed_vocabulary_and_map_retrieval(self):
        docs = [_source("合成资料正文[1]")]

        async def generate(*_args, **_kwargs):
            yield "依据资料，结论如下[1]。"

        with patch.object(followup, "plan_retrieval", return_value=followup.RetrievalPlan("合成问题")), \
             patch.object(rag, "retrieve", return_value=docs) as retrieve, \
             patch.object(rag, "prepare_answer",
                          return_value=SimpleNamespace(pairs=docs, input_tokens=7, input_budget=100)), \
             patch.object(rag, "answer_astream", side_effect=generate):
            result = self.post(mode="rag", kb_id=str(self.kb_id))
        self.assertIn("event: done", result.text)
        retrieve.assert_called_once()
        self.assertEqual(metrics.snapshot()["chat"]["started"], {"rag": 1})
        self.assertEqual(metrics.snapshot()["chat"]["finished"], {"rag:completed": 1})
        self.assertEqual(metrics.snapshot()["chat"]["in_flight"], {"rag": 0})
        values = _series(metrics.render())
        stages = {name.split('stage="', 1)[1].split('"', 1)[0]
                  for name in values if name.startswith("ragkb_chat_stage_seconds_count")}
        # trace 只取 retrieve_ms 对应 retrieval；其余键不得成为标签
        self.assertEqual(stages, {"rewrite", "retrieval", "prompt", "generation", "persistence"})
        self.assertTrue(stages <= set(CHAT_STAGES))
        for stage in stages:
            self.assertEqual(values[f'ragkb_chat_stage_seconds_count{{mode="rag",stage="{stage}"}}'], 1.0)
        self.assertNotIn('stage="other"', metrics.render())


if __name__ == "__main__":
    unittest.main()
