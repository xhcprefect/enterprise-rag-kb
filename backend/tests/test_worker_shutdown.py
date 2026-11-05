"""worker 停机行为：真实 PostgreSQL，仅 B 专用 *_test 库。

覆盖：信号只置位事件、不再领新任务、空闲等待可被唤醒、领取竞态交回、
stale 恢复扫描与上传清理周期独立，以及两个真实 SIGTERM 子进程用例
（Windows 明确 skip；主代理在 Linux 容器内执行，不以 Windows 终止冒充）：

- 空闲 worker 收到 SIGTERM 后尽快退出；
- 正在向量化的 worker 收到 SIGTERM：在途批次成功返回后先提交向量与
  embedded 检查点，再退回 pending 并清理租约；重领不产生重复向量，
  旧租约无法再写入。
"""
import os
import queue
import signal
import subprocess
import sys
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
from sqlalchemy import func, select

from app.core.config import settings
from app.db.init_db import init_db
from app.models import Document, DocumentChunk, DocumentStatus, KnowledgeBase, User
from app.services import ingest, vector_store
import worker

requires_db = unittest.skipUnless(env.ENABLED, getattr(env, "_REASON", "需要专用测试库"))
# 子进程用例需要真实 POSIX 信号 + 专用测试库；Windows 一律跳过。
requires_linux_db = unittest.skipUnless(
    env.ENABLED and not sys.platform.startswith("win"),
    "需要专用测试库且在 Linux 容器内执行（Windows 无 POSIX SIGTERM）",
)


class FakeEmbeddings(Embeddings):
    """父进程重领/旧租约用例使用的可控假向量。"""

    def __init__(self):
        self.calls = 0
        self.seen = []

    def embed_documents(self, texts):
        self.calls += 1
        self.seen.append(list(texts))
        return [[1.0] + [0.0] * 1023 for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * 1023


class _WorkerDbFixtureMixin:
    """真实测试库夹具；只被带 guard 的测试类继承，本身不参与发现。"""

    @classmethod
    def setUpClass(cls):
        env.assert_owned_test_database()
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        env.truncate_all()
        worker.stop_event.clear()
        worker._running = True
        self.addCleanup(worker.stop_event.clear)
        vector_store._store.cache_clear()
        self.addCleanup(vector_store._store.cache_clear)
        with SessionLocal() as db:
            user = User(username="worker_stop_admin", role="admin", is_active=True, password_hash="unused-test-only")
            db.add(user)
            db.flush()
            kb = KnowledgeBase(name="worker-stop", collection_name="worker_" + uuid.uuid4().hex,
                               owner_id=user.id, is_public=True)
            db.add(kb)
            db.commit()
            self.kb_id = kb.id
            self.collection = kb.collection_name
        self.temp = Path(env._STATE) / ("worker-" + uuid.uuid4().hex)
        self.temp.mkdir(parents=True, exist_ok=True)

    def make_doc(self, status=DocumentStatus.PENDING.value, *, heartbeat=None, lease=None,
                 parse_done=False, chunks=0):
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename="w.txt", file_ext=".txt", file_size=1,
                           stored_path=str(self.temp / "w.txt"), status=status,
                           heartbeat_at=heartbeat, lease_id=lease, parse_done=parse_done,
                           chunk_count=chunks)
            db.add(doc)
            db.flush()
            for index in range(chunks):
                db.add(DocumentChunk(document_id=doc.id, chunk_index=index, content=f"content {index}"))
            db.commit()
            return doc.id

    def snapshot(self, doc_id):
        store = vector_store.get_vector_store(self.collection)
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)).all()
            vectors = db.scalar(select(func.count()).select_from(store.EmbeddingStore)) or 0
            return {
                "status": doc.status, "progress": doc.progress, "parse_done": doc.parse_done,
                "lease": doc.lease_id, "heartbeat": doc.heartbeat_at,
                "chunks": len(chunks), "embedded": sum(1 for c in chunks if c.embedded),
                "vectors": vectors,
            }


