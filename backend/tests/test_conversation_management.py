"""会话分页/搜索/元数据 PATCH 的真实 PostgreSQL 回归。

覆盖：
- 超过 100 条时 `/conversations/page` 仍能翻到全部；旧 `/conversations` 保持数组兼容；
- 与当前用户无关的会话不进入 items/total；
- 相同 pinned_at/updated_at 下按 id 稳定排序；
- q 为字面匹配（trim、% _ \ 不作为通配符）；
- PATCH 仅会话本人可改（管理员也不行）、字段严格校验、updated_at 与租约保持。
"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone

from tests._env import TEST_URL
from tests.test_database import _bind_test_engine

_bind_test_engine()

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.core.security import create_access_token
from app.db.init_db import init_db
from app.db.session import SessionLocal, engine
from app.models import Conversation, KnowledgeBase, KnowledgeBaseMember, User



@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离会话数据库测试")
class ConversationManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL) or not engine.url.database.endswith("_test"):
            raise RuntimeError("拒绝在业务库运行会话管理回归")
        init_db(create_admin=False)
        from app.main import app

        cls.app = app

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        with engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, conversations, chat_turns, messages, audit_logs "
                "RESTART IDENTITY CASCADE"
            ))
        with SessionLocal() as db:
            self.user = User(username="conv_owner", role="user", is_active=True, password_hash="x")
            self.other = User(username="conv_other", role="user", is_active=True, password_hash="x")
            self.admin = User(username="conv_admin", role="admin", is_active=True, password_hash="x")
            db.add_all([self.user, self.other, self.admin])
            db.flush()
            self.kb = KnowledgeBase(name="conv-kb", collection_name="c_" + uuid.uuid4().hex,
                                    owner_id=self.other.id, is_public=True)
            self.private_kb = KnowledgeBase(name="conv-private", collection_name="p_" + uuid.uuid4().hex,
                                            owner_id=self.other.id, is_public=False)
            db.add_all([self.kb, self.private_kb])
            db.commit()
            self.user_id, self.other_id, self.admin_id = self.user.id, self.other.id, self.admin.id
            self.kb_id, self.private_kb_id = self.kb.id, self.private_kb.id
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer " + create_access_token(str(self.user_id), "user")}
        self.other_headers = {"Authorization": "Bearer " + create_access_token(str(self.other_id), "user")}
        self.admin_headers = {"Authorization": "Bearer " + create_access_token(str(self.admin_id), "admin")}

    # ---------- helpers ----------

    def add(self, *, user_id=None, title="会话", mode="rag", kb_id="default", pinned_at=None,
            updated_at=None, scope_revision=0, document_scope=None):
        with SessionLocal() as db:
            conv = Conversation(
                user_id=user_id or self.user_id,
                mode=mode,
                kb_id=(self.kb_id if kb_id == "default" else kb_id),
                title=title,
                pinned_at=pinned_at,
                scope_revision=scope_revision,
                document_scope=document_scope or [],
            )
            db.add(conv)
            db.commit()
            conv_id = conv.id
        if updated_at is not None:
            # 绕过 TimestampMixin 的 onupdate，直接写死排序用的时间。
            with engine.begin() as conn:
                conn.execute(text("UPDATE conversations SET updated_at = :t WHERE id = :id"),
                             {"t": updated_at, "id": conv_id})
        return conv_id

    def page(self, headers=None, **params):
        return self.client.get("/api/v1/chat/conversations/page", headers=headers or self.headers, params=params)

    def patch(self, conv_id, body, headers=None):
        return self.client.patch(f"/api/v1/chat/conversations/{conv_id}", headers=headers or self.headers, json=body)

    # ---------- 分页 / total ----------

    def test_pagination_covers_more_than_the_legacy_hundred_item_cap(self):
        ids = [self.add(title=f"会话 {i:03d}") for i in range(105)]
        first = self.page(page=1, page_size=100)
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()["data"]
        self.assertEqual(body["total"], 105)
        self.assertEqual(len(body["items"]), 100)

        second = self.page(page=2, page_size=100)
        self.assertEqual(second.status_code, 200, second.text)
        body2 = second.json()["data"]
        self.assertEqual(body2["total"], 105)
        self.assertEqual(len(body2["items"]), 5)

        seen = {item["id"] for item in body["items"]} | {item["id"] for item in body2["items"]}
        self.assertEqual(seen, {str(i) for i in ids}, "两页合起来必须覆盖全部会话，不能有重复或遗漏")

        # 旧接口保持数组响应且最多 100 条：兼容性不因新接口改变
        legacy = self.client.get("/api/v1/chat/conversations", headers=self.headers)
        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertIsInstance(legacy.json()["data"], list)
        self.assertEqual(len(legacy.json()["data"]), 100)

    def test_page_and_page_size_bounds_are_enforced(self):
        self.assertEqual(self.page(page=0).status_code, 422)
        self.assertEqual(self.page(page_size=0).status_code, 422)
        self.assertEqual(self.page(page_size=101).status_code, 422)
        self.assertEqual(self.page(page_size=100).status_code, 200)
        # 超出范围的页码返回空 items，但 total 仍然是真实总数
        self.add()
        out = self.page(page=99, page_size=20).json()["data"]
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["items"], [])

    def test_total_and_items_are_scoped_to_the_current_user(self):
        mine = self.add(title="我的会话")
        self.add(title="别人的会话", user_id=self.other_id)
        out = self.page().json()["data"]
        self.assertEqual(out["total"], 1, "total 必须是当前用户口径，不能算上别人的会话")
        self.assertEqual([item["id"] for item in out["items"]], [str(mine)])

        # 另一个用户只看到自己的
        other = self.page(headers=self.other_headers).json()["data"]
        self.assertEqual(other["total"], 1)
        self.assertNotIn(str(mine), [item["id"] for item in other["items"]])

    def test_filters_keep_kb_and_mode_semantics(self):
        rag_kb = self.add(title="A", mode="rag", kb_id=self.kb_id)
        rag_other = self.add(title="B", mode="rag", kb_id=self.private_kb_id)
        chat = self.add(title="C", mode="chat", kb_id=None)
        self.add(title="D", mode="rag", kb_id=self.kb_id, user_id=self.other_id)
        # 私有库成员授权后才可见：列表 SQL 在 total/分页前按当前成员 EXISTS 过滤。
        with SessionLocal() as db:
            db.add(KnowledgeBaseMember(kb_id=self.private_kb_id, user_id=self.user_id, role="reader"))
            db.commit()

        by_kb = self.page(kb_id=str(self.kb_id)).json()["data"]
        self.assertEqual(by_kb["total"], 1)
        self.assertEqual([item["id"] for item in by_kb["items"]], [str(rag_kb)])

        by_mode = self.page(mode="chat").json()["data"]
        self.assertEqual(by_mode["total"], 1)
        self.assertEqual([item["id"] for item in by_mode["items"]], [str(chat)])

        combined = self.page(kb_id=str(self.private_kb_id), mode="rag").json()["data"]
        self.assertEqual([item["id"] for item in combined["items"]], [str(rag_other)])

        both = self.page(mode="rag").json()["data"]
        self.assertEqual(both["total"], 2)

    # ---------- 排序 ----------

    def test_ordering_is_pinned_then_updated_then_id_and_stable_on_ties(self):
        stamp = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        a = self.add(title="A", updated_at=stamp)
        b = self.add(title="B", updated_at=stamp)
        c = self.add(title="C", updated_at=stamp - timedelta(minutes=1))
        pinned_old = self.add(title="P1", updated_at=stamp - timedelta(days=2),
                              pinned_at=stamp - timedelta(days=3))
        pinned_new = self.add(title="P2", updated_at=stamp - timedelta(days=3),
                              pinned_at=stamp - timedelta(days=1))

        ids = [item["id"] for item in self.page(page_size=100).json()["data"]["items"]]
        # 置顶在前（按 pinned_at 倒序），未置顶在后（按 updated_at 倒序，同刻按 id 倒序）
        expected_tail = sorted([str(a), str(b)], reverse=True) + [str(c)]
        self.assertEqual(ids, [str(pinned_new), str(pinned_old)] + expected_tail)

    def test_identical_pinned_timestamps_fall_back_to_updated_at_not_arbitrary_order(self):
        stamp = datetime(2026, 3, 3, 9, 0, tzinfo=timezone.utc)
        newest = self.add(title="new", pinned_at=stamp, updated_at=stamp)
        older = self.add(title="old", pinned_at=stamp, updated_at=stamp - timedelta(hours=1))
        for _ in range(3):
            ids = [item["id"] for item in self.page(page_size=10).json()["data"]["items"]]
            self.assertEqual(ids, [str(newest), str(older)])

    # ---------- q 字面搜索 ----------

    def test_q_is_literal_and_handles_wildcards_backslash_and_slash(self):
        percent = self.add(title="季度增长率 100% 复盘")
        underscore = self.add(title="snake_case 命名规范")
        backslash = self.add(title="路径 C:\\temp 说明")
        slash = self.add(title="A/B 测试")
        plain = self.add(title="季度增长率 1000 复盘")

        def titles(**params):
            out = self.page(page_size=100, **params).json()["data"]
            return {item["id"] for item in out["items"]}, out["total"]

        found, total = titles(q="100%")
        self.assertEqual(found, {str(percent)}, "% 必须是字面量，不能匹配 1000")
        self.assertEqual(total, 1)

        found, _ = titles(q="1000")
        self.assertEqual(found, {str(plain)})

        found, _ = titles(q="snake_case")
        self.assertEqual(found, {str(underscore)}, "_ 不能当成单字符通配符")

        found, _ = titles(q="C:\\temp")
        self.assertEqual(found, {str(backslash)}, "反斜杠是字面量，且不会被转义字符吃掉")

        found, _ = titles(q="A/B")
        self.assertEqual(found, {str(slash)}, "转义符 / 本身也要按字面匹配")

        # trim 后再匹配，大小写不敏感
        found, _ = titles(q="  SNAKE_CASE  ")
        self.assertEqual(found, {str(underscore)})

        # 纯空白等于没有搜索
        self.assertEqual(titles(q="   ")[1], 5)

        # 没有任何匹配时 total 为 0
        self.assertEqual(titles(q="不存在的标题")[1], 0)

    def test_q_longer_than_two_hundred_characters_is_rejected(self):
        self.add()
        self.assertEqual(self.page(q="x" * 201).status_code, 422)
        self.assertEqual(self.page(q="x" * 200).status_code, 200)

    def test_q_search_combines_with_kb_and_mode_filters(self):
        hit = self.add(title="季度复盘", mode="rag", kb_id=self.kb_id)
        self.add(title="季度复盘", mode="chat", kb_id=None)
        self.add(title="季度复盘", mode="rag", kb_id=self.private_kb_id)
        out = self.page(q="复盘", mode="rag", kb_id=str(self.kb_id)).json()["data"]
        self.assertEqual(out["total"], 1)
        self.assertEqual([item["id"] for item in out["items"]], [str(hit)])

    # ---------- PATCH 权限 ----------

    def test_owner_can_rename_and_unpin_their_own_conversation(self):
        conv = self.add(title="旧标题")
        resp = self.patch(conv, {"title": "  新标题  "})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()["data"]
        self.assertEqual(data["title"], "新标题")
        self.assertIsNone(data["pinned_at"])

        pinned = self.patch(conv, {"pinned": True})
        self.assertEqual(pinned.status_code, 200, pinned.text)
        self.assertIsNotNone(pinned.json()["data"]["pinned_at"])

        unpinned = self.patch(conv, {"pinned": False})
        self.assertEqual(unpinned.status_code, 200, unpinned.text)
        self.assertIsNone(unpinned.json()["data"]["pinned_at"])

    def test_other_users_and_admins_cannot_modify_someone_elses_conversation(self):
        conv = self.add(title="受保护")
        self.assertEqual(self.patch(conv, {"title": "被改"}, headers=self.other_headers).status_code, 404)
        # 管理员可以读别人的会话，但元数据接口只认本人
        self.assertEqual(self.patch(conv, {"title": "管理员改"}, headers=self.admin_headers).status_code, 404)
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv).title, "受保护")

        # 管理员读权限本身不受影响
        self.assertEqual(self.client.get(f"/api/v1/chat/conversations/{conv}",
                                         headers=self.admin_headers).status_code, 200)

    def test_unknown_conversation_is_not_distinguishable_from_forbidden(self):
        resp = self.patch(uuid.uuid4(), {"title": "x"})
        self.assertEqual(resp.status_code, 404)

    def test_rag_conversation_rechecks_knowledge_base_permission(self):
        conv = self.add(title="私有库会话", kb_id=self.private_kb_id)
        # 会话属于本人，但知识库是别人的私有库且无成员授权：保留既有 403 契约。
        self.assertEqual(self.patch(conv, {"title": "改不了"}).status_code, 403)
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv).title, "私有库会话")
        # 成员授权后可以操作；撤销后立即恢复 403，且不改动标题。
        with SessionLocal() as db:
            db.add(KnowledgeBaseMember(kb_id=self.private_kb_id, user_id=self.user_id, role="reader"))
            db.commit()
        self.assertEqual(self.patch(conv, {"title": "成员可改"}).status_code, 200)
        with SessionLocal() as db:
            db.execute(text("DELETE FROM knowledge_base_members WHERE kb_id = :kb AND user_id = :user"),
                       {"kb": self.private_kb_id, "user": self.user_id})
            db.commit()
        self.assertEqual(self.patch(conv, {"title": "撤权后不可见"}).status_code, 403)
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv).title, "成员可改")

    # ---------- PATCH 字段校验 ----------

    def test_invalid_bodies_are_rejected_without_touching_the_row(self):
        conv = self.add(title="原样")
        cases = [
            ({}, "空对象"),
            ({"pinned": None}, "显式 null 的 pinned"),
            ({"title": None}, "显式 null 的 title"),
            ({"title": "   "}, "纯空白标题"),
            ({"title": ""}, "空标题"),
            ({"title": "x" * 201}, "超长标题"),
            ({"title": 123}, "非字符串标题"),
            ({"pinned": 1}, "整数 1 不是布尔"),
            ({"pinned": 0}, "整数 0 不是布尔"),
            ({"pinned": "true"}, "字符串不是布尔"),
            ({"title": "ok", "mode": "chat"}, "非法状态字段"),
            ({"document_scope": []}, "范围不能走元数据接口"),
            ({"kb_id": str(self.kb_id)}, "知识库不能走元数据接口"),
            ({"active_turn_id": str(uuid.uuid4())}, "租约不能走元数据接口"),
        ]
        for body, label in cases:
            with self.subTest(label=label):
                resp = self.patch(conv, body)
                self.assertEqual(resp.status_code, 422, f"{label}: {resp.text}")
        with SessionLocal() as db:
            row = db.get(Conversation, conv)
            self.assertEqual(row.title, "原样")
            self.assertIsNone(row.pinned_at)

    def test_title_boundary_values_are_strict(self):
        conv = self.add(title="边界")
        self.assertEqual(self.patch(conv, {"title": "x"}).status_code, 200)
        self.assertEqual(self.patch(conv, {"title": "中" * 200}).status_code, 200)
        self.assertEqual(self.patch(conv, {"title": "中" * 199 + "x"}).status_code, 200)
        self.assertEqual(self.patch(conv, {"title": "中" * 201}).status_code, 422)

    def test_title_and_pinned_can_be_updated_together(self):
        conv = self.add(title="一起改")
        resp = self.patch(conv, {"title": "新名字", "pinned": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()["data"]
        self.assertEqual(data["title"], "新名字")
        self.assertIsNotNone(data["pinned_at"])

    # ---------- 不改变的字段 ----------

    def test_metadata_update_preserves_updated_at_mode_scope_and_lease(self):
        stamp = datetime(2026, 2, 2, 8, 0, tzinfo=timezone.utc)
        scope = [{"document_id": str(uuid.uuid4()), "filename": "a.pdf", "index_version": 2}]
        conv = self.add(title="保留", updated_at=stamp, scope_revision=7, document_scope=scope)
        turn_id = uuid.uuid4()
        lease = stamp + timedelta(minutes=5)
        with engine.begin() as conn:
            conn.execute(text("UPDATE conversations SET active_turn_id = :t, lease_expires_at = :e WHERE id = :id"),
                         {"t": turn_id, "e": lease, "id": conv})

        resp = self.patch(conv, {"title": "只改标题", "pinned": True})
        self.assertEqual(resp.status_code, 200, resp.text)

        with SessionLocal() as db:
            row = db.get(Conversation, conv, populate_existing=True)
            self.assertEqual(row.title, "只改标题")
            self.assertIsNotNone(row.pinned_at)
            self.assertEqual(row.updated_at, stamp, "元数据变更不能刷新 updated_at（否则会打乱排序）")
            self.assertEqual(row.mode, "rag")
            self.assertEqual(row.kb_id, self.kb_id)
            self.assertEqual(row.scope_revision, 7)
            self.assertEqual(row.document_scope, scope)
            self.assertEqual(row.active_turn_id, turn_id, "租约不能被元数据接口清掉")
            self.assertEqual(row.lease_expires_at, lease)

    def test_repinning_keeps_the_original_pinned_at_so_order_does_not_drift(self):
        first_pin = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
        conv = self.add(title="置顶", pinned_at=first_pin)
        resp = self.patch(conv, {"pinned": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv, populate_existing=True).pinned_at, first_pin)

    def test_metadata_write_is_audited_but_audit_failure_does_not_undo_it(self):
        conv = self.add(title="审计")
        resp = self.patch(conv, {"title": "有审计"})
        self.assertEqual(resp.status_code, 200, resp.text)
        with engine.begin() as conn:
            count = conn.execute(text(
                "SELECT COUNT(*) FROM audit_logs WHERE action = 'conversation_metadata'")).scalar_one()
        self.assertGreaterEqual(count, 1)
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv).title, "有审计")

    def test_audit_failure_still_returns_success(self):
        conv = self.add(title="审计失败")
        from unittest.mock import patch as mock_patch

        with mock_patch("app.api.v1.chat.write_audit", side_effect=RuntimeError("audit down")):
            resp = self.patch(conv, {"title": "仍然成功"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(resp.json()["data"]["title"], "仍然成功")
        with SessionLocal() as db:
            self.assertEqual(db.get(Conversation, conv).title, "仍然成功")

    # ---------- 列表与元数据的一致性 ----------

    def test_renamed_conversation_appears_under_the_new_title_search(self):
        conv = self.add(title="旧标题 ABC")
        self.patch(conv, {"title": "新标题 XYZ"})
        self.assertEqual(self.page(q="XYZ").json()["data"]["total"], 1)
        self.assertEqual(self.page(q="ABC").json()["data"]["total"], 0)

    def test_pinned_conversation_sorts_first_after_patch(self):
        stamp = datetime(2026, 4, 4, 4, 0, tzinfo=timezone.utc)
        older = self.add(title="旧", updated_at=stamp - timedelta(days=2))
        newer = self.add(title="新", updated_at=stamp)
        self.assertEqual([i["id"] for i in self.page(page_size=10).json()["data"]["items"]],
                         [str(newer), str(older)])
        self.patch(older, {"pinned": True})
        self.assertEqual([i["id"] for i in self.page(page_size=10).json()["data"]["items"]][0], str(older))


if __name__ == "__main__":
    unittest.main()
