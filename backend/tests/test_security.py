"""无需数据库/模型服务的安全回归测试。"""
import io
import unittest
from unittest.mock import patch

import bcrypt
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.core.api_response import sanitized_validation_errors
from app.core.security import hash_password, password_needs_upgrade, verify_password
from app.services.user_import import CreatedUser, build_receipt


class SecurityTests(unittest.TestCase):
    def test_long_passwords_are_distinct(self):
        for prefix in ("A" * 72, "汉" * 25):
            first, second = prefix + "x", prefix + "y"
            hashed = hash_password(first)
            self.assertTrue(verify_password(first, hashed))
            self.assertFalse(verify_password(second, hashed))
            self.assertFalse(password_needs_upgrade(hashed))

    def test_legacy_bcrypt_is_compatible(self):
        hashed = bcrypt.hashpw(b"Legacy123", bcrypt.gensalt()).decode()
        self.assertTrue(verify_password("Legacy123", hashed))
        self.assertTrue(password_needs_upgrade(hashed))
        self.assertFalse(verify_password("wrong", hashed))

    def test_validation_errors_remove_raw_values(self):
        secret = "VALID_SECRET_PASSWORD_123"
        errors = [{"type": "missing", "loc": ["body", "username"], "input": {"password": secret},
                   "msg": secret, "ctx": {"error": secret}}]
        self.assertNotIn(secret, str(sanitized_validation_errors(errors)))

    def test_api_validation_does_not_log_or_return_password(self):
        with patch("app.core.logging.setup_logging"):
            from app import main
        with patch.object(main.logger, "warning") as log, patch("app.core.request_log.logger.warning") as request_log:
            response = TestClient(main.app).post("/api/v1/auth/login", json={"password": "VALID_SECRET_PASSWORD_123"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("VALID_SECRET_PASSWORD_123", response.text)
        self.assertNotIn("VALID_SECRET_PASSWORD_123", repr(log.call_args_list))
        self.assertNotIn("VALID_SECRET_PASSWORD_123", repr(request_log.call_args_list))

    def test_receipt_cells_are_literal_text(self):
        _, data = build_receipt([CreatedUser(2, "test_user", "=1+1", "=2+2", "user", "Secret123")], [], "=1+1")
        wb = load_workbook(io.BytesIO(data), data_only=False)
        try:
            for sheet in wb:
                for row in sheet:
                    for cell in row:
                        self.assertNotEqual(cell.data_type, "f")
        finally:
            wb.close()
