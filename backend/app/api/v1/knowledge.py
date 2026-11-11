"""知识库、文档与内容校验的分片上传接口。"""
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user, require_admin, write_audit
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.models import (
    KNOWLEDGE_BASE_KIND_PROJECT,
    KNOWLEDGE_BASE_KIND_UPLOAD,
    ChatTurn,
    Conversation,
    Document,
    DocumentChunk,
    DocumentStatus,
    KnowledgeBase,
    Message,
    UploadSession,
    UploadStatus,
    User,
)
from app.schemas.common import ApiResponse, PageResult
from app.schemas.document_comparison import DocumentComparisonRead
from app.schemas.inspection import (
    DocumentChunkPage, DocumentQualityRead, RetrievalDiagnosticsRead, RetrievalDiagnosticsRequest,
)
from app.schemas.knowledge import (
    DocumentGovernanceUpdate, DocumentRead, DocumentReenableRequest, DocumentSupersedeRequest,
    DocumentUploadRead, KnowledgeBaseCreate, KnowledgeBaseRead, KnowledgeBaseUpdate, SourceRead,
    UploadChunkRead, UploadCompleteRead, UploadSessionCreate, UploadSessionRead,
)
from app.services import (
    chat_turns, document_comparison, document_identity, document_lifecycle,
    document_governance, document_quality, kb_access, project_sources, retrieval_diagnostics,
    source_documents,
)
from app.services import upload as upload_service
from app.services.ingest import reset_document_for_reindex
from app.services.parser import SUPPORTED_EXTENSIONS
from app.services.vector_store import delete_document_vectors, get_vector_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])

# 文件搜索词的硬上限：超出部分截断，避免超长 LIKE 拖慢列表查询。
SEARCH_MAX_LENGTH = 200

_AUTH_INVALID = "登录状态已失效，请重新登录"
_MAINTENANCE_DENIED = "无权维护该知识库的文档"


@dataclass(frozen=True)
class _Actor:
    """审计用不可变身份快照：提交/刷新后不再读认证 ORM 实例。"""

    id: uuid.UUID
    username: str



def _get_kb_or_404(db: DBSession, kb_id: uuid.UUID, *, lock: bool = False) -> KnowledgeBase:
    stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    kb = db.scalar(stmt)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


def _get_kb_for_user(db: DBSession, kb_id: uuid.UUID, user: User) -> KnowledgeBase:
    """普通读取入口（列表项/详情/原文）：统一走成员读取 ACL。

    admin/public/owner 的旧语义由 ``can_read_kb`` 兼容，显式 reader/maintainer
    补充私有库读取权；不可见一律 404，不泄露私有库是否存在。
    """
    kb = _get_kb_or_404(db, kb_id)
    if not kb_access.can_read_kb(db, kb, user):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


def _reject_project_kb(kb: KnowledgeBase) -> None:
    """上传/分片/重排/取消等写入口只接受 ``kind='upload'``。

    ``project`` 库只能通过项目 API 登记管理，任何普通上传入口都不得执行，防止
    绕过项目索引写入不可变原文；未知 ``kind`` 同样没有旁路，一律 400。
    """
    if kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        raise HTTPException(status_code=400, detail="项目源码库不支持此操作")
    if kb.kind != KNOWLEDGE_BASE_KIND_UPLOAD:
        raise HTTPException(status_code=400, detail="该知识库类型不支持此操作")


def _request_token_version(request: Request) -> int | None:
    """取认证依赖在本次请求上留下的原始 ``ver`` 快照；缺失或非 int 返回 None。

    只用于治理写入口的"新鲜校验"：即使依赖层已经验过，管理员权限与令牌版本
    也可能在请求排队期间被撤销，必须在写入事务里按锁定后的 User 行复核。

    这里**不再重新解码**令牌：请求排队到取锁之间令牌可能已过期或签名失效，
    重新解码失败会得到 None 并让旧实现跳过版本校验（fail open）。快照缺失
    （例如绕过认证依赖的直接调用）一律视为无效，由调用方 fail closed。
    """
    try:
        state = request.state
    except Exception:  # noqa: BLE001 - 直接调用场景可能是极简 Request
        return None
    version = getattr(state, "auth_token_version", None)
    return version if type(version) is int else None


def _request_identity(request: Request, user: User) -> tuple[uuid.UUID, int, _Actor]:
    """请求开始处捕获不可变的 ``user_id`` / 原 token 版本 / 审计身份快照。

    之后即使 commit/rollback 刷新或过期了认证 ORM 实例，写路径也只使用这些
    原始值；绝不从已刷新的 User 反推令牌版本。
    """
    version = _request_token_version(request)
    if version is None or version < 0:
        raise HTTPException(status_code=401, detail=_AUTH_INVALID)
    user_id = user.id
    return user_id, version, _Actor(id=user_id, username=getattr(user, "username", ""))


def _maintenance_gate(
    db: DBSession, kb_id: uuid.UUID, user_id: uuid.UUID, auth_token_version,
) -> tuple[User, KnowledgeBase]:
    """文档维护写入口的根门禁（锁序 User -> KnowledgeBase -> 成员判定）。

    - ``lock_current_user``：User ``FOR UPDATE`` + ``populate_existing``，复核
      active 与认证期原 token 快照；失败 401。
    - 随后锁**新鲜** KB 行（``FOR UPDATE``），再按当前数据库成员关系判定维护权。
    - 可见性 404 -> 项目/未知 kind 400 -> 无维护权 403：与既有入口语义一致。
    """
    fresh = kb_access.lock_current_user(db, user_id, auth_token_version)
    kb = _get_kb_or_404(db, kb_id, lock=True)
    if not kb_access.can_read_kb(db, kb, fresh):
        raise HTTPException(status_code=404, detail="知识库不存在")
    _reject_project_kb(kb)
    if not kb_access.can_manage_documents(db, kb, fresh):
        raise HTTPException(status_code=403, detail=_MAINTENANCE_DENIED)
    return fresh, kb


