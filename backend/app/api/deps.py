"""公共依赖：当前用户、角色校验、审计记录。"""
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import AuditLog, User

bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录或令牌缺失")

    payload = decode_access_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")

    try:
        user_id = uuid.UUID(payload.get("sub", ""))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已被禁用")
    # 供请求日志中间件记录"是谁在调接口"
    request.state.user = user
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


def write_audit(
    db: Session,
    request: Request | None,
    user: User | None,
    action: str,
    target: str = "",
    detail: dict | None = None,
) -> None:
    """写审计日志；失败不影响主流程。"""
    try:
        log = AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else "",
            action=action,
            target=target,
            detail=detail,
            ip=request.client.host if request and request.client else "",
            user_agent=request.headers.get("user-agent", "") if request else "",
        )
        db.add(log)
        db.commit()
    except Exception:  # noqa: BLE001 - 审计失败不应阻断业务
        db.rollback()
