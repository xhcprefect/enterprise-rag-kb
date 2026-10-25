"""部署配置和问答准入限制。"""
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
