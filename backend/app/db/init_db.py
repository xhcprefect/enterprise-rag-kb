"""启动时建表、安装扩展、初始化管理员账号。"""
import logging

from sqlalchemy import inspect, select, text

from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.models import User, UserRole

logger = logging.getLogger(__name__)

# 轻量迁移：``create_all`` 只会建缺失的表，不会给已存在的表补列，
# 所以这些列要单独 ALTER。表还不存在时跳过（新库交给 create_all）。
_ADDED_COLUMNS: dict[str, str] = {
    "progress": "INTEGER NOT NULL DEFAULT 0",
    "cancel_requested": "BOOLEAN NOT NULL DEFAULT FALSE",
    "heartbeat_at": "TIMESTAMPTZ",
    "parse_done": "BOOLEAN NOT NULL DEFAULT FALSE",
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
        if "documents" in table_names:
            cols = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'documents'"
                    )
                )
            }
            for name, ddl in _ADDED_COLUMNS.items():
                if name not in cols:
                    conn.execute(
                        text(f"ALTER TABLE documents ADD COLUMN {name} {ddl}")
                    )

        # upload_sessions.document_id 是"合并认领标记"，Document 行晚于它创建，不能带外键
        if "upload_sessions" in table_names:
            conn.execute(
                text(
                    "ALTER TABLE upload_sessions "
                    "DROP CONSTRAINT IF EXISTS upload_sessions_document_id_fkey"
                )
            )

    if not create_admin:
        return

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == settings.ADMIN_USERNAME))
        if admin is None:
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
