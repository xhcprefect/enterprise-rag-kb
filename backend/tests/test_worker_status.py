"""worker 进程心跳与角色队列观测。

覆盖：
- 固定 role 顺序、启用状态、not_started/running/stale/stopped/disabled/unknown；
- 多实例聚合、三个角色的任务统计、空队列、最老等待；
- 最近失败只输出白名单安全码与记录更新时间，不泄露异常/路径/名字/UUID；
- 心跳独立线程：首次提交失败后恢复、写失败隔离、stop 有界且不在调用线程碰数据库、
  首次心跳与 stop 交错；
- 查询失败/超时返回 unknown/null，绝不补零。

真实 PostgreSQL 用例只接受显式 ``TEST_DATABASE_URL``（库名以 ``_test`` 结尾）；
未提供时整组 skip。
"""
import json
import os
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

os.environ["RAG_DISABLE_DOTENV"] = "1"
os.environ.setdefault("EMBEDDING_BASE_URL", "http://127.0.0.1:9/v1")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_BASE_URL", "http://127.0.0.1:9")

from tests._env import TEST_URL, prepare  # noqa: E402

prepare()
_TEST_URL = TEST_URL

from sqlalchemy import create_engine, delete, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db import session as db_session  # noqa: E402


def _bind_engine() -> None:
    if not _TEST_URL:
        return
    target = make_url(_TEST_URL)
    if db_session.engine.url != target:
        db_session.engine.dispose()
        db_session.engine = create_engine(
            _TEST_URL, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800
        )
        db_session.SessionLocal.configure(bind=db_session.engine)


_bind_engine()

from app.db.base import Base  # noqa: E402
import app.models  # noqa: E402,F401  确保所有表已注册
from app.models import (  # noqa: E402
    Conversation,
    Document,
    DocumentStatus,
    FeedbackRegressionJob,
    KnowledgeBase,
    ProjectSnapshot,
    ProjectSnapshotStatus,
    ProjectSource,
    SummaryJob,
    SummaryJobStatus,
    User,
    WorkerHeartbeat,
)
from app.services import worker_status as ws  # noqa: E402
from tests import _memory_project_test_gate as _gate  # noqa: E402

# 与既有真实 PG 用例共用归属门：回环 + _test + owner comment 全部匹配才建表/清理。
# 两项都缺 -> skip；部分/错误配置由共享门硬失败，绝不仅凭库名 _test 清空数据。
_OWNER_OK = False
_OWNER_REASON = "未配置 TEST_DATABASE_URL 与 TEST_DATABASE_OWNER"
if _TEST_URL or _gate.TARGET_OWNER:
    try:
        _gate.verify(_TEST_URL, _gate.TARGET_OWNER, db_session.engine)
        _OWNER_OK = True
        _OWNER_REASON = ""
    except unittest.SkipTest as exc:  # 共享门的显式跳过（两项都缺）
        _OWNER_REASON = str(exc)

if _OWNER_OK:
    Base.metadata.create_all(bind=db_session.engine)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _CommitFailingSession:
    """真实 Session 代理：读写语句真正执行，仅在事务 COMMIT 阶段失败。

    ``begin()`` 返回的上下文里，``__exit__`` 先对真实会话执行 ``commit()``；
    无论提交成功与否，随后统一 ``rollback()`` 丢弃这次登记，再抛出
    ``RuntimeError`` 模拟提交失败。因此 INSERT 确实到达数据库，失败点就是
    “提交阶段”，而不是更早的 AttributeError。
    """

    def __init__(self, session):
        self._session = session
        self.entered_begin = False
        self.executed = 0

    def __getattr__(self, name):
        attribute = getattr(self._session, name)
        if name == "execute":
            def _execute(*args, **kwargs):
                self.executed += 1
                return attribute(*args, **kwargs)
            return _execute
        return attribute

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._session.close()
        return False

    def begin(self):
        session = self._session
        self.entered_begin = True

        class _Tx:
            def __enter__(self):
                return session

            def __exit__(self, exc_type, exc, tb):
                if exc_type is not None:
                    session.rollback()
                    session.close()
                    return False
                # INSERT 已实际执行；这里回滚并抛出提交阶段错误，模拟 COMMIT 失败。
                session.rollback()
                session.close()
                raise RuntimeError("simulated commit failure")

        return _Tx()


