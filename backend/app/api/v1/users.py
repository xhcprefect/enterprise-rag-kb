import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, require_admin, write_audit
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models import User
from app.schemas.common import ApiResponse, PageResult
from app.schemas.user import (
    ChangePassword,
    ImportFailureItem,
    ImportedUser,
    ImportResult,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services import user_import

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


@router.post(
    "/import",
    response_model=ApiResponse[ImportResult],
    summary="批量导入用户（管理员）",
)
def import_users(
    request: Request,
    file: UploadFile = File(...),
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """从 Excel(.xlsx) / CSV 批量建号，每人一个随机初始密码。

    尽力而为：单行不合法不影响其它行，响应里逐行给出结果，并附带一份
    含明文初始密码的回执（base64 xlsx）供下载——密码不落库，只有这一次机会拿到。
    """
    # 边读边数，不信任 file.size：Starlette 的 UploadFile 会先把大文件落到临时文件，
    # 直接 read() 等于把上传的东西整个拉进内存。这里超限立刻停，和上传接口的做法一致。
    limit = user_import.MAX_IMPORT_BYTES
    buffer = bytearray()
    while chunk := file.file.read(256 * 1024):
        buffer.extend(chunk)
        if len(buffer) > limit:
            raise HTTPException(
                status_code=413,
                detail=f"文件超过 {limit // 1024 // 1024}MB 限制，请精简后重试",
            )
    data = bytes(buffer)
    try:
        outcome = user_import.import_users(db, data, file.filename or "")
    except user_import.ImportFormatError as exc:
        # 文件本身的问题（格式/表头/行数）是一次 400，而不是逐行失败
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info(
        "批量导入用户: 文件=%s 共%d行 成功%d 失败%d 操作人=%s",
        file.filename or "-", outcome.total, len(outcome.created),
        len(outcome.failed), admin.username,
    )
    write_audit(
        db, request, admin, "user_import", file.filename or "",
        {
            "total": outcome.total,
            "created": len(outcome.created),
            "failed": len(outcome.failed),
            "roles": {r: sum(1 for u in outcome.created if u.role == r) for r in ("admin", "user")},
        },
    )

    return ApiResponse(
        data=ImportResult(
            total=outcome.total,
            created=[ImportedUser(**vars(u)) for u in outcome.created],
            failed=[ImportFailureItem(**vars(f)) for f in outcome.failed],
            receipt_filename=outcome.receipt_filename,
            receipt_base64=user_import.receipt_b64(outcome),
        ),
        message=f"导入完成：成功 {len(outcome.created)} 个，失败 {len(outcome.failed)} 个",
    )


@router.get("/import/template", summary="下载用户导入模板（管理员）")
def download_import_template(_: User = Depends(require_admin)):
    """表头 + 角色下拉 + 填写说明。不含示例数据，避免被当成真实账号导入。"""
    filename, payload = user_import.build_template()
    # 中文文件名要按 RFC 5987 编码，否则部分浏览器会存成乱码
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f"attachment; filename=user-import-template.xlsx; "
                f"filename*=UTF-8''{quote(filename)}"
            )
        },
    )


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
