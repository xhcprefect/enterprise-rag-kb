"""R2b execution through real job/PG/source/retrieval contracts; model IO is deterministic."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
import unittest
import uuid
from unittest.mock import patch

import httpx
from langchain_core.documents import Document as LCDocument
from langchain_core.embeddings import Embeddings
from langchain_core.messages import AIMessageChunk
from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL
from tests import test_feedback_regression_jobs as fixtures

if TEST_URL:
    from sqlalchemy import func, select, text
    from app.core.config import settings
    from app.models import (ChatTurn, Conversation, Document, DocumentChunk, Message,
                            ModelUsage, ProjectChunk, ProjectFileVersion, ProjectSnapshot,
                            ProjectSnapshotFile, ProjectSource, User)
    from app.services import (answer_sources, feedback_regression_executor as executor,
                              feedback_regression_jobs as jobs, followup, project_retrieval,
                              rag, usage_tracking, vector_store)
    from app.services.cloud_models import CloudModelError
    SessionLocal, engine = fixtures.SessionLocal, fixtures.engine


class _Embeddings(Embeddings):
    def __init__(self):
        self.queries = []
        self.before_query = None
        self.vector_for_query = None

    def embed_documents(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    def embed_query(self, query):
        self.queries.append(query)
        if self.before_query:
            self.before_query()
        call = usage_tracking.ModelCall('embedding', 'regression.test', 'embedding')
        call.observe({'input_tokens': 7})
        call.finish()
        return self.vector_for_query(query) if self.vector_for_query else [1.0] + [0.0] * 1023


class _Chat:
    def __init__(self, mode='normal'):
        self.mode = mode
        self.calls = []
        self.entered = threading.Event()
        self.unblock = threading.Event()
        self.closed = threading.Event()

    async def astream(self, messages, *, usage_context=None):
        self.calls.append([{'type': message.type, 'content': message.content} for message in messages])
        number = len(self.calls)
        call = await asyncio.to_thread(
            lambda: usage_tracking.ModelCall('chat', 'regression.test', 'generation', context=usage_context))
        status = 'failed'
        try:
            self.entered.set()
            if self.mode == 'stall':
                while not self.unblock.is_set():
                    await asyncio.sleep(0.005)
            await asyncio.to_thread(call.observe, {'input_tokens': 20, 'output_tokens': 4})
            if self.mode == 'empty':
                yield AIMessageChunk(content='  ', response_metadata={'finish_reason': 'stop'})
            elif self.mode == 'interrupted' or (self.mode == 'retry' and number == 1):
                yield AIMessageChunk(content='partial')
                raise CloudModelError('PRIVATE_PROVIDER_BODY_MUST_NOT_BE_LOGGED')
            else:
                yield AIMessageChunk(
                    content=f'NEW_EXECUTION_{number} [1]',
                    response_metadata={'finish_reason': 'length' if self.mode == 'length' else 'stop'},
                    usage_metadata={'input_tokens': 20, 'output_tokens': 4, 'total_tokens': 24})
            status = 'succeeded'
        except (asyncio.CancelledError, GeneratorExit):
            status = 'cancelled'
            raise
        finally:
            await asyncio.to_thread(call.finish, status)
            self.closed.set()


@unittest.skipUnless(TEST_URL, '设置 TEST_DATABASE_URL 才运行真实回归执行测试')
class _ExecutionFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.FeedbackRegressionJobTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.cleanup()
        fixtures.FeedbackRegressionJobTests.client.close()
        fixtures.FeedbackRegressionJobTests.tearDownClass()

    @classmethod
    def cleanup(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(f'TRUNCATE {fixtures.TABLES} RESTART IDENTITY CASCADE'))
            if conn.execute(text("SELECT to_regclass('langchain_pg_collection')")).scalar():
                conn.execute(text('TRUNCATE langchain_pg_collection CASCADE'))
        vector_store._store.cache_clear()

    def setUp(self):
        self.cleanup()
        self.fx = fixtures.FeedbackRegressionJobTests()
        self.fx.setUp()
        self.embeddings = _Embeddings()
        self.chat = _Chat()
        for target, name, value in (
            (vector_store, 'get_embeddings', self.embeddings),
            (project_retrieval, 'get_embeddings', self.embeddings),
            (rag, 'get_stream_chat', self.chat),
        ):
            patcher = patch.object(target, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (
            ('RETRIEVAL_NEIGHBOR_CHUNKS', 0), ('RETRIEVAL_TOP_K', 6),
            ('HYBRID_RETRIEVAL_ENABLED', True), ('RERANK_ENABLED', False),
            ('EMBEDDING_PROVIDER', 'cloud'), ('LLM_PROVIDER', 'cloud'),
            ('RETRIEVAL_REWRITE_ENABLED', False), ('RETRIEVAL_MIN_SIMILARITY', 0),
            ('FEEDBACK_REGRESSION_JOB_RETRY_DELAY_SECONDS', 0),
        ):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ('WATCH_INTERVAL_SECONDS', 'STREAM_POLL_SECONDS'):
            patcher = patch.object(executor, name, 0.02)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = vector_store.get_vector_store(self.collection)
        self.store.embedding_function = self.embeddings
        self.persist_document(self.fx.doc_id)

    def tearDown(self):
        self.cleanup()

    @property
    def collection(self):
        with SessionLocal() as db:
            return db.get(fixtures.KnowledgeBase, self.fx.kb_id).collection_name

    def persist_document(self, identifier, vector=None):
        with SessionLocal() as db:
            document = db.get(Document, identifier)
            chunk = db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == identifier))
            value = LCDocument(page_content=chunk.content, metadata={
                'document_id': str(identifier), 'kb_id': str(document.kb_id),
                'filename': document.filename, 'index_version': document.index_version, 'chunk_index': 0})
            vector_store.persist_vectors(db, self.store, [value],
                                         [vector or [1.0] + [0.0] * 1023], [f'{identifier}:{document.index_version}:0'])
            db.commit()

    def add_document(self, name='extra.md'):
        with SessionLocal() as db:
            document = Document(kb_id=self.fx.kb_id, filename=name, file_ext='.md', file_size=1,
                                stored_path='/tmp/' + uuid.uuid4().hex, status='ready', enabled=True,
                                parse_done=True, chunk_count=1, index_version=0)
            db.add(document)
            db.flush()
            identifier = document.id
            db.add(DocumentChunk(document_id=identifier, chunk_index=0, content='在线回归资料分块 ' + name,
                                 search_text='在线回归资料分块 ' + name, source_metadata={'filename': name}, embedded=True))
            db.commit()
        self.persist_document(identifier)
        return identifier

    def feedback(self, *, mode='rag', questions=None, expected=True):
        if questions:
            target = self.fx.create_multi_turn(questions, mode=mode, kb_id=None if mode == 'chat' else 'default')
        else:
            target = self.fx.add_answer(mode=mode, kb_id=None if mode == 'chat' else 'default',
                                        question='在线回归资料问题', answer='OLD_ANSWER_MUST_NOT_BE_REUSED', citations=[])
        with SessionLocal() as db:
            for turn in db.scalars(select(ChatTurn).where(ChatTurn.conversation_id == target.conversation_id)):
                turn.trace = answer_sources.build_source_snapshot([], [], mode=mode)
            db.commit()
        feedback_id = self.fx.submit_ok(target.message_id)
        reviewed = self.fx.review(feedback_id,
            expected_sources=[{'document_id': str(self.fx.doc_id)}] if expected and mode == 'rag' else [],
            corrected_outcome='answer' if expected or mode == 'chat' else 'refuse')
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.fx.enroll_ok(feedback_id)
        return feedback_id, target

    def loaded(self, feedback_id, revision=1):
        job = self.fx.create_job_ok(feedback_id, revision=revision)
        claim = self.fx.claim(job['job_id'])
        return job, claim, self.fx.load(claim)

    def execute(self, loaded, stop=None):
        return asyncio.run(executor.execute(loaded, stop_event=stop or threading.Event()))

    def publish(self, claim, outcome):
        with SessionLocal() as db:
            return jobs.complete(db, job_id=claim.job_id, lease_id=claim.lease_id,
                                 answer=outcome.answer, citations=list(outcome.citations), trace=outcome.trace,
                                 supplied_sources=list(outcome.supplied_sources), history_sources=list(outcome.history_sources))

    def original_rows(self, conversation_id):
        with SessionLocal() as db:
            return {
                cls.__tablename__: [dict(row) for row in db.execute(
                    select(cls.__table__).where(
                        cls.id == conversation_id if cls is Conversation else cls.conversation_id == conversation_id)
                    .order_by(cls.id)).mappings().all()]
                for cls in (Conversation, ChatTurn, Message)
            }


class FeedbackRegressionExecutorTests(_ExecutionFixture):
    def test_two_executions_call_new_models_and_leave_original_rows_unchanged(self):
        feedback_id, target = self.feedback()
        before = self.original_rows(target.conversation_id)
        job, claim, loaded = self.loaded(feedback_id)
        outcome = self.execute(loaded)
        first = self.publish(claim, outcome)
        self.assertTrue(first.passed)
        second_job, second_claim, second_loaded = self.loaded(feedback_id, revision=2)
        second_outcome = self.execute(second_loaded)
        self.publish(second_claim, second_outcome)
        self.assertNotEqual(first.run_id, second_claim.run_id)
        self.assertNotEqual(outcome.answer, second_outcome.answer)
        self.assertEqual(len(self.chat.calls), 2)
        self.assertEqual(before, self.original_rows(target.conversation_id))
        self.assertNotIn('OLD_ANSWER_MUST_NOT_BE_REUSED', str(self.chat.calls))
        with SessionLocal() as db:
            rows = db.scalars(select(ModelUsage).where(ModelUsage.regression_job_id == claim.job_id)).all()
            self.assertEqual({row.kind for row in rows}, {'chat', 'embedding'})
            self.assertTrue(all(row.conversation_id is None and row.turn_id is None for row in rows))
            self.assertTrue(all(row.user_id == self.fx.admin_id and row.kb_id == self.fx.kb_id for row in rows))
        usage = self.fx.get_job(job['job_id']).json()['data']['usage']
        self.assertEqual(usage['input_tokens'], 27, [(row.kind, row.model, row.operation, row.input_tokens, row.output_tokens, row.status) for row in rows])
        self.assertEqual(usage['output_tokens'], 4)
        self.assertIsNone(usage['total_cost'])

    def test_six_document_frozen_manifest_excludes_new_document(self):
        before_ids = {str(self.fx.doc_id)} | {str(self.add_document(f'inside-{i}.md')) for i in range(5)}
        feedback_id, _ = self.feedback()
        _job, claim, loaded = self.loaded(feedback_id)
        outside = str(self.add_document('late-outside.md'))
        self.assertEqual(len(loaded.execution.upload_manifest), 6)
        outcome = self.execute(loaded)
        actual = {item['document_id'] for item in outcome.trace['candidate_sources']}
        self.assertEqual(actual, before_ids)
        self.assertNotIn(outside, actual)
        self.publish(claim, outcome)
        self.assertEqual(len(self.embeddings.queries), 1)

    def test_limited_scope_is_used_by_real_sql(self):
        self.add_document('not-selected.md')
        target = self.fx.add_answer(citations=[])
        with SessionLocal() as db:
            db.get(Conversation, target.conversation_id).document_scope = [
                {'document_id': str(self.fx.doc_id), 'index_version': 0}]
            db.get(ChatTurn, target.turn_id).trace = answer_sources.build_source_snapshot([], [], mode='rag')
            db.commit()
        feedback_id = self.fx.submit_ok(target.message_id)
        self.assertEqual(self.fx.review(feedback_id).status_code, 200)
        self.fx.enroll_ok(feedback_id)
        _job, claim, loaded = self.loaded(feedback_id)
        outcome = self.execute(loaded)
        self.assertEqual({item['document_id'] for item in outcome.supplied_sources}, {str(self.fx.doc_id)})
        self.publish(claim, outcome)

    def test_empty_manifest_avoids_embedding_and_never_widens(self):
        with SessionLocal() as db:
            db.delete(db.get(Document, self.fx.doc_id))
            db.commit()
        feedback_id, _ = self.feedback(expected=False)
        _job, claim, loaded = self.loaded(feedback_id)
        self.assertEqual(loaded.execution.upload_manifest, ())
        self.add_document('late-after-empty.md')
        outcome = self.execute(loaded)
        self.assertFalse(outcome.supplied_sources)
        self.assertFalse(outcome.trace['candidate_sources'])
        self.assertEqual(self.embeddings.queries, [])
        self.assertEqual(self.chat.calls, [])
        self.publish(claim, outcome)

    def test_multiturn_history_contains_only_new_answers_and_transitive_sources(self):
        second = self.add_document('second-topic.md')
        second_vector = [0.0, 1.0] + [0.0] * 1022
        self.persist_document(second, second_vector)
        self.embeddings.vector_for_query = lambda query: ([1.0] + [0.0] * 1023
            if 'phaseone' in query else second_vector)
        feedback_id, target = self.feedback(questions=['phaseone topic', 'phasetwo topic', 'phasethree topic'])
        _job, claim, loaded = self.loaded(feedback_id)
        captured = []
        real_prepare = rag.prepare_answer
        def keep_last(question, docs, history, **kwargs):
            captured.append([dict(item) for item in history])
            # Simulate the real context window removing the oldest complete turn.
            return real_prepare(question, docs, history[-2:] if len(history) > 2 else history, **kwargs)
        with patch.object(settings, 'HYBRID_RETRIEVAL_ENABLED', False), \
             patch.object(settings, 'RETRIEVAL_MIN_SIMILARITY', 0.5), \
             patch.object(executor, '_prepare_turn', side_effect=lambda q, d, h, m, p:
                          keep_last(q, d, h, mode=m, retrieval_query=p.query)):
            outcome = self.execute(loaded)
        self.assertEqual(len(self.chat.calls), 3)
        self.assertNotIn('回答1', str(self.chat.calls))
        self.assertIn('NEW_EXECUTION_1', str(self.chat.calls[1]))
        assistant = captured[2][-1]
        snapshot = assistant['source_provenance']
        self.assertEqual(snapshot['source_snapshot_version'], 1)
        self.assertEqual({item['document_id'] for item in snapshot['history_sources']}, {str(self.fx.doc_id)})
        self.assertEqual({item['document_id'] for item in outcome.supplied_sources}, {str(second)})
        self.assertIn(str(self.fx.doc_id), {item['document_id'] for item in outcome.history_sources})
        final_snapshot = executor._snapshot(outcome.supplied_sources, outcome.history_sources, 'rag')
        self.assertEqual({item['document_id'] for item in final_snapshot['history_sources']}, {str(self.fx.doc_id)})
        self.publish(claim, outcome)
        persisted = self.fx.body_row(claim.job_id).trace
        self.assertEqual({item['document_id'] for item in persisted['history_sources']}, {str(self.fx.doc_id)})

    def test_project_execution_keeps_original_snapshot_after_active_switch(self):
        kb_id, snapshot_id, original_file = self.fx.complete_project_binding()
        target = self.fx.add_answer(kb_id=kb_id, project_snapshot_id=snapshot_id,
                                    question='project frozen snapshot', citations=[])
        with SessionLocal() as db:
            db.get(ChatTurn, target.turn_id).trace = answer_sources.build_source_snapshot([], [], mode='rag')
            db.commit()
        feedback_id = self.fx.submit_ok(target.message_id)
        reviewed = self.fx.review(feedback_id, expected_sources=[{
            'document_id': str(original_file), 'filename': 'src/main.py'}])
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.fx.enroll_ok(feedback_id)
        _job, claim, loaded = self.loaded(feedback_id)
        before = self.original_rows(target.conversation_id)
        with SessionLocal() as db:
            source_id = db.get(ProjectSnapshot, snapshot_id).source_id
            newer = ProjectSnapshot(source_id=source_id, status='ready',
                                    manifest_fingerprint='f' * 64, config_fingerprint='e' * 64)
            db.add(newer)
            db.flush()
            version = ProjectFileVersion(source_id=source_id, relative_path='src/new.py',
                content_sha256='b' * 64, blob_path='/tmp/' + uuid.uuid4().hex,
                parse_done=True, parse_status='ready')
            db.add(version)
            db.flush()
            new_file = version.id
            db.add(ProjectSnapshotFile(snapshot_id=newer.id, relative_path='src/new.py', file_version_id=new_file))
            db.add(ProjectChunk(file_version_id=new_file, chunk_index=0,
                content='NEW_ACTIVE_SNAPSHOT_CONTENT', search_text='project frozen snapshot', embedded=True))
            db.get(ProjectSource, source_id).active_snapshot_id = newer.id
            collection = db.get(fixtures.KnowledgeBase, kb_id).collection_name
            db.commit()
        store = vector_store.get_vector_store(collection)
        store.embedding_function = self.embeddings
        with SessionLocal() as db:
            for identifier in (original_file, new_file):
                chunk = db.scalar(select(ProjectChunk).where(ProjectChunk.file_version_id == identifier))
                doc = LCDocument(page_content=chunk.content, metadata={
                    'source_kind': 'project', 'file_version_id': str(identifier), 'chunk_index': 0})
                vector_store.persist_vectors(db, store, [doc], [[1.0] + [0.0] * 1023],
                    [f'{settings.PROJECT_VECTOR_PREFIX}{identifier}:0'])
            db.commit()
        self.embeddings.before_query = lambda: self.assertEqual(engine.pool.checkedout(), 0)
        with patch.object(executor, 'WATCH_INTERVAL_SECONDS', 30):
            outcome = self.execute(loaded)
        for sources in (outcome.trace['candidate_sources'], outcome.supplied_sources, outcome.citations):
            self.assertEqual({item['file_version_id'] for item in sources}, {str(original_file)})
            self.assertEqual({item['snapshot_id'] for item in sources}, {str(snapshot_id)})
            self.assertTrue(all(item['manifest_fingerprint'] == 'd' * 64 for item in sources))
            self.assertTrue(all(item['config_fingerprint'] == 'e' * 64 for item in sources))
        # Public project citations legitimately retain the legacy compatibility version.
        self.assertEqual(outcome.citations[0]['index_version'], 0)
        self.assertNotIn('NEW_ACTIVE_SNAPSHOT_CONTENT', str(self.chat.calls))
        self.assertEqual(len(self.embeddings.queries), 1)
        self.assertTrue(self.publish(claim, outcome).passed)
        self.assertEqual(before, self.original_rows(target.conversation_id))

    def test_snapshot_retains_flat_history_and_rejects_malformed_identity(self):
        a = self.fx.citation()
        b = {**a, 'document_id': str(uuid.uuid4()), 'filename': 'inherited.md'}
        snapshot = executor._snapshot([a], [b], 'rag')
        self.assertEqual(snapshot['history_sources'][0]['document_id'], b['document_id'])
        self.assertEqual(len(answer_sources.parse_source_snapshot(snapshot)), 2)
        with self.assertRaises(executor.ExecutionAborted):
            executor._snapshot([a], [{**b, 'index_version': True}], 'rag')

    def test_actual_rewrite_embedding_rerank_and_generation_share_job_usage(self):
        feedback_id, _ = self.feedback(questions=['original policy topic', 'what about retry behavior'])
        _job, claim, loaded = self.loaded(feedback_id)
        requests = []
        def respond(request):
            payload = json.loads(request.content)
            requests.append((request.url.path, payload))
            if request.url.path.endswith('/rerank'):
                return httpx.Response(200, json={
                    'results': [{'index': i, 'relevance_score': 1.0} for i in range(len(payload['documents']))],
                    'usage': {'total_tokens': 11}})
            self.assertTrue(request.url.path.endswith('/chat/completions'))
            return httpx.Response(200, json={
                'choices': [{'message': {'content': json.dumps({'action': 'search', 'query': 'original policy retry behavior'})}}],
                'usage': {'prompt_tokens': 13, 'completion_tokens': 3}})
        actual_client = httpx.AsyncClient
        def client(*args, **kwargs):
            return actual_client(*args, **kwargs, transport=httpx.MockTransport(respond))
        with patch.object(settings, 'RETRIEVAL_REWRITE_ENABLED', True), \
             patch.object(settings, 'RERANK_ENABLED', True), \
             patch.object(settings, 'RERANK_BASE_URL', 'http://127.0.0.1:9/v1'), \
             patch.object(settings, 'RERANK_MODEL', 'regression.rerank'), \
             patch.object(settings, 'LLM_BASE_URL', 'http://127.0.0.1:9/v1'), \
             patch.object(settings, 'LLM_API_KEY', 'regression-test-only'), \
             patch.object(httpx, 'AsyncClient', side_effect=client):
            outcome = self.execute(loaded)
        self.assertEqual(sum(path.endswith('/chat/completions') for path, _ in requests), 1)
        self.assertEqual(sum(path.endswith('/rerank') for path, _ in requests), 2)
        self.assertEqual(len(self.embeddings.queries), 2)
        self.assertEqual(len(self.chat.calls), 2)
        self.assertIn('original policy retry behavior', self.embeddings.queries)
        with SessionLocal() as db:
            rows = db.scalars(select(ModelUsage).where(ModelUsage.regression_job_id == claim.job_id)).all()
            self.assertEqual({row.operation for row in rows}, {'rewrite', 'query_embedding', 'rerank', 'generation'})
            self.assertEqual(len(rows), 7)
            self.assertTrue(all(row.user_id == self.fx.admin_id and row.kb_id == self.fx.kb_id for row in rows))
            self.assertTrue(all(row.conversation_id is None and row.turn_id is None and row.document_id is None for row in rows))
        self.publish(claim, outcome)
        usage = self.fx.get_job(claim.job_id).json()['data']['usage']
        self.assertEqual((usage['input_tokens'], usage['output_tokens']), (89, 11))
        self.assertIsNone(usage['total_cost'])
        self.assertIsNone(usage['known_cost_subtotal'])

    def test_generation_empty_interrupted_and_length_never_publish(self):
        for mode, code in [('empty', 'generation_empty'), ('interrupted', 'model_error'), ('length', 'answer_too_large')]:
            with self.subTest(mode=mode):
                self.chat.mode = mode
                feedback_id, _ = self.feedback(mode='chat')
                _job, claim, loaded = self.loaded(feedback_id)
                with self.assertRaises(Exception) as raised:
                    self.execute(loaded)
                actual_code, _ = executor.classify_failure(raised.exception)
                self.assertEqual(actual_code, code)
                self.assertTrue(self.chat.closed.is_set())
                self.assertIsNone(self.fx.body_row(claim.job_id))
                with SessionLocal() as db:
                    jobs.fail(db, job_id=claim.job_id, lease_id=claim.lease_id, error_code=actual_code)

    def _watch_boundary(self, mutation, *, expected, mode='rag'):
        feedback_id, _ = self.feedback(mode=mode)
        job, claim, loaded = self.loaded(feedback_id)
        self.chat.mode = 'stall'
        stop = threading.Event()
        async def exercise():
            task = asyncio.create_task(executor.execute(loaded, stop_event=stop))
            self.assertTrue(await asyncio.to_thread(self.chat.entered.wait, 3))
            await asyncio.to_thread(mutation, claim, stop)
            with self.assertRaises(expected):
                await asyncio.wait_for(task, 4)
        asyncio.run(exercise())
        self.assertTrue(self.chat.closed.is_set())
        self.assertIsNone(self.fx.body_row(claim.job_id))
        return job, claim

    def test_cancel_while_waiting_closes_stream_and_fences_publication(self):
        def cancel(claim, _stop):
            current = self.fx.get_job(claim.job_id).json()['data']
            self.assertEqual(self.fx.cancel_job(claim.job_id, current['revision']).status_code, 200)
        self._watch_boundary(cancel, expected=executor.ExecutionLeaseLost)

    def test_owner_acl_loss_while_waiting_closes_stream(self):
        def revoke(_claim, _stop):
            with SessionLocal() as db:
                db.get(fixtures.KnowledgeBase, self.fx.kb_id).is_public = False
                db.get(fixtures.KnowledgeBase, self.fx.kb_id).owner_id = self.fx.other_id
                db.commit()
        self._watch_boundary(revoke, expected=executor.ExecutionAborted)

    def test_original_admin_token_change_while_waiting_closes_stream(self):
        def revoke(_claim, _stop):
            with SessionLocal() as db:
                db.get(User, self.fx.admin_id).token_version += 1
                db.commit()
        self._watch_boundary(revoke, expected=executor.ExecutionAborted)

    def test_expired_lease_while_waiting_closes_stream(self):
        def expire(claim, _stop):
            with SessionLocal() as db:
                db.get(fixtures.FeedbackRegressionJob, claim.job_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                db.commit()
        self._watch_boundary(expire, expected=executor.ExecutionLeaseLost)

    def test_total_deadline_while_waiting_is_not_extended_by_heartbeat(self):
        def expire(claim, _stop):
            with SessionLocal() as db:
                db.get(fixtures.FeedbackRegressionJob, claim.job_id).deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                db.commit()
        self._watch_boundary(expire, expected=executor.ExecutionAborted)

    def test_cooperative_stop_while_waiting_closes_stream(self):
        self._watch_boundary(lambda _claim, stop: stop.set(), expected=executor.ExecutionStopped)

    def test_first_content_timeout_with_live_authorization_heartbeats(self):
        with patch.object(settings, 'CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS', 0.12):
            feedback_id, _ = self.feedback(mode='chat')
            _job, _claim, loaded = self.loaded(feedback_id)
            self.chat.mode = 'stall'
            with self.assertRaises(Exception) as raised:
                self.execute(loaded)
            self.assertEqual(executor.classify_failure(raised.exception), ('model_timeout', True))
            self.assertTrue(self.chat.closed.is_set())

    def test_model_wait_has_no_connection_owned_by_execution(self):
        feedback_id, _ = self.feedback()
        _job, _claim, loaded = self.loaded(feedback_id)
        self.chat.mode = 'stall'
        async def exercise():
            task = asyncio.create_task(executor.execute(loaded, stop_event=threading.Event()))
            self.assertTrue(await asyncio.to_thread(self.chat.entered.wait, 3))
            # Hold every root used by the job in a separate real transaction. A model wait
            # retaining roots would prevent these NOWAIT locks.
            def lock_roots():
                self.assertEqual(engine.pool.checkedout(), 0)
                with SessionLocal() as db:
                    db.execute(select(User).where(User.id.in_([self.fx.owner_id, self.fx.admin_id])).order_by(User.id).with_for_update(nowait=True)).all()
                    db.execute(select(fixtures.KnowledgeBase).where(fixtures.KnowledgeBase.id == self.fx.kb_id).with_for_update(nowait=True)).all()
                    db.rollback()
            try:
                await asyncio.to_thread(lock_roots)
            finally:
                self.chat.unblock.set()
            return await asyncio.wait_for(task, 3)
        # Check the model waiter between authorization ticks, not while the independent
        # watcher legitimately owns its short authorization transaction (covered above).
        with patch.object(executor, 'WATCH_INTERVAL_SECONDS', 30):
            outcome = asyncio.run(exercise())
        self.assertIn('NEW_EXECUTION', outcome.answer)


if __name__ == '__main__':
    unittest.main()