class _FlakyFactory:
    """前 N 次会话的提交阶段失败，之后恢复正常；记录调用线程。"""

    def __init__(self, real_factory, failures: int = 1):
        self._real = real_factory
        self._remaining = failures
        self.failures = 0
        self.calls = 0
        self.call_threads: list[str] = []
        self.proxies: list[_CommitFailingSession] = []
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            self.call_threads.append(threading.current_thread().name)
            should_fail = self._remaining > 0
            if should_fail:
                self._remaining -= 1
                self.failures += 1
        session = self._real()
        if not should_fail:
            return session
        proxy = _CommitFailingSession(session)
        with self._lock:
            self.proxies.append(proxy)
        return proxy


class _BlockingFactory:
    """一旦进入阻塞状态，之后**每次**获取会话都阻塞，直到 release。

    旧版 stop() 会在调用线程同步获取连接/会话，本替身会让那种实现直接超时失败。
    """

    def __init__(self, real_factory, block_after: int = 1):
        self._real = real_factory
        self.release = threading.Event()
        self.started = threading.Event()
        self.block_after = block_after
        self.call_threads: list[str] = []
        self.callers: list[threading.Thread] = []
        self._calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self._calls += 1
            should_block = self._calls > self.block_after
            self.call_threads.append(threading.current_thread().name)
            self.callers.append(threading.current_thread())
        if should_block:
            self.started.set()
            self.release.wait(30)
        return self._real()


class StateAndPayloadTests(unittest.TestCase):
    """纯计算：不连接数据库。"""

    def test_roles_order_is_frozen(self):
        self.assertEqual(
            ws.WORKER_ROLES, ("ingest", "summary", "project", "regression")
        )
        self.assertEqual(ws.STALE_AFTER_SECONDS, 45)

    def test_state_machine(self):
        self.assertEqual(ws._state_for(False, {"alive_count": 5}), "disabled")
        self.assertEqual(ws._state_for(True, {"alive_count": 0, "heartbeat_total": 0}), "not_started")
        self.assertEqual(
            ws._state_for(True, {"alive_count": 2, "heartbeat_total": 2, "unstopped_total": 2}),
            "running",
        )
        self.assertEqual(
            ws._state_for(True, {"alive_count": 0, "heartbeat_total": 1, "unstopped_total": 1}),
            "stale",
        )
        self.assertEqual(
            ws._state_for(True, {"alive_count": 0, "heartbeat_total": 2, "unstopped_total": 0}),
            "stopped",
        )

    def test_failure_codes_are_allowlisted(self):
        self.assertEqual(ws._safe_failure_code("ingest", "任意异常正文 C:\\secret\\a.txt"), "document-failed")
        self.assertEqual(ws._safe_failure_code("summary", "model_error"), "model_error")
        self.assertEqual(ws._safe_failure_code("summary", "C:\\secret\\a.txt"), "failed")
        self.assertEqual(ws._safe_failure_code("project", "parse-error"), "parse-error")
        self.assertEqual(ws._safe_failure_code("project", "../evil/path"), "failed")
        # regression 只接受 R1b 冻结白名单；未知/正文一律收敛。
        self.assertEqual(ws._safe_failure_code("regression", "source_verification_failed"),
                         "source_verification_failed")
        self.assertEqual(ws._safe_failure_code("regression", "model_timeout"), "model_timeout")
        self.assertEqual(ws._safe_failure_code("regression", "问答正文泄漏"), "failed")
        self.assertEqual(ws._safe_failure_code("bogus", "model_error"), "failed")

    def test_degraded_payload_is_unknown_and_never_zeroed(self):
        with patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
             patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"x": "/tmp/x"}):
            payload = ws.build_worker_status({"status": "unavailable", "error": "timeout"})
        self.assertTrue(payload["degraded"])
        self.assertEqual(payload["stale_after_seconds"], 45)
        self.assertEqual([item["role"] for item in payload["roles"]],
                         ["ingest", "summary", "project", "regression"])
        for item in payload["roles"]:
            self.assertEqual(item["state"], "unknown")
            self.assertIsNone(item["alive_count"])
            self.assertIsNone(item["pending"])
            self.assertIsNone(item["processing"])
            self.assertIsNone(item["stale_tasks"])
            self.assertIsNone(item["oldest_pending_age_seconds"])
            self.assertIsNone(item["last_heartbeat_at"])
            self.assertIsNone(item["last_failure"])

    def test_enabled_flags_follow_api_settings(self):
        with patch.object(settings, "CHAT_MEMORY_ENABLED", False), \
             patch.object(settings, "PROJECT_ALLOWED_ROOTS", {}):
            flags = ws._enabled_roles()
        self.assertEqual(
            flags, {"ingest": True, "summary": False, "project": False, "regression": True}
        )
        with patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
             patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"demo": "/srv/demo"}):
            flags = ws._enabled_roles()
        self.assertEqual(
            flags, {"ingest": True, "summary": True, "project": True, "regression": True}
        )


