import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, write_audit
from app.core.config import settings
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    hash_password,
    password_needs_upgrade,
    verify_password,
)
from app.db.session import get_db
from app.models import User
from app.schemas.auth import LoginRequest, TokenResponse
from app.schemas.common import ApiResponse
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["认证"])
logger = logging.getLogger(__name__)


@router.post("/login", response_model=ApiResponse[TokenResponse], summary="用户名密码登录")
def login(payload: LoginRequest, request: Request, db: DBSession = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == payload.username).with_for_update())
    ip = request.client.host if request.client else "-"

    if user is None:
        # 未知用户名也做一次同成本校验：否则"账号不存在"会因跳过 bcrypt 而明显更快。
        # 使用模块级固定 dummy hash（进程启动即固定，不是每请求生成），
        # 每个请求只做这一次校验；真实账号分支只校验真实 hash 一次。
        verify_password(payload.password, DUMMY_PASSWORD_HASH)
        logger.warning("登录失败: username=%s ip=%s 原因=用户不存在", payload.username, ip)
        write_audit(db, request, None, "login_failed", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )

    if not verify_password(payload.password, user.password_hash):
        logger.warning("登录失败: username=%s ip=%s 原因=密码错误", payload.username, ip)
        write_audit(db, request, user, "login_failed", payload.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        )
    if not user.is_active:
        logger.warning("登录被拒绝（账号已禁用）: username=%s ip=%s", payload.username, ip)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")

    if password_needs_upgrade(user.password_hash):
        user.password_hash = hash_password(payload.password)
        db.commit()
    token = create_access_token(str(user.id), user.role, token_version=user.token_version)
    logger.info("登录成功: %s (role=%s) ip=%s", user.username, user.role, ip)
    write_audit(db, request, user, "login_success", user.username)

    return ApiResponse(
        data=TokenResponse(
            access_token=token, expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
        ),
        message="登录成功",
    )


@router.get("/me", response_model=ApiResponse[UserRead], summary="当前登录用户")
def me(user: User = Depends(get_current_user)):
    return ApiResponse(data=user)


@router.post("/logout", response_model=ApiResponse[None], summary="退出登录并撤销该账号现有令牌")
def logout(request: Request, user: User = Depends(get_current_user), db: DBSession = Depends(get_db)):
    db.execute(update(User).where(User.id == user.id).values(token_version=User.token_version + 1))
    db.commit()
    logger.info("退出登录并撤销现有令牌: %s", user.username)
    write_audit(db, request, user, "logout", user.username)
    return ApiResponse(data=None, message="已退出登录")
