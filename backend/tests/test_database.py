"""真实 PostgreSQL/pgvector 回归；只接受显式提供、数据库名以 _test 结尾的连接。"""
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests._env import TEST_URL

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from langchain_core.embeddings import Embeddings
from sqlalchemy import delete, func, select, text, update

from app.api.deps import get_current_user
from app.api.v1 import auth, chat, knowledge, users
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.init_db import init_db
from app.db import session as db_session


def _bind_test_engine() -> None:
    """把应用 engine 重绑到专用测试库。

    `app.db.session` 在导入时就按当时的 DATABASE_URL 建好 engine，而
    `unittest discover` 按文件名导入测试模块，任何排在 `test_database` 之前、
    又导入了 `app.*` 的模块都会让 engine 先绑定到开发库。这里显式重建，
    使结果与导入顺序无关；未提供 TEST_DATABASE_URL 时保持原样。
    """
    if not TEST_URL:
        return
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker

    target = make_url(TEST_URL)
    if db_session.engine.url == target:
        return
    db_session.engine.dispose()
    db_session.engine = create_engine(
        TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20, pool_recycle=1800
    )
    db_session.SessionLocal.configure(bind=db_session.engine)


_bind_test_engine()
SessionLocal, engine = db_session.SessionLocal, db_session.engine
from app.models import ChatTurn, Conversation, Document, DocumentChunk, KnowledgeBase, Message, UploadSession, User
from app.schemas.auth import LoginRequest
from app.schemas.chat import ChatRequest
from app.schemas.user import ChangePassword, UserUpdate
from app.services import chat_turns, ingest, rag, upload, vector_store


