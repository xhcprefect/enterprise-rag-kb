"""真实 PostgreSQL/pgvector 回归；只接受显式提供、数据库名以 _test 结尾的连接。"""
import hashlib
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

TEST_URL = os.environ.get("TEST_DATABASE_URL", "")
if TEST_URL:
    from sqlalchemy.engine import make_url
    if not (make_url(TEST_URL).database or "").endswith("_test"):
        raise RuntimeError("TEST_DATABASE_URL 必须指向以 _test 结尾的专用数据库")
    os.environ["DATABASE_URL"] = TEST_URL
    os.environ["JWT_SECRET"] = "regression-tests-only-not-for-production"

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from langchain_core.embeddings import Embeddings
from sqlalchemy import delete, func, select, text, update

from app.api.deps import get_current_user
from app.api.v1 import auth, chat, knowledge, users
from app.core.config import settings
from app.core.security import create_access_token, hash_password, verify_password
from app.db.init_db import init_db
from app.db.session import SessionLocal, engine
from app.models import Conversation, Document, DocumentChunk, KnowledgeBase, UploadSession, User
from app.schemas.auth import LoginRequest
from app.schemas.chat import ChatRequest
from app.schemas.user import ChangePassword, UserUpdate
from app.services import ingest, rag, upload, vector_store


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
                db.add(Message(conversation_id=conv.id, role="user" if index % 2 == 0 else "assistant", content=str(index)))
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

    def test_schema_migration_is_idempotent(self):
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE users DROP COLUMN token_version"))
            conn.execute(text("ALTER TABLE documents DROP COLUMN index_version, DROP COLUMN lease_id"))
            conn.execute(text("ALTER TABLE upload_sessions DROP COLUMN content_sha256"))
        init_db(create_admin=False)
        init_db(create_admin=False)
        with SessionLocal() as db:
            self.assertEqual(db.get(User, self.admin.id).token_version, 0)