@requires_db
class WorkerShutdownTests(_WorkerDbFixtureMixin, unittest.TestCase):
    def test_signal_handler_only_sets_event_and_stays_fast(self):
        started = time.perf_counter()
        worker._stop(signal.SIGTERM, None)
        elapsed = time.perf_counter() - started
        self.assertTrue(worker.stop_event.is_set())
        self.assertFalse(worker._running)
        self.assertLess(elapsed, 0.1, "signal handler 不应做数据库/日志等重活")

    def test_stop_event_wait_is_woken_without_waiting_full_interval(self):
        # 直接验证空闲等待用的原语：置位后 wait 立即返回。
        worker.stop_event.set()
        started = time.perf_counter()
        self.assertTrue(worker.stop_event.wait(30))
        self.assertLess(time.perf_counter() - started, 1.0)

    def test_claim_one_still_uses_skip_locked(self):
        self.make_doc()
        with SessionLocal() as db:
            claimed = worker.claim_one(db)
        self.assertIsNotNone(claimed)
        self.assertFalse(worker.stop_event.is_set())

    def test_worker_loop_does_not_claim_after_stop_and_exits(self):
        doc_id = self.make_doc()  # pending 任务在场，但循环启动时已经停止
        calls = []

        def fake_ingest(*args, **kwargs):
            calls.append((args, kwargs))

        worker.stop_event.set()
        with patch.object(worker, "init_db"), patch.object(worker, "setup_logging"), \
             patch.object(worker, "ingest_document_task", side_effect=fake_ingest), \
             patch.object(worker, "recover_stale"), patch.object(worker, "cleanup_expired"):
            self.assertEqual(worker.main([]), 0)
        self.assertEqual(calls, [])
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, doc_id).status, DocumentStatus.PENDING.value)

    def test_claimed_then_stopped_document_is_released(self):
        doc_id = self.make_doc()
        calls = []
        original_claim = worker.claim_one

        def claim_then_stop(db):
            claimed = original_claim(db)
            worker.stop_event.set()  # 模拟领取成功后、开始处理前收到 SIGTERM
            return claimed

        def fake_ingest(*args, **kwargs):  # 不应被调用
            calls.append(args)

        with patch.object(worker, "init_db"), patch.object(worker, "setup_logging"), \
             patch.object(worker, "ingest_document_task", side_effect=fake_ingest), \
             patch.object(worker, "recover_stale"), patch.object(worker, "cleanup_expired"), \
             patch.object(worker, "claim_one", side_effect=claim_then_stop):
            self.assertEqual(worker.main([]), 0)
        self.assertEqual(calls, [])
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            self.assertEqual(doc.status, DocumentStatus.PENDING.value)
            self.assertIsNone(doc.lease_id)
            self.assertIsNone(doc.heartbeat_at)

    def test_stop_during_task_passes_event_and_stops_claiming(self):
        self.make_doc()
        seen = []

        def fake_ingest(document_id, lease_id=None, *, stop_event=None):
            seen.append(stop_event)
            stop_event.set()

        with patch.object(worker, "init_db"), patch.object(worker, "setup_logging"), \
             patch.object(worker, "ingest_document_task", side_effect=fake_ingest), \
             patch.object(worker, "recover_stale"), patch.object(worker, "cleanup_expired"):
            self.assertEqual(worker.main([]), 0)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0], worker.stop_event)

    def test_recovery_scan_is_independent_of_upload_cleanup(self):
        self.assertEqual(worker.RECOVER_INTERVAL_SECONDS, 30)
        self.assertEqual(worker.CLEANUP_INTERVAL_SECONDS, 600)
        stale, cleanups = [], []
        ticks = iter([1.0, 33.0, 64.0])

        def fake_monotonic():
            try:
                return next(ticks)
            except StopIteration:
                worker.stop_event.set()
                return 64.0

        def fake_recover(db):
            stale.append(1)

        def fake_cleanup(db):
            cleanups.append(1)

        with patch.object(worker, "init_db"), patch.object(worker, "setup_logging"), \
             patch.object(worker, "recover_stale", side_effect=fake_recover), \
             patch.object(worker, "cleanup_expired", side_effect=fake_cleanup), \
             patch.object(worker, "claim_one", return_value=None), \
             patch.object(worker, "IDLE_POLL_SECONDS", 0.01), \
             patch.object(worker.time, "monotonic", side_effect=fake_monotonic):
            self.assertEqual(worker.main([]), 0)
        self.assertEqual(len(stale), 3)    # 启动一次 + 33s/64s 两次周期
        self.assertEqual(len(cleanups), 0)  # 600s 内不触发上传清理

    def test_recover_stale_keeps_existing_semantics(self):
        expired = self.make_doc(DocumentStatus.PROCESSING.value, lease=None,
                                heartbeat=datetime.now(timezone.utc))
        fresh = self.make_doc(DocumentStatus.PROCESSING.value, lease=uuid.uuid4(),
                              heartbeat=datetime.now(timezone.utc))
        with SessionLocal() as db:
            self.assertEqual(worker.recover_stale(db), 1)
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, expired).status, DocumentStatus.PENDING.value)
            self.assertEqual(db.get(Document, fresh).status, DocumentStatus.PROCESSING.value)
            self.assertIsNotNone(db.get(Document, fresh).lease_id)


