import logging
import uuid
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user, require_admin, write_audit
from app.core.config import settings
from app.db.session import get_db
from app.models import (
    Document,
    DocumentChunk,
    DocumentStatus,
    KnowledgeBase,
    UploadSession,
    UploadStatus,
    User,
)
from app.schemas.common import ApiResponse, PageResult
from app.schemas.knowledge import (
    DocumentRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
    KnowledgeBaseUpdate,
    UploadChunkRead,
    UploadCompleteRead,
    UploadSessionCreate,
    UploadSessionRead,
)
from app.services import upload as upload_service
from app.services.ingest import reset_document_for_reindex
from app.services.parser import SUPPORTED_EXTENSIONS
from app.services.vector_store import delete_document_vectors, get_vector_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])


def _get_kb_or_404(db: DBSession, kb_id: uuid.UUID) -> KnowledgeBase:
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


def _get_kb_for_user(
    db: DBSession, kb_id: uuid.UUID, user: User
) -> KnowledgeBase:
    """读取知识库并校验可见性：私有库仅创建者与管理员可访问。"""
    kb = _get_kb_or_404(db, kb_id)
    if not user.is_admin and not kb.is_public and kb.owner_id != user.id:
        # 不暴露"存在但无权"，统一按不存在处理，避免探测私有库
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


def _kb_out(db: DBSession, kb: KnowledgeBase) -> KnowledgeBaseRead:
    count = db.scalar(
        select(func.count()).select_from(Document).where(Document.kb_id == kb.id)
    ) or 0
    data = KnowledgeBaseRead.model_validate(kb)
    data.document_count = count
    return data


@router.get("", response_model=ApiResponse[list[KnowledgeBaseRead]], summary="知识库列表")
def list_kbs(db: DBSession = Depends(get_db), user: User = Depends(get_current_user)):
    stmt = select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc())
    if not user.is_admin:
        stmt = stmt.where(
            (KnowledgeBase.is_public.is_(True)) | (KnowledgeBase.owner_id == user.id)
        )
    return ApiResponse(data=[_kb_out(db, kb) for kb in db.scalars(stmt).all()])


@router.post(
    "",
    response_model=ApiResponse[KnowledgeBaseRead],
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库（管理员）",
)
def create_kb(
    payload: KnowledgeBaseCreate,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == payload.name)):
        raise HTTPException(status_code=400, detail="知识库名称已存在")

    kb = KnowledgeBase(
        name=payload.name,
        description=payload.description,
        collection_name=f"kb_{uuid.uuid4().hex[:16]}",
        owner_id=admin.id,
        is_public=payload.is_public,
    )
    db.add(kb)
    db.commit()
    db.refresh(kb)
    logger.info(
        "创建知识库: %s (id=%s, collection=%s, owner=%s, is_public=%s)",
        kb.name, kb.id, kb.collection_name, admin.username, kb.is_public,
    )
    write_audit(db, request, admin, "kb_create", kb.name)
    return ApiResponse(data=_kb_out(db, kb))


@router.patch("/{kb_id}", response_model=ApiResponse[KnowledgeBaseRead], summary="更新知识库（管理员）")
def update_kb(
    kb_id: uuid.UUID,
    payload: KnowledgeBaseUpdate,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    kb = _get_kb_or_404(db, kb_id)
    data = payload.model_dump(exclude_unset=True)

    # 名称唯一：改名成已有名称会撞数据库唯一约束（500），这里先给出可读提示
    new_name = data.get("name")
    if new_name is not None and new_name != kb.name:
        conflict = db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.name == new_name, KnowledgeBase.id != kb.id
            )
        )
        if conflict is not None:
            raise HTTPException(status_code=400, detail="知识库名称已存在")

    for field, value in data.items():
        setattr(kb, field, value)
    db.commit()
    db.refresh(kb)
    logger.info("更新知识库: %s (id=%s) 字段=%s 操作人=%s", kb.name, kb.id, list(data), admin.username)
    write_audit(db, request, admin, "kb_update", kb.name, {"fields": list(data)})
    return ApiResponse(data=_kb_out(db, kb))


