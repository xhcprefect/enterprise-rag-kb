"""T3 发布路径的真实 PostgreSQL 回归：来源快照必须可复核、失败不写成功回答。

破坏性 fixture 前先走共享门 ``tests._memory_project_test_gate``（loopback / ``_test``
后缀 / owner 标签 / 实际 engine 一致 / ``pg_database`` comment 精确匹配），任何不符硬 fail。
仅 TRUNCATE 本文件拥有的表；无配置时整类 skip 并明说未运行。
"""
import unittest
import uuid

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from fastapi import HTTPException
from sqlalchemy import select, text

from app.services.answer_sources import SOURCE_SNAPSHOT_VERSION

if TEST_URL:
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, engine
    from app.models import (
        Conversation,
        Document,
        DocumentChunk,
        KnowledgeBase,
        Message,
        ProjectChunk,
        ProjectFileVersion,
        ProjectSnapshot,
        ProjectSnapshotFile,
        ProjectSource,
        User,
    )
    from app.services import chat_turns
    from app.services.answer_sources import SOURCE_PROVENANCE_FIELD


MANIFEST = "a" * 64
CONFIG = "b" * 64
TABLES = ("users, knowledge_bases, documents, document_chunks, conversations, chat_turns, "
          "messages, project_sources, project_snapshots, project_file_versions, "
          "project_snapshot_files, project_chunks, audit_logs")


def _empty(mode="rag"):
    return {"source_snapshot_version": SOURCE_SNAPSHOT_VERSION,
            "supplied_sources": [], "history_sources": []}


def _doc_source(document_id, *, index_version=0, chunk_index=0):
    return {"document_id": str(document_id), "index_version": index_version,
            "chunk_index": chunk_index, "filename": "a.txt"}


