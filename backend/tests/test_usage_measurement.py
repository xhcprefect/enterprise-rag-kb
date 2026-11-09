"""精确费用测量 API 的离线校验 + 隔离 *_test PostgreSQL 集成回归。

只读取/写入显式提供的专用测试库；不访问真实模型、业务库或 .env。
"""
from __future__ import annotations

import json
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ["RAG_DISABLE_DOTENV"] = "1"
os.environ.setdefault("EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

from pydantic import ValidationError  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from tests._env import TEST_URL, prepare  # noqa: E402

prepare()
_TEST_URL = TEST_URL


from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api.v1 import usage as usage_api  # noqa: E402
from app.core.security import create_access_token  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import ChatTurn, Document, KnowledgeBase, ModelUsage, User  # noqa: E402
from app.schemas.usage import (  # noqa: E402
    MEASUREMENT_MAX_DOCUMENTS,
    MEASUREMENT_MAX_REQUESTS,
    UsageMeasurementRequest,
)
from app.services import usage_measurement  # noqa: E402


def _bind_engine() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    if not _TEST_URL:
        return
    target = make_url(_TEST_URL)
    if db_session.engine.url != target:
        db_session.engine.dispose()
        db_session.engine = create_engine(
            _TEST_URL, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800
        )
        db_session.SessionLocal.configure(bind=db_session.engine)


_bind_engine()

if _TEST_URL:
    Base.metadata.create_all(bind=db_session.engine)


def build_app() -> FastAPI:
    application = FastAPI()
    application.include_router(usage_api.router, prefix="/api/v1")
    return application


class MeasurementSchemaTests(unittest.TestCase):
    def _payload(self, **overrides):
        value = {
            "start": datetime(2026, 6, 15, tzinfo=timezone.utc),
            "end": datetime(2026, 6, 16, tzinfo=timezone.utc),
            "requests": [{"user_id": uuid.uuid4(), "request_id": uuid.uuid4()}],
        }
        value.update(overrides)
        return value

    def test_extra_and_naive_datetimes_are_rejected(self):
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(**self._payload(extra=True))
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(start=datetime(2026, 6, 15),
                                    end=datetime(2026, 6, 16, tzinfo=timezone.utc),
                                    requests=self._payload()["requests"])
        request = UsageMeasurementRequest(
            **self._payload(start=datetime(2026, 6, 15, 8, tzinfo=timezone(timedelta(hours=8))))
        )
        self.assertEqual(request.start, datetime(2026, 6, 15, tzinfo=timezone.utc))

    def test_window_numbers_are_rejected_instead_of_implicit_timestamps(self):
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(**self._payload(
                start=1781481600, end=1781568000))

    def test_unix_style_datetime_strings_are_rejected_before_pydantic_parsing(self):
        pairs = [
            ("0", "86400"),
            ("1781481600", "1781568000"),
            ("1781481600000", "1781568000000"),
            ("0.1", "86400.1"),
        ]
        for start, end in pairs:
            with self.subTest(start=start, end=end), self.assertRaises(ValidationError):
                UsageMeasurementRequest(**self._payload(start=start, end=end))
        valid = UsageMeasurementRequest(**self._payload(
            start="2026-06-15T08:00:00+08:00", end="2026-06-16T00:00:00Z"))
        self.assertEqual(valid.start, datetime(2026, 6, 15, tzinfo=timezone.utc))
        self.assertEqual(valid.end, datetime(2026, 6, 16, tzinfo=timezone.utc))

    def test_window_must_be_positive_bounded_and_half_open(self):
        for start, end in [
            (datetime(2026, 6, 16, tzinfo=timezone.utc), datetime(2026, 6, 15, tzinfo=timezone.utc)),
            (datetime(2026, 6, 15, tzinfo=timezone.utc), datetime(2026, 6, 15, tzinfo=timezone.utc)),
            (datetime(2026, 6, 1, tzinfo=timezone.utc), datetime(2026, 7, 3, tzinfo=timezone.utc)),
        ]:
            with self.subTest(start=start), self.assertRaises(ValidationError):
                UsageMeasurementRequest(**self._payload(start=start, end=end))

    def test_empty_duplicate_and_oversized_selections_are_rejected(self):
        pair = {"user_id": uuid.uuid4(), "request_id": uuid.uuid4()}
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(**self._payload(requests=[]))
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(**self._payload(requests=[pair, dict(pair)]))
        document_id = uuid.uuid4()
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(**self._payload(requests=[], document_ids=[document_id, document_id]))
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(**self._payload(
                requests=[{"user_id": uuid.uuid4(), "request_id": uuid.uuid4()}
                          for _ in range(MEASUREMENT_MAX_REQUESTS + 1)]))
        with self.assertRaises(ValidationError):
            UsageMeasurementRequest(**self._payload(
                requests=[], document_ids=[uuid.uuid4() for _ in range(MEASUREMENT_MAX_DOCUMENTS + 1)]))

    def test_document_only_selection_is_valid(self):
        request = UsageMeasurementRequest(**self._payload(requests=[], document_ids=[uuid.uuid4()]))
        self.assertEqual(request.requests, [])
        self.assertEqual(len(request.document_ids), 1)


class MeasurementAccumulatorTests(unittest.TestCase):
    def test_missing_currency_is_flagged_and_never_assumed_to_be_cny(self):
        row = SimpleNamespace(
            status="succeeded", operation="generation", input_tokens=1, output_tokens=2,
            cached_input_tokens=0, estimated_cost=Decimal("0.1000000000"),
            finished_at=datetime.now(timezone.utc), currency=None,
        )
        accumulator = usage_measurement._Accumulator()
        accumulator.add(row)
        self.assertIn("currency_missing", accumulator.issues)
        self.assertEqual(accumulator.currencies, set())
        self.assertFalse(accumulator.has_known_cost)
        self.assertEqual(accumulator.known_cost, Decimal("0"))
        self.assertIsNone(accumulator.metrics()["known_cost_subtotal"])

    def test_foreign_and_mixed_currency_never_enter_the_cny_subtotal(self):
        accumulator = usage_measurement._Accumulator()
        accumulator.add(SimpleNamespace(
            status="succeeded", operation="generation", input_tokens=1, output_tokens=1,
            cached_input_tokens=0, estimated_cost=Decimal("0.0010000000"),
            finished_at=datetime.now(timezone.utc), currency="CNY",
        ))
        accumulator.add(SimpleNamespace(
            status="succeeded", operation="rerank", input_tokens=1, output_tokens=0,
            cached_input_tokens=0, estimated_cost=Decimal("2.0000000000"),
            finished_at=datetime.now(timezone.utc), currency="USD",
        ))
        self.assertEqual(accumulator.known_cost, Decimal("0.0010000000"))
        self.assertIn("currency_mismatch", accumulator.issues)
        self.assertNotEqual(accumulator.metrics()["known_cost_subtotal"],
                            "2.0010000000")


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL（*_test）才运行精确费用集成测试")
class UsageMeasurementDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TEST_URL or not make_url(TEST_URL).database.endswith("_test") \
                or db_session.engine.url != make_url(TEST_URL):
            raise RuntimeError("拒绝在业务库运行精确费用测量回归")
        from app.db.init_db import init_db

        init_db(create_admin=False)
        cls.client = TestClient(build_app())

    @classmethod
    def tearDownClass(cls):
        db_session.engine.dispose()

    def setUp(self):
        with db_session.engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, conversations, model_usage, model_prices "
                "RESTART IDENTITY CASCADE"
            ))
        with db_session.SessionLocal() as db:
            self.admin = User(username=f"measure_admin_{uuid.uuid4().hex[:8]}", role="admin",
                              is_active=True, password_hash="unused-test-only")
            self.member = User(username=f"measure_member_{uuid.uuid4().hex[:8]}", role="user",
                               is_active=True, password_hash="unused-test-only")
            self.other = User(username=f"measure_other_{uuid.uuid4().hex[:8]}", role="user",
                              is_active=True, password_hash="unused-test-only")
            db.add_all([self.admin, self.member, self.other])
            db.flush()
            self.kb = KnowledgeBase(name="measure-" + uuid.uuid4().hex,
                                    collection_name="measure_" + uuid.uuid4().hex,
                                    owner_id=self.admin.id, is_public=True, kind="upload")
            db.add(self.kb)
            db.commit()
            db.refresh(self.admin)
            db.refresh(self.member)
            db.refresh(self.other)
        self.start = datetime(2026, 6, 15, tzinfo=timezone.utc)
        self.end = datetime(2026, 6, 16, tzinfo=timezone.utc)
        self.at = datetime(2026, 6, 15, 8, tzinfo=timezone.utc)

    # ---- fixtures ---------------------------------------------------------
    def turn(self, *, user=None, request_id=None, status="completed", trace=None):
        with db_session.SessionLocal() as db:
            row = ChatTurn(
                request_id=request_id or uuid.uuid4(),
                user_id=(user or self.member).id,
                user_message_id=uuid.uuid4(),
                payload_hash="a" * 64,
                status=status,
                heartbeat_at=datetime.now(timezone.utc),
                trace=trace or {"secret": "SECRET_TRACE_BODY"},
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def usage(self, *, turn_id=None, document_id=None, user=None, operation="generation",
              status="succeeded", input_tokens=100, output_tokens=20,
              cost="0.0010000000", currency="CNY", occurred_at=None, finished=True):
        with db_session.SessionLocal() as db:
            row = ModelUsage(
                user_id=(user or self.member).id,
                kb_id=self.kb.id,
                occurred_at=occurred_at or self.at,
                finished_at=self.at + timedelta(seconds=1) if finished else None,
                turn_id=turn_id,
                document_id=document_id,
                provider="measure.test",
                model="measure-model",
                kind="chat" if operation in {"generation", "rewrite"} else "embedding",
                operation=operation,
                status=status,
                source="provider",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=None,
                currency=currency,
                estimated_cost=None if cost is None else Decimal(cost),
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def document(self, *, status="ready", uploaded_by=None):
        with db_session.SessionLocal() as db:
            row = Document(
                kb_id=self.kb.id,
                filename="synthetic.txt",
                file_ext=".txt",
                file_size=10,
                stored_path="/synthetic/measure.txt",
                status=status,
                chunk_count=1,
                uploaded_by=(uploaded_by or self.member).id,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return row

    def payload(self, requests=(), documents=(), start=None, end=None):
        return {
            "start": (start or self.start).isoformat(),
            "end": (end or self.end).isoformat(),
            "requests": [{"user_id": str(user.id), "request_id": str(request_id)}
                         for user, request_id in requests],
            "document_ids": [str(item) for item in documents],
        }

    def post(self, body, user=None):
        headers = {}
        if user is not None:
            headers["Authorization"] = "Bearer " + create_access_token(str(user.id), user.role)
        return self.client.post("/api/v1/usage/measurement", json=body, headers=headers)

    def report(self, body):
        response = self.post(body, self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["code"], 0)
        # 响应模型内部字段名避开 BaseModel.schema，但对外 JSON 必须是 "schema"。
        self.assertEqual(data["data"]["schema"], "rag-usage-measurement/v1")
        self.assertNotIn("schema_version", data["data"])
        return data["data"]

    # ---- access and validation -------------------------------------------
    def test_endpoint_is_admin_only_and_validation_precedes_query(self):
        body = self.payload(requests=[(self.member, uuid.uuid4())])
        self.assertEqual(self.post(body).status_code, 401)
        self.assertEqual(self.post(body, self.member).status_code, 403)
        self.assertEqual(self.post(body, self.admin).status_code, 200)

        invalid = [
            {**body, "extra": 1},
            {**body, "start": "2026-06-15T00:00:00"},
            {**body, "start": "0", "end": "86400"},
            {**body, "start": "1781481600000", "end": "1781568000000"},
            {**body, "start": "0.1", "end": "86400.1"},
            {**body, "end": body["start"]},
            {**body, "requests": [], "document_ids": []},
            {**body, "requests": [body["requests"][0], body["requests"][0]]},
        ]
        with patch.object(usage_measurement, "measure") as measure:
            for item in invalid:
                with self.subTest(item=item):
                    self.assertEqual(self.post(item, self.admin).status_code, 422)
            measure.assert_not_called()

    # ---- exact request accounting ----------------------------------------
    def test_exact_aggregation_includes_failed_attempts_and_never_reads_trace(self):
        request_id = uuid.uuid4()
        turn = self.turn(request_id=request_id)
        other_turn = self.turn(user=self.other, request_id=request_id)
        self.usage(turn_id=turn.id, input_tokens=100, output_tokens=20, cost="0.0010000000")
        self.usage(turn_id=turn.id, operation="rewrite", status="failed",
                   input_tokens=50, output_tokens=0, cost="0.0005000000")
        self.usage(turn_id=other_turn.id, user=self.other, input_tokens=99999,
                   output_tokens=99999, cost="9.0000000000")

        data = self.report(self.payload(requests=[(self.member, request_id)]))
        self.assertEqual(data["totals"]["attempts"], 2)
        self.assertEqual(data["totals"]["failed_attempts"], 1)
        self.assertEqual(data["totals"]["input_tokens"], 150)
        self.assertEqual(data["totals"]["total_tokens"], 170)
        self.assertEqual(data["totals"]["known_cost_subtotal"], "0.0015000000")
        self.assertEqual(data["totals"]["total_cost"], "0.0015000000")
        self.assertEqual(data["coverage"]["status"], "complete")
        self.assertEqual(data["currency"], "CNY")
        self.assertEqual([row["operation"] for row in data["by_operation"]],
                         ["generation", "rewrite"])
        self.assertEqual(data["requests"][0]["found"], True)
        self.assertEqual(data["requests"][0]["turn_status"], "completed")
        self.assertEqual(data["requests"][0]["user_id"], str(self.member.id))
        self.assertEqual(data["requests"][0]["request_id"], str(request_id))
        serialized = json.dumps(data, default=str)
        self.assertNotIn("SECRET_TRACE_BODY", serialized)
        self.assertNotIn("99999", serialized)
        self.assertNotIn("9.0000000000", serialized)

    def test_missing_turn_and_existing_turn_without_rows_are_unknown_not_zero(self):
        missing_id = uuid.uuid4()
        present = self.turn()
        data = self.report(self.payload(requests=[(self.member, missing_id),
                                                  (self.member, present.request_id)]))
        self.assertEqual(data["totals"]["attempts"], 0)
        self.assertIsNone(data["totals"]["known_cost_subtotal"])
        self.assertIsNone(data["totals"]["total_cost"])
        self.assertEqual(data["coverage"]["status"], "unknown")
        self.assertEqual(data["coverage"]["missing_requests"], 1)
        self.assertEqual(data["coverage"]["attempts_missing_requests"], 1)
        self.assertEqual(data["requests"][0]["coverage_status"], "unknown")
        self.assertIn("turn_not_found", data["requests"][0]["reasons"])
        self.assertEqual(data["requests"][1]["found"], True)
        self.assertIn("no_usage_rows", data["requests"][1]["reasons"])

    def test_unpriced_started_and_currency_conflicts_never_become_total_cost(self):
        turn = self.turn()
        self.usage(turn_id=turn.id, cost="0.0010000000")
        self.usage(turn_id=turn.id, operation="query_embedding", status="failed",
                   input_tokens=10, output_tokens=0, cost=None)
        self.usage(turn_id=turn.id, operation="rerank", status="started",
                   input_tokens=5, output_tokens=0, cost="0.0001000000", finished=False)
        data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
        self.assertEqual(data["totals"]["attempts"], 3)
        self.assertEqual(data["totals"]["unpriced_attempts"], 1)
        self.assertEqual(data["coverage"]["unfinished_attempts"], 1)
        self.assertEqual(data["coverage"]["status"], "partial")
        self.assertIsNone(data["totals"]["total_cost"])
        self.assertEqual(data["totals"]["known_cost_subtotal"], "0.0011000000")
        self.assertEqual(data["requests"][0]["coverage_status"], "partial")

        with db_session.SessionLocal() as db:
            row = db.query(ModelUsage).filter(ModelUsage.status == "succeeded").one()
            row.currency = "USD"
            db.commit()
        mixed = self.report(self.payload(requests=[(self.member, turn.request_id)]))
        self.assertTrue(mixed["coverage"]["currency_mismatch"])
        self.assertIsNone(mixed["currency"])
        self.assertEqual(mixed["coverage"]["currencies"], ["CNY", "USD"])
        self.assertIsNone(mixed["totals"]["total_cost"])

    def test_consistent_foreign_currency_is_still_not_a_complete_cny_total(self):
        turn = self.turn()
        self.usage(turn_id=turn.id, cost="1.0000000000", currency="USD")
        data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
        self.assertTrue(data["coverage"]["currency_mismatch"])
        self.assertIsNone(data["currency"])
        self.assertEqual(data["coverage"]["currencies"], ["USD"])
        self.assertEqual(data["coverage"]["status"], "partial")
        self.assertIsNone(data["totals"]["total_cost"])
        self.assertIsNone(data["totals"]["known_cost_subtotal"])
        self.assertIn("currency_mismatch", data["requests"][0]["reasons"])

    def test_mixed_currency_has_no_unitless_subtotal_at_any_level(self):
        turn = self.turn()
        self.usage(turn_id=turn.id, operation="generation", cost="0.0010000000",
                   currency="CNY")
        self.usage(turn_id=turn.id, operation="rerank", status="failed",
                   input_tokens=20, output_tokens=0, cost="2.0000000000", currency="USD")
        data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
        self.assertIsNone(data["currency"])
        self.assertIsNone(data["totals"]["known_cost_subtotal"])
        self.assertIsNone(data["totals"]["total_cost"])
        self.assertIsNone(data["requests"][0]["known_cost_subtotal"])
        self.assertTrue(all(item["known_cost_subtotal"] is None
                            for item in data["by_operation"]))
        self.assertEqual(data["totals"]["priced_attempts"], 2)
        self.assertEqual(data["coverage"]["currencies"], ["CNY", "USD"])

    def test_nonterminal_or_unknown_lifecycle_is_never_complete_cost(self):
        for status, reason, field in (
            ("processing", "turn_not_terminal", "nonterminal_requests"),
            ("pending", "turn_not_terminal", "nonterminal_requests"),
            ("unexpected", "turn_status_unknown", "unknown_request_statuses"),
        ):
            with self.subTest(kind="turn", status=status):
                turn = self.turn(status=status)
                self.usage(turn_id=turn.id, operation="query_embedding",
                           input_tokens=20, output_tokens=0, cost="0.0010000000")
                data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
                self.assertFalse(data["coverage"]["complete"])
                self.assertIsNone(data["totals"]["total_cost"])
                self.assertNotEqual(data["requests"][0]["coverage_status"], "complete")
                self.assertIn(reason, data["requests"][0]["reasons"])
                self.assertEqual(data["coverage"][field], 1)
                self.assertEqual(data["totals"]["known_cost_subtotal"], "0.0010000000")

        for status, reason, field in (
            ("processing", "document_not_terminal", "nonterminal_documents"),
            ("pending", "document_not_terminal", "nonterminal_documents"),
            ("unexpected", "document_status_unknown", "unknown_document_statuses"),
        ):
            with self.subTest(kind="document", status=status):
                document = self.document(status=status)
                self.usage(document_id=document.id, operation="document_embedding",
                           input_tokens=20, output_tokens=0, cost="0.0010000000")
                data = self.report(self.payload(documents=[document.id]))
                self.assertFalse(data["coverage"]["complete"])
                self.assertIsNone(data["totals"]["total_cost"])
                self.assertNotEqual(data["documents"][0]["coverage_status"], "complete")
                self.assertIn(reason, data["documents"][0]["reasons"])
                self.assertEqual(data["coverage"][field], 1)
                self.assertEqual(data["documents"][0]["document_status"], status)

    def test_terminal_failed_and_cancelled_keep_their_priced_attempts(self):
        for status in ("completed", "failed", "cancelled"):
            with self.subTest(kind="turn", status=status):
                turn = self.turn(status=status)
                self.usage(turn_id=turn.id,
                           status="failed" if status == "failed" else "succeeded",
                           cost="0.0010000000")
                data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
                self.assertTrue(data["coverage"]["complete"])
                self.assertEqual(data["totals"]["total_cost"], "0.0010000000")

        for status in ("ready", "failed", "cancelled"):
            with self.subTest(kind="document", status=status):
                document = self.document(status=status)
                self.usage(document_id=document.id, operation="document_embedding",
                           input_tokens=20, output_tokens=0, cost="0.0010000000")
                data = self.report(self.payload(documents=[document.id]))
                self.assertTrue(data["coverage"]["complete"])
                self.assertEqual(data["totals"]["total_cost"], "0.0010000000")

    def test_unknown_attempt_status_blocks_completeness_but_keeps_known_metering(self):
        turn = self.turn()
        self.usage(turn_id=turn.id, status="unexpected", cost="0.0010000000")
        data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
        self.assertFalse(data["coverage"]["complete"])
        self.assertIsNone(data["totals"]["total_cost"])
        self.assertEqual(data["totals"]["known_cost_subtotal"], "0.0010000000")
        self.assertEqual(data["coverage"]["unknown_attempt_statuses"], 1)
        self.assertNotEqual(data["requests"][0]["coverage_status"], "complete")
        self.assertIn("attempt_status_unknown", data["requests"][0]["reasons"])

    def test_association_mismatch_is_excluded_and_flagged(self):
        turn = self.turn()
        self.usage(turn_id=turn.id, user=self.other, input_tokens=777,
                   output_tokens=777, cost="7.0000000000")
        self.usage(turn_id=turn.id, input_tokens=100, output_tokens=10, cost="0.0010000000")
        data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
        self.assertEqual(data["totals"]["attempts"], 1)
        self.assertEqual(data["coverage"]["association_mismatch_attempts"], 1)
        self.assertEqual(data["coverage"]["status"], "partial")
        self.assertEqual(data["totals"]["input_tokens"], 100)
        self.assertEqual(data["totals"]["known_cost_subtotal"], "0.0010000000")
        self.assertIn("association_mismatch", data["requests"][0]["reasons"])

    def test_background_summary_and_project_embedding_are_not_measured(self):
        turn = self.turn()
        document = self.document()
        self.usage(turn_id=turn.id, input_tokens=100, output_tokens=10, cost="0.0010000000")
        self.usage(turn_id=turn.id, operation="summary", input_tokens=9000,
                   output_tokens=9000, cost="9.0000000000")
        self.usage(document_id=document.id, operation="project_embedding",
                   input_tokens=8000, output_tokens=0, cost="8.0000000000")
        self.usage(document_id=document.id, operation="document_embedding",
                   input_tokens=50, output_tokens=0, cost="0.0001000000")
        data = self.report(self.payload(requests=[(self.member, turn.request_id)],
                                        documents=[document.id]))
        self.assertEqual(data["totals"]["attempts"], 2)
        self.assertEqual(data["totals"]["input_tokens"], 150)
        self.assertEqual(data["totals"]["total_cost"], "0.0011000000")
        operations = {row["operation"] for row in data["by_operation"]}
        self.assertEqual(operations, {"generation", "document_embedding"})
        self.assertNotIn("summary", operations)
        self.assertNotIn("project_embedding", operations)
        self.assertEqual(data["coverage"]["background_operations_unmeasured"],
                         ["summary", "project_embedding"])

    def test_document_measurement_is_isolated_by_document_and_half_open_window(self):
        document = self.document()
        other_document = self.document()
        self.usage(document_id=document.id, operation="document_embedding",
                   input_tokens=100, output_tokens=0, cost="0.0001000000")
        for stamp in (self.start - timedelta(microseconds=1), self.end):
            self.usage(document_id=document.id, operation="document_embedding",
                       input_tokens=9999, output_tokens=0, cost="9.0000000000",
                       occurred_at=stamp)
        self.usage(document_id=other_document.id, operation="document_embedding",
                   input_tokens=7777, output_tokens=0, cost="7.0000000000")
        data = self.report(self.payload(requests=[], documents=[document.id]))
        self.assertEqual(data["totals"]["attempts"], 1)
        self.assertEqual(data["totals"]["input_tokens"], 100)
        self.assertEqual(data["totals"]["total_cost"], "0.0001000000")
        self.assertEqual(data["documents"][0]["exists"], True)
        self.assertEqual(data["documents"][0]["attempts"], 1)
        self.assertEqual(data["documents"][0]["coverage_status"], "complete")

    def test_missing_document_and_no_embedding_rows_are_explicit_unknowns(self):
        missing = uuid.uuid4()
        present = self.document()
        data = self.report(self.payload(requests=[], documents=[missing, present.id]))
        self.assertEqual(data["coverage"]["missing_documents"], 1)
        self.assertEqual(data["coverage"]["attempts_missing_documents"], 2)
        self.assertIsNone(data["totals"]["total_cost"])
        self.assertEqual(data["documents"][0]["exists"], False)
        self.assertIn("document_not_found", data["documents"][0]["reasons"])
        self.assertEqual(data["documents"][1]["exists"], True)
        self.assertIn("no_document_embedding_rows", data["documents"][1]["reasons"])

    def test_row_limit_truncation_is_unknown_and_never_summed_as_total(self):
        turn = self.turn()
        self.usage(turn_id=turn.id, cost="0.0010000000")
        self.usage(turn_id=turn.id, cost="0.0020000000")
        with patch.object(usage_measurement, "MEASUREMENT_MAX_ROWS", 1):
            data = self.report(self.payload(requests=[(self.member, turn.request_id)]))
        self.assertEqual(data["coverage"]["truncated"], True)
        self.assertEqual(data["coverage"]["status"], "unknown")
        self.assertIsNone(data["totals"]["total_cost"])
        self.assertIn("results_truncated", data["requests"][0]["reasons"])
        self.assertEqual(data["totals"]["attempts"], 1)

    def test_query_failure_is_503_without_sql_or_connection_details(self):
        db = MagicMock()
        db.execute.side_effect = OperationalError(
            "SELECT secret FROM somewhere", {}, Exception("connection refused"))
        request = UsageMeasurementRequest(
            start=self.start, end=self.end,
            requests=[{"user_id": self.member.id, "request_id": uuid.uuid4()}] if hasattr(self, "member")
            else [],
        )
        with self.assertRaises(Exception) as error:
            usage_measurement.measure(db, request)
        self.assertEqual(getattr(error.exception, "status_code", None), 503)
        self.assertNotIn("SELECT", str(error.exception.detail))
        self.assertNotIn("connection refused", str(error.exception.detail))


if __name__ == "__main__":
    unittest.main()
