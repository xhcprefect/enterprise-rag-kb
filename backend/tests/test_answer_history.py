"""真实隔离 PG：无效来源整轮离开模型历史，展示记录和传递来源仍准确。"""
import asyncio
import copy
import json
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL
from app.core.config import settings
from app.services import answer_history, answer_sources, chat_turns, followup, rag

if TEST_URL:
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, engine
    from app.models import ChatTurn, Conversation, Document, DocumentChunk, KnowledgeBase, Message, User


TABLES = "users, knowledge_bases, documents, document_chunks, conversations, chat_turns, messages, audit_logs"


def snapshot(supplied=(), historical=(), **extra):
    return {"source_snapshot_version": 1, "supplied_sources": list(supplied),
            "history_sources": list(historical), **extra}


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离历史来源数据库测试")
class AnswerHistoryTests(unittest.TestCase):
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
            user = User(username="history_user", password_hash="x", role="user", is_active=True, token_version=4)
            owner = User(username="history_owner", password_hash="x", role="user", is_active=True)
            db.add_all([user, owner])
            db.flush()
            kb = KnowledgeBase(name="history", owner_id=owner.id, is_public=True, kind="upload",
                               collection_name="hist_" + uuid.uuid4().hex)
            db.add(kb)
            db.commit()
            self.user_id, self.owner_id, self.kb_id = user.id, owner.id, kb.id
        self.token = 4

    def add_document(self, name):
        with SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename=name, file_ext=".txt", file_size=1,
                           stored_path="/tmp/" + uuid.uuid4().hex, status="ready", index_version=0,
                           cancel_requested=False, parse_done=True, chunk_count=1)
            db.add(doc)
            db.flush()
            db.add(DocumentChunk(document_id=doc.id, chunk_index=0, content=name + "正文", embedded=True))
            db.commit()
            return {"source_kind": "document", "document_id": str(doc.id), "filename": name,
                    "index_version": 0, "chunk_index": 0}

    def claim(self, conversation_id=None):
        with SessionLocal() as db:
            return chat_turns.claim_turn(db, user_id=self.user_id, conversation_id=conversation_id,
                                         kb_id=self.kb_id, mode="rag", question="本轮问题", top_k=6,
                                         request_id=uuid.uuid4(), auth_token_version=self.token)

    def publish(self, reservation, trace, answer="历史结论[1]。", citations=()):
        with SessionLocal() as db:
            return chat_turns.complete_turn(db, reservation, answer, list(citations), 1, trace)

    def load(self, reservation):
        with SessionLocal() as db:
            return answer_history.load_rag_history(db, reservation)

    def test_three_turns_carry_uncited_source_and_exclude_descendants_after_disable(self):
        a, b = self.add_document("A.txt"), self.add_document("B.txt")
        first = self.claim()
        self.publish(first, snapshot([a, b], followup_topic="未成年人量刑"), citations=[{**a, "source_index": 1}])
        second = self.claim(first.conversation_id)
        history = self.load(second)
        self.assertEqual(len(history), 2)
        self.assertEqual([item["document_id"] for item in history[1]["citations"]], [a["document_id"]])
        inherited = answer_sources.sources_from_history(history)
        self.assertEqual({item["document_id"] for item in inherited}, {a["document_id"], b["document_id"]})
        self.publish(second, snapshot(historical=inherited), answer="延续原先的条件。")
        third = self.claim(first.conversation_id)
        self.assertEqual(len(self.load(third)), 4)
        with SessionLocal() as db:
            db.get(Document, uuid.UUID(b["document_id"])).enabled = False
            db.commit()
        model_history = self.load(third)
        self.assertEqual(model_history, [])
        with patch.object(followup, "complete_short_chat") as rewrite:
            plan = followup.plan_retrieval("那未成年人呢？", model_history)
        rewrite.assert_not_called()
        self.assertTrue(plan.clarification)
        prepared = rag.prepare_answer("独立新问题", [], model_history)
        self.assertFalse(any("历史结论" in message.content or "延续原先" in message.content for message in prepared.messages))
        with SessionLocal() as db:
            visible = db.scalars(select(Message).where(Message.conversation_id == first.conversation_id,
                                                       Message.role == "assistant")).all()
            self.assertEqual(len(visible), 2)
            self.assertEqual(visible[0].content if visible[0].id == self._answer_id(db, first.turn_id) else visible[1].content,
                             "历史结论[1]。")

    def _answer_id(self, db, turn_id):
        return db.scalar(select(Message.id).where(Message.turn_id == turn_id, Message.role == "assistant"))

    def test_expiry_reindex_supersession_and_missing_chunk_remove_the_whole_pair(self):
        for change in ("expiry", "reindex", "superseded", "chunk"):
            with self.subTest(change=change):
                source = self.add_document(change + ".txt")
                first = self.claim()
                self.publish(first, snapshot([source]))
                current = self.claim(first.conversation_id)
                self.assertEqual(len(self.load(current)), 2)
                with SessionLocal() as db:
                    doc = db.get(Document, uuid.UUID(source["document_id"]))
                    if change == "expiry":
                        doc.valid_until = datetime.now(timezone.utc) - timedelta(seconds=1)
                    elif change == "reindex":
                        doc.index_version += 1
                    elif change == "superseded":
                        doc.superseded_at = datetime.now(timezone.utc)
                    else:
                        chunk = db.scalar(select(DocumentChunk).where(DocumentChunk.document_id == doc.id))
                        chunk.embedded = False
                    db.commit()
                self.assertEqual(self.load(current), [])

    def test_legacy_null_bad_and_oversized_snapshot_cannot_enter_model_history(self):
        a = self.add_document("A.txt")
        bad = (None, {}, {"supplied_sources": [a]}, snapshot(source_snapshot_version=True),
               snapshot(supplied_sources=None), snapshot(history_sources="bad"),
               snapshot([{**a, "index_version": True}]),
               snapshot([{**a, "document_id": str(uuid.uuid4())} for _ in range(257)]))
        for trace in bad:
            with self.subTest(trace_type=type(trace).__name__):
                first = self.claim()
                self.publish(first, snapshot([a], followup_topic="should-not-reach-the-model"))
                with SessionLocal() as db:
                    db.get(ChatTurn, first.turn_id).trace = trace
                    db.commit()
                current = self.claim(first.conversation_id)
                self.assertEqual(self.load(current), [])

    def test_explicit_empty_snapshot_keeps_a_completed_refusal(self):
        first = self.claim()
        self.publish(first, snapshot(), answer="根据现有知识库无法回答。")
        current = self.claim(first.conversation_id)
        history = self.load(current)
        self.assertEqual(len(history), 2)
        self.assertEqual(answer_sources.sources_from_history(history), ())

    def test_bad_pair_turn_owner_status_and_snapshot_are_not_repaired_by_adjacency(self):
        for field in ("reply", "user_message", "owner", "status", "snapshot", "message_status"):
            with self.subTest(field=field):
                first = self.claim()
                message_id = self.publish(first, snapshot())
                current = self.claim(first.conversation_id)
                with SessionLocal() as db:
                    turn = db.get(ChatTurn, first.turn_id)
                    answer = db.get(Message, message_id)
                    if field == "reply":
                        answer.reply_to_id = current.user_message_id
                    elif field == "user_message":
                        turn.user_message_id = current.user_message_id
                    elif field == "owner":
                        turn.user_id = self.owner_id
                    elif field == "status":
                        turn.status = "failed"
                    elif field == "snapshot":
                        turn.project_snapshot_id = uuid.uuid4()
                    else:
                        answer.status = "failed"
                    db.commit()
                self.assertEqual(self.load(current), [])

    def test_fresh_root_auth_is_required_even_when_history_has_no_sources(self):
        for change in ("token", "disabled", "acl"):
            with self.subTest(change=change):
                first = self.claim()
                self.publish(first, snapshot())
                current = self.claim(first.conversation_id)
                with SessionLocal() as reader:
                    reader.get(User, self.user_id)
                    reader.get(KnowledgeBase, self.kb_id)
                    with SessionLocal() as writer:
                        if change == "token":
                            writer.get(User, self.user_id).token_version += 1
                        elif change == "disabled":
                            writer.get(User, self.user_id).is_active = False
                        else:
                            writer.get(KnowledgeBase, self.kb_id).is_public = False
                        writer.commit()
                    with self.assertRaises(chat_turns.ChatLeaseLost):
                        answer_history.load_rag_history(reader, current)
                with SessionLocal() as db:
                    db.get(User, self.user_id).token_version = self.token
                    db.get(User, self.user_id).is_active = True
                    db.get(KnowledgeBase, self.kb_id).is_public = True
                    db.commit()

    def test_missing_invalid_original_token_and_changed_conversation_fail_closed(self):
        first = self.claim()
        for token in (None, True, "4", 4.0, -1, 5):
            with self.subTest(token=token), self.assertRaises(chat_turns.ChatLeaseLost):
                self.load(replace(first, auth_token_version=token))
        for change in ({"user_id": self.owner_id}, {"kb_id": uuid.uuid4()}, {"project_snapshot_id": uuid.uuid4()},
                       {"scope_revision": 99}, {"mode": "chat"}):
            with self.subTest(change=change), self.assertRaises(chat_turns.ChatLeaseLost):
                self.load(replace(first, **change))

    def test_bounded_read_never_uses_an_assistant_without_its_question(self):
        first = self.claim()
        self.publish(first, snapshot(), answer="旧轮")
        second = self.claim(first.conversation_id)
        self.publish(second, snapshot(), answer="近轮")
        third = self.claim(first.conversation_id)
        with patch.object(settings, "HISTORY_MAX_TURNS", 1):
            history = self.load(third)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["content"], "近轮")
        with patch.object(settings, "HISTORY_MAX_TURNS", 0):
            self.assertEqual(self.load(third), [])

    def http_stream(self, conversation_id, docs, parts, *, question="独立问题", on_generation=None):
        from fastapi import Request
        from app.api.v1 import chat
        from app.schemas.chat import ChatRequest

        async def generate(*_args, **_kwargs):
            for part in parts:
                yield part
            if on_generation:
                on_generation()

        async def consume(response):
            return [frame async for frame in response.body_iterator]

        request = Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": []})
        request.state.auth_token_version = self.token
        with SessionLocal() as db, patch.object(rag, "retrieve", return_value=docs), \
             patch.object(rag, "answer_astream", side_effect=generate) as generation:
            user = db.get(User, self.user_id)
            response = chat.chat_stream(ChatRequest(kb_id=self.kb_id, conversation_id=conversation_id,
                                                    question=question), request, db, user)
            frames = asyncio.run(consume(response))
        events = [(frame.split("\n", 1)[0].removeprefix("event: "),
                   json.loads(frame.split("data: ", 1)[1])) for frame in frames]
        return events, generation

    def _rewrite_scenario(self, *, on_generation=None, source_handle=None):
        """独立会话：第一轮真实发布（supplied A+B、assistant 正文含 B 标记）；
        第二轮用真实 claim_turn/complete_turn 发布、trace 明确只 A 无 B（不靠改写遮蔽）；
        第三轮追问经 http_stream：4096/256/64 预算下 rewrite 输入含 B 标记、
        generation bounded_history 不含。返回 (events, rewrite_msgs, prepared, a, b, conv_id)。

        source_handle（可选 dict）在 B 文档创建后接收其 document_id，供调用方在
        传入回调前无法预先知晓具体文档身份时按句柄构造回调。"""
        from langchain_core.documents import Document as LCDocument
        a, b = self.add_document("A.txt"), self.add_document("B.txt")
        if source_handle is not None:
            source_handle["document_id"] = b["document_id"]
        docs = [(LCDocument(page_content=source["filename"] + "原文", metadata=source), 0.1)
                for source in (a, b)]
        # 第一轮 assistant 正文明确合成 B 标记；padding 使 B 轮字节超过生成
        # bounded_history 的 remaining 预算被整轮裁出、但仍在 rewrite 的
        # history_budget（≈2048）内保留（校准窗口 450-600，取 520）。
        first, _ = self.http_stream(None, docs,
                                    ["参考B文件结论的合并回答[1]。" + "的" * 520],
                                    )
        self.assertEqual(first[-1][0], "done")
        conv_id = uuid.UUID(first[0][1]["conversation_id"])
        # 第二轮真实发布且 trace 明确只 A：旧 B 只能经第三轮 rewrite 依赖传递。
        second_docs = [(LCDocument(page_content="当前可用资料" * 10000, metadata=a), 0.1)]
        with SessionLocal() as db:
            reservation = chat_turns.claim_turn(db, user_id=self.user_id, conversation_id=conv_id,
                                                kb_id=self.kb_id, mode="rag", question="较新独立问题",
                                                top_k=6, request_id=uuid.uuid4(),
                                                auth_token_version=self.token)
            chat_turns.complete_turn(db, reservation, "较新回答只用A[1]。",
                                     [{**a, "source_index": 1}], 100, snapshot([a]))
            db.commit()
        with SessionLocal() as db:
            newer = db.scalars(select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant")).all()
            self.assertEqual(len(newer), 2)
        derived = {"action": "search", "query": "inherited subject from the older source"}
        captured = []
        original_prepare = rag.prepare_answer

        def record_prepare(question, docs, history, **kwargs):
            prepared = original_prepare(question, docs, history, **kwargs)
            captured.append(prepared)
            return prepared

        with patch.object(settings, "LLM_CONTEXT_WINDOW_TOKENS", 4096), \
             patch.object(settings, "LLM_MAX_OUTPUT_TOKENS", 256), \
             patch.object(settings, "CONTEXT_TOKEN_SAFETY_MARGIN", 64), \
             patch.object(followup, "complete_short_chat", return_value=json.dumps(derived)) as rewrite, \
             patch.object(rag, "prepare_answer", side_effect=record_prepare):
            events, _ = self.http_stream(conv_id, second_docs, ["仅使用本轮资料。"], question="那种情况呢？",
                                         on_generation=on_generation)
        self.assertEqual(len(rewrite.call_args_list), 1)
        self.assertEqual(len(captured), 1)
        return events, rewrite.call_args_list[0].args[0], captured[0], a, b, conv_id

    def test_http_rewrite_planning_survives_generation_trim_positive(self):
        """独立正例会话：旧轮 B 参与改写、被生成预算裁出 bounded_history 且无引用时，
        派生 query 进生成 prompt、B 随规划依赖落 trace，正常发布。"""
        events, rewrite_msgs, prepared, a, b, conv_id = self._rewrite_scenario()
        # rewrite 实际输入含第一轮正文（含 B 标记）；generation bounded_history 不含该标记。
        rewrite_text = "".join(str(message.content) for message in rewrite_msgs)
        self.assertIn("参考B文件结论", rewrite_text)
        prompt_text = "".join(str(message.content) for message in prepared.messages)
        self.assertIn("inherited subject from the older source", prompt_text)
        self.assertNotIn("参考B文件结论", prompt_text)
        self.assertNotIn(b["document_id"], [item["document_id"] for item in prepared.history_sources])
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["citations"], [])
        with SessionLocal() as db:
            answer = db.get(Message, uuid.UUID(events[-1][1]["message_id"]))
            trace = db.get(ChatTurn, answer.turn_id).trace
            self.assertEqual(trace["status"], "rewritten")
            # B 只能经规划依赖进 history_sources；supplied（本轮实际送入）不含 B。
            self.assertNotIn(b["document_id"], [item["document_id"] for item in trace["supplied_sources"]])
            self.assertIn(b["document_id"], [item["document_id"] for item in trace["history_sources"]])

    def test_http_rewrite_planning_survives_generation_trim_negative_disabled_source(self):
        """独立负例会话：同样构造，但第三轮 generation 阶段停用 B——发布复核因 B 失效
        整轮拒绝：error、无 done、该会话 assistant 计数停在 2。"""
        from app.models import Message
        handle = {}
        events, rewrite_msgs, prepared, a, b, conv_id = self._rewrite_scenario(
            source_handle=handle,
            on_generation=lambda: self._disable_document(handle["document_id"]))
        self.assertEqual(events[-1][0], "error")
        self.assertFalse(any(name == "done" for name, _ in events))
        with SessionLocal() as db:
            assistant = db.scalars(select(Message).where(
                Message.conversation_id == conv_id, Message.role == "assistant")).all()
            self.assertEqual(len(assistant), 2)
            # 证明拒绝由 B 的发布门禁触发：B 已停用且本轮无成功 assistant（仍 2 条）。
            self.assertFalse(db.get(Document, uuid.UUID(b["document_id"])).enabled)

    def _disable_document(self, document_id):
        with SessionLocal() as db:
            db.get(Document, uuid.UUID(document_id)).enabled = False
            db.commit()

    def test_http_path_persists_transitive_sources_and_filters_before_followup_planning(self):
        from langchain_core.documents import Document as LCDocument
        a, b = self.add_document("A.txt"), self.add_document("B.txt")
        docs = [(LCDocument(page_content=source["filename"] + "原文", metadata=source), 0.1)
                for source in (a, b)]
        first, _ = self.http_stream(None, docs, ["两份资料共同影响回答[1]。"])
        self.assertEqual(first[-1][0], "done")
        conv_id = uuid.UUID(first[0][1]["conversation_id"])
        second, _ = self.http_stream(conv_id, [(docs[0])], ["后续回答没有编号。"])
        self.assertEqual(second[-1][0], "done")
        self.assertEqual(second[-1][1]["citations"], [])
        with SessionLocal() as db:
            answer = db.get(Message, uuid.UUID(second[-1][1]["message_id"]))
            trace = db.get(ChatTurn, answer.turn_id).trace
            self.assertEqual({item["document_id"] for item in trace["history_sources"]},
                             {a["document_id"], b["document_id"]})
            db.get(Document, uuid.UUID(b["document_id"])).enabled = False
            db.commit()
        with patch.object(followup, "complete_short_chat") as rewrite, \
             patch.object(followup, "plan_retrieval", wraps=followup.plan_retrieval) as planner:
            third, generation = self.http_stream(conv_id, [], ["must-not-run"], question="那未成年人呢？")
        rewrite.assert_not_called()
        generation.assert_not_called()
        self.assertEqual(planner.call_args.args[1], [])
        self.assertEqual(third[-1][0], "done")

    def test_http_generation_cannot_save_after_an_uncited_supplied_document_is_disabled(self):
        from langchain_core.documents import Document as LCDocument
        a, b = self.add_document("A.txt"), self.add_document("B.txt")
        docs = [(LCDocument(page_content=source["filename"] + "原文", metadata=source), 0.1)
                for source in (a, b)]
        def disable():
            with SessionLocal() as db:
                db.get(Document, uuid.UUID(b["document_id"])).enabled = False
                db.commit()
        events, _ = self.http_stream(None, docs, ["回答仅引用 A[1]。"], on_generation=disable)
        self.assertEqual(events[-1][0], "error")
        self.assertFalse(any(name == "done" for name, _ in events))
        with SessionLocal() as db:
            self.assertEqual(db.scalars(select(Message).where(Message.role == "assistant")).all(), [])
            turn = db.scalar(select(ChatTurn))
            self.assertEqual(turn.status, "cancelled")

    def test_project_history_keeps_its_snapshot_and_rechecks_revocation_even_without_sources(self):
        from app.models import ProjectChunk, ProjectFileVersion, ProjectSnapshot, ProjectSnapshotFile, ProjectSource
        with SessionLocal() as db:
            db.get(KnowledgeBase, self.kb_id).kind = "project"
            source = ProjectSource(kb_id=self.kb_id, name="history-project", root_key="root", relative_dir="",
                                   root_fingerprint="f" * 64, created_by=self.user_id)
            db.add(source)
            db.flush()
            first_snapshot = ProjectSnapshot(source_id=source.id, status="ready",
                                             manifest_fingerprint="a" * 64, config_fingerprint="b" * 64)
            second_snapshot = ProjectSnapshot(source_id=source.id, status="ready",
                                              manifest_fingerprint="c" * 64, config_fingerprint="b" * 64)
            version = ProjectFileVersion(source_id=source.id, relative_path="src/main.py", content_sha256="d" * 64,
                                         blob_path="/tmp/history-blob", parse_done=True, parse_status="ready",
                                         config_fingerprint="b" * 64)
            db.add_all([first_snapshot, second_snapshot, version])
            db.flush()
            db.add(ProjectChunk(file_version_id=version.id, chunk_index=0, content="project body", embedded=True))
            for item in (first_snapshot, second_snapshot):
                db.add(ProjectSnapshotFile(snapshot_id=item.id, relative_path="src/main.py", file_version_id=version.id))
            source.active_snapshot_id = first_snapshot.id
            db.commit()
            source_id, first_sid, second_sid, file_id = source.id, first_snapshot.id, second_snapshot.id, version.id
        identity = {"source_kind": "project", "source_id": str(source_id), "snapshot_id": str(first_sid),
                    "file_version_id": str(file_id), "document_id": str(file_id), "chunk_index": 0,
                    "relative_path": "src/main.py", "manifest_fingerprint": "a" * 64,
                    "config_fingerprint": "b" * 64}
        first = self.claim()
        self.publish(first, snapshot([identity]))
        with SessionLocal() as db:
            db.get(ProjectSource, source_id).active_snapshot_id = second_sid
            db.commit()
        current = self.claim(first.conversation_id)
        self.assertEqual(current.project_snapshot_id, first_sid)
        self.assertEqual(len(self.load(current)), 2)
        with SessionLocal() as db:
            db.get(ChatTurn, first.turn_id).trace = snapshot([{**identity, "snapshot_id": str(second_sid),
                                                            "manifest_fingerprint": "c" * 64}])
            db.commit()
        self.assertEqual(self.load(current), [])
        with SessionLocal() as db:
            db.get(ChatTurn, first.turn_id).trace = snapshot()
            db.get(ProjectSource, source_id).revoked_at = datetime.now(timezone.utc)
            db.commit()
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.load(current)

    def test_http_original_token_is_not_replaced_with_the_fresh_user_version(self):
        from fastapi import HTTPException, Request
        from app.api.v1 import chat
        from app.schemas.chat import ChatRequest
        with SessionLocal() as db:
            user = db.get(User, self.user_id)
            user.token_version += 1
            db.commit()
            request = Request({"type": "http", "method": "POST", "path": "/api/v1/chat/stream", "headers": []})
            request.state.auth_token_version = self.token
            with self.assertRaises(HTTPException) as caught:
                chat.chat_stream(ChatRequest(kb_id=self.kb_id, question="问题"), request, db, user)
            self.assertEqual(caught.exception.status_code, 401)
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(ChatTurn)))

    def test_database_error_is_not_reported_as_empty_successful_history(self):
        first = self.claim()
        self.publish(first, snapshot())
        current = self.claim(first.conversation_id)
        error = OperationalError("synthetic", {}, RuntimeError("synthetic"))
        with patch.object(answer_sources, "validate_sources", side_effect=error):
            with self.assertRaises(OperationalError):
                self.load(current)

    def test_loading_does_not_commit_rollback_flush_or_modify_persisted_messages(self):
        a = self.add_document("A.txt")
        first = self.claim()
        message_id = self.publish(first, snapshot([a], private_diagnostic="do-not-send"))
        current = self.claim(first.conversation_id)
        with SessionLocal() as db:
            original = db.get(Message, message_id).content
            with patch.object(db, "commit") as commit, patch.object(db, "rollback") as rollback, patch.object(db, "flush") as flush:
                history = answer_history.load_rag_history(db, current)
                commit.assert_not_called()
                rollback.assert_not_called()
                flush.assert_not_called()
            before = copy.deepcopy(history)
            history[-1]["source_provenance"]["supplied_sources"].clear()
            self.assertTrue(before[-1]["source_provenance"]["supplied_sources"])
            self.assertEqual(db.get(Message, message_id).content, original)
            self.assertNotIn("private_diagnostic", before[-1])
