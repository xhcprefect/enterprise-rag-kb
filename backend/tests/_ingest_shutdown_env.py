"""B 专用停机回归的环境准备：必须在导入任何 ``app.*`` 之前 import 本模块。

约束（与任务边界一致）：
- 只接受显式提供、数据库名以 ``_test`` 结尾的连接；
- 显式禁用 .env，避免从 backend 工作目录读到真实配置；
- 模型地址指向 loopback 黑洞，测试不会发出任何真实请求；
- 上传/日志落到独立临时目录。

未提供 ``TEST_DATABASE_URL`` 时本模块不导入应用、不修改环境，宿主测试类
通过 ``ENABLED`` 跳过，避免拖垮其它测试模块的发现。
"""
import os
import tempfile

from tests._env import TEST_URL

# RAG_DISABLE_DOTENV 必须在 app.core.config 导入前设置；这里再兜底一次，
# 使单独运行本组测试时也不依赖外部 shell 是否导出了该开关。
os.environ["RAG_DISABLE_DOTENV"] = "1"

ENABLED = False
_REASON = "设置 TEST_DATABASE_URL 才运行 B 停机回归"
_STATE = tempfile.mkdtemp(prefix="ingest-shutdown-")
TEST_URL_VALUE = TEST_URL

engine = None
SessionLocal = None


def _configure() -> None:
    """仅在有专用测试库时导入应用并绑定 engine。"""
    global ENABLED, _REASON, engine, SessionLocal
    if not TEST_URL:
        return
    from sqlalchemy.engine import make_url

    url = make_url(TEST_URL)
    if not (url.database or "").endswith("_test"):
        _REASON = "TEST_DATABASE_URL 必须指向以 _test 结尾的专用数据库"
        return
    existing = os.environ.get("DATABASE_URL")
    if existing and make_url(existing) != url:
        _REASON = "DATABASE_URL 与 TEST_DATABASE_URL 不一致，拒绝在业务库上运行停机回归"
        return

    os.environ.update(
        DATABASE_URL=TEST_URL,
        JWT_SECRET="regression-tests-only-not-for-production",
        EMBEDDING_PROVIDER="ollama",
        LLM_PROVIDER="ollama",
        OLLAMA_BASE_URL="http://127.0.0.1:9",
        EMBEDDING_BASE_URL="http://127.0.0.1:9/v1",
        EMBEDDING_API_KEY="not-a-real-key",
        LLM_BASE_URL="http://127.0.0.1:9",
        LLM_API_KEY="not-a-real-key",
        UPLOAD_DIR=os.path.join(_STATE, "uploads"),
        LOG_DIR=os.path.join(_STATE, "logs"),
        RERANK_ENABLED="false",
    )

    from app.db import session as db_session
    from app.db.session import engine as app_engine

    # 与 test_database 一样：显式把应用 engine 绑到专用测试库，避免导入顺序影响。
    if app_engine.url != url:
        from sqlalchemy import create_engine

        app_engine.dispose()
        bound = create_engine(TEST_URL, pool_pre_ping=True, pool_size=10,
                              max_overflow=20, pool_recycle=1800)
        db_session.engine = bound
        db_session.SessionLocal.configure(bind=bound)
        from app.db import session as _session
        _session.SessionLocal.configure(bind=bound)
        engine = bound
    else:
        engine = app_engine
    SessionLocal = db_session.SessionLocal
    ENABLED = True


_configure()


def assert_owned_test_database() -> None:
    """每个测试类在清理前再次确认连接的确实是 _test 库。"""
    if not ENABLED:
        raise RuntimeError(_REASON)
    if not (engine.url.database or "").endswith("_test"):
        raise RuntimeError("应用引擎未指向专用测试库，拒绝清理")


def truncate_all() -> None:
    from sqlalchemy import text

    assert_owned_test_database()
    with engine.begin() as conn:
        conn.execute(text(
            "TRUNCATE users, knowledge_bases, documents, conversations, upload_sessions, "
            "audit_logs, document_chunks, messages, chat_turns, model_usage, model_prices "
            "RESTART IDENTITY CASCADE"
        ))
        if conn.execute(text("SELECT to_regclass('langchain_pg_collection')")).scalar():
            conn.execute(text("TRUNCATE langchain_pg_collection CASCADE"))
