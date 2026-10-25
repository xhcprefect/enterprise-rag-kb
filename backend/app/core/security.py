"""密码哈希与可撤销的 JWT 令牌。

新密码先做带用途隔离的 SHA-256，再用 bcrypt 加盐哈希，避免 72 字节截断。
旧的裸 bcrypt 哈希仍可验证，成功登录时升级；数据库中不保存明文密码。
"""
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import settings

_PASSWORD_PREFIX = "bcrypt-sha256:v1:"
_PASSWORD_DOMAIN = b"enterprise-rag-kb/password/v1\x00"


def _encode(password: str) -> bytes:
    digest = hashlib.sha256(_PASSWORD_DOMAIN + password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    return _PASSWORD_PREFIX + bcrypt.hashpw(_encode(password), bcrypt.gensalt()).decode("ascii")


def password_needs_upgrade(hashed: str) -> bool:
    return not hashed.startswith(_PASSWORD_PREFIX)


def verify_password(password: str, hashed: str) -> bool:
    try:
        if hashed.startswith(_PASSWORD_PREFIX):
            return bcrypt.checkpw(_encode(password), hashed[len(_PASSWORD_PREFIX):].encode("ascii"))
        # 仅兼容已有账号；新密码绝不截断。旧哈希无法恢复原密码的第 73 字节之后。
        if hashed.startswith(("$2a$", "$2b$", "$2y$")):
            return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode("ascii"))
        return False
    except (ValueError, TypeError, UnicodeError):
        return False


def create_access_token(
    subject: str, role: str, expires_minutes: int | None = None, *, token_version: int = 0
) -> str:
    now = datetime.now(timezone.utc)
    expire = now + timedelta(
        minutes=expires_minutes if expires_minutes is not None else settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "ver": token_version,
        "exp": expire,
        "iat": now,
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        return jwt.decode(
            token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "exp", "iat"]},
        )
    except jwt.PyJWTError:
        return None
