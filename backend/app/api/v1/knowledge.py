"""知识库、文档与内容校验的分片上传接口。"""
import hashlib
import logging
import os
import shutil
import uuid
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
    document_governance, document_quality, project_sources, retrieval_diagnostics,
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


def _get_kb_or_404(db: DBSession, kb_id: uuid.UUID, *, lock: bool = False) -> KnowledgeBase:
    stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    kb = db.scalar(stmt)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


def _get_kb_for_user(db: DBSession, kb_id: uuid.UUID, user: User) -> KnowledgeBase:
    kb = _get_kb_or_404(db, kb_id)
    if not user.is_admin and not kb.is_public and kb.owner_id != user.id:
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


def _kb_out(db: DBSession, kb: KnowledgeBase, count: int | None = None) -> KnowledgeBaseRead:
    data = KnowledgeBaseRead.model_validate(kb)
    data.document_count = count if count is not None else (db.scalar(
        select(func.count()).select_from(Document).where(Document.kb_id == kb.id)) or 0)
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
    stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
    if not user.is_admin:
        stmt = stmt.where(KnowledgeBase.is_public.is_(True) | (KnowledgeBase.owner_id == user.id))
    kbs = db.scalars(stmt).all()
    counts = dict(db.execute(select(Document.kb_id, func.count()).where(
        Document.kb_id.in_([kb.id for kb in kbs])).group_by(Document.kb_id)).all()) if kbs else {}
    return ApiResponse(data=[_kb_out(db, kb, counts.get(kb.id, 0)) for kb in kbs])


@router.post("", response_model=ApiResponse[KnowledgeBaseRead], status_code=201, summary="创建知识库（管理员）")
def create_kb(payload: KnowledgeBaseCreate, request: Request, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    if db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.name == payload.name)):
        raise HTTPException(status_code=400, detail="知识库名称已存在")
    kb = KnowledgeBase(name=payload.name, description=payload.description,
                       collection_name=f"kb_{uuid.uuid4().hex}", owner_id=admin.id, is_public=payload.is_public)
    db.add(kb)
    db.commit()
    write_audit(db, request, admin, "kb_create", kb.name)
    return ApiResponse(data=_kb_out(db, kb, 0))


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
    return ApiResponse(data=_kb_out(db, kb))


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


