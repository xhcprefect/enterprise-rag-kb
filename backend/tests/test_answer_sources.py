"""T3 回答来源快照：严格解析、去重、trace/provenance 读取与 SQL 形状（纯单测，不连库）。

真实 PostgreSQL 复核/锁顺序回归只在 ``TEST_DATABASE_URL`` 存在时尝试；本机无库时
整类 skip 并明确说明未运行，不会静默通过。
"""
import os

os.environ["RAG_DISABLE_DOTENV"] = "1"

import unittest
import uuid
from types import SimpleNamespace

from langchain_core.documents import Document as LCDocument

from app.services import answer_sources as src
from app.services.answer_sources import (
    MAX_SNAPSHOT_SOURCES,
    SOURCE_KIND_DOCUMENT,
    SOURCE_KIND_PROJECT,
    SOURCE_PROVENANCE_FIELD,
    SOURCE_SNAPSHOT_VERSION,
    SourceSnapshotError,
    SourceSnapshotOverflow,
    SourceSnapshotUnavailable,
    SourceVerificationError,
)

DOC_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
DOC_ID_2 = uuid.UUID("33333333-3333-3333-3333-333333333333")
KB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SNAP = uuid.UUID("44444444-4444-4444-4444-444444444444")
SNAP_2 = uuid.UUID("77777777-7777-7777-7777-777777777777")
SOURCE = uuid.UUID("55555555-5555-5555-5555-555555555555")
SOURCE_2 = uuid.UUID("88888888-8888-8888-8888-888888888888")
FV = uuid.UUID("66666666-6666-6666-6666-666666666666")
MANIFEST = "a" * 64
CONFIG = "b" * 64
NOW = None


def upload(chunk_index=0, **kwargs):
    payload = {"document_id": str(DOC_ID), "index_version": 0, "chunk_index": chunk_index,
               "filename": "a.txt"}
    payload.update(kwargs)
    return payload


def project(chunk_index=0, **kwargs):
    payload = {
        "source_kind": "project", "document_id": str(FV), "file_version_id": str(FV),
        "snapshot_id": str(SNAP), "source_id": str(SOURCE), "relative_path": "src/main.py",
        "manifest_fingerprint": MANIFEST, "config_fingerprint": CONFIG,
        "chunk_index": chunk_index,
    }
    payload.update(kwargs)
    return payload


class _Recorder:
    """记录 SQL / no_autoflush 进入次数的最小 Session 替身；commit 会显式报错。"""

    def __init__(self, scalar_results=(), scalars_results=(), execute_results=()):
        self._scalar = list(scalar_results)
        self._scalars = [list(item) for item in scalars_results]
        self._execute = [list(item) for item in execute_results]
        self.statements = []
        self.quiet_entries = 0

    @property
    def no_autoflush(self):
        recorder = self

        class _Context:
            def __enter__(self):
                recorder.quiet_entries += 1

            def __exit__(self, *exc):
                return False

        return _Context()

    def scalar(self, statement):
        self.statements.append(str(statement))
        return self._scalar.pop(0) if self._scalar else None

    def scalars(self, statement):
        self.statements.append(str(statement))
        return SimpleNamespace(all=lambda: self._scalars.pop(0) if self._scalars else [])

    def execute(self, statement):
        self.statements.append(str(statement))
        return SimpleNamespace(all=lambda: self._execute.pop(0) if self._execute else [])

    def commit(self):  # pragma: no cover - helper 必须永不调用
        raise AssertionError("helper 不得提交事务")


class StrictParseTests(unittest.TestCase):
    def test_document_defaults_and_identity_only(self):
        parsed = src.parse_source({**upload(chunk_index=3, chunk_start=2, chunk_end=4),
                                   "score": 0.9, "snippet": "正文"})
        self.assertEqual(parsed["source_kind"], SOURCE_KIND_DOCUMENT)
        self.assertEqual(parsed["document_id"], str(DOC_ID))
        self.assertEqual((parsed["chunk_index"], parsed["chunk_start"], parsed["chunk_end"]), (3, 2, 4))
        self.assertNotIn("score", parsed)
        self.assertNotIn("snippet", parsed)

    def test_index_version_rejects_bool_float_negative_string_none(self):
        for bad in (True, 1.0, -1, "1", None):
            with self.assertRaises(SourceSnapshotError, msg=repr(bad)):
                src.parse_source(upload(index_version=bad))

    def test_chunk_fields_reject_bool_float_string_negative(self):
        for field in ("chunk_index", "chunk_start", "chunk_end"):
            for bad in (True, 1.5, "1", -1):
                payload = upload(chunk_index=1)
                payload[field] = bad
                with self.assertRaises(SourceSnapshotError, msg=f"{field}={bad!r}"):
                    src.parse_source(payload)

    def test_chunk_range_ordering_and_span(self):
        with self.assertRaises(SourceSnapshotError):
            src.parse_source(upload(chunk_index=1, chunk_start=2, chunk_end=3))
        with self.assertRaises(SourceSnapshotError):
            src.parse_source(upload(chunk_index=5, chunk_start=0, chunk_end=6))
        self.assertEqual(src.parse_source(upload(chunk_index=5, chunk_start=1, chunk_end=5))["chunk_end"], 5)

    def test_document_id_requires_uuid(self):
        for bad in (1, True, "not-a-uuid", None):
            with self.assertRaises(SourceSnapshotError):
                src.parse_source(upload(document_id=bad))

    def test_document_source_with_project_identity_rejected(self):
        with self.assertRaises(SourceSnapshotError):
            src.parse_source({**upload(), "snapshot_id": str(SNAP)})

    def test_explicit_null_source_kind_rejected_but_missing_defaults(self):
        with self.assertRaises(SourceSnapshotError):
            src.parse_source({"source_kind": None, **upload()})
        self.assertEqual(src.parse_source(upload())["source_kind"], SOURCE_KIND_DOCUMENT)

    def test_project_requires_full_identity(self):
        for missing in ("snapshot_id", "source_id", "relative_path", "file_version_id",
                        "manifest_fingerprint", "config_fingerprint"):
            payload = project()
            payload.pop(missing)
            with self.assertRaises(SourceSnapshotError, msg=missing):
                src.parse_source(payload)
        # 缺省 source_kind 按 document 解释是契约的一部分；项目来源必须显式声明。
        self.assertEqual(src.parse_source({"document_id": str(FV), "index_version": 0,
                                           "chunk_index": 0})["source_kind"], SOURCE_KIND_DOCUMENT)

    def test_project_document_id_must_equal_file_version_id(self):
        with self.assertRaises(SourceSnapshotError):
            src.parse_source(project(document_id=str(DOC_ID)))
        self.assertEqual(src.parse_source(project())["document_id"], str(FV))

    def test_project_span_limit_is_eight(self):
        self.assertEqual(src.parse_source(project(chunk_index=0, chunk_end=8))["chunk_end"], 8)
        with self.assertRaises(SourceSnapshotError):
            src.parse_source(project(chunk_index=0, chunk_end=9))

    def test_unknown_source_kind_rejected(self):
        for bad in ("upload", "", "PROJECT", 1, True):
            with self.assertRaises(SourceSnapshotError):
                src.parse_source({**upload(), "source_kind": bad})


