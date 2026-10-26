"""部署配置和问答准入限制。"""
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.core.limits import ChatAdmission


class RuntimeTests(unittest.TestCase):
    def test_production_rejects_template_secret(self):
        with self.assertRaises(ValidationError) as error:
            Settings(_env_file=None, ENVIRONMENT="production", JWT_SECRET="please-change-this-secret-in-production", LLM_API_KEY="never-print-this-key")
        self.assertNotIn("never-print-this-key", str(error.exception))
        config = Settings(_env_file=None, ENVIRONMENT="production", JWT_SECRET="test-random-value-with-at-least-32-characters")
        self.assertEqual(config.ENVIRONMENT, "production")

    def test_context_window_reserves_output_and_safety_before_input(self):
        config = Settings(_env_file=None, LLM_CONTEXT_WINDOW_TOKENS=4096,
                          LLM_MAX_OUTPUT_TOKENS=2048, CONTEXT_TOKEN_SAFETY_MARGIN=512)
        self.assertEqual(config.LLM_CONTEXT_WINDOW_TOKENS - config.LLM_MAX_OUTPUT_TOKENS - config.CONTEXT_TOKEN_SAFETY_MARGIN, 1536)
        for overrides in [dict(LLM_CONTEXT_WINDOW_TOKENS=2048), dict(CONTEXT_TOKEN_SAFETY_MARGIN=40000),
                          dict(LLM_MAX_OUTPUT_TOKENS=32768), dict(HISTORY_MAX_TURNS=-1),
                          dict(HISTORY_MAX_CHARS=-1), dict(RETRIEVAL_REWRITE_TIMEOUT=0)]:
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                Settings(_env_file=None, **overrides)
        no_history = Settings(_env_file=None, HISTORY_MAX_TURNS=0, HISTORY_MAX_CHARS=0)
        self.assertEqual(no_history.HISTORY_MAX_TURNS, 0)

    def test_reasoning_effort_accepts_supported_modes_and_normalizes_env_values(self):
        for mode in ("", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            with self.subTest(mode=mode):
                config = Settings(_env_file=None, LLM_REASONING_EFFORT=mode)
                self.assertEqual(config.LLM_REASONING_EFFORT, mode)
        for value, expected in [(" NONE ", "none"), (" High ", "high"), ("   ", "")]:
            with self.subTest(value=value), patch.dict(os.environ, {"LLM_REASONING_EFFORT": value}):
                self.assertEqual(Settings(_env_file=None).LLM_REASONING_EFFORT, expected)

    def test_invalid_reasoning_effort_fails_at_startup_without_disclosing_keys(self):
        with self.assertRaises(ValidationError) as error:
            Settings(_env_file=None, LLM_REASONING_EFFORT="disabled", LLM_API_KEY="never-print-this-key")
        self.assertIn("LLM_REASONING_EFFORT", str(error.exception))
        self.assertNotIn("never-print-this-key", str(error.exception))

    def test_admission_rejects_overload_and_releases_after_error(self):
        limiter = ChatAdmission()
        with patch.object(settings, "CHAT_MAX_PER_USER", 1), patch.object(settings, "CHAT_MAX_CONCURRENT", 1):
            with limiter.slot("one"):
                for user in ("one", "two"):
                    with self.assertRaises(HTTPException) as error:
                        with limiter.slot(user):
                            pass
                    self.assertEqual(error.exception.status_code, 429)
            with self.assertRaises(RuntimeError):
                with limiter.slot("one"):
                    raise RuntimeError("simulated disconnect")
            with limiter.slot("two"):
                pass
        self.assertEqual(limiter._total, 0)
        self.assertEqual(limiter._users, {})