@unittest.skipUnless(_OWNER_OK, "需要显式 TEST_DATABASE_URL + 匹配的 TEST_DATABASE_OWNER")
class HeartbeatReporterDbTests(unittest.TestCase):
    """真实 PostgreSQL：心跳登记/续期/恢复/停止标记。"""

    def setUp(self):
        self.instance_ids: list[uuid.UUID] = []

    def tearDown(self):
        with db_session.SessionLocal() as db:
            if self.instance_ids:
                db.execute(delete(WorkerHeartbeat).where(
                    WorkerHeartbeat.instance_id.in_(self.instance_ids)
                ))
                db.commit()

    def _reporter(self, **kwargs):
        reporter = ws.WorkerHeartbeatReporter("ingest", **kwargs)
        self.instance_ids.append(reporter.instance_id)
        return reporter

    def _wait_row(self, instance_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with db_session.SessionLocal() as db:
                row = db.get(WorkerHeartbeat, instance_id)
                if row is not None:
                    return row
            time.sleep(0.02)
        self.fail("心跳行未在时限内出现")

    def test_registers_and_marks_stopped_on_exit(self):
        reporter = self._reporter(interval_seconds=0.05, join_timeout_seconds=2.0)
        reporter.start()
        row = self._wait_row(reporter.instance_id)
        self.assertEqual(row.role, "ingest")
        self.assertIsNone(row.stopped_at)
        reporter.stop()
        with db_session.SessionLocal() as db:
            row = db.get(WorkerHeartbeat, reporter.instance_id)
            self.assertIsNotNone(row.stopped_at)
            self.assertIsNotNone(row.heartbeat_at)
            self.assertEqual(row.role, "ingest")

    def test_first_commit_failure_is_retried_after_database_recovers(self):
        flaky = _FlakyFactory(db_session.SessionLocal, failures=1)
        reporter = self._reporter(
            session_factory=flaky, interval_seconds=0.05, join_timeout_seconds=2.0
        )
        reporter.start()
        try:
            row = self._wait_row(reporter.instance_id)
            self.assertEqual(row.role, "ingest")
            self.assertGreaterEqual(flaky.failures, 1)
            deadline = time.monotonic() + 5.0
            while not reporter._registered and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(reporter._registered)
            # 失败的那一次确实执行了 SQL 并到达事务退出（不是 db.execute 阶段先抛 AttributeError）。
            self.assertEqual(len(flaky.proxies), 1)
            self.assertTrue(flaky.proxies[0].entered_begin)
            self.assertGreaterEqual(flaky.proxies[0].executed, 1)
            # 心跳线程是唯一调用会话工厂的线程（恢复后的登记也来自它）。
            self.assertTrue(
                all(name.startswith("worker-heartbeat-") for name in flaky.call_threads),
                flaky.call_threads,
            )
        finally:
            reporter.stop()

    def test_heartbeat_write_failure_is_isolated(self):
        class _Failing:
            def __call__(self):
                raise RuntimeError("database unavailable")

        reporter = self._reporter(
            session_factory=_Failing(), interval_seconds=0.02, join_timeout_seconds=0.5
        )
        reporter.start()
        time.sleep(0.1)
        started = time.perf_counter()
        reporter.stop()  # 不得抛出，也不得无限等待
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertFalse(reporter._registered)
        self.assertIsNone(
            self._find_row_or_none(reporter.instance_id)
        )

    def _find_row_or_none(self, instance_id):
        with db_session.SessionLocal() as db:
            return db.get(WorkerHeartbeat, instance_id)

    def test_stop_is_bounded_when_connection_acquisition_blocks(self):
        # 第一次调用（初始心跳）可用；之后 every call 都阻塞，包括 stop 唤醒的最终心跳。
        blocking = _BlockingFactory(db_session.SessionLocal, block_after=1)
        reporter = self._reporter(
            session_factory=blocking, interval_seconds=0.02, join_timeout_seconds=0.2
        )
        reporter.start()
        try:
            self.assertTrue(blocking.started.wait(3.0), "心跳线程未按预期阻塞")
            started = time.perf_counter()
            reporter.stop()
            elapsed = time.perf_counter() - started
            # 调用线程只 set + 有界 join；连接池等待不得把 stop 挂住。
            self.assertLess(elapsed, 1.5)
            # stop 期间调用线程不得自己获取会话/连接（旧实现会在此阻塞或被本替身卡住）。
            for caller in blocking.callers:
                self.assertTrue(
                    caller.name.startswith("worker-heartbeat-"),
                    f"非心跳线程访问了会话工厂: {caller.name}",
                )
        finally:
            blocking.release.set()
            if reporter._thread is not None:
                reporter._thread.join(timeout=3.0)

    def test_stop_only_touches_database_from_heartbeat_thread(self):
        calls: list[str] = []
        lock = threading.Lock()

        def factory():
            with lock:
                calls.append(threading.current_thread().name)
            return db_session.SessionLocal()

        reporter = self._reporter(
            session_factory=factory, interval_seconds=0.02, join_timeout_seconds=2.0
        )
        reporter.start()
        self._wait_row(reporter.instance_id)
        reporter.stop()
        with db_session.SessionLocal() as db:
            self.assertIsNotNone(db.get(WorkerHeartbeat, reporter.instance_id).stopped_at)
        self.assertTrue(calls)
        self.assertTrue(all(name.startswith("worker-heartbeat-") for name in calls), calls)

    def test_first_heartbeat_and_stop_interleave_leaves_stopped_row(self):
        reporter = self._reporter(interval_seconds=10.0, join_timeout_seconds=2.0)
        reporter.start()
        reporter.stop()  # 可能在首次心跳完成前就置位
        if reporter._thread is not None:
            reporter._thread.join(timeout=3.0)
        with db_session.SessionLocal() as db:
            row = db.get(WorkerHeartbeat, reporter.instance_id)
        self.assertIsNotNone(row)
        self.assertIsNotNone(row.stopped_at)
        self.assertEqual(row.role, "ingest")

    def test_stop_before_start_is_noop(self):
        reporter = self._reporter(interval_seconds=0.05)
        reporter.stop()
        self.assertIsNone(self._find_row_or_none(reporter.instance_id))

    def test_start_helper_never_raises(self):
        with patch.object(ws, "WorkerHeartbeatReporter", side_effect=RuntimeError("boom")),              patch.object(ws.logger, "warning", side_effect=OSError("logging broken")):
            # 构造失败 + 日志故障组合也不得逃出 start_worker_heartbeat。
            self.assertIsNone(ws.start_worker_heartbeat("ingest"))
        ws.stop_worker_heartbeat(None)

    def test_thread_start_failure_with_broken_logging_does_not_escape(self):
        with patch.object(ws.threading, "Thread", side_effect=RuntimeError("no threads")),              patch.object(ws.logger, "warning", side_effect=OSError("logging broken")):
            reporter = ws.WorkerHeartbeatReporter("ingest")
            try:
                reporter.start()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"心跳启动失败不应外溢: {type(exc).__name__}")
            started = time.perf_counter()
            try:
                reporter.stop()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"心跳停止失败不应外溢: {type(exc).__name__}")
            self.assertLess(time.perf_counter() - started, 2.0)

    def test_heartbeat_write_failure_with_broken_logging_does_not_escape(self):
        class _Failing:
            def __call__(self):
                raise RuntimeError("database unavailable")

        with patch.object(ws.logger, "warning", side_effect=OSError("logging broken")):
            reporter = ws.WorkerHeartbeatReporter(
                "ingest", session_factory=_Failing(), interval_seconds=0.02,
                join_timeout_seconds=0.5,
            )
            before = threading.active_count()
            reporter._beat_once()  # 直接验证写失败 + 日志失败组合不外溢
            reporter._beat_once(final=True)
        self.assertLessEqual(threading.active_count(), before)

    def test_stop_logging_failure_does_not_escape(self):
        class _Failing:
            def __call__(self):
                raise RuntimeError("database unavailable")

        reporter = ws.WorkerHeartbeatReporter(
            "ingest", session_factory=_Failing(), interval_seconds=0.02,
            join_timeout_seconds=0.2,
        )
        reporter.start()
        with patch.object(ws.logger, "warning", side_effect=OSError("logging broken")):
            reporter._stopped.set()
            try:
                reporter.stop()
            except Exception as exc:  # noqa: BLE001
                self.fail(f"stop 日志故障不应外溢: {type(exc).__name__}")
            if reporter._thread is not None:
                reporter._thread.join(timeout=3.0)