class DedupeAndLimitTests(unittest.TestCase):
    def test_dedupe_keeps_first_and_ignores_snippet(self):
        parsed = src.parse_sources([upload(chunk_index=1),
                                    {**upload(chunk_index=1), "score": 0.3},
                                    upload(chunk_index=2)])
        self.assertEqual([item["chunk_index"] for item in parsed], [1, 2])

    def test_same_chunk_different_index_version_distinct(self):
        self.assertEqual(len(src.parse_sources([upload(), upload(index_version=1)])), 2)

    def test_conflicting_project_security_metadata_raises(self):
        with self.assertRaises(SourceSnapshotError):
            src.parse_sources([project(), project(relative_path="src/other.py")])
        with self.assertRaises(SourceSnapshotError):
            src.parse_sources([project(), project(source_id=str(SOURCE_2))])

    def test_identical_project_duplicates_collapse(self):
        self.assertEqual(len(src.parse_sources([project(), project()])), 1)

    def test_exactly_256_allowed(self):
        items = [upload(chunk_index=index) for index in range(MAX_SNAPSHOT_SOURCES)]
        self.assertEqual(len(src.parse_sources(items)), MAX_SNAPSHOT_SOURCES)

    def test_257_raises_without_truncation(self):
        items = [upload(chunk_index=index) for index in range(MAX_SNAPSHOT_SOURCES + 1)]
        with self.assertRaises(SourceSnapshotOverflow) as ctx:
            src.parse_sources(items)
        self.assertIn("257", str(ctx.exception))

    def test_none_and_string_are_not_source_collections(self):
        for bad in (None, "x", {"document_id": str(DOC_ID)}, 1):
            with self.assertRaises(SourceSnapshotError, msg=repr(bad)):
                src.parse_sources(bad)
        self.assertEqual(src.parse_sources(()), ())

    def test_source_identity_does_not_swallow_bad_ints(self):
        with self.assertRaises(SourceSnapshotError):
            src.source_identity({"source_kind": "document", "document_id": str(DOC_ID),
                                 "index_version": True, "chunk_index": 0,
                                 "chunk_start": 0, "chunk_end": 0})


class SourcesFromPairsTests(unittest.TestCase):
    def test_upload_pairs_carry_identity_without_snippet(self):
        doc = LCDocument(page_content="正文", metadata={
            "document_id": str(DOC_ID), "index_version": 2, "chunk_index": 4,
            "chunk_start": 3, "chunk_end": 5, "filename": "manual.md", "citation_score": 0.8})
        parsed = src.sources_from_pairs([(doc, 0.0)])
        self.assertEqual(parsed[0]["index_version"], 2)
        self.assertEqual((parsed[0]["chunk_start"], parsed[0]["chunk_end"]), (3, 5))
        self.assertNotIn("snippet", parsed[0])
        self.assertNotIn("score", parsed[0])

    def test_bad_raw_index_version_rejected_before_normalization(self):
        # 上传来源必须显式携带严格非负整数 index_version，不做 legacy 缺省放行。
        for bad in (True, 1.5, "2", None):
            doc = LCDocument(page_content="x", metadata={
                "document_id": str(DOC_ID), "index_version": bad, "chunk_index": 0})
            with self.assertRaises(SourceSnapshotError, msg=repr(bad)):
                src.sources_from_pairs([(doc, 0.0)])
        missing = LCDocument(page_content="x", metadata={
            "document_id": str(DOC_ID), "chunk_index": 0})
        with self.assertRaises(SourceSnapshotError):
            src.sources_from_pairs([(missing, 0.0)])

    def test_unknown_or_null_source_kind_rejected(self):
        # rag.to_citations 会把未知 kind 当上传文档；这里必须在它之前拒绝。
        for bad in (None, "upload", "", "PROJECT"):
            doc = LCDocument(page_content="x", metadata={
                "document_id": str(DOC_ID), "index_version": 0, "chunk_index": 0,
                "source_kind": bad})
            with self.assertRaises(SourceSnapshotError, msg=repr(bad)):
                src.sources_from_pairs([(doc, 0.0)])

    def test_upload_pair_cannot_hide_project_identity_during_metadata_projection(self):
        for key, value in (("source_id", str(SOURCE)), ("snapshot_id", str(SNAP)),
                           ("file_version_id", str(FV)), ("relative_path", "src/main.py"),
                           ("snapshot_manifest_fingerprint", MANIFEST),
                           ("snapshot_config_fingerprint", CONFIG)):
            for kind in ({}, {"source_kind": "document"}):
                with self.subTest(key=key, kind=kind):
                    doc = LCDocument(page_content="x", metadata={
                        "document_id": str(DOC_ID), "index_version": 0, "chunk_index": 0,
                        key: value, **kind})
                    with self.assertRaises(SourceSnapshotError):
                        src.sources_from_pairs([(doc, 0.0)])

    def test_missing_document_metadata_rejected(self):
        doc = LCDocument(page_content="x", metadata={"chunk_index": 0, "index_version": 0})
        with self.assertRaises(SourceSnapshotError):
            src.sources_from_pairs([(doc, 0.0)])

    def test_project_pairs_use_file_version_and_fingerprints(self):
        doc = LCDocument(page_content="code", metadata={
            "source_kind": "project", "document_id": str(FV), "file_version_id": str(FV),
            "snapshot_id": str(SNAP), "source_id": str(SOURCE), "relative_path": "src/main.py",
            "snapshot_manifest_fingerprint": MANIFEST, "snapshot_config_fingerprint": CONFIG,
            "chunk_index": 7, "index_version": 0})
        parsed = src.sources_from_pairs([(doc, 0.0)])
        self.assertEqual(parsed[0]["source_kind"], SOURCE_KIND_PROJECT)
        self.assertEqual(parsed[0]["manifest_fingerprint"], MANIFEST)

    def test_project_pair_without_index_version_allowed(self):
        doc = LCDocument(page_content="code", metadata={
            "source_kind": "project", "document_id": str(FV), "file_version_id": str(FV),
            "snapshot_id": str(SNAP), "source_id": str(SOURCE), "relative_path": "src/main.py",
            "snapshot_manifest_fingerprint": MANIFEST, "snapshot_config_fingerprint": CONFIG,
            "chunk_index": 0})
        self.assertEqual(src.sources_from_pairs([(doc, 0.0)])[0]["source_kind"], SOURCE_KIND_PROJECT)

    def test_project_pair_missing_fingerprint_rejected(self):
        doc = LCDocument(page_content="code", metadata={
            "source_kind": "project", "document_id": str(FV), "file_version_id": str(FV),
            "snapshot_id": str(SNAP), "source_id": str(SOURCE), "relative_path": "src/main.py",
            "chunk_index": 0, "index_version": 0})
        with self.assertRaises(SourceSnapshotError):
            src.sources_from_pairs([(doc, 0.0)])

    def test_empty_pairs(self):
        self.assertEqual(src.sources_from_pairs([]), ())


