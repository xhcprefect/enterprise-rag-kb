"""资料更新差异预览：纯函数测试（不依赖数据库或网络）。

覆盖拆分（CRLF/空行/中文/重复行/XSS 原样）、对齐与 summary、上限判断、
不可比较原因、以及预览指纹的稳定性与敏感性。
"""
import re
import unittest
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app.services import document_comparison as dc

_NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def document(**overrides):
    base = {
        "id": uuid.uuid4(),
        "kb_id": uuid.uuid4(),
        "filename": "a.txt",
        "status": "ready",
        "cancel_requested": False,
        "index_version": 0,
        "governance_revision": 0,
        "chunk_count": 0,
        "char_count": 0,
        "enabled": True,
        "valid_from": None,
        "valid_until": None,
        "superseded_at": None,
        "superseded_by_id": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def chunk(index, content, *, embedded=True, metadata=None):
    return SimpleNamespace(
        chunk_index=index, content=content, embedded=embedded,
        source_metadata=metadata if metadata is not None else {},
    )


def units(*texts, chunk_index=0, metadata=None):
    return [
        {"chunk_index": chunk_index, "line_index": index, "text": text,
         "location": dict(metadata or {})}
        for index, text in enumerate(texts)
    ]


class SplitUnitsTests(unittest.TestCase):
    def test_splitlines_normalizes_crlf_cr_and_keeps_internal_blank_lines(self):
        units_ = dc.split_units([chunk(0, "甲\r\n\r\n乙\r丙\n丁")])
        self.assertEqual([unit["text"] for unit in units_], ["甲", "", "乙", "丙", "丁"])
        self.assertEqual([unit["line_index"] for unit in units_], [0, 1, 2, 3, 4])
        self.assertEqual({unit["chunk_index"] for unit in units_}, {0})

    def test_units_do_not_merge_across_chunks_or_drop_duplicate_lines(self):
        chunks = [chunk(0, "same\nsame"), chunk(1, "same"), chunk(2, "尾部\n")]
        units_ = dc.split_units(chunks)
        self.assertEqual([unit["text"] for unit in units_], ["same", "same", "same", "尾部"])
        self.assertEqual([unit["chunk_index"] for unit in units_], [0, 0, 1, 2])
        self.assertEqual([unit["line_index"] for unit in units_], [0, 1, 0, 0])

    def test_chinese_and_xss_text_are_preserved_verbatim(self):
        payload = '<script>alert("x")</script> & 中文'
        units_ = dc.split_units([chunk(0, payload)])
        self.assertEqual(units_[0]["text"], payload)

    def test_location_uses_public_location_and_is_copied_per_unit(self):
        metadata = {"page_start": 3, "section": "第一章", "internal_path": "/secret"}
        units_ = dc.split_units([chunk(0, "a\nb", metadata=metadata)])
        self.assertEqual(units_[0]["location"], {"page_start": 3, "section": "第一章"})
        self.assertEqual(units_[1]["location"], {"page_start": 3, "section": "第一章"})
        self.assertIsNot(units_[0]["location"], units_[1]["location"])

    def test_empty_content_produces_zero_units_but_single_newline_produces_one_blank(self):
        self.assertEqual(dc.split_units([chunk(0, "")]), [])
        blank = dc.split_units([chunk(0, "\n")])
        self.assertEqual([unit["text"] for unit in blank], [""])


class AlignUnitsTests(unittest.TestCase):
    def test_added_removed_modified_unchanged_and_summary(self):
        left = units("a", "b", "c")
        right = units("a", "B", "c")
        rows, summary = dc.align_units(left, right)
        self.assertEqual([row["kind"] for row in rows], ["unchanged", "modified", "unchanged"])
        self.assertEqual(summary, {"added": 0, "removed": 0, "modified": 1, "unchanged": 2})
        self.assertEqual(rows[1]["original"]["text"], "b")
        self.assertEqual(rows[1]["replacement"]["text"], "B")

        rows, summary = dc.align_units(units("a", "b", "c"), units("a", "c"))
        self.assertEqual([row["kind"] for row in rows], ["unchanged", "removed", "unchanged"])
        self.assertEqual(summary, {"added": 0, "removed": 1, "modified": 0, "unchanged": 2})

        rows, summary = dc.align_units(units("a", "c"), units("a", "c", "d"))
        self.assertEqual([row["kind"] for row in rows], ["unchanged", "unchanged", "added"])
        self.assertEqual(summary, {"added": 1, "removed": 0, "modified": 0, "unchanged": 2})

    def test_replace_pairs_min_and_then_extra_removed_added(self):
        rows, summary = dc.align_units(units("old-1", "old-2", "old-3"), units("new-1"))
        self.assertEqual([row["kind"] for row in rows], ["modified", "removed", "removed"])
        self.assertEqual(summary, {"added": 0, "removed": 2, "modified": 1, "unchanged": 0})
        self.assertEqual(rows[0]["original"]["text"], "old-1")
        self.assertEqual(rows[0]["replacement"]["text"], "new-1")

        rows, summary = dc.align_units(units("old-1"), units("new-1", "new-2", "new-3"))
        self.assertEqual([row["kind"] for row in rows], ["modified", "added", "added"])
        self.assertEqual(summary, {"added": 2, "removed": 0, "modified": 1, "unchanged": 0})

    def test_no_changes_is_all_unchanged_and_empty_is_empty(self):
        source = units("same", "same")
        rows, summary = dc.align_units(source, source)
        self.assertEqual([row["kind"] for row in rows], ["unchanged", "unchanged"])
        self.assertEqual(summary, {"added": 0, "removed": 0, "modified": 0, "unchanged": 2})
        self.assertEqual(dc.align_units([], []), ([], {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}))

    def test_summary_always_matches_row_kinds(self):
        rows, summary = dc.align_units(
            units("a", "b", "b", "c"), units("a", "x", "b", "d", "e"))
        counts = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
        for row in rows:
            counts[row["kind"]] += 1
        self.assertEqual(summary, counts)
        self.assertEqual(sum(summary.values()), len(rows))


class ReasonTests(unittest.TestCase):
    def test_readiness_reasons(self):
        self.assertIsNone(dc.readiness_reason(document()))
        for overrides in (
            {"status": "pending"}, {"status": "failed"}, {"cancel_requested": True},
            {"cancel_requested": None}, {"cancel_requested": 0},
            {"index_version": -1}, {"index_version": True}, {"index_version": 1.0},
            {"index_version": None},
        ):
            self.assertEqual(dc.readiness_reason(document(**overrides)),
                             dc.REASON_NOT_READY, str(overrides))

    def test_precheck_original_may_be_superseded_but_replacement_must_be_active(self):
        original = document(superseded_at=_NOW)
        replacement = document()
        self.assertIsNone(dc.precheck_reason(original, replacement, now=_NOW))
        for overrides in ({"enabled": False}, {"valid_from": datetime(2999, 1, 1, tzinfo=timezone.utc)},
                          {"valid_until": datetime(2000, 1, 1, tzinfo=timezone.utc)},
                          {"superseded_at": _NOW}):
            self.assertEqual(
                dc.precheck_reason(original, document(**overrides), now=_NOW),
                dc.REASON_NOT_ELIGIBLE, str(overrides))
        # not_ready 优先于 not_eligible
        self.assertEqual(
            dc.precheck_reason(document(status="pending"), document(enabled=False), now=_NOW),
            dc.REASON_NOT_READY)

    def test_chunk_layout_reasons(self):
        ok = document(chunk_count=2)
        self.assertIsNone(dc.chunk_layout_reason(ok, [0, 1], [True, True]))
        self.assertEqual(dc.chunk_layout_reason(document(chunk_count=0), [], []),
                         dc.REASON_EMPTY_DOCUMENT)
        self.assertEqual(dc.chunk_layout_reason(ok, [], []), dc.REASON_EMPTY_DOCUMENT)
        self.assertEqual(dc.chunk_layout_reason(ok, [0], [True]), dc.REASON_INCOMPLETE)
        self.assertEqual(dc.chunk_layout_reason(ok, [1, 2], [True, True]), dc.REASON_INCOMPLETE)
        self.assertEqual(dc.chunk_layout_reason(ok, [0, 2], [True, True]), dc.REASON_INCOMPLETE)
        self.assertEqual(dc.chunk_layout_reason(ok, [0, 1], [True, False]), dc.REASON_INCOMPLETE)
        self.assertEqual(dc.chunk_layout_reason(ok, [0, 1], [True, 1]), dc.REASON_INCOMPLETE)

    def test_limit_reasons_and_unit_limit(self):
        self.assertEqual(dc.chunk_limit_reason(dc.MAX_CHUNKS_PER_DOCUMENT, dc.MAX_CHARS_PER_DOCUMENT), None)
        self.assertEqual(dc.chunk_limit_reason(dc.MAX_CHUNKS_PER_DOCUMENT + 1, 0),
                         dc.REASON_TOO_MANY_CHUNKS)
        self.assertEqual(dc.chunk_limit_reason(1, dc.MAX_CHARS_PER_DOCUMENT + 1),
                         dc.REASON_TOO_MANY_CHARS)
        self.assertFalse(dc.units_limit_exceeded(dc.MAX_UNITS_PER_DOCUMENT))
        self.assertTrue(dc.units_limit_exceeded(dc.MAX_UNITS_PER_DOCUMENT + 1))
        many = [chunk(index, "a\nb\nc") for index in range(1000)]
        self.assertEqual(dc.count_units(many), 3000)
        self.assertTrue(dc.units_limit_exceeded(dc.count_units(many)))


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.original = document(filename="old.txt")
        self.replacement = document(filename="new.txt")
        self.original_chunks = [chunk(0, "第一行\n第二行"), chunk(1, "旧内容")]
        self.replacement_chunks = [chunk(0, "第一行\n第二行改"), chunk(1, "新内容")]

    def fingerprint(self, original=None, replacement=None, left=None, right=None):
        return dc.comparison_fingerprint(
            original or self.original,
            replacement or self.replacement,
            self.original_chunks if left is None else left,
            self.replacement_chunks if right is None else right,
        )

    def test_fingerprint_is_stable_lowercase_hex_and_hides_content(self):
        first = self.fingerprint()
        self.assertRegex(first, re.compile(r"^[a-f0-9]{64}$"))
        self.assertEqual(first, self.fingerprint())
        self.assertNotIn("第一行", first)

    def test_fingerprint_changes_with_content_index_version_revision_and_enabled(self):
        base = self.fingerprint()
        changed_content = [chunk(0, "第一行\n第二行"), chunk(1, "被篡改")]
        self.assertNotEqual(base, self.fingerprint(left=changed_content))
        self.assertNotEqual(base, self.fingerprint(original=document(
            id=self.original.id, kb_id=self.original.kb_id, filename="old.txt", index_version=1)))
        self.assertNotEqual(base, self.fingerprint(original=document(
            id=self.original.id, kb_id=self.original.kb_id, filename="old.txt", governance_revision=1)))
        self.assertNotEqual(base, self.fingerprint(replacement=document(
            id=self.replacement.id, kb_id=self.replacement.kb_id, filename="new.txt", enabled=False)))
        self.assertNotEqual(base, self.fingerprint(original=document(id=self.original.id, kb_id=self.original.kb_id,
                                                                     filename="old.txt", valid_from=_NOW)))
        self.assertNotEqual(base, self.fingerprint(original=document(id=uuid.uuid4(), kb_id=self.original.kb_id,
                                                                     filename="old.txt")))

    def test_fingerprint_ignores_filename_metadata_and_chunk_location(self):
        base = self.fingerprint()
        self.assertEqual(base, self.fingerprint(original=document(
            id=self.original.id, kb_id=self.original.kb_id, filename="renamed.txt")))
        left_with_metadata = [chunk(0, "第一行\n第二行", metadata={"page_start": 9}),
                              chunk(1, "旧内容", metadata={"section": "x"})]
        self.assertEqual(base, self.fingerprint(left=left_with_metadata))

    def test_fingerprint_covers_supersession_and_times(self):
        base = self.fingerprint()
        self.assertNotEqual(base, self.fingerprint(original=document(
            id=self.original.id, kb_id=self.original.kb_id, filename="old.txt",
            superseded_at=_NOW)))
        self.assertNotEqual(base, self.fingerprint(original=document(
            id=self.original.id, kb_id=self.original.kb_id, filename="old.txt",
            valid_until=_NOW)))

    def test_require_fingerprint_shape(self):
        valid = "a" * 64
        self.assertEqual(dc.require_fingerprint(valid), valid)
        from fastapi import HTTPException

        for bad in ("A" * 64, "a" * 63, "g" * 64, 123, None, "a" * 65):
            with self.assertRaises(HTTPException) as error:
                dc.require_fingerprint(bad)
            self.assertEqual(error.exception.status_code, 422)


class BuildComparisonOrchestrationTests(unittest.TestCase):
    """用替身锁/扫描函数验证编排：不可比较时不泄漏部分 diff。"""

    def setUp(self):
        self.original = document(chunk_count=2, char_count=4)
        self.replacement = document(chunk_count=2, char_count=4)
        self.original_chunks = [chunk(0, "a\nb"), chunk(1, "c")]
        self.replacement_chunks = [chunk(0, "a\nB"), chunk(1, "c")]

    def _build(self, original=None, replacement=None, scans=None):
        with patch.object(dc, "_lock_documents_for_read",
                          return_value=(original or self.original, replacement or self.replacement)):
            if scans is not None:
                with patch.object(dc, "scan_document", side_effect=scans):
                    return dc.build_comparison(
                        object(), uuid.uuid4(), self.original.id, self.replacement.id)
            return dc.build_comparison(
                object(), uuid.uuid4(), self.original.id, self.replacement.id)

    def test_comparable_payload_contains_rows_summary_and_fingerprint(self):
        payload = self._build(scans=[(None, self.original_chunks), (None, self.replacement_chunks)])
        self.assertTrue(payload["comparable"])
        self.assertEqual(payload["reason"], "ok")
        self.assertEqual(payload["comparison_version"], 1)
        self.assertEqual(payload["basis"], "ingested_chunks")
        self.assertEqual(payload["summary"],
                         {"added": 0, "removed": 0, "modified": 1, "unchanged": 2})
        self.assertEqual([row["kind"] for row in payload["rows"]],
                         ["unchanged", "modified", "unchanged"])
        self.assertRegex(payload["comparison_fingerprint"], r"^[a-f0-9]{64}$")

    def test_not_ready_payload_has_no_partial_diff(self):
        payload = self._build(original=document(status="pending"))
        self.assertFalse(payload["comparable"])
        self.assertEqual(payload["reason"], "not_ready")
        self.assertEqual(payload["rows"], [])
        self.assertIsNone(payload["summary"])
        self.assertIsNone(payload["comparison_fingerprint"])

    def test_not_eligible_payload_has_no_partial_diff(self):
        payload = self._build(replacement=document(enabled=False))
        self.assertFalse(payload["comparable"])
        self.assertEqual(payload["reason"], "not_eligible")
        self.assertEqual(payload["rows"], [])
        self.assertIsNone(payload["summary"])
        self.assertIsNone(payload["comparison_fingerprint"])

    def test_too_many_units_payload_has_no_partial_diff(self):
        huge = [chunk(index, "a\nb\nc") for index in range(1000)]
        payload = self._build(scans=[(None, huge), (None, self.replacement_chunks)])
        self.assertFalse(payload["comparable"])
        self.assertEqual(payload["reason"], "too_many_units")
        self.assertEqual(payload["rows"], [])
        self.assertIsNone(payload["summary"])
        self.assertIsNone(payload["comparison_fingerprint"])

    def test_scan_failure_reason_is_propagated_without_rows(self):
        payload = self._build(scans=[(None, self.original_chunks), (dc.REASON_INCOMPLETE, [])])
        self.assertFalse(payload["comparable"])
        self.assertEqual(payload["reason"], "incomplete")
        self.assertEqual(payload["rows"], [])
        self.assertIsNone(payload["comparison_fingerprint"])

    def test_self_comparison_is_400_before_touching_database(self):
        from fastapi import HTTPException

        doc_id = uuid.uuid4()
        with self.assertRaises(HTTPException) as error:
            dc.build_comparison(object(), uuid.uuid4(), doc_id, doc_id)
        self.assertEqual(error.exception.status_code, 400)

    def test_non_uuid_ids_are_rejected(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as error:
            dc.build_comparison(object(), uuid.uuid4(), "not-a-uuid", uuid.uuid4())
        self.assertEqual(error.exception.status_code, 422)


class EnsureCurrentFingerprintTests(unittest.TestCase):
    """supersede 前的指纹复核：通过才继续，任何变化都 409 且不碰文档。"""

    def setUp(self):
        self.original = document(chunk_count=2, char_count=4)
        self.replacement = document(chunk_count=2, char_count=4)
        self.original_chunks = [chunk(0, "a\nb"), chunk(1, "c")]
        self.replacement_chunks = [chunk(0, "a\nB"), chunk(1, "c")]
        self.fingerprint = dc.comparison_fingerprint(
            self.original, self.replacement, self.original_chunks, self.replacement_chunks)

    def test_matching_fingerprint_passes(self):
        with patch.object(dc, "scan_document",
                          side_effect=[(None, self.original_chunks), (None, self.replacement_chunks)]):
            dc.ensure_current_fingerprint(
                object(), self.original, self.replacement, self.fingerprint, now=_NOW)

    def test_changed_content_or_governance_is_409(self):
        from fastapi import HTTPException

        changed_chunks = [chunk(0, "a\n被修改"), chunk(1, "c")]
        with patch.object(dc, "scan_document",
                          side_effect=[(None, self.original_chunks), (None, changed_chunks)]):
            with self.assertRaises(HTTPException) as error:
                dc.ensure_current_fingerprint(
                    object(), self.original, self.replacement, self.fingerprint, now=_NOW)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail, dc.STALE_FINGERPRINT_DETAIL)

        changed_replacement = SimpleNamespace(**{**vars(self.replacement), "governance_revision": 1})
        with patch.object(dc, "scan_document",
                          side_effect=[(None, self.original_chunks), (None, self.replacement_chunks)]):
            with self.assertRaises(HTTPException) as error:
                dc.ensure_current_fingerprint(
                    object(), self.original, changed_replacement, self.fingerprint, now=_NOW)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail, dc.STALE_FINGERPRINT_DETAIL)

    def test_not_ready_or_inactive_is_409_without_scanning(self):
        from fastapi import HTTPException

        pending = document(status="pending")
        with patch.object(dc, "scan_document", side_effect=AssertionError("must not scan")):
            with self.assertRaises(HTTPException) as error:
                dc.ensure_current_fingerprint(
                    object(), pending, self.replacement, self.fingerprint, now=_NOW)
        self.assertEqual(error.exception.status_code, 409)

        inactive = document(enabled=False)
        with patch.object(dc, "scan_document", side_effect=AssertionError("must not scan")):
            with self.assertRaises(HTTPException) as error:
                dc.ensure_current_fingerprint(
                    object(), self.original, inactive, self.fingerprint, now=_NOW)
        self.assertEqual(error.exception.status_code, 409)

    def test_malformed_fingerprint_is_422(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as error:
            dc.ensure_current_fingerprint(
                object(), self.original, self.replacement, "NOT-A-FINGERPRINT", now=_NOW)
        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
