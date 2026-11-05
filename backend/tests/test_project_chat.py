"""项目回答的来源身份与追问隔离；无需数据库或模型。"""
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.documents import Document

from app.api.v1 import chat
from app.schemas.chat import Citation, ProjectSnapshotUpdate
from app.services import followup, project_retrieval, rag


def project_document(snapshot_id, *, file_id=None, chunk_index=0, content="原始项目内容"):
    file_id = file_id or uuid.uuid4()
    return Document(page_content=content, metadata={
        "source_kind": "project", "source_id": str(uuid.uuid4()),
        "snapshot_id": str(snapshot_id), "file_version_id": str(file_id),
        "document_id": str(file_id), "chunk_index": chunk_index,
        "relative_path": "src/worker.py", "language": "python",
        "symbol": "run", "symbol_kind": "function", "line_start": 3, "line_end": 8,
        "snapshot_manifest_fingerprint": "a" * 64,
        "snapshot_config_fingerprint": "b" * 64,
    })


class ProjectCitationTests(unittest.TestCase):
    def test_project_identity_survives_context_selection_and_public_schema(self):
        snapshot_id = uuid.uuid4()
        doc = project_document(snapshot_id)
        prepared = rag.prepare_answer("run 如何工作？", [(doc, 0.2)], [])
        candidate = rag.to_citations(prepared.pairs)[0]
        citation = Citation.model_validate(rag.finalize_citations("工作流程见[1]。", [candidate])[0])
        self.assertEqual(citation.source_kind, "project")
        self.assertEqual(citation.snapshot_id, str(snapshot_id))
        self.assertEqual(citation.file_version_id, doc.metadata["file_version_id"])
        self.assertEqual(citation.filename, "src/worker.py")
        self.assertEqual(citation.relative_path, "src/worker.py")
        self.assertEqual((citation.line_start, citation.line_end), (3, 8))
        self.assertEqual(citation.symbol, "run")
        self.assertEqual(citation.manifest_fingerprint, "a" * 64)
        self.assertEqual(citation.config_fingerprint, "b" * 64)
        self.assertIn("来源：src/worker.py", prepared.messages[-1].content)
        self.assertIn("原始项目内容", prepared.messages[-1].content)

    def test_snapshot_update_requires_revision_and_rejects_extra_fields(self):
        from pydantic import ValidationError
        for values in ({"snapshot_id": uuid.uuid4()},
                       {"snapshot_id": uuid.uuid4(), "expected_revision": True},
                       {"snapshot_id": uuid.uuid4(), "expected_revision": 0, "kb_id": uuid.uuid4()}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ProjectSnapshotUpdate(**values)


class ProjectFollowupTests(unittest.TestCase):
    def setUp(self):
        self.snapshot_id, self.user_id = uuid.uuid4(), uuid.uuid4()
        self.db = MagicMock()
        self.db.__enter__.return_value = self.db
        self.db.get.return_value = SimpleNamespace(id=self.user_id, is_active=True)
        self.doc = project_document(self.snapshot_id)
        self.citation = rag.to_citations([(self.doc, 0.1)])[0]

    def retrieve(self, plan, *, anchors=None, fresh=None):
        with patch.object(chat, "SessionLocal", return_value=self.db), \
             patch.object(project_retrieval, "load_project_citations", return_value=anchors or []) as load, \
             patch.object(project_retrieval, "retrieve_project", return_value=fresh or []) as search:
            result = chat._retrieve_project_plan(self.snapshot_id, self.user_id, plan, 6)
        return result, load, search

    def test_reference_reloads_original_body_and_preserves_historical_number(self):
        plan = followup.RetrievalPlan("解释来源3", "reference", citations=[{**self.citation, "source_index": 3}])
        result, load, search = self.retrieve(plan, anchors=[(self.doc, 0.0)])
        self.assertEqual(result[0][0][0].page_content, "原始项目内容")
        self.assertEqual(result[0][0][0].metadata["historical_source_index"], 3)
        load.assert_called_once()
        search.assert_not_called()
        self.assertIsNone(result[2], "项目追问不能隐式写入普通文档范围")

    def test_previous_snapshot_or_document_reference_requires_clarification(self):
        for overrides in ({"snapshot_id": str(uuid.uuid4())}, {"source_kind": "document"}):
            plan = followup.RetrievalPlan("解释来源1", "reference", citations=[{**self.citation, **overrides}])
            with self.subTest(overrides=overrides):
                result, load, search = self.retrieve(plan)
                self.assertEqual(result[0], [])
                self.assertEqual(result[1].status, "clarify")
                load.assert_not_called()
                search.assert_not_called()

    def test_missing_original_never_falls_back_to_unrelated_project_hits(self):
        plan = followup.RetrievalPlan("worker.py", "document_followup", citations=[self.citation], topic="旧问题")
        result, load, search = self.retrieve(plan, fresh=[project_document(self.snapshot_id)])
        load.assert_called_once()
        search.assert_not_called()
        self.assertTrue(result[1].clarification)

    def test_named_file_followup_limits_supplemental_search_to_verified_file_versions(self):
        plan = followup.RetrievalPlan("worker.py", "document_followup", citations=[self.citation], topic="旧问题")
        fresh = project_document(self.snapshot_id, file_id=self.doc.metadata["file_version_id"], chunk_index=1)
        result, _load, search = self.retrieve(plan, anchors=[(self.doc, 0.0)], fresh=[self.doc, fresh])
        self.assertEqual(search.call_args.kwargs["file_version_ids"], [self.doc.metadata["file_version_id"]])
        self.assertEqual(search.call_args.args[3], "旧问题")
        self.assertEqual([doc.metadata["chunk_index"] for doc, _ in result[0]], [0, 1])
        self.assertIsNone(result[0][-1][0].metadata["citation_score"], "未知距离不能显示成100%相似")


if __name__ == "__main__":
    unittest.main()