class HistoryProvenanceTests(unittest.TestCase):
    def snapshot(self, supplied=None, history=None):
        return {"source_snapshot_version": 1,
                "supplied_sources": list(supplied or []),
                "history_sources": list(history or [])}

    def history(self, provenance):
        return [{"role": "user", "content": "上一问"},
                {"role": "assistant", "content": "上一答[1]", "citations": [upload(chunk_index=1)],
                 SOURCE_PROVENANCE_FIELD: provenance}]

    def test_transitive_provenance_includes_unquoted_supplied(self):
        # 原轮 supplied A,B，但只引用 A：B 也必须通过 provenance 继承下来。
        provenance = self.snapshot(supplied=[upload(chunk_index=0), upload(chunk_index=1)])
        parsed = src.sources_from_history(self.history(provenance))
        self.assertEqual(sorted(item["chunk_index"] for item in parsed), [0, 1])

    def test_final_citations_alone_are_insufficient(self):
        # citations 只有 chunk 1；若只读 citations 会漏掉 chunk 0。
        provenance = self.snapshot(supplied=[upload(chunk_index=0)])
        parsed = src.sources_from_history(self.history(provenance))
        self.assertEqual([item["chunk_index"] for item in parsed], [0])

    def test_missing_provenance_is_unavailable(self):
        item = {"role": "assistant", "content": "旧答案", "citations": [upload()]}
        with self.assertRaises(SourceSnapshotUnavailable):
            src.sources_from_history([item])

    def test_bad_provenance_is_unavailable(self):
        for bad in ({"source_snapshot_version": 2, "supplied_sources": [], "history_sources": []},
                    {"source_snapshot_version": 1, "supplied_sources": None, "history_sources": []},
                    {"source_snapshot_version": 1, "supplied_sources": [], },
                    "not-a-dict"):
            with self.assertRaises(SourceSnapshotUnavailable, msg=repr(bad)):
                src.sources_from_history(self.history(bad))

    def test_chat_history_without_provenance_or_citations_tolerated(self):
        self.assertEqual(src.sources_from_history(
            [{"role": "assistant", "content": "hi"}], mode="chat"), ())

    def test_explicit_null_provenance_is_not_legacy(self):
        for mode in ("chat", "rag"):
            with self.assertRaises(SourceSnapshotUnavailable, msg=mode):
                src.sources_from_history(
                    [{"role": "assistant", "content": "hi", SOURCE_PROVENANCE_FIELD: None}],
                    mode=mode)

    def test_history_container_must_be_list_or_tuple(self):
        for bad in ("x", {"a": 1}, 1):
            with self.assertRaises(SourceSnapshotError, msg=repr(bad)):
                src.sources_from_history(bad, mode="chat")
        self.assertEqual(src.sources_from_history(None, mode="chat"), ())

    def test_unknown_mode_rejected(self):
        with self.assertRaises(SourceSnapshotError):
            src.sources_from_history([], mode="bogus")


