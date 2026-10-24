"""一次性迁移：给向量列补上维度，并建立 HNSW 索引。

背景
----
``langchain-postgres`` 只有在传入 ``embedding_length`` 时才会把列建成
``vector(N)``；不传就是无维度的 ``vector``，而 pgvector 拒绝给无维度列建
HNSW/IVFFlat 索引，检索会一直走全表顺序扫描。

``embedding_length`` 只保证**列类型**正确，**不会建索引**——而且索引与列类型无关，
是新库也得跑的一步。两种情况都需要：

1. 新库：入库首个文档后跑一次，补建 HNSW 索引（否则检索走全表扫描）；
2. 老库升级：列仍是 ``vector``，需要补维度，顺带补索引。

用法::

    python -m scripts.migrate_vector_index            # 执行
    python -m scripts.migrate_vector_index --dry-run  # 只看会做什么

安全性：整个迁移在一条更新类型的事务里完成；索引用 ``CONCURRENTLY`` 建，
不阻塞读写。重复执行是幂等的。
"""
from __future__ import annotations

import argparse
import logging
import sys

from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine
from app.services.vector_store import EMBEDDING_DIM

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")
logger = logging.getLogger("migrate")

INDEX_NAME = "ix_langchain_pg_embedding_hnsw"
TABLE = "langchain_pg_embedding"


def _column_type(conn) -> str | None:
    return conn.execute(
        text(
            "SELECT format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "WHERE c.relname = :table AND a.attname = 'embedding' "
            "AND a.attnum > 0 AND NOT a.attisdropped"
        ),
        {"table": TABLE},
    ).scalar()


def _index_exists(conn) -> bool:
    return bool(
        conn.execute(
            text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
            {"name": INDEX_NAME},
        ).scalar()
    )


def _row_count(conn) -> int:
    return conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar() or 0


def main() -> int:
    parser = argparse.ArgumentParser(description="补向量维度并建 HNSW 索引")
    parser.add_argument("--dry-run", action="store_true", help="只检查，不修改")
    args = parser.parse_args()

    with engine.connect() as conn:
        col_type = _column_type(conn)
        if col_type is None:
            logger.warning("%s 表不存在（还没有任何向量数据）——无需迁移", TABLE)
            return 0
        logger.info("当前列类型: %s", col_type)
        logger.info("目标维度: %d", EMBEDDING_DIM)

        # 维度不匹配时直接拒绝，避免 ALTER 在脏数据上失败一半
        if col_type.lower() == "vector":
            bad = conn.execute(
                text(
                    f"SELECT count(*) FROM {TABLE} "
                    f"WHERE embedding IS NOT NULL AND vector_dims(embedding) <> :dim"
                ),
                {"dim": EMBEDDING_DIM},
            ).scalar() or 0
            if bad:
                logger.error("有 %d 条向量不是 %d 维，先重建索引再迁移", bad, EMBEDDING_DIM)
                return 1
            logger.info("需要补维度（%d 行数据将在事务内重写）", _row_count(conn))
        elif col_type == f"vector({EMBEDDING_DIM})":
            logger.info("列维度已正确，跳过 ALTER")
        else:
            logger.error("列类型 %s 与预期不符，请人工确认", col_type)
            return 1

        idx = _index_exists(conn)
        logger.info("HNSW 索引%s", "已存在" if idx else "待创建")

        if args.dry_run:
            logger.info("--dry-run：未做任何修改")
            return 0

    if col_type.lower() == "vector":
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {TABLE} "
                    f"ALTER COLUMN embedding TYPE vector({EMBEDDING_DIM}) "
                    f"USING embedding::vector({EMBEDDING_DIM})"
                )
            )
        logger.info("✔ 列类型已改为 vector(%d)", EMBEDDING_DIM)

    if not idx:
        # CONCURRENTLY 不能放在事务里，需要 AUTOCOMMIT
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(
                text(
                    f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} "
                    f"ON {TABLE} USING hnsw (embedding vector_cosine_ops)"
                )
            )
        logger.info("✔ 已创建 HNSW 索引 %s（余弦距离）", INDEX_NAME)

    with engine.connect() as conn:
        logger.info("完成。列类型=%s 行数=%d", _column_type(conn), _row_count(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