IDLE_SCRIPT = r"""
import signal
import worker
from app.db.init_db import init_db

# 只验证信号/事件/退出路径：不真正处理文档。
init_db(create_admin=False)
signal.signal(signal.SIGINT, worker._stop)
signal.signal(signal.SIGTERM, worker._stop)
print("signal-ready", flush=True)
worker.main([])
print("signal-exit", flush=True)
"""


# 在途向量请求：等待真实 SIGTERM 到达后才返回成功，验证
# 1) 成功批次先于交回提交；2) 之后退回 pending 并清理租约。
ACTIVE_SCRIPT = r"""
import sys
from unittest.mock import patch

from langchain_core.embeddings import Embeddings

import worker
from app.core.config import settings
from app.services import vector_store

DOC_ID = sys.argv[1]


class BlockingEmbeddings(Embeddings):
    def embed_documents(self, texts):
        print("EMBED_START", flush=True)
        # 等真实信号处理器置位；返回成功，模拟在途请求正常完成。
        got_stop = worker.stop_event.wait(60)
        print("EMBED_RETURN got_stop=%s" % got_stop, flush=True)
        return [[1.0] + [0.0] * 1023 for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * 1023


fake = BlockingEmbeddings()
original_claim = worker.claim_one


def claim_and_report(db):
    claimed = original_claim(db)
    if claimed is not None:
        print("LEASE=" + str(claimed[1]), flush=True)
    return claimed


with patch.object(settings, "INGEST_BATCH_SIZE", 1), \
     patch.object(settings, "CHUNK_PERSIST_BATCH", 1), \
     patch.object(vector_store, "get_embeddings", return_value=fake), \
     patch.object(worker, "claim_one", side_effect=claim_and_report):
    worker.main([])
print("WORKER_EXIT", flush=True)
"""


class _SignalProcessMixin(_WorkerDbFixtureMixin):
    """子进程辅助：后台线程持续排空输出，读取带超时上限，绝无无界 readline。"""

    def spawn_worker(self, script, *args):
        child_env = dict(os.environ)
        child_env.update(RAG_DISABLE_DOTENV="1", DATABASE_URL=env.TEST_URL_VALUE)
        process = subprocess.Popen(
            [sys.executable, "-c", script, *args],
            cwd=str(Path(env.__file__).resolve().parents[1]),
            env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        lines = queue.Queue()

        def drain():
            try:
                for line in process.stdout:
                    lines.put(line)
            finally:
                lines.put(None)  # 进程结束哨兵

        threading.Thread(target=drain, name="worker-output", daemon=True).start()
        self.addCleanup(self._terminate, process)
        return process, lines

    @staticmethod
    def _terminate(process):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    def expect_line(self, lines, token, timeout):
        """有界等待包含 ``token`` 的一行；超时立即失败并附最近输出。"""
        seen = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"等待 {token!r} 超过 {timeout}s；已见输出：{seen[-10:]}")
            try:
                line = lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:  # 子进程已退出但没等到目标输出
                self.fail(f"子进程已结束仍未出现 {token!r}；已见输出：{seen[-10:]}")
            seen.append(line.rstrip())
            if token in line:
                return line.strip()


