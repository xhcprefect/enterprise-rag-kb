"""启动时建表、安装扩展、初始化管理员账号。"""
import logging

from sqlalchemy import inspect, select, text

from app.core.config import settings
from app.core.security import hash_password, verify_password
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models import User, UserRole

logger = logging.getLogger(__name__)

# 轻量迁移：``create_all`` 只会建缺失的表，不会给已存在的表补列，
# 所以这些列要单独 ALTER。表还不存在时跳过（新库交给 create_all）。
# 按表分组，新增表直接往里加一项即可。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "users": {"token_version": "INTEGER NOT NULL DEFAULT 0"},
    "upload_sessions": {
        "content_sha256": "VARCHAR(64)",
        # 分片完成时是否复用了同库已有文档；持久化以保证 complete 重放一致。
        "deduplicated": "BOOLEAN NOT NULL DEFAULT FALSE",
    },
    "documents": {
        "progress": "INTEGER NOT NULL DEFAULT 0",
        "cancel_requested": "BOOLEAN NOT NULL DEFAULT FALSE",
        "heartbeat_at": "TIMESTAMPTZ",
        "parse_done": "BOOLEAN NOT NULL DEFAULT FALSE",
        "index_version": "INTEGER NOT NULL DEFAULT 0",
        "lease_id": "UUID",
        # 服务端真实内容摘要；老数据保持 NULL，由 backfill 脚本按需补齐。
        "content_sha256": "VARCHAR(64)",
    },
    "conversations": {
        # 普通对话模式新增；老会话默认按知识库问答解释，行为与升级前一致
        "mode": "VARCHAR(16) NOT NULL DEFAULT 'rag'",
        "active_turn_id": "UUID",
        "lease_expires_at": "TIMESTAMPTZ",
        "document_scope": "JSONB NOT NULL DEFAULT '[]'::jsonb",
        "scope_revision": "INTEGER NOT NULL DEFAULT 0",
        # 会话置顶时间；NULL 表示未置顶，由会话排序使用。
        "pinned_at": "TIMESTAMPTZ",
    },
    "messages": {
        "turn_id": "UUID REFERENCES chat_turns(id) ON DELETE CASCADE",
        "reply_to_id": "UUID REFERENCES messages(id) ON DELETE SET NULL",
        "status": "VARCHAR(16) NOT NULL DEFAULT 'completed'",
    },
    "document_chunks": {
        "source_metadata": "JSONB NOT NULL DEFAULT '{}'::jsonb",
        "search_text": "TEXT NOT NULL DEFAULT ''",
    },
}


# 建表期的互斥锁键。API 与 worker 会同时启动并各自调用 init_db，
# 而 ``CREATE EXTENSION IF NOT EXISTS`` 在并发下并不安全——两个事务同时
# 通过 "不存在" 检查后一起插入，后到的那个会撞 pg_extension_name_index 唯一约束
# 直接报错，进程退出。用 advisory lock 把整个初始化串行化。
_INIT_LOCK_KEY = 742_193_551


def _create_all(conn) -> None:
    Base.metadata.create_all(bind=conn)


