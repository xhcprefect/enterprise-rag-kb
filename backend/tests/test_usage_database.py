"""真实 PostgreSQL 用量、计价、权限、历史补录和模型传输回归；仅 *_test 数据库。"""
import asyncio
import contextvars
import json
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from fastapi import HTTPException
from langchain_core.messages import HumanMessage
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import make_url

from tests._env import TEST_URL
from app.core.config import settings
from app.core.security import create_access_token
from app.db.init_db import init_db
from app.db.session import SessionLocal, engine
from app.models import ChatTurn, Conversation, Document, DocumentChunk, KnowledgeBase, ModelPrice, ModelUsage, User
from app.schemas.usage import PriceUpdate
from app.services import cloud_models, ingest, rag, usage_statistics as stats, usage_tracking as tracking, vector_store


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离用量数据库测试")
class UsageDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TEST_URL or not make_url(TEST_URL).database.endswith("_test") or engine.url != make_url(TEST_URL):
            raise RuntimeError("拒绝在业务库运行用量回归")
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        vector_store._store.cache_clear()
        engine.dispose()

    def setUp(self):
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE users, knowledge_bases, conversations, model_usage, model_prices RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            self.admin = User(username="usage_admin", role="admin", is_active=True, password_hash="unused-test-only")
            self.member = User(username="usage_member", role="user", is_active=True, password_hash="unused-test-only")
            self.other = User(username="usage_other", role="user", is_active=True, password_hash="unused-test-only")
            db.add_all([self.admin, self.member, self.other])
            db.flush()
            self.kb = KnowledgeBase(name="usage synthetic", collection_name="usage_" + uuid.uuid4().hex,
                                    owner_id=self.admin.id, is_public=True)
            db.add(self.kb)
            db.commit()
        self.at = datetime(2026, 6, 15, 8, tzinfo=timezone.utc)
        self.context = tracking.UsageContext(user_id=self.member.id, kb_id=self.kb.id)

    def event(self, *, user=None, stamp=None, incoming=1000, outgoing=100, priced=True, model="test-chat", status="succeeded"):
        values = {"input_tokens": incoming, "output_tokens": outgoing, "cached_input_tokens": None}
        price = {"input_per_million": Decimal(2), "output_per_million": Decimal(8)} if priced else {}
        input_cost, output_cost, cost = tracking.estimate_cost(values, price)
        with SessionLocal() as db:
            row = ModelUsage(user_id=(user or self.member).id, kb_id=self.kb.id, occurred_at=stamp or self.at,
                             provider="usage.test", model=model, kind="chat", operation="generation", status=status,
                             source="provider", currency="CNY", **values, **price,
                             input_cost=input_cost, output_cost=output_cost, estimated_cost=cost)
            db.add(row)
            db.commit()
            return row.id

    def price(self, incoming="2", outgoing="8", cached=None, revision=0, provider="usage.test", model="test-chat", kind="chat"):
        with SessionLocal() as db:
            return stats.update_price(db, PriceUpdate(provider=provider, model=model, kind=kind,
                                      input_per_million=incoming, output_per_million=outgoing,
                                      cached_input_per_million=cached, expected_revision=revision))

    def report(self, user=None, **options):
        with SessionLocal() as db:
            return stats.statistics(db, user or self.admin, start=date(2026, 6, 15), end=date(2026, 6, 16), **options)

    def test_totals_preserve_unknown_usage_and_partial_cost_coverage(self):
        self.event()
        self.event(incoming=None, outgoing=None, priced=False, status="failed")
        self.event(incoming=2000, outgoing=200, priced=False)
        report = self.report()
        total = report["totals"]
        self.assertEqual(total["requests"], 3)
        self.assertEqual(total["usage_known_requests"], 2)
        self.assertEqual(total["usage_missing_requests"], 1)
        self.assertEqual(total["priced_requests"], 1)
        self.assertEqual(total["unpriced_requests"], 2)
        self.assertEqual(total["total_tokens"], 3300)
        self.assertEqual(total["estimated_cost"], "0.0028000000")
        self.assertEqual(len(report["series"]), 2)
        self.assertEqual(report["series"][1]["requests"], 0)
        self.assertIsNone(report["series"][1]["estimated_cost"])
        self.assertIsNotNone(report["tracking_started_at"])
        self.assertEqual(sum(row["requests"] for row in report["series"]), total["requests"])
        self.assertEqual(sum(row["total_tokens"] for row in report["breakdown"]), total["total_tokens"])

    def test_members_only_see_their_models_and_usage_and_cannot_request_global(self):
        self.event(model="member-model")
        self.event(user=self.other, model="private-other-model", incoming=9999)
        own = self.report(self.member)
        self.assertEqual(own["scope"], "mine")
        self.assertEqual(own["models"], ["member-model"])
        self.assertEqual(own["totals"]["requests"], 1)
        self.assertNotIn("private-other-model", json.dumps(own, default=str))
        self.assertEqual(self.report()["totals"]["requests"], 2)
        with self.assertRaises(HTTPException) as error:
            self.report(self.member, scope="all")
        self.assertEqual(error.exception.status_code, 403)

    def test_local_calendar_boundaries_and_hourly_buckets_use_shanghai_time(self):
        left = datetime(2026, 6, 14, 16, tzinfo=timezone.utc)
        for stamp in [left - timedelta(microseconds=1), left, left + timedelta(days=2) - timedelta(microseconds=1), left + timedelta(days=2)]:
            self.event(stamp=stamp)
        report = self.report(granularity="hour")
        self.assertEqual(report["totals"]["requests"], 2)
        self.assertEqual(len(report["series"]), 48)
        self.assertEqual(report["series"][0]["period"].isoformat(), "2026-06-15T00:00:00+08:00")
        self.assertEqual(report["series"][0]["requests"], 1)
        self.assertEqual(report["series"][-1]["requests"], 1)

    def test_month_grouping_keeps_partial_month_filter_and_fills_empty_buckets(self):
        self.event(stamp=datetime(2026, 1, 15, 8, tzinfo=timezone.utc))
        self.event(stamp=datetime(2026, 2, 15, 8, tzinfo=timezone.utc))
        self.event(stamp=datetime(2026, 1, 1, 8, tzinfo=timezone.utc))
        with SessionLocal() as db:
            report = stats.statistics(db, self.admin, start=date(2026, 1, 15), end=date(2026, 3, 15), granularity="month")
        self.assertEqual(report["totals"]["requests"], 2)
        self.assertEqual([row["period"].month for row in report["series"]], [1, 2, 3])
        self.assertIsNone(report["series"][-1]["estimated_cost"])

    def test_price_snapshots_do_not_reprice_history_and_same_revision_conflicts(self):
        with tracking.usage_scope(self.context):
            unknown = tracking.ModelCall("chat", "usage.test", "test-chat")
            unknown.observe({"prompt_tokens": 1000, "completion_tokens": 100})
            unknown.finish()
        self.price()
        with tracking.usage_scope(self.context):
            old = tracking.ModelCall("chat", "usage.test", "test-chat")
            old.observe({"prompt_tokens": 1000, "completion_tokens": 100})
            self.price("4", "16", revision=1)
            old.finish()
            new = tracking.ModelCall("chat", "usage.test", "test-chat")
            new.observe({"prompt_tokens": 1000, "completion_tokens": 100})
            new.finish()
        with SessionLocal() as db:
            self.assertIsNone(db.get(ModelUsage, unknown.id).estimated_cost)
            self.assertEqual(db.get(ModelUsage, old.id).estimated_cost, Decimal("0.0028"))
            self.assertEqual(db.get(ModelUsage, new.id).estimated_cost, Decimal("0.0056"))
            self.assertEqual(db.get(ModelUsage, old.id).price_revision, 1)
        with self.assertRaises(HTTPException) as error:
            self.price("1", "1", revision=1)
        self.assertEqual(error.exception.status_code, 409)

    def test_stream_usage_duplicates_are_one_event_and_context_survives_threaded_next(self):
        self.price()
        client, response = MagicMock(), MagicMock()
        response.iter_lines.return_value = [
            'data: {"choices":[{"delta":{"content":"回答"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":100}}',
            'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":100}}', 'data: [DONE]',
        ]
        model = cloud_models.OpenAICompatibleChat(cloud_models._Endpoint("https://usage.test", "test-only", 1), "test-chat", 0)
        with patch.object(settings, "LLM_PROVIDER", "cloud"), patch.object(rag, "get_llm", return_value=model), \
             patch.object(cloud_models, "_post", return_value=(client, response)):
            metrics = {}
            iterator = rag.answer_stream("synthetic", [], [], mode="chat", metrics=metrics, usage_context=self.context)
            sentinel, output = object(), []
            with ThreadPoolExecutor(max_workers=2) as pool:
                while True:
                    item = pool.submit(lambda: contextvars.Context().run(next, iterator, sentinel)).result()
                    if item is sentinel:
                        break
                    output.append(item)
        self.assertEqual("".join(output), "回答")
        self.assertEqual(metrics, {"input_tokens": 1000, "output_tokens": 100})
        with SessionLocal() as db:
            rows = db.scalars(select(ModelUsage)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].user_id, self.member.id)
            self.assertEqual(rows[0].status, "succeeded")
            self.assertEqual(rows[0].estimated_cost, Decimal("0.0028"))
        response.close.assert_called_once()
        client.close.assert_called_once()

    def test_cancelled_stream_retains_already_reported_usage_and_cost(self):
        self.price()
        client, response = MagicMock(), MagicMock()
        response.iter_lines.return_value = ['data: {"usage":{"prompt_tokens":1000,"completion_tokens":100},"choices":[]}',
                                           'data: {"choices":[{"delta":{"content":"partial"}}]}']
        model = cloud_models.OpenAICompatibleChat(cloud_models._Endpoint("https://usage.test", "test-only", 1), "test-chat", 0)
        with tracking.usage_scope(self.context), patch.object(cloud_models, "_post", return_value=(client, response)):
            iterator = model.stream([])
            next(iterator)
            iterator.close()
        with SessionLocal() as db:
            row = db.scalar(select(ModelUsage))
            self.assertEqual(row.status, "cancelled")
            self.assertEqual(row.estimated_cost, Decimal("0.0028"))

    def test_transport_retries_are_counted_individually_without_inventing_failed_token_usage(self):
        self.price()
        requests = []
        def handle(request):
            requests.append(request)
            if len(requests) < 3:
                return httpx.Response(503, json={"error": {"message": "synthetic failure"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                             "usage": {"prompt_tokens": 1000, "completion_tokens": 100}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
             patch.object(cloud_models, "_shared_client", return_value=client), patch.object(cloud_models.time, "sleep"), \
             tracking.usage_scope(self.context):
            model = cloud_models.OpenAICompatibleChat(cloud_models._Endpoint("https://usage.test", "test-only", 1), "test-chat", 0)
            self.assertEqual(model.invoke([]).content, "ok")
        with SessionLocal() as db:
            rows = db.scalars(select(ModelUsage).order_by(ModelUsage.occurred_at)).all()
            self.assertEqual([row.status for row in rows], ["failed", "failed", "succeeded"])
            self.assertEqual([row.input_tokens for row in rows], [None, None, 1000])
            self.assertEqual([row.estimated_cost for row in rows], [None, None, Decimal("0.0028")])

    def test_network_and_stream_retries_do_not_create_extra_attempts(self):
        client_class = httpx.Client
        for stream in (False, True):
            with self.subTest(stream=stream):
                attempts = []
                model_name = "retry-stream" if stream else "retry-network"
                def handle(request):
                    attempts.append(request)
                    if len(attempts) == 1:
                        raise httpx.ConnectError("synthetic connect failure", request=request)
                    if len(attempts) == 2:
                        return httpx.Response(503, json={"error": {"message": "synthetic retry"}})
                    if stream:
                        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\ndata: [DONE]\n\n')
                    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                                     "usage": {"prompt_tokens": 10, "completion_tokens": 2}})
                def create(**kwargs):
                    return client_class(transport=httpx.MockTransport(handle), **kwargs)
                with client_class(transport=httpx.MockTransport(handle)) as client, \
                     patch.object(cloud_models, "_shared_client", return_value=client), \
                     patch.object(cloud_models.httpx, "Client", side_effect=create), patch.object(cloud_models.time, "sleep"), \
                     tracking.usage_scope(self.context):
                    model = cloud_models.OpenAICompatibleChat(cloud_models._Endpoint("https://usage.test", "test-only", 1), model_name, 0)
                    result = "".join(chunk.content for chunk in model.stream([])) if stream else model.invoke([]).content
                    self.assertEqual(result, "ok")
                with SessionLocal() as db:
                    rows = db.scalars(select(ModelUsage).where(ModelUsage.model == model_name).order_by(ModelUsage.occurred_at)).all()
                    self.assertEqual([row.status for row in rows], ["failed", "failed", "succeeded"])
                    self.assertEqual([row.input_tokens for row in rows], [None, None, 10])
                self.assertEqual(len(attempts), 3)

    def test_interrupted_backoff_does_not_count_an_attempt_that_never_started(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503, json={}))) as client, \
             patch.object(cloud_models, "_shared_client", return_value=client), \
             patch.object(cloud_models.time, "sleep", side_effect=RuntimeError("synthetic interrupted backoff")), \
             tracking.usage_scope(self.context):
            model = cloud_models.OpenAICompatibleChat(cloud_models._Endpoint("https://usage.test", "test-only", 1), "test-chat", 0)
            with self.assertRaises(RuntimeError):
                model.invoke([])
        with SessionLocal() as db:
            rows = db.scalars(select(ModelUsage)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "failed")

    def test_synchronous_local_stream_failure_is_recorded_as_failed(self):
        model = MagicMock()
        model.stream.side_effect = RuntimeError("synthetic local failure")
        with patch.object(settings, "LLM_PROVIDER", "ollama"), patch.object(rag, "get_llm", return_value=model):
            with self.assertRaises(RuntimeError):
                list(rag.answer_stream("synthetic", [], [], mode="chat", usage_context=self.context))
        with SessionLocal() as db:
            rows = db.scalars(select(ModelUsage)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].status, "failed")
            self.assertIsNone(rows[0].input_tokens)

    def test_concurrent_price_edit_has_one_winner_and_one_revision_conflict(self):
        barrier = Barrier(2)
        def save(rate):
            barrier.wait(timeout=5)
            try:
                return self.price(incoming=rate).revision
            except HTTPException as error:
                return error.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(save, ["2", "4"]))
        self.assertEqual(sorted(outcomes), [1, 409])
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ModelPrice)), 1)

    def test_rerank_usage_has_its_own_operation_and_real_input_count(self):
        from langchain_core.documents import Document as LCDocument
        from app.services.hybrid_search import rerank_results
        client_class = httpx.AsyncClient
        def create(**kwargs):
            return client_class(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
                "results": [{"index": 0, "relevance_score": 0.9}], "usage": {"total_tokens": 42}})), **kwargs)
        with patch.object(httpx, "AsyncClient", side_effect=create), tracking.usage_scope(self.context.for_operation("query_embedding")):
            results = rerank_results("synthetic", [(LCDocument(page_content="synthetic"), 0.1)],
                                     base_url="https://usage.test/v1", api_key="test-only", model="rerank-test")
        self.assertEqual(len(results), 1)
        with SessionLocal() as db:
            row = db.scalar(select(ModelUsage))
            self.assertEqual((row.kind, row.operation, row.input_tokens, row.output_tokens), ("rerank", "rerank", 42, 0))
            self.assertEqual(row.user_id, self.member.id)
            self.assertEqual(row.status, "succeeded")

    def test_chat_sse_replay_and_backfill_never_double_count_and_deletion_keeps_usage(self):
        from fastapi.testclient import TestClient
        with patch("app.core.logging.setup_logging"):
            from app.main import app
        self.price()
        headers = {"Authorization": "Bearer " + create_access_token(str(self.member.id), "user")}
        payload = {"question": "synthetic once", "mode": "chat", "request_id": str(uuid.uuid4())}
        client_class = httpx.AsyncClient
        def handle(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
                'data: {"choices":[{"delta":{"content":"synthetic answer"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":100}}\n\n'
                'data: [DONE]\n\n'))
        transport = MagicMock(side_effect=handle)
        def create(**kwargs):
            return client_class(transport=httpx.MockTransport(transport), **kwargs)
        model = cloud_models.OpenAICompatibleChat(cloud_models._Endpoint("https://usage.test", "test-only", 1), "test-chat", 0)
        with TestClient(app) as client, patch.object(settings, "LLM_PROVIDER", "cloud"), \
             patch.object(settings, "LLM_STREAM_INCLUDE_USAGE", True), \
             patch.object(rag, "get_stream_chat", return_value=model), \
             patch.object(cloud_models.httpx, "AsyncClient", side_effect=create):
            first = client.post("/api/v1/chat/stream", headers=headers, json=payload)
            replay = client.post("/api/v1/chat/stream", headers=headers, json=payload)
            self.assertEqual(first.status_code, 200, first.text)
            self.assertIn("event: done", first.text)
            self.assertIn('"replayed": true', replay.text)
            transport.assert_called_once()
            with SessionLocal() as db:
                row = db.scalar(select(ModelUsage))
                turn = db.get(ChatTurn, row.turn_id)
                self.assertEqual(turn.trace["usage"], {"input_tokens": 1000, "output_tokens": 100})
                self.assertEqual(row.estimated_cost, Decimal("0.0028"))
                conversation_id = row.conversation_id
            init_db(create_admin=False)
            with SessionLocal() as db:
                self.assertEqual(db.scalar(select(func.count()).select_from(ModelUsage)), 1)
            self.assertEqual(client.delete(f"/api/v1/chat/conversations/{conversation_id}", headers=headers).status_code, 200)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ModelUsage)), 1)
            self.assertEqual(db.scalar(select(ModelUsage)).estimated_cost, Decimal("0.0028"))

    def test_rewrite_reads_the_actual_usage_frame_after_finish_reason(self):
        client_class = httpx.AsyncClient
        def handle(request):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=(
                'data: {"choices":[{"delta":{"content":"rewrite"}}]}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":5}}\n\n'
                'data: [DONE]\n\n'))
        def create(**kwargs):
            return client_class(transport=httpx.MockTransport(handle), **kwargs)
        with patch.object(settings, "LLM_PROVIDER", "cloud"), patch.object(settings, "LLM_BASE_URL", "https://usage.test"), \
             patch.object(settings, "LLM_MODEL", "test-chat"), patch.object(settings, "LLM_API_KEY", "test-only"), \
             patch.object(settings, "LLM_STREAM_INCLUDE_USAGE", True), patch.object(httpx, "AsyncClient", side_effect=create), \
             tracking.usage_scope(self.context.for_operation("rewrite")):
            self.assertEqual(cloud_models.complete_short_chat([HumanMessage(content="synthetic")], timeout=1, max_tokens=64), "rewrite")
        with SessionLocal() as db:
            row = db.scalar(select(ModelUsage))
            self.assertEqual((row.operation, row.input_tokens, row.output_tokens), ("rewrite", 20, 5))

    def test_rewrite_with_finished_text_does_not_fail_while_waiting_for_late_usage(self):
        client_class = httpx.AsyncClient
        class LateUsage(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"complete"},"finish_reason":"stop"}]}\n\n'
                await asyncio.sleep(1)
                yield b'data: {"choices":[],"usage":{"prompt_tokens":20,"completion_tokens":5}}\n\n'
        def create(**kwargs):
            return client_class(transport=httpx.MockTransport(lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=LateUsage())), **kwargs)
        with patch.object(settings, "LLM_PROVIDER", "cloud"), patch.object(settings, "LLM_BASE_URL", "https://usage.test"), \
             patch.object(settings, "LLM_MODEL", "test-chat"), patch.object(settings, "LLM_API_KEY", "test-only"), \
             patch.object(settings, "LLM_STREAM_INCLUDE_USAGE", True), patch.object(httpx, "AsyncClient", side_effect=create), \
             tracking.usage_scope(self.context.for_operation("rewrite")):
            result = cloud_models.complete_short_chat([HumanMessage(content="synthetic")], timeout=0.05, max_tokens=64)
        self.assertEqual(result, "complete")
        with SessionLocal() as db:
            row = db.scalar(select(ModelUsage))
            self.assertEqual(row.status, "succeeded")
            self.assertIsNone(row.input_tokens)
            self.assertIsNone(row.estimated_cost)

    def test_document_and_query_embedding_capture_owner_and_separate_operations(self):
        adapter = cloud_models.OpenAICompatibleEmbeddings(cloud_models._Endpoint("https://usage.test", "test-only", 1), "embed-test", 16)
        def handle(request):
            count = len(json.loads(request.content)["input"])
            return httpx.Response(200, json={"data": [{"index": index, "embedding": [1.0] + [0.0] * 1023} for index in range(count)],
                                             "usage": {"prompt_tokens": count * 20, "total_tokens": count * 20}})
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb.id, uploaded_by=self.member.id, filename="synthetic.txt", file_ext=".txt", file_size=1,
                           stored_path="not-read-because-parse-done", parse_done=True, chunk_count=1)
            db.add(doc)
            db.flush()
            db.add(DocumentChunk(document_id=doc.id, chunk_index=0, content="Synthetic evidence."))
            db.commit()
            did = doc.id
        vector_store._store.cache_clear()
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
             patch.object(settings, "EMBEDDING_PROVIDER", "cloud"), patch.object(settings, "RERANK_ENABLED", False), \
             patch.object(cloud_models, "_shared_client", return_value=client), \
             patch.object(vector_store, "get_embeddings", return_value=adapter), patch.object(vector_store, "_vector_engine", return_value=engine):
            ingest.ingest_document_task(str(did))
            with tracking.usage_scope(tracking.UsageContext(user_id=self.other.id, kb_id=self.kb.id, operation="query_embedding")):
                self.assertTrue(rag.retrieve(self.kb.collection_name, "Synthetic evidence", 1))
        with SessionLocal() as db:
            rows = {row.operation: row for row in db.scalars(select(ModelUsage)).all()}
            self.assertEqual(set(rows), {"document_embedding", "query_embedding"})
            self.assertEqual(rows["document_embedding"].user_id, self.member.id)
            self.assertEqual(rows["query_embedding"].user_id, self.other.id)
            self.assertEqual(rows["document_embedding"].document_id, did)
            self.assertTrue(all(row.input_tokens == 20 and row.output_tokens == 0 for row in rows.values()))
        vector_store._store.cache_clear()

    def test_backfill_is_idempotent_and_never_guesses_historical_model_or_cost(self):
        with SessionLocal() as db:
            conv = Conversation(user_id=self.member.id, kb_id=self.kb.id, mode="rag")
            db.add(conv)
            db.flush()
            for index, usage in enumerate([{"input_tokens": 10, "output_tokens": 2}, None,
                                           {"input_tokens": True, "output_tokens": 2}, {"input_tokens": 1.2, "output_tokens": 2},
                                           {"input_tokens": "10", "output_tokens": 2}, {"input_tokens": -1, "output_tokens": 2}]):
                db.add(ChatTurn(user_id=self.member.id, conversation_id=conv.id, request_id=uuid.uuid4(), user_message_id=uuid.uuid4(),
                                payload_hash="a" * 64, status="completed", heartbeat_at=self.at,
                                trace={"usage": usage, "usage_scope": "generation_only", "input_tokens_estimate": 99999}))
            db.commit()
        init_db(create_admin=False)
        init_db(create_admin=False)
        with SessionLocal() as db:
            rows = db.scalars(select(ModelUsage)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].input_tokens, rows[0].output_tokens), (10, 2))
            self.assertEqual((rows[0].model, rows[0].provider, rows[0].source), ("historical-unknown", "historical", "legacy"))
            self.assertIsNone(rows[0].estimated_cost)
            self.assertNotIn("historical", [row.provider for row in stats.price_list(db)])

    def test_user_or_kb_deletion_keeps_spend_without_retaining_text(self):
        self.event()
        with SessionLocal() as db:
            db.execute(delete(User).where(User.id == self.member.id))
            db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == self.kb.id))
            db.commit()
            row = db.scalar(select(ModelUsage))
            self.assertIsNone(row.user_id)
            self.assertIsNone(row.kb_id)
            self.assertEqual(row.estimated_cost, Decimal("0.0028"))
            self.assertNotIn("content", row.__table__.columns)
        self.assertEqual(self.report()["totals"]["requests"], 1)

    def test_api_prices_are_admin_only_and_money_is_serialized_as_decimal_strings(self):
        from fastapi.testclient import TestClient
        with patch("app.core.logging.setup_logging"):
            from app.main import app
        client = TestClient(app)
        self.addCleanup(client.close)
        member = {"Authorization": "Bearer " + create_access_token(str(self.member.id), "user")}
        admin = {"Authorization": "Bearer " + create_access_token(str(self.admin.id), "admin")}
        payload = dict(provider="usage.test", model="test-chat", kind="chat", input_per_million="2", output_per_million="8", expected_revision=0)
        self.assertEqual(client.get("/api/v1/usage/statistics").status_code, 401)
        self.assertEqual(client.get("/api/v1/usage/prices", headers=member).status_code, 403)
        self.assertEqual(client.put("/api/v1/usage/prices", headers=member, json=payload).status_code, 403)
        self.assertEqual(client.get("/api/v1/usage/statistics?scope=all", headers=member).status_code, 403)
        result = client.put("/api/v1/usage/prices", headers=admin, json=payload)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["data"]["input_per_million"], "2.000000")
        self.assertEqual(client.put("/api/v1/usage/prices", headers=admin, json=payload).status_code, 409)
        self.event()
        report = client.get("/api/v1/usage/statistics?start=2026-06-15&end=2026-06-16", headers=member)
        self.assertEqual(report.status_code, 200)
        body = report.json()["data"]
        self.assertEqual(body["scope"], "mine")
        self.assertEqual(body["totals"]["estimated_cost"], "0.0028000000")
        self.assertEqual(body["series"][0]["period"], "2026-06-15T00:00:00+08:00")
        self.assertEqual(client.get("/api/v1/usage/statistics?start=2026-01-01&end=2026-03-01&granularity=hour", headers=admin).status_code, 422)
        self.assertEqual(client.get("/api/v1/usage/statistics?granularity=wrong", headers=admin).status_code, 422)