def _project_source(source):
    return {"source_kind": "project", "document_id": str(source["file_version_id"]),
            "file_version_id": str(source["file_version_id"]),
            "snapshot_id": str(source["snapshot_id"]), "source_id": str(source["source_id"]),
            "relative_path": source["relative_path"], "manifest_fingerprint": MANIFEST,
            "config_fingerprint": CONFIG, "chunk_index": 0}


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离发布数据库测试")
class AnswerPublicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            self.user = User(username="pub_user", role="user", is_active=True,
                             password_hash="x", token_version=5)
            self.owner = User(username="pub_owner", role="user", is_active=True,
                              password_hash="x", token_version=0)
            db.add_all([self.user, self.owner])
            db.flush()
            self.kb = KnowledgeBase(name="pub-kb", collection_name="pub_" + uuid.uuid4().hex,
                                    owner_id=self.owner.id, is_public=True, kind="upload")
            # 项目库私有：认领者本人即拥有者（ACL 由真实 DB 判定）。
            self.project_kb = KnowledgeBase(name="pub-project", collection_name="pubp_" + uuid.uuid4().hex,
                                            owner_id=self.user.id, is_public=False, kind="project")
            db.add_all([self.kb, self.project_kb])
            db.commit()
            self.user_id, self.owner_id = self.user.id, self.owner.id
            self.kb_id, self.project_kb_id = self.kb.id, self.project_kb.id
        self.token = 5

    # ---------- fixtures ----------

    def add_document(self, *, embedded=True, index_version=0, kb_id=None):
        with SessionLocal() as db:
            doc = Document(kb_id=kb_id or self.kb_id, filename="a.txt", file_ext=".txt",
                           file_size=1, stored_path=f"/tmp/{uuid.uuid4().hex}.txt",
                           status="ready", cancel_requested=False, index_version=index_version,
                           parse_done=True, chunk_count=1)
            db.add(doc)
            db.flush()
            db.add(DocumentChunk(document_id=doc.id, chunk_index=0, content="正文", embedded=embedded))
            db.commit()
            return doc.id

    def add_project(self, *, status="ready", revoked=False):
        with SessionLocal() as db:
            source = ProjectSource(kb_id=self.project_kb_id, name="src", root_key="root",
                                   relative_dir="", root_fingerprint="f" * 64,
                                   created_by=self.owner_id)
            db.add(source)
            db.flush()
            snapshot = ProjectSnapshot(source_id=source.id, status=status,
                                       manifest_fingerprint=MANIFEST, config_fingerprint=CONFIG)
            db.add(snapshot)
            db.flush()
            version = ProjectFileVersion(source_id=source.id, relative_path="src/main.py",
                                         content_sha256="c" * 64, blob_path="/tmp/blob",
                                         parse_done=True, parse_status="ready",
                                         config_fingerprint=CONFIG)
            db.add(version)
            db.flush()
            db.add(ProjectSnapshotFile(snapshot_id=snapshot.id, relative_path="src/main.py",
                                       file_version_id=version.id))
            db.add(ProjectChunk(file_version_id=version.id, chunk_index=0, content="代码",
                                embedded=True))
            if revoked:
                source.revoked_at = chat_turns._now()
            else:
                source.active_snapshot_id = snapshot.id
            db.commit()
            return {"source_id": source.id, "snapshot_id": snapshot.id,
                    "file_version_id": version.id, "relative_path": "src/main.py"}

    def claim(self, *, mode="rag", kb_id="upload", conversation_id=None, **kwargs):
        with SessionLocal() as db:
            reservation = chat_turns.claim_turn(
                db, user_id=self.user_id, conversation_id=conversation_id,
                kb_id=(self.kb_id if kb_id == "upload" else self.project_kb_id) if mode == "rag" else None,
                mode=mode, question="问题", top_k=6, request_id=uuid.uuid4(),
                auth_token_version=self.token, **kwargs)
        return reservation

    def complete(self, reservation, trace, citations=(), answer="回答"):
        with SessionLocal() as db:
            return chat_turns.complete_turn(db, reservation, answer, list(citations), 1, trace)

    def assert_no_answer(self, reservation):
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant")))
            self.assertEqual(db.get(chat_turns.ChatTurn, reservation.turn_id).status, "processing")

    # ---------- positives ----------

    def test_rag_supplied_document_is_published_with_explicit_provenance(self):
        document_id = self.add_document()
        reservation = self.claim()
        trace = {"source_snapshot_version": 1, "supplied_sources": [_doc_source(document_id)],
                 "history_sources": [], "status": "ok"}
        self.complete(reservation, trace, [_doc_source(document_id)])
        with SessionLocal() as db:
            turn = db.get(chat_turns.ChatTurn, reservation.turn_id)
            self.assertEqual(turn.status, "completed")
            self.assertEqual(turn.trace["supplied_sources"][0]["document_id"], str(document_id))
            self.assertEqual(turn.trace["history_sources"], [])
            answer = db.scalar(select(Message).where(Message.turn_id == reservation.turn_id,
                                                     Message.role == "assistant"))
            self.assertEqual(answer.status, "completed")
            self.assertIsNone(db.get(Conversation, reservation.conversation_id).active_turn_id)

    def test_no_source_rag_refusal_is_published_with_empty_lists(self):
        reservation = self.claim()
        self.complete(reservation, _empty(), [], answer="根据现有资料无法回答。")
        with SessionLocal() as db:
            self.assertEqual(db.get(chat_turns.ChatTurn, reservation.turn_id).status, "completed")

    def test_plain_chat_is_published_and_never_gets_sources(self):
        reservation = self.claim(mode="chat")
        self.complete(reservation, _empty("chat"))
        with SessionLocal() as db:
            self.assertEqual(db.get(chat_turns.ChatTurn, reservation.turn_id).status, "completed")

    def test_ready_project_source_is_published_under_its_fixed_snapshot(self):
        project = self.add_project()
        reservation = self.claim(kb_id="project")
        self.assertEqual(reservation.project_snapshot_id, project["snapshot_id"])
        self.complete(reservation, {"source_snapshot_version": 1,
                                    "supplied_sources": [_project_source(project)],
                                    "history_sources": []})
        with SessionLocal() as db:
            self.assertEqual(db.get(chat_turns.ChatTurn, reservation.turn_id).status, "completed")

    # ---------- fail-closed negatives ----------

    def test_missing_or_legacy_trace_never_writes_a_successful_answer(self):
        for trace in ({}, {"supplied_sources": [], "history_sources": []}, None):
            with self.subTest(trace=trace):
                reservation = self.claim()
                with self.assertRaises(chat_turns.ChatLeaseLost):
                    self.complete(reservation, trace)
                self.assert_no_answer(reservation)

    def test_plain_chat_with_supplied_source_is_rejected(self):
        document_id = self.add_document()
        reservation = self.claim(mode="chat")
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation, {"source_snapshot_version": 1,
                                        "supplied_sources": [_doc_source(document_id)],
                                        "history_sources": []})
        self.assert_no_answer(reservation)

    def test_invalid_supplied_source_blocks_refusal_with_empty_citations(self):
        document_id = self.add_document(embedded=False)
        reservation = self.claim()
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation, {"source_snapshot_version": 1,
                                        "supplied_sources": [_doc_source(document_id)],
                                        "history_sources": []}, citations=[])
        self.assert_no_answer(reservation)
        # 让本轮可以干净结束，避免影响后续断言
        with SessionLocal() as db:
            chat_turns.cancel_request(db, reservation.request_id, self.user_id)

    def test_history_source_is_validated_even_when_not_cited(self):
        document_id = self.add_document()
        reservation = self.claim()
        with SessionLocal() as db:
            db.execute(text("UPDATE documents SET index_version = 1 WHERE id = :id"), {"id": document_id})
            db.commit()
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation, {"source_snapshot_version": 1, "supplied_sources": [],
                                        "history_sources": [_doc_source(document_id)]})
        self.assert_no_answer(reservation)

    def test_revoked_token_blocks_publication(self):
        reservation = self.claim()
        with SessionLocal() as db:
            db.execute(text("UPDATE users SET token_version = token_version + 1 WHERE id = :id"),
                       {"id": self.user_id})
            db.commit()
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation, _empty())
        self.assert_no_answer(reservation)

    def test_disabled_user_and_lost_acl_block_publication(self):
        for statement in ("UPDATE users SET is_active = false WHERE id = :id",
                          "UPDATE knowledge_bases SET is_public = false WHERE id = :id"):
            with self.subTest(statement=statement):
                with SessionLocal() as db:
                    db.execute(text("UPDATE users SET is_active = true WHERE id = :id"),
                               {"id": self.user_id})
                    db.execute(text("UPDATE knowledge_bases SET is_public = true WHERE id = :id"),
                               {"id": self.kb_id})
                    db.commit()
                reservation = self.claim()
                with SessionLocal() as db:
                    db.execute(text(statement),
                               {"id": self.user_id if "users" in statement else self.kb_id})
                    db.commit()
                with self.assertRaises(chat_turns.ChatLeaseLost):
                    self.complete(reservation, _empty())
                self.assert_no_answer(reservation)

    def test_reindex_during_generation_blocks_scope_answer(self):
        document_id = self.add_document()
        reservation = self.claim(document_ids=[document_id])
        with SessionLocal() as db:
            db.execute(text("UPDATE documents SET index_version = 1 WHERE id = :id"), {"id": document_id})
            db.commit()
        with self.assertRaises(HTTPException) as error:
            self.complete(reservation, _empty())
        self.assertEqual(error.exception.status_code, 409)
        self.assert_no_answer(reservation)

    def test_revoked_project_source_blocks_publication(self):
        project = self.add_project()
        reservation = self.claim(kb_id="project")
        with SessionLocal() as db:
            db.execute(text("UPDATE project_sources SET revoked_at = now() WHERE id = :id"),
                       {"id": project["source_id"]})
            db.commit()
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation, _empty())
        self.assert_no_answer(reservation)

    def test_project_source_from_a_different_snapshot_is_rejected(self):
        project = self.add_project()
        reservation = self.claim(kb_id="project")
        other = {"snapshot_id": uuid.uuid4(), "file_version_id": uuid.uuid4(),
                 "source_id": project["source_id"], "relative_path": "src/main.py"}
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation, {"source_snapshot_version": 1,
                                        "supplied_sources": [_project_source(other)],
                                        "history_sources": []})
        self.assert_no_answer(reservation)

    def test_nonready_project_snapshot_cannot_be_claimed(self):
        self.add_project(status="pending")
        with self.assertRaises(HTTPException):
            self.claim(kb_id="project")
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(chat_turns.ChatTurn)), None)


class ReservationAuthFixtureTests(unittest.TestCase):
    """纯离线：合法旧式 fixture 必须显式给 auth/source，不存在 module/mock 放行旁路。"""

    def test_reservation_requires_explicit_token_for_publication(self):
        from app.services import chat_turns
        reservation = chat_turns.Reservation(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(),
                                             uuid.uuid4(), uuid.uuid4(), mode="chat")
        self.assertIsNone(reservation.auth_token_version)
        with self.assertRaises(chat_turns.ChatLeaseLost):
            chat_turns.complete_turn(None, reservation, "a", [], 1, _empty("chat"))


if __name__ == "__main__":
    unittest.main()