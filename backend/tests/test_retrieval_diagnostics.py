"""管理员检索诊断：trace 映射、候选白名单、异常收敛与可选数据库门控测试。"""
import itertools
import json
import unittest
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document as LCDocument
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from tests._env import TEST_URL

from app.core.config import settings
from app.schemas.inspection import RetrievalDiagnosticsRequest
from app.services import retrieval_diagnostics as diagnostics


class DiagnosticsServiceTests(unittest.TestCase):
    def setUp(self):
        self.kb = SimpleNamespace(id=uuid.uuid4(), collection_name="kb_diag")
        self.user = SimpleNamespace(id=uuid.uuid4())
        self.document_id = uuid.uuid4()
        self.db = MagicMock()
        self.db.scalar.side_effect = itertools.cycle([10, 8, 6])

    def pair(self, content="正文", distance=0.123, **metadata):
        base = {
            "document_id": str(self.document_id), "filename": "a.pdf",
            "chunk_index": 3, "chunk_start": 2, "chunk_end": 4, "index_version": 1,
            "page_start": 2, "page_end": 2, "retrieval_method": "hybrid",
        }
        base.update(metadata)
        return LCDocument(page_content=content, metadata=base), distance

    def ready_row(self, identifier=None):
        return SimpleNamespace(
            id=identifier or self.document_id, status="ready", cancel_requested=False,
            index_version=1, superseded_at=None, superseded_by_id=None, enabled=True,
            valid_from=None, valid_until=None,
        )

    def run_diagnostics(self, pairs, *, document_ids=None, trace_update=None, captured=None,
                        side_effect=None):
        def fake_retrieve(collection_name, question, top_k, *, document_scope=None, trace=None):
            if captured is not None:
                captured.update(collection=collection_name, question=question, top_k=top_k,
                                document_scope=document_scope)
            if trace is not None and trace_update is not None:
                trace.update(trace_update)
            if side_effect is not None:
                side_effect()
            return pairs

        with patch.object(diagnostics.rag, "retrieve", side_effect=fake_retrieve):
            return diagnostics.run(self.db, self.kb, "问题", document_ids or [], user=self.user)

    def test_no_scope_passes_none_and_maps_trace_with_usage_scope(self):
        recorded = []

        @contextmanager
        def recorder(context):
            recorded.append(context)
            yield

        trace_update = {
            "vector": {"status": "ok", "ms": 12, "candidates": 34, "error_type": None},
            "keyword": {"status": "ok", "ms": 8, "candidates": 60, "error_type": None},
            "fusion": {"status": "ok", "ms": 0, "candidates": 94},
            "rerank": {"status": "skipped", "ms": 0, "candidates": 94,
                       "applied": False, "reason": "disabled"},
            "select": {"status": "ok", "ms": 0, "candidates": 6},
            "neighbors": {"status": "ok", "ms": 3, "candidates": 5, "dropped": 1},
            "total_ms": 25,
        }
        captured = {}
        with patch.object(diagnostics, "resolve_scope") as resolve, \
             patch.object(diagnostics, "usage_scope", recorder):
            data = self.run_diagnostics([self.pair()], trace_update=trace_update, captured=captured)

        resolve.assert_not_called()
        self.assertIsNone(captured["document_scope"])
        self.assertEqual(captured["top_k"], settings.RETRIEVAL_TOP_K)
        self.assertEqual(recorded[0].user_id, self.user.id)
        self.assertEqual(recorded[0].kb_id, self.kb.id)
        self.assertEqual(recorded[0].operation, "retrieval_diagnostics")
        self.assertEqual(data["status"], "ok")
        self.assertIsNone(data["error_type"])
        self.assertFalse(data["answer_generated"])
        self.assertEqual(data["scope"], [])
        self.assertEqual(data["total_ms"], 25)
        self.assertEqual([stage["name"] for stage in data["stages"]],
                         ["vector", "keyword", "fusion", "rerank", "select", "neighbors"])
        self.assertEqual([stage["ms"] for stage in data["stages"]], [12, 8, 0, 0, 0, 3])
        self.assertEqual(data["stages"][3]["reason"], "disabled")
        self.assertFalse(data["stages"][3]["applied"])
        self.assertEqual(data["kb"], {"total_documents": 10, "ready_documents": 8,
                                      "retrievable_documents": 6})
        self.assertEqual(data["config"]["top_k"], settings.RETRIEVAL_TOP_K)
        self.assertEqual(data["config"]["min_similarity"], settings.RETRIEVAL_MIN_SIMILARITY)
        self.assertEqual(data["collection_name"], "kb_diag")

    def test_scope_is_resolved_and_forwarded(self):
        scope = [{"document_id": str(self.document_id), "filename": "a.pdf", "index_version": 1}]
        captured = {}
        with patch.object(diagnostics, "resolve_scope", return_value=scope) as resolve:
            data = self.run_diagnostics([], document_ids=[self.document_id], captured=captured)
        resolve.assert_called_once_with(self.db, self.kb.id, [self.document_id])
        self.assertEqual(captured["document_scope"], scope)
        self.assertEqual(data["scope"], scope)

    def test_candidate_whitelist_excerpt_truncation_and_secret_metadata(self):
        content = "X" * 650
        pair = self.pair(
            content=content, distance=0.123, fusion_score=0.03, rerank_score=float("inf"),
            citation_score="bad", stored_path="/business/secret/path", search_text="SEARCH_SECRET",
            _anchor_content="ANCHOR_SECRET", internal_token="TOKEN_SECRET",
        )
        self.db.scalars.return_value.all.return_value = [self.ready_row()]
        data = self.run_diagnostics([pair])
        self.assertEqual(len(data["candidates"]), 1)
        item = data["candidates"][0]
        self.assertEqual(item["rank"], 1)
        self.assertEqual(item["document_id"], self.document_id)
        self.assertEqual(item["filename"], "a.pdf")
        self.assertEqual(item["chunk_index"], 3)
        self.assertEqual((item["chunk_start"], item["chunk_end"]), (2, 4))
        self.assertEqual(item["index_version"], 1)
        self.assertEqual(item["location"], {"page_start": 2, "page_end": 2})
        self.assertEqual(len(item["excerpt"]), 600)
        self.assertEqual(item["excerpt_chars"], 650)
        self.assertTrue(item["excerpt_truncated"])
        self.assertEqual(item["scores"], {"distance": 0.123, "fusion_score": 0.03})
        self.assertEqual(item["retrieval_method"], "hybrid")
        self.assertEqual(item["lifecycle_state"], "active")
        self.assertIs(item["retrievable"], True)
        serialized = json.dumps(data, default=str)
        for secret in ("SEARCH_SECRET", "ANCHOR_SECRET", "TOKEN_SECRET", "/business/secret/path"):
            self.assertNotIn(secret, serialized)

    def test_governance_count_query_and_candidate_lookup_use_retrievable_conditions(self):
        self.db.scalars.return_value.all.return_value = [self.ready_row()]
        self.run_diagnostics([self.pair()])
        count_sql = str(self.db.scalar.call_args_list[2].args[0].compile(dialect=postgresql.dialect()))
        for clause in ("documents.status =", "documents.cancel_requested IS false",
                       "documents.superseded_at IS NULL", "documents.superseded_by_id IS NULL",
                       "documents.enabled IS true", "documents.valid_from IS NULL OR",
                       "documents.valid_until IS NULL OR", "documents.index_version >= "):
            self.assertIn(clause, count_sql)
        lookup_sql = str(self.db.scalars.call_args.args[0].compile(dialect=postgresql.dialect()))
        self.assertIn("documents.id IN", lookup_sql)

    def test_missing_trace_stages_are_filled_as_skipped_with_zeros(self):
        data = self.run_diagnostics([])
        self.assertEqual(len(data["stages"]), 6)
        for stage in data["stages"]:
            self.assertEqual(stage["status"], "skipped")
            self.assertEqual(stage["ms"], 0)
            self.assertEqual(stage["candidates"], 0)
            self.assertIsNone(stage["error_type"])
        self.assertEqual(data["total_ms"], 0)

    def test_retrieval_failure_returns_error_without_message_and_keeps_partial_trace(self):
        def fail():
            raise RuntimeError("TOP_SECRET_SERVER_MESSAGE")

        def update(trace):
            trace["vector"] = {"status": "ok", "ms": 1, "candidates": 0, "error_type": None}

        with patch.object(diagnostics.logger, "warning") as warning:
            data = self.run_diagnostics([], trace_update=None, side_effect=fail)
        # 上面的调用没有 trace_update，模拟真实 rag 已写入阶段的情况改用下面重新执行。
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["error_type"], "RuntimeError")
        self.assertEqual(data["candidates"], [])
        self.assertNotIn("TOP_SECRET_SERVER_MESSAGE", json.dumps(data, default=str))
        self.assertTrue(all(stage["status"] == "skipped" for stage in data["stages"]))
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args, ("检索诊断失败: %s", "RuntimeError"))

        def fake_with_trace(collection_name, question, top_k, *, document_scope=None, trace=None):
            update(trace)
            raise RuntimeError("TOP_SECRET_SERVER_MESSAGE")

        with patch.object(diagnostics.rag, "retrieve", side_effect=fake_with_trace), \
             patch.object(diagnostics.logger, "warning"):
            data = diagnostics.run(self.db, self.kb, "问题", [], user=self.user)
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["stages"][0]["status"], "ok")
        self.assertEqual(data["stages"][0]["ms"], 1)
        self.assertTrue(all(stage["status"] == "skipped" for stage in data["stages"][1:]))

    def test_bad_metadata_and_unknown_document_degrade_without_500(self):
        bad = LCDocument(page_content="正文", metadata={
            "document_id": "not-a-uuid", "filename": 123, "chunk_index": True,
            "chunk_start": 1.5, "chunk_end": -2, "index_version": -1,
            "retrieval_method": "other", "page_start": 0,
        })
        unknown = uuid.uuid4()
        other = LCDocument(page_content="正文", metadata={
            "document_id": str(unknown), "filename": "b.pdf", "index_version": 1,
        })
        self.db.scalars.return_value.all.return_value = [self.ready_row()]
        data = self.run_diagnostics([(bad, float("nan")), (other, 0.5)])
        first, second = data["candidates"]
        self.assertIsNone(first["document_id"])
        self.assertIsNone(first["filename"])
        self.assertIsNone(first["chunk_index"])
        self.assertIsNone(first["chunk_start"])
        self.assertEqual(first["chunk_end"], -2)
        self.assertIsNone(first["index_version"])
        self.assertEqual(first["location"], {})
        self.assertIsNone(first["retrieval_method"])
        self.assertEqual(first["scores"], {})
        self.assertIsNone(first["lifecycle_state"])
        self.assertIsNone(first["retrievable"])
        self.assertEqual(second["document_id"], unknown)
        self.assertIsNone(second["lifecycle_state"])
        self.assertIsNone(second["retrievable"])