def _preview_gate(db: DBSession, kb_id: uuid.UUID, user: User) -> KnowledgeBase:
    """质量/分块/差异预览等维护级只读入口：不取根锁，但按当前库判定维护权。

    只读预览不产生写提交，无需 token 根锁；权限判定仍走统一 ACL，且先 404
    （不可见）再 400（项目库）最后 403（无维护权），不泄露私有库内容。
    """
    kb = _get_kb_or_404(db, kb_id)
    if not kb_access.can_read_kb(db, kb, user):
        raise HTTPException(status_code=404, detail="知识库不存在")
    _reject_project_kb(kb)
    if not kb_access.can_manage_documents(db, kb, user):
        raise HTTPException(status_code=403, detail=_MAINTENANCE_DENIED)
    return kb


def _lock_governance_admin(db: DBSession, request: Request, admin: User) -> User:
    """治理写入口的第一把锁：新鲜锁定 User 并复核管理员身份与令牌版本。

    锁顺序固定为 User -> KnowledgeBase -> Document；这里必须在取 KB 锁之前完成，
    任何失败（禁用/降权/令牌已失效）都不会再碰知识库与文档行。

    版本比较只使用 ``get_current_user`` 在认证成功时写入的原始令牌快照：
    快照缺失或类型不对一律 401（fail closed），绝不因为"取不到版本"就放行。
    """
    token_version = _request_token_version(request)
    if token_version is None or token_version < 0:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    fresh = db.scalar(
        select(User).where(User.id == admin.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    if fresh is None or not fresh.is_active or not fresh.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    if token_version != fresh.token_version:
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    request.state.user = fresh
    return fresh


def _get_doc(db: DBSession, kb_id: uuid.UUID, doc_id: uuid.UUID, *, lock=False) -> Document:
    stmt = select(Document).where(Document.id == doc_id, Document.kb_id == kb_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    doc = db.scalar(stmt)
    if doc is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    return doc


def _can_manage_members(user: User) -> bool:
    """成员管理仍仅限 active admin（项目库也一样）；maintainer 不获得此能力。"""
    return bool(getattr(user, "is_active", False)) and getattr(user, "role", None) == "admin"


def _kb_out(db: DBSession, kb: KnowledgeBase, user: User, count: int | None = None) -> KnowledgeBaseRead:
    """KB 响应：能力字段一律按当前用户与当前库实时计算，不由客户端兜底。"""
    data = KnowledgeBaseRead.model_validate(kb)
    data.document_count = count if count is not None else (db.scalar(
        select(func.count()).select_from(Document).where(Document.kb_id == kb.id)) or 0)
    data.can_manage_documents = kb_access.can_manage_documents(db, kb, user)
    data.can_manage_members = _can_manage_members(user)
    return data


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("数据库删除已提交，源文件清理待重试: %s", path, exc_info=True)


def _check_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型 {ext}")
    return ext


@router.get("", response_model=ApiResponse[list[KnowledgeBaseRead]], summary="知识库列表")
def list_kbs(db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    # 可见性在 SQL 侧用相关 EXISTS 过滤（分页/限制之前），不在 Python 里事后筛。
    stmt = (select(KnowledgeBase)
            .where(kb_access.visible_kb_predicate(user))
            .order_by(KnowledgeBase.created_at.desc()))
    kbs = db.scalars(stmt).all()
    counts = dict(db.execute(select(Document.kb_id, func.count()).where(
        Document.kb_id.in_([kb.id for kb in kbs])).group_by(Document.kb_id)).all()) if kbs else {}
    return ApiResponse(data=[_kb_out(db, kb, user, counts.get(kb.id, 0)) for kb in kbs])


@router.post("", response_model=ApiResponse[KnowledgeBaseRead], status_code=201, summary="创建知识库（管理员）")
def create_kb(payload: KnowledgeBaseCreate, request: Request, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    if db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == payload.name)):
        raise HTTPException(status_code=400, detail="知识库名称已存在")
    kb = KnowledgeBase(name=payload.name, description=payload.description,
                       collection_name=f"kb_{uuid.uuid4().hex}", owner_id=admin.id, is_public=payload.is_public)
    db.add(kb)
    db.commit()
    write_audit(db, request, admin, "kb_create", kb.name)
    return ApiResponse(data=_kb_out(db, kb, admin, 0))


@router.patch("/{kb_id}", response_model=ApiResponse[KnowledgeBaseRead], summary="更新知识库（管理员）")
def update_kb(kb_id: uuid.UUID, payload: KnowledgeBaseUpdate, request: Request, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    kb = _get_kb_or_404(db, kb_id, lock=True)
    data = payload.model_dump(exclude_unset=True)
    if any(value is None for value in data.values()):
        raise HTTPException(status_code=422, detail="知识库字段不能为 null")
    if kb.kind == KNOWLEDGE_BASE_KIND_PROJECT and "is_public" in data:
        # 项目源码库恒定私有：普通 PATCH 不得把它公开（kind 只读、不发 in schema）。
        raise HTTPException(status_code=400, detail="项目源码库不允许修改公开属性")
    if "name" in data and db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == data["name"], KnowledgeBase.id != kb.id)):
        raise HTTPException(status_code=400, detail="知识库名称已存在")
    for field, value in data.items():
        setattr(kb, field, value)
    db.commit()
    write_audit(db, request, admin, "kb_update", kb.name, {"fields": list(data)})
    return ApiResponse(data=_kb_out(db, kb, admin))


@router.delete("/{kb_id}", response_model=ApiResponse[None], summary="删除知识库（管理员）")
def delete_kb(kb_id: uuid.UUID, request: Request, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    # 统一锁序：User -> KnowledgeBase -> (项目源 -> 快照) / (Conversation ->
    # ChatTurn -> Message) -> Document（各类内按 UUID 排序）。
    admin = _lock_governance_admin(db, request, admin)
    kb = _get_kb_or_404(db, kb_id, lock=True)
    if kb.kind == KNOWLEDGE_BASE_KIND_PROJECT:
        # 项目源码库删除走源软撤销 + 取消活跃任务：不做硬删历史。
        # （快照/引用仍有效，授权读取此后按 410 处理，不删原目录、不自动 GC。）
        # revoke_source_by_kb 内部再按 KB -> Source -> Snapshot 顺序取新鲜行锁。
        project_sources.revoke_source_by_kb(db, kb.id)
        db.commit()
        write_audit(db, request, admin, "project_kb_delete", kb.name)
        return ApiResponse(data=None, message="项目源码库已撤销（源软删除，历史保留）")
    name, collection = kb.name, kb.collection_name
    # 会话 -> 轮次 -> 消息：先锁后改，批量清理诊断时看到的都是锁定的真相。
    conversations = db.scalars(select(Conversation).where(Conversation.kb_id == kb_id)
                              .order_by(Conversation.id).with_for_update()).all()
    conversation_ids = [conv.id for conv in conversations]
    if conversation_ids:
        db.scalars(select(ChatTurn).where(ChatTurn.conversation_id.in_(conversation_ids))
                   .order_by(ChatTurn.id).with_for_update()).all()
        db.scalars(select(Message).where(Message.conversation_id.in_(conversation_ids))
                   .order_by(Message.id).with_for_update()).all()
        chat_turns.clear_conversation_requests(db, conversation_ids)
    # 上传文档最后锁，按 UUID 排序（与最终发布的 Document 锁序一致）。
    docs = db.scalars(select(Document).where(Document.kb_id == kb.id)
                      .order_by(Document.id).with_for_update()).all()
    paths = [Path(doc.stored_path) for doc in docs]
    store = get_vector_store(collection)
    db.execute(delete(store.EmbeddingStore).where(store.EmbeddingStore.collection_id.in_(
        select(store.CollectionStore.uuid).where(store.CollectionStore.name == collection))))
    db.execute(delete(store.CollectionStore).where(store.CollectionStore.name == collection))
    db.delete(kb)
    db.commit()
    for path in paths:
        _unlink(path)
    write_audit(db, request, admin, "kb_delete", name)
    return ApiResponse(data=None, message="已删除")


def _normalize_search(value: str | None) -> str | None:
    """trim 后截断到 200 字符；只有空白视为未搜索。

    超长输入截断而不是 422：搜索框边输入边请求，拒绝会把一次误粘贴变成报错。
    """
    if value is None or not isinstance(value, str):
        return None
    term = value.strip()[:SEARCH_MAX_LENGTH]
    return term or None


def _like_pattern(term: str) -> str:
    """把用户输入当作字面文本：转义 LIKE 的 %、_ 与转义符本身。"""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


@router.get("/{kb_id}/documents", response_model=ApiResponse[PageResult[DocumentRead]], summary="文档列表（q 为字面匹配，trim 后超过 200 字符截断）")
def list_documents(kb_id: uuid.UUID,
                   page: Annotated[int, Query(ge=1)] = 1,
                   page_size: Annotated[int, Query(ge=1, le=100)] = 20,
                   status_filter: Annotated[str | None, Query(alias="status")] = None,
                   q: Annotated[str | None, Query()] = None,
                   db: DBSession = Depends(get_db), user: User = Depends(get_current_user),
                   retrievable_only: Annotated[bool, Query()] = False):
    """列表分页。

    参数一律使用 ``Annotated[...] + 普通 Python 默认值``：HTTP 语义与 FastAPI 完全
    一致，同时保持"直接调用（现有测试）省略参数时拿到真正默认值"的兼容性——
    ``bool = Query(False)`` 在直接调用里会被 ``bool(Query(...))`` 判成 True，
    从而把 status=failed 等查询悄悄过滤成 0 条。
    """
    _get_kb_for_user(db, kb_id, user)
    stmt = select(Document).where(Document.kb_id == kb_id)
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)
    # retrievable_only 先于 count/limit 应用：total 与页长都反映过滤后的真实规模。
    if retrievable_only:
        stmt = stmt.where(*document_lifecycle.retrievable_conditions())
    term = _normalize_search(q)
    if term is not None:
        stmt = stmt.where(Document.filename.ilike(_like_pattern(term), escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    # id 兜底排序：同一时间戳的多行在不同页之间顺序稳定，不会重复或漏行。
    rows = db.scalars(stmt.order_by(Document.created_at.desc(), Document.id.desc())
                      .offset((page - 1) * page_size).limit(page_size)).all()
    return ApiResponse(data=PageResult(total=total, items=[DocumentRead.model_validate(row) for row in rows]))


@router.get("/{kb_id}/documents/{doc_id}", response_model=ApiResponse[DocumentRead], summary="文档状态（不受分页和筛选影响）")
def get_document(kb_id: uuid.UUID, doc_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    _get_kb_for_user(db, kb_id, user)
    return ApiResponse(data=_get_doc(db, kb_id, doc_id))


@router.patch("/{kb_id}/documents/{doc_id}/governance", response_model=ApiResponse[DocumentRead], summary="更新文档治理元数据（管理员或维护者）")
def update_document_governance(kb_id: uuid.UUID, doc_id: uuid.UUID, payload: DocumentGovernanceUpdate,
                               request: Request, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version, actor = _request_identity(request, user)
    _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
    doc = document_governance.update_governance(db, kb, doc_id, payload)
    changes = sorted(set(payload.model_fields_set) - {"expected_revision"})
    write_audit(db, request, actor, "document_governance_update", str(doc.id),
                document_governance.audit_detail(doc, changes))
    return ApiResponse(data=doc)


@router.post("/{kb_id}/documents/{doc_id}/supersede", response_model=ApiResponse[DocumentRead], summary="替代文档（管理员或维护者）")
def supersede_document(kb_id: uuid.UUID, doc_id: uuid.UUID, payload: DocumentSupersedeRequest,
                       request: Request, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version, actor = _request_identity(request, user)
    _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
    doc = document_governance.supersede_document(
        db, kb, doc_id, payload.replacement_document_id, payload.expected_revision,
        comparison_fingerprint=payload.comparison_fingerprint,
    )
    write_audit(db, request, actor, "document_supersede", str(doc.id),
                document_governance.audit_detail(doc, ["superseded_at", "superseded_by_id"]))
    return ApiResponse(data=doc)


@router.get("/{kb_id}/documents/{doc_id}/comparison",
            response_model=ApiResponse[DocumentComparisonRead],
            summary="资料更新差异预览（管理员或维护者，只读）")
def compare_document(kb_id: uuid.UUID, doc_id: uuid.UUID, response: Response,
                     replacement_document_id: Annotated[uuid.UUID, Query()],
                     db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    """只读比较两份文档已入库的文本。

    不写审计、不写会话、不生成答案、不记录正文；响应禁止缓存。
    不可比较时仍返回 200，由 ``comparable/reason`` 表达原因。
    维护级预览：admin 或该上传库显式 maintainer；项目库仍 400。
    """
    _preview_gate(db, kb_id, user)
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=document_comparison.build_comparison(
        db, kb_id, doc_id, replacement_document_id))


@router.post("/{kb_id}/documents/{doc_id}/reenable", response_model=ApiResponse[DocumentRead], summary="显式重新启用文档（管理员或维护者）")
def reenable_document(kb_id: uuid.UUID, doc_id: uuid.UUID, payload: DocumentReenableRequest,
                      request: Request, db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version, actor = _request_identity(request, user)
    _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
    doc = document_governance.reenable_document(db, kb, doc_id, payload.expected_revision)
    write_audit(db, request, actor, "document_reenable", str(doc.id),
                document_governance.audit_detail(doc, ["superseded_at", "superseded_by_id", "enabled"]))
    return ApiResponse(data=doc)


@router.get("/{kb_id}/documents/{doc_id}/source", response_model=ApiResponse[SourceRead], summary="读取当前有效的引用原文")
def get_source(kb_id: uuid.UUID, doc_id: uuid.UUID, chunk_index: int = Query(ge=0),
               index_version: int = Query(0, ge=0), chunk_start: int | None = Query(None, ge=0),
               chunk_end: int | None = Query(None, ge=0), db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    _get_kb_for_user(db, kb_id, user)
    return ApiResponse(data=source_documents.read_source(db, kb_id, doc_id, chunk_index, index_version, chunk_start, chunk_end))


@router.get("/{kb_id}/documents/{doc_id}/pdf", summary="带鉴权的当前 PDF 原文件预览")
def get_source_pdf(kb_id: uuid.UUID, doc_id: uuid.UUID, index_version: int = Query(ge=0),
                   db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    _get_kb_for_user(db, kb_id, user)
    doc = source_documents.ready_document(db, kb_id, doc_id, index_version)
    path = source_documents.pdf_file_path(doc)
    return FileResponse(path, media_type="application/pdf", filename=doc.filename, content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/{kb_id}/documents/{doc_id}/quality", response_model=ApiResponse[DocumentQualityRead], summary="入库质量预览（管理员或维护者）")
def get_document_quality(kb_id: uuid.UUID, doc_id: uuid.UUID,
                         db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    _preview_gate(db, kb_id, user)
    return ApiResponse(data=document_quality.build_quality(db, _get_doc(db, kb_id, doc_id)))


@router.get("/{kb_id}/documents/{doc_id}/chunks", response_model=ApiResponse[DocumentChunkPage], summary="分块分页预览（管理员或维护者，正文最多 1200 字符）")
def get_document_chunks(kb_id: uuid.UUID, doc_id: uuid.UUID,
                        offset: Annotated[int, Query(ge=0)] = 0,
                        limit: Annotated[int, Query(ge=1, le=50)] = 20,
                        db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    _preview_gate(db, kb_id, user)
    return ApiResponse(data=document_quality.chunk_page(db, _get_doc(db, kb_id, doc_id), offset, limit))


@router.post("/{kb_id}/retrieval-diagnostics", response_model=ApiResponse[RetrievalDiagnosticsRead], summary="检索诊断（管理员，不生成答案）")
def run_retrieval_diagnostics(kb_id: uuid.UUID, payload: RetrievalDiagnosticsRequest,
                              db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    kb = _get_kb_or_404(db, kb_id)
    _reject_project_kb(kb)
    return ApiResponse(data=retrieval_diagnostics.run(db, kb, payload.question, payload.document_ids, user=admin))


@router.post("/{kb_id}/documents", response_model=ApiResponse[DocumentUploadRead], summary="上传文档（小文件单请求，后台入库）")
def upload_document(kb_id: uuid.UUID, request: Request, file: UploadFile = File(...), db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    """小文件上传：慢写盘不持 User/KB 根锁；写盘前后各做一次新鲜维护权校验。

    初始预检只读（存在/读取/类型/维护权），避免未授权请求落盘；慢写完成后、
    去重返回或 INSERT 之前，按 User -> KB 根序重新加锁并复核当前维护权与
    认证期原 token。唯一约束冲突 rollback 释放根锁后，必须再次根门禁才允许
    复用 canonical。
    """
    user_id, token_version, actor = _request_identity(request, user)
    # ---- 初始门禁（只读预检；不持根锁）----
    kb = _get_kb_or_404(db, kb_id)
    if not kb_access.can_read_kb(db, kb, user):
        raise HTTPException(status_code=404, detail="知识库不存在")
    _reject_project_kb(kb)
    if not kb_access.can_manage_documents(db, kb, user):
        raise HTTPException(status_code=403, detail=_MAINTENANCE_DENIED)
    db.rollback()  # 预检结束，磁盘写入期间不持任何行锁

    ext = _check_extension(file.filename or "")
    doc_id = uuid.uuid4()
    stored_path = upload_service.final_path(kb_id, doc_id, ext)
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    digest = hashlib.sha256()
    # 慢写阶段不持有知识库/用户行锁：只有下面短事务才与并发删除/撤权竞争。
    try:
        with stored_path.open("xb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(status_code=413, detail=f"文件超过 {settings.MAX_UPLOAD_MB}MB 限制")
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        if not size:
            raise HTTPException(status_code=400, detail="文件为空")
        content_sha256 = digest.hexdigest()
        # ---- 写盘后的权威门禁：解锁的当前维护权 + 原 token ----
        _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
        canonical = document_identity.canonical_document(db, kb_id, content_sha256)
        if canonical is not None:
            # 治理契约：inactive canonical 显式拒绝（409），绝不复活或假装可检索。
            document_governance.ensure_active_dedup_canonical(canonical)
            # 复用同库已有内容：返回原文档 ID/状态，不新建、不改名、不改旧引用。
            # 本次写入的文件不是 canonical，必须由本请求清理。
            _unlink(stored_path)
            write_audit(db, request, actor, "document_upload", canonical.filename,
                        {"kb": kb.name, "deduplicated": True})
            return ApiResponse(data=DocumentUploadRead.model_validate(canonical).model_copy(update={"deduplicated": True}))
        doc = Document(id=doc_id, kb_id=kb.id, filename=file.filename or stored_path.name,
                       file_ext=ext, file_size=size, stored_path=str(stored_path),
                       content_sha256=content_sha256,
                       status=DocumentStatus.PENDING.value, uploaded_by=user_id)
        db.add(doc)
        try:
            db.commit()
        except IntegrityError:
            # 并发上传同一内容：唯一索引只允许一行 canonical。rollback 已释放根锁，
            # 必须重新按 User -> KB 门禁后才能查 canonical/复用；绝不先查后授权。
            db.rollback()
            _unlink(stored_path)
            _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
            canonical = document_identity.canonical_document(db, kb_id, content_sha256)
            if canonical is None:
                raise HTTPException(status_code=409, detail="文档内容冲突，请重试")
            document_governance.ensure_active_dedup_canonical(canonical)
            write_audit(db, request, actor, "document_upload", canonical.filename,
                        {"kb": kb.name, "deduplicated": True})
            return ApiResponse(data=DocumentUploadRead.model_validate(canonical).model_copy(update={"deduplicated": True}))
    except Exception:
        db.rollback()
        _unlink(stored_path)
        raise
    write_audit(db, request, actor, "document_upload", doc.filename, {"kb": kb.name})
    return ApiResponse(data=DocumentUploadRead.model_validate(doc))


@router.post("/{kb_id}/documents/{doc_id}/reindex", response_model=ApiResponse[DocumentRead], summary="重新索引文档（管理员或维护者）")
def reindex_document(kb_id: uuid.UUID, doc_id: uuid.UUID, request: Request,
                    db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version, _ = _request_identity(request, user)
    _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
    doc = _get_doc(db, kb_id, doc_id, lock=True)
    if doc.status in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value}:
        raise HTTPException(status_code=400, detail="文档正在处理中，请先取消入库")
    reset_document_for_reindex(db, doc)
    return ApiResponse(data=doc, message="已加入处理队列")


@router.post("/{kb_id}/documents/{doc_id}/cancel", response_model=ApiResponse[DocumentRead], summary="取消入库（管理员或维护者）")
def cancel_document(kb_id: uuid.UUID, doc_id: uuid.UUID, request: Request,
                    db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version, _ = _request_identity(request, user)
    _maintenance_gate(db, kb_id, user_id, token_version)
    doc = _get_doc(db, kb_id, doc_id, lock=True)
    if doc.status not in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value}:
        raise HTTPException(status_code=400, detail="当前状态无法取消")
    doc.cancel_requested = True
    db.commit()
    return ApiResponse(data=doc, message="取消请求已提交")


@router.delete("/{kb_id}/documents/{doc_id}", response_model=ApiResponse[None], summary="删除文档（管理员或维护者）")
def delete_document(kb_id: uuid.UUID, doc_id: uuid.UUID, request: Request,
                    db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    # 与治理串行化：删除替代文档的自引用 FK SET NULL 还会锁住原文档。
    user_id, token_version, actor = _request_identity(request, user)
    _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
    doc = _get_doc(db, kb_id, doc_id, lock=True)
    filename, path = doc.filename, Path(doc.stored_path)
    delete_document_vectors(kb.collection_name, str(doc.id), db=db)
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    db.delete(doc)
    db.commit()
    _unlink(path)
    write_audit(db, request, actor, "document_delete", filename, {"kb": kb.name})
    return ApiResponse(data=None, message="已删除")


def _session_out(session: UploadSession) -> UploadSessionRead:
    received = list(range(session.total_chunks)) if session.status == UploadStatus.COMPLETED.value else upload_service.received_indices(session)
    return UploadSessionRead(upload_id=session.id, filename=session.filename, file_size=session.file_size,
                             chunk_size=session.chunk_size, total_chunks=session.total_chunks, received=received,
                             received_bytes=sum(upload_service.chunk_length(session, i) for i in received),
                             status=session.status, document_id=session.document_id)


def _get_session_or_404(db: DBSession, kb_id: uuid.UUID, session_id: uuid.UUID, user_id: uuid.UUID, *, lock=False) -> UploadSession:
    session = upload_service.get_session(db, kb_id, session_id, user_id=user_id, lock=lock)
    if session is None:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    return session


@router.post("/{kb_id}/upload-sessions", response_model=ApiResponse[UploadSessionRead], summary="创建或安全恢复分片上传（管理员或维护者）")
def create_upload_session(kb_id: uuid.UUID, payload: UploadSessionCreate, request: Request,
                          db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version, _ = _request_identity(request, user)
    _maintenance_gate(db, kb_id, user_id, token_version)
    ext = _check_extension(payload.filename)
    if payload.file_size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.MAX_UPLOAD_MB}MB 限制")

    def reauthorize_conflict():
        # create_or_resume 的唯一冲突 rollback 已释放根锁，复用前必须重新根门禁。
        _maintenance_gate(db, kb_id, user_id, token_version)

    try:
        session = upload_service.create_or_resume(
            db, kb_id, payload.filename, payload.file_size, ext, user_id,
            payload.content_sha256, authorize_after_conflict=reauthorize_conflict,
        )
    except upload_service.UploadConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=_session_out(session))


@router.get("/{kb_id}/upload-sessions/{session_id}", response_model=ApiResponse[UploadSessionRead], summary="查询自己的上传进度")
def get_upload_session(kb_id: uuid.UUID, session_id: uuid.UUID,
                       db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    # 状态读取要求当前维护能力，并保持会话仅创建者可读。
    _preview_gate(db, kb_id, user)
    return ApiResponse(data=_session_out(_get_session_or_404(db, kb_id, session_id, user.id)))


def _chunk_metadata(kb_id, session_id, user_id, index, auth_token_version) -> int:
    """请求体读取前的小门禁：独立 Session，root locks 在返回前释放。"""
    with SessionLocal() as db:
        _maintenance_gate(db, kb_id, user_id, auth_token_version)
        session = _get_session_or_404(db, kb_id, session_id, user_id)
        try:
            if session.status != UploadStatus.UPLOADING.value:
                raise upload_service.UploadConflict("上传会话已结束")
            return upload_service.chunk_length(session, index)
        except upload_service.UploadConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _write_chunk(kb_id, session_id, user_id, index, data, auth_token_version) -> UploadChunkRead:
    """分三阶段写分片，任何阶段都不反序取锁，也不跨慢写持根锁。

    1. 短事务 User -> KB 复核维护权/原认证 token，确认会话可写后立即释放；
    2. 仅持 UploadSession 行锁完成磁盘写/读校验（save_chunk 的同片契约要求
       内容不可变），写盘后立刻 rollback 释放会话锁；
    3. 重新按 User -> KB -> UploadSession 获取新鲜授权，确认会话仍可写后更新
       received/updated_at 并提交。

    最终短事务不再读整片：同片异内容已由阶段二 save_chunk 拒绝，abort/
    complete/TTL 等状态变化由最终会话检查拒绝，避免把磁盘读放大进根锁。
    若期间撤权/token 轮换，则 401/403 拒绝且不提交元数据，保留该会话合法的
    临时分片供新 token 续传。
    """
    with SessionLocal() as db:
        _maintenance_gate(db, kb_id, user_id, auth_token_version)
        session = _get_session_or_404(db, kb_id, session_id, user_id)
        if session.status != UploadStatus.UPLOADING.value:
            raise HTTPException(status_code=409, detail="上传会话已结束")
        db.rollback()  # 释放根锁，磁盘写入不跨 User/KB 锁
        session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
        try:
            created = upload_service.save_chunk(session, index, data)
        except upload_service.UploadConflict as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # 常量规模元数据校验留在仅持会话锁的阶段，不进入根锁临界区。
        part = upload_service.tmp_dir(session.id) / f"{index}.part"
        try:
            persisted = part.stat().st_size == len(data)
        except OSError:
            persisted = False
        if not persisted:
            db.rollback()
            raise HTTPException(status_code=409, detail="分片写入失败，请查询进度后重试")
        db.rollback()  # 释放会话锁，慢写之后重新做最终新鲜复核
        _maintenance_gate(db, kb_id, user_id, auth_token_version)
        session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
        if session.status != UploadStatus.UPLOADING.value:
            db.rollback()
            raise HTTPException(status_code=409, detail="上传会话已结束")
        session.received = upload_service.received_indices(session)
        # 即使重放的是同一片也刷新活动时间，避免仍在续传的会话被过期清理。
        from datetime import datetime, timezone
        session.updated_at = datetime.now(timezone.utc)
        db.commit()
        return UploadChunkRead(index=index, created=created, received=len(session.received))


@router.post("/{kb_id}/upload-sessions/{session_id}/chunks/{index}", response_model=ApiResponse[UploadChunkRead], summary="上传分片（有界读取，内容幂等）")
async def upload_chunk(kb_id: uuid.UUID, session_id: uuid.UUID, index: int, request: Request, user: User = Depends(get_current_user)):
    user_id, token_version, _ = _request_identity(request, user)
    expected = await run_in_threadpool(_chunk_metadata, kb_id, session_id, user_id, index, token_version)
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > expected:
            raise HTTPException(status_code=413, detail="分片超过期望大小")
        data.extend(block)
    if len(data) != expected:
        raise HTTPException(status_code=400, detail="分片大小不符")
    # 请求体读取可能很慢：写盘前用请求开始时的原 token 再做一次新鲜根门禁。
    receipt = await run_in_threadpool(_write_chunk, kb_id, session_id, user_id, index, bytes(data), token_version)
    return ApiResponse(data=receipt)


def _cleanup_owned_assembly(db: DBSession, assembled, session_id: uuid.UUID | None = None,
                            *, creator_id: uuid.UUID | None = None,
                            discard_own: bool = False) -> None:
    """清理本次慢写产出的文件，且绝不误删被合法完成收编的 canonical。

    清理必须与**同一上传会话**的采用串行：仅"查询当前没有引用"不足以防
    TOCTOU（另一请求可在查询与 unlink 之间提交 canonical）。这里按全局锁序的
    最后一级取 ``UploadSession FOR UPDATE``（绝不在会话锁之后反取 User/KB），
    把"查引用 + 删除"放进同一临界区。采用方（``reuse`` 与最终 INSERT）整段也
    持有同一个会话行锁，因此：
    - 采用先提交 -> 这里能看到 Document 引用，必须保留文件；
    - 采用未提交 -> 被本锁挡住，不可能在查询与 unlink 之间抢先收编。

    ``discard_own=False``（根门禁失败）：会话仍是 uploading 且
    ``created_by == creator_id`` 时保留合并文件与分片——本次请求只是被拒绝，
    所有者可用新 token 合法完成同一认领路径；孤儿由 TTL/主动放弃负责。

    ``discard_own=True``（调用方已持会话锁并确认本次内容不可采用：inactive
    canonical、唯一冲突且无 canonical）：本次合并文件与本会话分片只属于本请求，
    一并清理（与既有 409 语义一致）。

    所有权：只动本请求创建者名下、或已因创建者删除失去归属（``created_by``
    为 NULL）、或已随知识库删除而不存在的会话目录；绝按外部传入的 session_id
    越权删除仍在他人名下的目录。
    """
    try:
        session = None
        if session_id is not None:
            session = db.scalar(
                select(UploadSession).where(UploadSession.id == session_id)
                .with_for_update().execution_options(populate_existing=True))
        owned = (session is None
                 or session.created_by is None
                 or (creator_id is not None and session.created_by == creator_id))
        recoverable = (not discard_own
                       and session is not None
                       and session.status == UploadStatus.UPLOADING.value
                       and session.created_by is not None
                       and session.created_by == creator_id)
        if owned and not recoverable:
            referenced = db.scalar(
                select(Document.id).where(Document.stored_path == str(assembled.path)))
            if referenced is None:
                _unlink(assembled.path)
            if session_id is not None:
                shutil.rmtree(upload_service.tmp_dir(session_id), ignore_errors=True)
    finally:
        db.commit()  # 释放会话行锁（只清理磁盘文件，不改业务行）


def _cleanup_abandoned_upload_tmp(db: DBSession, kb_id: uuid.UUID,
                                  session_id: uuid.UUID) -> None:
    """根门禁失败后，仅清理"已不可能被任何用户合法完成"的上传临时文件。

    安全边界（不按调用方给的 session_id 直删别人目录）：

    - 只接受本 ``kb_id`` 下的会话（跨库 session_id 一律不动）；
    - 会话行仍在且 ``created_by`` 非空 -> 属于某个仍可登录的账号（可能正是
      本用户轮换后的新令牌），保留分片与合并文件，允许其继续续传/完成；
    - ``created_by`` 为 NULL（创建者已被删除，FK SET NULL）或会话行已不存在
      -> 任何用户都不可能再通过 ``get_session(created_by=...)`` 完成它，
      才清理其 tmp 分片与未被引用的合并文件（与 TTL 清理同一语义）。

    锁序：调用方必须已释放 User/KB 根锁；这里只取锁序最后一级的会话行锁，
    绝不在会话锁之后回取 User/KB。
    """
    try:
        session = db.scalar(
            select(UploadSession)
            .where(UploadSession.id == session_id, UploadSession.kb_id == kb_id)
            .with_for_update().execution_options(populate_existing=True))
        if session is None or session.created_by is not None:
            # 行不存在（可能属于别的库/别的会话）或仍是活账号的上传：一律不清理。
            return
        upload_service.remove_unreferenced_final(db, session)
    finally:
        db.rollback()
    shutil.rmtree(upload_service.tmp_dir(session_id), ignore_errors=True)


def _completed_response(db: DBSession, session: UploadSession) -> ApiResponse:
    doc = db.get(Document, session.document_id)
    if doc is None:
        raise HTTPException(status_code=410, detail="该上传对应的文档已删除，请重新上传")
    # 已完成的会话重放同样不得把治理性不可用的 canonical 当成可用结果返回。
    document_governance.ensure_active_governance_document(doc)
    return ApiResponse(data=UploadCompleteRead(upload_id=session.id, document=doc,
                                               deduplicated=session.deduplicated), message="上传已完成")


def _replay_completed_upload(db: DBSession, kb_id: uuid.UUID, session_id: uuid.UUID,
                             user_id: uuid.UUID, auth_token_version: int) -> ApiResponse:
    """已完成会话的幂等重放：不得持会话锁反向取 User，也不得沿用旧权限直返。

    先释放会话事务，再按 User -> KB -> 会话根序新鲜复核 active/token/维护权，
    最后物化响应并释放锁。
    """
    try:
        _maintenance_gate(db, kb_id, user_id, auth_token_version)
        session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
        if session.status != UploadStatus.COMPLETED.value:
            raise HTTPException(status_code=409, detail="上传会话状态已变化，请重试")
        response = _completed_response(db, session)
    except HTTPException:
        db.rollback()
        raise
    db.commit()  # 响应已物化，释放根锁与会话锁
    return response


@router.post("/{kb_id}/upload-sessions/{session_id}/complete", response_model=ApiResponse[UploadCompleteRead], summary="校验合并并原子创建文档（幂等）")
def complete_upload(kb_id: uuid.UUID, session_id: uuid.UUID, request: Request,
                    db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    # 提交/回滚可能使认证 ORM 对象过期：只使用请求开始时的不可变身份与令牌快照。
    user_id, token_version, actor = _request_identity(request, user)
    # 阶段一（短事务）：User -> KB 根门禁复核维护权与认证期原 token，立即释放。
    try:
        _maintenance_gate(db, kb_id, user_id, token_version)
    except HTTPException:
        # 等待根锁期间用户可能已被删除（会话 created_by 被 FK 置 NULL）：该会话
        # 已不可能被任何人合法完成/续传，本请求负责清理它自己的 tmp 分片。
        # 先释放根锁，再按锁序最后一级取会话行，绝不在 User/KB 锁内碰会话。
        db.rollback()
        _cleanup_abandoned_upload_tmp(db, kb_id, session_id)
        raise
    db.commit()

    # 阶段二（慢写，只持有会话行锁）：合并、fsync、服务端摘要。
    # 这段可能耗时数十秒，不能把 User/KB 行锁跨在磁盘操作上。
    try:
        session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
    except HTTPException:
        db.rollback()
        raise
    if session.status == UploadStatus.COMPLETED.value:
        db.commit()  # 先释放会话锁：不得持锁反向取 User，也不能沿用旧权限直返
        return _replay_completed_upload(db, kb_id, session_id, user_id, token_version)
    if session.status == UploadStatus.ABORTED.value:
        db.rollback()
        raise HTTPException(status_code=409, detail="上传会话已终止")
    try:
        assembled = upload_service.assemble(db, session)
    except upload_service.UploadConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="上传分片缺失，请查询进度后重试") from exc
    except OSError as exc:
        db.rollback()
        logger.exception("分片合并失败: %s", session_id)
        raise HTTPException(status_code=507, detail="文件合并失败，请检查磁盘空间后重试") from exc
    # 合并结果落盘已持久；提交只释放会话锁，会话仍是 uploading，进程中断后重试会
    # 复用同一认领路径，不会产生第二个文件。
    db.commit()

    # 阶段三（短事务）：固定锁顺序 User -> 知识库 -> 上传会话，与用户/知识库删除一致。
    # 慢写期间删除用户/知识库或终止会话都可能发生：不能依赖 TTL 找回合并文件，
    # 必须在这里清理本次合并出的文件——但前提是没有文档引用它（并发完成可能已收编）。
    try:
        _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
        session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
    except HTTPException:
        db.rollback()
        _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)
        raise
    if session.status == UploadStatus.COMPLETED.value:
        try:
            response = _completed_response(db, session)
        except HTTPException:
            db.rollback()
            _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)
            raise
        db.commit()
        _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)
        return response
    if session.status == UploadStatus.ABORTED.value:
        db.rollback()
        _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)
        raise HTTPException(status_code=409, detail="上传会话已终止")

    def reuse(canonical: Document) -> ApiResponse:
        session.document_id = canonical.id
        session.deduplicated = True
        session.status = UploadStatus.COMPLETED.value
        db.commit()
        shutil.rmtree(upload_service.tmp_dir(session.id), ignore_errors=True)
        _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)  # 本次合并出的文件不是 canonical，只清理自己
        write_audit(db, request, actor, "document_upload", canonical.filename,
                    {"kb": kb.name, "mode": "chunked", "deduplicated": True})
        return ApiResponse(data=UploadCompleteRead(upload_id=session.id, document=canonical, deduplicated=True),
                           message="上传完成：已复用知识库中的相同内容")

    canonical = document_identity.canonical_document(db, kb_id, assembled.digest)
    if canonical is not None:
        # 治理契约：inactive canonical 显式拒绝（409），绝不复活或假装可检索。
        # 此时本次合并出的文件不是 canonical、也不会被任何文档引用：必须由本请求
        # 清理，否则 409 路径会留下孤儿文件。会话仍 uploading，但本次内容明确
        # 不可采用（discard_own=True），分片一并清理；用户重传即可重建。
        try:
            document_governance.ensure_active_dedup_canonical(canonical)
        except HTTPException:
            db.rollback()
            _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id,
                                    discard_own=True)
            raise
        # 复用同库 canonical：返回原文档 ID/状态，不改名、不改旧消息引用、不跨库。
        return reuse(canonical)

    doc = Document(id=assembled.claimed_doc_id, kb_id=kb_id, filename=session.filename,
                   file_ext=session.file_ext, file_size=session.file_size,
                   stored_path=str(assembled.path), content_sha256=assembled.digest,
                   status=DocumentStatus.PENDING.value, uploaded_by=user_id)
    db.add(doc)
    session.status = UploadStatus.COMPLETED.value
    session.deduplicated = False
    try:
        db.commit()  # 会话完成标记与文档记录一起提交。
    except IntegrityError:
        # 与小文件入口并发上传同一内容：另一行先拿到唯一索引。rollback 已释放
        # 根锁，必须重新按 User -> KB -> 会话根门禁后才允许查 canonical/复用。
        db.rollback()
        try:
            _, kb = _maintenance_gate(db, kb_id, user_id, token_version)
            session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
        except HTTPException:
            _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)
            raise
        if session.status == UploadStatus.COMPLETED.value:
            try:
                response = _completed_response(db, session)
            except HTTPException:
                db.rollback()
                _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)
                raise
            db.commit()
            _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id)
            return response
        canonical = document_identity.canonical_document(db, kb_id, assembled.digest)
        if canonical is None:
            db.rollback()
            _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id,
                                    discard_own=True)
            raise HTTPException(status_code=409, detail="文档内容冲突，请重试")
        try:
            document_governance.ensure_active_dedup_canonical(canonical)
        except HTTPException:
            db.rollback()
            _cleanup_owned_assembly(db, assembled, session_id, creator_id=user_id,
                                    discard_own=True)
            raise
        return reuse(canonical)
    shutil.rmtree(upload_service.tmp_dir(session.id), ignore_errors=True)
    write_audit(db, request, actor, "document_upload", doc.filename, {"kb": kb.name, "mode": "chunked"})
    return ApiResponse(data=UploadCompleteRead(upload_id=session.id, document=doc, deduplicated=False),
                       message="上传完成，已加入入库队列")


@router.delete("/{kb_id}/upload-sessions/{session_id}", response_model=ApiResponse[None], summary="放弃自己的分片上传")
def abort_upload(kb_id: uuid.UUID, session_id: uuid.UUID, request: Request,
                 db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    user_id, token_version, _ = _request_identity(request, user)
    _maintenance_gate(db, kb_id, user_id, token_version)
    session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
    upload_service.abort_session(db, session)
    return ApiResponse(data=None, message="已放弃")