class SnapshotBuildTests(unittest.TestCase):
    def history(self, *items):
        return [{"role": "assistant", "content": "x", SOURCE_PROVENANCE_FIELD: {
            "source_snapshot_version": 1, "supplied_sources": list(items), "history_sources": []}}]

    def test_version_and_split(self):
        snapshot = src.build_source_snapshot([upload(chunk_index=0)], self.history(upload(chunk_index=1)))
        self.assertEqual(snapshot["source_snapshot_version"], SOURCE_SNAPSHOT_VERSION)
        self.assertEqual([i["chunk_index"] for i in snapshot["supplied_sources"]], [0])
        self.assertEqual([i["chunk_index"] for i in snapshot["history_sources"]], [1])

    def test_history_duplicate_of_supplied_removed_from_history_bucket(self):
        snapshot = src.build_source_snapshot([upload()], self.history(upload()))
        self.assertEqual(snapshot["history_sources"], [])

    def test_chat_rejects_sources_but_allows_empty(self):
        with self.assertRaises(SourceSnapshotError):
            src.build_source_snapshot([upload()], [], mode="chat")
        snapshot = src.build_source_snapshot([], [], mode="chat")
        self.assertEqual((snapshot["supplied_sources"], snapshot["history_sources"]), ([], []))

    def test_supplied_none_is_not_empty(self):
        with self.assertRaises(SourceSnapshotError):
            src.build_source_snapshot(None, [])

    def test_overflow_is_fatal_not_trimmed(self):
        items = [upload(chunk_index=index) for index in range(MAX_SNAPSHOT_SOURCES + 1)]
        with self.assertRaises(SourceSnapshotOverflow):
            src.build_source_snapshot(items, [])

    def test_overflow_after_history_merge_also_fatal(self):
        supplied = [upload(chunk_index=index) for index in range(MAX_SNAPSHOT_SOURCES)]
        with self.assertRaises(SourceSnapshotOverflow):
            src.build_source_snapshot(supplied, self.history(upload(chunk_index=999)))


class TraceReadTests(unittest.TestCase):
    def trace(self, **kwargs):
        payload = {"source_snapshot_version": 1, "supplied_sources": [upload()],
                   "history_sources": []}
        payload.update(kwargs)
        return payload

    def test_v1_rag_trace_roundtrips(self):
        parsed = src.sources_from_trace(self.trace(), mode="rag")
        self.assertEqual(parsed[0]["document_id"], str(DOC_ID))

    def test_old_rag_trace_without_version_is_excluded(self):
        for trace in ({}, {"supplied_sources": [upload()], "history_sources": []}):
            with self.assertRaises(SourceSnapshotUnavailable):
                src.sources_from_trace(trace, mode="rag")

    def test_bad_version_values_are_excluded(self):
        for bad in (True, 0, 2, "1", 1.0, None):
            with self.assertRaises(SourceSnapshotUnavailable, msg=repr(bad)):
                src.sources_from_trace(self.trace(source_snapshot_version=bad), mode="rag")

    def test_missing_or_non_list_source_keys_are_excluded(self):
        for key in ("supplied_sources", "history_sources"):
            trace = self.trace()
            trace.pop(key)
            with self.assertRaises(SourceSnapshotUnavailable, msg=key):
                src.sources_from_trace(trace, mode="rag")
            for bad in (None, (upload(),), "x", {"document_id": str(DOC_ID)}):
                with self.assertRaises(SourceSnapshotUnavailable, msg=f"{key}={bad!r}"):
                    src.sources_from_trace(self.trace(**{key: bad}), mode="rag")

    def test_plain_chat_without_snapshot_is_empty_and_usable(self):
        self.assertEqual(src.sources_from_trace({}, mode="chat"), ())

    def test_chat_legacy_exemption_requires_all_three_keys_absent(self):
        for key in ("source_snapshot_version", "supplied_sources", "history_sources"):
            for bad in (None, [], (), "x"):
                with self.assertRaises(SourceSnapshotError, msg=f"{key}={bad!r}"):
                    src.sources_from_trace({key: bad}, mode="chat")

    def test_plain_chat_with_sources_or_version_rejected(self):
        with self.assertRaises(SourceSnapshotError):
            src.sources_from_trace(self.trace(), mode="chat")

    def test_unknown_mode_rejected(self):
        with self.assertRaises(SourceSnapshotError):
            src.sources_from_trace(self.trace(), mode="bogus")

    def test_trace_overflow_propagates(self):
        items = [upload(chunk_index=index) for index in range(MAX_SNAPSHOT_SOURCES + 1)]
        with self.assertRaises(SourceSnapshotOverflow):
            src.sources_from_trace(self.trace(supplied_sources=items), mode="rag")

    def test_combined_order_dedupes_supplied_before_history(self):
        trace = self.trace(history_sources=[upload(chunk_index=1), upload()])
        self.assertEqual([i["chunk_index"] for i in src.sources_from_trace(trace, mode="rag")], [0, 1])


