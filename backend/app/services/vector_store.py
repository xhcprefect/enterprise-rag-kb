"""pgvector 向量存储封装（LangChain PGVector）。"""
import logging
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_ollama import OllamaEmbeddings
from langchain_postgres import PGVector
from sqlalchemy import delete

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


def delete_document_vectors(collection_name: str, document_id: str) -> int:
    """删除某个文档的所有向量，返回删除条数。

    注意：langchain-postgres 0.0.19 的 ``PGVector.delete()`` 只支持 ``ids``，
    其余关键字参数会被 ``**kwargs`` 静默吞掉（``delete(filter=...)`` 是空操作），
    所以这里直接按 ``cmetadata->>'document_id'`` 删。
    """
    store = get_vector_store(collection_name)
    embedding_store = store.EmbeddingStore

    with store._make_sync_session() as session:  # noqa: SLF001 - 官方未暴露按 metadata 删除
        collection = store.get_collection(session)
        if collection is None:
            return 0
        result = session.execute(
            delete(embedding_store).where(
                embedding_store.collection_id == collection.uuid,
                embedding_store.cmetadata["document_id"].astext == str(document_id),
            )
        )
        session.commit()
        deleted = result.rowcount or 0

    if deleted:
        logger.info("清理文档向量: %s -> %d 条", document_id, deleted)
    return deleted
