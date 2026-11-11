"""在线回归冻结文档清单（最多 256）进入现有内部检索链的真实 PostgreSQL 验证。

覆盖：内部 6/256 份清单在 vector/keyword 两路 SQL 排名与 LIMIT 前生效、范围外
更强候选不能挤掉指定范围结果、固定代次排除旧代次、257 明确拒绝不截断、显式空清单
不发起 embedding，以及 supplement/contains 认识内部大清单。系统消息/生成流程不在此
文件验证，也不改变普通 chat 的 5 份用户选择上限。
"""
import unittest
import uuid
from unittest.mock import patch

from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import Embeddings

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

if TEST_URL:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from app.core.config import settings
    from app.db.init_db import init_db
    from app.models import Document, DocumentChunk, KnowledgeBase, User
    from app.services import document_scopes, rag, retrieval_candidates, vector_store

    from app.db import session as db_session

    SessionLocal, engine = db_session.SessionLocal, db_session.engine

VECTOR_DIM = 1024


class _FakeEmbeddings(Embeddings):
    """模型网络替身：只记录查询调用，返回固定查询向量；不访问任何真实模型。"""

    def __init__(self, vector):
        self.vector = list(vector)
        self.query_calls = 0

    def embed_documents(self, texts):
        return [list(self.vector) for _ in texts]

    def embed_query(self, text):
        self.query_calls += 1
        return list(self.vector)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行在线回归清单检索测试")
class RegressionManifestRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        cls._cleanup()
        vector_store._store.cache_clear()
        engine.dispose()

    @classmethod
    def _cleanup(cls):
        # 任何 TRUNCATE 之前都先经共享 gate 核对真实 engine 与声明库；不一致硬失败。
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, documents, document_chunks, conversations, "
                "upload_sessions, audit_logs RESTART IDENTITY CASCADE"
            ))
            if conn.execute(text("SELECT to_regclass('langchain_pg_collection')")).scalar():
                conn.execute(text("TRUNCATE langchain_pg_collection CASCADE"))

    def setUp(self):
        self._cleanup()
        vector_store._store.cache_clear()
        for patcher in (
            patch.object(rag.settings, "HYBRID_RETRIEVAL_ENABLED", True),
            patch.object(rag.settings, "RERANK_ENABLED", False),
            patch.object(settings, "RETRIEVAL_MIN_SIMILARITY", 0),
            patch.object(settings, "RETRIEVAL_NEIGHBOR_CHUNKS", 0),
            patch.object(vector_store, "_vector_engine", return_value=engine),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with SessionLocal() as db:
            owner = User(username="manifest_owner", role="user", is_active=True, password_hash="x")
            db.add(owner)
            db.flush()
            kb = KnowledgeBase(name="manifest_kb_" + uuid.uuid4().hex,
                               collection_name="manifest_" + uuid.uuid4().hex,
                               owner_id=owner.id, is_public=True)
            db.add(kb)
            db.commit()
            self.kb_id = kb.id
            self.collection = kb.collection_name
        self.embeddings = _FakeEmbeddings([1.0] + [0.0] * (VECTOR_DIM - 1))
        self.query_vector = [1.0] + [0.0] * (VECTOR_DIM - 1)
        self.store = vector_store.get_vector_store(self.collection)
        self.store.embedding_function = self.embeddings

    def _add_documents(self, specs):
        """specs: (filename, content, version, vector)；同一事务写业务行与真实 pgvector 行。"""
        inserted = []
        with SessionLocal() as db:
            for filename, content, version, vector in specs:
                doc = Document(
                    kb_id=self.kb_id, filename=filename, file_ext=".txt", file_size=len(content),
                    stored_path=f"/tmp/{uuid.uuid4().hex}.txt", status="ready", parse_done=True,
                    chunk_count=1, index_version=version,
                )
                db.add(doc)
                db.flush()
                db.add(DocumentChunk(
                    document_id=doc.id, chunk_index=0, content=content,
                    source_metadata={"section": "正文"}, search_text=content, embedded=True,
                ))
                inserted.append((doc.id, filename, version, content, vector))
            db.commit()
        payload, vectors, identifiers = [], [], []
        for identifier, filename, version, content, vector in inserted:
            metadata = {"document_id": str(identifier), "kb_id": str(self.kb_id),
                        "filename": filename, "chunk_index": 0, "index_version": version}
            payload.append(LCDocument(page_content=content, metadata=metadata))
            vectors.append(vector)
            identifiers.append(f"{identifier}:{version}:0")
        with SessionLocal() as db:
            vector_store.persist_vectors(db, self.store, payload, vectors, identifiers)
            db.commit()
        return [(identifier, filename, version) for identifier, filename, version, _content, _vector in inserted]

    def _inside_vector(self):
        return [0.9, (1 - 0.81) ** 0.5] + [0.0] * (VECTOR_DIM - 2)

    def _strong_outside(self, filename="outside-strong.txt"):
        return self._add_documents([(filename, "manifestprobe " * 20, 0, self.query_vector)])[0][0]

    def test_six_item_manifest_filters_both_sql_legs_before_limit_and_honours_versions(self):
        selected = self._add_documents([
            (f"selected-{index}.txt", f"manifestprobe selected {index}", 0, self._inside_vector())
            for index in range(6)
        ])
        outside = self._strong_outside()
        # 第 6 份清单显式固定为旧代次：业务行/向量仍是 0，必须被 SQL 排除。
        scope = [{"document_id": str(identifier), "index_version": 0} for identifier, _name, _version in selected[:5]]
        scope.append({"document_id": str(selected[5][0]), "index_version": 1})

        global_vector = vector_store.search_ready_vectors(self.store, self.query_vector, 1)
        self.assertEqual(global_vector[0][0].metadata["document_id"], str(outside))
        global_keyword = retrieval_candidates.keyword_candidates(self.collection, "manifestprobe", 1)
        self.assertEqual(global_keyword[0][0].metadata["document_id"], str(outside))

        vector_hits = vector_store.search_ready_vectors(
            self.store, self.query_vector, 1, document_scope=scope)
        keyword_hits = retrieval_candidates.keyword_candidates(
            self.collection, "manifestprobe", 1, document_scope=scope)
        allowed = {str(identifier) for identifier, _name, _version in selected[:5]}
        self.assertTrue(vector_hits[0][0].metadata["document_id"] in allowed)
        self.assertTrue(keyword_hits[0][0].metadata["document_id"] in allowed)
        for hits in (vector_hits, keyword_hits):
            self.assertNotIn(str(outside), {doc.metadata["document_id"] for doc, _ in hits})
            self.assertNotIn(str(selected[5][0]), {doc.metadata["document_id"] for doc, _ in hits})

        with patch.object(rag, "get_vector_store", return_value=self.store):
            result = rag.retrieve(self.collection, "manifestprobe", 3, document_scope=scope)
        self.assertEqual(len(result), 3)
        self.assertTrue({doc.metadata["document_id"] for doc, _ in result} <= allowed)
        self.assertNotIn(str(outside), {doc.metadata["document_id"] for doc, _ in result})
        self.assertNotIn(str(selected[5][0]), {doc.metadata["document_id"] for doc, _ in result})

    def test_256_item_manifest_is_searchable_without_truncation_and_excludes_outside(self):
        specs = [(f"inside-{index:03d}.txt", f"manifestprobe inside {index}", 0, self._inside_vector())
                 for index in range(256)]
        selected = self._add_documents(specs)
        outside = self._strong_outside()
        scope = [{"document_id": str(identifier), "index_version": 0} for identifier, _name, _version in selected]
        self.assertEqual(len(scope), document_scopes.MAX_INTERNAL_SCOPE_DOCUMENTS)
        self.assertEqual(len(document_scopes.scope_pairs(
            tuple(scope), max_documents=document_scopes.MAX_INTERNAL_SCOPE_DOCUMENTS)), 256)
        allowed = {str(identifier) for identifier, _name, _version in selected}

        vector_hits = vector_store.search_ready_vectors(
            self.store, self.query_vector, 1, document_scope=scope)
        keyword_hits = retrieval_candidates.keyword_candidates(
            self.collection, "manifestprobe", 1, document_scope=scope)
        self.assertIn(vector_hits[0][0].metadata["document_id"], allowed)
        self.assertIn(keyword_hits[0][0].metadata["document_id"], allowed)
        self.assertNotEqual(vector_hits[0][0].metadata["document_id"], str(outside))

        with patch.object(rag, "get_vector_store", return_value=self.store):
            # 执行上下文的 upload_manifest 是 tuple，端到端也必须能直接使用。
            result = rag.retrieve(self.collection, "manifestprobe", 6, document_scope=tuple(scope))
        self.assertEqual(len(result), 6)
        self.assertTrue({doc.metadata["document_id"] for doc, _ in result} <= allowed)
        self.assertNotIn(str(outside), {doc.metadata["document_id"] for doc, _ in result})

    def test_257_corrupt_manifests_and_empty_scope_never_start_retrieval_or_embedding(self):
        manifest = [{"document_id": str(uuid.uuid4()), "index_version": 0} for _ in range(257)]
        corrupt = [
            manifest,
            [{**manifest[0], "index_version": True}],
            [{**manifest[0], "index_version": 1.5}],
            [{**manifest[0], "index_version": -1}],
            [{**manifest[0], "document_id": "not-a-uuid"}],
            [manifest[0], manifest[0]],
        ]
        with patch.object(rag, "get_vector_store") as store, \
             patch.object(retrieval_candidates, "keyword_candidates") as keyword:
            for value in corrupt:
                with self.subTest(size=len(value) if isinstance(value, list) else value), \
                     self.assertRaises(ValueError):
                    rag.retrieve(self.collection, "manifestprobe", 6, document_scope=value)
            self.assertEqual(rag.retrieve(self.collection, "manifestprobe", 6, document_scope=[]), [])
            self.assertEqual(rag.retrieve(self.collection, "manifestprobe", 6, document_scope=()), [])
        store.assert_not_called()
        keyword.assert_not_called()
        self.assertEqual(self.embeddings.query_calls, 0)

    def test_supplement_and_contains_accept_the_internal_manifest(self):
        manifest = [{"document_id": str(uuid.uuid4()), "index_version": 0} for _ in range(256)]
        inside = manifest[-1]
        anchor = (LCDocument(page_content="内部原文", metadata={
            **inside, "chunk_index": 0,
        }), 0.1)
        outsider = (LCDocument(page_content="范围外更强原文", metadata={
            "document_id": str(uuid.uuid4()), "index_version": 0, "chunk_index": 0,
        }), 0.0)
        self.assertTrue(document_scopes.contains(manifest, inside))
        self.assertTrue(document_scopes.contains(
            manifest, inside, max_documents=document_scopes.MAX_INTERNAL_SCOPE_DOCUMENTS))
        self.assertFalse(document_scopes.contains(
            manifest, inside, max_documents=document_scopes.MAX_SCOPE_DOCUMENTS))
        with patch.object(rag, "retrieve", return_value=[outsider, anchor]) as retrieve:
            result = rag.supplement_sources(self.collection, "manifestprobe", [anchor], 6, manifest)
        retrieve.assert_called_once_with(self.collection, "manifestprobe", 6, document_scope=manifest)
        self.assertEqual([doc.page_content for doc, _ in result], ["内部原文"])
