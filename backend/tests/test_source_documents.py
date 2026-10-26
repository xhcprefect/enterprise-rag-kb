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

    def make_pdf(self, pages):
        import pymupdf
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        patcher = patch.object(settings, "UPLOAD_DIR", root.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        path = Path(root.name) / "legacy.pdf"
        with pymupdf.open() as pdf:
            for text in pages:
                page = pdf.new_page()
                if text:
                    page.insert_text((50, 50), text)
            pdf.save(path)
            contents = [page.get_text("text").strip() for page in pdf]
        self.doc.stored_path, self.doc.index_version = str(path), 0
        return contents

    def legacy_preview(self, content, chunk_index=38):
        self.rows = [SimpleNamespace(chunk_index=chunk_index, content=content, source_metadata={})]
        self.db.scalars.return_value.all.return_value = self.rows
        return sources.read_source(self.db, self.kb_id, self.doc_id, chunk_index, 0)

    def test_screenshot_legacy_marker_restores_physical_page_21_without_reindexing(self):
        pages = self.make_pdf([f"Physical page {number}. Unique article {number:03d}. Evidence for this page." for number in range(1, 89)])
        result = self.legacy_preview("<!-- page 21 -->\n" + pages[20])
        self.assertEqual(result["location"], {"page_start": 21, "page_end": 21})
        self.assertEqual(result["chunks"][0]["location"], result["location"])
        self.assertEqual(result["index_version"], 0)
        self.assertEqual(self.rows[0].source_metadata, {})
        self.db.commit.assert_not_called()

    def test_legacy_content_without_its_own_marker_uses_unique_original_text(self):
        pages = self.make_pdf([f"Physical page {number}. Unique article {number:03d}. Evidence for this page." for number in range(1, 4)])
        result = self.legacy_preview(pages[1][10:])
        self.assertEqual(result["location"], {"page_start": 2, "page_end": 2})

    def test_legacy_chunk_before_a_page_marker_retains_the_actual_previous_page(self):
        pages = self.make_pdf([f"Physical page {number}. Unique article {number:03d}. Evidence for this page." for number in range(1, 4)])
        result = self.legacy_preview(pages[0][-24:] + "\n<!-- page 2 -->\n" + pages[1][:40])
        self.assertEqual(result["location"], {"page_start": 1, "page_end": 2})

    def test_wrong_page_markers_and_ambiguous_text_never_guess_a_page(self):
        pages = self.make_pdf([f"Repeated heading.\nUnique article {number:03d}." for number in range(1, 4)])
        for content in ["<!-- page 99 -->\n" + pages[1], "<!-- page 3 -->\n" + pages[1], "Repeated heading."]:
            with self.subTest(content=content):
                self.assertEqual(self.legacy_preview(content)["location"], {})
        self.assertEqual(self.legacy_preview("<!-- page 2 -->\nRepeated heading.")["location"],
                         {"page_start": 2, "page_end": 2})

    def test_blank_pdf_pages_do_not_shift_the_recovered_physical_page(self):
        pages = self.make_pdf(["", "Unique evidence on the second physical page.", ""])
        self.assertEqual(self.legacy_preview("<!-- page 2 -->\n" + pages[1])["location"],
                         {"page_start": 2, "page_end": 2})

    def test_modern_page_metadata_skips_legacy_pdf_scanning(self):
        with patch.object(sources, "_recover_pdf_locations") as lookup:
            result = sources.read_source(self.db, self.kb_id, self.doc_id, 2, 2, 2, 3)
        lookup.assert_not_called()
        self.assertEqual(result["location"]["page_start"], 2)

    def test_legacy_pdf_lookup_limits_preserve_text_preview_without_false_positions(self):
        pages = self.make_pdf(["Unique first physical page.", "Unique second physical page."])
        for limit in ["MAX_PAGE_LOOKUP_PAGES", "MAX_PAGE_LOOKUP_CHARS"]:
            with self.subTest(limit=limit), patch.object(sources, limit, 1):
                result = self.legacy_preview("<!-- page 2 -->\n" + pages[1])
                self.assertEqual(result["location"], {})
                self.assertIn("second physical", result["chunks"][0]["content"])

    def test_legacy_lookup_cannot_read_a_pdf_outside_the_upload_root(self):
        import pymupdf
        pages = self.make_pdf(["Unique evidence inside an otherwise valid PDF."])
        with tempfile.TemporaryDirectory() as other_root, patch.object(settings, "UPLOAD_DIR", other_root), \
             patch.object(pymupdf, "open") as open_pdf:
            self.assertEqual(self.legacy_preview("<!-- page 1 -->\n" + pages[0])["location"], {})
        open_pdf.assert_not_called()

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