@router.delete("/{kb_id}", response_model=ApiResponse[None], summary="删除知识库（管理员）")
def delete_kb(
    kb_id: uuid.UUID,
    request: Request,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    kb = _get_kb_or_404(db, kb_id)
    name, collection = kb.name, kb.collection_name

    # 先删向量集合，再删数据库记录
    try:
        get_vector_store(collection).delete_collection()
        logger.info("已删除知识库向量集合: %s", collection)
    except Exception as exc:  # noqa: BLE001 - 集合可能本就不存在
        logger.warning("删除向量集合失败（可能是空库）: %s -> %s", collection, exc)

    # 清理磁盘文件
    removed = 0
    for doc in db.scalars(select(Document).where(Document.kb_id == kb.id)).all():
        Path(doc.stored_path).unlink(missing_ok=True)
        removed += 1

    db.delete(kb)
    db.commit()
    logger.info(
        "删除知识库: %s (id=%s) 清理文档文件 %d 个 操作人=%s", name, kb_id, removed, admin.username
    )
    write_audit(db, request, admin, "kb_delete", name)
    return ApiResponse(data=None, message="已删除")


# ---------------- 文档 ----------------

@router.get(
    "/{kb_id}/documents",
    response_model=ApiResponse[PageResult[DocumentRead]],
    summary="文档列表",
)
def list_documents(
    kb_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: str | None = Query(None, alias="status"),
    db: DBSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    _get_kb_for_user(db, kb_id, user)

    stmt = select(Document).where(Document.kb_id == kb_id)
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(Document.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return ApiResponse(
        data=PageResult(
            total=total,
            items=[DocumentRead.model_validate(r) for r in rows],
        )
    )


@router.post(
    "/{kb_id}/documents",
    response_model=ApiResponse[DocumentRead],
    summary="上传文档（小文件单请求，后台入库）",
)
def upload_document(
    kb_id: uuid.UUID,
    request: Request,
    file: UploadFile = File(...),
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    kb = _get_kb_or_404(db, kb_id)

    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext}，仅支持：{', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    doc_id = uuid.uuid4()
    target_dir = Path(settings.UPLOAD_DIR) / str(kb_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    stored_path = target_dir / f"{doc_id}{ext}"

    size = 0
    try:
        with stored_path.open("wb") as out:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"文件超过 {settings.MAX_UPLOAD_MB}MB 限制",
                    )
                out.write(chunk)
    except HTTPException:
        stored_path.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001
        stored_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"文件保存失败: {exc}") from exc

    doc = Document(
        id=doc_id,
        kb_id=kb.id,
        filename=file.filename or stored_path.name,
        file_ext=ext,
        file_size=size,
        stored_path=str(stored_path),
        status=DocumentStatus.PENDING.value,
        uploaded_by=user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    logger.info(
        "上传文档（单请求）: %s (%.2fMB) doc=%s kb=%s 操作人=%s，已入队待 worker 处理",
        doc.filename, size / 1024 / 1024, doc.id, kb.name, user.username,
    )
    # 入库由独立 worker 领取（status=pending）；API 进程重启不会丢任务
    write_audit(db, request, user, "document_upload", doc.filename, {"kb": kb.name})

    return ApiResponse(data=doc)


@router.post(
    "/{kb_id}/documents/{doc_id}/reindex",
    response_model=ApiResponse[DocumentRead],
    summary="重新索引文档",
)
def reindex_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value}:
        raise HTTPException(status_code=400, detail="文档正在处理中，请先取消入库")

    reset_document_for_reindex(db, doc)
    db.refresh(doc)
    logger.info("文档重建索引: %s doc=%s 操作人=%s", doc.filename, doc.id, admin.username)
    return ApiResponse(data=doc, message="已加入处理队列")


@router.post(
    "/{kb_id}/documents/{doc_id}/cancel",
    response_model=ApiResponse[DocumentRead],
    summary="取消入库",
)
def cancel_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """置取消标志，后台任务在下一批写入前检查到后终止并清理。"""
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")
    if doc.status not in {DocumentStatus.PENDING.value, DocumentStatus.PROCESSING.value}:
        raise HTTPException(status_code=400, detail="当前状态无法取消")

    doc.cancel_requested = True
    db.commit()
    db.refresh(doc)
    logger.info(
        "请求取消入库: %s doc=%s 当前状态=%s 进度=%d%% 操作人=%s",
        doc.filename, doc.id, doc.status, doc.progress, admin.username,
    )
    return ApiResponse(data=doc, message="取消请求已提交")


@router.delete(
    "/{kb_id}/documents/{doc_id}",
    response_model=ApiResponse[None],
    summary="删除文档",
)
def delete_document(
    kb_id: uuid.UUID,
    doc_id: uuid.UUID,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    kb = _get_kb_or_404(db, kb_id)
    doc = db.get(Document, doc_id)
    if doc is None or doc.kb_id != kb_id:
        raise HTTPException(status_code=404, detail="文档不存在")

    try:
        delete_document_vectors(kb.collection_name, str(doc.id))
    except Exception as exc:  # noqa: BLE001 - 向量可能未生成
        logger.warning("删除文档向量失败（可能尚未生成）: doc=%s -> %s", doc.id, exc)

    filename = doc.filename
    Path(doc.stored_path).unlink(missing_ok=True)
    # 明确删分块（不依赖数据库层 CASCADE 的 ondelete 行为）
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    db.delete(doc)
    db.commit()
    logger.info(
        "删除文档: %s doc=%s kb=%s 操作人=%s（向量、分块、源文件已一并清理）",
        filename, doc_id, kb.name, user.username,
    )
    write_audit(db, request, user, "document_delete", filename, {"kb": kb.name})
    return ApiResponse(data=None, message="已删除")


# ---------------- 分片上传（大文件断点续传） ----------------

def _session_out(session: UploadSession) -> UploadSessionRead:
    received = upload_service.received_indices(session)
    got_bytes = sum(upload_service.chunk_length(session, i) for i in received)
    return UploadSessionRead(
        upload_id=session.id,
        filename=session.filename,
        file_size=session.file_size,
        chunk_size=session.chunk_size,
        total_chunks=session.total_chunks,
        received=received,
        received_bytes=got_bytes,
        status=session.status,
        document_id=session.document_id,
    )


def _get_session_or_404(
    db: DBSession, kb_id: uuid.UUID, session_id: uuid.UUID
) -> UploadSession:
    session = upload_service.get_session(db, kb_id, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    return session


@router.post(
    "/{kb_id}/upload-sessions",
    response_model=ApiResponse[UploadSessionRead],
    summary="创建/恢复分片上传会话（管理员）",
)
def create_upload_session(
    kb_id: uuid.UUID,
    payload: UploadSessionCreate,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    _get_kb_or_404(db, kb_id)

    ext = Path(payload.filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型 {ext}，仅支持：{', '.join(sorted(SUPPORTED_EXTENSIONS))}",
        )

    session = upload_service.create_or_resume(
        db, kb_id, payload.filename, payload.file_size, ext, admin.id
    )
    logger.info(
        "上传会话: %s (%.2fMB, %d 片) session=%s 已收 %d 片 操作人=%s",
        session.filename, session.file_size / 1024 / 1024, session.total_chunks,
        session.id, len(session.received or []), admin.username,
    )
    return ApiResponse(data=_session_out(session))


@router.get(
    "/{kb_id}/upload-sessions/{session_id}",
    response_model=ApiResponse[UploadSessionRead],
    summary="查询上传会话进度（用于续传）",
)
def get_upload_session(
    kb_id: uuid.UUID,
    session_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    return ApiResponse(data=_session_out(_get_session_or_404(db, kb_id, session_id)))


@router.post(
    "/{kb_id}/upload-sessions/{session_id}/chunks/{index}",
    response_model=ApiResponse[UploadChunkRead],
    summary="上传一个分片（幂等）",
)
async def upload_chunk(
    kb_id: uuid.UUID,
    session_id: uuid.UUID,
    index: int,
    request: Request,
    db: DBSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    session = _get_session_or_404(db, kb_id, session_id)
    if session.status != UploadStatus.UPLOADING.value:
        raise HTTPException(status_code=400, detail="该上传会话已结束，请重新创建")
    if index < 0 or index >= session.total_chunks:
        raise HTTPException(
            status_code=400,
            detail=f"分片下标越界：应在 0 ~ {session.total_chunks - 1} 之间",
        )

    expected = upload_service.chunk_length(session, index)
    data = await request.body()
    if len(data) != expected:
        raise HTTPException(
            status_code=400,
            detail=f"分片大小不符：期望 {expected} 字节，实际 {len(data)} 字节",
        )

    created = upload_service.save_chunk(session, index, data)
    if created:
        received = upload_service.received_indices(session)
        session.received = received
        db.commit()
        # 每片都记太吵：只在进度里程碑或收尾时输出
        done = len(received)
        if done == session.total_chunks or done % 10 == 0:
            logger.info(
                "分片进度: %s session=%s %d/%d 片 (%.0f%%)",
                session.filename, session.id, done, session.total_chunks,
                done / max(1, session.total_chunks) * 100,
            )
    else:
        logger.debug(
            "分片已存在（幂等跳过）: %s session=%s 第 %d 片", session.filename, session.id, index
        )
    return ApiResponse(
        data=UploadChunkRead(index=index, created=created, received=len(session.received))
    )


@router.post(
    "/{kb_id}/upload-sessions/{session_id}/complete",
    response_model=ApiResponse[UploadCompleteRead],
    summary="合并分片并创建文档（幂等）",
)
def complete_upload(
    kb_id: uuid.UUID,
    session_id: uuid.UUID,
    request: Request,
    db: DBSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    kb = _get_kb_or_404(db, kb_id)
    session = _get_session_or_404(db, kb_id, session_id)

    if session.status == UploadStatus.COMPLETED.value and session.document_id:
        # 幂等重放：上次可能已建好文档，也可能在"合并完成→建行"之间崩过
        existing = db.get(Document, session.document_id)
        if existing is not None:
            logger.info(
                "完成上传（重放，已完成的会话）: %s session=%s doc=%s",
                session.filename, session.id, existing.id,
            )
            return ApiResponse(
                data=UploadCompleteRead(upload_id=session.id, document=existing),
                message="上传已完成",
            )
        target = upload_service.final_path(session.kb_id, session.document_id, session.file_ext)
        if target.exists():
            return ApiResponse(
                data=UploadCompleteRead(
                    upload_id=session.id,
                    document=_create_document(
                        db, request, user, kb, session, session.document_id
                    ),
                ),
                message="上传已完成",
            )

    if session.status == UploadStatus.ABORTED.value:
        raise HTTPException(status_code=400, detail="该上传会话已终止，请重新创建")

    missing = [
        i for i in range(session.total_chunks)
        if not (upload_service.tmp_dir(session.id) / f"{i}.part").exists()
    ]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"仍有 {len(missing)} 个分片未上传，例如：{missing[:10]}",
        )

    try:
        doc_id = upload_service.assemble(db, session)
    except OSError as exc:
        logger.error("合并分片失败（磁盘问题）: %s session=%s -> %s", session.filename, session.id, exc)
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        logger.error("合并分片失败（缺片）: %s session=%s -> %s", session.filename, session.id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    doc = db.get(Document, doc_id)
    if doc is None:
        doc = _create_document(db, request, user, kb, session, doc_id)
    logger.info(
        "完成分片上传: %s (%.2fMB, %d 片) session=%s doc=%s 操作人=%s，已入队",
        session.filename, session.file_size / 1024 / 1024, session.total_chunks,
        session.id, doc.id, user.username,
    )
    return ApiResponse(
        data=UploadCompleteRead(upload_id=session.id, document=doc),
        message="上传完成，已加入入库队列",
    )


def _create_document(
    db: DBSession,
    request: Request,
    user: User,
    kb: KnowledgeBase,
    session: UploadSession,
    doc_id: uuid.UUID,
) -> Document:
    target = upload_service.final_path(session.kb_id, doc_id, session.file_ext)
    doc = Document(
        id=doc_id,
        kb_id=session.kb_id,
        filename=session.filename,
        file_ext=session.file_ext,
        file_size=session.file_size,
        stored_path=str(target),
        status=DocumentStatus.PENDING.value,
        uploaded_by=user.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    logger.info(
        "创建文档记录（分片上传）: %s doc=%s kb=%s 上传人=%s",
        doc.filename, doc.id, kb.name, user.username,
    )
    write_audit(
        db, request, user, "document_upload", doc.filename,
        {"kb": kb.name, "mode": "chunked", "chunks": session.total_chunks},
    )
    return doc


@router.delete(
    "/{kb_id}/upload-sessions/{session_id}",
    response_model=ApiResponse[None],
    summary="放弃分片上传（清理临时分片）",
)
def abort_upload(
    kb_id: uuid.UUID,
    session_id: uuid.UUID,
    db: DBSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    session = _get_session_or_404(db, kb_id, session_id)
    if session.status == UploadStatus.UPLOADING.value:
        done = len(session.received or [])
        upload_service.abort_session(db, session)
        logger.info(
            "放弃分片上传: %s session=%s 已收 %d/%d 片，临时分片已清理 操作人=%s",
            session.filename, session.id, done, session.total_chunks, admin.username,
        )
    return ApiResponse(data=None, message="已放弃")
