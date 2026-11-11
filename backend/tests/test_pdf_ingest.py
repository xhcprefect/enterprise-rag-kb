"""真实 PostgreSQL + 确定性向量替身验证 PDF 入库的成功/失败边界（不启动服务/模型）。

数据库门沿用 ``tests._memory_project_test_gate``（loopback + ``_test`` + owner
comment 精确匹配）；未显式配置时全部 skip，绝不连接业务库。正常表格识别仍由本机
真实 ``find_tables`` 完成，只在“图像探测不可用”的失败路径注入第三方异常。
"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import pymupdf
from langchain_core.embeddings import Embeddings
from sqlalchemy import func, select, text

from tests import _memory_project_test_gate as gate
from app.core.config import settings
from app.db import session as db_session
from app.models import Document, DocumentChunk, KnowledgeBase, User
from app.services import ingest, rag, source_documents, vector_store

_TABLES = ("users, knowledge_bases, documents, document_chunks, conversations, upload_sessions, "
           "audit_logs, messages, chat_turns, model_usage, model_prices")


class CountingEmbeddings(Embeddings):
    """确定性 1024 维向量；只统计 ``embed_documents`` 调用，不访问任何服务。"""

    def __init__(self):
        self.calls = 0
        self.query_calls = 0
        self.seen: list[list[str]] = []

    def embed_documents(self, texts):
        self.calls += 1
        self.seen.append(list(texts))
        return [[1.0] + [0.0] * 1023 for _ in texts]

    def embed_query(self, query):
        self.query_calls += 1
        return [1.0] + [0.0] * 1023


def rule_table(page, left, top, rows, cols, values, cell_w=90, cell_h=22):
    for row in range(rows + 1):
        page.draw_line((left, top + row * cell_h), (left + cols * cell_w, top + row * cell_h))
    for col in range(cols + 1):
        page.draw_line((left + col * cell_w, top), (left + col * cell_w, top + rows * cell_h))
    for row in range(rows):
        for col in range(cols):
            if values[row][col]:
                page.insert_text((left + col * cell_w + 4, top + row * cell_h + cell_h - 7),
                                 values[row][col])


def tiny_png(color=120):
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 24, 24), 0)
    pixmap.clear_with(color)
    return pixmap.tobytes("png")


@unittest.skipIf(
    gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
    "需要显式 owned PostgreSQL 测试库（TEST_DATABASE_URL + TEST_DATABASE_OWNER）",
)
class PdfIngestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        from app.db.init_db import init_db

        init_db(create_admin=False)
        cls.engine = db_session.engine
        cls.Session = db_session.SessionLocal

    @classmethod
    def tearDownClass(cls):
        vector_store._store.cache_clear()
        cls.engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, self.engine)
        with self.engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
            if conn.execute(text("SELECT to_regclass('langchain_pg_collection')")).scalar():
                conn.execute(text("TRUNCATE langchain_pg_collection CASCADE"))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.embeddings = CountingEmbeddings()
        self.addCleanup(vector_store._store.cache_clear)
        for patcher in (
            patch.object(settings, "UPLOAD_DIR", str(self.root)),
            patch.object(settings, "INGEST_BATCH_SIZE", 2),
            patch.object(settings, "CHUNK_PERSIST_BATCH", 1),
            patch.object(settings, "CHUNK_SIZE", 160),
            patch.object(settings, "CHUNK_OVERLAP", 0),
            patch.object(vector_store, "get_embeddings", return_value=self.embeddings),
            patch.object(vector_store, "_vector_engine", return_value=self.engine),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        vector_store._store.cache_clear()
        with self.Session() as db:
            self.user = User(username="pdf_ingest_admin", role="admin", is_active=True,
                             password_hash="unused-test-only")
            db.add(self.user)
            db.flush()
            self.user_id = self.user.id
            self.kb = KnowledgeBase(name="pdf-ingest", collection_name="pdf_" + uuid.uuid4().hex,
                                    owner_id=self.user.id, is_public=True)
            db.add(self.kb)
            db.commit()
            self.kb_id = self.kb.id
        self.store = vector_store.get_vector_store(self.kb.collection_name)

    # ---- fixtures ----
    def table_pdf(self, name="tables.pdf"):
        document = pymupdf.open()
        try:
            page = document.new_page()
            page.insert_text((60, 50), "Context before the table.")
            rule_table(page, 60, 80, 3, 3, [["Plan", "Owner", "Budget"],
                                            ["Alpha", "North", "120"],
                                            ["Beta", "", "240"]])
            document.new_page()  # 第 2 页真正空白：跳过但不重编号
            third = document.new_page()
            third.insert_text((60, 60), "Details on the third physical page.")
            rule_table(third, 60, 90, 2, 2, [["Region", "Sales"], ["East", "42"]])
            path = self.root / name
            document.save(path)
        finally:
            document.close()
        return path

    def scan_mixed_pdf(self, name="mixed.pdf"):
        document = pymupdf.open()
        try:
            document.new_page().insert_text((60, 60), "page one text")
            scanned = document.new_page()
            scanned.insert_image(pymupdf.Rect(50, 50, 250, 250), stream=tiny_png())
            document.new_page().insert_text((60, 60), "page three text")
            path = self.root / name
            document.save(path)
        finally:
            document.close()
        return path

    def add_document(self, path: Path, *, ext=".pdf"):
        with self.Session() as db:
            doc = Document(kb_id=self.kb_id, filename=path.name, file_ext=ext,
                           file_size=path.stat().st_size, stored_path=str(path))
            db.add(doc)
            db.commit()
            return doc.id

    # ---- assertions ----
    def snapshot(self, doc_id):
        with self.Session() as db:
            doc = db.get(Document, doc_id)
            chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == doc_id)
                                .order_by(DocumentChunk.chunk_index)).all()
            vectors = db.scalar(select(func.count()).select_from(self.store.EmbeddingStore)) or 0
            return {
                "status": doc.status, "error": doc.error_message, "parse_done": doc.parse_done,
                "chunk_count": doc.chunk_count, "lease": doc.lease_id, "progress": doc.progress,
                "chunks": len(chunks), "embedded": sum(1 for c in chunks if c.embedded),
                "metadata": [dict(chunk.source_metadata or {}) for chunk in chunks],
                "indexes": [chunk.chunk_index for chunk in chunks],
                "contents": [chunk.content for chunk in chunks], "vectors": vectors,
                "retrievable": doc.retrievable,
            }

    def retrieved_ids(self):
        return {doc.metadata["document_id"]
                for doc, _ in rag.retrieve(self.kb.collection_name, "q", 6)}

    def run_ingest(self, doc_id):
        ingest.ingest_document_task(str(doc_id))

    # ---- 成功路径 ----
    def test_tables_pdf_becomes_ready_with_page_and_table_positions(self):
        path = self.table_pdf()
        doc_id = self.add_document(path)
        self.run_ingest(doc_id)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], "ready", state["error"])
        self.assertIsNone(state["error"])
        self.assertTrue(state["parse_done"])
        self.assertTrue(state["retrievable"])
        self.assertEqual(state["progress"], 100)
        self.assertIsNone(state["lease"])
        self.assertGreater(state["chunk_count"], 0)
        self.assertEqual(state["embedded"], state["chunks"])
        self.assertGreater(state["vectors"], 0)
        self.assertIn(str(doc_id), self.retrieved_ids())

        tables = [meta for meta in state["metadata"] if meta.get("table_index")]
        self.assertTrue(tables)
        self.assertEqual({meta["page_start"] for meta in tables}, {1, 3})
        self.assertTrue(all(meta["page_start"] == meta["page_end"] for meta in tables))
        self.assertEqual(sorted({meta["table_index"] for meta in tables}), [1, 2])
        self.assertTrue(all(meta["row_start"] >= 2 for meta in tables))

        joined = "\n".join(state["contents"])
        self.assertIn("Context before the table.", joined)
        self.assertIn("Beta |  | 240", joined)
        self.assertIn("Details on the third physical page.", joined)
        self.assertIn("East | 42", joined)
        # 表头只出现在分块前缀里，不再作为正文行重复。
        self.assertEqual(joined.count("Plan | Owner | Budget"), 1)
        self.assertNotIn("Alpha\nNorth", joined)

        with self.Session() as db:
            index = state["indexes"][state["metadata"].index(tables[0])]
            preview = source_documents.read_source(db, self.kb_id, doc_id, index, 0)
        self.assertEqual(preview["location"]["page_start"], 1)

    def test_table_rows_are_searchable_and_multi_page_tables_keep_positions(self):
        path = self.table_pdf()
        doc_id = self.add_document(path)
        self.run_ingest(doc_id)
        state = self.snapshot(doc_id)
        joined = "\n".join(state["contents"])
        self.assertIn("Beta |  | 240", joined)
        # 逻辑行号：表头是第 1 行、正文从第 2 行起；一行一个 block，合并后仍是真实行范围。
        first_table = [meta for meta in state["metadata"] if meta.get("table_index") == 1]
        self.assertTrue(first_table)
        self.assertEqual((first_table[0]["row_start"], first_table[0]["row_end"]), (2, 3))
        self.assertEqual(first_table[0]["page_start"], 1)
        third_page = [meta for meta in state["metadata"] if meta.get("page_start") == 3]
        self.assertTrue(third_page)
        self.assertTrue(all(meta["page_end"] == 3 for meta in third_page))
        # 空白第 2 页没有产出任何分块，但页号没有被重编。
        self.assertNotIn(2, {meta.get("page_start") for meta in state["metadata"]})

    # ---- 失败路径 ----
    def test_mixed_scan_pdf_fails_with_safe_ocr_hint_and_no_embedding(self):
        path = self.scan_mixed_pdf()
        doc_id = self.add_document(path)
        with self.assertLogs("app.services.ingest", level="ERROR"):
            self.run_ingest(doc_id)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["parse_done"])
        self.assertFalse(state["retrievable"])
        self.assertIsNone(state["lease"])
        self.assertEqual(state["vectors"], 0)
        self.assertEqual(state["embedded"], 0)
        self.assertEqual(self.embeddings.calls, 0)
        message = state["error"]
        self.assertIn("第 2 页", message)
        self.assertIn("OCR", message)
        self.assertNotIn(str(self.root), message)   # 不含路径
        self.assertNotIn("page one text", message)  # 不含原文
        # 前页已落库的分块保留为不可检索的未向量化行，但不发布。
        self.assertGreater(state["chunks"], 0)
        self.assertIn("page one text", "\n".join(state["contents"]))
        self.assertNotIn(str(doc_id), self.retrieved_ids())
        self.assertEqual(self.embeddings.calls, 0)

    def test_unknown_parse_failure_keeps_generic_message_without_paths(self):
        path = self.table_pdf()
        doc_id = self.add_document(path)
        leaked = f"{self.root / 'secret.pdf'} boom"

        def explode(*args, **kwargs):
            raise RuntimeError(leaked)
            yield  # pragma: no cover

        with patch.object(ingest, "iter_structured_chunks", explode):
            with self.assertLogs("app.services.ingest", level="ERROR"):
                self.run_ingest(doc_id)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["parse_done"])
        self.assertEqual(state["error"], "文档处理失败，请联系管理员检查日志")
        self.assertNotIn("secret", state["error"])
        self.assertEqual(state["vectors"], 0)
        self.assertEqual(self.embeddings.calls, 0)
        self.assertNotIn(str(doc_id), self.retrieved_ids())

    def test_image_probe_failure_fails_the_document_instead_of_publishing(self):
        path = self.scan_mixed_pdf()
        doc_id = self.add_document(path)
        with patch.object(pymupdf.Page, "get_image_info",
                          side_effect=RuntimeError("probe failed at /private/secret")):
            with self.assertLogs("app.services.ingest", level="ERROR"):
                self.run_ingest(doc_id)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["parse_done"])
        self.assertEqual(state["error"], "文档处理失败，请联系管理员检查日志")
        self.assertNotIn("private", state["error"])
        self.assertEqual(state["vectors"], 0)
        self.assertEqual(state["embedded"], 0)
        self.assertEqual(self.embeddings.calls, 0)
        self.assertNotIn(str(doc_id), self.retrieved_ids())

    def test_blank_only_pdf_fails_as_empty_without_publishing(self):
        document = pymupdf.open()
        try:
            document.new_page()
            document.new_page()
            path = self.root / "empty.pdf"
            document.save(path)
        finally:
            document.close()
        doc_id = self.add_document(path)
        with self.assertLogs("app.services.ingest", level="ERROR"):
            self.run_ingest(doc_id)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], "failed")
        self.assertFalse(state["parse_done"])
        self.assertIn("全部页面为空", state["error"])
        self.assertEqual(state["vectors"], 0)
        self.assertEqual(self.embeddings.calls, 0)
        self.assertNotIn(str(doc_id), self.retrieved_ids())

    def test_cancel_and_retry_semantics_are_unchanged_for_pdf(self):
        path = self.table_pdf()
        doc_id = self.add_document(path)
        with self.Session() as db:
            db.execute(text("UPDATE documents SET cancel_requested = true WHERE id = :id"),
                       {"id": doc_id})
            db.commit()
        self.run_ingest(doc_id)
        state = self.snapshot(doc_id)
        self.assertEqual(state["status"], "cancelled")
        self.assertFalse(state["parse_done"])
        self.assertEqual(state["vectors"], 0)
        self.assertEqual(self.embeddings.calls, 0)


if __name__ == "__main__":
    unittest.main()
