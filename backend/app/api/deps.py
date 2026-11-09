"""公共依赖：当前用户、角色校验、审计记录。"""
from collections.abc import Generator
import logging
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.observability import metrics
from app.core.security import decode_access_token
from app.db.session import get_db
from app.models import AuditLog, User

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False)

# 固定的日志事件名；日志行只允许再附加"安全异常类型"，
# 绝不带 SQL、异常正文、target/detail、用户名、IP、请求头或 URL。
AUDIT_WRITE_FAILED_EVENT = "audit_write_failed"
AUDIT_ROLLBACK_FAILED_EVENT = "audit_rollback_failed"


def _safe_exception_type(exc: BaseException) -> str:
    """只保留类名中的 ASCII 字母/数字/下划线，避免异常类名本身携带脏字符。"""
    name = "".join(ch for ch in type(exc).__name__ if ch.isascii() and (ch.isalnum() or ch == "_"))
    return name[:64] or "Exception"


def _log_audit_failure(event: str, exc: BaseException) -> None:
    """best-effort 打日志：handler/formatter 自身抛错也不能打断业务或跳过 rollback。"""
    try:
        logger.error("%s exc_type=%s", event, _safe_exception_type(exc))
    except Exception:  # noqa: BLE001 - 审计观测不能反过来变成故障源
        pass


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
    version = payload.get("ver", 0)
    if type(version) is not int or version != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效，请重新登录")
    # 认证通过时刻的原始令牌版本快照：只在这里捕获一次。
    # 后续治理写入口必须依赖该快照（而不是重新解码令牌，或读取可能已被刷新/改写的 ORM 实例）。
    request.state.auth_token_version = version
    # 供请求日志中间件记录"是谁在调接口"
    request.state.user = user
    return user


def admit_chat(user: User = Depends(get_current_user)) -> Generator[None, None, None]:
    # request 生命周期依赖：流结束、异常、客户端断开都会归还名额。
    from app.core.limits import chat_admission
    with chat_admission.slot(str(user.id)):
        yield


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
    """写审计日志；失败不影响主流程，但必须可观测。

    构造模型、``add``、``commit`` 任一步失败都会：

    - 让 ``ragkb_audit_write_failures_total`` 恰好 +1（无标签，避免业务数据进指标）；
    - 输出一条 ERROR 日志，内容只有固定事件名和安全异常类型；
    - 尝试 ``rollback`` 清掉脏会话，rollback 自身失败也不会抛出。

    日志与指标都不包含 SQL、``str(exc)``、traceback、target/detail、用户名、IP、
    请求头或 URL —— 这些正是审计记录里最敏感的部分。
    """
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
    except Exception as exc:  # noqa: BLE001 - 审计失败不应阻断业务
        # 先计数再打日志：即使日志 handler 自身异常，失败仍然可被采集到。
        # 同一失败写入只 +1，rollback 失败不重复计数。
        metrics.audit_write_failed()
        # 日志失败受局部保护：绝不让它跳过 rollback 或逃出调用栈。
        _log_audit_failure(AUDIT_WRITE_FAILED_EVENT, exc)
        try:
            db.rollback()
        except Exception as rollback_exc:  # noqa: BLE001 - rollback 失败也不得逃出
            _log_audit_failure(AUDIT_ROLLBACK_FAILED_EVENT, rollback_exc)
