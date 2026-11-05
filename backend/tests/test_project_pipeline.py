"""项目同步→真实 pgvector 检索→SSE→引用预览的隔离数据库验收。

只使用显式 owner 标记的 loopback *_test 库；模型用确定性替身，源目录和
blob 都在 TemporaryDirectory 中。测试不启动服务或碰真实项目目录。
"""
import json
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from sqlalchemy import delete, select, update

from tests import _memory_project_test_gate as gate

from app.core.config import settings
from app.core.security import create_access_token
from app.db import session as db_session
from app.db.init_db import init_db
from app.models import ChatTurn, Conversation, KnowledgeBase, ProjectChunk, ProjectFileVersion, ProjectSnapshot, ProjectSource, User
from app.services import project_index, project_retrieval, project_sources, rag, vector_store


class FixtureEmbeddings(Embeddings):
    def embed_documents(self, texts):
        return [[1.0] + [0.0] * (vector_store.EMBEDDING_DIM - 1) for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * (vector_store.EMBEDDING_DIM - 1)


def events(response):
    result = {}
    for frame in response.text.split("\n\n"):
        lines = frame.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if event and data:
            result.setdefault(event, []).append(json.loads(data))
    return result


@unittest.skipIf(gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
                 "需要显式 owned PostgreSQL 测试库")
class ProjectPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        init_db(create_admin=False)
        with patch("app.core.logging.setup_logging"):
            from app.main import app
        cls.app = app

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.notes = self.root / "notes.md"
        self.notes.write_text("# 项目说明\n\nALPHA_VERSION_ONE：后台任务执行规则。\n", encoding="utf-8")
        self.embeddings = FixtureEmbeddings()
        for patcher in (
            patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"fixture": str(self.root)}),
            patch.object(settings, "PROJECT_STORAGE_DIR", str(Path(self.temp.name) / "blobs")),
            patch.object(settings, "RETRIEVAL_REWRITE_ENABLED", False),
            patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False),
            patch.object(settings, "RERANK_ENABLED", False),
            patch.object(vector_store, "_vector_engine", return_value=db_session.engine),
            patch.object(vector_store, "get_embeddings", return_value=self.embeddings),
            patch.object(project_index, "get_embeddings", return_value=self.embeddings),
            patch.object(project_retrieval, "get_embeddings", return_value=self.embeddings),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        vector_store._store.cache_clear()
        self.addCleanup(vector_store._store.cache_clear)
        with db_session.SessionLocal() as db:
            self.user = User(username="pipeline_" + uuid.uuid4().hex, role="admin", is_active=True,
                             password_hash="test-only-no-login")
            db.add(self.user)
            db.flush()
            source, kb = project_sources.create_project_source(
                db, self.user, name="pipeline_" + uuid.uuid4().hex, root_key="fixture", relative_dir="",
            )
            db.commit()
            self.user_id, self.source_id, self.kb_id = self.user.id, source.id, kb.id
            self.collection_name = kb.collection_name
        self.addCleanup(self.cleanup_rows)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + create_access_token(str(self.user_id), "admin")}
        self.prompts = []

    def cleanup_rows(self):
        with db_session.SessionLocal() as db:
            db.execute(delete(Conversation).where(Conversation.user_id == self.user_id))
            db.execute(delete(ChatTurn).where(ChatTurn.user_id == self.user_id))
            db.execute(delete(KnowledgeBase).where(KnowledgeBase.id == self.kb_id))
            db.execute(delete(User).where(User.id == self.user_id))
            db.commit()
        vector_store.get_vector_store(self.collection_name).delete_collection()

    def sync(self):
        with db_session.SessionLocal() as db:
            snap = project_index.enqueue_project_sync(db, self.source_id, db.get(User, self.user_id))
            db.commit()
            snapshot_id = snap.id
        claim = project_index.claim_next_project_snapshot()
        self.assertIsNotNone(claim)
        self.assertEqual(claim.snapshot_id, snapshot_id)
        self.assertIsInstance(claim.lease_id, uuid.UUID)
        status = project_index.run_project_sync(claim, threading.Event())
        with db_session.SessionLocal() as db:
            snap = db.get(ProjectSnapshot, snapshot_id)
            self.assertEqual(status, "ready", f"{snap.error_code}: {snap.error_message}")
            self.assertEqual(snap.status, "ready")
            self.assertEqual(db.get(ProjectSource, self.source_id).active_snapshot_id, snapshot_id)
        return snapshot_id

    async def answer(self, _question, _pairs, _history, **options):
        self.prompts.append(options["prepared"].messages[-1].content)
        yield "项目执行规则见[1]。"

    def ask(self, *, snapshot_id=None, conversation_id=None, question="说明 ALPHA 项目任务执行规则", request_id=None):
        body = {"kb_id": str(self.kb_id), "question": question, "mode": "rag", "include_trace": True,
                "request_id": str(request_id or uuid.uuid4())}
        if snapshot_id is not None:
            body["project_snapshot_id"] = str(snapshot_id)
        if conversation_id is not None:
            body["conversation_id"] = str(conversation_id)
        with patch.object(rag, "answer_astream", side_effect=self.answer), \
             patch.object(rag, "retrieve") as document_search:
            response = self.client.post("/api/v1/chat/stream", headers=self.headers, json=body)
        document_search.assert_not_called()
        return response, events(response), body

    def test_sync_vectors_chat_replay_preview_and_explicit_snapshot_switch(self):
        first = self.sync()
        # 禁止关键词兜底把坏向量关联掩盖成成功。
        with patch.object(project_retrieval, "_project_keyword_pairs", return_value=[]) as keyword:
            response, result, original = self.ask(snapshot_id=first)
        keyword.assert_not_called()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("done", result, response.text)
        self.assertIn("ALPHA_VERSION_ONE", self.prompts[-1])
        conv_id = result["meta"][0]["conversation_id"]
        citation = result["done"][0]["citations"][0]
        self.assertEqual(citation["source_kind"], "project")
        self.assertEqual(citation["snapshot_id"], str(first))
        self.assertEqual(citation["relative_path"], "notes.md")
        self.assertEqual(citation["filename"], "notes.md")
        self.assertTrue(citation["manifest_fingerprint"])
        self.assertTrue(citation["config_fingerprint"])
        self.assertEqual(result["meta"][0]["project_snapshot_id"], str(first))
        self.assertEqual(result["done"][0]["project_snapshot_id"], str(first))

        self.notes.write_text("# 项目说明\n\nALPHA_VERSION_TWO：任务按新规则执行。\n", encoding="utf-8")
        second = self.sync()
        # active 已更新，显式旧快照和既有会话都继续按旧快照回答。
        _, explicit_old, _ = self.ask(snapshot_id=first)
        self.assertEqual(explicit_old["done"][0]["project_snapshot_id"], str(first))
        _, pinned, _ = self.ask(conversation_id=conv_id)
        self.assertEqual(pinned["done"][0]["project_snapshot_id"], str(first))
        self.assertIn("ALPHA_VERSION_ONE", self.prompts[-1])
        self.assertNotIn("ALPHA_VERSION_TWO", self.prompts[-1])
        mismatch, _, _ = self.ask(snapshot_id=second, conversation_id=conv_id)
        self.assertEqual(mismatch.status_code, 409)

        changed = self.client.patch(f"/api/v1/chat/conversations/{conv_id}/project-snapshot", headers=self.headers,
                                    json={"snapshot_id": str(second), "expected_revision": 0})
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["data"]["scope_revision"], 1)
        self.assertEqual(changed.json()["data"]["project_snapshot_id"], str(second))
        stale = self.client.patch(f"/api/v1/chat/conversations/{conv_id}/project-snapshot", headers=self.headers,
                                  json={"snapshot_id": str(first), "expected_revision": 0})
        self.assertEqual(stale.status_code, 409)
        _, switched, _ = self.ask(conversation_id=conv_id)
        self.assertEqual(switched["done"][0]["project_snapshot_id"], str(second))
        self.assertIn("ALPHA_VERSION_TWO", self.prompts[-1])

        with patch.object(rag, "answer_astream") as model:
            replay = self.client.post("/api/v1/chat/stream", headers=self.headers, json=original)
        model.assert_not_called()
        self.assertEqual(events(replay)["done"][0]["project_snapshot_id"], str(first))
        state = self.client.get(f"/api/v1/chat/requests/{original['request_id']}", headers=self.headers)
        self.assertEqual(state.status_code, 200, state.text)
        self.assertEqual(state.json()["data"]["project_snapshot_id"], str(first))
        detail = self.client.get(f"/api/v1/chat/conversations/{conv_id}", headers=self.headers)
        self.assertEqual(detail.json()["data"]["project_snapshot_id"], str(second))
        preview = self.client.get(
            f"/api/v1/projects/{self.source_id}/files/{citation['file_version_id']}/source",
            headers=self.headers, params={"snapshot_id": str(first), "chunk_start": citation["chunk_start"],
                                          "chunk_end": citation["chunk_end"]},
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertIn("ALPHA_VERSION_ONE", json.dumps(preview.json()))
        self.assertNotIn("ALPHA_VERSION_TWO", json.dumps(preview.json()))

    def test_snapshot_switch_obeys_owner_busy_and_project_scope_guards(self):
        from app.services import chat_turns

        snapshot_id = self.sync()
        response, result, _original = self.ask(snapshot_id=snapshot_id)
        self.assertIn("done", result, response.text)
        conv_id = result["meta"][0]["conversation_id"]
        scope = self.client.patch(f"/api/v1/chat/conversations/{conv_id}/scope", headers=self.headers,
                                  json={"document_ids": [], "expected_revision": 0})
        self.assertEqual(scope.status_code, 400)
        with db_session.SessionLocal() as db:
            other = User(username="other_admin_" + uuid.uuid4().hex, role="admin", is_active=True,
                         password_hash="test-only")
            db.add(other)
            db.commit()
            other_id = other.id
        try:
            other_headers = {"Authorization": "Bearer " + create_access_token(str(other_id), "admin")}
            with patch.object(rag, "answer_astream") as model:
                denied = self.client.post("/api/v1/chat/stream", headers=other_headers,
                                          json={"question": "不能续聊他人会话", "conversation_id": conv_id})
                changed = self.client.patch(f"/api/v1/chat/conversations/{conv_id}/project-snapshot",
                                            headers=other_headers,
                                            json={"snapshot_id": str(snapshot_id), "expected_revision": 0})
            self.assertEqual(denied.status_code, 404)
            self.assertEqual(changed.status_code, 404)
            model.assert_not_called()
        finally:
            with db_session.SessionLocal() as db:
                db.execute(delete(User).where(User.id == other_id))
                db.commit()
        with db_session.SessionLocal() as db:
            reservation = chat_turns.claim_turn(
                db, user_id=self.user_id, conversation_id=uuid.UUID(conv_id), kb_id=self.kb_id,
                mode="rag", question="正在生成", top_k=6,
            )
        try:
            busy = self.client.patch(f"/api/v1/chat/conversations/{conv_id}/project-snapshot", headers=self.headers,
                                     json={"snapshot_id": str(snapshot_id), "expected_revision": 0})
            self.assertEqual(busy.status_code, 409)
        finally:
            chat_turns.end_turn(reservation)

    def test_revocation_blocks_replay_recovery_history_and_source_preview(self):
        snapshot_id = self.sync()
        response, result, original = self.ask(snapshot_id=snapshot_id)
        self.assertIn("done", result, response.text)
        conv_id = result["meta"][0]["conversation_id"]
        citation = result["done"][0]["citations"][0]
        with db_session.SessionLocal() as db:
            db.get(ProjectSource, self.source_id).revoked_at = datetime.now(timezone.utc)
            db.commit()
        for path in (f"/api/v1/chat/requests/{original['request_id']}",
                     f"/api/v1/chat/conversations/{conv_id}",
                     f"/api/v1/projects/{self.source_id}/files/{citation['file_version_id']}/source"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=self.headers).status_code, 410)
        with patch.object(rag, "answer_astream") as model:
            replay = self.client.post("/api/v1/chat/stream", headers=self.headers, json=original)
        self.assertEqual(replay.status_code, 410)
        model.assert_not_called()

    def test_source_revoked_during_generation_cannot_publish_a_completed_answer(self):
        snapshot_id = self.sync()

        async def revoked_answer(*_args, **_kwargs):
            with db_session.SessionLocal() as db:
                db.get(ProjectSource, self.source_id).revoked_at = datetime.now(timezone.utc)
                db.commit()
            yield "这份迟到的结果[1]不能发布。"

        with patch.object(self, "answer", side_effect=revoked_answer):
            response, result, original = self.ask(snapshot_id=snapshot_id)
        self.assertNotIn("done", result, response.text)
        self.assertEqual(result["error"][0]["code"], "lease_lost")
        with db_session.SessionLocal() as db:
            turn = db.scalar(select(ChatTurn).where(ChatTurn.request_id == uuid.UUID(original["request_id"])))
            self.assertEqual(turn.status, "cancelled")

    def test_named_file_followup_searches_verified_file_before_ranking(self):
        snapshot_id = self.sync()
        response, result, _original = self.ask(snapshot_id=snapshot_id)
        self.assertIn("done", result, response.text)
        conv_id = result["meta"][0]["conversation_id"]
        file_id = result["done"][0]["citations"][0]["file_version_id"]
        with patch.object(project_retrieval, "_project_keyword_pairs", return_value=[]) as keyword:
            response, result, _ = self.ask(conversation_id=conv_id, question="notes.md")
        self.assertIn("done", result, response.text)
        self.assertEqual(result["done"][0]["trace"]["status"], "document_followup")
        self.assertIn("ALPHA_VERSION_ONE", self.prompts[-1])
        self.assertEqual({item["file_version_id"] for item in result["done"][0]["citations"]}, {file_id})
        keyword.assert_not_called()

    def test_legacy_vectors_without_chunk_metadata_are_retrieved(self):
        snapshot_id = self.sync()
        store = vector_store.get_vector_store(self.collection_name)
        with db_session.SessionLocal() as db:
            collection = store.get_collection(db)
            vectors = db.scalars(select(store.EmbeddingStore).where(
                store.EmbeddingStore.collection_id == collection.uuid,
            )).all()
            self.assertTrue(vectors)
            for vector in vectors:
                metadata = dict(vector.cmetadata)
                metadata.pop("chunk_index", None)
                vector.cmetadata = metadata
            db.commit()
        with patch.object(project_retrieval, "_project_keyword_pairs", return_value=[]) as keyword:
            response, result, _ = self.ask(snapshot_id=snapshot_id)
        self.assertIn("done", result, response.text)
        self.assertIn("ALPHA_VERSION_ONE", self.prompts[-1])
        self.assertEqual(result["done"][0]["citations"][0]["chunk_index"], 0)
        keyword.assert_not_called()

    def test_final_authorization_requires_the_candidates_own_embedded_chunk(self):
        (self.root / "other.md").write_text("# Other\n\nOTHER_FILE_MARKER\n", encoding="utf-8")
        snapshot_id = self.sync()
        with db_session.SessionLocal() as db:
            files = {file.relative_path: file.id for file in db.scalars(
                select(ProjectFileVersion).where(ProjectFileVersion.source_id == self.source_id)
            )}
            target, other = files["notes.md"], files["other.md"]
            db.execute(update(ProjectChunk).where(ProjectChunk.file_version_id == target)
                       .values(embedded=False))
            db.commit()
            user = db.get(User, self.user_id)
            kb = db.get(KnowledgeBase, self.kb_id)
            allowed = project_retrieval._still_authorized_fv_ids(
                db, kb, snapshot_id, user, [target, other], None,
            )
            self.assertEqual(allowed, {str(other)})

    def test_network_wait_rechecks_source_acl_and_snapshot_with_cached_rows(self):
        snapshot_id = self.sync()
        with db_session.SessionLocal() as db:
            viewer = User(username="viewer_" + uuid.uuid4().hex, role="user", is_active=True,
                          password_hash="test-only")
            db.add(viewer)
            db.commit()
            viewer_id = viewer.id
        try:
            for stage in ("embed", "rerank"):
                for change in ("revoked", "private", "not_ready"):
                    with self.subTest(stage=stage, change=change):
                        with db_session.SessionLocal() as db:
                            db.get(KnowledgeBase, self.kb_id).is_public = True
                            db.get(ProjectSource, self.source_id).revoked_at = None
                            db.get(ProjectSnapshot, snapshot_id).status = "ready"
                            db.commit()
                        expected = {
                            "revoked": project_sources.ProjectSourceRevoked,
                            "private": project_sources.ProjectSourceNotFound,
                            "not_ready": project_retrieval.ProjectSnapshotNotReady,
                        }[change]
                        with db_session.SessionLocal(expire_on_commit=False) as db:
                            viewer = db.get(User, viewer_id)
                            self.assertFalse(viewer.is_admin)
                            cached = project_retrieval.validate_project_binding(db, snapshot_id, viewer)
                            db.commit()

                            def change_during_wait(*args, **_kwargs):
                                self.assertFalse(db.in_transaction(), "模型等待期间不应持有事务")
                                with db_session.SessionLocal() as other:
                                    if change == "revoked":
                                        other.get(ProjectSource, self.source_id).revoked_at = datetime.now(timezone.utc)
                                    elif change == "private":
                                        other.get(KnowledgeBase, self.kb_id).is_public = False
                                    else:
                                        other.get(ProjectSnapshot, snapshot_id).status = "failed"
                                    other.commit()
                                # 保留引用，确保 identity map 中的授权三元组确实仍是旧值。
                                self.assertEqual(cached[0].status, "ready")
                                self.assertIsNone(cached[1].revoked_at)
                                self.assertTrue(cached[2].is_public)
                                return self.embeddings.embed_query(args[0]) if stage == "embed" else args[1]

                            target = "get_embeddings" if stage == "embed" else "rerank_results"
                            if stage == "embed":
                                from unittest.mock import Mock
                                replacement = Mock()
                                replacement.embed_query.side_effect = change_during_wait
                                patcher = patch.object(project_retrieval, target, return_value=replacement)
                            else:
                                patcher = patch.object(project_retrieval, target, side_effect=change_during_wait)
                            with patch.object(settings, "RERANK_ENABLED", stage == "rerank"), patcher, \
                                 patch.object(project_retrieval, "_project_keyword_pairs", return_value=[]):
                                with self.assertRaises(expected):
                                    project_retrieval.project_retrieve_documents(
                                        db, snapshot_id, viewer, "ALPHA", top_k=6,
                                    )
        finally:
            with db_session.SessionLocal() as db:
                db.execute(delete(User).where(User.id == viewer_id))
                db.commit()

    def test_template_only_vue_and_long_line_are_indexed_without_losing_tail(self):
        self.notes.unlink()
        long_line = "prefix_" + "x" * 2200 + "_IMPORTANT_TAIL\n"
        (self.root / "long.txt").write_bytes(long_line.encode("utf-8"))
        (self.root / "Card.vue").write_text("<template><div>hello</div></template>\n", encoding="utf-8")
        self.sync()
        with db_session.SessionLocal() as db:
            file = db.scalar(select(ProjectFileVersion).where(ProjectFileVersion.source_id == self.source_id,
                                                               ProjectFileVersion.relative_path == "long.txt"))
            chunks = db.scalars(select(ProjectChunk).where(ProjectChunk.file_version_id == file.id)
                                .order_by(ProjectChunk.chunk_index)).all()
            self.assertEqual("".join(chunk.content for chunk in chunks), long_line)
            self.assertTrue(all(chunk.embedded for chunk in chunks))
            self.assertTrue(all(chunk.line_start == chunk.line_end == 1 for chunk in chunks))
            vue = db.scalar(select(ProjectFileVersion).where(ProjectFileVersion.source_id == self.source_id,
                                                              ProjectFileVersion.relative_path == "Card.vue"))
            self.assertTrue(vue.parse_done)
            self.assertIn(vue.parse_status, {"ready", "fallback"})


if __name__ == "__main__":
    unittest.main()