@requires_linux_db
class WorkerSignalProcessTests(_SignalProcessMixin, unittest.TestCase):
    """真实进程 SIGTERM：Windows 跳过，主代理在 Linux 容器内执行。"""

    def test_sigterm_stops_idle_worker_promptly(self):
        process, lines = self.spawn_worker(IDLE_SCRIPT)
        self.expect_line(lines, "signal-ready", 60)
        process.send_signal(signal.SIGTERM)
        started = time.perf_counter()
        code = process.wait(timeout=60)
        elapsed = time.perf_counter() - started
        self.assertEqual(code, 0, "worker 应正常退出而非被信号杀死")
        self.assertLess(elapsed, 30)
        self.assertTrue(self.expect_line(lines, "signal-exit", 10))

    def test_sigterm_during_embedding_commits_batch_then_hands_back(self):
        doc_id = self.make_doc(parse_done=True, chunks=3)
        process, lines = self.spawn_worker(ACTIVE_SCRIPT, str(doc_id))

        # 子进程先领取并进入向量请求，父进程再发真实 SIGTERM。
        lease = uuid.UUID(self.expect_line(lines, "LEASE=", 60).split("=", 1)[1])
        self.expect_line(lines, "EMBED_START", 60)
        process.send_signal(signal.SIGTERM)
        returned = self.expect_line(lines, "EMBED_RETURN", 60)
        self.assertIn("got_stop=True", returned)
        self.assertEqual(process.wait(timeout=60), 0)
        self.expect_line(lines, "WORKER_EXIT", 10)

        # 在途批次已提交，然后才交回 pending 并清空租约/心跳。
        after_stop = self.snapshot(doc_id)
        self.assertEqual(after_stop["status"], DocumentStatus.PENDING.value)
        self.assertIsNone(after_stop["lease"])
        self.assertIsNone(after_stop["heartbeat"])
        self.assertTrue(after_stop["parse_done"])
        self.assertEqual(after_stop["embedded"], 1)
        self.assertEqual(after_stop["vectors"], 1)
        self.assertGreater(after_stop["progress"], ingest.EMBED_FROM)

        # 重领：续传剩余批次，确定性向量 ID 不产生重复。
        # snapshot() 已用真实实现缓存过 store，必须先清缓存让假向量生效。
        embeddings = FakeEmbeddings()
        vector_store._store.cache_clear()
        with patch.object(settings, "INGEST_BATCH_SIZE", 1), \
             patch.object(settings, "CHUNK_PERSIST_BATCH", 1), \
             patch.object(vector_store, "get_embeddings", return_value=embeddings), \
             patch.object(vector_store, "_vector_engine", return_value=engine):
            ingest.ingest_document_task(str(doc_id))
        resumed = self.snapshot(doc_id)
        self.assertEqual(resumed["status"], DocumentStatus.READY.value)
        self.assertEqual(resumed["embedded"], 3)
        self.assertEqual(resumed["vectors"], 3)   # 不是 1+3=4
        self.assertEqual(embeddings.calls, 2)     # 只补剩余两块
        self.assertIsNone(resumed["lease"])

        # 旧租约（停机交回前的租约）已失权，不能再写入任何状态。
        with patch.object(vector_store, "get_embeddings", return_value=embeddings), \
             patch.object(vector_store, "_vector_engine", return_value=engine):
            ingest.ingest_document_task(str(doc_id), lease)
        fenced = self.snapshot(doc_id)
        self.assertEqual(fenced["status"], DocumentStatus.READY.value)
        self.assertEqual(fenced["progress"], 100)
        self.assertEqual(fenced["embedded"], 3)
        self.assertEqual(fenced["vectors"], 3)
        self.assertEqual(embeddings.calls, 2)     # 旧租约未触发任何模型调用


if __name__ == "__main__":
    unittest.main()
