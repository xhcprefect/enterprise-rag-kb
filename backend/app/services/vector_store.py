"""pgvector 向量存储封装（LangChain PGVector）。"""
import logging
import uuid
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_ollama import OllamaEmbeddings
from langchain_postgres import PGVector
from sqlalchemy import String, and_, cast, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from langchain_core.documents import Document as LCDocument

from app.models import Document, DocumentStatus, KnowledgeBase

from app.core.config import settings
from app.db.session import engine as app_engine
from app.services.cloud_models import get_cloud_embeddings

logger = logging.getLogger(__name__)

# 向量维度必须显式声明，否则 langchain-postgres 会把列建成无维度的 ``vector``，
# 而 pgvector 不允许给无维度列建 HNSW/IVFFlat 索引。
# bge-m3（云端与本地）都是 1024 维。
#
# 注意：embedding_length 只决定列类型是 vector(1024) 还是 vector，
# langchain-postgres 0.0.19 的 PGVector 不会自己建索引——它只建表与集合。
# 索引需要跑 scripts/migrate_vector_index.py 手工补，否则检索走全表顺序扫描。
EMBEDDING_DIM = 1024


@lru_cache
def _vector_engine():
    """向量库专用的 SQLAlchemy 引擎，进程内复用。

    原来每次 ``get_vector_store()`` 都传连接串，langchain-postgres 会为此新建一个
    Engine（默认池 5+10）且永不 dispose。检索发生在每次问答，连接会持续累积。
    这里复用应用自己的引擎，连接池与 API 其余部分共享。
    """
    return app_engine


@lru_cache(maxsize=64)
def _store(collection_name: str) -> PGVector:
    return PGVector(
        embeddings=get_embeddings(),
        collection_name=collection_name,
        connection=_vector_engine(),
        embedding_length=EMBEDDING_DIM,
    )


@lru_cache
def get_embeddings() -> Embeddings:
    """按 EMBEDDING_PROVIDER 返回向量模型。

    两种实现产出的都是 1024 维、同一向量空间（云端 BAAI/bge-m3 与本地 bge-m3:latest
    是同一份权重），所以切换服务商不影响已入库的向量。
    """
    if settings.EMBEDDING_PROVIDER == "cloud":
        logger.info(
            "向量模型：%s @ %s", settings.EMBEDDING_MODEL, settings.EMBEDDING_BASE_URL
        )
        return get_cloud_embeddings()
    return OllamaEmbeddings(
        model=settings.EMBEDDING_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
    )


def get_vector_store(collection_name: str) -> PGVector:
    """按知识库返回独立的向量集合（进程内缓存，避免每次检索重建引擎与表）。

    参数必须是 ``KnowledgeBase.collection_name``（形如 ``kb_99ae064d594a4b2f``），
    不是知识库的 UUID。``search_ready_vectors`` 会拿它和库里的 collection_name
    做等值匹配，传错了不报错、只是静默返回空结果——排查起来很像"检索坏了"。
    """
    return _store(collection_name)


def delete_document_vectors(
    collection_name: str, document_id: str, *, db: Session | None = None
) -> int:
    """清理向量；传入 db 时与文档删除/取消共用事务，不自行提交。"""
    store = get_vector_store(collection_name)
    if db is None:
        with store._make_sync_session() as session:
            deleted = _delete_vectors(store, session, document_id)
            session.commit()
            return deleted
    return _delete_vectors(store, db, document_id)


def _delete_vectors(store: PGVector, db: Session, document_id: str) -> int:
    collection = store.get_collection(db)
    if collection is None:
        return 0
    rows = store.EmbeddingStore
    result = db.execute(delete(rows).where(
        rows.collection_id == collection.uuid,
        rows.cmetadata["document_id"].astext == document_id,
    ))
    return result.rowcount or 0


def persist_vectors(
    db: Session, store: PGVector, documents: list[LCDocument],
    vectors: list[list[float]], ids: list[str],
) -> None:
    """在调用方已锁住文档并检查租约后，原子写向量与分块进度。"""
    if len(vectors) != len(documents) or any(len(v) != EMBEDDING_DIM for v in vectors):
        raise ValueError(f"向量数量或维度不符（要求 {EMBEDDING_DIM} 维）")
    collection = store.get_collection(db)
    if collection is None:
        raise ValueError("知识库向量集合不存在")
    rows = store.EmbeddingStore
    statement = insert(rows).values([
        {"id": identifier, "collection_id": collection.uuid, "embedding": vector,
         "document": doc.page_content, "cmetadata": doc.metadata}
        for identifier, vector, doc in zip(ids, vectors, documents)
    ])
    db.execute(statement.on_conflict_do_update(
        index_elements=["id"], set_={
            "collection_id": statement.excluded.collection_id,
            "embedding": statement.excluded.embedding,
            "document": statement.excluded.document,
            "cmetadata": statement.excluded.cmetadata,
        },
    ))


