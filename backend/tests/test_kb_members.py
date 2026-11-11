"""A1 知识库成员 CRUD：真实 FastAPI + 真实 JWT + 真实 PostgreSQL。

覆盖冻结 HTTP 契约与安全不变量：

- 仅管理员可读写成员；普通用户 403、缺失令牌 401、旧令牌 401、禁用管理员 403、
  依赖被绕过（无 token 版本快照）401（fail closed）。
- GET 分页：page>=1、1<=page_size<=100；revision/total/items 同一快照；按
  username（再按 UUID）稳定排序。
- PUT/DELETE：严格 role 与严格非负 expected_revision；CAS 冲突 409；无变化
  幂等且仍核对 expected_revision（只核对、不递增）；PUT 目标不存在 404、
  目标禁用 400；DELETE 无成员幂等，即使目标用户不存在也不扩大清理。
- 并发：两个请求使用同一 expected_revision 时恰好一个成功、一个 409；管理员
  在请求等待 User 锁期间被撤销令牌 -> 401（锁后复核原始快照，不重解 JWT）。
- 审计：只在真实变更时写入，target/detail 只含稳定 KB/User ID、角色与 revision。

门禁沿用 ``tests._memory_project_test_gate``；所有破坏性操作前都重新 verify。
"""
import json
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from app.api.deps import bearer_scheme, get_current_user
from app.api.v1 import kb_members as kb_members_api
from app.core.security import create_access_token
from app.services import kb_members as kb_members_service

if TEST_URL:
    from app.db import session as db_session
    from app.db.init_db import init_db
    from app.models import AuditLog, KnowledgeBase, KnowledgeBaseMember, User

    SessionLocal, engine = db_session.SessionLocal, db_session.engine

WAIT_TIMEOUT = 25.0


def _request(version=0):
    return SimpleNamespace(
        client=None, headers={},
        state=SimpleNamespace(auth_token_version=version),
    )