class FakeEmbeddings(Embeddings):
    def __init__(self):
        self.calls = 0
        self.fail_on = None
        self.on_embed = None

    def embed_documents(self, texts):
        self.calls += 1
        if self.on_embed:
            self.on_embed()
        if self.calls == self.fail_on:
            raise RuntimeError("simulated upstream failure")
        return [[1.0] + [0.0] * 1023 for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * 1023


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(engine.url) != str(__import__("sqlalchemy").engine.make_url(TEST_URL)):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝清理")
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        # 仅在已双重校验的测试库内清空业务表。
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE users, knowledge_bases, documents, conversations, upload_sessions, audit_logs, document_chunks, messages RESTART IDENTITY CASCADE"))
            if conn.execute(text("SELECT to_regclass('langchain_pg_collection')")).scalar():
                conn.execute(text("TRUNCATE langchain_pg_collection CASCADE"))
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.embeddings = FakeEmbeddings()
        for patcher in (
            patch.object(settings, "UPLOAD_DIR", self.temp.name),
            patch.object(settings, "INGEST_BATCH_SIZE", 1),
            patch.object(settings, "UPLOAD_CHUNK_SIZE", 4),
            patch.object(vector_store, "get_embeddings", return_value=self.embeddings),
            # discover 不带 -t 时，向量模块可能早于测试库重绑导入；显式隔离其引擎。
            patch.object(vector_store, "_vector_engine", return_value=engine),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        vector_store._store.cache_clear()
        self.request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("127.0.0.1", 1)})
        with SessionLocal() as db:
            self.admin = User(username="admin_test", role="admin", is_active=True, password_hash=hash_password("Original123"))
            db.add(self.admin)
            db.flush()
            self.kb = KnowledgeBase(name="review", collection_name="test_" + uuid.uuid4().hex, owner_id=self.admin.id, is_public=True)
            db.add(self.kb)
            db.commit()
        self.store = vector_store.get_vector_store(self.kb.collection_name)

    def add_doc(self, count=1):
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb.id, filename="test.txt", file_ext=".txt", file_size=8,
                           stored_path=str(upload.upload_root() / f"unused-{uuid.uuid4()}.txt"), parse_done=True, chunk_count=count)
            db.add(doc)
            db.flush()
            for i in range(count):
                db.add(DocumentChunk(document_id=doc.id, chunk_index=i, content=f"content {i}"))
            db.commit()
            return doc.id

    def retrieve_ids(self):
        return {doc.metadata["document_id"] for doc, _ in rag.retrieve(self.kb.collection_name, "q", 6)}

    def test_partial_failure_is_not_searchable(self):
        did = self.add_doc(2)
        self.embeddings.fail_on = 2
        with self.assertLogs("app.services.ingest", level="ERROR"):
            ingest.ingest_document_task(str(did))
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, did).status, "failed")
            self.assertEqual(db.scalar(select(func.count()).select_from(self.store.EmbeddingStore)), 1)
        self.assertNotIn(str(did), self.retrieve_ids())

    def test_deleted_during_embedding_cannot_write_vectors(self):
        did = self.add_doc()
        def remove():
            with SessionLocal() as db:
                knowledge.delete_document(self.kb.id, did, self.request, db, self.admin)
        self.embeddings.on_embed = remove
        ingest.ingest_document_task(str(did))
        with SessionLocal() as db:
            self.assertIsNone(db.get(Document, did))
            self.assertEqual(db.scalar(select(func.count()).select_from(self.store.EmbeddingStore)), 0)
        self.assertNotIn(str(did), self.retrieve_ids())

    def test_ready_publication_reindex_and_legacy_vectors(self):
        did = self.add_doc()
        ingest.ingest_document_task(str(did))
        self.assertIn(str(did), self.retrieve_ids())
        with SessionLocal() as db:
            rows = self.store.EmbeddingStore
            db.execute(update(rows).values(cmetadata=rows.cmetadata.op("-")("index_version")))
            db.commit()
        self.assertIn(str(did), self.retrieve_ids())  # 兼容升级前没有代次字段的就绪向量。
        with SessionLocal() as db:
            doc = db.get(Document, did)
            ingest.reset_document_for_reindex(db, doc)
            self.assertEqual(doc.index_version, 1)
        self.assertNotIn(str(did), self.retrieve_ids())
        # 即使新代次已就绪，旧代次向量也不能复活。
        with SessionLocal() as db:
            db.execute(update(Document).where(Document.id == did).values(status="ready"))
            db.commit()
        self.assertNotIn(str(did), self.retrieve_ids())

    def test_historical_citation_reads_ready_original_and_checks_legacy_prefix(self):
        did = self.add_doc()
        ingest.ingest_document_task(str(did))
        citation = {**rag.to_citations(rag.retrieve(self.kb.collection_name, "q", 1))[0], "source_index": 3}
        loaded = vector_store.load_ready_citations(self.kb.collection_name, [citation])
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0][0].page_content, "content 0")
        self.assertEqual(loaded[0][0].metadata["historical_source_index"], 3)
        self.assertIsNone(rag.to_citations(loaded)[0]["score"])
        legacy = {key: value for key, value in citation.items() if key != "index_version"}
        self.assertEqual(len(vector_store.load_ready_citations(self.kb.collection_name, [legacy])), 1)
        self.assertEqual(vector_store.load_ready_citations(self.kb.collection_name, [{**legacy, "snippet": "different"}]), [])
        with SessionLocal() as db:
            rows = self.store.EmbeddingStore
            db.execute(update(rows).values(cmetadata=rows.cmetadata.op("-")("index_version")))
            db.commit()
        self.assertEqual(len(vector_store.load_ready_citations(self.kb.collection_name, [legacy])), 1)

    def test_historical_citation_excludes_failed_cancelled_and_deleted_documents(self):
        did = self.add_doc()
        ingest.ingest_document_task(str(did))
        citation = rag.to_citations(rag.retrieve(self.kb.collection_name, "q", 1))[0]
        for status, cancelled in [("failed", False), ("processing", False), ("cancelled", True), ("ready", True)]:
            with self.subTest(status=status, cancelled=cancelled):
                with SessionLocal() as db:
                    db.execute(update(Document).where(Document.id == did).values(status=status, cancel_requested=cancelled))
                    db.commit()
                self.assertEqual(vector_store.load_ready_citations(self.kb.collection_name, [citation]), [])
        with SessionLocal() as db:
            # 故意留下向量孤儿，验证读取必须关联仍存在的业务文档。
            db.execute(delete(Document).where(Document.id == did))
            db.commit()
        self.assertEqual(vector_store.load_ready_citations(self.kb.collection_name, [citation]), [])

    def test_historical_citation_cannot_resolve_to_a_new_index_generation(self):
        did = self.add_doc()
        ingest.ingest_document_task(str(did))
        citation = rag.to_citations(rag.retrieve(self.kb.collection_name, "q", 1))[0]
        with SessionLocal() as db:
            doc = db.get(Document, did)
            ingest.reset_document_for_reindex(db, doc)
            # 模拟新一代解析完成，避免依赖 add_doc 的占位文件路径。
            db.add(DocumentChunk(document_id=did, chunk_index=0, content="new content 0"))
            doc.parse_done, doc.chunk_count = True, 1
            db.commit()
        ingest.ingest_document_task(str(did))
        self.assertEqual(vector_store.load_ready_citations(self.kb.collection_name, [citation]), [])
        current = rag.to_citations(rag.retrieve(self.kb.collection_name, "q", 1))[0]
        self.assertEqual(current["index_version"], 1)
        self.assertEqual(len(vector_store.load_ready_citations(self.kb.collection_name, [current])), 1)

    def test_historical_citation_is_scoped_to_both_collection_and_document_knowledge_base(self):
        from langchain_core.documents import Document as LCDocument
        did = self.add_doc()
        ingest.ingest_document_task(str(did))
        citation = rag.to_citations(rag.retrieve(self.kb.collection_name, "q", 1))[0]
        with SessionLocal() as db:
            other = KnowledgeBase(name="other", collection_name="other_" + uuid.uuid4().hex, owner_id=self.admin.id)
            db.add(other)
            db.commit()
        self.assertEqual(vector_store.load_ready_citations(other.collection_name, [citation]), [])
        other_store = vector_store.get_vector_store(other.collection_name)
        with SessionLocal() as db:
            # 即便向量被错误写入另一集合，也必须校验业务文档所属知识库。
            vector_store.persist_vectors(db, other_store, [LCDocument(page_content="misplaced", metadata={
                "document_id": str(did), "chunk_index": 0, "index_version": 0,
            })], [[1.0] + [0.0] * 1023], ["misplaced-" + str(did)])
            db.commit()
        self.assertEqual(vector_store.load_ready_citations(other.collection_name, [citation]), [])

    def test_cancel_last_batch_prevents_publication(self):
        did = self.add_doc()
        def cancel():
            with SessionLocal() as db:
                knowledge.cancel_document(self.kb.id, did, db, self.admin)
        self.embeddings.on_embed = cancel
        ingest.ingest_document_task(str(did))
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, did).status, "cancelled")
        self.assertNotIn(str(did), self.retrieve_ids())

    def test_old_worker_cannot_write_after_lease_changes(self):
        did = self.add_doc()
        replacement = uuid.uuid4()
        def replace():
            with SessionLocal() as db:
                db.execute(update(Document).where(Document.id == did).values(lease_id=replacement))
                db.commit()
        self.embeddings.on_embed = replace
        ingest.ingest_document_task(str(did))
        with SessionLocal() as db:
            self.assertEqual(db.get(Document, did).lease_id, replacement)
            self.assertEqual(db.scalar(select(func.count()).select_from(self.store.EmbeddingStore)), 0)

    def make_upload(self, data=b"abcdefgh", *, digest=None):
        with SessionLocal() as db:
            return upload.create_or_resume(db, self.kb.id, "policy.txt", len(data), ".txt", self.admin.id,
                                           digest or hashlib.sha256(data).hexdigest())

    def write_parts(self, session, data=b"abcdefgh"):
        for index in range(session.total_chunks):
            knowledge._write_chunk(self.kb.id, session.id, self.admin.id, index, data[index * 4:(index + 1) * 4])

    def test_content_identity_and_owner_isolation(self):
        first = self.make_upload()
        self.assertEqual(first.id, self.make_upload().id)
        self.assertNotEqual(first.id, self.make_upload(b"ABCDEFGH").id)
        with SessionLocal() as db:
            self.assertIsNone(upload.get_session(db, self.kb.id, first.id, user_id=uuid.uuid4()))
            a = upload.create_or_resume(db, self.kb.id, "legacy.txt", 8, ".txt", self.admin.id)
            b = upload.create_or_resume(db, self.kb.id, "legacy.txt", 8, ".txt", self.admin.id)
            self.assertNotEqual(a.id, b.id)

    def test_chunk_replay_conflict_and_hash_mismatch(self):
        session = self.make_upload(digest=hashlib.sha256(b"XXXXXXXX").hexdigest())
        self.write_parts(session)
        receipt = knowledge._write_chunk(self.kb.id, session.id, self.admin.id, 0, b"abcd")
        self.assertFalse(receipt.created)
        with self.assertRaises(HTTPException) as error:
            knowledge._write_chunk(self.kb.id, session.id, self.admin.id, 0, b"ZZZZ")
        self.assertEqual(error.exception.status_code, 409)
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            knowledge.complete_upload(self.kb.id, session.id, self.request, db, self.admin)
        self.assertEqual(error.exception.status_code, 409)
        with SessionLocal() as db:
            self.assertEqual(db.get(UploadSession, session.id).status, "uploading")
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 0)

    def test_concurrent_complete_is_idempotent_and_does_not_resurrect(self):
        session = self.make_upload()
        self.write_parts(session)
        def complete(_):
            with SessionLocal() as db:
                result = knowledge.complete_upload(self.kb.id, session.id, self.request, db, self.admin)
                return result.data.document.id
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(complete, range(2)))
        self.assertEqual(ids[0], ids[1])
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 1)
            knowledge.delete_document(self.kb.id, ids[0], self.request, db, self.admin)
        with self.assertRaises(HTTPException) as error:
            complete(0)
        self.assertEqual(error.exception.status_code, 410)

    def test_password_reset_and_logout_revoke_tokens(self):
        old = create_access_token(str(self.admin.id), "admin", token_version=0)
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=old)
        with SessionLocal() as db:
            users.change_password(ChangePassword(old_password="Original123", new_password="Changed123"), self.request, db.get(User, self.admin.id), db)
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            get_current_user(self.request, credentials, db)
        self.assertEqual(error.exception.status_code, 401)
        with SessionLocal() as db:
            result = auth.login(LoginRequest(username="admin_test", password="Changed123"), self.request, db)
            new_credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=result.data.access_token)
            user = get_current_user(self.request, new_credentials, db)
            auth.logout(self.request, user, db)
        with SessionLocal() as db, self.assertRaises(HTTPException):
            get_current_user(self.request, new_credentials, db)

    def test_conversation_timestamp_and_bounded_history(self):
        import asyncio
        from app.models import Message
        fixed = datetime(2020, 1, 1, tzinfo=timezone.utc)
        with SessionLocal() as db:
            conv = Conversation(user_id=self.admin.id, mode="chat", title="history", updated_at=fixed)
            db.add(conv)
            db.flush()
            for index in range(6):
                db.add(Message(conversation_id=conv.id, role="user" if index % 2 == 0 else "assistant",
                               content=str(index), created_at=fixed + timedelta(seconds=index)))
            db.commit()
            async def consume(response):
                return "".join([frame async for frame in response.body_iterator])
            with patch.object(settings, "HISTORY_MAX_TURNS", 1), patch.object(rag, "answer_stream", return_value=iter(["answer"])) as answer:
                response = chat.chat_stream(ChatRequest(mode="chat", question="follow-up", conversation_id=conv.id), self.request, db, self.admin)
                output = asyncio.run(consume(response))
                self.assertIn("event: done", output)
                self.assertEqual(len(answer.call_args.args[2]), 2)
            db.refresh(conv)
            self.assertGreater(conv.updated_at, fixed)

    def test_document_choice_persists_topic_and_verified_body_across_short_history_requests(self):
        import asyncio
        from types import SimpleNamespace
        from langchain_core.documents import Document as LCDocument

        definitions = [
            ("数据安全指南.txt", "数据安全指南：一级公开、二级内部、三级机密。"),
            ("星海科技内部管理手册（2026版）.txt", "第四章 信息安全：数据分为四级：公开级、内部级、机密级、绝密级。内部级仅限内部使用，机密级和绝密级按授权范围访问。"),
        ]
        pairs = []
        for filename, content in definitions:
            identifier = self.add_doc()
            with SessionLocal() as db:
                db.get(Document, identifier).filename = filename
                chunk = db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == identifier))
                chunk.content, chunk.source_metadata = content, {"section": "信息安全"}
                db.commit()
            ingest.ingest_document_task(str(identifier))
            pairs.append((LCDocument(page_content=content, metadata={"document_id": str(identifier),
                         "filename": filename, "chunk_index": 0, "index_version": 0}), 0.1))
        citations = rag.to_citations(pairs)
        with SessionLocal() as db:
            conv = Conversation(user_id=self.admin.id, kb_id=self.kb.id, mode="rag", title="文档选择追问")
            db.add(conv)
            db.flush()
            beginning = datetime.now(timezone.utc) - timedelta(minutes=1)
            for index, (role, content, sources) in enumerate([
                ("user", "数据安全分几级", None),
                ("assistant", "《数据安全指南》分三级[1]。\n《星海科技内部管理手册（2026版）》分四级[2]。", citations),
                ("user", "具体点呢", None),
                ("assistant", "您想了解《数据安全指南》还是《星海科技内部管理手册（2026版）》？", None),
            ]):
                db.add(Message(conversation_id=conv.id, role=role, content=content, citations=sources,
                               created_at=beginning + timedelta(seconds=index)))
            db.commit()
            conversation_id = conv.id

        async def consume(response):
            return "".join([frame async for frame in response.body_iterator])

        with patch.object(rag, "retrieve", return_value=[]) as search, patch.object(rag, "get_llm") as model:
            model.return_value.stream.return_value = [SimpleNamespace(content="按手册，数据安全分为四级：公开级、内部级、机密级、绝密级[1]。")]
            with SessionLocal() as db:
                response = chat.chat_stream(ChatRequest(question="星海科技内部管理手册", conversation_id=conversation_id),
                                            self.request, db, self.admin)
                self.assertIn("event: done", asyncio.run(consume(response)))
            with SessionLocal() as db:
                previous = db.scalar(select(ChatTurn).where(ChatTurn.conversation_id == conversation_id))
                self.assertEqual(previous.trace["status"], "document_followup")
                self.assertIn("数据安全分几级", previous.trace["followup_topic"])
            # 新请求只载入最近一轮，原问题已不在 Message 历史窗口内。
            with patch.object(settings, "HISTORY_MAX_TURNS", 1), SessionLocal() as db:
                response = chat.chat_stream(ChatRequest(question="再详细一点", conversation_id=conversation_id),
                                            self.request, db, self.admin)
                self.assertIn("event: done", asyncio.run(consume(response)))
            self.assertEqual(search.call_count, 2)
            for call in search.call_args_list:
                self.assertEqual([item["document_id"] for item in call.kwargs["document_scope"]],
                                 [str(pairs[1][0].metadata["document_id"])])
            prompt = model.return_value.stream.call_args.args[0][-1].content
            self.assertIn("数据安全分几级", prompt)
            self.assertIn(definitions[1][1], prompt)
            self.assertNotIn(definitions[0][1], prompt)
        with SessionLocal() as db:
            answers = db.scalars(select(Message).where(Message.conversation_id == conversation_id,
                                                       Message.role == "assistant", Message.turn_id.is_not(None))).all()
            self.assertEqual(len(answers), 2)
            for answer in answers:
                self.assertEqual([item["document_id"] for item in answer.citations], [str(pairs[1][0].metadata["document_id"])])
                self.assertEqual([item["source_index"] for item in answer.citations], [1])

    def test_worker_recovers_legacy_and_expired_leases(self):
        from worker import claim_one, recover_stale
        did = self.add_doc()
        with SessionLocal() as db:
            db.execute(update(Document).where(Document.id == did).values(status="processing", lease_id=None, heartbeat_at=datetime.now(timezone.utc)))
            db.commit()
            self.assertEqual(recover_stale(db), 1)
            claimed = claim_one(db)
            self.assertEqual(claimed[0], did)
            self.assertIsNotNone(claimed[1])
            self.assertEqual(recover_stale(db), 0)
            db.execute(update(Document).where(Document.id == did).values(heartbeat_at=datetime.now(timezone.utc) - timedelta(seconds=settings.INGEST_STALE_SECONDS + 1)))
            db.commit()
            self.assertEqual(recover_stale(db), 1)
            self.assertIsNone(db.get(Document, did).lease_id)

    def test_api_upload_and_chat_contract(self):
        from fastapi.testclient import TestClient
        from app.core.limits import chat_admission
        with patch("app.core.logging.setup_logging"):
            from app import main
        client = TestClient(main.app)
        response = client.post("/api/v1/auth/login", json={"username": "admin_test", "password": "Original123"})
        self.assertEqual(response.status_code, 200)
        headers = {"Authorization": "Bearer " + response.json()["data"]["access_token"]}
        base = f"/api/v1/knowledge-bases/{self.kb.id}"
        response = client.post(base + "/upload-sessions", headers=headers, json={
            "filename": "api.txt", "file_size": 8, "content_sha256": hashlib.sha256(b"abcdefgh").hexdigest(),
        })
        self.assertEqual(response.status_code, 200, response.text)
        sid = response.json()["data"]["upload_id"]
        for index, data in enumerate((b"abcd", b"efgh")):
            result = client.post(f"{base}/upload-sessions/{sid}/chunks/{index}", headers=headers, content=data)
            self.assertEqual(result.status_code, 200, result.text)
        result = client.post(f"{base}/upload-sessions/{sid}/complete", headers=headers)
        self.assertEqual(result.status_code, 200, result.text)
        did = result.json()["data"]["document"]["id"]
        self.assertEqual(client.get(f"{base}/documents/{did}", headers=headers).status_code, 200)
        with patch.object(rag, "answer_stream", return_value=iter(["answer"])):
            result = client.post("/api/v1/chat/stream", headers=headers, json={"question": "hello", "mode": "chat"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("event: done", result.text)
        self.assertEqual(chat_admission._total, 0)

    def test_rag_final_citations_match_saved_and_reloaded_messages(self):
        import asyncio
        from langchain_core.documents import Document as LCDocument
        from app.models import Message
        docs = [(LCDocument(page_content=f"条文 {index}", metadata={
            "document_id": str(self.add_doc()), "filename": f"law-{index}.txt", "chunk_index": 0,
        }), 0.1) for index in range(1, 4)]
        async def consume(response):
            return [frame async for frame in response.body_iterator]
        for answer, expected in [("根据现有知识库无法回答。", []), ("法律规定见[3]。", [3])]:
            with self.subTest(answer=answer), SessionLocal() as db:
                with patch.object(rag, "retrieve", return_value=docs), patch.object(rag, "answer_stream", return_value=iter([answer])):
                    response = chat.chat_stream(ChatRequest(question="测试引用", kb_id=self.kb.id), self.request, db, self.admin)
                    frames = asyncio.run(consume(response))
                meta = json.loads(frames[0].split("data: ", 1)[1])
                done = json.loads(frames[-1].split("data: ", 1)[1])
                self.assertEqual(meta["citations"], [])
                self.assertEqual([item["source_index"] for item in done["citations"]], expected)
                conv_id = uuid.UUID(meta["conversation_id"])
                stored = db.scalar(select(Message).where(Message.conversation_id == conv_id, Message.role == "assistant"))
                self.assertEqual(stored.citations, done["citations"] or None)
                history = chat.get_conversation(conv_id, db, self.admin).data
                assistant = next(message for message in history.messages if message.role == "assistant")
                self.assertEqual([item.source_index for item in assistant.citations or []], expected)

    def test_admin_reset_and_legacy_login_upgrade(self):
        import bcrypt
        from app.core.security import password_needs_upgrade
        with SessionLocal() as db:
            db.execute(update(User).where(User.id == self.admin.id).values(
                password_hash=bcrypt.hashpw(b"Original123", bcrypt.gensalt()).decode()))
            db.commit()
            auth.login(LoginRequest(username="admin_test", password="Original123"), self.request, db)
            admin = db.get(User, self.admin.id)
            self.assertFalse(password_needs_upgrade(admin.password_hash))
            users.update_user(admin.id, UserUpdate(role="admin", is_active=True), self.request, db, admin)
            self.assertEqual(admin.token_version, 0)
            victim = User(username="victim", role="user", password_hash=hash_password("Original123"))
            db.add(victim)
            db.commit()
            token = create_access_token(str(victim.id), "user")
            users.update_user(victim.id, UserUpdate(password="Reset12345"), self.request, db, admin)
            self.assertEqual(victim.token_version, 1)
            with self.assertRaises(HTTPException):
                get_current_user(self.request, HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db)

    def test_preexisting_orphan_vectors_are_filtered(self):
        from langchain_core.documents import Document as LCDocument
        identifier = str(uuid.uuid4())
        with SessionLocal() as db:
            vector_store.persist_vectors(db, self.store, [LCDocument(page_content="orphan", metadata={"document_id": identifier})],
                                         [[1.0] + [0.0] * 1023], [identifier])
            db.commit()
        self.assertNotIn(identifier, self.retrieve_ids())

    def test_private_knowledge_and_admin_routes_are_protected(self):
        from fastapi.testclient import TestClient
        with SessionLocal() as db:
            db.execute(update(KnowledgeBase).where(KnowledgeBase.id == self.kb.id).values(is_public=False))
            viewer = User(username="viewer", role="user", password_hash=hash_password("Viewer123"))
            db.add(viewer)
            db.commit()
            token = create_access_token(str(viewer.id), "user")
        with patch("app.core.logging.setup_logging"):
            from app import main
        client = TestClient(main.app)
        headers = {"Authorization": f"Bearer {token}"}
        result = client.get("/api/v1/knowledge-bases", headers=headers)
        self.assertEqual(result.json()["data"], [])
        with patch("app.core.request_log.logger.warning"), patch.object(main.logger, "warning"):
            self.assertEqual(client.get(f"/api/v1/knowledge-bases/{self.kb.id}/documents", headers=headers).status_code, 404)
            self.assertEqual(client.get("/api/v1/users", headers=headers).status_code, 403)

    def _claim_chat(self, db, *, conversation_id=None, request_id=None, question="question"):
        return chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=conversation_id,
                                     kb_id=None, mode="chat", question=question, top_k=6,
                                     request_id=request_id or uuid.uuid4())

    def test_parallel_duplicate_request_creates_one_conversation_and_one_question(self):
        key, barrier = uuid.uuid4(), Barrier(2)
        def submit(_):
            with SessionLocal() as db:
                barrier.wait(timeout=10)
                try:
                    return self._claim_chat(db, request_id=key)
                except HTTPException as exc:
                    return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(sum(isinstance(result, chat_turns.Reservation) for result in results), 1)
        self.assertIn(409, results)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Conversation)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(ChatTurn)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 1)

    def test_same_conversation_is_serialized_across_database_connections(self):
        with SessionLocal() as db:
            conv = Conversation(user_id=self.admin.id, mode="chat")
            db.add(conv)
            db.commit()
        barrier = Barrier(2)
        def submit(_):
            with SessionLocal() as db:
                barrier.wait(timeout=10)
                try:
                    return self._claim_chat(db, conversation_id=conv.id)
                except HTTPException as exc:
                    return exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        active = next(result for result in results if isinstance(result, chat_turns.Reservation))
        self.assertIn(409, results)
        with SessionLocal() as db:
            self.assertTrue(chat_turns.cancel_request(db, active.request_id, self.admin.id))
            replacement = self._claim_chat(db, conversation_id=conv.id)
        with SessionLocal() as db, self.assertRaises(chat_turns.ChatLeaseLost):
            chat_turns.complete_turn(db, active, "stale answer", [], 1, {})
        with SessionLocal() as db:
            chat_turns.complete_turn(db, replacement, "new answer", [], 1, {})
            answers = db.scalars(select(Message).where(Message.role == "assistant")).all()
            self.assertEqual(len(answers), 1)
            self.assertEqual(answers[0].reply_to_id, replacement.user_message_id)

    def test_completed_request_replay_does_not_create_a_second_turn(self):
        key = uuid.uuid4()
        with SessionLocal() as db:
            first = self._claim_chat(db, request_id=key)
            chat_turns.complete_turn(db, first, "saved answer", [], 12, {"status": "original"})
        with SessionLocal() as db:
            replay = self._claim_chat(db, request_id=key)
            self.assertEqual(replay.replay_answer, "saved answer")
            self.assertEqual(replay.conversation_id, first.conversation_id)
            self.assertEqual(db.scalar(select(func.count()).select_from(ChatTurn)), 1)
            self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 2)
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            self._claim_chat(db, request_id=key, question="changed question")
        self.assertEqual(error.exception.status_code, 409)

    def test_api_replay_invokes_model_once_and_deleted_conversation_cannot_reappear(self):
        from fastapi.testclient import TestClient
        with patch("app.core.logging.setup_logging"):
            from app import main
        client = TestClient(main.app)
        headers = {"Authorization": "Bearer " + create_access_token(str(self.admin.id), "admin")}
        key = uuid.uuid4()
        payload = {"request_id": str(key), "question": "hello once", "mode": "chat"}
        with patch.object(rag, "answer_stream", side_effect=lambda *args, **kwargs: iter(["saved", " answer"])) as model:
            original = client.post("/api/v1/chat/stream", headers=headers, json=payload)
            replay = client.post("/api/v1/chat/stream", headers=headers, json=payload)
            self.assertEqual(original.status_code, 200, original.text)
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(model.call_count, 1)
            def events(response):
                return {frame.splitlines()[0][7:]: json.loads(frame.split("data: ", 1)[1])
                        for frame in response.text.strip().split("\n\n")}
            first_events, replay_events = events(original), events(replay)
            conv_id = first_events["meta"]["conversation_id"]
            self.assertEqual(replay_events["meta"]["conversation_id"], conv_id)
            self.assertTrue(replay_events["done"]["replayed"])
            self.assertEqual(first_events["done"]["message_id"], replay_events["done"]["message_id"])
            with SessionLocal() as db:
                self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 2)
            self.assertEqual(client.delete(f"/api/v1/chat/conversations/{conv_id}", headers=headers).status_code, 200)
            with patch.object(main.logger, "warning"), patch("app.core.request_log.logger.warning"):
                deleted = client.post("/api/v1/chat/stream", headers=headers, json=payload)
            self.assertEqual(deleted.status_code, 410, deleted.text)
            self.assertEqual(model.call_count, 1)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Conversation)), 0)
            tombstone = db.scalar(select(ChatTurn).where(ChatTurn.request_id == key))
            self.assertIsNotNone(tombstone)
            self.assertIsNone(tombstone.conversation_id)
            self.assertIsNone(tombstone.trace)

    def test_deleted_active_conversation_fences_worker_and_retains_deduplication(self):
        with SessionLocal() as db:
            active = self._claim_chat(db)
            chat.delete_conversation(active.conversation_id, db, self.admin)
        with SessionLocal() as db:
            self.assertEqual(db.get(ChatTurn, active.turn_id).status, "cancelled")
        with SessionLocal() as db, self.assertRaises(chat_turns.ChatLeaseLost):
            chat_turns.complete_turn(db, active, "late answer", [], 1, {})
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            self._claim_chat(db, request_id=active.request_id)
        self.assertEqual(error.exception.status_code, 410)

    def test_knowledge_base_deletion_clears_request_diagnostics_and_fences_active_answers(self):
        with SessionLocal() as db:
            kwargs = dict(user_id=self.admin.id, conversation_id=None, kb_id=self.kb.id,
                          mode="rag", question="private question", top_k=6)
            finished = chat_turns.claim_turn(db, request_id=uuid.uuid4(), **kwargs)
            chat_turns.complete_turn(db, finished, "private answer", [], 1, {"retrieval_query": "private query"})
            active = chat_turns.claim_turn(db, request_id=uuid.uuid4(), **kwargs)
            knowledge.delete_kb(self.kb.id, self.request, db, self.admin)
        with SessionLocal() as db:
            for turn_id in [finished.turn_id, active.turn_id]:
                turn = db.get(ChatTurn, turn_id)
                self.assertIsNone(turn.conversation_id)
                self.assertIsNone(turn.trace)
            self.assertEqual(db.get(ChatTurn, active.turn_id).status, "cancelled")
            self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 0)
            with self.assertRaises(chat_turns.ChatLeaseLost):
                chat_turns.complete_turn(db, active, "late private answer", [], 1, {})

    def test_startup_reclaims_turns_whose_lease_expired_unattended(self):
        """进程在生成中途退出时，没有后续请求触发 _expire_active，只能靠启动回收。"""
        with SessionLocal() as db:
            expired = self._claim_chat(db)
            conversation_id = expired.conversation_id
            live = self._claim_chat(db)
        # 会话 A 的租约早已过期，且不会再有人访问它；会话 B 仍在租约内。
        with engine.begin() as conn:
            conn.execute(text("UPDATE conversations SET lease_expires_at = :past WHERE id = :id"),
                         {"past": datetime.now(timezone.utc) - timedelta(hours=1), "id": conversation_id})
        with SessionLocal() as db:
            self.assertEqual(chat_turns.reclaim_expired(db), 1)
        with SessionLocal() as db:
            turn, message, conv = db.get(ChatTurn, expired.turn_id), db.get(Message, expired.user_message_id), db.get(Conversation, conversation_id)
            self.assertEqual(turn.status, "failed")
            self.assertEqual(message.status, "failed")
            self.assertIsNone(conv.active_turn_id)
            self.assertIsNone(conv.lease_expires_at)
            # 未过期的会话必须原样保留，否则会打断正在生成的回答。
            self.assertEqual(db.get(ChatTurn, live.turn_id).status, "processing")
            self.assertEqual(db.get(Conversation, live.conversation_id).active_turn_id, live.turn_id)
        with SessionLocal() as db:
            # 幂等：再跑一次不应重复处理，也不该把已完成/失败的失败轮次算进去。
            self.assertEqual(chat_turns.reclaim_expired(db), 0)

    def test_model_failure_releases_lease_and_is_excluded_from_next_history(self):
        import asyncio
        async def consume(response):
            return "".join([frame async for frame in response.body_iterator])
        def broken(*_args, **_kwargs):
            yield "partial answer"
            raise RuntimeError("simulated provider failure")
        with SessionLocal() as db:
            with patch.object(rag, "answer_stream", side_effect=broken), patch.object(chat.logger, "exception"):
                output = asyncio.run(consume(chat.chat_stream(ChatRequest(mode="chat", question="failed question"),
                                                              self.request, db, self.admin)))
            self.assertIn("event: error", output)
            self.assertNotIn("event: done", output)
            conv = db.scalar(select(Conversation))
            self.assertIsNone(conv.active_turn_id)
            failed = db.scalar(select(Message))
            self.assertEqual(failed.status, "failed")
            with patch.object(rag, "answer_stream", return_value=iter(["successful answer"])) as model:
                result = asyncio.run(consume(chat.chat_stream(ChatRequest(mode="chat", question="next question",
                                                                          conversation_id=conv.id),
                                                              self.request, db, self.admin)))
            self.assertIn("event: done", result)
            self.assertEqual(model.call_args.args[2], [])
            self.assertEqual(db.scalar(select(func.count()).select_from(Message).where(Message.role == "assistant")), 1)

    def test_diagnostics_are_admin_only_before_a_turn_is_claimed(self):
        from fastapi.testclient import TestClient
        with SessionLocal() as db:
            viewer = User(username="trace_viewer", role="user", password_hash=hash_password("Original123"))
            db.add(viewer)
            db.commit()
        with patch("app.core.logging.setup_logging"):
            from app import main
        client = TestClient(main.app)
        headers = {"Authorization": "Bearer " + create_access_token(str(viewer.id), "user")}
        with patch.object(main.logger, "warning"), patch("app.core.request_log.logger.warning"), \
             patch.object(rag, "answer_stream") as model:
            result = client.post("/api/v1/chat/stream", headers=headers,
                                 json={"mode": "chat", "question": "hello", "include_trace": True})
        self.assertEqual(result.status_code, 403, result.text)
        model.assert_not_called()
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ChatTurn)), 0)

    def test_request_identifiers_and_cancellation_are_scoped_to_each_user(self):
        key = uuid.uuid4()
        with SessionLocal() as db:
            other = User(username="second_chat_user", role="user", password_hash=hash_password("Original123"))
            db.add(other)
            db.commit()
            first = self._claim_chat(db, request_id=key)
            with self.assertRaises(HTTPException) as error:
                chat_turns.cancel_request(db, key, other.id)
            self.assertEqual(error.exception.status_code, 404)
            second = chat_turns.claim_turn(db, user_id=other.id, conversation_id=None, kb_id=None,
                                          mode="chat", question="other question", top_k=6, request_id=key)
            self.assertNotEqual(first.conversation_id, second.conversation_id)
            self.assertTrue(chat_turns.cancel_request(db, key, other.id))
            chat_turns.complete_turn(db, first, "first answer", [], 1, {})
            self.assertEqual(db.get(ChatTurn, second.turn_id).status, "cancelled")
            self.assertEqual(db.get(ChatTurn, first.turn_id).status, "completed")

    def test_expired_worker_is_fenced_and_failed_question_is_not_a_history_turn(self):
        with SessionLocal() as db:
            first = self._claim_chat(db)
            db.execute(update(Conversation).where(Conversation.id == first.conversation_id).values(
                lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            db.commit()
            second = self._claim_chat(db, conversation_id=first.conversation_id)
        with SessionLocal() as db, self.assertRaises(chat_turns.ChatLeaseLost):
            chat_turns.complete_turn(db, first, "wrong", [], 1, {})
        with SessionLocal() as db:
            chat_turns.complete_turn(db, second, "right", [], 1, {})
            self.assertEqual(db.get(ChatTurn, first.turn_id).status, "failed")
            self.assertEqual(db.get(Message, first.user_message_id).status, "failed")
            answer = db.scalar(select(Message).where(Message.role == "assistant"))
            self.assertEqual(answer.reply_to_id, second.user_message_id)

    def test_keyword_search_is_scoped_ready_and_handles_exact_codes_and_chinese_articles(self):
        from app.services.hybrid_search import build_search_text
        from app.services.retrieval_candidates import keyword_candidates
        did = self.add_doc()
        ingest.ingest_document_task(str(did))
        with SessionLocal() as db:
            chunk = db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == did))
            chunk.content = "第一百四十三条 ERR_AUTH_401 登录凭据失效"
            chunk.search_text = build_search_text(chunk.content, {"filename": "policy.txt"})
            db.commit()
        for query in ["ERR_AUTH_401", "第143条"]:
            with self.subTest(query=query):
                found = keyword_candidates(self.kb.collection_name, query)
                self.assertEqual([doc.metadata["document_id"] for doc, _ in found], [str(did)])
                self.assertIsNone(rag.to_citations(found)[0]["score"])
        self.assertEqual(keyword_candidates("other_collection", "ERR_AUTH_401"), [])
        with SessionLocal() as db:
            db.execute(update(Document).where(Document.id == did).values(cancel_requested=True))
            db.commit()
        self.assertEqual(keyword_candidates(self.kb.collection_name, "ERR_AUTH_401"), [])

    def test_source_preview_generation_and_metadata_survive_real_ingest(self):
        from app.services.source_documents import read_source
        path = Path(self.temp.name) / "structured.md"
        path.write_text("# 第一章\\n第一条 请按流程审批。\\n第二条 保留凭据。".replace("\\n", "\n"), encoding="utf-8")
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb.id, filename="structured.md", file_ext=".md", stored_path=str(path), file_size=path.stat().st_size)
            db.add(doc)
            db.commit()
        ingest.ingest_document_task(str(doc.id))
        with SessionLocal() as db:
            doc = db.get(Document, doc.id)
            self.assertEqual(doc.status, "ready")
            preview = read_source(db, self.kb.id, doc.id, 0, 0)
            self.assertTrue(preview["chunks"][0]["content"])
            self.assertTrue(preview["location"])
            ingest.reset_document_for_reindex(db, doc)
            with self.assertRaises(HTTPException) as error:
                read_source(db, self.kb.id, doc.id, 0, 0)
            self.assertEqual(error.exception.status_code, 409)

    def test_schema_migration_is_idempotent_and_preserves_legacy_history(self):
        with SessionLocal() as db:
            legacy = Conversation(user_id=self.admin.id, kb_id=self.kb.id, title="legacy history")
            db.add(legacy)
            db.flush()
            db.add_all([Message(conversation_id=legacy.id, role="user", content="old question"),
                        Message(conversation_id=legacy.id, role="assistant", content="old answer")])
            db.commit()
            legacy_id = legacy.id
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users DROP COLUMN token_version"))
            conn.execute(text("ALTER TABLE documents DROP COLUMN index_version, DROP COLUMN lease_id"))
            conn.execute(text("ALTER TABLE upload_sessions DROP COLUMN content_sha256"))
            conn.execute(text("ALTER TABLE conversations DROP COLUMN mode, DROP COLUMN active_turn_id, DROP COLUMN lease_expires_at, DROP COLUMN document_scope, DROP COLUMN scope_revision"))
            conn.execute(text("ALTER TABLE messages DROP COLUMN turn_id, DROP COLUMN reply_to_id, DROP COLUMN status"))
            conn.execute(text("ALTER TABLE document_chunks DROP COLUMN source_metadata, DROP COLUMN search_text"))
            conn.execute(text("DROP TABLE chat_turns"))
        init_db(create_admin=False)
        init_db(create_admin=False)
        with SessionLocal() as db:
            self.assertEqual(db.get(User, self.admin.id).token_version, 0)
            migrated = db.get(Conversation, legacy_id)
            self.assertEqual(migrated.mode, "rag")
            self.assertEqual(migrated.document_scope, [])
            self.assertEqual(migrated.scope_revision, 0)
            self.assertEqual({row.content for row in migrated.messages}, {"old question", "old answer"})
            self.assertTrue(all(row.status == "completed" and row.turn_id is None for row in migrated.messages))
            self.assertEqual(db.scalar(select(func.count()).select_from(ChatTurn)), 0)

    def _ready_scope_document(self, filename, contents):
        identifier = self.add_doc(len(contents))
        with SessionLocal() as db:
            db.get(Document, identifier).filename = filename
            chunks = db.scalars(select(DocumentChunk).where(DocumentChunk.document_id == identifier)
                                 .order_by(DocumentChunk.chunk_index)).all()
            for chunk, content in zip(chunks, contents):
                chunk.content = content
                chunk.source_metadata = {"section": f"正文章节{chunk.chunk_index}",
                                         "section_key": f"body-{chunk.chunk_index}"}
            db.commit()
        ingest.ingest_document_task(str(identifier))
        return identifier

    def test_scope_filters_both_channels_before_top_k_even_with_better_other_hits(self):
        from app.services.retrieval_candidates import keyword_candidates
        selected = self._ready_scope_document("选定手册.txt", ["内部级数据外发须审批并脱敏。"])
        other = self._ready_scope_document("其他指南.txt", ["内部级数据外发可以直接公开。" * 10])
        with SessionLocal() as db:
            rows = self.store.EmbeddingStore
            db.execute(update(rows).where(rows.cmetadata["document_id"].astext == str(selected))
                       .values(embedding=[0.8, 0.6] + [0.0] * 1022))
            db.commit()
        scope = [{"document_id": str(selected), "filename": "选定手册.txt", "index_version": 0}]
        vector = [1.0] + [0.0] * 1023
        with patch.object(settings, "RETRIEVAL_MIN_SIMILARITY", 0):
            global_hits = vector_store.search_ready_vectors(self.store, vector, 1)
            self.assertEqual(global_hits[0][0].metadata["document_id"], str(other))
            scoped = vector_store.search_ready_vectors(self.store, vector, 1, document_scope=scope)
            self.assertEqual([doc.metadata["document_id"] for doc, _ in scoped], [str(selected)])
            self.assertEqual(vector_store.search_ready_vectors(self.store, vector, 1, document_scope=[]), [])
        keyword = keyword_candidates(self.kb.collection_name, "内部级数据外发", 1, document_scope=scope)
        self.assertEqual([doc.metadata["document_id"] for doc, _ in keyword], [str(selected)])
        self.assertEqual(keyword_candidates(self.kb.collection_name, "内部级数据外发", 1, document_scope=[]), [])
        stale = [{**scope[0], "index_version": 1}]
        self.assertEqual(vector_store.search_ready_vectors(self.store, vector, 1, document_scope=stale), [])
        self.assertEqual(keyword_candidates(self.kb.collection_name, "内部级数据外发", 1, document_scope=stale), [])

    def test_draft_scope_and_uncited_answer_survive_reloading_and_later_claims(self):
        identifier = self._ready_scope_document("限定手册.txt", ["仅限内部使用。"])
        with SessionLocal() as db:
            first = chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=None, kb_id=self.kb.id,
                                         mode="rag", question="未规定事项", top_k=6, document_ids=[identifier])
            self.assertEqual(first.scope_revision, 1)
            chat_turns.complete_turn(db, first, "根据现有知识库无法回答。", [], 1, {})
        with SessionLocal() as db:
            detail = chat.get_conversation(first.conversation_id, db, self.admin).data
            self.assertEqual(detail.document_scope[0].document_id, str(identifier))
            self.assertEqual(detail.scope_revision, 1)
            next_turn = chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=first.conversation_id,
                                             kb_id=self.kb.id, mode="rag", question="内部级能否外发", top_k=6,
                                             scope_revision=1)
            self.assertEqual(next_turn.document_scope, first.document_scope)
            with self.assertRaises(HTTPException) as error:
                chat_turns.set_document_scope(db, first.conversation_id, [], 1)
            self.assertEqual(error.exception.status_code, 409)
            db.rollback()
            chat_turns.cancel_request(db, next_turn.request_id, self.admin.id)
            self.assertEqual(chat_turns.set_document_scope(db, first.conversation_id, [], 1), ([], 2))
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=first.conversation_id,
                                 kb_id=self.kb.id, mode="rag", question="旧页面提问", top_k=6, scope_revision=1)
        self.assertEqual(error.exception.status_code, 409)

    def test_replay_reports_current_scope_without_reverting_a_later_switch(self):
        import asyncio
        first_doc = self._ready_scope_document("甲手册.txt", ["甲规定"])
        second_doc = self._ready_scope_document("乙手册.txt", ["乙规定"])
        key = uuid.uuid4()
        with SessionLocal() as db:
            turn = chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=None, kb_id=self.kb.id,
                                        mode="rag", question="原问题", top_k=6, request_id=key, document_ids=[first_doc])
            chat_turns.complete_turn(db, turn, "原结果", [], 1, {})
            selected, revision = chat_turns.set_document_scope(db, turn.conversation_id, [second_doc], 1)
        async def consume(response):
            return [json.loads(frame.split("data: ", 1)[1]) async for frame in response.body_iterator]
        with SessionLocal() as db, patch.object(rag, "retrieve") as search:
            events = asyncio.run(consume(chat.chat_stream(ChatRequest(kb_id=self.kb.id, question="原问题", request_id=key,
                                                                     document_ids=[first_doc]), self.request, db, self.admin)))
            self.assertEqual(events[-1]["document_scope"], selected)
            self.assertEqual(events[-1]["scope_revision"], revision)
            self.assertTrue(events[-1]["replayed"])
            self.assertEqual(db.scalar(select(func.count()).select_from(ChatTurn)), 1)
            self.assertEqual(db.get(Conversation, turn.conversation_id).document_scope, selected)
        search.assert_not_called()

    def test_invalidated_scope_rejects_next_question_and_can_be_cleared(self):
        identifier = self._ready_scope_document("可失效手册.txt", ["仅限内部。"])
        with SessionLocal() as db:
            first = chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=None, kb_id=self.kb.id,
                                         mode="rag", question="q", top_k=6, document_ids=[identifier])
            chat_turns.complete_turn(db, first, "answer", [], 1, {})
        for changes in [dict(status="processing"), dict(cancel_requested=True), dict(index_version=1)]:
            with SessionLocal() as db:
                db.execute(update(Document).where(Document.id == identifier).values(**changes))
                db.commit()
            with self.subTest(changes=changes), SessionLocal() as db, self.assertRaises(HTTPException):
                chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=first.conversation_id,
                                     kb_id=self.kb.id, mode="rag", question="不得扩大范围", top_k=6)
            with SessionLocal() as db:
                self.assertEqual(db.scalar(select(func.count()).select_from(Message)), 2)
                db.execute(update(Document).where(Document.id == identifier)
                           .values(status="ready", cancel_requested=False, index_version=0))
                db.commit()
        with SessionLocal() as db:
            db.execute(delete(Document).where(Document.id == identifier))
            db.commit()
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=first.conversation_id,
                                 kb_id=self.kb.id, mode="rag", question="deleted", top_k=6)
        self.assertEqual(error.exception.status_code, 404)
        with SessionLocal() as db:
            self.assertEqual(chat_turns.set_document_scope(db, first.conversation_id, [], 1), ([], 2))

    def test_source_reindexed_during_generation_prevents_atomic_answer_and_scope_commit(self):
        identifier = self._ready_scope_document("代次隔离.txt", ["旧正文"])
        with SessionLocal() as db:
            turn = chat_turns.claim_turn(db, user_id=self.admin.id, conversation_id=None, kb_id=self.kb.id,
                                        mode="rag", question="q", top_k=6, document_ids=[identifier])
        with SessionLocal() as db:
            db.execute(update(Document).where(Document.id == identifier).values(index_version=1))
            db.commit()
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            chat_turns.complete_turn(db, turn, "旧代次回答", [], 1, {})
        self.assertEqual(error.exception.status_code, 409)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Message).where(Message.role == "assistant")), 0)
            self.assertEqual(db.get(ChatTurn, turn.turn_id).status, "processing")
            chat_turns.cancel_request(db, turn.request_id, self.admin.id)

    def test_cross_connection_scope_updates_require_the_current_revision(self):
        from app.services.document_scopes import resolve_scope
        identifier = self._ready_scope_document("甲.txt", ["甲"])
        other = self._ready_scope_document("乙.txt", ["乙"])
        with SessionLocal() as db:
            conv = Conversation(user_id=self.admin.id, kb_id=self.kb.id, mode="rag", scope_revision=1,
                                document_scope=resolve_scope(db, self.kb.id, [identifier]))
            db.add(conv)
            db.commit()
            conv_id = conv.id
        barrier = Barrier(2)
        def change(identifiers):
            with SessionLocal() as db:
                barrier.wait(timeout=10)
                try:
                    return "ok", chat_turns.set_document_scope(db, conv_id, identifiers, 1)[1]
                except HTTPException as exc:
                    return "error", exc.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, [[], [other]]))
        self.assertCountEqual(results, [("ok", 2), ("error", 409)])
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv_id).scope_revision, 2)

    def test_scope_patch_rejects_foreign_documents_and_revoked_private_kb_access(self):
        from app.schemas.chat import DocumentScopeUpdate
        own = self._ready_scope_document("本库.txt", ["本库"])
        foreign = self._ready_scope_document("外库.txt", ["外库"])
        with SessionLocal() as db:
            other_kb = KnowledgeBase(name="scope-private", collection_name="scope_" + uuid.uuid4().hex,
                                     owner_id=self.admin.id, is_public=False)
            viewer = User(username="scope_viewer", role="user", password_hash=hash_password("Original123"))
            db.add_all([other_kb, viewer])
            db.flush()
            db.get(Document, foreign).kb_id = other_kb.id
            conv = Conversation(user_id=self.admin.id, kb_id=self.kb.id, mode="rag")
            revoked = Conversation(user_id=viewer.id, kb_id=other_kb.id, mode="rag")
            db.add_all([conv, revoked])
            db.commit()
            conv_id, revoked_id, viewer_id = conv.id, revoked.id, viewer.id
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            chat.set_document_scope(conv_id, DocumentScopeUpdate(document_ids=[foreign]), db, self.admin)
        self.assertEqual(error.exception.status_code, 404)
        with SessionLocal() as db, self.assertRaises(HTTPException) as error:
            chat.set_document_scope(revoked_id, DocumentScopeUpdate(document_ids=[foreign]), db, db.get(User, viewer_id))
        self.assertEqual(error.exception.status_code, 403)
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv_id).document_scope, [])
            self.assertEqual(chat.set_document_scope(conv_id, DocumentScopeUpdate(document_ids=[own], expected_revision=0),
                                                    db, self.admin).data.scope_revision, 1)

    def test_security_multiturn_supplements_uncited_body_within_selected_file_after_reload(self):
        import asyncio
        import re
        from types import SimpleNamespace
        guide = self._ready_scope_document("数据安全指南.txt", ["指南将数据分为三级：公开、内部、机密。"])
        manual = self._ready_scope_document("星海科技内部管理手册（2026版）.txt", [
            "手册将数据分为四级：公开级、内部级、机密级、绝密级。",
            "内部级数据对外发送前必须获得负责人审批，并先做脱敏处理。禁止未经批准直接外发。",
        ])
        async def consume(response):
            return [json.loads(frame.split("data: ", 1)[1]) async for frame in response.body_iterator]
        prompts = []
        def model(messages):
            current = messages[-1].content
            prompts.append(current)
            blocks = re.findall(r"\[([0-9]+)\] 来源：([^\n]+)\n(.*?)(?=\n\n---\n\n|\n\n【本轮追问|\n\n【用户问题】)", current, re.DOTALL)
            def number(fragment):
                return next(index for index, _title, text in blocks if fragment in text)
            question = current.split("【用户问题】\n", 1)[1]
            if question == "内部级能否外发？":
                self.assertNotIn("数据安全指南.txt", current)
                self.assertIn("审批", current)
                text = f"内部级外发必须审批并脱敏，禁止未经批准直接外发[{number('负责人审批')}]。"
            elif question == "未规定的太空数据规则？":
                text = "根据现有知识库无法回答该细节。"
            elif question == "星海科技内部管理手册":
                text = f"手册分四级：公开级、内部级、机密级、绝密级[{number('手册将数据')}]。"
            else:
                text = f"《数据安全指南》分三级[{number('指南将数据')}]。\n《星海科技内部管理手册（2026版）》分四级[{number('手册将数据')}]。"
            return [SimpleNamespace(content=text)]
        conversation_id, revision = None, None
        with patch.object(rag, "get_llm") as llm:
            llm.return_value.stream.side_effect = model
            for question in ["数据安全分几级", "具体点呢", "星海科技内部管理手册", "内部级能否外发？", "未规定的太空数据规则？"]:
                with self.subTest(question=question), SessionLocal() as db:
                    payload = ChatRequest(question=question, kb_id=self.kb.id, conversation_id=conversation_id,
                                          scope_revision=revision)
                    frames = asyncio.run(consume(chat.chat_stream(payload, self.request, db, self.admin)))
                    done = frames[-1]
                    self.assertIn("message_id", done)
                    conversation_id = uuid.UUID(frames[0]["conversation_id"])
                    revision = done["scope_revision"]
                    if question in {"数据安全分几级", "具体点呢"}:
                        self.assertEqual(done["document_scope"], [])
                        self.assertEqual({item["document_id"] for item in done["citations"]}, {str(guide), str(manual)})
                    else:
                        self.assertEqual([item["document_id"] for item in done["document_scope"]], [str(manual)])
                        self.assertEqual(revision, 1)
                    if question == "内部级能否外发？":
                        self.assertEqual([item["document_id"] for item in done["citations"]], [str(manual)])
                        self.assertEqual(done["citations"][0]["chunk_index"], 1)
                    if question == "未规定的太空数据规则？":
                        self.assertEqual(done["citations"], [])
                # 模拟刷新：每轮均用新连接读取服务端保存的范围。
                with SessionLocal() as db:
                    detail = chat.get_conversation(conversation_id, db, self.admin).data
                    self.assertEqual(detail.scope_revision, revision)
                    self.assertEqual([item.document_id for item in detail.document_scope],
                                     [item["document_id"] for item in done["document_scope"]])
        self.assertEqual(len(prompts), 5)
