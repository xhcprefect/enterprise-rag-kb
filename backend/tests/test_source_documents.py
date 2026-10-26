"""引用原文的权限、代次、范围和文件路径边界；无真实数据库。"""
import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1 import knowledge
from app.core.config import settings
from app.services import source_documents as sources


class SourceDocumentTests(unittest.TestCase):
    def setUp(self):
        self.kb_id, self.doc_id = uuid.uuid4(), uuid.uuid4()
        self.doc = SimpleNamespace(id=self.doc_id, kb_id=self.kb_id, status="ready", cancel_requested=False,
                                   index_version=2, filename="law.pdf", file_ext=".pdf", stored_path="unused")
        self.db = MagicMock()
        self.db.scalar.return_value = self.doc
        self.rows = [SimpleNamespace(chunk_index=index, content=f"原文{index}", source_metadata={
            "page_start": index, "page_end": index, "section": "第一章", "stored_path": "not-public",
        }) for index in (2, 3)]
        self.db.scalars.return_value.all.return_value = self.rows

    def test_preview_reads_current_original_with_positions_without_internal_metadata(self):
        before = copy.deepcopy([row.source_metadata for row in self.rows])
        result = sources.read_source(self.db, self.kb_id, self.doc_id, 2, 2, 2, 3)
        self.assertEqual(result["location"], {"page_start": 2, "page_end": 3, "section": "第一章"})
        self.assertEqual([item["content"] for item in result["chunks"]], ["原文2", "原文3"])
        self.assertNotIn("stored_path", str(result))
        self.assertEqual(before, [row.source_metadata for row in self.rows])
        query = self.db.scalars.call_args.args[0]
        sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for condition in ["documents.status = 'ready'", "documents.cancel_requested IS false", "documents.index_version = 2",
                          "document_chunks.embedded IS true", str(self.kb_id)]:
            self.assertIn(condition, sql)

    def test_stale_cancelled_processing_and_missing_sources_are_rejected(self):
        for status, cancelled, version in [("ready", False, 3), ("ready", True, 2), ("processing", False, 2), ("failed", False, 2)]:
            with self.subTest(status=status, cancelled=cancelled, version=version):
                self.doc.status, self.doc.cancel_requested, self.doc.index_version = status, cancelled, version
                with self.assertRaises(HTTPException) as error:
                    sources.read_source(self.db, self.kb_id, self.doc_id, 2, 2)
                self.assertEqual(error.exception.status_code, 409)
        self.db.scalar.return_value = None
        with self.assertRaises(HTTPException) as error:
            sources.read_source(self.db, self.kb_id, self.doc_id, 2, 2)
        self.assertEqual(error.exception.status_code, 404)

    def test_ranges_are_bounded_and_cannot_omit_the_referenced_chunk(self):
        for start, end in [(-1, 2), (3, 4), (0, 6), (3, 1)]:
            with self.subTest(start=start, end=end), self.assertRaises(HTTPException) as error:
                sources.read_source(self.db, self.kb_id, self.doc_id, 2, 2, start, end)
            self.assertEqual(error.exception.status_code, 422)
        self.db.scalars.assert_not_called()

    def test_missing_neighbor_is_not_silently_replaced_by_another_piece(self):
        self.db.scalars.return_value.all.return_value = self.rows[:1]
        with self.assertRaises(HTTPException) as error:
            sources.read_source(self.db, self.kb_id, self.doc_id, 2, 2, 2, 3)
        self.assertEqual(error.exception.status_code, 409)

    def test_preview_is_bounded_and_legacy_location_stays_empty(self):
        self.rows[0].content = "x" * 30000
        self.rows[0].source_metadata = {}
        self.db.scalars.return_value.all.return_value = self.rows[:1]
        result = sources.read_source(self.db, self.kb_id, self.doc_id, 2, 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["location"], {})
        self.assertEqual(len(result["chunks"][0]["content"]), sources.MAX_PREVIEW_CHARS)

    def test_location_whitelist_rejects_invalid_numbers_and_does_not_guess(self):
        result = sources.public_location({"page_start": True, "page_end": 0, "line_start": -1,
                                          "row_start": "2", "sheet": "预算", "section": "x" * 1000})
        self.assertEqual(set(result), {"sheet", "section"})
        self.assertEqual(len(result["section"]), 512)
        self.assertEqual(sources.merge_locations([{"page_start": 1}, {}]), {})

    def test_api_checks_private_knowledge_permission_before_reading_source(self):
        user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
        kb = SimpleNamespace(id=self.kb_id, is_public=False, owner_id=uuid.uuid4())
        with patch.object(knowledge, "_get_kb_or_404", return_value=kb), patch.object(sources, "read_source") as read:
            with self.assertRaises(HTTPException) as error:
                knowledge.get_source(self.kb_id, self.doc_id, 2, 2, None, None, self.db, user)
        self.assertEqual(error.exception.status_code, 404)
        read.assert_not_called()

    def test_pdf_preview_requires_current_version_pdf_type_and_upload_root(self):
        user = SimpleNamespace(id=uuid.uuid4())
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside, \
             patch.object(settings, "UPLOAD_DIR", root), patch.object(knowledge, "_get_kb_for_user"):
            safe = Path(root) / "document.pdf"
            safe.write_bytes(b"%PDF-test")
            self.doc.stored_path = str(safe)
            response = knowledge.get_source_pdf(self.kb_id, self.doc_id, 2, self.db, user)
            self.assertEqual(response.media_type, "application/pdf")
            self.assertEqual(response.headers["cache-control"], "private, no-store")
            unsafe = Path(outside) / "private.pdf"
            unsafe.write_bytes(b"%PDF-outside")
            self.doc.stored_path = str(unsafe)
            with self.assertRaises(HTTPException) as error:
                knowledge.get_source_pdf(self.kb_id, self.doc_id, 2, self.db, user)
            self.assertEqual(error.exception.status_code, 404)
            self.doc.stored_path, self.doc.file_ext = str(safe), ".html"
            with self.assertRaises(HTTPException) as error:
                knowledge.get_source_pdf(self.kb_id, self.doc_id, 2, self.db, user)
            self.assertEqual(error.exception.status_code, 400)