def init_db(create_admin: bool = True) -> None:
    """建表 + 幂等迁移。``create_admin`` 只在 API 进程里为 True。

    worker 不该负责建管理员：它与 API 同时启动时可能抢先用默认密码建出账号，
    之后 API 读到"已存在"就跳过，导致 .env 里配置的密码从未生效。
    """
    # 整个初始化放在一个事务里，advisory lock 在事务结束（提交）时自动释放，
    # 并发启动的另一个进程会阻塞到这边提交后再继续，不会再撞唯一约束。
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _INIT_LOCK_KEY})
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        # 顺序很关键：旧实现在 create_all 之前就 ALTER documents，
        # 空库（全新部署、重建容器卷）会直接 UndefinedTable 崩溃，服务起不来。
        _create_all(conn)

        table_names = set(inspect(conn).get_table_names())
        for table, columns in _ADDED_COLUMNS.items():
            if table not in table_names:
                continue
            existing = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND table_name = :t"
                    ),
                    {"t": table},
                )
            }
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

        if "messages" in table_names:
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_turn_role ON messages (turn_id, role)"))

        # 本轮新增（记忆摘要 + 项目索引）的列与部分唯一索引：统一交给专用迁移 helper。
        from app.db.migrations import ensure_foundation_schema

        ensure_foundation_schema(conn)

        # 文档内容去重：部分唯一索引只约束已算出摘要的行。
        # 老库全为 NULL，因此可安全创建；若历史数据里已存在重复非空摘要
        # （手工导入/绕过约束写入），唯一索引建不起来——此时 fail closed，
        # 不允许在没有并发去重约束的情况下继续启动。脚本也不会自动改/删任何行，
        # 只有人工确认 canonical 后清掉其余行的 hash 才能继续。
        if "documents" in table_names:
            duplicates = conn.execute(text(
                "SELECT kb_id, content_sha256, count(*) AS n, "
                "array_agg(id::text ORDER BY created_at, id::text) AS ids "
                "FROM documents WHERE content_sha256 IS NOT NULL "
                "GROUP BY kb_id, content_sha256 HAVING count(*) > 1"
            )).all()
            if duplicates:
                details = "; ".join(
                    f"kb={row.kb_id} sha256={row.content_sha256} rows={row.n} ids={','.join(row.ids)}"
                    for row in duplicates
                )
                raise RuntimeError(
                    "documents 存在重复 content_sha256，无法创建并发去重唯一索引 "
                    "uq_documents_kb_content_sha256。启动已中止，避免在没有约束的情况下运行。"
                    f"重复组：{details}。请先确认每组保留哪一行（canonical），"
                    "人工把其余行的 content_sha256 置为 NULL（不删除文档/引用/向量），再重启服务；"
                    "可先运行 scripts/backfill_document_hashes.py（默认 dry-run）查看重复组与缺文件。"
                )
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_documents_kb_content_sha256 "
                "ON documents (kb_id, content_sha256) WHERE content_sha256 IS NOT NULL"
            ))

        # conversations.kb_id 原来非空（会话必须挂知识库）。普通对话没有知识库，
        # 需要放开。已是可空时这条是空操作，可重复执行。
        if "conversations" in table_names:
            conn.execute(
                text("ALTER TABLE conversations ALTER COLUMN kb_id DROP NOT NULL")
            )

        # upload_sessions.document_id 是"合并认领标记"，Document 行晚于它创建，不能带外键
        if "upload_sessions" in table_names:
            conn.execute(
                text(
                    "ALTER TABLE upload_sessions "
                    "DROP CONSTRAINT IF EXISTS upload_sessions_document_id_fkey"
                )
            )

        # 已存在的计量表也补上索引，避免启动补录检查逐条扫描整个账本。
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_model_usage_turn_operation ON model_usage (turn_id, operation)"))
        # 独立计量表随 create_all 创建；补录仅接受历史中明确返回的实际用量。
        from app.services.usage_statistics import initialize_usage
        initialize_usage(conn)

    if not create_admin:
        return

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == settings.ADMIN_USERNAME))
        if admin is not None:
            # 管理员已存在，ADMIN_PASSWORD 不再有任何作用——它只在建号那一次被读取。
            # 但如果配置文件里的值已经和库里的密码对不上，它就成了一条会误导人的
            # 过期记录：有人会以为"改这里就能改密码"，或者拿它当找回凭据。
            # 只在这种真的对不上时才告警，否则每次重启都刷一行没意义的日志。
            if settings.ADMIN_PASSWORD and not verify_password(
                settings.ADMIN_PASSWORD, admin.password_hash
            ):
                logger.warning(
                    "backend/.env 的 ADMIN_PASSWORD 与管理员 %s 的实际密码不一致，"
                    "该配置项已失效：改密码请走「个人中心」，或删除这一行避免误导",
                    settings.ADMIN_USERNAME,
                )
            return
        if settings.ENVIRONMENT == "production" and (
            len(settings.ADMIN_PASSWORD) < 12 or settings.ADMIN_PASSWORD in {"Admin@123456", "change-me-on-first-login"}
        ):
            raise RuntimeError("首次创建生产管理员前必须配置至少 12 位的非默认 ADMIN_PASSWORD")
        admin = User(
            username=settings.ADMIN_USERNAME,
            display_name="系统管理员",
            password_hash=hash_password(settings.ADMIN_PASSWORD),
            role=UserRole.ADMIN.value,
        )
        db.add(admin)
        try:
            db.commit()
            logger.info("已创建初始管理员: %s", settings.ADMIN_USERNAME)
        except Exception:  # noqa: BLE001 - API 与 worker 可能同时启动
            db.rollback()
            logger.info("初始管理员已由其他进程创建: %s", settings.ADMIN_USERNAME)
