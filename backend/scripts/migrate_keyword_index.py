"""为结构化分块的关键词全文检索建索引；不修改源文档或分块内容。"""
import argparse

from sqlalchemy import inspect, text
from app.db.session import engine

INDEX_NAME = "ix_document_chunks_search_gin"
DDL = f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX_NAME} ON document_chunks USING gin (to_tsvector('simple', search_text))"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只检查前置条件和索引状态")
    args = parser.parse_args(argv)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        if not inspect(conn).has_table("document_chunks"):
            parser.error("请先启动新版 API 完成表结构迁移")
        if "search_text" not in {column["name"] for column in inspect(conn).get_columns("document_chunks")}:
            parser.error("缺少 search_text 列，请先完成迁移")
        existing = conn.execute(text("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relname=:name AND n.nspname=current_schema()"), {"name": INDEX_NAME}).scalar()
        if existing is False:
            parser.error("存在无效的同名索引；请由管理员检查后删除无效索引再重试")
        if args.dry_run:
            print("keyword index:", "ready" if existing else "missing; would create concurrently")
            return 0
        conn.execute(text("SET lock_timeout = '5s'"))
        conn.execute(text(DDL))
        print("keyword index ready; existing documents need reindexing to populate search_text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
