"""入库停机（协作式）回归：真实 PostgreSQL/pgvector，仅 B 专用 *_test 库。

先设置 RAG_DISABLE_DOTENV=1 与独立临时目录，再导入 app.*。
"""
import threading
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tests import _ingest_shutdown_env as env
from tests._ingest_shutdown_env import SessionLocal, engine

from langchain_core.embeddings import Embeddings
from sqlalchemy import func, select, update

from app.core.config import settings
from app.db.init_db import init_db
from app.models import Document, DocumentChunk, DocumentStatus, KnowledgeBase, User
from app.services import ingest, vector_store


class FakeEmbeddings(Embeddings):
    """可控的 1024 维向量假实现；``on_embed`` 可注入竞态动作。"""

    def __init__(self):
        self.calls = 0
        self.fail_on = None
        self.on_embed = None
        self.seen: list[list[str]] = []

    def embed_documents(self, texts):
        self.calls += 1
        self.seen.append(list(texts))
        if self.on_embed:
            self.on_embed()
        if self.calls == self.fail_on:
            raise RuntimeError("simulated upstream failure")
        return [[1.0] + [0.0] * 1023 for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * 1023


@unittest.skipUnless(env.ENABLED, getattr(env, "_REASON", "需要专用测试库"))
class IngestShutdownTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env.assert_owned_test_database()
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        vector_store._store.cache_clear()
        engine.dispose()

    def setUp(self):
        env.truncate_all()
        self.embeddings = FakeEmbeddings()
        self.addCleanup(vector_store._store.cache_clear)
        for patcher in (
            patch.object(settings, "INGEST_BATCH_SIZE", 1),
            patch.object(settings, "CHUNK_PERSIST_BATCH", 1),
            patch.object(settings, "CHUNK_SIZE", 40),
            patch.object(settings, "CHUNK_OVERLAP", 0),
            patch.object(vector_store, "get_embeddings", return_value=self.embeddings),
            patch.object(vector_store, "_vector_engine", return_value=engine),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        vector_store._store.cache_clear()
        with SessionLocal() as db:
            self.user = User(username="ingest_stop_admin", role="admin", is_active=True, password_hash="unused-test-only")
            db.add(self.user)
            db.flush()
            self.kb = KnowledgeBase(name="ingest-stop", collection_name="stop_" + uuid.uuid4().hex,
                                    owner_id=self.user.id, is_public=True)
            db.add(self.kb)
            db.commit()
        self.store = vector_store.get_vector_store(self.kb.collection_name)
        self.temp = Path(env._STATE) / ("doc-" + uuid.uuid4().hex)
        self.temp.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: [p.unlink() for p in self.temp.glob("*")])

    # --- 工具 -----------------------------------------------------------------
    def make_doc(self, *, text=None, parse_done=False, chunks=0) -> uuid.UUID:
        path = self.temp / f"{uuid.uuid4().hex}.txt"
        path.write_text(text if text is not None else ("段落内容。" * 400), encoding="utf-8")
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb.id, filename="stop.txt", file_ext=".txt",
                           file_size=path.stat().st_size, stored_path=str(path),
                           parse_done=parse_done, chunk_count=chunks)
            db.add(doc)
            db.flush()
            for index in range(chunks):
                db.add(DocumentChunk(document_id=doc.id, chunk_index=index, content=f"content {index}"))
            db.commit()
            return doc.id

    def snapshot(self, doc_id):
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)
                                .order_by(DocumentChunk.chunk_index)).all()
            vectors = db.scalar(select(func.count()).select_from(self.store.EmbeddingStore)) or 0
            return {
                "status": doc.status, "progress": doc.progress, "parse_done": doc.parse_done,
                "lease": doc.lease_id, "heartbeat": doc.heartbeat_at, "error": doc.error_message,
                "attempts": doc.ingest_attempts, "next": doc.next_attempt_at, "code": doc.last_error_code,
                "chunk_count": doc.chunk_count, "chunks": len(chunks),
                "embedded": sum(1 for c in chunks if c.embedded), "vectors": vectors,
            }

    @staticmethod
    def stop_after(limit, stop_event, action=None):
        """包装 iter_structured_chunks：产出 ``limit`` 块后置位 stop（可选附带竞态动作）。"""
        real = ingest.iter_structured_chunks
        seen = {"count": 0}

        def wrapper(*args, **kwargs):
            for chunk in real(*args, **kwargs):
                if seen["count"] >= limit:
                    if action:
                        action()
                    stop_event.set()
                seen["count"] += 1
                yield chunk

        return wrapper

    # --- 解析/检查点 -----------------------------------------------------------
    def test_stop_during_parse_keeps_parse_done_false_and_discards_partial_chunks(self):
        doc_id = self.make_doc(parse_done=False)
        stop = threading.Event()
        with patch.object(ingest, "iter_structured_chunks", self.stop_after(1, stop)):
            ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.PENDING.value)
        self.assertFalse(state["parse_done"])
        self.assertEqual(state["chunks"], 0)
        self.assertEqual(state["vectors"], 0)
        self.assertIsNone(state["lease"])
        self.assertIsNone(state["heartbeat"])

    def test_complete_parse_is_kept_when_stop_arrives_before_first_batch(self):
        doc_id = self.make_doc(parse_done=False)
        stop = threading.Event()
        # 产出最后一块（耗尽）之后才置位，模拟“解析完整结束、尚未向量化”。
        real = ingest.iter_structured_chunks
        produced = []

        def wrapper(*args, **kwargs):
            yield from real(*args, **kwargs)
            stop.set()

        with patch.object(ingest, "iter_structured_chunks", wrapper):
            ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.PENDING.value)
        self.assertTrue(state["parse_done"])
        self.assertGreater(state["chunks"], 0)
        self.assertEqual(state["embedded"], 0)
        self.assertEqual(state["vectors"], 0)
        self.assertEqual(self.embeddings.calls, 0)
        self.assertIsNone(state["lease"])

    # --- 向量的批次边界 --------------------------------------------------------
    def test_successful_batch_commits_then_stops_without_starting_another(self):
        doc_id = self.make_doc(parse_done=True, chunks=3)
        stop = threading.Event()
        self.embeddings.on_embed = stop.set  # 第一批成功返回前收到停止信号
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(self.embeddings.calls, 1)  # 成功批次之后不再发起
        self.assertEqual(state["status"], DocumentStatus.PENDING.value)
        self.assertTrue(state["parse_done"])
        self.assertEqual(state["embedded"], 1)
        self.assertEqual(state["vectors"], 1)
        self.assertGreater(state["progress"], ingest.EMBED_FROM)
        self.assertIsNone(state["lease"])

    def test_resume_after_shutdown_does_not_duplicate_vectors(self):
        doc_id = self.make_doc(parse_done=True, chunks=3)
        stop = threading.Event()
        self.embeddings.on_embed = stop.set
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        self.embeddings.on_embed = None
        ingest.ingest_document_task(str(doc_id))  # 重领：未传 stop，恢复原语义
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.READY.value)
        self.assertEqual(state["embedded"], 3)
        self.assertEqual(state["vectors"], 3)
        self.assertEqual(self.embeddings.calls, 3)  # 首批 + 剩余两批，无重复

    def test_stop_near_final_publication_still_publishes_when_no_batch_remains(self):
        doc_id = self.make_doc(parse_done=True, chunks=1)
        stop = threading.Event()
        self.embeddings.on_embed = stop.set  # 唯一一批成功返回后立刻停
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.READY.value)
        self.assertEqual(state["progress"], 100)
        self.assertEqual(state["embedded"], 1)
        self.assertIsNone(state["lease"])

    # --- 取消/租约优先级 -------------------------------------------------------
    def test_user_cancel_beats_stop_and_removes_vectors(self):
        doc_id = self.make_doc(parse_done=True, chunks=2)
        stop = threading.Event()

        def cancel():
            stop.set()
            with SessionLocal() as db:
                db.execute(update(Document).where(Document.id == doc_id).values(cancel_requested=True))
                db.commit()

        self.embeddings.on_embed = cancel
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.CANCELLED.value)
        self.assertEqual(state["chunks"], 0)
        self.assertEqual(state["vectors"], 0)
        self.assertFalse(state["parse_done"])
        self.assertIsNone(state["lease"])

    def test_replaced_lease_beats_stop_and_old_worker_cannot_write(self):
        doc_id = self.make_doc(parse_done=True, chunks=2)
        stop = threading.Event()
        replacement = uuid.uuid4()

        def replace():
            stop.set()
            with SessionLocal() as db:
                db.execute(update(Document).where(Document.id == doc_id).values(lease_id=replacement))
                db.commit()

        self.embeddings.on_embed = replace
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["lease"], replacement)
        self.assertEqual(state["status"], DocumentStatus.PROCESSING.value)
        self.assertEqual(state["chunks"], 2)
        self.assertEqual(state["embedded"], 0)
        self.assertEqual(state["vectors"], 0)

    def test_lease_change_after_stop_prevents_hand_back_resurrection(self):
        doc_id = self.make_doc(parse_done=False)
        stop = threading.Event()
        replacement = uuid.uuid4()

        def steal():
            stop.set()
            with SessionLocal() as db:
                db.execute(update(Document).where(Document.id == doc_id).values(lease_id=replacement))
                db.commit()

        with patch.object(ingest, "iter_structured_chunks", self.stop_after(0, stop, steal)):
            ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        # 旧 worker 不得把已被接管的处理中任务退回 pending，也不得清掉新租约。
        self.assertEqual(state["status"], DocumentStatus.PROCESSING.value)
        self.assertEqual(state["lease"], replacement)

    # --- 网络失败/心跳 ---------------------------------------------------------
    def test_network_failure_with_stop_hands_back_instead_of_failing(self):
        doc_id = self.make_doc(parse_done=True, chunks=2)
        stop = threading.Event()

        def fail():
            stop.set()

        self.embeddings.on_embed = fail
        self.embeddings.fail_on = 1
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.PENDING.value)
        self.assertTrue(state["parse_done"])
        self.assertIsNone(state["error"])
        self.assertEqual(state["embedded"], 0)
        self.assertEqual(self.embeddings.calls, 1)

    def test_network_failure_without_stop_schedules_retry_and_keeps_checkpoints(self):
        doc_id = self.make_doc(parse_done=True, chunks=2)
        self.embeddings.fail_on = 1
        ingest.ingest_document_task(str(doc_id))
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.PENDING.value)
        self.assertEqual(state["attempts"], 1)
        self.assertGreater(state["next"], datetime.now(timezone.utc))
        self.assertTrue(state["parse_done"])
        self.assertEqual(state["chunks"], 2)
        self.assertEqual(state["embedded"], 0)
        self.assertIsNone(state["error"])
        self.assertIsNone(state["lease"])

    def test_single_attempt_budget_still_finishes_failed(self):
        doc_id = self.make_doc(parse_done=True, chunks=2)
        self.embeddings.fail_on = 1
        with patch.object(settings, "INGEST_MAX_ATTEMPTS", 1):
            ingest.ingest_document_task(str(doc_id))
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.FAILED.value)
        self.assertEqual(state["attempts"], 1)
        self.assertIsNone(state["next"])
        self.assertIsNotNone(state["error"])
        self.assertIsNone(state["lease"])

    def test_heartbeat_thread_cannot_renew_released_lease(self):
        doc_id = self.make_doc(parse_done=True, chunks=2)
        stop = threading.Event()
        self.embeddings.on_embed = stop.set
        with patch.object(settings, "INGEST_STALE_SECONDS", 3):  # 心跳间隔 1s
            ingest.ingest_document_task(str(doc_id), stop_event=stop)
            self.assertIsNone(self.snapshot(doc_id)["heartbeat"])
            time.sleep(1.4)
        # 交回后即便有迟到线程醒来，条件更新也不会刷新已释放租约。
        self.assertIsNone(self.snapshot(doc_id)["heartbeat"])
        self.assertIsNone(self.snapshot(doc_id)["lease"])

    # --- 领取竞态/旧行为 -------------------------------------------------------
    def test_stop_before_claim_without_lease_leaves_document_pending(self):
        doc_id = self.make_doc(parse_done=False)
        stop = threading.Event()
        stop.set()
        ingest.ingest_document_task(str(doc_id), stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.PENDING.value)
        self.assertEqual(state["chunks"], 0)
        self.assertIsNone(state["lease"])

    def test_stop_after_worker_claim_hands_back_processing_document(self):
        doc_id = self.make_doc(parse_done=True, chunks=2)
        with SessionLocal() as db:
            lease = uuid.uuid4()
            db.execute(update(Document).where(Document.id == doc_id).values(
                status=DocumentStatus.PROCESSING.value, lease_id=lease))
            db.commit()
        stop = threading.Event()
        stop.set()
        ingest.ingest_document_task(str(doc_id), lease, stop_event=stop)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.PENDING.value)
        self.assertIsNone(state["lease"])
        self.assertTrue(state["parse_done"])

    def test_without_stop_event_behavior_is_unchanged(self):
        doc_id = self.make_doc(parse_done=False)
        ingest.ingest_document_task(str(doc_id))
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], DocumentStatus.READY.value)
        self.assertTrue(state["parse_done"])
        self.assertEqual(state["progress"], 100)
        self.assertEqual(state["vectors"], state["chunks"])

    def test_stop_event_does_not_leak_into_plain_call(self):
        doc_id = self.make_doc(parse_done=True, chunks=1)
        stop = threading.Event()
        stop.set()
        ingest.ingest_document_task(str(doc_id))  # 同一文档不传 stop → 正常完成
        self.assertEqual(self.snapshot(doc_id)["status"], DocumentStatus.READY.value)


if __name__ == "__main__":
    unittest.main()
