"""A2 读取接入：显式成员可读私有上传库/项目库；撤权立即隐藏（真实 PostgreSQL）。

覆盖：
- 私有上传库 A 的 reader/maintainer 可问答/回放/反馈；库 B stranger 拒绝；
- public/owner/admin 兼容、disabled 拒绝；
- 会话列表/分页在 SQL 的 total/LIMIT 之前按成员 EXISTS 过滤；撤权后标题消失；
- 会话详情、请求状态、请求重放、历史装配、记忆状态/重置在撤权后保留既有 403 契约；
- 反馈提交走当前成员 ACL；
- 项目源列表/源详情/原文/搜索对成员可见，撤权后 404，撤销源保留 410；
- 成员撤权后 check_lease 停止、最终发布不写成功 assistant；
- token 在发布锁复核中失效仍拒绝；
- 全程确定性替身，不调用真实模型。

破坏性操作前先过共享 owner 门（loopback / ``_test`` / owner 标签 / engine 一致 /
库 comment 精确匹配）；未配置时整类 skip 并说明未运行。
"""
import asyncio
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

if TEST_URL:
    from app.core.security import create_access_token
    from app.db.init_db import init_db
    from app.db.session import SessionLocal, engine
    from app.main import app
    from app.models import (
        Conversation,
        Document,
        DocumentChunk,
        KnowledgeBase,
        KnowledgeBaseMember,
        Message,
        ProjectChunk,
        ProjectFileVersion,
        ProjectSnapshot,
        ProjectSnapshotFile,
        ProjectSource,
        User,
    )
    from app.services import answer_history, chat_turns
    from app.services.answer_sources import SOURCE_SNAPSHOT_VERSION


TABLES = (
    "users, knowledge_bases, knowledge_base_members, documents, document_chunks, "
    "conversations, chat_turns, messages, answer_feedbacks, audit_logs, model_usage, "
    "project_sources, project_snapshots, project_file_versions, project_snapshot_files, "
    "project_chunks"
)
MANIFEST = "a" * 64
CONFIG = "b" * 64


def _empty_snapshot():
    return {"source_snapshot_version": SOURCE_SNAPSHOT_VERSION,
            "supplied_sources": [], "history_sources": []}


