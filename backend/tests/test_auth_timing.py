"""登录验证次数与 dummy hash 成本：未知用户/错误密码/正确用户各恰好一次校验。

不依赖墙钟计时；用 verify_password 调用次数与 dummy hash 的成本参数断言。
"""
import os
import unittest
import uuid
from unittest.mock import patch

os.environ["RAG_DISABLE_DOTENV"] = "1"
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1:9/unused")
os.environ.setdefault("JWT_SECRET", "regression-tests-only-not-for-production")

from fastapi import HTTPException
from starlette.requests import Request
from sqlalchemy.orm import Session

from app.api.v1 import auth
from app.core.security import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.schemas.auth import LoginRequest


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/v1/auth/login",
                    "headers": [], "client": ("127.0.0.1", 1234)})


class FakeUser:
    def __init__(self, password_hash, is_active=True, role="user"):
        self.id = uuid.uuid4()
        self.username = "login_probe"
        self.password_hash = password_hash
        self.is_active = is_active
        self.role = role
        self.token_version = 0


class FakeSession:
    def __init__(self, user):
        self.user = user
        self.commits = 0

    def scalar(self, statement):
        return self.user

    def commit(self):
        self.commits += 1


class LoginVerificationTests(unittest.TestCase):
    def _login(self, user, password):
        db = FakeSession(user)
        with patch.object(auth, "write_audit"), \
             patch.object(auth, "create_access_token", return_value="token"):
            try:
                auth.login(LoginRequest(username="login_probe", password=password), make_request(), db)
                return "ok", db
            except HTTPException as exc:
                return exc.status_code, db

    def test_unknown_user_verifies_dummy_exactly_once(self):
        calls = []
        real = verify_password

        def spy(password, hashed):
            calls.append(hashed)
            return real(password, hashed)

        with patch.object(auth, "verify_password", side_effect=spy):
            status, db = self._login(None, "whatever-password")
        self.assertEqual(status, 401)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], DUMMY_PASSWORD_HASH)

    def test_wrong_password_verifies_real_hash_exactly_once_without_dummy(self):
        stored = hash_password("CorrectPass123")
        user = FakeUser(stored)
        calls = []
        real = verify_password

        def spy(password, hashed):
            calls.append(hashed)
            return real(password, hashed)

        with patch.object(auth, "verify_password", side_effect=spy):
            status, _ = self._login(user, "WrongPass123")
        self.assertEqual(status, 401)
        self.assertEqual(calls, [stored])

    def test_correct_password_verifies_real_hash_exactly_once(self):
        stored = hash_password("CorrectPass123")
        user = FakeUser(stored)
        calls = []
        real = verify_password

        def spy(password, hashed):
            calls.append(hashed)
            return real(password, hashed)

        with patch.object(auth, "verify_password", side_effect=spy):
            status, _ = self._login(user, "CorrectPass123")
        self.assertEqual(status, "ok")
        self.assertEqual(calls, [stored])

    def test_dummy_hash_is_static_and_same_cost_as_real_hash(self):
        # 同一对象、每次请求相同：不是每请求新生成。
        self.assertIs(auth.DUMMY_PASSWORD_HASH, DUMMY_PASSWORD_HASH)
        self.assertTrue(DUMMY_PASSWORD_HASH.startswith("bcrypt-sha256:v1:$2b$"))
        dummy_cost = DUMMY_PASSWORD_HASH.split("$")[2]
        real_cost = hash_password("anything").split("$")[2]
        self.assertEqual(dummy_cost, real_cost)
        self.assertFalse(verify_password("anyone-can-try-this", DUMMY_PASSWORD_HASH))

    def test_legacy_bcrypt_hash_still_logs_in_and_upgrades(self):
        import bcrypt

        legacy = bcrypt.hashpw(b"LegacyPass123", bcrypt.gensalt()).decode()
        user = FakeUser(legacy)
        status, db = self._login(user, "LegacyPass123")
        self.assertEqual(status, "ok")
        self.assertTrue(user.password_hash.startswith("bcrypt-sha256:v1:"))
        self.assertTrue(verify_password("LegacyPass123", user.password_hash))
        self.assertEqual(db.commits, 1)  # 升级提交

    def test_disabled_account_still_checks_password_once_then_403(self):
        stored = hash_password("CorrectPass123")
        user = FakeUser(stored, is_active=False)
        calls = []
        real = verify_password

        def spy(password, hashed):
            calls.append(hashed)
            return real(password, hashed)

        with patch.object(auth, "verify_password", side_effect=spy):
            status, _ = self._login(user, "CorrectPass123")
        self.assertEqual(status, 403)
        self.assertEqual(calls, [stored])


class DummyHashConstantTests(unittest.TestCase):
    def test_dummy_hash_is_stable_across_imports(self):
        import importlib

        first = importlib.import_module("app.core.security").DUMMY_PASSWORD_HASH
        second = importlib.import_module("app.core.security").DUMMY_PASSWORD_HASH
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