class LockShapeTests(unittest.TestCase):
    """只验证 SQL 形状与顺序（FOR UPDATE + no_autoflush），不连库。"""

    def test_lock_root_orders_user_then_kb(self):
        recorder = _Recorder(scalar_results=[SimpleNamespace(id=DOC_ID, is_admin=True),
                                             SimpleNamespace(id=KB_ID, kind="upload")])
        root = src.lock_root(recorder, DOC_ID, KB_ID)
        self.assertEqual(root.knowledge_base.id, KB_ID)
        self.assertGreaterEqual(recorder.quiet_entries, 1)
        self.assertEqual(len(recorder.statements), 2)
        self.assertIn("users", recorder.statements[0])
        self.assertIn("FOR UPDATE", recorder.statements[0])
        self.assertIn("knowledge_bases", recorder.statements[1])
        self.assertIn("FOR UPDATE", recorder.statements[1])

    def test_lock_root_chat_locks_only_user(self):
        recorder = _Recorder(scalar_results=[SimpleNamespace(id=DOC_ID, is_admin=False)])
        self.assertIsNone(src.lock_root(recorder, DOC_ID, None).knowledge_base)
        self.assertEqual(len(recorder.statements), 1)

    def test_lock_root_missing_user(self):
        with self.assertRaises(SourceVerificationError):
            src.lock_root(_Recorder(), DOC_ID, None)

    def project_context(self):
        return (SimpleNamespace(id=KB_ID, kind="project", is_public=False, owner_id=DOC_ID),
                SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True))

    def test_lock_project_binding_locks_source_then_snapshot(self):
        source = SimpleNamespace(id=SOURCE, kb_id=KB_ID, revoked_at=None)
        snapshot = SimpleNamespace(id=SNAP, source_id=SOURCE, status="ready",
                                   manifest_fingerprint=MANIFEST, config_fingerprint=CONFIG)
        recorder = _Recorder(scalars_results=[[source], [snapshot]],
                             execute_results=[[(SNAP, SOURCE)]])
        kb, user = self.project_context()
        binding = src.lock_project_binding(recorder, [project()], kb=kb, user=user)
        self.assertEqual((set(binding.snapshots), set(binding.sources)), ({SNAP}, {SOURCE}))
        source_lock = next(i for i, sql in enumerate(recorder.statements)
                           if "project_sources" in sql and "FOR UPDATE" in sql)
        snapshot_lock = next(i for i, sql in enumerate(recorder.statements)
                             if "project_snapshots" in sql and "FOR UPDATE" in sql
                             and "ORDER BY project_snapshots.id" in sql)
        self.assertLess(source_lock, snapshot_lock)

    def test_lock_project_binding_requires_all_requested_snapshots(self):
        recorder = _Recorder(scalars_results=[[], []], execute_results=[[(SNAP, SOURCE)]])
        kb, user = self.project_context()
        with self.assertRaises(SourceVerificationError):
            src.lock_project_binding(recorder, [project(snapshot_id=str(SNAP_2))], kb=kb, user=user)

    def test_lock_project_binding_rejects_not_ready_and_revoked(self):
        for status, revoked in (("pending", None), ("ready", "2026-01-01")):
            source = SimpleNamespace(id=SOURCE, kb_id=KB_ID, revoked_at=revoked)
            snapshot = SimpleNamespace(id=SNAP, source_id=SOURCE, status=status,
                                       manifest_fingerprint=MANIFEST, config_fingerprint=CONFIG)
            recorder = _Recorder(scalars_results=[[source], [snapshot]],
                                 execute_results=[[(SNAP, SOURCE)]])
            kb, user = self.project_context()
            with self.assertRaises(SourceVerificationError, msg=f"{status}/{revoked}"):
                src.lock_project_binding(recorder, [project()], kb=kb, user=user)

    def test_lock_project_binding_rejects_non_project_kb(self):
        with self.assertRaises(SourceVerificationError):
            src.lock_project_binding(_Recorder(), [project()],
                                     kb=SimpleNamespace(id=KB_ID, kind="upload", is_public=True, owner_id=None),
                                     user=SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True))

    def test_lock_document_ids_sorted_for_update(self):
        recorder = _Recorder(scalars_results=[[SimpleNamespace(id=DOC_ID), SimpleNamespace(id=DOC_ID_2)]])
        ids = src.lock_document_ids(recorder, SimpleNamespace(id=KB_ID, kind="upload"),
                                    [DOC_ID_2, DOC_ID])
        self.assertEqual(ids, (DOC_ID, DOC_ID_2))
        self.assertIn("FOR UPDATE", recorder.statements[0])
        self.assertIn("ORDER BY documents.id", recorder.statements[0])

    def test_lock_document_rows_rejects_project_sources(self):
        recorder = _Recorder()
        with self.assertRaises(SourceVerificationError):
            src.lock_document_rows(recorder, SimpleNamespace(id=KB_ID, kind="upload"), [project()])
        self.assertEqual(recorder.statements, [])