@router.patch("/{kb_id}/documents/{doc_id}/governance", response_model=ApiResponse[DocumentRead], summary="更新文档治理元数据（管理员）")
def update_document_governance(kb_id: uuid.UUID, doc_id: uuid.UUID, payload: DocumentGovernanceUpdate,
                               request: Request, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _lock_governance_admin(db, request, admin)
    kb = document_governance.require_governance_kb(db, kb_id)
    doc = document_governance.update_governance(db, kb, doc_id, payload)
    changes = sorted(set(payload.model_fields_set) - {"expected_revision"})
    write_audit(db, request, admin, "document_governance_update", str(doc.id),
                document_governance.audit_detail(doc, changes))
    return ApiResponse(data=doc)


@router.post("/{kb_id}/documents/{doc_id}/supersede", response_model=ApiResponse[DocumentRead], summary="替代文档（管理员）")
def supersede_document(kb_id: uuid.UUID, doc_id: uuid.UUID, payload: DocumentSupersedeRequest,
                       request: Request, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _lock_governance_admin(db, request, admin)
    kb = document_governance.require_governance_kb(db, kb_id)
    doc = document_governance.supersede_document(
        db, kb, doc_id, payload.replacement_document_id, payload.expected_revision,
        comparison_fingerprint=payload.comparison_fingerprint,
    )
    write_audit(db, request, admin, "document_supersede", str(doc.id),
                document_governance.audit_detail(doc, ["superseded_at", "superseded_by_id"]))
    return ApiResponse(data=doc)


@router.get("/{kb_id}/documents/{doc_id}/comparison",
            response_model=ApiResponse[DocumentComparisonRead],
            summary="资料更新差异预览（管理员，只读）")
def compare_document(kb_id: uuid.UUID, doc_id: uuid.UUID, response: Response,
                     replacement_document_id: Annotated[uuid.UUID, Query()],
                     db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    """只读比较两份文档已入库的文本。

    不写审计、不写会话、不生成答案、不记录正文；响应禁止缓存。
    不可比较时仍返回 200，由 ``comparable/reason`` 表达原因。
    """
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(data=document_comparison.build_comparison(
        db, kb_id, doc_id, replacement_document_id))


@router.post("/{kb_id}/documents/{doc_id}/reenable", response_model=ApiResponse[DocumentRead], summary="显式重新启用文档（管理员）")
def reenable_document(kb_id: uuid.UUID, doc_id: uuid.UUID, payload: DocumentReenableRequest,
                      request: Request, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _lock_governance_admin(db, request, admin)
    kb = document_governance.require_governance_kb(db, kb_id)
    doc = document_governance.reenable_document(db, kb, doc_id, payload.expected_revision)
    write_audit(db, request, admin, "document_reenable", str(doc.id),
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


@router.get("/{kb_id}/documents/{doc_id}/quality", response_model=ApiResponse[DocumentQualityRead], summary="入库质量预览（管理员）")
def get_document_quality(kb_id: uuid.UUID, doc_id: uuid.UUID,
                         db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    return ApiResponse(data=document_quality.build_quality(db, _get_doc(db, kb_id, doc_id)))


@router.get("/{kb_id}/documents/{doc_id}/chunks", response_model=ApiResponse[DocumentChunkPage], summary="分块分页预览（管理员，正文最多 1200 字符）")
def get_document_chunks(kb_id: uuid.UUID, doc_id: uuid.UUID,
                        offset: Annotated[int, Query(ge=0)] = 0,
                        limit: Annotated[int, Query(ge=1, le=50)] = 20,
                        db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    return ApiResponse(data=document_quality.chunk_page(db, _get_doc(db, kb_id, doc_id), offset, limit))


@router.post("/{kb_id}/retrieval-diagnostics", response_model=ApiResponse[RetrievalDiagnosticsRead], summary="检索诊断（管理员，不生成答案）")
def run_retrieval_diagnostics(kb_id: uuid.UUID, payload: RetrievalDiagnosticsRequest,
                              db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    kb = _get_kb_or_404(db, kb_id)
    _reject_project_kb(kb)
    return ApiResponse(data=retrieval_diagnostics.run(db, kb, payload.question, payload.document_ids, user=admin))


@router.post("/{kb_id}/documents", response_model=ApiResponse[DocumentUploadRead], summary="上传文档（小文件单请求，后台入库）")
def upload_document(kb_id: uuid.UUID, request: Request, file: UploadFile = File(...), db: DBSession = Depends(get_db), user: User = Depends(require_admin)):
    kb = _get_kb_or_404(db, kb_id)
    _reject_project_kb(kb)
    ext = _check_extension(file.filename or "")
    doc_id = uuid.uuid4()
    stored_path = upload_service.final_path(kb_id, doc_id, ext)
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    digest = hashlib.sha256()
    # 慢写阶段不持有知识库行锁：只有下面插入文档那一小段事务才与并发删除竞争。
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
        canonical = document_identity.canonical_document(db, kb_id, content_sha256)
        if canonical is not None:
            # 治理契约：inactive canonical 显式拒绝（409），绝不复活或假装可检索。
            document_governance.ensure_active_dedup_canonical(canonical)
            # 复用同库已有内容：返回原文档 ID/状态，不新建、不改名、不改旧引用。
            # 本次写入的文件不是 canonical，必须由本请求清理。
            _unlink(stored_path)
            write_audit(db, request, user, "document_upload", canonical.filename,
                        {"kb": kb.name, "deduplicated": True})
            return ApiResponse(data=DocumentUploadRead.model_validate(canonical).model_copy(update={"deduplicated": True}))
        doc = Document(id=doc_id, kb_id=kb.id, filename=file.filename or stored_path.name,
                       file_ext=ext, file_size=size, stored_path=str(stored_path),
                       content_sha256=content_sha256,
                       status=DocumentStatus.PENDING.value, uploaded_by=user.id)
        db.add(doc)
        try:
            db.commit()
        except IntegrityError:
            # 并发上传同一内容：唯一索引只允许一行 canonical。回滚后重查并复用胜者，
            # 只清理本次拥有的文件，绝不删除 canonical。
            db.rollback()
            _unlink(stored_path)
            canonical = document_identity.canonical_document(db, kb_id, content_sha256)
            if canonical is None:
                raise HTTPException(status_code=409, detail="文档内容冲突，请重试")
            document_governance.ensure_active_dedup_canonical(canonical)
            write_audit(db, request, user, "document_upload", canonical.filename,
                        {"kb": kb.name, "deduplicated": True})
            return ApiResponse(data=DocumentUploadRead.model_validate(canonical).model_copy(update={"deduplicated": True}))
    except Exception:
        db.rollback()
        _unlink(stored_path)
        raise
    write_audit(db, request, user, "document_upload", doc.filename, {"kb": kb.name})
    return ApiResponse(data=DocumentUploadRead.model_validate(doc))


@router.post("/{kb_id}/documents/{doc_id}/reindex", response_model=ApiResponse[DocumentRead], summary="重新索引文档")
def reindex_document(kb_id: uuid.UUID, doc_id: uuid.UUID, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    doc = _get_doc(db, kb_id, doc_id, lock=True)
    if doc.status in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value}:
        raise HTTPException(status_code=400, detail="文档正在处理中，请先取消入库")
    reset_document_for_reindex(db, doc)
    return ApiResponse(data=doc, message="已加入处理队列")


@router.post("/{kb_id}/documents/{doc_id}/cancel", response_model=ApiResponse[DocumentRead], summary="取消入库")
def cancel_document(kb_id: uuid.UUID, doc_id: uuid.UUID, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    doc = _get_doc(db, kb_id, doc_id, lock=True)
    if doc.status not in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value}:
        raise HTTPException(status_code=400, detail="当前状态无法取消")
    doc.cancel_requested = True
    db.commit()
    return ApiResponse(data=doc, message="取消请求已提交")


@router.delete("/{kb_id}/documents/{doc_id}", response_model=ApiResponse[None], summary="删除文档")
def delete_document(kb_id: uuid.UUID, doc_id: uuid.UUID, request: Request, db: DBSession = Depends(get_db), user: User = Depends(require_admin)):
    # 与治理串行化：删除替代文档的自引用 FK SET NULL 还会锁住原文档。
    kb = _get_kb_or_404(db, kb_id, lock=True)
    _reject_project_kb(kb)
    doc = _get_doc(db, kb_id, doc_id, lock=True)
    filename, path = doc.filename, Path(doc.stored_path)
    delete_document_vectors(kb.collection_name, str(doc.id), db=db)
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    db.delete(doc)
    db.commit()
    _unlink(path)
    write_audit(db, request, user, "document_delete", filename, {"kb": kb.name})
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


@router.post("/{kb_id}/upload-sessions", response_model=ApiResponse[UploadSessionRead], summary="创建或安全恢复分片上传（管理员）")
def create_upload_session(kb_id: uuid.UUID, payload: UploadSessionCreate, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    ext = _check_extension(payload.filename)
    if payload.file_size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"文件超过 {settings.MAX_UPLOAD_MB}MB 限制")
    session = upload_service.create_or_resume(db, kb_id, payload.filename, payload.file_size, ext, admin.id, payload.content_sha256)
    return ApiResponse(data=_session_out(session))


@router.get("/{kb_id}/upload-sessions/{session_id}", response_model=ApiResponse[UploadSessionRead], summary="查询自己的上传进度")
def get_upload_session(kb_id: uuid.UUID, session_id: uuid.UUID, db: DBSession = Depends(get_db), user: User = Depends(require_admin)):
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    return ApiResponse(data=_session_out(_get_session_or_404(db, kb_id, session_id, user.id)))


def _chunk_metadata(kb_id, session_id, user_id, index) -> int:
    with SessionLocal() as db:
        _reject_project_kb(_get_kb_or_404(db, kb_id))
        session = _get_session_or_404(db, kb_id, session_id, user_id)
        try:
            if session.status != UploadStatus.UPLOADING.value:
                raise upload_service.UploadConflict("上传会话已结束")
            return upload_service.chunk_length(session, index)
        except upload_service.UploadConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc


def _write_chunk(kb_id, session_id, user_id, index, data) -> UploadChunkRead:
    with SessionLocal() as db:
        _reject_project_kb(_get_kb_or_404(db, kb_id))
        session = _get_session_or_404(db, kb_id, session_id, user_id, lock=True)
        try:
            created = upload_service.save_chunk(session, index, data)
        except upload_service.UploadConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.received = upload_service.received_indices(session)
        # 即使重放的是同一片也刷新活动时间，避免仍在续传的会话被过期清理。
        from datetime import datetime, timezone
        session.updated_at = datetime.now(timezone.utc)
        db.commit()
        return UploadChunkRead(index=index, created=created, received=len(session.received))


@router.post("/{kb_id}/upload-sessions/{session_id}/chunks/{index}", response_model=ApiResponse[UploadChunkRead], summary="上传分片（有界读取，内容幂等）")
async def upload_chunk(kb_id: uuid.UUID, session_id: uuid.UUID, index: int, request: Request, user: User = Depends(require_admin)):
    expected = await run_in_threadpool(_chunk_metadata, kb_id, session_id, user.id, index)
    data = bytearray()
    async for block in request.stream():
        if len(data) + len(block) > expected:
            raise HTTPException(status_code=413, detail="分片超过期望大小")
        data.extend(block)
    if len(data) != expected:
        raise HTTPException(status_code=400, detail="分片大小不符")
    receipt = await run_in_threadpool(_write_chunk, kb_id, session_id, user.id, index, bytes(data))
    return ApiResponse(data=receipt)


def _cleanup_owned_assembly(db: DBSession, assembled, session_id: uuid.UUID | None = None) -> None:
    """删除本次慢写产出的文件，但绝不删除被任何文档引用的 canonical。

    删除知识库/终止会话可能发生在阶段二提交之后：此时会话行已不在，
    TTL 清理不会知道这个文件，必须由当前请求负责；而同内容并发完成的另一请求
    可能已把该路径收编为 canonical——所以先查引用再删。
    """
    referenced = db.scalar(
        select(Document.id).where(Document.stored_path == str(assembled.path))
    )
    if referenced is None:
        _unlink(assembled.path)
    if session_id is not None:
        shutil.rmtree(upload_service.tmp_dir(session_id), ignore_errors=True)


def _completed_response(db: DBSession, session: UploadSession) -> ApiResponse:
    doc = db.get(Document, session.document_id)
    if doc is None:
        raise HTTPException(status_code=410, detail="该上传对应的文档已删除，请重新上传")
    # 已完成的会话重放同样不得把治理性不可用的 canonical 当成可用结果返回。
    document_governance.ensure_active_governance_document(doc)
    return ApiResponse(data=UploadCompleteRead(upload_id=session.id, document=doc,
                                               deduplicated=session.deduplicated), message="上传已完成")


@router.post("/{kb_id}/upload-sessions/{session_id}/complete", response_model=ApiResponse[UploadCompleteRead], summary="校验合并并原子创建文档（幂等）")
def complete_upload(kb_id: uuid.UUID, session_id: uuid.UUID, request: Request, db: DBSession = Depends(get_db), user: User = Depends(require_admin)):
    # 提交可能使认证 ORM 对象过期；锁住 KB/会话后不得再靠刷新 User 读取身份。
    uploader_id = user.id
    # 阶段一（短事务）：确认知识库仍存在且非项目库，立刻释放 KB 行锁。
    _reject_project_kb(_get_kb_or_404(db, kb_id, lock=True))
    db.commit()

    # 阶段二（慢写，只持有会话行锁）：合并、fsync、服务端摘要。
    # 这段可能耗时数十秒，不能把 User/KB 行锁跨在磁盘操作上。
    session = _get_session_or_404(db, kb_id, session_id, uploader_id, lock=True)
    if session.status == UploadStatus.COMPLETED.value:
        return _completed_response(db, session)
    if session.status == UploadStatus.ABORTED.value:
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
        # 文档 uploaded_by 的 FK 需要 KEY SHARE，先取以免与用户删除的 FOR UPDATE 成环。
        # SQLAlchemy 只有 read=True + key_share=True 才生成 PostgreSQL FOR KEY SHARE。
        if db.scalar(select(User.id).where(User.id == uploader_id)
                     .with_for_update(read=True, key_share=True)) is None:
            raise HTTPException(status_code=401, detail="用户不存在")
        kb = _get_kb_or_404(db, kb_id, lock=True)
        _reject_project_kb(kb)
        session = _get_session_or_404(db, kb_id, session_id, uploader_id, lock=True)
    except HTTPException:
        db.rollback()
        _cleanup_owned_assembly(db, assembled, session_id)
        raise
    if session.status == UploadStatus.COMPLETED.value:
        return _completed_response(db, session)
    if session.status == UploadStatus.ABORTED.value:
        db.rollback()
        _cleanup_owned_assembly(db, assembled, session_id)
        raise HTTPException(status_code=409, detail="上传会话已终止")

    def reuse(canonical: Document) -> ApiResponse:
        session.document_id = canonical.id
        session.deduplicated = True
        session.status = UploadStatus.COMPLETED.value
        db.commit()
        shutil.rmtree(upload_service.tmp_dir(session.id), ignore_errors=True)
        _cleanup_owned_assembly(db, assembled, session_id)  # 本次合并出的文件不是 canonical，只清理自己
        write_audit(db, request, user, "document_upload", canonical.filename,
                    {"kb": kb.name, "mode": "chunked", "deduplicated": True})
        return ApiResponse(data=UploadCompleteRead(upload_id=session.id, document=canonical, deduplicated=True),
                           message="上传完成：已复用知识库中的相同内容")

    canonical = document_identity.canonical_document(db, kb_id, assembled.digest)
    if canonical is not None:
        # 治理契约：inactive canonical 显式拒绝（409），绝不复活或假装可检索。
        # 此时本次合并出的文件不是 canonical、也不会被任何文档引用：必须由本请求
        # 清理，否则 409 路径会留下孤儿文件。
        try:
            document_governance.ensure_active_dedup_canonical(canonical)
        except HTTPException:
            db.rollback()
            _cleanup_owned_assembly(db, assembled, session_id)
            raise
        # 复用同库 canonical：返回原文档 ID/状态，不改名、不改旧消息引用、不跨库。
        return reuse(canonical)

    doc = Document(id=assembled.claimed_doc_id, kb_id=kb_id, filename=session.filename,
                   file_ext=session.file_ext, file_size=session.file_size,
                   stored_path=str(assembled.path), content_sha256=assembled.digest,
                   status=DocumentStatus.PENDING.value, uploaded_by=uploader_id)
    db.add(doc)
    session.status = UploadStatus.COMPLETED.value
    session.deduplicated = False
    try:
        db.commit()  # 会话完成标记与文档记录一起提交。
    except IntegrityError:
        # 与小文件入口并发上传同一内容：另一行先拿到唯一索引。回滚后复用胜者，
        # 只删除本次合并出的文件，绝不触碰 canonical。
        db.rollback()
        try:
            session = _get_session_or_404(db, kb_id, session_id, uploader_id, lock=True)
        except HTTPException:
            _cleanup_owned_assembly(db, assembled, session_id)
            raise
        canonical = document_identity.canonical_document(db, kb_id, assembled.digest)
        if canonical is None:
            _cleanup_owned_assembly(db, assembled, session_id)
            raise HTTPException(status_code=409, detail="文档内容冲突，请重试")
        try:
            document_governance.ensure_active_dedup_canonical(canonical)
        except HTTPException:
            db.rollback()
            _cleanup_owned_assembly(db, assembled, session_id)
            raise
        return reuse(canonical)
    shutil.rmtree(upload_service.tmp_dir(session.id), ignore_errors=True)
    write_audit(db, request, user, "document_upload", doc.filename, {"kb": kb.name, "mode": "chunked"})
    return ApiResponse(data=UploadCompleteRead(upload_id=session.id, document=doc, deduplicated=False),
                       message="上传完成，已加入入库队列")


@router.delete("/{kb_id}/upload-sessions/{session_id}", response_model=ApiResponse[None], summary="放弃自己的分片上传")
def abort_upload(kb_id: uuid.UUID, session_id: uuid.UUID, db: DBSession = Depends(get_db), admin: User = Depends(require_admin)):
    _reject_project_kb(_get_kb_or_404(db, kb_id))
    session = _get_session_or_404(db, kb_id, session_id, admin.id, lock=True)
    upload_service.abort_session(db, session)
    return ApiResponse(data=None, message="已放弃")