def _project_snapshot(supplied):
    return {"source_snapshot_version": SOURCE_SNAPSHOT_VERSION,
            "supplied_sources": list(supplied), "history_sources": []}


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离成员读取数据库测试")
class KbMemberReadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)
        with patch("app.core.logging.setup_logging"):
            from app.main import app as application
        cls.app = application
        cls.client = TestClient(application)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        with SessionLocal() as db:
            self.owner = User(username="acl_owner", role="user", is_active=True, password_hash="x")
            self.member = User(username="acl_member", role="user", is_active=True, password_hash="x")
            self.stranger = User(username="acl_stranger", role="user", is_active=True, password_hash="x")
            self.admin = User(username="acl_admin", role="admin", is_active=True, password_hash="x")
            self.disabled = User(username="acl_disabled", role="user", is_active=True, password_hash="x")
            db.add_all([self.owner, self.member, self.stranger, self.admin, self.disabled])
            db.flush()
            # 私有上传库 A / 私有上传库 B（同属 owner，成员只授权 A）。
            self.kb_a = KnowledgeBase(name="acl-a", collection_name="acla_" + uuid.uuid4().hex,
                                      owner_id=self.owner.id, is_public=False, kind="upload")
            self.kb_b = KnowledgeBase(name="acl-b", collection_name="aclb_" + uuid.uuid4().hex,
                                      owner_id=self.owner.id, is_public=False, kind="upload")
            self.public_kb = KnowledgeBase(name="acl-public", collection_name="aclpub_" + uuid.uuid4().hex,
                                           owner_id=self.owner.id, is_public=True, kind="upload")
            # 私有项目库 P（owner 拥有；member 以 reader 授权）。
            self.project_kb = KnowledgeBase(name="acl-project", collection_name="aclp_" + uuid.uuid4().hex,
                                            owner_id=self.owner.id, is_public=False, kind="project")
            db.add_all([self.kb_a, self.kb_b, self.public_kb, self.project_kb])
            db.flush()
            db.add(KnowledgeBaseMember(kb_id=self.kb_a.id, user_id=self.member.id, role="reader"))
            db.add(KnowledgeBaseMember(kb_id=self.project_kb.id, user_id=self.member.id, role="reader"))
            db.commit()
            self.owner_id, self.member_id = self.owner.id, self.member.id
            self.stranger_id, self.admin_id = self.stranger.id, self.admin.id
            self.disabled_id = self.disabled.id
            self.kb_a_id, self.kb_b_id = self.kb_a.id, self.kb_b.id
            self.public_kb_id, self.project_kb_id = self.public_kb.id, self.project_kb.id
        self.owner_headers = self.headers(self.owner_id, "user")
        self.member_headers = self.headers(self.member_id, "user")
        self.stranger_headers = self.headers(self.stranger_id, "user")
        self.admin_headers = self.headers(self.admin_id, "admin")
        with SessionLocal() as db:
            db.get(User, self.disabled_id).is_active = False
            db.commit()

    # ---------- helpers ----------

    def headers(self, user_id, role):
        return {"Authorization": "Bearer " + create_access_token(str(user_id), role)}

    def add_document(self, *, kb_id=None, embedded=True):
        with SessionLocal() as db:
            doc = Document(kb_id=kb_id or self.kb_a_id, filename="a.txt", file_ext=".txt",
                           file_size=1, stored_path=f"/tmp/{uuid.uuid4().hex}.txt",
                           status="ready", cancel_requested=False, index_version=0,
                           parse_done=True, chunk_count=1)
            db.add(doc)
            db.flush()
            db.add(DocumentChunk(document_id=doc.id, chunk_index=0, content="正文", embedded=embedded))
            db.commit()
            return doc.id

    def add_project(self, *, revoked=False, kb_id=None, relative_path="src/main.py"):
        with SessionLocal() as db:
            source = ProjectSource(kb_id=kb_id or self.project_kb_id, name="src",
                                   root_key="root", relative_dir="", root_fingerprint="f" * 64,
                                   created_by=self.owner_id)
            db.add(source)
            db.flush()
            snapshot = ProjectSnapshot(source_id=source.id, status="ready",
                                       manifest_fingerprint=MANIFEST, config_fingerprint=CONFIG)
            db.add(snapshot)
            db.flush()
            version = ProjectFileVersion(source_id=source.id, relative_path=relative_path,
                                         content_sha256="c" * 64,
                                         blob_path="/tmp/blob", parse_done=True,
                                         parse_status="ready", config_fingerprint=CONFIG)
            db.add(version)
            db.flush()
            db.add(ProjectSnapshotFile(snapshot_id=snapshot.id, relative_path=relative_path,
                                       file_version_id=version.id))
            db.add(ProjectChunk(file_version_id=version.id, chunk_index=0, content="代码",
                                embedded=True))
            if revoked:
                source.revoked_at = datetime.now(timezone.utc)
            else:
                source.active_snapshot_id = snapshot.id
            db.commit()
            return {"source_id": source.id, "snapshot_id": snapshot.id,
                    "file_version_id": version.id}

    def claim(self, *, user_id=None, kb_id="a", mode="rag", question="问题"):
        target = {"a": self.kb_a_id, "b": self.kb_b_id, "public": self.public_kb_id,
                  "project": self.project_kb_id}.get(kb_id, kb_id)
        with SessionLocal() as db:
            return chat_turns.claim_turn(
                db, user_id=user_id or self.member_id, conversation_id=None,
                kb_id=target if mode == "rag" else None, mode=mode, question=question, top_k=6,
                request_id=uuid.uuid4(), auth_token_version=0)

    def complete(self, reservation, answer="回答", trace=None, citations=()):
        with SessionLocal() as db:
            return chat_turns.complete_turn(db, reservation, answer, list(citations), 1,
                                            trace if trace is not None else _empty_snapshot())

    def revoke_member(self, kb_id=None, user_id=None):
        with SessionLocal() as db:
            db.execute(text("DELETE FROM knowledge_base_members WHERE kb_id = :kb AND user_id = :user"),
                       {"kb": kb_id or self.kb_a_id, "user": user_id or self.member_id})
            db.commit()

    def add_member(self, kb_id=None, user_id=None, role="reader"):
        with SessionLocal() as db:
            db.add(KnowledgeBaseMember(kb_id=kb_id or self.kb_a_id,
                                       user_id=user_id or self.member_id, role=role))
            db.commit()

    def add_conversation(self, *, user_id=None, kb_id=None, title="标题", mode="rag"):
        with SessionLocal() as db:
            conv = Conversation(user_id=user_id or self.member_id,
                                kb_id=(kb_id if kb_id is not None else self.kb_a_id) if mode == "rag" else None,
                                mode=mode, title=title)
            db.add(conv)
            db.commit()
            return conv.id

    def stream(self, user, payload, *, token_version=0, request=None,
               retrieve=None, answer_parts=("成员回答[1]。",), answer_side_effect=None):
        """确定性 SSE 调用；只替换检索与生成，不触发真实模型。"""
        from app.api.v1 import chat
        from app.schemas.chat import ChatRequest
        from app.services import rag

        req = request or Request({"type": "http", "method": "POST",
                                  "path": "/api/v1/chat/stream", "headers": []})
        req.state.auth_token_version = token_version

        async def generate(*_args, **_kwargs):
            if answer_side_effect is not None:
                for part in answer_side_effect():
                    yield part
                return
            for part in answer_parts:
                yield part

        async def consume(response):
            return [frame async for frame in response.body_iterator]

        with patch.object(rag, "retrieve", return_value=retrieve or []), \
             patch.object(rag, "answer_astream", side_effect=generate), \
             patch.object(rag, "prepare_answer", return_value=SimpleNamespace(
                 pairs=[], history_sources=[], messages=[], input_tokens=0, input_budget=0)), \
             patch.object(chat.settings, "RETRIEVAL_REWRITE_ENABLED", False):
            with SessionLocal() as db:
                user_row = db.get(User, user.id)
                response = chat.chat_stream(ChatRequest(**payload), req, db, user_row)
                frames = asyncio_run_consume(consume, response)
        events = []
        for frame in frames:
            event, data = frame.strip().split("\n", 1)
            events.append((event.removeprefix("event: "), __import__("json").loads(data.removeprefix("data: "))))
        return events

    # ---------- 上传库成员读取 ----------

    def test_private_upload_member_can_chat_while_stranger_is_denied(self):
        events = self.stream(self.member, {"kb_id": str(self.kb_a_id), "question": "问题", "mode": "rag"})
        self.assertEqual(events[-1][0], "done")
        with self.assertRaises(Exception) as caught:
            self.stream(self.stranger, {"kb_id": str(self.kb_a_id), "question": "问题", "mode": "rag"})
        self.assertEqual(getattr(caught.exception, "status_code", None), 403)

    def test_member_of_library_a_cannot_reach_private_library_b(self):
        with self.assertRaises(Exception) as caught:
            self.claim(kb_id="b")
        self.assertEqual(getattr(caught.exception, "status_code", None), 404)

    def test_public_owner_admin_are_preserved_and_disabled_member_is_rejected(self):
        # 公开库：普通成员可读。
        public = self.stream(self.member, {"kb_id": str(self.public_kb_id), "question": "问题"})
        self.assertEqual(public[-1][0], "done")
        # owner 可读自己的私有库。
        self.assertEqual(self.stream(self.owner, {"kb_id": str(self.kb_a_id), "question": "问题"})[-1][0], "done")
        # admin 可读任意私有库。
        self.assertEqual(self.stream(self.admin, {"kb_id": str(self.kb_a_id), "question": "问题"})[-1][0], "done")
        # disabled 即使曾显式授权也不能读（服务层按不可读 403 拒绝）。
        self.add_member(user_id=self.disabled_id)
        with self.assertRaises(Exception) as caught:
            self.stream(self.disabled, {"kb_id": str(self.kb_a_id), "question": "问题"})
        self.assertEqual(getattr(caught.exception, "status_code", None), 403)

    def test_reader_and_maintainer_both_read_but_only_maintainer_manages(self):
        from app.services import kb_access

        with SessionLocal() as db:
            kb = db.get(KnowledgeBase, self.kb_a_id)
            member = db.get(User, self.member_id)
            self.assertTrue(kb_access.can_read_kb(db, kb, member))
            self.assertFalse(kb_access.can_manage_documents(db, kb, member))
        # reader 升级 maintainer：读取权不变，维护权只给 maintainer。
        self.revoke_member()
        self.add_member(role="maintainer")
        with SessionLocal() as db:
            kb = db.get(KnowledgeBase, self.kb_a_id)
            member = db.get(User, self.member_id)
            self.assertTrue(kb_access.can_read_kb(db, kb, member))
            self.assertTrue(kb_access.can_manage_documents(db, kb, member))

    # ---------- 会话列表 / 分页：SQL 前过滤 ----------

    def test_conversation_list_hides_revoked_private_titles_before_total_and_limit(self):
        chat_conv = self.add_conversation(mode="chat", title="普通对话")
        private_conv = self.add_conversation(title="私有标题")
        public_conv = self.add_conversation(kb_id=self.public_kb_id, title="公开标题")
        # 填充 105 条更旧的公开会话，并让三条指定会话各自有明确更新的时间戳：
        # 同档 updated_at 只按 UUID 排序，公开会话会随机落出前 100，不能靠运气通过。
        # 分档（旧 -> 新）：105 填充 < public(公开标题) < private(私有标题) / chat(普通对话)。
        stamp = datetime(2026, 5, 1, tzinfo=timezone.utc)
        with SessionLocal() as db:
            db.add_all([Conversation(user_id=self.member_id, kb_id=self.public_kb_id,
                                     mode="rag", title=f"填充 {index:03d}")
                        for index in range(105)])
            db.commit()
            db.execute(text("UPDATE conversations SET updated_at = :stamp WHERE user_id = :user"),
                       {"stamp": stamp, "user": self.member_id})
            db.execute(text("UPDATE conversations SET updated_at = :stamp + interval '1 hour' "
                            "WHERE id = :id"),
                       {"stamp": stamp, "id": public_conv})
            db.execute(text("UPDATE conversations SET updated_at = :stamp + interval '2 hours' "
                            "WHERE id = ANY(:ids)"),
                       {"stamp": stamp, "ids": [private_conv, chat_conv]})
            db.commit()

        page = self.client.get("/api/v1/chat/conversations/page", headers=self.member_headers,
                               params={"page": 1, "page_size": 100})
        data = page.json()["data"]
        self.assertEqual(data["total"], 108, "total 必须先于分页过滤（105 填充 + chat + public + private）")
        ids = {item["id"] for item in data["items"]}
        self.assertIn(str(private_conv), ids)
        self.assertIn(str(chat_conv), ids)
        self.assertIn(str(public_conv), ids)

        self.revoke_member()
        page = self.client.get("/api/v1/chat/conversations/page", headers=self.member_headers,
                               params={"page": 1, "page_size": 100})
        data = page.json()["data"]
        self.assertEqual(data["total"], 107, "撤权后私有库会话必须从 total 消失")
        ids = {item["id"] for item in data["items"]}
        self.assertNotIn(str(private_conv), ids)
        self.assertIn(str(chat_conv), ids)
        self.assertIn(str(public_conv), ids)

        legacy = self.client.get("/api/v1/chat/conversations", headers=self.member_headers)
        legacy_ids = {item["id"] for item in legacy.json()["data"]}
        self.assertNotIn(str(private_conv), legacy_ids)

        # 过滤发生在 SQL 的 LIMIT 之前：第一页完整 100 条中不出现私有会话。
        page = self.client.get("/api/v1/chat/conversations/page", headers=self.member_headers,
                               params={"page": 1, "page_size": 100})
        self.assertEqual(len(page.json()["data"]["items"]), 100)
        self.assertNotIn(str(private_conv), {item["id"] for item in page.json()["data"]["items"]})

    def test_conversation_list_stays_owner_only_even_for_kb_members(self):
        self.add_conversation(user_id=self.owner_id, title="别人的私有会话")
        page = self.client.get("/api/v1/chat/conversations/page", headers=self.member_headers)
        self.assertEqual(page.json()["data"]["total"], 0)

    # ---------- 详情 / 请求状态 / 重放 / 历史 / 记忆 ----------

    def test_detail_and_request_state_hide_content_after_member_revocation(self):
        reservation = self.claim()
        self.complete(reservation, answer="成员可见的回答")
        detail_url = f"/api/v1/chat/conversations/{reservation.conversation_id}"
        state_url = f"/api/v1/chat/requests/{reservation.request_id}"
        self.assertEqual(self.client.get(detail_url, headers=self.member_headers).status_code, 200)
        state = self.client.get(state_url, headers=self.member_headers)
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json()["data"]["message"]["content"], "成员可见的回答")
        # 管理员原有读取语义保留。
        self.assertEqual(self.client.get(detail_url, headers=self.admin_headers).status_code, 200)

        self.revoke_member()
        hidden = self.client.get(detail_url, headers=self.member_headers)
        self.assertEqual(hidden.status_code, 403, hidden.text)
        self.assertNotIn("成员可见的回答", hidden.text)
        state = self.client.get(state_url, headers=self.member_headers)
        self.assertEqual(state.status_code, 403)
        self.assertNotIn("成员可见的回答", state.text)

    def test_memory_status_and_reset_are_hidden_after_member_revocation(self):
        chat_conv = self.add_conversation(mode="chat", title="记忆会话")
        rag_conv = self.add_conversation(title="RAG 记忆")
        self.assertEqual(
            self.client.get(f"/api/v1/chat/conversations/{chat_conv}/memory",
                            headers=self.member_headers).status_code, 200)
        self.assertEqual(
            self.client.get(f"/api/v1/chat/conversations/{rag_conv}/memory",
                            headers=self.member_headers).status_code, 200)
        self.revoke_member()
        self.assertEqual(
            self.client.get(f"/api/v1/chat/conversations/{rag_conv}/memory",
                            headers=self.member_headers).status_code, 403)
        reset = self.client.post(f"/api/v1/chat/conversations/{rag_conv}/memory/reset",
                                 headers=self.member_headers, json={"expected_version": 0})
        self.assertEqual(reset.status_code, 403)
        # 普通对话（无 KB）不受影响。
        self.assertEqual(
            self.client.get(f"/api/v1/chat/conversations/{chat_conv}/memory",
                            headers=self.member_headers).status_code, 200)

    def test_stream_replay_and_history_are_hidden_after_revocation(self):
        document_id = self.add_document()
        source = {"document_id": str(document_id), "index_version": 0, "chunk_index": 0,
                  "filename": "a.txt", "source_kind": "document"}
        events = self.stream(self.member, {"kb_id": str(self.kb_a_id), "question": "问题"},
                             retrieve=[(SimpleNamespace(page_content="正文", metadata=source), 0.1)])
        self.assertEqual(events[-1][0], "done", events)
        conv_id = events[0][1]["conversation_id"]
        request_id = events[0][1]["request_id"]

        def reservation_for():
            with SessionLocal() as db:
                turn = db.scalar(select(chat_turns.ChatTurn).where(
                    chat_turns.ChatTurn.request_id == uuid.UUID(request_id)))
                return chat_turns.Reservation(
                    turn.id, turn.request_id, turn.conversation_id, turn.user_message_id,
                    turn.user_id, scope_revision=0, auth_token_version=0, mode="rag",
                    kb_id=self.kb_a_id)

        # 会话必须显式 with 关闭：无人认领的 open 事务会靠 GC 回收，
        # 而被它挡住的 TRUNCATE 会让主线程阻塞、永远等不到下一次 GC。
        with SessionLocal() as db:
            self.assertTrue(answer_history.load_rag_history(db, reservation_for()))

        self.revoke_member()
        # 请求状态（同 URL）与重放都不能借旧请求读取结果。
        state = self.client.get(f"/api/v1/chat/requests/{request_id}", headers=self.member_headers)
        self.assertEqual(state.status_code, 403)
        with self.assertRaises(Exception) as caught:
            self.stream(self.member, {"kb_id": str(self.kb_a_id), "question": "问题",
                                      "request_id": request_id})
        self.assertEqual(getattr(caught.exception, "status_code", None), 403)
        with SessionLocal() as db:
            with self.assertRaises(chat_turns.ChatLeaseLost):
                answer_history.load_rag_history(db, reservation_for())
        # 续聊也不能借旧会话访问旧内容。
        with self.assertRaises(Exception) as caught:
            self.stream(self.member, {"kb_id": str(self.kb_a_id), "question": "继续",
                                      "conversation_id": conv_id})
        self.assertEqual(getattr(caught.exception, "status_code", None), 403)

    # ---------- 成员撤权与发布门禁 ----------

    def test_check_lease_stops_after_member_revocation(self):
        reservation = self.claim()
        self.assertTrue(chat_turns.check_lease(reservation, renew=True))
        self.revoke_member()
        self.assertFalse(chat_turns.check_lease(reservation, renew=False))
        with SessionLocal() as db:
            chat_turns.cancel_request(db, reservation.request_id, self.member_id)

    def test_member_revoked_while_model_waits_stops_stream_with_lease_lost(self):
        """真实 chat_stream → stream_events 链路：模型等待中撤权必须终断整条流。

        模型 async generator 真正 ``await`` 一个 Event 后通知主线程；主线程用
        真实 PG 事务删除该私有库成员并 commit。不 mock check_lease/complete_turn：
        真实 ``watch_lease`` 必须有界结束流——终态 ``lease_lost``、无 ``done``、
        无成功 assistant 消息，且模型 generator 被取消并执行收尾。
        """
        from app.api.v1 import chat
        from app.schemas.chat import ChatRequest
        from app.services import rag

        req = Request({"type": "http", "method": "POST",
                       "path": "/api/v1/chat/stream", "headers": []})
        req.state.auth_token_version = 0
        model_started, model_closed = Event(), Event()
        bridge = {}

        async def generate(*_args, **_kwargs):
            loop = asyncio.get_running_loop()
            gate = asyncio.Event()
            bridge["loop"], bridge["gate"] = loop, gate
            model_started.set()
            try:
                await gate.wait()
            finally:
                # 流被 lease_lost 取消时也必须走到这里（generator 收尾证据）。
                model_closed.set()
            yield "这段正文不应被发布"

        async def consume(response):
            return [frame async for frame in response.body_iterator]

        def run_consumer():
            with patch.object(rag, "retrieve", return_value=[]), \
                 patch.object(rag, "answer_astream", side_effect=generate), \
                 patch.object(rag, "prepare_answer", return_value=SimpleNamespace(
                     pairs=[], history_sources=[], messages=[], input_tokens=0, input_budget=0)), \
                 patch.object(chat.settings, "RETRIEVAL_REWRITE_ENABLED", False), \
                 patch.object(chat.settings, "CHAT_STREAM_CANCEL_POLL_SECONDS", 0.1), \
                 patch.object(chat.settings, "CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS", 20.0), \
                 patch.object(chat.settings, "CHAT_STREAM_TOTAL_TIMEOUT_SECONDS", 25.0):
                # chat_stream 返回响应前已完成本请求的 DB 工作并 commit；
                # 立刻关闭会话，等模型期间不占用任何数据库连接。
                with SessionLocal() as db:
                    user_row = db.get(User, self.member_id)
                    response = chat.chat_stream(
                        ChatRequest(kb_id=str(self.kb_a_id), question="问题", mode="rag"),
                        req, db, user_row)
                return asyncio_run_consume(consume, response)

        future = None
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(run_consumer)
            self.assertTrue(model_started.wait(10), "模型 generator 必须真正进入等待")
            # 持有根锁序无关的真实事务：只 DELETE 成员行后 commit（不碰 User/KB 行锁）。
            self.revoke_member()
            frames = future.result(timeout=20)
        finally:
            if future is not None and not future.done() and "gate" in bridge:
                bridge["loop"].call_soon_threadsafe(bridge["gate"].set)
            pool.shutdown(wait=True, cancel_futures=True)

        events = []
        for frame in frames:
            event, data = frame.strip().split("\n", 1)
            events.append((event.removeprefix("event: "),
                           __import__("json").loads(data.removeprefix("data: "))))
        names = [name for name, _ in events]
        self.assertNotIn("done", names, events)
        self.assertEqual(events[-1][0], "error", events)
        self.assertEqual(events[-1][1]["code"], "lease_lost", events)
        self.assertTrue(model_closed.is_set(), "模型 generator 必须被取消并收尾")

        request_id = next(data["request_id"] for name, data in events if name == "meta")
        with SessionLocal() as db:
            turn = db.scalar(select(chat_turns.ChatTurn).where(
                chat_turns.ChatTurn.request_id == uuid.UUID(request_id)))
            self.assertIsNotNone(turn)
            self.assertEqual(turn.status, "cancelled")
            self.assertIsNone(db.scalar(select(Message).where(
                Message.turn_id == turn.id, Message.role == "assistant")))

    def test_publication_denied_after_member_revocation(self):
        reservation = self.claim()
        self.revoke_member()
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation)
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant")))
            self.assertEqual(db.get(chat_turns.ChatTurn, reservation.turn_id).status, "processing")
            chat_turns.cancel_request(db, reservation.request_id, self.member_id)

    def test_token_revoked_before_publication_is_rejected(self):
        reservation = self.claim()
        # 模拟发布前 token 被刷新：锁定后的 User 版本与认证快照不再一致。
        with SessionLocal() as db:
            db.get(User, self.member_id).token_version += 1
            db.commit()
        self.assertFalse(chat_turns.check_lease(reservation, renew=False))
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation)
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant")))
            chat_turns.cancel_request(db, reservation.request_id, self.member_id)

    def test_member_revoked_while_publication_waits_for_lock_never_publishes(self):
        """真实 PG 锁等待：holder 先锁 User 行，等待期间撤销成员，发布必须失败。"""
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event

        from tests._pg_lock_wait import wait_for_blocking

        reservation = self.claim()
        armed, actor_ready, release = Event(), Event(), Event()
        pids = {}
        pool = ThreadPoolExecutor(max_workers=2)
        actor = None

        def holder():
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["holder"] = db.scalar(text("SELECT pg_backend_pid()"))
                db.scalar(select(User.id).where(User.id == self.member_id).with_for_update())
                armed.set()
                self.assertTrue(release.wait(20), "测试必须有界释放持锁事务")
                db.commit()

        def action():
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                pids["actor"] = db.scalar(text("SELECT pg_backend_pid()"))
                actor_ready.set()
                try:
                    # 必须使用本 action 自己的连接发布，才能观察到它等待 User 行锁。
                    return chat_turns.complete_turn(db, reservation, "回答", [], 1,
                                                    _empty_snapshot())
                except chat_turns.ChatLeaseLost as exc:
                    db.rollback()
                    return exc

        held = pool.submit(holder)
        try:
            self.assertTrue(armed.wait(8), "holder 未就绪")
            actor = pool.submit(action)
            self.assertTrue(actor_ready.wait(8), "actor 未就绪")
            self.assertTrue(wait_for_blocking(engine, pids["holder"], pids["actor"]),
                            "必须观察到本次两个 PID 的真实阻塞关系")
            self.assertFalse(actor.done())
            # actor 已被 User 行锁阻塞：此刻在另一连接撤销成员授权。
            self.revoke_member()
        finally:
            release.set()
            try:
                held.result(timeout=20)
                if actor is not None:
                    actor.result(timeout=20)
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        self.assertIsInstance(actor.result(), chat_turns.ChatLeaseLost)
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant")))
            self.assertEqual(db.get(chat_turns.ChatTurn, reservation.turn_id).status, "processing")
            chat_turns.cancel_request(db, reservation.request_id, self.member_id)

    # ---------- 项目库 ----------

    def test_project_member_can_read_sources_preview_and_search(self):
        project = self.add_project()
        response = self.client.get("/api/v1/projects", headers=self.member_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([item["id"] for item in response.json()["data"]],
                         [str(project["source_id"])])

        detail = self.client.get(f"/api/v1/projects/{project['source_id']}",
                                 headers=self.member_headers)
        self.assertEqual(detail.status_code, 200, detail.text)

        preview = self.client.get(
            f"/api/v1/projects/{project['source_id']}/files/{project['file_version_id']}/source",
            headers=self.member_headers, params={"snapshot_id": str(project["snapshot_id"])})
        self.assertEqual(preview.status_code, 200, preview.text)
        # JSON 转义后中文以 \uXXXX 出现，直接断言 API 字段。
        self.assertEqual(preview.json()["data"]["chunks"][0]["content"], "代码")

        search = self.client.get(f"/api/v1/projects/{project['source_id']}/search",
                                 headers=self.member_headers,
                                 params={"query": "代码", "snapshot_id": str(project["snapshot_id"])})
        self.assertEqual(search.status_code, 200, search.text)

        # stranger 对私有项目库不可见（列表为空、直接访问 404）。
        stranger = self.client.get("/api/v1/projects", headers=self.stranger_headers)
        self.assertEqual(stranger.json()["data"], [])
        denied = self.client.get(f"/api/v1/projects/{project['source_id']}",
                                 headers=self.stranger_headers)
        self.assertEqual(denied.status_code, 404)

    def test_project_source_revoked_still_returns_410_to_authorized_member(self):
        project = self.add_project(revoked=True)
        denied = self.client.get(f"/api/v1/projects/{project['source_id']}",
                                 headers=self.member_headers)
        self.assertEqual(denied.status_code, 410)

    def test_project_member_revocation_hides_list_and_source(self):
        project = self.add_project()
        self.revoke_member(kb_id=self.project_kb_id)
        listing = self.client.get("/api/v1/projects", headers=self.member_headers)
        self.assertEqual(listing.status_code, 200)
        self.assertNotIn(str(project["source_id"]),
                         [item["id"] for item in listing.json()["data"]])
        detail = self.client.get(f"/api/v1/projects/{project['source_id']}",
                                 headers=self.member_headers)
        self.assertEqual(detail.status_code, 404)
        preview = self.client.get(
            f"/api/v1/projects/{project['source_id']}/files/{project['file_version_id']}/source",
            headers=self.member_headers, params={"snapshot_id": str(project["snapshot_id"])})
        self.assertEqual(preview.status_code, 404)

    def test_project_member_can_publish_on_fixed_snapshot_but_not_manage_sources(self):
        project = self.add_project()
        reservation = self.claim(kb_id="project")
        self.assertEqual(reservation.project_snapshot_id, project["snapshot_id"])
        citation = {
            "source_kind": "project", "document_id": str(project["file_version_id"]),
            "file_version_id": str(project["file_version_id"]),
            "snapshot_id": str(project["snapshot_id"]), "source_id": str(project["source_id"]),
            "relative_path": "src/main.py", "manifest_fingerprint": MANIFEST,
            "config_fingerprint": CONFIG, "chunk_index": 0,
        }
        self.complete(reservation, answer="项目成员回答[1]。",
                      trace=_project_snapshot([citation]), citations=[{**citation, "source_index": 1}])
        with SessionLocal() as db:
            answer = db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant"))
            self.assertIsNotNone(answer)
            self.assertEqual(answer.content, "项目成员回答[1]。")
        # 成员不能管理源配置（写接口仍要求 admin）。
        revoked = self.client.delete(f"/api/v1/projects/{project['source_id']}",
                                     headers=self.member_headers)
        self.assertEqual(revoked.status_code, 403)

    def test_project_member_revocation_stops_publication(self):
        self.add_project()
        reservation = self.claim(kb_id="project")
        self.revoke_member(kb_id=self.project_kb_id)
        with self.assertRaises(chat_turns.ChatLeaseLost):
            self.complete(reservation)
        with SessionLocal() as db:
            self.assertIsNone(db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant")))
            chat_turns.cancel_request(db, reservation.request_id, self.member_id)

    # ---------- 反馈 ----------

    def test_feedback_submission_uses_current_member_acl(self):
        reservation = self.claim()
        self.complete(reservation, answer="反馈目标回答")
        with SessionLocal() as db:
            answer = db.scalar(select(Message).where(
                Message.turn_id == reservation.turn_id, Message.role == "assistant"))
            answer_id = answer.id
        body = {"message_id": str(answer_id), "category": "inaccurate_answer", "note": ""}
        self.assertEqual(self.client.post("/api/v1/feedback", headers=self.member_headers,
                                          json=body).status_code, 200)
        # 撤权后不能借旧回答继续提交反馈（既有 403 契约）。
        self.revoke_member()
        with SessionLocal() as db:
            db.execute(text("DELETE FROM answer_feedbacks WHERE message_id = :id"),
                       {"id": answer_id})
            db.commit()
        self.assertEqual(self.client.post("/api/v1/feedback", headers=self.member_headers,
                                          json=body).status_code, 403)


def asyncio_run_consume(consume, response):
    import asyncio

    return asyncio.run(consume(response))


if __name__ == "__main__":
    unittest.main()
