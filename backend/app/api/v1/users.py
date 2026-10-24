import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, require_admin, write_audit
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.user import ChangePassword, UserCreate, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["用户管理"])
logger = logging.getLogger(__name__)


@router.get("", response_model=ApiResponse[PageResult[UserRead]], summary="用户列表（管理员）")
def list_users(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    db: DBSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    stmt = select(User)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(User.username.ilike(like) | User.display_name.ilike(like))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return ApiResponse(
        data=PageResult(
            total=total,
            items=[UserRead.model_validate(r) for r in rows],
        )
    )


@router.post(
    "",
    response_model=ApiResponse[UserRead],
    status_code=status.HTTP_201_CREATED,
    summary="创建用户（管理员）",
)
def create_user(
    payload: UserCreate,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(status_code=400, detail="用户名已存在")

    user = User(
        username=payload.username,
        display_name=payload.display_name or payload.username,
        email=payload.email,
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info(
        "创建用户: %s (role=%s) 操作人=%s", user.username, user.role, admin.username
    )
    write_audit(db, request, admin, "user_create", user.username, {"role": user.role})
    return ApiResponse(data=user, message="用户已创建")


@router.patch("/{user_id}", response_model=ApiResponse[UserRead], summary="更新用户（管理员）")
def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    data = payload.model_dump(exclude_unset=True)
    password = data.pop("password", None)
    if password:
        user.password_hash = hash_password(password)
    for field, value in data.items():
        setattr(user, field, value)

    # 不允许管理员把自己降级或禁用，避免锁死
    if user.id == admin.id and (data.get("role") == "user" or data.get("is_active") is False):
        raise HTTPException(status_code=400, detail="不能修改自己的角色或禁用自己")

    db.commit()
    db.refresh(user)
    changed = list(data) + (["password"] if password else [])
    logger.info("更新用户: %s 字段=%s 操作人=%s", user.username, changed, admin.username)
    write_audit(db, request, admin, "user_update", user.username, {"fields": changed})
    return ApiResponse(data=user, message="用户已更新")


@router.delete("/{user_id}", response_model=ApiResponse[None], summary="删除用户（管理员）")
def delete_user(
    user_id: uuid.UUID,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除自己")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    username = user.username
    db.delete(user)
    db.commit()
    logger.info("删除用户: %s 操作人=%s", username, admin.username)
    write_audit(db, request, admin, "user_delete", username)
    return ApiResponse(data=None, message="已删除")


@router.post(
    "/change-password", response_model=ApiResponse[None], summary="修改自己的密码"
)
def change_password(
    payload: ChangePassword,
    request: Request,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        logger.warning("修改密码失败（原密码错误）: %s", user.username)
        raise HTTPException(status_code=400, detail="原密码错误")
    user.password_hash = hash_password(payload.new_password)
    db.commit()
    logger.info("修改密码成功: %s", user.username)
    write_audit(db, request, user, "password_change", user.username)
    return ApiResponse(data=None, message="密码已更新")
