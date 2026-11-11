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
        # ---- 文档治理（T3 冻结契约）----
        # 全部带真实 server DEFAULT：已有行在 ALTER 时自动补齐，且绕过 ORM 的写入
        # 也得到确定值。superseded_by_id 的 FK 由 ensure_document_governance_fk 单独
        # 兜底（列已存在但缺 FK 的老库也能补上），这里只建裸列避免重复约束。
        "maintainer": "VARCHAR(200) NOT NULL DEFAULT ''",
        "last_reviewed_at": "TIMESTAMPTZ",
        "valid_from": "TIMESTAMPTZ",
        "valid_until": "TIMESTAMPTZ",
        "enabled": "BOOLEAN NOT NULL DEFAULT TRUE",
        "superseded_at": "TIMESTAMPTZ",
        "superseded_by_id": "UUID",
        "governance_revision": "INTEGER NOT NULL DEFAULT 0",
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
    "feedback_regression_runs": {
        # R1b：旧准备/导入轮次补列后保持 manual；在线作业轮次显式写 online。
        "execution_kind": "VARCHAR(16) NOT NULL DEFAULT 'manual'",
    },
    "model_usage": {
        # 在线反馈回归作业关联；裸 UUID、无级联外键。
        "regression_job_id": "UUID",
    },
}


# 建表期的互斥锁键。API 与 worker 会同时启动并各自调用 init_db，
# 而 ``CREATE EXTENSION IF NOT EXISTS`` 在并发下并不安全——两个事务同时
# 通过 "不存在" 检查后一起插入，后到的那个会撞 pg_extension_name_index 唯一约束
# 直接报错，进程退出。用 advisory lock 把整个初始化串行化。
_INIT_LOCK_KEY = 742_193_551


def _create_all(conn) -> None:
    Base.metadata.create_all(bind=conn)


# 文档治理列的真实 server default：列已存在（老库迁移补列）时也强制对齐，
# 保证任何绕过 ORM 的 INSERT 都得到确定值而不是 NULL。
_GOVERNANCE_SERVER_DEFAULTS = {
    "maintainer": "''",
    "enabled": "TRUE",
    "governance_revision": "0",
}

_SUPERSEDED_FK_NAME = "fk_documents_superseded_by_id_documents"


def _ensure_document_governance_constraints(conn) -> None:
    """补齐治理列的 server default 与自引用外键（幂等）。

    两种场景都要覆盖：
    - 新库：create_all 已建好列与外键，这里全是空操作。
    - 老库：列可能是上一版迁移补的（无 DEFAULT），或列已存在但 FK 缺失；
      这里补 DEFAULT / 补 FK，绝不删改任何原用户内容。
    """
    existing = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'documents'"
            )
        )
    }
    if "superseded_by_id" not in existing:
        return
    for column, default in _GOVERNANCE_SERVER_DEFAULTS.items():
        if column in existing:
            conn.execute(text(f"ALTER TABLE documents ALTER COLUMN {column} SET DEFAULT {default}"))
    # FK 存在性判定必须锚定当前 schema 里实际 documents 表的 OID：
    # 其他 schema 内同名 documents 的 FK（哪怕也是自引用 SET NULL）不能算数；
    # 复合外键（conkey 多于 1 列）或指向非 id 列同样不算所需的单列自 FK。
    table_oid = conn.execute(
        text(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relname = 'documents' AND n.nspname = current_schema() LIMIT 1"
        )
    ).scalar()
    if table_oid is None:
        return
    has_fk = conn.execute(
        text(
            "SELECT 1 FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = c.confkey[1] "
            "WHERE c.contype = 'f' AND c.conrelid = :table_oid "
            "AND cardinality(c.conkey) = 1 AND cardinality(c.confkey) = 1 "
            "AND a.attname = 'superseded_by_id' AND ra.attname = 'id' "
            "AND c.conrelid = c.confrelid "
            "AND c.confdeltype = 'n' LIMIT 1"
        ),
        {"table_oid": table_oid},
    ).first()
    if has_fk is None:
        conn.execute(
            text(
                "ALTER TABLE documents ADD CONSTRAINT "
                f"{_SUPERSEDED_FK_NAME} FOREIGN KEY (superseded_by_id) "
                "REFERENCES documents(id) ON DELETE SET NULL"
            )
        )



def _ensure_feedback_regression_request_scope(conn, table_names: set[str]) -> None:
    """R1b 幂等迁移：请求幂等键从“全局 UUID”改为“管理员 + UUID”。

    旧版本给 ``feedback_regression_jobs.request_id`` / ``feedback_regression_batches.request_id``
    建的是全局唯一约束，会让两个管理员无法各自使用同一个客户端 UUID。这里显式删除旧约束
    （及其索引），再按 ``(admin_id, request_id)`` 建唯一索引；可重复执行。
    """
    targets = (
        ("feedback_regression_jobs", "uq_feedback_regression_jobs_request",
         "uq_feedback_regression_jobs_admin_request"),
        ("feedback_regression_batches", "uq_feedback_regression_batches_request",
         "uq_feedback_regression_batches_admin_request"),
    )
    for table, old_name, new_name in targets:
        if table not in table_names:
            continue
        conn.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {old_name}"))
        conn.execute(text(f"DROP INDEX IF EXISTS {old_name}"))
        # 老库的 admin_id 列由 R1b 更早版本建表时已存在；若表是更早的实验版本且缺列，
        # 这里补一个可空列，保证唯一索引能建立（缺失归属的旧行彼此不冲突）。
        columns = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :t"
                ),
                {"t": table},
            )
        }
        if "admin_id" not in columns:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN admin_id UUID"))
        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {new_name} "
            f"ON {table} (admin_id, request_id)"
        ))

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

        if "documents" in table_names:
            _ensure_document_governance_constraints(conn)

        # R1b：请求幂等键按管理员隔离的真实迁移（旧全局唯一约束 -> admin+UUID 唯一索引）。
        _ensure_feedback_regression_request_scope(conn, table_names)

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
        # R1b：在线回归用量关联索引；老库由这里幂等补齐（create_all 不补缺失索引）。
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_model_usage_regression_job ON model_usage (regression_job_id)"))
        if "feedback_regression_runs" in table_names:
            # 兼容曾经手工补过可空列/缺省值的老库：已知 NULL 语义只可能是旧 manual。
            conn.execute(text(
                "UPDATE feedback_regression_runs SET execution_kind = 'manual' "
                "WHERE execution_kind IS NULL"
            ))
            conn.execute(text(
                "ALTER TABLE feedback_regression_runs ALTER COLUMN execution_kind SET DEFAULT 'manual'"
            ))
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
