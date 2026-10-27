"""实际用量解析、人民币估价、价格校验和上下文隔离，不访问数据库/网络。"""
import contextvars
import unittest
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.usage import PriceUpdate
from app.services import usage_statistics as stats, usage_tracking as tracking


class UsageUnitTests(unittest.TestCase):
    def test_openai_and_langchain_usage_parse_without_double_counting_reasoning(self):
        raw = {"prompt_tokens": 1000, "completion_tokens": 100,
               "prompt_tokens_details": {"cached_tokens": 400},
               "completion_tokens_details": {"reasoning_tokens": 80}, "total_tokens": 1100}
        self.assertEqual(tracking.normalize_usage(raw), {"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 400})
        self.assertEqual(tracking.normalize_usage({"input_tokens": 8, "output_tokens": 2,
                                                  "input_token_details": {"cache_read": 3}})["cached_input_tokens"], 3)
        self.assertEqual(tracking.normalize_usage({"total_tokens": 15}, "embedding"),
                         {"input_tokens": 15, "output_tokens": 0, "cached_input_tokens": None})
        self.assertIsNone(tracking.normalize_usage({"total_tokens": 15}, "chat"))

    def test_deepseek_cache_counts_must_be_consistent_with_total_input(self):
        raw = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 40, "prompt_cache_miss_tokens": 60}
        self.assertEqual(tracking.normalize_usage(raw)["cached_input_tokens"], 40)
        for invalid in [dict(prompt_cache_hit_tokens=101), dict(prompt_cache_miss_tokens=70), dict(prompt_cache_hit_tokens=True)]:
            self.assertIsNone(tracking.normalize_usage({**raw, **invalid})["cached_input_tokens"])

    def test_missing_and_invalid_usage_is_unknown_not_zero(self):
        for raw in [None, [], {}, {"prompt_tokens": True, "completion_tokens": -1},
                    {"prompt_tokens": "10", "completion_tokens": float("nan")},
                    {"input_tokens": tracking.MAX_TOKENS + 1}]:
            with self.subTest(raw=raw):
                self.assertIsNone(tracking.normalize_usage(raw))
        self.assertEqual(tracking.normalize_usage({"prompt_tokens": 0, "completion_tokens": 0})["output_tokens"], 0)
        partial = tracking.normalize_usage({"prompt_tokens": 12})
        self.assertEqual(partial["input_tokens"], 12)
        self.assertIsNone(partial["output_tokens"])

    def test_decimal_cost_uses_cached_and_uncached_input_once(self):
        price = {"input_per_million": Decimal("2"), "output_per_million": Decimal("8"),
                 "cached_input_per_million": Decimal("0.2")}
        costs = tracking.estimate_cost({"input_tokens": 1000, "output_tokens": 100, "cached_input_tokens": 400}, price)
        self.assertEqual(costs, (Decimal("0.0012800000"), Decimal("0.0008000000"), Decimal("0.0020800000")))
        self.assertEqual(tracking.estimate_cost({"input_tokens": 1, "output_tokens": 1},
                         {"input_per_million": Decimal("0.123456"), "output_per_million": Decimal("0.654321")})[2], Decimal("0.0000007778"))

    def test_free_price_is_distinct_from_unconfigured_or_unknown_cache_usage(self):
        usage = {"input_tokens": 100, "output_tokens": 20}
        self.assertEqual(tracking.estimate_cost(usage, {}), (None, None, None))
        self.assertEqual(tracking.estimate_cost(usage, {"input_per_million": Decimal(0), "output_per_million": Decimal(0)})[2], Decimal(0))
        self.assertIsNone(tracking.estimate_cost(usage, {"input_per_million": Decimal(2), "output_per_million": Decimal(8),
                                                       "cached_input_per_million": Decimal("0.2")})[2])
        self.assertIsNotNone(tracking.estimate_cost(usage, {"input_per_million": Decimal(2), "output_per_million": Decimal(8)})[2])
        self.assertIsNone(tracking.estimate_cost({"input_tokens": 100}, {"input_per_million": Decimal(2), "output_per_million": Decimal(8)})[2])

    def test_price_schema_rejects_nonfinite_negative_oversized_and_incomplete_rates(self):
        base = dict(provider="api.example.test", model="model", kind="chat", input_per_million="2", output_per_million="8", expected_revision=0)
        for changes in [dict(input_per_million="NaN"), dict(input_per_million="Infinity"), dict(input_per_million="-1"),
                        dict(output_per_million="1000001"), dict(input_per_million="0.1234567"), dict(output_per_million=None),
                        dict(kind="embedding"), dict(provider=" "), dict(model="x\ny"), dict(expected_revision=True),
                        dict(input_per_million=None, output_per_million=None, cached_input_per_million="1")]:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                PriceUpdate(**{**base, **changes})
        parsed = PriceUpdate(**{**base, "provider": " API.Example.Test ", "model": " model "})
        self.assertEqual(parsed.provider, "api.example.test")
        self.assertEqual(parsed.model, "model")

    def test_scope_is_reset_and_not_shared_across_contexts(self):
        context = tracking.UsageContext(user_id=uuid.uuid4(), operation="rewrite")
        self.assertIsNone(tracking._context.get())
        with tracking.usage_scope(context):
            self.assertEqual(tracking._context.get(), context)
            self.assertIsNone(contextvars.Context().run(tracking._context.get))
        self.assertIsNone(tracking._context.get())
        self.assertEqual(context.for_operation("query_embedding").user_id, context.user_id)
        self.assertEqual(context.operation, "rewrite")

    def test_generator_context_is_set_and_reset_for_each_next_including_close(self):
        context, seen = tracking.UsageContext(user_id=uuid.uuid4()), []
        def source():
            try:
                seen.append(tracking._context.get())
                yield "one"
                seen.append(tracking._context.get())
                yield "two"
            finally:
                seen.append(tracking._context.get())
        wrapped = tracking.iterate_with_context(source(), context)
        self.assertEqual(contextvars.Context().run(next, wrapped), "one")
        self.assertEqual(contextvars.Context().run(next, wrapped), "two")
        contextvars.Context().run(wrapped.close)
        self.assertEqual(seen, [context, context, context])
        self.assertIsNone(tracking._context.get())

    def test_repeated_usage_keeps_reported_cache_details_without_double_counting(self):
        call = tracking.ModelCall("chat", "example.test", "m")
        call.observe({"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 40})
        call.observe({"prompt_tokens": 100, "completion_tokens": 20})
        self.assertEqual(call.usage, {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 40})
        call.observe({"prompt_tokens": 200, "completion_tokens": 30})
        self.assertIsNone(call.usage["cached_input_tokens"])

    def test_calls_without_application_context_never_open_a_database(self):
        with patch.object(tracking, "SessionLocal") as sessions:
            call = tracking.ModelCall("chat", "example.test", "m")
            call.observe({"prompt_tokens": 10, "completion_tokens": 2})
            call.finish()
        sessions.assert_not_called()
        self.assertFalse(call.enabled)

    def test_stats_rejects_oversized_dates_and_global_access_before_reading_rows(self):
        for start, end, grain in [(date(2026, 2, 1), date(2026, 1, 1), "day"),
                                  (date(2025, 1, 1), date(2026, 1, 2), "day"),
                                  (date(2026, 1, 1), date(2026, 2, 1), "hour")]:
            with self.subTest(grain=grain), self.assertRaises(HTTPException):
                stats.date_range(start, end, grain)
        user, db = MagicMock(), MagicMock()
        user.is_admin = False
        with self.assertRaises(HTTPException) as error:
            stats.statistics(db, user, scope="all")
        self.assertEqual(error.exception.status_code, 403)
        db.execute.assert_not_called()