class ValidationDispatchTests(unittest.TestCase):
    def upload_kb(self, **kwargs):
        defaults = dict(id=KB_ID, kind="upload", is_public=True, owner_id=None)
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_empty_sources_pass_without_db(self):
        recorder = _Recorder()
        self.assertEqual(src.validate_sources(recorder, None, [],
                                              user=SimpleNamespace(id=DOC_ID, is_active=True)), ())
        self.assertEqual(recorder.statements, [])

    def test_sources_without_kb_rejected(self):
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(_Recorder(), None, [upload()],
                                 user=SimpleNamespace(id=DOC_ID, is_active=True))

    def test_mixed_kinds_rejected_before_db(self):
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(_Recorder(), self.upload_kb(), [upload(), project()],
                                 user=SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True))

    def test_inactive_user_rejected_for_upload(self):
        with self.assertRaises(SourceVerificationError):
            src.validate_document_sources(_Recorder(), self.upload_kb(),
                                          [upload()],
                                          user=SimpleNamespace(id=DOC_ID, is_admin=True, is_active=False))

    def test_private_kb_non_owner_rejected(self):
        with self.assertRaises(SourceVerificationError):
            src.validate_document_sources(_Recorder(), self.upload_kb(is_public=False),
                                          [upload()],
                                          user=SimpleNamespace(id=DOC_ID, is_admin=False, is_active=True))

    def test_document_validation_rejects_project_kb(self):
        with self.assertRaises(SourceVerificationError):
            src.validate_document_sources(_Recorder(), self.upload_kb(kind="project"), [upload()],
                                          user=SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True))

    def test_project_lock_false_reads_without_for_update(self):
        source = SimpleNamespace(id=SOURCE, kb_id=KB_ID, revoked_at=None)
        snapshot = SimpleNamespace(id=SNAP, source_id=SOURCE, status="ready",
                                   manifest_fingerprint=MANIFEST, config_fingerprint=CONFIG)
        motif_rows = [(SNAP, FV, SOURCE, "src/main.py", "ready", 0)]
        recorder = _Recorder(scalars_results=[[source], [snapshot]],
                             execute_results=[[(SNAP, SOURCE)], motif_rows])
        kb = SimpleNamespace(id=KB_ID, kind="project", is_public=False, owner_id=DOC_ID)
        user = SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True)
        result = src.validate_sources(recorder, kb, [project()], user=user, lock=False)
        self.assertEqual(len(result), 1)
        self.assertFalse(any("FOR UPDATE" in sql for sql in recorder.statements),
                         "lock=False 项目复核不得加锁")

    def test_project_binding_passed_avoids_rereading_snapshot(self):
        binding = src.ProjectBinding(
            snapshots={SNAP: SimpleNamespace(id=SNAP, source_id=SOURCE, status="ready",
                                             manifest_fingerprint=MANIFEST,
                                             config_fingerprint=CONFIG)},
            sources={SOURCE: SimpleNamespace(id=SOURCE, kb_id=KB_ID, revoked_at=None)})
        recorder = _Recorder(execute_results=[[(SNAP, FV, SOURCE, "src/main.py", "ready", 0)]])
        kb = SimpleNamespace(id=KB_ID, kind="project", is_public=False, owner_id=DOC_ID)
        user = SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True)
        src.validate_sources(recorder, kb, [project()], user=user, lock=False, binding=binding)
        self.assertFalse(any("project_snapshots" in sql for sql in recorder.statements))

    def test_project_source_id_mismatch_rejected(self):
        binding = src.ProjectBinding(
            snapshots={SNAP: SimpleNamespace(id=SNAP, source_id=SOURCE_2, status="ready",
                                             manifest_fingerprint=MANIFEST,
                                             config_fingerprint=CONFIG)},
            sources={SOURCE_2: SimpleNamespace(id=SOURCE_2, kb_id=KB_ID, revoked_at=None)})
        kb = SimpleNamespace(id=KB_ID, kind="project", is_public=False, owner_id=DOC_ID)
        user = SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True)
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(_Recorder(), kb, [project()], user=user, lock=False, binding=binding)

    def test_project_query_is_scoped_to_requested_chunk_range(self):
        binding = src.ProjectBinding(
            snapshots={SNAP: SimpleNamespace(id=SNAP, source_id=SOURCE, status="ready",
                                             manifest_fingerprint=MANIFEST,
                                             config_fingerprint=CONFIG)},
            sources={SOURCE: SimpleNamespace(id=SOURCE, kb_id=KB_ID, revoked_at=None)})
        recorder = _Recorder(execute_results=[[(SNAP, FV, SOURCE, "src/main.py", "ready", 2)]])
        kb = SimpleNamespace(id=KB_ID, kind="project", is_public=False, owner_id=DOC_ID)
        user = SimpleNamespace(id=DOC_ID, is_admin=True, is_active=True)
        src.validate_sources(recorder, kb, [project(chunk_index=2, chunk_start=2, chunk_end=2)],
                             user=user, lock=False, binding=binding)
        sql = recorder.statements[-1]
        self.assertIn("project_chunks.chunk_index BETWEEN", sql)


from tests import _memory_project_test_gate as gate  # noqa: E402
from app.db import session as db_session  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

_TABLES = ("users, knowledge_bases, documents, document_chunks, project_sources, "
           "project_snapshots, project_file_versions, project_snapshot_files, project_chunks")


class _SqlRecorder:
    """在真实 engine 上记录实际执行语句（before_cursor_execute）。"""

    def __init__(self, engine):
        self.engine = engine
        self.statements = []
        self._listener = self._record

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(" ".join(statement.split()))

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._listener)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._listener)
        return False

    def tables_locked(self):
        """按出现顺序返回被 FOR UPDATE 的表名（只取真实语句）。"""
        order = []
        for sql in self.statements:
            if "FOR UPDATE" not in sql:
                continue
            for table in ("users", "knowledge_bases", "project_sources", "project_snapshots",
                          "documents"):
                if f"FROM {table}" in sql and table not in order:
                    order.append(table)
        return order


class DatabaseGateTests(unittest.TestCase):
    """数据库门的纯配置校验（不连接任何库）。"""

    OWNER = "owned-test:ragkb-owner-a"

    def test_unconfigured_is_missing_both(self):
        self.assertEqual(gate.classify("", ""), "missing_both")

    def test_partial_configuration_is_refused_not_skipped(self):
        with self.assertRaises(gate.ConfigError):
            gate.raise_bad_config("postgresql://u:p@127.0.0.1:5432/ragkb_test", "")
        with self.assertRaises(gate.ConfigError):
            gate.raise_bad_config("", self.OWNER)

    def test_non_loopback_and_non_test_suffix_refused(self):
        self.assertEqual(gate.classify("postgresql://u:p@db.example.com:5432/ragkb_test",
                                       self.OWNER), "non_loopback")
        self.assertEqual(gate.classify("postgresql://u:p@127.0.0.1:5432/ragkb",
                                       self.OWNER), "not_test_suffix")

    def test_owner_must_declare_disposable(self):
        self.assertEqual(gate.classify("postgresql://u:p@127.0.0.1:5432/ragkb_test",
                                       "plain-owner-a"), "bad_owner")

    def test_fully_valid_configuration_is_ok(self):
        self.assertEqual(gate.classify("postgresql://u:p@127.0.0.1:5432/ragkb_test",
                                       self.OWNER), "ok")


