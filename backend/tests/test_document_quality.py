"""入库质量预览：纯函数汇总/问题排序，以及可选的真实数据库分页测试。"""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._env import TEST_URL

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.services import document_quality as dq


def chunk(content, index=0, embedded=True, metadata=None):
    return SimpleNamespace(chunk_index=index, content=content,
                           embedded=embedded, source_metadata=metadata or {})


def document(**overrides):
    base = {
        "id": uuid.uuid4(), "filename": "a.pdf", "file_ext": ".pdf", "status": "ready",
        "error_message": None, "index_version": 1, "parse_done": True,
        "cancel_requested": False, "chunk_count": 0, "char_count": 0, "stored_path": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class QualityStatsTests(unittest.TestCase):
    def test_stats_cover_empty_short_duplicate_replacement_control_oversized_and_positions(self):
        duplicate = "重复内容" + "d" * 60
        long_text = "x" * (dq.oversized_threshold() + 1)
        rows = [
            chunk("", 0),
            chunk("   ", 1),
            chunk(duplicate + "\r\n", 2),
            chunk(duplicate + "\n", 3),
            chunk("bad�" + "r" * 60, 4),
            chunk("ctrl\x00" + "c" * 60, 5),
            chunk(long_text, 6),
            chunk("y" * 60, 7, metadata={
                "page_start": 1, "section": "第一章", "line_start": 3,
                "sheet": "Sheet1", "table_index": 0,
            }),
        ]
        stats = dq.compute_stats(rows)
        self.assertEqual(stats["empty_chunks"], 2)
        self.assertEqual(stats["short_chunks"], 0)
        # 两个空块 strip 后同组，按契约“多余份数总和”也计 1；重复正文再计 1。
        self.assertEqual(stats["duplicate_chunks"], 2)
        self.assertEqual(stats["replacement_chunks"], 1)
        self.assertEqual(stats["control_chunks"], 1)
        self.assertEqual(stats["oversized_chunks"], 1)
        self.assertEqual(stats["min_chars"], 0)
        self.assertEqual(stats["max_chars"], len(long_text))
        self.assertEqual(stats["avg_chars"], round(sum(len(item.content) for item in rows) / len(rows)))
        self.assertEqual(stats["page_chunks"], 1)
        self.assertEqual(stats["section_chunks"], 1)
        self.assertEqual(stats["line_chunks"], 1)
        self.assertEqual(stats["sheet_chunks"], 1)
        self.assertEqual(stats["table_chunks"], 1)

    def test_newline_tab_are_not_control_characters(self):
        self.assertFalse(dq.has_control_chars("a\nb\tc\rd"))
        self.assertTrue(dq.has_control_chars("a\x00b"))
        self.assertTrue(dq.has_control_chars("a\x07b"))

    def test_empty_sample_stats_are_zero(self):
        stats = dq.compute_stats([])
        self.assertEqual(set(stats.values()), {0})

    def test_lone_carriage_return_is_normalized_for_duplicate_detection(self):
        rows = [chunk("same\r", 0), chunk("same\n", 1), chunk("same", 2)]
        self.assertEqual(dq.compute_stats(rows)["duplicate_chunks"], 2)


class QualityIssueTests(unittest.TestCase):
    def test_issue_order_is_severity_then_fixed_code_order(self):
        doc = document(status="failed", file_ext=".pdf")
        rows = [chunk("", 0), chunk("short", 2)]
        stats = dq.compute_stats(rows)
        issues = dq.collect_issues(
            doc, stats, scanned_chunks=3, chunk_total=4, unembedded_count=1,
            file_exists=False, chunk_indexes=[0, 2],
        )
        self.assertEqual([item["code"] for item in issues], [
            "file_missing", "document_failed", "empty_chunks",
            "chunk_index_gaps", "short_chunks", "sample_truncated",
        ])
        self.assertEqual([item["severity"] for item in issues],
                         ["error", "error", "warning", "warning", "info", "info"])
        by_code = {item["code"]: item for item in issues}
        self.assertEqual(by_code["short_chunks"]["details"], {"threshold_chars": 50})
        self.assertEqual(by_code["chunk_index_gaps"]["count"], 1)
        self.assertEqual(by_code["sample_truncated"]["count"], 1)

    def test_ready_pdf_with_missing_page_metadata_reports_difference(self):
        doc = document(status="ready", file_ext="pdf")
        rows = [chunk("a" * 60, 0, metadata={}), chunk("b" * 60, 1, metadata={"page_start": 2})]
        stats = dq.compute_stats(rows)
        issues = dq.collect_issues(doc, stats, scanned_chunks=2, chunk_total=2,
                                   unembedded_count=0, file_exists=True, chunk_indexes=[0, 1])
        self.assertEqual([item["code"] for item in issues], ["missing_page_metadata"])
        self.assertEqual(issues[0]["count"], 1)

    def test_no_chunks_and_vector_incomplete_issues(self):
        ready = document(status="ready", file_ext="txt")
        no_chunks = dq.collect_issues(ready, dq.compute_stats([]), scanned_chunks=0,
                                      chunk_total=0, unembedded_count=0, file_exists=True)
        self.assertEqual([item["code"] for item in no_chunks], ["no_chunks"])

        rows = [chunk("a" * 60, 0)]
        incomplete = dq.collect_issues(ready, dq.compute_stats(rows), scanned_chunks=1,
                                       chunk_total=3, unembedded_count=2, file_exists=True,
                                       chunk_indexes=[0])
        self.assertIn("vector_incomplete", [item["code"] for item in incomplete])
        self.assertEqual({item["code"]: item for item in incomplete}["vector_incomplete"]["count"], 2)

    def test_pending_document_reports_not_ready_and_parse_incomplete(self):
        doc = document(status="pending", parse_done=False, file_ext="txt")
        issues = dq.collect_issues(doc, dq.compute_stats([]), scanned_chunks=0,
                                   chunk_total=0, unembedded_count=0, file_exists=True)
        self.assertEqual([item["code"] for item in issues],
                         ["document_not_ready", "parse_incomplete"])

    def test_chunk_index_gaps_formula_ignores_bool_and_negative(self):
        self.assertEqual(dq.chunk_index_gaps([0, 1, 4]), 2)
        self.assertEqual(dq.chunk_index_gaps([2]), 2)
        self.assertEqual(dq.chunk_index_gaps([]), 0)
        self.assertEqual(dq.chunk_index_gaps([True, "x", -1]), 0)


class QualityPayloadTests(unittest.TestCase):
    def test_payload_never_contains_chunk_body(self):
        doc = document(filename="a.pdf", file_ext=".PDF", chunk_count=2, char_count=2400)
        rows = [
            chunk("SECRET_BODY_ONE" * 10, 0, metadata={"page_start": 1}),
            chunk("SECRET_BODY_TWO" * 10, 1, metadata={"page_start": 2}),
        ]
        payload = dq.build_payload(doc, rows, chunk_total=2, embedded_count=1,
                                   unembedded_count=1, file_exists=True)
        serialized = json.dumps(payload, default=str)
        self.assertNotIn("SECRET_BODY", serialized)
        self.assertEqual(payload["file_ext"], "pdf")
        self.assertEqual(payload["document_id"], doc.id)
        self.assertEqual(payload["scanned_chunks"], 2)
        self.assertEqual(payload["scan_limit"], 2000)
        self.assertFalse(payload["scan_truncated"])
        self.assertEqual(set(payload["stats"]), {
            "empty_chunks", "short_chunks", "duplicate_chunks", "replacement_chunks",
            "control_chunks", "oversized_chunks", "min_chars", "max_chars", "avg_chars",
            "page_chunks", "section_chunks", "line_chunks", "sheet_chunks", "table_chunks",
        })
        self.assertIn("vector_incomplete", [item["code"] for item in payload["issues"]])

    def test_scan_truncation_sets_flag_and_info_issue(self):
        doc = document(file_ext="txt")
        rows = [chunk("a" * 60, index) for index in range(3)]
        payload = dq.build_payload(doc, rows, chunk_total=5, embedded_count=3,
                                   unembedded_count=2, file_exists=True)
        self.assertTrue(payload["scan_truncated"])
        self.assertIn("sample_truncated", [item["code"] for item in payload["issues"]])


if TEST_URL:
    from sqlalchemy import create_engine

    from app.db import session as db_session

    _target = make_url(TEST_URL)
    if db_session.engine.url != _target:
        db_session.engine.dispose()
        db_session.engine = create_engine(TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
        db_session.SessionLocal.configure(bind=db_session.engine)

    from app.api.v1 import knowledge
    from app.db.init_db import init_db
    from app.models import Document, DocumentChunk, KnowledgeBase, User

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class QualityDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests import _memory_project_test_gate as gate

        if engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        from tests import _memory_project_test_gate as gate

        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, documents, conversations, upload_sessions, "
                "audit_logs, document_chunks RESTART IDENTITY CASCADE"
            ))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(settings, "UPLOAD_DIR", self.temp.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        with SessionLocal() as db:
            admin = User(username="quality_admin", role="admin", is_active=True, password_hash="x")
            db.add(admin)
            db.flush()
            kb = KnowledgeBase(name="quality-kb", collection_name="q_" + uuid.uuid4().hex,
                               owner_id=admin.id, is_public=True)
            project = KnowledgeBase(name="quality-project", collection_name="q_" + uuid.uuid4().hex,
                                    owner_id=admin.id, is_public=False, kind="project")
            db.add_all([kb, project])
            db.commit()
            self.admin_id, self.kb_id, self.project_id = admin.id, kb.id, project.id

    def add_document(self, *, create_file=True, **overrides):
        with SessionLocal() as db:
            doc_id = uuid.uuid4()
            path = Path(self.temp.name) / str(self.kb_id) / f"{doc_id}.txt"
            if create_file:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("body", encoding="utf-8")
            values = {
                "id": doc_id, "kb_id": self.kb_id, "filename": "quality.txt", "file_ext": ".txt",
                "file_size": 4, "stored_path": str(path), "status": "ready",
                "chunk_count": 3, "char_count": 1530, **overrides,
            }
            db.add(Document(**values))
            db.commit()
            return doc_id

    def add_chunks(self, document_id, definitions):
        with SessionLocal() as db:
            for index, content, metadata, embedded in definitions:
                db.add(DocumentChunk(document_id=document_id, chunk_index=index, content=content,
                                     source_metadata=metadata, search_text="", embedded=embedded))
            db.commit()

    def admin(self, db):
        return db.get(User, self.admin_id)

    def test_quality_summary_pagination_and_project_rejection(self):
        doc_id = self.add_document()
        self.add_chunks(doc_id, [
            (0, "A" * 1500, {"page_start": 2, "page_end": 2, "hidden": "x"}, True),
            (1, "B" * 10, {}, True),
            (2, "C" * 20, {}, False),
        ])
        with SessionLocal() as db:
            admin = self.admin(db)
            data = knowledge.get_document_quality(self.kb_id, doc_id, db, admin).data
            self.assertEqual(data["document_id"], doc_id)
            self.assertEqual(data["scanned_chunks"], 3)
            self.assertEqual(data["embedded_count"], 2)
            self.assertEqual(data["unembedded_count"], 1)
            self.assertTrue(data["file_exists"])
            self.assertEqual(data["stats"]["min_chars"], 10)
            self.assertEqual(data["stats"]["max_chars"], 1500)
            codes = [item["code"] for item in data["issues"]]
            self.assertIn("vector_incomplete", codes)
            self.assertNotIn("A" * 100, json.dumps(data, default=str))

            first = knowledge.get_document_chunks(self.kb_id, doc_id, 0, 1, db, admin).data
            self.assertEqual(first["total"], 3)
            self.assertEqual(first["offset"], 0)
            self.assertEqual(first["limit"], 1)
            self.assertEqual(len(first["items"]), 1)
            item = first["items"][0]
            self.assertEqual(item["chunk_index"], 0)
            self.assertEqual(len(item["content"]), 1200)
            self.assertEqual(item["content_chars"], 1500)
            self.assertTrue(item["truncated"])
            self.assertEqual(item["location"], {"page_start": 2, "page_end": 2})
            self.assertNotIn("hidden", item["location"])

            second = knowledge.get_document_chunks(self.kb_id, doc_id, 1, 50, db, admin).data
            self.assertEqual([entry["chunk_index"] for entry in second["items"]], [1, 2])
            self.assertFalse(second["items"][0]["truncated"])

            with self.assertRaises(HTTPException) as caught:
                knowledge.get_document_quality(self.project_id, doc_id, db, admin)
            self.assertEqual(caught.exception.status_code, 400)
            with self.assertRaises(HTTPException) as caught:
                knowledge.get_document_chunks(self.project_id, doc_id, 0, 20, db, admin)
            self.assertEqual(caught.exception.status_code, 400)

    def test_missing_file_is_reported_without_exposing_path(self):
        doc_id = self.add_document(create_file=False)
        self.add_chunks(doc_id, [(0, "A" * 60, {}, True)])
        with SessionLocal() as db:
            data = knowledge.get_document_quality(self.kb_id, doc_id, db, self.admin(db)).data
        self.assertFalse(data["file_exists"])
        self.assertIn("file_missing", [item["code"] for item in data["issues"]])
        self.assertNotIn("stored_path", data)
        self.assertNotIn(self.temp.name, json.dumps(data, default=str))


if __name__ == "__main__":
    unittest.main()
