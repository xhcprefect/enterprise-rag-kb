"""Ingest worker database recovery: pure loop tests and owner-gated PostgreSQL tests.

No engine rebinding, service/model calls, or timing sleeps. Database fixture writes
always follow tests._memory_project_test_gate.verify against the actual app engine.
"""
import logging
import os
import threading
import time
import traceback
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Standalone pure tests must not load the business .env or connect to any database.
os.environ["RAG_DISABLE_DOTENV"] = "1"

import psycopg
from sqlalchemy import text, update
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
    SQLAlchemyError,
    TimeoutError,
)

from tests import _memory_project_test_gate as database_gate
from app.core.config import settings
from app.db import session as db_session
from app.db.init_db import init_db
from app.models import Document, DocumentStatus, KnowledgeBase, User
import worker


_PRIVATE_TEXT = "private_exception postgresql://private:private_password@private/ SELECT private_sql"


def database_error(sqlstate="08006", *, invalidated=False):
    original = psycopg.errors.lookup(sqlstate)(_PRIVATE_TEXT)
    return DBAPIError.instance(
        "SELECT private_sql", {"password": "private_password"}, original,
        psycopg.Error, connection_invalidated=invalidated,
    )


@contextmanager
def isolated_worker():
    """No database or heartbeat thread can escape a pure supervisor test."""
    with ExitStack() as stack:
        for name in ("setup_logging", "init_db", "recover_stale", "cleanup_expired",
                     "ingest_document_task", "_stop_heartbeat"):
            stack.enter_context(patch.object(worker, name))
        stack.enter_context(patch.object(worker.signal, "signal"))
        stack.enter_context(patch.object(worker, "_start_heartbeat", return_value="heartbeat"))
        stack.enter_context(patch.object(worker, "SessionLocal"))
        stack.enter_context(patch.object(worker.time, "monotonic", return_value=1.0))
        yield


class WorkerDatabaseClassificationTests(unittest.TestCase):
    def test_explicit_transient_sqlstates_on_real_psycopg_errors(self):
        for state in ("08000", "08001", "08003", "08004", "08006", "08007", "08P01",
                      "40001", "40P01", "53300", "55P03", "57P01", "57P02", "57P03"):
            with self.subTest(sqlstate=state):
                self.assertTrue(worker._is_recoverable_database_error(database_error(state)))

    def test_permanent_sqlstates_remain_permanent_even_if_connection_is_invalidated(self):
        for state in ("28000", "28P01", "23505", "23503", "3D000", "3F000", "42501",
                      "42601", "42P01", "42703", "53000", "53100", "53200", "57014", "40003"):
            for invalidated in (False, True):
                with self.subTest(sqlstate=state, invalidated=invalidated):
                    self.assertFalse(worker._is_recoverable_database_error(
                        database_error(state, invalidated=invalidated),
                    ))

    def test_pool_timeout_disconnect_and_connection_invalidated_are_retryable(self):
        for error in (
            TimeoutError(_PRIVATE_TEXT),
            DisconnectionError(_PRIVATE_TEXT),
            DBAPIError(None, None, Exception(_PRIVATE_TEXT), connection_invalidated=True),
            InterfaceError(None, None, psycopg.InterfaceError(_PRIVATE_TEXT), connection_invalidated=True),
            OperationalError(None, None, psycopg.errors.ConnectionTimeout(_PRIVATE_TEXT)),
        ):
            with self.subTest(error_type=type(error).__name__):
                self.assertTrue(worker._is_recoverable_database_error(error))

    def test_unknown_connect_diagnostics_fail_closed_even_if_invalidated(self):
        for needs_password, used_password in ((True, False), (False, True), (False, False)):
            original = psycopg.OperationalError(
                _PRIVATE_TEXT,
                pgconn=SimpleNamespace(needs_password=needs_password, used_password=used_password),
            )
            for invalidated in (False, True):
                with self.subTest(needs_password=needs_password, used_password=used_password,
                                  invalidated=invalidated):
                    error = OperationalError(None, None, original, connection_invalidated=invalidated)
                    self.assertFalse(worker._is_recoverable_database_error(error))

    def test_text_names_and_broad_operational_errors_do_not_enable_retry(self):
        lookalike = type("OperationalError", (RuntimeError,), {})
        for error in (
            RuntimeError("connection refused 08006 40001"),
            lookalike("database connection lost"),
            psycopg.OperationalError("connection refused"),
            OperationalError(None, None, psycopg.OperationalError("connection refused")),
            OperationalError(None, None, OSError("connection refused")),
            OperationalError(None, None, SimpleNamespace(sqlstate="08\nprivate")),
            SQLAlchemyError("connection lost"),
            ProgrammingError(None, None, psycopg.errors.ConnectionFailure(_PRIVATE_TEXT),
                             connection_invalidated=True),
            IntegrityError(None, None, psycopg.errors.SerializationFailure(_PRIVATE_TEXT)),
        ):
            with self.subTest(error_type=type(error).__name__):
                self.assertFalse(worker._is_recoverable_database_error(error))


class WorkerDatabaseLoopTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        patcher = patch.object(worker, "stop_event", self.stop)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_persistent_failure_uses_one_capped_wait_per_attempt(self):
        operation = Mock(side_effect=database_error())
        waits = []

        def wait(delay):
            waits.append(delay)
            if len(waits) == 8:
                self.stop.set()
                return True
            return False

        with patch.object(self.stop, "wait", side_effect=wait), \
             patch.object(worker.time, "sleep", side_effect=AssertionError("must use Event.wait")):
            result = worker._run_database_operation("claim", operation, worker._DatabaseRetryBackoff())
        self.assertIs(result, worker._STOPPED)
        self.assertEqual(operation.call_count, 8)
        self.assertEqual(waits, [2, 4, 8, 16, 30, 30, 30, 30])

    def test_connection_timeout_still_waits_interruptibly_with_safe_logs(self):
        error = OperationalError(None, None, psycopg.errors.ConnectionTimeout(_PRIVATE_TEXT))
        operation = Mock(side_effect=error)
        with self.assertLogs(worker.logger, level=logging.WARNING) as captured, \
             patch.object(self.stop, "wait", side_effect=lambda _: self.stop.set() or True) as wait:
            result = worker._run_database_operation("initialize", operation, worker._DatabaseRetryBackoff())
        self.assertIs(result, worker._STOPPED)
        operation.assert_called_once()
        wait.assert_called_once_with(2)
        self.assertNotIn(_PRIVATE_TEXT, " ".join(captured.output))
        self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_ambiguous_connect_failure_never_waits_and_logs_are_safe(self):
        original = psycopg.OperationalError(
            _PRIVATE_TEXT, pgconn=SimpleNamespace(needs_password=False, used_password=True),
        )
        operation = Mock(side_effect=OperationalError(None, None, original))
        with self.assertLogs(worker.logger, level=logging.ERROR) as captured, \
             patch.object(self.stop, "wait") as wait:
            with self.assertRaisesRegex(RuntimeError, "sqlstate=none"):
                worker._run_database_operation("initialize", operation, worker._DatabaseRetryBackoff())
        operation.assert_called_once()
        wait.assert_not_called()
        self.assertNotIn(_PRIVATE_TEXT, " ".join(captured.output))
        self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_successful_claim_including_empty_queue_resets_backoff(self):
        for claimed in (None, (uuid.uuid4(), uuid.uuid4())):
            with self.subTest(claimed=claimed):
                backoff = worker._DatabaseRetryBackoff()
                operation = Mock(side_effect=[database_error(), database_error(), claimed])
                with patch.object(self.stop, "wait", return_value=False) as wait:
                    self.assertEqual(worker._run_database_operation("claim", operation, backoff), claimed)
                self.assertEqual([call.args[0] for call in wait.call_args_list], [2, 4])
                self.assertEqual(backoff.failures, 0)
                with patch.object(self.stop, "wait", side_effect=lambda _: self.stop.set() or True) as wait:
                    self.assertIs(worker._run_database_operation(
                        "claim", Mock(side_effect=database_error()), backoff,
                    ), worker._STOPPED)
                wait.assert_called_once_with(2)
                self.stop.clear()

    def test_real_event_interrupts_database_wait_and_no_claim_follows(self):
        entered_wait = threading.Event()
        original_wait = self.stop.wait
        calls, results, errors = [], [], []

        def wait(delay):
            entered_wait.set()
            return original_wait(delay)

        def claim():
            calls.append(1)
            raise database_error()

        def run():
            try:
                results.append(worker._run_database_operation("claim", claim, worker._DatabaseRetryBackoff()))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(self.stop, "wait", side_effect=wait):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered_wait.wait(5))
                started = time.perf_counter()
                self.stop.set()
                thread.join(1)
                self.assertFalse(thread.is_alive())
                self.assertLess(time.perf_counter() - started, 1)
            finally:
                self.stop.set()
                thread.join(1)
        self.assertEqual(errors, [])
        self.assertEqual(results, [worker._STOPPED])
        self.assertEqual(calls, [1])

    def test_preexisting_stop_never_opens_session_or_initializes(self):
        self.stop.set()
        with isolated_worker():
            self.assertEqual(worker.main([]), 0)
            worker.init_db.assert_not_called()
            worker.SessionLocal.assert_not_called()
            worker._stop_heartbeat.assert_called_once_with("heartbeat")

    def test_stop_in_initialize_backoff_has_signals_and_heartbeat_cleanup(self):
        with isolated_worker(), patch.object(worker, "init_db", side_effect=database_error()) as initialize, \
             patch.object(self.stop, "wait", side_effect=lambda _: self.stop.set() or True):
            self.assertEqual(worker.main([]), 0)
            initialize.assert_called_once_with(create_admin=False)
            self.assertEqual(worker.signal.signal.call_count, 2)
            worker.SessionLocal.assert_not_called()
            worker._stop_heartbeat.assert_called_once_with("heartbeat")

    def test_permanent_errors_in_initialize_recovery_and_claim_exit_immediately(self):
        for target, stage in (("init_db", "initialize"), ("recover_stale", "startup_recovery"),
                              ("claim_one", "claim")):
            with self.subTest(stage=stage), isolated_worker(), \
                 patch.object(worker, target, side_effect=database_error("28P01")), \
                 patch.object(self.stop, "wait") as wait:
                with self.assertRaisesRegex(RuntimeError, "stage=" + stage):
                    worker.main([])
                wait.assert_not_called()
                worker._stop_heartbeat.assert_called_once_with("heartbeat")

    def test_runtime_errors_keep_identity_and_heartbeat_cleanup(self):
        for target in ("init_db", "recover_stale", "claim_one"):
            boom = RuntimeError("unexpected programming failure")
            with self.subTest(target=target), isolated_worker(), \
                 patch.object(worker, target, side_effect=boom), patch.object(self.stop, "wait") as wait:
                with self.assertRaises(RuntimeError) as raised:
                    worker.main([])
                self.assertIs(raised.exception, boom)
                wait.assert_not_called()
                worker._stop_heartbeat.assert_called_once_with("heartbeat")

    def test_retry_and_permanent_logs_and_traceback_do_not_include_private_text(self):
        attempts = Mock(side_effect=[database_error(), database_error("28P01")])
        with self.assertLogs(worker.logger, level=logging.WARNING) as captured, \
             patch.object(self.stop, "wait", return_value=False):
            try:
                worker._run_database_operation("claim", attempts, worker._DatabaseRetryBackoff())
            except RuntimeError:
                failure = traceback.format_exc()
            else:
                self.fail("permanent error must exit")
        output = " ".join(captured.output) + failure
        for forbidden in ("private_exception", "private_password", "postgresql://", "SELECT private_sql"):
            self.assertNotIn(forbidden, output)
        self.assertIn("stage=claim", output)
        self.assertIn("sqlstate=28P01", output)
        self.assertTrue(all(record.exc_info is None for record in captured.records))

    def test_startup_scan_and_claim_always_close_session_before_wait(self):
        for target, stage, operation in (
            ("recover_stale", "startup_recovery", worker._recover_stale_in_new_session),
            ("claim_one", "claim", worker._claim_in_new_session),
        ):
            sessions, closed = [], []

            @contextmanager
            def session_factory():
                session = object()
                sessions.append(session)
                try:
                    yield session
                finally:
                    closed.append(session)

            def wait(_):
                self.assertEqual(sessions, closed)
                return False

            with self.subTest(stage=stage), patch.object(worker, "SessionLocal", side_effect=session_factory), \
                 patch.object(worker, target, side_effect=[database_error(), None]), \
                 patch.object(self.stop, "wait", side_effect=wait):
                self.assertIsNone(worker._run_database_operation(stage, operation, worker._DatabaseRetryBackoff()))
            self.assertEqual(len(sessions), 2)
            self.assertIsNot(sessions[0], sessions[1])
            self.assertEqual(sessions, closed)

    def test_periodic_recovery_retry_does_not_repeat_upload_cleanup(self):
        with isolated_worker(), patch.object(worker.time, "monotonic", return_value=601.0), \
             patch.object(worker, "recover_stale", side_effect=[0, database_error(), 0]), \
             patch.object(worker, "claim_one", return_value=None), \
             patch.object(self.stop, "wait", side_effect=[False, True]) as wait:
            self.assertEqual(worker.main([]), 0)
            self.assertEqual(worker.recover_stale.call_count, 3)
            worker.cleanup_expired.assert_called_once()
            self.assertEqual([call.args[0] for call in wait.call_args_list], [2, worker.IDLE_POLL_SECONDS])
            worker._stop_heartbeat.assert_called_once_with("heartbeat")


