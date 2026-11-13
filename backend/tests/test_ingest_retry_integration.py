"""真实 PostgreSQL/pgvector 入库重试；所有写入先通过实际 app engine owner gate。

运行器必须在导入 app 前注入独占 backend 环境并禁用 dotenv。无配置显式跳过，
部分/不匹配配置硬失败；不重绑 engine，不请求真实模型，不靠长 sleep 制造竞态。
"""
import os
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from langchain_core.embeddings import Embeddings
from sqlalchemy import select, text, update

from tests._memory_project_test_gate import verify
from app.core.config import settings
from app.db import session as db_session
from app.db.init_db import init_db
from app.models import Document, DocumentChunk, KnowledgeBase, User
from app.schemas.knowledge import DocumentRead
from app.services import ingest, vector_store
from app.services.cloud_models import CloudModelError, _format_error
import worker


def assert_owned():
    return verify(os.environ.get("TEST_DATABASE_URL", ""),
                  os.environ.get("TEST_DATABASE_OWNER", "") or os.environ.get("TEST_EXPECTED_OWNER", ""),
                  db_session.engine)


class ControlledEmbeddings(Embeddings):
    def __init__(self):
        self.seen = []
        self.fail_calls = set()
        self.entered = None
        self.release = None
        self.stop = None
        self.failure = lambda: ConnectionError("private upstream body must not persist")

    def embed_documents(self, texts):
        self.seen.append(list(texts))
        if self.entered is not None:
            self.entered.set()
            if not self.release.wait(10):
                raise AssertionError("test did not release embedding request")
        if self.stop is not None:
            self.stop.set()
        if len(self.seen) in self.fail_calls:
            raise self.failure()
        return [[1.0] + [0.0] * 1023 for _ in texts]

    def embed_query(self, text):
        raise AssertionError("ingest must not query embeddings")


class RetryPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert_owned()
        init_db(create_admin=False)

    def setUp(self):
        assert_owned()
        vector_store._store.cache_clear()
        with db_session.engine.begin() as conn:
            conn.execute(text("TRUNCATE documents, knowledge_bases, users RESTART IDENTITY CASCADE"))
            if conn.scalar(text("SELECT to_regclass('langchain_pg_collection')")):
                conn.execute(text("TRUNCATE langchain_pg_collection CASCADE"))
        self.embeddings = ControlledEmbeddings()
        self.addCleanup(vector_store._store.cache_clear)
        for patcher in (
            patch.object(settings, "INGEST_BATCH_SIZE", 1),
            patch.object(settings, "CHUNK_PERSIST_BATCH", 1),
            patch.object(settings, "CHUNK_SIZE", 40),
            patch.object(settings, "CHUNK_OVERLAP", 0),
            patch.object(settings, "INGEST_MAX_ATTEMPTS", 3),
            patch.object(settings, "INGEST_RETRY_BASE_SECONDS", 30),
            patch.object(settings, "INGEST_RETRY_MAX_SECONDS", 600),
            patch.object(vector_store, "get_embeddings", return_value=self.embeddings),
            patch.object(vector_store, "_vector_engine", return_value=db_session.engine),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="ragkb-retry-backend-c8e2-fixture-")
        self.addCleanup(self.temp.cleanup)
        with db_session.SessionLocal() as db:
            user = User(username="retry-test", role="admin", is_active=True, password_hash="unused")
            db.add(user)
            db.flush()
            kb = KnowledgeBase(name="retry-test", collection_name="retry_" + uuid.uuid4().hex, owner_id=user.id)
            db.add(kb)
            db.commit()
            self.kb_id, self.collection = kb.id, kb.collection_name
        self.store = vector_store.get_vector_store(self.collection)

    def make_doc(self, *, chunks=3, suffix=".txt", content=None, **kwargs):
        path = Path(self.temp.name) / (uuid.uuid4().hex + suffix)
        path.write_bytes(content if content is not None else b"source content for retry test\n")
        with db_session.SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename="retry" + suffix, file_ext=suffix,
                           file_size=path.stat().st_size, stored_path=str(path),
                           parse_done=bool(chunks), chunk_count=chunks, **kwargs)
            db.add(doc)
            db.flush()
            for index in range(chunks):
                db.add(DocumentChunk(document_id=doc.id, chunk_index=index, content=f"content {index}"))
            db.commit()
            return doc.id

    def snapshot(self, doc_id):
        with db_session.SessionLocal() as db:
            doc = db.get(Document, doc_id)
            chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)
                                .order_by(DocumentChunk.chunk_index)).all()
            vectors = db.execute(select(self.store.EmbeddingStore.id, self.store.EmbeddingStore.document,
                                        self.store.EmbeddingStore.cmetadata).where(
                self.store.EmbeddingStore.cmetadata["document_id"].astext == str(doc_id))).all()
            return {
                "status": doc.status, "attempts": doc.ingest_attempts, "next": doc.next_attempt_at,
                "code": doc.last_error_code, "error": doc.error_message, "lease": doc.lease_id,
                "heartbeat": doc.heartbeat_at, "version": doc.index_version, "parse_done": doc.parse_done,
                "progress": doc.progress, "chunk_count": doc.chunk_count, "char_count": doc.char_count,
                "chunks": [(row.id, row.chunk_index, row.content, row.embedded) for row in chunks],
                "vectors": [(row.id, row.document, row.cmetadata) for row in vectors],
                "dto": DocumentRead.model_validate(doc).model_dump(mode="json"),
            }

    def change(self, doc_id, **values):
        with db_session.SessionLocal() as db:
            db.execute(update(Document).where(Document.id == doc_id).values(**values))
            db.commit()

    def due(self, doc_id):
        self.change(doc_id, next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1))

    def claim(self):
        with db_session.SessionLocal() as db:
            return worker.claim_one(db)

    def assert_reset(self, state, status):
        self.assertEqual(state["status"], status)
        self.assertEqual(state["attempts"], 0)
        self.assertIsNone(state["next"])
        self.assertIsNone(state["code"])
        self.assertIsNone(state["lease"])

    def test_transient_backoff_blocks_both_entries_then_resumes_remaining_chunks(self):
        doc_id = self.make_doc()
        self.embeddings.fail_calls = {2}
        ingest.ingest_document_task(str(doc_id))
        failed = self.snapshot(doc_id)
        self.assertEqual((failed["status"], failed["attempts"], failed["code"]), ("pending", 1, "network"))
        self.assertGreater(failed["next"], datetime.now(timezone.utc))
        self.assertTrue(failed["parse_done"])
        self.assertEqual(sum(row[3] for row in failed["chunks"]), 1)
        self.assertEqual(len(failed["vectors"]), 1)
        self.assertIsNone(self.claim())
        ingest.ingest_document_task(str(doc_id))
        self.assertEqual(self.snapshot(doc_id), failed)
        self.assertEqual(len(self.embeddings.seen), 2)
        self.due(doc_id)
        claimed_id, lease = self.claim()
        self.assertEqual(claimed_id, doc_id)
        self.assertIsNone(self.snapshot(doc_id)["next"])
        with patch.object(ingest, "iter_structured_chunks", side_effect=AssertionError("must reuse complete parse")):
            ingest.ingest_document_task(str(doc_id), lease)
        ready = self.snapshot(doc_id)
        self.assert_reset(ready, "ready")
        self.assertEqual([row[:3] for row in ready["chunks"]], [row[:3] for row in failed["chunks"]])
        self.assertEqual((ready["chunk_count"], ready["char_count"], ready["progress"]), (3, 27, 100))
        self.assertEqual(len(ready["vectors"]), 3)
        self.assertEqual({row[1] for row in ready["vectors"]}, {"content 0", "content 1", "content 2"})
        self.assertEqual(self.embeddings.seen, [["content 0"], ["content 1"], ["content 1"], ["content 2"]])
        self.assertIsNone(ready["dto"]["next_attempt_at"])

    def test_real_file_parse_and_committed_vector_batch_are_reused_after_retry(self):
        doc_id = self.make_doc(chunks=0, content=("first complete paragraph.\n" * 12).encode())
        self.embeddings.fail_calls = {2}
        ingest.ingest_document_task(str(doc_id))
        pending = self.snapshot(doc_id)
        self.assertTrue(pending["parse_done"])
        self.assertGreater(len(pending["chunks"]), 2)
        self.assertEqual(sum(row[3] for row in pending["chunks"]), 1)
        self.assertEqual(len(pending["vectors"]), 1)
        self.due(doc_id)
        with patch.object(ingest, "iter_structured_chunks", side_effect=AssertionError("must not parse again")):
            ingest.ingest_document_task(str(doc_id))
        ready = self.snapshot(doc_id)
        self.assert_reset(ready, "ready")
        self.assertEqual([row[:3] for row in ready["chunks"]], [row[:3] for row in pending["chunks"]])
        self.assertTrue(all(row[3] for row in ready["chunks"]))
        self.assertEqual(len(ready["vectors"]), len(ready["chunks"]))
        self.assertEqual(ready["char_count"], sum(len(row[2]) for row in ready["chunks"]))
        self.assertEqual(len(self.embeddings.seen), len(ready["chunks"]) + 1)
        by_index = {row[2]["chunk_index"]: row[1] for row in ready["vectors"]}
        self.assertEqual(by_index, {row[1]: row[2] for row in ready["chunks"]})

    def test_repeated_failures_exhaust_exact_budget_without_future_schedule(self):
        doc_id = self.make_doc(chunks=1)
        self.embeddings.fail_calls = {1, 2, 3, 4}
        for attempt in (1, 2, 3):
            ingest.ingest_document_task(str(doc_id))
            state = self.snapshot(doc_id)
            self.assertEqual(state["attempts"], attempt)
            self.assertEqual(state["status"], "failed" if attempt == 3 else "pending")
            self.assertEqual(state["code"], "network")
            if attempt < 3:
                self.assertGreater(state["next"], datetime.now(timezone.utc))
                self.due(doc_id)
        self.assertIsNone(state["next"])
        self.assertEqual(state["error"], "文档处理失败，请联系管理员检查日志")
        ingest.ingest_document_task(str(doc_id))
        self.assertIsNone(self.claim())
        self.assertEqual(len(self.embeddings.seen), 3)
        self.assertEqual(len(state["vectors"]), 0)

    def test_unknown_parser_failure_retries_exactly_then_exhausts_with_safe_message(self):
        doc_id = self.make_doc(chunks=0)

        def unknown(*args):
            raise RuntimeError("secret private path /private/document.pdf 401")
            yield  # pragma: no cover

        for attempt in (1, 2, 3):
            with patch.object(ingest, "iter_structured_chunks", side_effect=unknown):
                ingest.ingest_document_task(str(doc_id))
            state = self.snapshot(doc_id)
            self.assertEqual((state["attempts"], state["code"]), (attempt, "unknown"))
            self.assertFalse(state["parse_done"])
            if attempt < 3:
                self.assertEqual(state["status"], "pending")
                self.assertIsNone(state["error"])
                self.assertGreater(state["next"], datetime.now(timezone.utc))
                self.due(doc_id)
            else:
                self.assertEqual(state["status"], "failed")
                self.assertIsNone(state["next"])
                self.assertEqual(state["error"], "文档处理失败，请联系管理员检查日志")
            self.assertNotIn("secret", str(state["dto"]))
            self.assertNotIn("/private", str(state["dto"]))
        self.assertEqual(self.embeddings.seen, [])
        self.assertIsNone(self.claim())

    def test_zero_chunk_guard_is_permanent_and_never_embeds(self):
        doc_id = self.make_doc(chunks=0)
        with patch.object(ingest, "iter_structured_chunks", return_value=iter(())):
            ingest.ingest_document_task(str(doc_id))
        state = self.snapshot(doc_id)
        self.assertEqual((state["status"], state["attempts"], state["code"]), ("failed", 1, "empty_content"))
        self.assertIsNone(state["next"])
        self.assertEqual(self.embeddings.seen, [])

    def test_empty_unsupported_and_corrupt_ooxml_inputs_fail_permanently(self):
        for suffix, content, code in ((".txt", b" \n", "empty_content"),
                                      (".invalid", b"unsupported", "unsupported_format"),
                                      (".docx", b"not a zip", "corrupt_file"),
                                      (".xlsx", b"not a zip", "corrupt_file")):
            with self.subTest(suffix=suffix):
                doc_id = self.make_doc(chunks=0, suffix=suffix, content=content)
                ingest.ingest_document_task(str(doc_id))
                state = self.snapshot(doc_id)
                self.assertEqual((state["status"], state["attempts"], state["code"]), ("failed", 1, code))
                self.assertIsNone(state["next"])
                self.assertIsNone(self.claim())
        self.assertEqual(self.embeddings.seen, [])

    def test_missing_ooxml_container_parts_are_permanent(self):
        for suffix in (".docx", ".xlsx"):
            with self.subTest(suffix=suffix):
                doc_id = self.make_doc(chunks=0, suffix=suffix)
                with db_session.SessionLocal() as db:
                    path = db.get(Document, doc_id).stored_path
                with ZipFile(path, "w") as archive:
                    archive.writestr("unrelated.xml", "<x/>")
                ingest.ingest_document_task(str(doc_id))
                state = self.snapshot(doc_id)
                self.assertEqual((state["status"], state["code"]), ("failed", "corrupt_file"))
                self.assertIsNone(state["next"])
        self.assertEqual(self.embeddings.seen, [])

    def test_authentication_fails_once_without_exposing_upstream_body(self):
        doc_id = self.make_doc(chunks=1)
        self.embeddings.fail_calls = {1}
        self.embeddings.failure = lambda: CloudModelError(_format_error("local.test", 401, "secret"))
        ingest.ingest_document_task(str(doc_id))
        state = self.snapshot(doc_id)
        self.assertEqual((state["status"], state["attempts"], state["code"]), ("failed", 1, "authentication"))
        self.assertIsNone(state["next"])
        self.assertNotIn("secret", str(state["dto"]))

    def test_partial_parse_can_restart_without_duplicate_chunks(self):
        from app.services.structured_documents import StructuredChunk
        doc_id = self.make_doc(chunks=0)

        def partial(*args):
            yield StructuredChunk("partial", {}, 0.2)
            raise ConnectionError("transient parser resource failure")

        with patch.object(ingest, "iter_structured_chunks", side_effect=partial):
            ingest.ingest_document_task(str(doc_id))
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], "pending")
        self.assertFalse(state["parse_done"])
        self.assertEqual(len(state["chunks"]), 1)
        self.assertEqual(self.embeddings.seen, [])
        self.due(doc_id)
        ingest.ingest_document_task(str(doc_id))
        ready = self.snapshot(doc_id)
        self.assert_reset(ready, "ready")
        self.assertNotIn("partial", [row[2] for row in ready["chunks"]])
        self.assertEqual(len(ready["chunks"]), len(ready["vectors"]))
        self.assertEqual(ready["char_count"], sum(len(row[2]) for row in ready["chunks"]))

    def test_backoff_cancel_is_immediately_claimable_by_worker_and_direct_entry(self):
        for direct in (False, True):
            with self.subTest(direct=direct):
                doc_id = self.make_doc(chunks=1, ingest_attempts=2, last_error_code="timeout",
                                       next_attempt_at=datetime.now(timezone.utc) + timedelta(seconds=600),
                                       cancel_requested=True)
                with patch.object(ingest, "get_vector_store", side_effect=AssertionError("cancel must precede store creation")):
                    if direct:
                        ingest.ingest_document_task(str(doc_id))
                    else:
                        claimed_id, lease = self.claim()
                        self.assertEqual(claimed_id, doc_id)
                        ingest.ingest_document_task(str(doc_id), lease)
                state = self.snapshot(doc_id)
                self.assert_reset(state, "cancelled")
                self.assertEqual(state["chunks"], [])
                self.assertEqual(state["vectors"], [])
        self.assertEqual(self.embeddings.seen, [])

    def run_failure_race(self, doc_id, action, *, stop=None):
        self.embeddings.entered, self.embeddings.release = threading.Event(), threading.Event()
        self.embeddings.fail_calls = {1}
        errors = []

        def run():
            try:
                ingest.ingest_document_task(str(doc_id), stop_event=stop)
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.embeddings.entered.wait(10), "embedding did not start")
            action()
        finally:
            self.embeddings.release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_network_exception_racing_user_cancel_cancels_instead_of_retrying(self):
        doc_id = self.make_doc(ingest_attempts=1, last_error_code="timeout")
        self.run_failure_race(doc_id, lambda: self.change(doc_id, cancel_requested=True))
        self.assert_reset(self.snapshot(doc_id), "cancelled")
        self.assertEqual(self.snapshot(doc_id)["chunks"], [])

    def test_cancel_also_beats_stop_during_network_failure(self):
        doc_id = self.make_doc()
        stop = threading.Event()

        def cancel_stop():
            self.change(doc_id, cancel_requested=True)
            stop.set()

        self.run_failure_race(doc_id, cancel_stop, stop=stop)
        self.assert_reset(self.snapshot(doc_id), "cancelled")

    def test_reindex_during_failed_network_call_fences_old_generation(self):
        doc_id = self.make_doc(ingest_attempts=2, last_error_code="network")

        def reindex():
            with db_session.SessionLocal() as db:
                doc = db.scalar(select(Document).where(Document.id == doc_id).with_for_update())
                ingest.reset_document_for_reindex(db, doc)

        self.run_failure_race(doc_id, reindex)
        state = self.snapshot(doc_id)
        self.assert_reset(state, "pending")
        self.assertEqual(state["version"], 1)
        self.assertFalse(state["parse_done"])
        self.assertEqual(state["chunks"], [])
        self.assertEqual(state["vectors"], [])
        self.embeddings.entered = None
        self.embeddings.fail_calls = {2}
        ingest.ingest_document_task(str(doc_id))
        self.assertEqual((self.snapshot(doc_id)["attempts"], self.snapshot(doc_id)["status"]), (1, "pending"))
        self.due(doc_id)
        ingest.ingest_document_task(str(doc_id))
        ready = self.snapshot(doc_id)
        self.assert_reset(ready, "ready")
        self.assertTrue(ready["vectors"])
        self.assertTrue(all(row[2]["index_version"] == 1 for row in ready["vectors"]))

    def test_stolen_lease_during_network_failure_cannot_overwrite_new_worker(self):
        doc_id = self.make_doc()
        replacement = uuid.uuid4()

        def steal():
            self.change(doc_id, lease_id=replacement, ingest_attempts=2, last_error_code="timeout")

        self.run_failure_race(doc_id, steal)
        state = self.snapshot(doc_id)
        self.assertEqual((state["status"], state["lease"], state["attempts"], state["code"]),
                         ("processing", replacement, 2, "timeout"))
        self.assertIsNone(state["next"])
        self.assertEqual(len(state["vectors"]), 0)

    def test_shutdown_network_interruption_preserves_failure_budget_and_checkpoints(self):
        doc_id = self.make_doc(ingest_attempts=1, last_error_code="timeout")
        stop = threading.Event()
        self.embeddings.stop = stop
        self.embeddings.fail_calls = {1}
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual((state["status"], state["attempts"], state["code"]), ("pending", 1, "timeout"))
        self.assertIsNone(state["next"])
        self.assertIsNone(state["lease"])
        self.assertTrue(state["parse_done"])
        self.embeddings.stop = None
        ingest.ingest_document_task(str(doc_id))
        self.assert_reset(self.snapshot(doc_id), "ready")

    def test_shutdown_after_successful_batch_and_claim_release_do_not_consume_budget(self):
        doc_id = self.make_doc(ingest_attempts=1, last_error_code="network",
                               next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        claimed_id, lease = self.claim()
        with db_session.SessionLocal() as db:
            worker._release(db, claimed_id, lease)
        released = self.snapshot(doc_id)
        self.assertEqual((released["status"], released["attempts"], released["code"]), ("pending", 1, "network"))
        self.assertIsNone(released["next"])
        stop = threading.Event()
        self.embeddings.stop = stop
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        stopped = self.snapshot(doc_id)
        self.assertEqual((stopped["status"], stopped["attempts"], stopped["code"]), ("pending", 1, "network"))
        self.assertEqual(sum(row[3] for row in stopped["chunks"]), 1)
        self.assertEqual(len(stopped["vectors"]), 1)
        self.assertIsNone(stopped["lease"])
        self.embeddings.stop = None
        ingest.ingest_document_task(str(doc_id))
        ready = self.snapshot(doc_id)
        self.assert_reset(ready, "ready")
        self.assertEqual(len(ready["vectors"]), 3)
        self.assertEqual(len(self.embeddings.seen), 3)

    def test_stale_recovery_preserves_budget_and_fences_inflight_failure(self):
        doc_id = self.make_doc(ingest_attempts=1, last_error_code="timeout")
        claimed = []

        def recover():
            self.change(doc_id, heartbeat_at=datetime.now(timezone.utc) - timedelta(days=1))
            with db_session.SessionLocal() as db:
                self.assertEqual(worker.recover_stale(db), 1)
            claimed.append(self.claim())

        self.run_failure_race(doc_id, recover)
        state = self.snapshot(doc_id)
        self.assertEqual(state["lease"], claimed[0][1])
        self.assertEqual((state["status"], state["attempts"], state["code"]), ("processing", 1, "timeout"))
        self.assertIsNone(state["next"])
        self.embeddings.entered = None
        ingest.ingest_document_task(str(doc_id), claimed[0][1])
        self.assert_reset(self.snapshot(doc_id), "ready")

    def test_reindex_from_exhausted_failure_restores_full_budget(self):
        doc_id = self.make_doc(status="failed", ingest_attempts=3, last_error_code="network",
                               error_message="safe old error", cancel_requested=True,
                               next_attempt_at=datetime.now(timezone.utc) + timedelta(days=1))
        with db_session.SessionLocal() as db:
            doc = db.scalar(select(Document).where(Document.id == doc_id).with_for_update())
            ingest.reset_document_for_reindex(db, doc)
        state = self.snapshot(doc_id)
        self.assert_reset(state, "pending")
        self.assertEqual(state["version"], 1)
        self.assertIsNone(state["error"])
        self.embeddings.fail_calls = {1}
        ingest.ingest_document_task(str(doc_id))
        self.assertEqual((self.snapshot(doc_id)["attempts"], self.snapshot(doc_id)["status"]), (1, "pending"))

    def test_real_skip_locked_competitors_claim_distinct_unlocked_rows(self):
        first = self.make_doc(chunks=1, created_at=datetime.now(timezone.utc) - timedelta(days=1))
        others = {self.make_doc(chunks=1), self.make_doc(chunks=1)}
        barrier = threading.Barrier(3)
        results, errors = [], []

        def claim_competitor():
            try:
                with db_session.SessionLocal() as db:
                    db.execute(text("SET LOCAL statement_timeout = 3000"))
                    barrier.wait(5)
                    results.append(worker.claim_one(db))
            except BaseException as exc:
                errors.append(exc)

        with db_session.SessionLocal() as locked:
            locked.scalar(select(Document).where(Document.id == first).with_for_update())
            threads = [threading.Thread(target=claim_competitor, daemon=True) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(5)
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive(), "SKIP LOCKED competitor blocked")
            self.assertEqual(errors, [])
            self.assertEqual({row[0] for row in results}, others)
            self.assertEqual(len({row[1] for row in results}), 2)
            locked.rollback()
        self.assertEqual(self.claim()[0], first)
        self.assertIsNone(self.claim())

    def test_schema_upgrade_adds_missing_retry_columns_and_index_idempotently(self):
        assert_owned()
        doc_id = self.make_doc(chunks=0)
        try:
            with db_session.engine.begin() as conn:
                for column in ("ingest_attempts", "next_attempt_at", "last_error_code"):
                    conn.execute(text(f"ALTER TABLE documents DROP COLUMN {column}"))
            init_db(create_admin=False)
            init_db(create_admin=False)
            state = self.snapshot(doc_id)
            self.assertEqual((state["attempts"], state["code"], state["next"]), (0, None, None))
            with db_session.engine.connect() as conn:
                columns = {row.column_name: row for row in conn.execute(text(
                    "SELECT column_name, is_nullable, column_default, data_type FROM information_schema.columns "
                    "WHERE table_schema=current_schema() AND table_name='documents'"))}
                self.assertEqual(columns["ingest_attempts"].is_nullable, "NO")
                self.assertIn("0", columns["ingest_attempts"].column_default)
                self.assertEqual(columns["next_attempt_at"].data_type, "timestamp with time zone")
                index = conn.scalar(text("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                                         "AND indexname='ix_documents_ingest_queue'"))
                self.assertIn("next_attempt_at, created_at", index)
        finally:
            init_db(create_admin=False)


if __name__ == "__main__":
    unittest.main()