@unittest.skipIf(
    gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
    "需要显式 owned PostgreSQL 测试库",
)
class KbMembersHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        init_db(create_admin=False)
        from fastapi import FastAPI

        cls.app = FastAPI()
        cls.app.include_router(kb_members_api.router, prefix="/api/v1")

        def _session_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        from app.db.session import get_db

        cls.app.dependency_overrides[get_db] = _session_override
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.engine = db_session.engine
        cls.engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        self.engine = db_session.engine
        self.created_users: list[uuid.UUID] = []
        self.created_kbs: list[uuid.UUID] = []
        self.addCleanup(self._cleanup)
        self._purge_audit_targets: list[str] = []
        with SessionLocal() as db:
            self.admin = self._user(db, admin=True)
            self.other_admin = self._user(db, admin=True)
            self.kb = self._kb(db, self.admin, public=False)
            self.other_kb = self._kb(db, self.admin, public=False)
            db.commit()
            self.admin_id, self.admin_version = self.admin.id, self.admin.token_version
            self.kb_id, self.other_kb_id = self.kb.id, self.other_kb.id

    def _cleanup(self):
        with SessionLocal() as db:
            for target in self._purge_audit_targets:
                db.execute(delete(AuditLog).where(AuditLog.target == target))
            if self.created_kbs:
                db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(self.created_kbs)))
            if self.created_users:
                db.execute(delete(User).where(User.id.in_(self.created_users)))
            db.commit()

    def _user(self, db, *, admin=False, active=True) -> User:
        user = User(
            username="kbm_" + uuid.uuid4().hex[:14],
            display_name="显示名",
            role="admin" if admin else "user",
            is_active=active,
            password_hash="test-only-no-login",
        )
        db.add(user)
        db.flush()
        self.created_users.append(user.id)
        return user

    def _kb(self, db, owner: User, *, public=False, kind="upload") -> KnowledgeBase:
        kb = KnowledgeBase(
            name="kbm_kb_" + uuid.uuid4().hex[:12],
            description="",
            collection_name="kbm_col_" + uuid.uuid4().hex[:14],
            owner_id=owner.id,
            is_public=public,
            kind=kind,
        )
        db.add(kb)
        db.flush()
        self.created_kbs.append(kb.id)
        return kb

    def _headers(self, user_id=None, version=None, role="admin"):
        token = create_access_token(
            str(user_id or self.admin_id), role,
            token_version=self.admin_version if version is None else version,
        )
        return {"Authorization": f"Bearer {token}"}

    def _url(self, kb_id=None, user_id=None):
        base = f"/api/v1/knowledge-bases/{kb_id or self.kb_id}/members"
        return base if user_id is None else f"{base}/{user_id}"

    def _add_member(self, kb_id, user_id, role, expected=0, headers=None):
        return self.client.put(
            self._url(kb_id, user_id), headers=headers or self._headers(),
            json={"role": role, "expected_revision": expected},
        )

    def _revision(self, kb_id=None):
        with SessionLocal() as db:
            return db.get(KnowledgeBase, kb_id or self.kb_id).membership_revision

    # ---------------- 认证/授权 ----------------

    def test_member_endpoints_require_admin_and_fresh_token(self):
        with SessionLocal() as db:
            member_user = self._user(db)
            db.commit()
            member_id = member_user.id
        cases = [
            (self._url(), None, 401),
            (self._url(), self._headers(member_id, 0, "user"), 403),
            (self._url(), self._headers(version=self.admin_version + 1), 401),
        ]
        for url, headers, expected in cases:
            with self.subTest(url=url, expected=expected):
                response = self.client.get(url, headers=headers or {})
                self.assertEqual(response.status_code, expected, response.text)
        for headers, expected in (
            (self._headers(member_id, 0, "user"), 403),
            (self._headers(version=self.admin_version + 1), 401),
        ):
            with self.subTest("put", expected=expected):
                response = self.client.put(
                    self._url(user_id=member_id), headers=headers,
                    json={"role": "reader", "expected_revision": 0})
                self.assertEqual(response.status_code, expected, response.text)
                response = self.client.delete(
                    f"{self._url(user_id=member_id)}?expected_revision=0", headers=headers)
                self.assertEqual(response.status_code, expected, response.text)
        self.assertEqual(self._revision(), 0)

    def test_disabled_admin_is_rejected(self):
        with SessionLocal() as db:
            disabled = self._user(db, admin=True, active=False)
            db.commit()
            disabled_id, version = disabled.id, disabled.token_version
        response = self.client.get(self._url(), headers=self._headers(disabled_id, version))
        self.assertEqual(response.status_code, 403, response.text)

    def test_missing_auth_token_snapshot_fails_closed(self):
        """依赖被替换成"只返回用户、不写快照"时，写入端点必须 401 而不是绕过。"""
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id

        def _no_snapshot_user(
            request: Request,
            credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
            db=Depends(lambda: None),
        ):
            with SessionLocal() as session:
                return session.get(User, self.admin_id)

        from app.db.session import get_db

        self.app.dependency_overrides[get_current_user] = _no_snapshot_user
        try:
            response = self.client.put(
                self._url(user_id=target_id), headers=self._headers(),
                json={"role": "reader", "expected_revision": 0})
            self.assertEqual(response.status_code, 401, response.text)
            response = self.client.delete(
                f"{self._url(user_id=target_id)}?expected_revision=0",
                headers=self._headers())
            self.assertEqual(response.status_code, 401, response.text)
        finally:
            self.app.dependency_overrides.pop(get_current_user, None)
        self.assertEqual(self._revision(), 0)

    # ---------------- 基本 CRUD / 幂等 / CAS ----------------

    def test_put_grants_member_and_returns_list_element_shape(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id, username = target.id, target.username
        response = self._add_member(self.kb_id, target_id, "reader")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["revision"], 1)
        self.assertEqual(set(data["member"]), {
            "user_id", "username", "display_name", "is_active", "role"})
        self.assertEqual(data["member"]["user_id"], str(target_id))
        self.assertEqual(data["member"]["username"], username)
        self.assertEqual(data["member"]["role"], "reader")
        self.assertTrue(data["member"]["is_active"])
        self.assertEqual(self._revision(), 1)

        listing = self.client.get(self._url(), headers=self._headers())
        self.assertEqual(listing.status_code, 200, listing.text)
        listed = listing.json()["data"]
        self.assertEqual(listed["revision"], 1)
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"], [data["member"]])

    def test_role_change_increments_revision_by_one(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id
        self.assertEqual(self._add_member(self.kb_id, target_id, "reader")
                         .json()["data"]["revision"], 1)
        response = self._add_member(self.kb_id, target_id, "maintainer", expected=1)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["revision"], 2)
        self.assertEqual(response.json()["data"]["member"]["role"], "maintainer")
        self.assertEqual(self._revision(), 2)

    def test_no_change_is_idempotent_but_still_checks_expected_revision(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id
        self._add_member(self.kb_id, target_id, "reader")
        # 版本正确 + 角色相同 -> 200，revision 不变。
        same = self._add_member(self.kb_id, target_id, "reader", expected=1)
        self.assertEqual(same.status_code, 200, same.text)
        self.assertEqual(same.json()["data"]["revision"], 1)
        self.assertEqual(same.json()["data"]["member"]["role"], "reader")
        # 版本过期 + 角色相同 -> 仍必须 409，不能因为“无变化”跳过 CAS。
        stale = self._add_member(self.kb_id, target_id, "reader", expected=0)
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(self._revision(), 1)
        # DELETE 无成员：版本正确幂等成功，版本过期仍 409。
        self.assertEqual(self.client.delete(
            f"{self._url(user_id=target_id)}?expected_revision=1",
            headers=self._headers()).status_code, 200)
        self.assertEqual(self._revision(), 2)
        again = self.client.delete(
            f"{self._url(user_id=target_id)}?expected_revision=2",
            headers=self._headers())
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["data"], {"revision": 2, "member": None})
        stale_delete = self.client.delete(
            f"{self._url(user_id=target_id)}?expected_revision=1",
            headers=self._headers())
        self.assertEqual(stale_delete.status_code, 409, stale_delete.text)
        self.assertEqual(self._revision(), 2)

    def test_cas_conflict_returns_409_without_changing_members(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id
        first = self._add_member(self.kb_id, target_id, "reader", expected=0)
        self.assertEqual(first.status_code, 200)
        conflict = self._add_member(self.kb_id, target_id, "maintainer", expected=0)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        with SessionLocal() as db:
            member = db.get(KnowledgeBaseMember, (self.kb_id, target_id))
            self.assertEqual(member.role, "reader")
        self.assertEqual(self._revision(), 1)

    def test_strict_role_and_expected_revision_validation(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id
        for payload in (
            {"role": "admin", "expected_revision": 0},
            {"role": "reader", "expected_revision": True},
            {"role": "reader", "expected_revision": -1},
            {"role": "reader", "expected_revision": "0"},
            {"role": "reader", "expected_revision": 0.0},
            {"role": "reader"},
            {"role": "reader", "expected_revision": 0, "extra": 1},
        ):
            with self.subTest(payload=payload):
                response = self.client.put(
                    self._url(user_id=target_id), headers=self._headers(), json=payload)
                self.assertEqual(response.status_code, 422, response.text)
        for query in ("expected_revision=true", "expected_revision=-1",
                      "expected_revision=abc", "expected_revision=1.5", ""):
            with self.subTest(query=query):
                response = self.client.delete(
                    f"{self._url(user_id=target_id)}?{query}", headers=self._headers())
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self._revision(), 0)

    def test_put_missing_or_disabled_targets(self):
        missing = uuid.uuid4()
        response = self._add_member(self.kb_id, missing, "reader")
        self.assertEqual(response.status_code, 404, response.text)
        with SessionLocal() as db:
            disabled = self._user(db, active=False)
            db.commit()
            disabled_id = disabled.id
        response = self._add_member(self.kb_id, disabled_id, "reader")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self._revision(), 0)

        # 已有相同角色的禁用成员：按无变化处理（不扩大授予，也不误报 400）。
        with SessionLocal() as db:
            db.add(KnowledgeBaseMember(kb_id=self.kb_id, user_id=disabled_id, role="reader"))
            kb = db.get(KnowledgeBase, self.kb_id)
            kb.membership_revision = 1
            db.commit()
        noop = self._add_member(self.kb_id, disabled_id, "reader", expected=1)
        self.assertEqual(noop.status_code, 200, noop.text)
        self.assertEqual(noop.json()["data"]["revision"], 1)
        # 角色变化则仍必须拒绝。
        change = self._add_member(self.kb_id, disabled_id, "maintainer", expected=1)
        self.assertEqual(change.status_code, 400, change.text)

    def test_delete_missing_target_is_idempotent_and_does_not_touch_other_members(self):
        with SessionLocal() as db:
            target = self._user(db)
            other = self._user(db)
            db.commit()
            target_id, other_id = target.id, other.id
        self._add_member(self.kb_id, target_id, "reader")
        self._add_member(self.other_kb_id, other_id, "reader")
        # 目标用户完全不存在：DELETE 幂等成功，版本不变，也不扩大清理。
        response = self.client.delete(
            f"{self._url(user_id=uuid.uuid4())}?expected_revision=1",
            headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], {"revision": 1, "member": None})
        with SessionLocal() as db:
            self.assertIsNotNone(db.get(KnowledgeBaseMember, (self.kb_id, target_id)))
            self.assertIsNotNone(db.get(KnowledgeBaseMember, (self.other_kb_id, other_id)))
        # 目标存在但无成员：同样幂等。
        response = self.client.delete(
            f"{self._url(user_id=other_id)}?expected_revision=1", headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["revision"], 1)

    def test_delete_removes_only_requested_member_and_increments_revision(self):
        with SessionLocal() as db:
            target = self._user(db)
            other = self._user(db)
            db.commit()
            target_id, other_id = target.id, other.id
        self._add_member(self.kb_id, target_id, "reader")
        self._add_member(self.kb_id, other_id, "maintainer", expected=1)
        response = self.client.delete(
            f"{self._url(user_id=target_id)}?expected_revision=2",
            headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], {"revision": 3, "member": None})
        with SessionLocal() as db:
            self.assertIsNone(db.get(KnowledgeBaseMember, (self.kb_id, target_id)))
            self.assertEqual(
                db.get(KnowledgeBaseMember, (self.kb_id, other_id)).role, "maintainer")

    def test_unknown_kb_returns_404(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id
        missing_kb = uuid.uuid4()
        self.assertEqual(self.client.get(
            self._url(kb_id=missing_kb), headers=self._headers()).status_code, 404)
        response = self._add_member(missing_kb, target_id, "reader")
        self.assertEqual(response.status_code, 404, response.text)
        response = self.client.delete(
            f"{self._url(kb_id=missing_kb, user_id=target_id)}?expected_revision=0",
            headers=self._headers())
        self.assertEqual(response.status_code, 404, response.text)

    def test_project_kb_membership_is_allowed_but_does_not_grant_document_management(self):
        with SessionLocal() as db:
            project_kb = self._kb(db, self.admin, kind="project")
            target = self._user(db)
            db.commit()
            project_kb_id, target_id = project_kb.id, target.id
        response = self._add_member(project_kb_id, target_id, "maintainer")
        self.assertEqual(response.status_code, 200, response.text)
        with SessionLocal() as db:
            from app.services import kb_access
            self.assertTrue(kb_access.can_read_kb(
                db, db.get(KnowledgeBase, project_kb_id), db.get(User, target_id)))
            self.assertFalse(kb_access.can_manage_documents(
                db, db.get(KnowledgeBase, project_kb_id), db.get(User, target_id)))

    # ---------------- 分页 ----------------

    def test_pagination_is_bounded_and_stably_sorted(self):
        with SessionLocal() as db:
            targets = []
            for index in range(5):
                user = self._user(db)
                # 固定前缀 + 序号，保证与 UUID 排序无关时顺序仍确定。
                user.username = f"kbm_order_{index:02d}_{uuid.uuid4().hex[:6]}"
                targets.append(user)
            # 一个 display_name 相同的成员，验证排序不依赖展示名。
            db.commit()
            pairs = [(user.id, user.username) for user in targets]
        revision = 0
        for user_id, _ in pairs:
            response = self._add_member(self.kb_id, user_id, "reader", expected=revision)
            self.assertEqual(response.status_code, 200, response.text)
            revision = response.json()["data"]["revision"]
        first = self.client.get(
            f"{self._url()}?page=1&page_size=2", headers=self._headers())
        self.assertEqual(first.status_code, 200, first.text)
        data = first.json()["data"]
        self.assertEqual(data["revision"], 5)
        self.assertEqual(data["total"], 5)
        self.assertEqual([item["username"] for item in data["items"]],
                         [name for _, name in sorted(pairs, key=lambda pair: pair[1])][:2])
        last = self.client.get(
            f"{self._url()}?page=3&page_size=2", headers=self._headers()).json()["data"]
        self.assertEqual(last["total"], 5)
        self.assertEqual(len(last["items"]), 1)
        empty = self.client.get(
            f"{self._url()}?page=4&page_size=2", headers=self._headers()).json()["data"]
        self.assertEqual((empty["revision"], empty["total"], empty["items"]), (5, 5, []))
        for query in ("page=0", "page_size=0", "page_size=101", "page=abc"):
            with self.subTest(query=query):
                response = self.client.get(
                    f"{self._url()}?{query}", headers=self._headers())
                self.assertEqual(response.status_code, 422, response.text)

    # ---------------- 并发 ----------------

    def _wait_for_blocked_users_lock(self, holder_pid, timeout=WAIT_TIMEOUT):
        statement = text(
            "SELECT pid, state, wait_event_type, query FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND :holder_pid = ANY(pg_blocking_pids(pid))"
        )
        deadline = time.monotonic() + timeout
        with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT") as observer:
            while time.monotonic() < deadline:
                for row in observer.execute(statement, {"holder_pid": holder_pid}).all():
                    query = (row.query or "").upper()
                    if (row.wait_event_type == "Lock" and "FOR UPDATE" in query
                            and "USERS" in query):
                        return row
                time.sleep(0.05)
        return None

    def test_concurrent_put_with_same_revision_has_exactly_one_winner(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id
        barrier = threading.Event()

        def act(role):
            with SessionLocal() as db:
                db.execute(text("SET LOCAL statement_timeout = '15s'"))
                self.assertTrue(barrier.wait(timeout=10))
                try:
                    result = kb_members_service.upsert_member(
                        db, _request(self.admin_version),
                        db.get(User, self.admin_id), self.kb_id, target_id, role, 0)
                    return 200 if result.changed else 200
                except HTTPException as exc:
                    db.rollback()
                    return exc.status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(act, "reader"), pool.submit(act, "maintainer")]
            barrier.set()
            outcomes = sorted(future.result(timeout=WAIT_TIMEOUT) for future in futures)
        self.assertEqual(outcomes, [200, 409], "同一 expected_revision 不得双写成功")
        self.assertEqual(self._revision(), 1)
        with SessionLocal() as db:
            self.assertIn(db.get(KnowledgeBaseMember, (self.kb_id, target_id)).role,
                          {"reader", "maintainer"})

    def test_admin_revoked_while_waiting_for_user_lock_is_401(self):
        """真实观测到 User 行锁等待后才撤销令牌；锁后复核快照 -> 401 且无写入。"""
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id = target.id
        holder = SessionLocal()
        holder.execute(text("SELECT set_config('application_name', 'kbm_holder', false)"))
        holder.scalar(select(User).where(User.id == self.admin_id).with_for_update())
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()
        started = threading.Event()
        result = {}

        def call():
            started.set()
            try:
                result["response"] = self._add_member(self.kb_id, target_id, "reader")
            except Exception as exc:  # noqa: BLE001 - 主线程统一断言
                result["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        try:
            worker.start()
            self.assertTrue(started.wait(10), "请求线程未启动")
            blocked = self._wait_for_blocked_users_lock(holder_pid)
            self.assertIsNotNone(blocked, "未观察到真实的 User 行锁等待")
            self.assertEqual(blocked.wait_event_type, "Lock")
            holder.execute(
                text("UPDATE users SET token_version = token_version + 1 WHERE id = :uid"),
                {"uid": self.admin_id})
            holder.commit()
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive(), "放锁后请求仍未结束")
        finally:
            try:
                holder.rollback()
            except Exception:  # noqa: BLE001
                pass
            if worker.is_alive():
                worker.join(timeout=30)
            holder.close()
        self.assertIn("response", result, repr(result.get("error")))
        self.assertEqual(result["response"].status_code, 401, result["response"].text)
        self.assertEqual(self._revision(), 0)

    # ---------------- 审计 ----------------

    def test_audit_only_on_real_changes_and_contains_only_stable_ids(self):
        with SessionLocal() as db:
            target = self._user(db)
            db.commit()
            target_id, username = target.id, target.username
        self._purge_audit_targets.append(str(self.kb_id))
        self._add_member(self.kb_id, target_id, "reader")
        # 无变化不写审计。
        self._add_member(self.kb_id, target_id, "reader", expected=1)
        # 角色变化写审计。
        self._add_member(self.kb_id, target_id, "maintainer", expected=1)
        self.client.delete(
            f"{self._url(user_id=target_id)}?expected_revision=2", headers=self._headers())
        with SessionLocal() as db:
            rows = db.scalars(
                select(AuditLog).where(AuditLog.target == str(self.kb_id))
                .order_by(AuditLog.created_at, AuditLog.id)
            ).all()
            self.assertEqual([row.action for row in rows],
                             ["kb_member_update", "kb_member_update", "kb_member_remove"])
            details = [json.dumps(row.detail, ensure_ascii=False) for row in rows]
            self.assertEqual(rows[0].detail["role"], "reader")
            self.assertEqual(rows[0].detail["membership_revision"], 1)
            self.assertEqual(rows[1].detail["previous_role"], "reader")
            self.assertEqual(rows[1].detail["role"], "maintainer")
            self.assertEqual(rows[2].detail["role"], None)
            self.assertEqual(rows[2].detail["previous_role"], "maintainer")
            for row, raw in zip(rows, details):
                self.assertEqual(row.username, self.admin.username)
                self.assertEqual(row.detail["kb_id"], str(self.kb_id))
                self.assertEqual(row.detail["user_id"], str(target_id))
                self.assertNotIn(username, raw)
                self.assertNotIn("显示名", raw)
                self.assertNotIn("password", raw.lower())


if __name__ == "__main__":
    unittest.main()