@unittest.skipIf(
    gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
    "需要显式 owned PostgreSQL 测试库（TEST_DATABASE_URL + TEST_DATABASE_OWNER）",
)
class AnswerSourcesDatabaseTests(unittest.TestCase):
    """真实 PostgreSQL：来源复核正反例、ACL、加锁顺序与 no_autoflush。

    数据库门沿用 ``tests._memory_project_test_gate``（loopback + ``_test`` + owner
    comment 精确匹配）；未显式配置时整类 skip，绝不连接业务库。
    """

    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        from app.db.init_db import init_db

        init_db(create_admin=False)
        cls.engine = db_session.engine
        cls.Session = db_session.SessionLocal

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, self.engine)
        with self.engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
        self.session = self.Session()
        self.addCleanup(self.session.rollback)
        self.addCleanup(self.session.close)

    # ---- fixtures ----

    def _user(self, name="u", *, active=True, admin=False):
        from app.models import User

        user = User(username=f"{name}_{uuid.uuid4().hex[:8]}", role="admin" if admin else "user",
                    is_active=active, password_hash="x")
        self.session.add(user)
        self.session.flush()
        return user

    def _kb(self, user, *, kind="upload", public=True, owner=None):
        from app.models import KnowledgeBase

        kb = KnowledgeBase(name="kb_" + uuid.uuid4().hex, collection_name="c_" + uuid.uuid4().hex,
                           owner_id=(owner or user).id, is_public=public, kind=kind)
        self.session.add(kb)
        self.session.flush()
        return kb

    def _document(self, kb, *, index_version=0, status="ready", **governance):
        from app.models import Document

        doc = Document(kb_id=kb.id, filename=f"{uuid.uuid4().hex}.txt", file_ext=".txt",
                       file_size=1, stored_path=f"/tmp/{uuid.uuid4().hex}.txt",
                       status=status, index_version=index_version, **governance)
        self.session.add(doc)
        self.session.flush()
        return doc

    def _chunks(self, doc, indexes, *, embedded=True):
        from app.models import DocumentChunk

        for index in indexes:
            self.session.add(DocumentChunk(document_id=doc.id, chunk_index=index,
                                           content=f"c{index}", embedded=embedded))
        self.session.flush()

    def _project(self, user, *, ready=True, revoked=False, path="src/main.py"):
        from datetime import datetime, timezone
        from app.models import (ProjectChunk, ProjectFileVersion, ProjectSnapshot,
                                ProjectSnapshotFile, ProjectSource)

        kb = self._kb(user, kind="project", public=False, owner=user)
        source = ProjectSource(kb_id=kb.id, name="src_" + uuid.uuid4().hex, root_key="fixture",
                               relative_dir="", root_fingerprint="0" * 64, created_by=user.id,
                               revoked_at=datetime.now(timezone.utc) if revoked else None)
        self.session.add(source)
        self.session.flush()
        snapshot = ProjectSnapshot(source_id=source.id,
                                   status="ready" if ready else "pending",
                                   manifest_fingerprint="m" * 64, config_fingerprint="c" * 64)
        self.session.add(snapshot)
        self.session.flush()
        version = ProjectFileVersion(source_id=source.id, relative_path=path,
                                     content_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                                     blob_path="/blobs/b", size=1, parse_status="ready",
                                     config_fingerprint="c" * 64)
        self.session.add(version)
        self.session.flush()
        self.session.add(ProjectSnapshotFile(snapshot_id=snapshot.id, relative_path=path,
                                             file_version_id=version.id))
        self.session.add(ProjectChunk(file_version_id=version.id, chunk_index=0,
                                      content="code", embedded=True))
        self.session.flush()
        return kb, {
            "source_kind": "project", "document_id": str(version.id),
            "file_version_id": str(version.id), "snapshot_id": str(snapshot.id),
            "source_id": str(source.id), "relative_path": path,
            "manifest_fingerprint": "m" * 64, "config_fingerprint": "c" * 64,
            "chunk_index": 0, "chunk_start": 0, "chunk_end": 0,
        }

    def _upload_source(self, doc, **kwargs):
        payload = {"document_id": str(doc.id), "index_version": doc.index_version,
                   "chunk_index": 0, "chunk_start": 0, "chunk_end": 0}
        payload.update(kwargs)
        return payload

    # ---- upload 正反例 ----

    def test_upload_active_current_generation_passes(self):
        user = self._user(admin=True)
        kb = self._kb(user)
        doc = self._document(kb)
        self._chunks(doc, [0, 1])
        self.session.commit()
        result = src.validate_sources(self.session, kb, [self._upload_source(doc)],
                                      user=user, lock=True)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["document_id"], str(doc.id))

    def test_upload_disabled_expired_superseded_fail(self):
        from datetime import datetime, timezone

        user = self._user(admin=True)
        kb = self._kb(user)
        past = datetime(2000, 1, 1, tzinfo=timezone.utc)
        cases = {"disabled": {"enabled": False}, "expired": {"valid_until": past},
                 "superseded": {"superseded_at": past}}
        for label, governance in cases.items():
            doc = self._document(kb, **governance)
            self._chunks(doc, [0])
            self.session.commit()
            with self.assertRaises(SourceVerificationError, msg=label):
                src.validate_sources(self.session, kb, [self._upload_source(doc)],
                                     user=user, lock=False)

    def test_upload_missing_and_unembedded_chunk_fail(self):
        user = self._user(admin=True)
        kb = self._kb(user)
        doc = self._document(kb)
        self._chunks(doc, [0])
        self.session.commit()
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb,
                                 [self._upload_source(doc, chunk_index=1, chunk_start=1,
                                                      chunk_end=1)],
                                 user=user, lock=False)
        unembedded = self._document(kb)
        self._chunks(unembedded, [0], embedded=False)
        self.session.commit()
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb, [self._upload_source(unembedded)],
                                 user=user, lock=False)

    def test_upload_wrong_kb_and_wrong_generation_fail(self):
        user = self._user(admin=True)
        kb = self._kb(user)
        other_kb = self._kb(user)
        doc = self._document(other_kb)
        self._chunks(doc, [0])
        self.session.commit()
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb, [self._upload_source(doc)],
                                 user=user, lock=False)
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb,
                                 [self._upload_source(doc, index_version=doc.index_version + 1)],
                                 user=user, lock=False)

    def test_upload_acl_rejects_inactive_and_foreign_private(self):
        owner = self._user("owner")
        stranger = self._user("stranger")
        kb = self._kb(owner, public=False)
        doc = self._document(kb)
        self._chunks(doc, [0])
        self.session.commit()
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb, [self._upload_source(doc)],
                                 user=stranger, lock=False)
        inactive = self._user("inactive", active=False, admin=True)
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb, [self._upload_source(doc)],
                                 user=inactive, lock=False)

    # ---- project 正反例 ----

    def test_project_ready_binding_passes(self):
        user = self._user(admin=True)
        kb, ref = self._project(user)
        self.session.commit()
        binding = src.lock_project_binding(self.session, [ref], kb=kb, user=user)
        result = src.validate_sources(self.session, kb, [ref], user=user, lock=False,
                                      binding=binding)
        self.assertEqual(result[0]["file_version_id"], ref["file_version_id"])

    def test_project_never_queries_upload_documents(self):
        user = self._user(admin=True)
        kb, ref = self._project(user)
        self.session.commit()
        binding = src.lock_project_binding(self.session, [ref], kb=kb, user=user)
        with _SqlRecorder(self.engine) as recorder:
            src.validate_sources(self.session, kb, [ref], user=user, lock=False, binding=binding)
        self.assertFalse(any("FROM documents" in sql for sql in recorder.statements),
                         recorder.statements)

    def test_project_wrong_identity_fields_fail(self):
        user = self._user(admin=True)
        kb, ref = self._project(user)
        self.session.commit()
        binding = src.lock_project_binding(self.session, [ref], kb=kb, user=user)
        broken = {"source_id": str(uuid.uuid4()),
                  "relative_path": "src/other.py",
                  "manifest_fingerprint": "z" * 64,
                  "config_fingerprint": "z" * 64}
        for field, bad in broken.items():
            with self.assertRaises(SourceVerificationError, msg=field):
                src.validate_sources(self.session, kb, [{**ref, field: bad}], user=user,
                                     lock=False, binding=binding)
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb,
                                 [{**ref, "chunk_index": 1, "chunk_start": 1, "chunk_end": 1}],
                                 user=user, lock=False, binding=binding)

    def test_project_missing_snapshot_fails(self):
        user = self._user(admin=True)
        kb, ref = self._project(user)
        self.session.commit()
        with self.assertRaises(SourceVerificationError):
            src.lock_project_binding(self.session, [{**ref, "snapshot_id": str(uuid.uuid4())}],
                                     kb=kb, user=user)

    def test_project_nonready_and_revoked_fail(self):
        user = self._user(admin=True)
        for label, kwargs in (("pending", {"ready": False}), ("revoked", {"revoked": True})):
            kb, ref = self._project(user, **kwargs)
            self.session.commit()
            with self.assertRaises(SourceVerificationError, msg=label):
                src.lock_project_binding(self.session, [ref], kb=kb, user=user)

    def test_project_unmapped_file_version_fails(self):
        user = self._user(admin=True)
        kb, ref = self._project(user)
        self.session.commit()
        binding = src.lock_project_binding(self.session, [ref], kb=kb, user=user)
        other = uuid.uuid4()
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb,
                                 [{**ref, "document_id": str(other), "file_version_id": str(other)}],
                                 user=user, lock=False, binding=binding)

    def test_project_cross_kb_source_fails(self):
        user = self._user(admin=True)
        kb_one, ref_one = self._project(user)
        kb_two, _ = self._project(user)
        self.session.commit()
        with self.assertRaises(SourceVerificationError):
            src.validate_sources(self.session, kb_two, [ref_one], user=user, lock=False)

    # ---- 锁：真实 SQL 观察 ----

    def test_lock_root_and_project_binding_actual_sql_order(self):
        user = self._user(admin=True)
        kb, ref = self._project(user)
        self.session.commit()
        with _SqlRecorder(self.engine) as recorder:
            src.lock_root(self.session, user.id, kb.id)
            src.lock_project_binding(self.session, [ref], kb=kb, user=user)
        self.assertEqual(recorder.tables_locked(),
                         ["users", "knowledge_bases", "project_sources", "project_snapshots"])

    def test_lock_document_ids_is_last_and_uuid_sorted(self):
        user = self._user(admin=True)
        kb = self._kb(user)
        first = self._document(kb)
        second = self._document(kb)
        self.session.commit()
        with _SqlRecorder(self.engine) as recorder:
            src.lock_root(self.session, user.id, kb.id)
            locked = src.lock_document_ids(self.session, kb, [second.id, first.id])
        self.assertEqual(locked, tuple(sorted([first.id, second.id])))
        documents_sql = [sql for sql in recorder.statements
                         if "FOR UPDATE" in sql and "FROM documents" in sql]
        self.assertEqual(len(documents_sql), 1)
        self.assertIn("ORDER BY documents.id", documents_sql[0])
        self.assertEqual(recorder.tables_locked()[-1], "documents")

    def test_lock_false_project_issues_no_for_update(self):
        user = self._user(admin=True)
        kb, ref = self._project(user)
        self.session.commit()
        with _SqlRecorder(self.engine) as recorder:
            src.validate_sources(self.session, kb, [ref], user=user, lock=False)
        self.assertFalse(any("FOR UPDATE" in sql for sql in recorder.statements))

    def test_helper_never_commits_and_suppresses_autoflush(self):
        from sqlalchemy import select as sa_select

        from app.models import KnowledgeBase

        user = self._user(admin=True)
        self.session.commit()
        pending = KnowledgeBase(name="pending_" + uuid.uuid4().hex,
                                collection_name="pending_" + uuid.uuid4().hex,
                                owner_id=user.id, is_public=True, kind="upload")
        self.session.add(pending)  # 故意不 flush：autoflush 会提前写出它
        with _SqlRecorder(self.engine) as recorder:
            src.lock_root(self.session, user.id, None)
        self.assertFalse(any("INSERT INTO knowledge_bases" in sql for sql in recorder.statements),
                         recorder.statements)
        # 强证据：从未 flush，主键仍为 None；且 helper 没提交任何东西。
        self.assertIsNone(pending.id)
        pending_name = pending.name
        self.session.rollback()
        with self.Session() as probe:
            self.assertEqual(probe.scalar(sa_select(KnowledgeBase)
                                          .where(KnowledgeBase.name == pending_name)), None)


if __name__ == "__main__":
    unittest.main()