def assert_owned_database():
    return database_gate.verify(
        os.environ.get("TEST_DATABASE_URL", ""),
        os.environ.get("TEST_DATABASE_OWNER", "") or os.environ.get("TEST_EXPECTED_OWNER", ""),
        db_session.engine,
    )


class WorkerDatabaseConnectPostgresTests(unittest.TestCase):
    """Read-only connection regressions; run only after verifying the owned app DB."""

    def setUp(self):
        assert_owned_database()
        self.stop = threading.Event()
        patcher = patch.object(worker, "stop_event", self.stop)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_real_wrong_password_connect_exits_without_retry_and_never_logs_credentials(self):
        wrong_password = "intentionally-wrong-isolated-test-only"
        self.assertNotEqual(db_session.engine.url.password, wrong_password)
        connect_url = db_session.engine.url.set(password=wrong_password)
        args, kwargs = db_session.engine.dialect.create_connect_args(connect_url)
        kwargs["connect_timeout"] = 2
        try:
            connection = psycopg.connect(*args, **kwargs)
        except psycopg.OperationalError as original:
            error = DBAPIError.instance(None, None, original, psycopg.Error)
            # Actual connect() diagnostics, not a constructed InvalidPassword.
            self.assertFalse(worker._is_recoverable_database_error(error))
            operation = Mock(side_effect=error)
            with self.assertLogs(worker.logger, level=logging.ERROR) as captured, \
                 patch.object(self.stop, "wait") as wait:
                try:
                    worker._run_database_operation("initialize", operation, worker._DatabaseRetryBackoff())
                except RuntimeError:
                    failure = traceback.format_exc()
                else:
                    self.fail("wrong-password connect must fail closed")
            operation.assert_called_once()
            wait.assert_not_called()
            output = " ".join(captured.output) + failure
            for secret in (wrong_password, db_session.engine.url.password):
                if secret:
                    self.assertNotIn(secret, output)
            self.assertNotIn("postgresql://", output)
            self.assertTrue(all(record.exc_info is None for record in captured.records))
        else:
            connection.close()
            self.fail("owned test server must reject the intentionally wrong password")

    def test_real_unavailable_endpoint_uses_only_structured_timeout_or_fails_closed(self):
        # A real libpq connection attempt, no socket probes or server changes.
        # Port 0 cannot be a PostgreSQL listening endpoint; libpq may report a
        # typed timeout or an ambiguous OperationalError depending on platform.
        try:
            connection = psycopg.connect(host="127.0.0.1", hostaddr="127.0.0.1", port=0,
                                         dbname="unused_test", user="unused_test", connect_timeout=2)
        except psycopg.OperationalError as original:
            error = DBAPIError.instance(None, None, original, psycopg.Error)
            expected_retry = isinstance(original, psycopg.errors.ConnectionTimeout)
            self.assertEqual(worker._is_recoverable_database_error(error), expected_retry)
            operation = Mock(side_effect=error)
            with self.assertLogs(worker.logger, level=logging.WARNING) as captured, \
                 patch.object(self.stop, "wait", side_effect=lambda _: self.stop.set() or True) as wait:
                if expected_retry:
                    self.assertIs(worker._run_database_operation(
                        "initialize", operation, worker._DatabaseRetryBackoff(),
                    ), worker._STOPPED)
                    wait.assert_called_once_with(2)
                else:
                    with self.assertRaises(RuntimeError):
                        worker._run_database_operation("initialize", operation, worker._DatabaseRetryBackoff())
                    wait.assert_not_called()
            operation.assert_called_once()
            output = " ".join(captured.output)
            self.assertNotIn("127.0.0.1", output)
            self.assertNotIn("unused_test", output)
            self.assertTrue(all(record.exc_info is None for record in captured.records))
        else:
            connection.close()
            self.fail("port 0 must not provide a PostgreSQL connection")


class WorkerDatabaseRecoveryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_owned_database()  # Missing both skips; all bad/partial configs fail closed.
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        db_session.engine.dispose()

    def setUp(self):
        assert_owned_database()  # Required immediately before every destructive fixture operation.
        with db_session.engine.begin() as conn:
            conn.execute(text("TRUNCATE documents, knowledge_bases, users RESTART IDENTITY CASCADE"))
        self.stop = threading.Event()
        patcher = patch.object(worker, "stop_event", self.stop)
        patcher.start()
        self.addCleanup(patcher.stop)
        with db_session.SessionLocal() as db:
            user = User(username="worker-recovery", role="admin", is_active=True, password_hash="unused-test-only")
            db.add(user)
            db.flush()
            kb = KnowledgeBase(name="worker-recovery", collection_name="worker_recovery_" + uuid.uuid4().hex,
                               owner_id=user.id, is_public=True)
            db.add(kb)
            db.commit()
            self.kb_id = kb.id

    def make_document(self, **values):
        with db_session.SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename="recovery.txt", file_ext=".txt", file_size=1,
                           stored_path="/not-read-by-mocked-worker/recovery.txt", **values)
            db.add(doc)
            db.commit()
            return doc.id

    def document_state(self, doc_id):
        with db_session.SessionLocal() as db:
            doc = db.get(Document, doc_id)
            return {
                "status": doc.status, "attempts": doc.ingest_attempts, "next": doc.next_attempt_at,
                "lease": doc.lease_id, "code": doc.last_error_code, "heartbeat": doc.heartbeat_at,
            }

    def test_startup_init_scan_and_claim_recover_then_real_pending_document_completes(self):
        pending_id = self.make_document(
            ingest_attempts=1, last_error_code="network",
            next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        )
        stale_id = self.make_document(
            status=DocumentStatus.PROCESSING.value, lease_id=uuid.uuid4(),
            heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS + 5),
            ingest_attempts=2, last_error_code="timeout",
            created_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        original_recover, original_claim = worker.recover_stale, worker.claim_one
        calls = {"init": 0, "recover": 0, "claim": 0, "task": 0}
        waits, sessions, closed = [], [], []

        @contextmanager
        def session_factory():
            with db_session.SessionLocal() as db:
                sessions.append(db)
                try:
                    yield db
                finally:
                    closed.append(db)

        def fail_once(key, original):
            def invoke(*args, **kwargs):
                calls[key] += 1
                if calls[key] == 1:
                    raise database_error()
                return original(*args, **kwargs)
            return invoke

        def finish_task(document_id, lease_id, *, stop_event=None):
            calls["task"] += 1
            with db_session.SessionLocal() as db:
                doc = db.get(Document, uuid.UUID(document_id))
                self.assertEqual(doc.id, pending_id)
                self.assertEqual(doc.lease_id, lease_id)
                self.assertEqual(doc.status, DocumentStatus.PROCESSING.value)
                self.assertEqual(doc.ingest_attempts, 1)
                doc.status = DocumentStatus.READY.value
                doc.lease_id = doc.heartbeat_at = None
                doc.ingest_attempts = 0
                doc.last_error_code = None
                db.commit()
            stop_event.set()

        def wait(delay):
            self.assertEqual(sessions, closed)
            waits.append(delay)
            return False

        with patch.object(worker, "setup_logging"), patch.object(worker.signal, "signal"), \
             patch.object(worker, "_start_heartbeat", return_value="heartbeat"), \
             patch.object(worker, "_stop_heartbeat") as stop, \
             patch.object(worker, "init_db", side_effect=fail_once("init", init_db)), \
             patch.object(worker, "recover_stale", side_effect=fail_once("recover", original_recover)), \
             patch.object(worker, "claim_one", side_effect=fail_once("claim", original_claim)), \
             patch.object(worker, "ingest_document_task", side_effect=finish_task), \
             patch.object(worker, "SessionLocal", side_effect=session_factory), \
             patch.object(worker.time, "monotonic", return_value=1.0), \
             patch.object(self.stop, "wait", side_effect=wait):
            self.assertEqual(worker.main([]), 0)
        stop.assert_called_once_with("heartbeat")
        self.assertEqual(calls, {"init": 2, "recover": 2, "claim": 2, "task": 1})
        self.assertEqual(waits, [2, 2, 2])
        self.assertEqual(len(sessions), 4)
        self.assertEqual(len({id(session) for session in sessions}), 4)
        self.assertEqual(sessions, closed)
        self.assertEqual(self.document_state(pending_id)["status"], DocumentStatus.READY.value)
        stale = self.document_state(stale_id)
        self.assertEqual((stale["status"], stale["attempts"], stale["code"]), ("pending", 2, "timeout"))

    def test_real_server_transaction_error_retries_new_session_without_changing_budget_or_schedule(self):
        retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        doc_id = self.make_document(ingest_attempts=2, next_attempt_at=retry_at, last_error_code="network")
        original_claim = worker.claim_one
        sessions, waits = [], []

        def transient_transaction(db):
            sessions.append(db)
            if len(sessions) == 1:
                db.execute(update(Document).where(Document.id == doc_id).values(ingest_attempts=99, next_attempt_at=None))
                db.execute(text("DO $$ BEGIN RAISE EXCEPTION 'synthetic serialization failure' USING ERRCODE = '40001'; END $$"))
            return original_claim(db)

        with patch.object(worker, "claim_one", side_effect=transient_transaction), \
             patch.object(self.stop, "wait", side_effect=lambda delay: waits.append(delay) or False):
            result = worker._run_database_operation("claim", worker._claim_in_new_session, worker._DatabaseRetryBackoff())
        self.assertIsNone(result)
        self.assertEqual(len(sessions), 2)
        self.assertIsNot(sessions[0], sessions[1])
        self.assertEqual(waits, [2])
        state = self.document_state(doc_id)
        self.assertEqual((state["status"], state["attempts"], state["next"], state["lease"], state["code"]),
                         ("pending", 2, retry_at, None, "network"))

    def test_unknown_commit_ack_does_not_replay_old_claim_and_requires_stale_new_lease(self):
        due = datetime.now(timezone.utc) - timedelta(seconds=1)
        doc_id = self.make_document(ingest_attempts=1, next_attempt_at=due, last_error_code="network")
        original_claim = worker.claim_one
        claimed, waits = [], []

        def commit_then_lose_ack(db):
            real_commit = db.commit

            def lost_ack():
                real_commit()  # Persist the lease, then simulate a lost commit acknowledgement.
                raise database_error()

            if not claimed:
                with patch.object(db, "commit", side_effect=lost_ack):
                    try:
                        return original_claim(db)
                    finally:
                        claimed.append("ack-lost")
            result = original_claim(db)
            claimed.append(result)
            return result

        with patch.object(worker, "claim_one", side_effect=commit_then_lose_ack), \
             patch.object(self.stop, "wait", side_effect=lambda delay: waits.append(delay) or False):
            result = worker._run_database_operation("claim", worker._claim_in_new_session, worker._DatabaseRetryBackoff())
        self.assertIsNone(result)
        self.assertEqual(claimed, ["ack-lost", None])
        self.assertEqual(waits, [2])
        state = self.document_state(doc_id)
        self.assertEqual((state["status"], state["attempts"], state["code"], state["next"]),
                         ("processing", 1, "network", None))
        old_lease = state["lease"]
        self.assertIsNotNone(old_lease)
        with db_session.SessionLocal() as db:
            self.assertEqual(worker.recover_stale(db), 0)
            self.assertIsNone(worker.claim_one(db))
            db.execute(update(Document).where(Document.id == doc_id).values(
                heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS + 5),
            ))
            db.commit()
            self.assertEqual(worker.recover_stale(db), 1)
            new_claim = worker.claim_one(db)
        self.assertEqual(new_claim[0], doc_id)
        self.assertNotEqual(new_claim[1], old_lease)
        with db_session.SessionLocal() as db:
            worker._release(db, doc_id, old_lease)
        fenced = self.document_state(doc_id)
        self.assertEqual((fenced["status"], fenced["attempts"], fenced["code"], fenced["lease"]),
                         ("processing", 1, "network", new_claim[1]))


if __name__ == "__main__":
    unittest.main()
