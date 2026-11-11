import logging
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, require_admin, write_audit
from app.core.security import hash_password, verify_password
from app.db.session import get_db
from app.models import (
    ChatTurn,
    Conversation,
    Document,
    KnowledgeBase,
    KnowledgeBaseMember,
    Message,
    ProjectSnapshot,
    ProjectSource,
    User,
)
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
    user = db.scalar(select(User).where(User.id == user_id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    data = payload.model_dump(exclude_unset=True)
    password = data.pop("password", None)
    if password:
        user.password_hash = hash_password(password)
    revoke = bool(password) or any(field in data and data[field] != getattr(user, field) for field in ("role", "is_active"))
    for field, value in data.items():
        setattr(user, field, value)

    # 不允许管理员把自己降级或禁用，避免锁死
    if user.id == admin.id and (data.get("role") == "user" or data.get("is_active") is False):
        raise HTTPException(status_code=400, detail="不能修改自己的角色或禁用自己")

    if revoke:
        user.token_version += 1
    db.commit()
    db.refresh(user)
    changed = list(data) + (["password"] if password else [])
    logger.info("更新用户: %s 字段=%s 操作人=%s", user.username, changed, admin.username)
    write_audit(db, request, admin, "user_update", user.username, {"fields": changed})
    return ApiResponse(data=user, message="用户已更新")


def _lock_sorted(db: DBSession, model, condition):
    """按主键 UUID 顺序锁一组行（`FOR UPDATE` + 强制从 DB 重读）。

    只接受可被索引限制的谓词（owner_id / user_id / uploaded_by / created_by /
    conversation_id / kb_id 上的等值或 IN），绝不整表加锁。
    """
    return db.scalars(
        select(model).where(condition).order_by(model.id)
        .with_for_update().execution_options(populate_existing=True)
    ).all()


@router.delete("/{user_id}", response_model=ApiResponse[None], summary="删除用户（管理员）")
def delete_user(
    user_id: uuid.UUID,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """删除用户：按全库统一锁序锁定**有限且稳定**的受影响集合后再删。

    目标用户行锁先取得（``FOR UPDATE``）：任何并发请求要新建指向该用户的
    FK 引用（会话/轮次/上传者/创建者）都必须先取该行的 KEY SHARE，因此会被
    挡住；锁住之后查到的集合就是删除时真正会变的行。

    锁序 ``User -> KnowledgeBase(UUID) -> ProjectSource(UUID) ->
    ProjectSnapshot(UUID) -> Conversation(UUID) -> ChatTurn(UUID) ->
    Message(UUID) -> Document(UUID)``，与最终发布/撤销事务一致，避免交叉成环：

    - 目标 ``owner_id`` 的知识库（``SET NULL``），以及其中的源/快照/文档；
    - 目标自己创建的项目源、目标在**他人**知识库上传的文档；
    - 目标自己的会话，以及挂在目标知识库下的**他人**会话 —— 后者不被删除，
      但其 ``kb_id`` 所属库的属主变更必须与最终发布的 KB 锁互斥。
    """
    if user_id == admin.id:
        raise HTTPException(status_code=400, detail="不能删除自己")

    version = getattr(request.state, "auth_token_version", None)
    if type(version) is not int or version < 0:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    # 管理员与目标用户一次按 UUID 排序锁定，两个管理员互删也不会反向取 User 锁。
    locked_users = {row.id: row for row in _lock_sorted(db, User, User.id.in_([admin.id, user_id]))}
    admin = locked_users.get(admin.id)
    if admin is None or not admin.is_active or not admin.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    if version != admin.token_version:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    request.state.user = admin
    user = locked_users.get(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 目标知识库（属主将被置空）∪ 目标当前显式成员 KB：一次按 UUID 稳定锁定。
    # 成员行读取发生在目标 User 行锁之后——成员写入口（PUT/DELETE）必须先锁目标
    # User，因此这里的成员集合不会被同目标的并发写入改变；集合大小由该用户的
    # 成员记录数决定，有界。
    member_kb_ids = set(db.scalars(
        select(KnowledgeBaseMember.kb_id).where(KnowledgeBaseMember.user_id == user.id)
    ).all())
    kb_condition = KnowledgeBase.owner_id == user.id
    if member_kb_ids:
        kb_condition = or_(kb_condition, KnowledgeBase.id.in_(member_kb_ids))
    locked_kbs = _lock_sorted(db, KnowledgeBase, kb_condition)
    # 只保留原“属主 KB”集合供后续源/会话/文档处理；成员 KB 不混入这份资源边界。
    owned_kbs = [kb for kb in locked_kbs if kb.owner_id == user.id]
    kb_ids = [kb.id for kb in owned_kbs]
    # FK CASCADE 会删除成员行：为确有成员行的 KB 同步 revision，避免成员列表
    # 已变而 CAS 版本不动。无成员行的 KB（只是成员集合里的旧 ID）不碰。
    for kb in locked_kbs:
        if kb.id in member_kb_ids:
            kb.membership_revision = int(kb.membership_revision) + 1

    # 目标创建的项目源，以及目标知识库下的源；随后是其快照。
    source_conditions = [ProjectSource.created_by == user.id]
    if kb_ids:
        source_conditions.append(ProjectSource.kb_id.in_(kb_ids))
    sources = _lock_sorted(db, ProjectSource, or_(*source_conditions))
    source_ids = [source.id for source in sources]
    if source_ids:
        _lock_sorted(db, ProjectSnapshot, ProjectSnapshot.source_id.in_(source_ids))

    # 会话：目标自己的会话（将被级联删除）与目标知识库下的他人会话。
    conversation_conditions = [Conversation.user_id == user.id]
    if kb_ids:
        conversation_conditions.append(Conversation.kb_id.in_(kb_ids))
    conversations = _lock_sorted(db, Conversation, or_(*conversation_conditions))
    conversation_ids = [conversation.id for conversation in conversations]

    # 轮次：目标发起的（任意会话，含会话已删除后保留的 tombstone）以及上述
    # 会话里的全部轮次。即使目标没有存活会话也要锁 —— 这些行同样随用户级联删除。
    turn_conditions = [ChatTurn.user_id == user.id]
    if conversation_ids:
        turn_conditions.append(ChatTurn.conversation_id.in_(conversation_ids))
    _lock_sorted(db, ChatTurn, or_(*turn_conditions))

    # 消息：上述会话内全部 + 目标轮次的消息（防御会话绑定缺失的历史行）。
    # 用子查询而不是把 turn id 拉成 Python 列表：锁语句本身保持有界。
    message_conditions = [
        Message.turn_id.in_(select(ChatTurn.id).where(ChatTurn.user_id == user.id)),
    ]
    if conversation_ids:
        message_conditions.append(Message.conversation_id.in_(conversation_ids))
    _lock_sorted(db, Message, or_(*message_conditions))

    # 上传文档最后锁（统一类内顺序）：目标上传的，以及目标知识库内的。
    document_conditions = [Document.uploaded_by == user.id]
    if kb_ids:
        document_conditions.append(Document.kb_id.in_(kb_ids))
    _lock_sorted(db, Document, or_(*document_conditions))

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
    user = db.scalar(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True))
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")
    if not verify_password(payload.old_password, user.password_hash):
        logger.warning("修改密码失败（原密码错误）: %s", user.username)
        raise HTTPException(status_code=400, detail="原密码错误")
    user.password_hash = hash_password(payload.new_password)
    user.token_version += 1
    db.commit()
    logger.info("修改密码成功: %s", user.username)
    write_audit(db, request, user, "password_change", user.username)
    return ApiResponse(data=None, message="密码已更新")