class DiagnosticsRequestSchemaTests(unittest.TestCase):
    def test_question_is_trimmed(self):
        self.assertEqual(RetrievalDiagnosticsRequest(question="  问题  ").question, "问题")

    def test_blank_question_is_rejected(self):
        for value in ("", "   ", "\n\t"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                RetrievalDiagnosticsRequest(question=value)

    def test_question_length_boundary(self):
        self.assertEqual(len(RetrievalDiagnosticsRequest(question="x" * 500).question), 500)
        self.assertEqual(RetrievalDiagnosticsRequest(question="  " + "x" * 500 + "  ").question, "x" * 500)
        with self.assertRaises(ValidationError):
            RetrievalDiagnosticsRequest(question="x" * 501)

    def test_document_ids_are_deduped_in_order_and_capped_at_five(self):
        first, second = uuid.uuid4(), uuid.uuid4()
        request = RetrievalDiagnosticsRequest(question="q", document_ids=[first, second, first])
        self.assertEqual(request.document_ids, [first, second])
        self.assertEqual(len(RetrievalDiagnosticsRequest(
            question="q", document_ids=[first] * 6).document_ids), 1)
        with self.assertRaises(ValidationError):
            RetrievalDiagnosticsRequest(question="q", document_ids=[uuid.uuid4() for _ in range(6)])

    def test_invalid_uuid_is_rejected(self):
        with self.assertRaises(ValidationError):
            RetrievalDiagnosticsRequest(question="q", document_ids=["not-a-uuid"])

    def test_extra_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            RetrievalDiagnosticsRequest(question="q", unexpected=True)


if TEST_URL:
    from sqlalchemy import create_engine

    from app.db import session as db_session

    _target = make_url(TEST_URL)
    if db_session.engine.url != _target:
        db_session.engine.dispose()
        db_session.engine = create_engine(TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
        db_session.SessionLocal.configure(bind=db_session.engine)

    from app.db.init_db import init_db
    from app.models import Document, KnowledgeBase, User

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DiagnosticsDatabaseTests(unittest.TestCase):
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
        with SessionLocal() as db:
            admin = User(username="diag_admin", role="admin", is_active=True, password_hash="x")
            db.add(admin)
            db.flush()
            kb = KnowledgeBase(name="diag-kb", collection_name="d_" + uuid.uuid4().hex,
                               owner_id=admin.id, is_public=True)
            db.add(kb)
            db.commit()
            self.admin_id, self.kb_id = admin.id, kb.id

    def add_document(self, filename="doc.txt", **overrides):
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename=filename, file_ext=".txt", file_size=1,
                           stored_path="missing", **{"status": "ready", **overrides})
            db.add(doc)
            db.commit()
            return doc.id

    def test_kb_counts_scope_echo_and_governance_boundaries(self):
        from fastapi import HTTPException

        active = self.add_document("active.txt")
        self.add_document("disabled.txt", enabled=False)
        pending = self.add_document("pending.txt", status="pending")
        with SessionLocal() as db:
            kb = db.get(KnowledgeBase, self.kb_id)
            admin = db.get(User, self.admin_id)
            with patch.object(diagnostics.rag, "retrieve", return_value=[]) as retrieve:
                data = diagnostics.run(db, kb, "问题", [], user=admin)
            self.assertEqual(data["kb"], {"total_documents": 3, "ready_documents": 2,
                                          "retrievable_documents": 1})
            retrieve.assert_called_once()
            self.assertIsNone(retrieve.call_args.kwargs["document_scope"])
            self.assertFalse(data["answer_generated"])

            with patch.object(diagnostics.rag, "retrieve", return_value=[]):
                scoped = diagnostics.run(db, kb, "问题", [active], user=admin)
            self.assertEqual(scoped["scope"], [
                {"document_id": str(active), "filename": "active.txt", "index_version": 0},
            ])

            with self.assertRaises(HTTPException) as caught:
                diagnostics.run(db, kb, "问题", [uuid.uuid4()], user=admin)
            self.assertEqual(caught.exception.status_code, 404)
            with self.assertRaises(HTTPException) as caught:
                diagnostics.run(db, kb, "问题", [pending], user=admin)
            # 非可检索文档先被 retrievable_conditions() SQL 预过滤掉，按“范围外”返回 404；
            # 409 仅保留给通过 SQL 后对象级复核失败的锁等待竞态。
            self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
