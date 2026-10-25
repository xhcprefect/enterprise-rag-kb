"""pgvector 向量存储封装（LangChain PGVector）。"""
import logging
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_ollama import OllamaEmbeddings
from langchain_postgres import PGVector
from sqlalchemy import String, cast, delete, func, select
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
    """按知识库返回独立的向量集合（进程内缓存，避免每次检索重建引擎与表）。"""
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


def search_ready_vectors(store: PGVector, vector: list[float], k: int) -> list[tuple[LCDocument, float]]:
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
        if settings.RETRIEVAL_MIN_SIMILARITY > 0:
            statement = statement.where(distance <= 1.0 - settings.RETRIEVAL_MIN_SIMILARITY)
        result = db.execute(statement.order_by(distance).limit(k)).all()
        return [(LCDocument(page_content=text, metadata=meta or {}), float(score)) for text, meta, score in result]