@unittest.skipUnless(_OWNER_OK, "需要显式 TEST_DATABASE_URL + 匹配的 TEST_DATABASE_OWNER")
class WorkerStatusAggregationTests(unittest.TestCase):
    """真实 PostgreSQL：三角色多实例聚合、任务统计与失败安全码。"""

    @classmethod
    def setUpClass(cls):
        with db_session.SessionLocal() as db:
            cls.user = User(
                username=f"ws_user_{uuid.uuid4().hex[:8]}", role="admin", is_active=True,
                password_hash="unused-test-only",
            )
            db.add(cls.user)
            db.flush()
            cls.kb = KnowledgeBase(
                name=f"ws_kb_{uuid.uuid4().hex[:8]}",
                collection_name=f"ws_col_{uuid.uuid4().hex[:8]}",
                owner_id=cls.user.id,
            )
            db.add(cls.kb)
            db.flush()
            cls.conversation = Conversation(user_id=cls.user.id, mode="chat")
            db.add(cls.conversation)
            db.flush()
            # 每个 ProjectSource 必须绑定自己的专用 KB（kb_id unique）。
            cls.project_kb1 = KnowledgeBase(
                name=f"ws_pkb1_{uuid.uuid4().hex[:8]}",
                collection_name=f"ws_pcol1_{uuid.uuid4().hex[:8]}",
                owner_id=cls.user.id,
                kind="project",
            )
            cls.project_kb2 = KnowledgeBase(
                name=f"ws_pkb2_{uuid.uuid4().hex[:8]}",
                collection_name=f"ws_pcol2_{uuid.uuid4().hex[:8]}",
                owner_id=cls.user.id,
                kind="project",
            )
            db.add_all([cls.project_kb1, cls.project_kb2])
            db.flush()
            cls.source = ProjectSource(
                kb_id=cls.project_kb1.id,
                name=f"ws_source_{uuid.uuid4().hex[:8]}",
                root_key="demo",
                relative_dir="",
                root_fingerprint="f" * 64,
            )
            db.add(cls.source)
            cls.source2 = ProjectSource(
                kb_id=cls.project_kb2.id,
                name=f"ws_source_{uuid.uuid4().hex[:8]}",
                root_key="demo",
                relative_dir="",
                root_fingerprint="e" * 64,
            )
            db.add(cls.source2)
            db.commit()
            cls.user_id = cls.user.id
            cls.kb_id = cls.kb.id
            cls.project_kb_ids = [cls.project_kb1.id, cls.project_kb2.id]
            cls.conversation_id = cls.conversation.id
            cls.source_id = cls.source.id
            cls.source2_id = cls.source2.id

    @classmethod
    def tearDownClass(cls):
        with db_session.SessionLocal() as db:
            db.execute(delete(WorkerHeartbeat))
            db.execute(delete(SummaryJob).where(SummaryJob.conversation_id == cls.conversation_id))
            db.execute(delete(ProjectSnapshot).where(
                ProjectSnapshot.source_id.in_([cls.source_id, cls.source2_id])
            ))
            db.execute(delete(Document).where(Document.kb_id == cls.kb_id))
            db.execute(delete(ProjectSource).where(
                ProjectSource.id.in_([cls.source_id, cls.source2_id])
            ))
            db.execute(delete(Conversation).where(Conversation.id == cls.conversation_id))
            db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(
                [cls.kb_id, *cls.project_kb_ids]
            )))
            db.execute(delete(User).where(User.id == cls.user_id))
            db.commit()

    def setUp(self):
        # 断言的是“全局角色队列”，因此必须清空本轮专用库里这四张任务表 + 心跳表；
        # 其它模块（如 worker_shutdown）也会在同一专用库留下任务行，不能只看本夹具的 kb。
        # 仅清理本模块断言的表，不动 users/knowledge_bases 等共享夹具。
        with db_session.SessionLocal() as db:
            db.execute(delete(WorkerHeartbeat))
            db.execute(delete(SummaryJob))
            db.execute(delete(ProjectSnapshot))
            db.execute(delete(Document))
            db.execute(delete(FeedbackRegressionJob))
            db.commit()

    def _heartbeat(self, role, *, age_seconds=0.0, stopped=False, instance_id=None):
        now = _now()
        with db_session.SessionLocal() as db:
            db.add(WorkerHeartbeat(
                instance_id=instance_id or uuid.uuid4(),
                role=role,
                started_at=now - timedelta(seconds=age_seconds + 5),
                heartbeat_at=now - timedelta(seconds=age_seconds),
                stopped_at=now if stopped else None,
            ))
            db.commit()

    def _document(self, status, *, heartbeat=None, created_at=None, lease_id=None):
        with db_session.SessionLocal() as db:
            doc = Document(
                kb_id=self.kb_id, filename=f"ws_test_{uuid.uuid4().hex[:8]}.txt",
                file_ext="txt", file_size=1, stored_path="ws_test.txt", status=status,
                heartbeat_at=heartbeat, lease_id=lease_id,
                **({"created_at": created_at} if created_at else {}),
            )
            db.add(doc)
            db.commit()
            return doc.id

    def _summary_job(self, status, *, heartbeat=None, error_code=None, updated_at=None):
        with db_session.SessionLocal() as db:
            job = SummaryJob(
                conversation_id=self.conversation_id,
                idempotency_key=f"ws-{uuid.uuid4().hex}",
                status=status, heartbeat_at=heartbeat, error_code=error_code,
                **({"updated_at": updated_at} if updated_at else {}),
            )
            db.add(job)
            db.commit()
            return job.id

    def _project_snapshot(self, status, *, heartbeat=None, error_code=None, updated_at=None,
                          source_id=None):
        with db_session.SessionLocal() as db:
            snap = ProjectSnapshot(
                source_id=source_id or self.source_id, status=status, heartbeat_at=heartbeat,
                error_code=error_code,
                **({"updated_at": updated_at} if updated_at else {}),
            )
            db.add(snap)
            db.commit()
            return snap.id

    async def _collect(self):
        stats = await ws.collect_worker_status()
        self.assertEqual(stats.get("status"), "ok", stats)
        return ws.build_worker_status(stats)

    def test_four_roles_multi_instance_states_and_queue_stats(self):
        now = _now()
        # 各角色任务停滞阈值沿用各自既有租约语义，不是进程心跳的 45 秒。
        old = now - timedelta(seconds=settings.INGEST_STALE_SECONDS + 30)
        old_summary = now - timedelta(seconds=settings.MEMORY_JOB_LEASE_SECONDS + 30)
        old_project = now - timedelta(seconds=settings.PROJECT_STALE_SECONDS + 30)
        # ingest：两个新鲜实例 -> running
        self._heartbeat("ingest", age_seconds=1)
        self._heartbeat("ingest", age_seconds=2)
        # summary：一个过期未停止实例 -> stale
        self._heartbeat("summary", age_seconds=ws.STALE_AFTER_SECONDS + 30)
        # project：两个正常停止实例 -> stopped
        self._heartbeat("project", age_seconds=1, stopped=True)
        self._heartbeat("project", age_seconds=2, stopped=True)

        document_pending = self._document(DocumentStatus.PENDING.value, created_at=now - timedelta(seconds=120))
        self._document(DocumentStatus.PENDING.value, created_at=now - timedelta(seconds=10))
        # 有效 processing：新鲜心跳 + 合法租约（不应计入 stale）。
        self._document(DocumentStatus.PROCESSING.value, heartbeat=now, lease_id=uuid.uuid4())
        # 心跳超时。
        self._document(DocumentStatus.PROCESSING.value, heartbeat=old, lease_id=uuid.uuid4())
        # 新鲜心跳但没有租约：worker.recover_stale 会恢复它，必须计入 stale。
        self._document(DocumentStatus.PROCESSING.value, heartbeat=now)
        future = now + timedelta(seconds=30)
        self._document(DocumentStatus.FAILED.value)

        self._summary_job(SummaryJobStatus.PENDING.value)
        self._summary_job(SummaryJobStatus.PROCESSING.value, heartbeat=old_summary)
        self._summary_job(
            SummaryJobStatus.FAILED.value, error_code="model_error",
            updated_at=future,
        )
        # 每源至多一个 pending/processing（部分唯一索引），不同状态用不同源。
        self._project_snapshot(ProjectSnapshotStatus.PENDING.value)
        self._project_snapshot(ProjectSnapshotStatus.PROCESSING.value, heartbeat=old_project,
                               source_id=self.source2_id)
        self._project_snapshot(
            ProjectSnapshotStatus.FAILED.value,
            error_code="C:\\secret\\project\\file.py",
            updated_at=future,
        )

        with patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
             patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"demo": "/tmp/demo"}):
            payload = self._anyio_run(self._collect())

        roles = {item["role"]: item for item in payload["roles"]}
        self.assertEqual(list(roles), ["ingest", "summary", "project", "regression"])
        self.assertFalse(payload["degraded"])
        regression = roles["regression"]
        self.assertTrue(regression["enabled"])
        self.assertEqual(regression["state"], "not_started")
        self.assertEqual((regression["pending"], regression["processing"], regression["stale_tasks"]), (0, 0, 0))

        ingest = roles["ingest"]
        self.assertTrue(ingest["enabled"])
        self.assertEqual(ingest["state"], "running")
        self.assertEqual(ingest["alive_count"], 2)
        self.assertIsNotNone(ingest["last_heartbeat_at"])
        self.assertEqual(ingest["pending"], 2)
        self.assertEqual(ingest["processing"], 3)
        self.assertEqual(ingest["stale_tasks"], 2)
        self.assertGreaterEqual(ingest["oldest_pending_age_seconds"], 100)

        summary = roles["summary"]
        self.assertEqual(summary["state"], "stale")
        self.assertEqual(summary["alive_count"], 0)
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["processing"], 1)
        self.assertEqual(summary["stale_tasks"], 1)
        self.assertEqual(summary["last_failure"]["code"], "model_error")

        project = roles["project"]
        self.assertEqual(project["state"], "stopped")
        self.assertEqual(project["alive_count"], 0)
        self.assertEqual(project["pending"], 1)
        self.assertEqual(project["processing"], 1)
        self.assertEqual(project["stale_tasks"], 1)
        self.assertEqual(project["last_failure"]["code"], "failed")

        blob = json.dumps(payload, default=str)
        # 原始异常/路径/文件名/UUID 一律不得出现在输出中
        self.assertNotIn("secret", blob)
        self.assertNotIn("ws_test_", blob)
        self.assertNotIn(str(document_pending), blob)
        self.assertNotIn(str(self.user_id), blob)

    def _anyio_run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_empty_queue_reports_zero_and_null_age(self):
        payload = self._anyio_run(self._collect())
        roles = {item["role"]: item for item in payload["roles"]}
        self.assertEqual(roles["ingest"]["state"], "not_started")
        self.assertEqual(roles["ingest"]["pending"], 0)
        self.assertEqual(roles["ingest"]["processing"], 0)
        self.assertEqual(roles["ingest"]["stale_tasks"], 0)
        self.assertIsNone(roles["ingest"]["oldest_pending_age_seconds"])
        self.assertEqual(roles["ingest"]["alive_count"], 0)
        self.assertIsNone(roles["ingest"]["last_heartbeat_at"])
        # regression 没有心跳行时是 not_started，但队列是真实查询到的 0，不是未知。
        self.assertTrue(roles["regression"]["enabled"])
        self.assertEqual(roles["regression"]["state"], "not_started")
        self.assertEqual(roles["regression"]["pending"], 0)
        self.assertEqual(roles["regression"]["processing"], 0)
        self.assertEqual(roles["regression"]["stale_tasks"], 0)
        self.assertIsNone(roles["regression"]["oldest_pending_age_seconds"])
        self.assertIsNone(roles["regression"]["last_failure"])

    def test_disabled_role_still_shows_observed_heartbeat(self):
        self._heartbeat("project", age_seconds=1)
        with patch.object(settings, "PROJECT_ALLOWED_ROOTS", {}), \
             patch.object(settings, "CHAT_MEMORY_ENABLED", False):
            payload = self._anyio_run(self._collect())
        project = {item["role"]: item for item in payload["roles"]}["project"]
        self.assertFalse(project["enabled"])
        self.assertEqual(project["state"], "disabled")
        self.assertEqual(project["alive_count"], 1)
        self.assertIsNotNone(project["last_heartbeat_at"])

    def test_unknown_role_heartbeat_rows_are_ignored(self):
        self._heartbeat("bogus", age_seconds=1)
        payload = self._anyio_run(self._collect())
        self.assertEqual([item["role"] for item in payload["roles"]],
                         ["ingest", "summary", "project", "regression"])
        self.assertEqual(payload["roles"][0]["alive_count"], 0)

    def test_query_failure_and_timeout_are_bounded_and_not_zeroed(self):
        from sqlalchemy import create_engine

        broken = create_engine(
            "postgresql+psycopg://ragtest:bad@127.0.0.1:9/unreachable",
            connect_args={"connect_timeout": 1},
        )
        with patch.object(db_session, "engine", broken), \
             patch.object(settings, "OPERATIONS_DB_TIMEOUT_SECONDS", 1.0), \
             patch.object(settings, "CHAT_MEMORY_ENABLED", True), \
             patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"demo": "/tmp/demo"}):
            stats = self._anyio_run(ws.collect_worker_status())
            # 启用状态按“当前 API 配置”判定，因此必须在同一 patch 上下文内构建。
            payload = ws.build_worker_status(stats)
        broken.dispose()
        self.assertEqual(stats["status"], "unavailable")
        self.assertTrue(payload["degraded"])
        for item in payload["roles"]:
            self.assertTrue(item["enabled"])
            self.assertEqual(item["state"], "unknown")
            self.assertIsNone(item["alive_count"])
            self.assertIsNone(item["pending"])

    def test_slow_query_hits_anyio_timeout(self):
        def slow(timeout_seconds):
            time.sleep(5)
            return {"status": "ok", "roles": {}}

        started = time.perf_counter()
        with patch.object(ws, "_collect_worker_stats", side_effect=slow), \
             patch.object(settings, "OPERATIONS_DB_TIMEOUT_SECONDS", 0.2):
            stats = self._anyio_run(ws.collect_worker_status())
        self.assertEqual(stats, {"status": "unavailable", "error": "timeout"})
        self.assertLess(time.perf_counter() - started, 4.0)


if __name__ == "__main__":
    unittest.main()