def load_ready_citations(collection_name: str, citations: list[dict]) -> list[tuple[LCDocument, float]]:
    """按历史来源身份读取当前仍有效的原文，不把旧 snippet 当作新证据。

    受当前知识库、ready 状态、取消标志、索引代次共同约束。缺少代次的旧引用
    仅接受仍在第 0 代、且片段前缀完全匹配的原文；重建后不能靠相同编号复活。
    """
    from app.db.session import SessionLocal

    requests = []
    for citation in citations[:20]:
        try:
            document_id = str(uuid.UUID(str(citation.get("document_id"))))
            chunk_index = citation.get("chunk_index")
            version = citation.get("index_version")
            if not isinstance(chunk_index, int) or isinstance(chunk_index, bool) or chunk_index < 0:
                continue
            if version is not None and (not isinstance(version, int) or isinstance(version, bool) or version < 0):
                continue
            start = citation.get("chunk_start") if citation.get("chunk_start") is not None else chunk_index
            end = citation.get("chunk_end") if citation.get("chunk_end") is not None else chunk_index
            if any(not isinstance(value, int) or isinstance(value, bool) for value in (start, end)) or not 0 <= start <= chunk_index <= end or end - start > 4:
                continue
            requests.append((document_id, chunk_index, version or 0, citation, start, end))
        except (ValueError, TypeError, AttributeError):
            continue
    if not requests:
        return []
    store = get_vector_store(collection_name)
    rows = store.EmbeddingStore
    with SessionLocal() as db:
        collection = store.get_collection(db)
        if collection is None:
            return []
        statement = (
            select(rows.document, rows.cmetadata)
            .join(Document, cast(Document.id, String) == rows.cmetadata["document_id"].astext)
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .where(
                rows.collection_id == collection.uuid,
                KnowledgeBase.collection_name == collection_name,
                Document.status == DocumentStatus.READY.value,
                Document.cancel_requested.is_(False),
                func.coalesce(rows.cmetadata["index_version"].astext, "0") == cast(Document.index_version, String),
                or_(*(and_(rows.cmetadata["document_id"].astext == document_id,
                           rows.cmetadata["chunk_index"].astext.in_([str(index) for index in range(start, end + 1)]),
                           Document.index_version == version)
                      for document_id, chunk_index, version, _, start, end in requests)),
            )
        )
        fetched = db.execute(statement).all()
    found = {}
    for text, metadata in fetched:
        meta = metadata or {}
        key = (meta.get("document_id"), meta.get("chunk_index"), meta.get("index_version") or 0)
        found[key] = (text, meta)
    result = []
    from app.services.source_documents import merge_locations, public_location

    for document_id, chunk_index, version, citation, start, end in requests:
        chunks = [found.get((document_id, index, version)) for index in range(start, end + 1)]
        current = found.get((document_id, chunk_index, version))
        if current is None or any(chunk is None or not isinstance(chunk[0], str) or not chunk[0].strip() for chunk in chunks):
            continue
        anchor_text, meta = current
        text = "\n\n".join(chunk[0] for chunk in chunks)
        snippet = citation.get("snippet")
        if citation.get("index_version") is None and (not isinstance(snippet, str) or not snippet or not text.startswith(snippet)):
            continue
        result.append((LCDocument(page_content=text, metadata={
            **meta, **merge_locations([chunk[1] for chunk in chunks]), "index_version": version, "citation_score": None,
            "chunk_start": start, "chunk_end": end,
            "_anchor_content": anchor_text, "_anchor_location": public_location(meta),
            "historical_source_index": citation.get("source_index"),
            "historical_message_id": citation.get("source_message_id"),
        }), 0.0))
    return result


def search_ready_vectors(store: PGVector, vector: list[float], k: int, *,
                         document_scope: list[dict] | None = None) -> list[tuple[LCDocument, float]]:
    """在 SQL 中过滤无效文档和旧代次；兼容旧向量缺少 index_version 的情况。"""
    from app.db.session import SessionLocal

    rows = store.EmbeddingStore
    distance = rows.embedding.cosine_distance(vector).label("distance")
    with SessionLocal() as db:
        collection = store.get_collection(db)
        if collection is None:
            return []
        statement = (
            select(rows.document, rows.cmetadata, distance)
            .join(Document, cast(Document.id, String) == rows.cmetadata["document_id"].astext)
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .where(
                rows.collection_id == collection.uuid,
                KnowledgeBase.collection_name == store.collection_name,
                Document.status == DocumentStatus.READY.value,
                Document.cancel_requested.is_(False),
                func.coalesce(rows.cmetadata["index_version"].astext, "0") == cast(Document.index_version, String),
            )
        )
        if document_scope is not None:
            from app.services.document_scopes import scope_filter
            statement = statement.where(scope_filter(document_scope))
        if settings.RETRIEVAL_MIN_SIMILARITY > 0:
            statement = statement.where(distance <= 1.0 - settings.RETRIEVAL_MIN_SIMILARITY)
        result = db.execute(statement.order_by(distance).limit(k)).all()
        return [(LCDocument(page_content=text, metadata=meta or {}), float(score)) for text, meta, score in result]
