"""普通对话摘要压缩配置默认值与校验（纯单测，不连库；RAG_DISABLE_DOTENV=1）。"""
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import Settings


def _input_budget(config) -> int:
    return config.LLM_CONTEXT_WINDOW_TOKENS - config.LLM_MAX_OUTPUT_TOKENS - config.CONTEXT_TOKEN_SAFETY_MARGIN


class MemorySummaryConfigTests(unittest.TestCase):
    def test_memory_feature_disabled_by_default(self):
        config = Settings(_env_file=None)
        self.assertFalse(config.CHAT_MEMORY_ENABLED)
        # 关闭时不新增后台模型调用，历史行为不变。
        self.assertFalse(config.CHAT_MEMORY_ENABLED)

    def test_memory_defaults_are_sane_and_unbounded_segments_removed(self):
        config = Settings(_env_file=None)
        self.assertEqual(config.MEMORY_HISTORY_TOKEN_BUDGET, 8000)
        self.assertEqual(config.MEMORY_TRIGGER_RATIO, 0.75)
        self.assertEqual(config.MEMORY_TRIGGER_TURNS, 8)
        self.assertEqual(config.MEMORY_KEEP_RECENT_TURNS, 4)
        self.assertEqual(config.MEMORY_SUMMARY_MAX_TOKENS, 1200)
        self.assertEqual(config.MEMORY_MAX_ATTEMPTS, 3)
        self.assertEqual(config.MEMORY_JOB_LEASE_SECONDS, 300)
        self.assertEqual(config.MEMORY_MAX_INPUT_TURNS, 200)
        # 默认预算未超过可用输入预算。
        self.assertLessEqual(
            config.MEMORY_HISTORY_TOKEN_BUDGET,
            _input_budget(config),
        )

    def test_enabling_memory_with_over_budget_fails(self):
        # 摘要预算不得超过可用输入预算（窗口 - 输出 - 安全余量）。
        overrides = dict(
            CHAT_MEMORY_ENABLED=True,
            LLM_CONTEXT_WINDOW_TOKENS=4096,
            LLM_MAX_OUTPUT_TOKENS=2048,
            CONTEXT_TOKEN_SAFETY_MARGIN=512,
            MEMORY_HISTORY_TOKEN_BUDGET=2048,
        )
        with self.assertRaises(ValidationError) as error:
            Settings(_env_file=None, **overrides)
        self.assertIn("MEMORY_HISTORY_TOKEN_BUDGET", str(error.exception))

    def test_memory_budget_within_limit_accepts(self):
        config = Settings(
            _env_file=None,
            CHAT_MEMORY_ENABLED=True,
            LLM_CONTEXT_WINDOW_TOKENS=32768,
            LLM_MAX_OUTPUT_TOKENS=2048,
            CONTEXT_TOKEN_SAFETY_MARGIN=512,
            MEMORY_HISTORY_TOKEN_BUDGET=8000,
        )
        self.assertTrue(config.CHAT_MEMORY_ENABLED)
        self.assertLessEqual(config.MEMORY_HISTORY_TOKEN_BUDGET, _input_budget(config))

    def test_invalid_ratio_and_boundaries_rejected(self):
        for value in (-0.1, 0.0, 1.1):
            with self.subTest(ratio=value), self.assertRaises(ValidationError):
                Settings(_env_file=None, MEMORY_TRIGGER_RATIO=value)
        for field, bad in (("MEMORY_SUMMARY_MAX_TOKENS", 0), ("MEMORY_TRIGGER_TURNS", 0),
                           ("MEMORY_MAX_INPUT_TURNS", 0), ("MEMORY_HISTORY_TOKEN_BUDGET", 128)):
            with self.subTest(field=field, bad=bad), self.assertRaises(ValidationError):
                Settings(_env_file=None, **{field: bad})

    def test_env_override_reads_strings_and_keeps_types(self):
        with patch.dict("os.environ", {
            "CHAT_MEMORY_ENABLED": "true",
            "MEMORY_TRIGGER_RATIO": "0.9",
            "MEMORY_KEEP_RECENT_TURNS": "6",
        }):
            config = Settings(_env_file=None)
            self.assertTrue(config.CHAT_MEMORY_ENABLED)
            self.assertEqual(config.MEMORY_TRIGGER_RATIO, 0.9)
            self.assertEqual(config.MEMORY_KEEP_RECENT_TURNS, 6)


if __name__ == "__main__":
    unittest.main()
