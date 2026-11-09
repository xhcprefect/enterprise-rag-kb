"""治理写入口的令牌版本快照：认证期捕获、锁后复核、缺失即 fail closed。

覆盖三条不变量：

1. ``get_current_user`` 只在认证成功时把**原始 JWT 的 ``ver``** 写进
   ``request.state.auth_token_version``；之后不再读 ORM 实例（实例可能已被
   刷新成新版本），也不再重新解码请求头（排队期间令牌可能已过期）。
2. ``_lock_governance_admin`` 锁住 User 行后只用该快照复核 active/admin/版本。
3. 快照缺失或类型不对 -> 401，绝不因为"取不到版本"而放行；生产 HTTP 路径
   同样 fail closed，不靠 Mock/类型判断走旁路。

纯单元部分不需要数据库；HTTP 部分沿用 ``tests._memory_project_test_gate``
的真实 PostgreSQL 门与真 JWT，未配置测试库时整类 skip。
"""
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
from sqlalchemy import text

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import get_current_user
from app.api.v1 import knowledge
from app.core.config import settings
from app.core.security import create_access_token

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL


def _request(headers=None):
    return Request({"type": "http", "headers": headers or []})


def _bearer(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


class _FakeDB:
    """只实现 ``get`` 的最小会话；``scalar`` 被调用即说明顺序错了。"""

    def __init__(self, user):
        self.user = user
        self.scalar_calls = 0

    def get(self, model, pk):
        return self.user

    def scalar(self, stmt):
        self.scalar_calls += 1
        return self.user


class TokenVersionSnapshotTests(unittest.TestCase):
    """不碰数据库：认证期快照的写入、取值与 fail-closed 顺序。"""

    def _user(self, version=4, *, is_admin=True, is_active=True):
        return SimpleNamespace(id=uuid.uuid4(), username="snap", is_admin=is_admin,
                               is_active=is_active, token_version=version)

    def test_success_writes_exact_original_ver_to_request_state(self):
        user = self._user(7)
        request = _request()
        token = create_access_token(str(user.id), "admin", token_version=7)
        returned = get_current_user(request, _bearer(token), _FakeDB(user))
        self.assertIs(returned, user)
        self.assertEqual(request.state.auth_token_version, 7)
        self.assertIs(type(request.state.auth_token_version), int)

    def test_legacy_token_without_ver_claim_snapshots_zero(self):
        user = self._user(0)
        request = _request()
        now = datetime.now(timezone.utc)
        legacy = jwt.encode(
            {"sub": str(user.id), "role": "admin", "type": "access",
             "iat": now, "exp": now + timedelta(minutes=5)},
            settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
        )
        get_current_user(request, _bearer(legacy), _FakeDB(user))
        self.assertEqual(request.state.auth_token_version, 0)

    def test_bool_ver_is_not_accepted_as_int(self):
        user = self._user(1)
        request = _request()
        now = datetime.now(timezone.utc)
        forged = jwt.encode(
            {"sub": str(user.id), "role": "admin", "type": "access", "ver": True,
             "iat": now, "exp": now + timedelta(minutes=5)},
            settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM,
        )
        with self.assertRaises(HTTPException) as fault:
            get_current_user(request, _bearer(forged), _FakeDB(user))
        self.assertEqual(fault.exception.status_code, 401)
        self.assertFalse(hasattr(request.state, "auth_token_version"))

    def test_helper_uses_snapshot_and_never_redecodes_the_header(self):
        """旧实现重解码头：排队期间令牌过期就得到 None 并跳过校验（fail open）。"""
        request = _request()
        request.state.auth_token_version = 4
        # 头部放一个完全无效的令牌；若实现重新解码，结果会是 None。
        request.scope["headers"] = [(b"authorization", b"Bearer not-a-jwt")]
        self.assertEqual(knowledge._request_token_version(request), 4)

    def test_helper_returns_none_when_snapshot_missing_or_wrong_type(self):
        self.assertIsNone(knowledge._request_token_version(_request()))
        request = _request()
        request.state.auth_token_version = "4"
        self.assertIsNone(knowledge._request_token_version(request))
        request.state.auth_token_version = True
        self.assertIsNone(knowledge._request_token_version(request))
        request.state.auth_token_version = None
        self.assertIsNone(knowledge._request_token_version(request))

    def test_missing_snapshot_fails_closed_before_touching_any_row(self):
        db = _FakeDB(self._user(4))
        with self.assertRaises(HTTPException) as fault:
            knowledge._lock_governance_admin(db, _request(), db.user)
        self.assertEqual(fault.exception.status_code, 401)
        self.assertEqual(db.scalar_calls, 0)  # 未取 User 锁

    def test_stale_snapshot_is_rejected_after_lock(self):
        user = self._user(5)
        request = _request()
        request.state.auth_token_version = 4
        with self.assertRaises(HTTPException) as fault:
            knowledge._lock_governance_admin(_FakeDB(user), request, user)
        self.assertEqual(fault.exception.status_code, 401)

    def test_demotion_or_disable_while_waiting_is_forbidden(self):
        for field in ("is_admin", "is_active"):
            with self.subTest(field=field):
                fresh = self._user(4)
                setattr(fresh, field, False)
                request = _request()
                request.state.auth_token_version = 4
                with self.assertRaises(HTTPException) as fault:
                    knowledge._lock_governance_admin(_FakeDB(fresh), request, fresh)
                self.assertEqual(fault.exception.status_code, 403)

    def test_matching_snapshot_refreshes_request_user(self):
        fresh = self._user(4)
        request = _request()
        request.state.auth_token_version = 4
        result = knowledge._lock_governance_admin(_FakeDB(fresh), request, fresh)
        self.assertIs(result, fresh)
        self.assertIs(request.state.user, fresh)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class GovernanceAuthHttpTests(unittest.TestCase):
    """真 FastAPI app + 真 JWT + 真 require_admin + 真行锁，只替换会话工厂。"""

    @classmethod
    def setUpClass(cls):
        from sqlalchemy.engine import make_url

        from app.db import session as db_session
        from app.db.init_db import init_db
        from app.models import User
        from app.core.security import hash_password
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.db.session import get_db

        if db_session.engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        init_db(create_admin=False)

        cls.SessionLocal, cls.engine = db_session.SessionLocal, db_session.engine
        cls.User = User
        # 必须是 staticmethod：裸函数挂到类上会变成描述符，``self.hash_password(x)``
        # 会多传一个 self（TypeError: takes 1 positional argument but 2 were given）。
        cls.hash_password = staticmethod(hash_password)

        cls.app = FastAPI()
        cls.app.include_router(knowledge.router, prefix="/api/v1")

        def _session_override():
            db = cls.SessionLocal()
            try:
                yield db
            finally:
                db.close()

        cls.app.dependency_overrides[get_db] = _session_override
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.engine.dispose()

    def setUp(self):
        from sqlalchemy import text
        from app.models import KnowledgeBase

        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, self.engine)
        with self.engine.begin() as conn:
            conn.execute(text(
                "TRUNCATE users, knowledge_bases, documents, audit_logs "
                "RESTART IDENTITY CASCADE"))
        with self.SessionLocal() as db:
            admin = self.User(username="gov_auth_admin", role="admin", is_active=True,
                              password_hash=self.hash_password("AdminPass123"), token_version=4)
            db.add(admin)
            db.flush()
            kb = KnowledgeBase(name="gov-auth-kb", collection_name="ga_" + uuid.uuid4().hex,
                               owner_id=admin.id, is_public=True, kind="upload")
            db.add(kb)
            db.commit()
            self.admin_id, self.kb_id = admin.id, kb.id
        self.base = f"/api/v1/knowledge-bases/{self.kb_id}/documents"

    def _headers(self, version):
        token = create_access_token(str(self.admin_id), "admin", token_version=version)
        return {"Authorization": f"Bearer {token}"}

    def _add_doc(self):
        from app.models import Document
        with self.SessionLocal() as db:
            doc = Document(kb_id=self.kb_id, filename="d.txt", file_ext=".txt", file_size=1,
                           stored_path=f"/tmp/{uuid.uuid4().hex}.txt", status="ready")
            db.add(doc)
            db.commit()
            return doc.id

    def _revision(self, doc_id):
        from app.models import Document
        with self.SessionLocal() as db:
            return db.get(Document, doc_id).governance_revision

    def test_bad_version_is_401_without_any_write(self):
        doc_id = self._add_doc()
        response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                     headers=self._headers(3),
                                     json={"expected_revision": 0, "maintainer": "x"})
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self._revision(doc_id), 0)

    def test_missing_snapshot_is_401_not_a_bypass(self):
        """依赖被绕过（无快照）时，治理端点必须 401，不能因为"没版本"而放行。"""
        doc_id = self._add_doc()
        from app.models import User
        from app.api.deps import bearer_scheme
        from app.db.session import get_db

        # 依赖签名必须与真实 ``get_current_user`` 一致（同样用 Depends 声明），
        # 否则 FastAPI 会把 credentials/db 当成请求体参数，返回 422 而不是 401。
        def _no_snapshot_user(request: Request,
                              credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
                              db=Depends(get_db)):
            with self.SessionLocal() as session:
                user = session.get(User, self.admin_id)
            return user  # 只返回对象，不写 request.state.auth_token_version

        self.app.dependency_overrides[get_current_user] = _no_snapshot_user
        try:
            response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                         headers=self._headers(4),
                                         json={"expected_revision": 0, "maintainer": "x"})
        finally:
            self.app.dependency_overrides.pop(get_current_user, None)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(self._revision(doc_id), 0)

    def _wait_for_blocked_request(self, holder_pid, timeout=20.0):
        """轮询 ``pg_stat_activity`` 直到看见被 holder 阻塞的请求后端。

        必须同时满足：``pg_blocking_pids(pid)`` 含 holder 的 PID、``wait_event_type
        = 'Lock'``、阻塞中的语句是 ``users`` 上的 ``FOR UPDATE``。后者证明该请求
        已经完成认证（认证期只有普通 SELECT），正卡在治理入口取 User 行锁。
        用 ``application_name`` 反向排除 holder 自己，避免把持锁方误认成等待方。
        """
        statement = text(
            "SELECT pid, application_name, state, wait_event_type, wait_event, query "
            "FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND :holder_pid = ANY(pg_blocking_pids(pid))"
        )
        deadline = time.monotonic() + timeout
        # AUTOCOMMIT 观测连接：默认模式下 SQLAlchemy 会在首次 execute 时开启事务，
        # 之后每轮查询都复用同一快照，看不到 worker 新出现的锁等待。必须每轮都是
        # 新快照，否则要么永远观测不到，要么只能靠运气。
        with self.engine.connect().execution_options(
                isolation_level="AUTOCOMMIT") as observer:
            while time.monotonic() < deadline:
                rows = observer.execute(statement, {"holder_pid": holder_pid}).all()
                for row in rows:
                    query = (row.query or "").upper()
                    if (row.wait_event_type == "Lock"
                            and row.application_name != "gov_auth_holder"
                            and "FOR UPDATE" in query
                            and "USERS" in query):
                        return row
                time.sleep(0.05)
        return None

    def test_revocation_while_waiting_for_user_lock_is_401(self):
        """真实观测到 User 行锁等待后再撤权：锁后读到新版本 401，且无治理写入。

        时间假定被替换为可观察事实：先由 ``pg_stat_activity`` /
        ``pg_blocking_pids`` 证明请求后端确实阻塞在我们持有的那一行上，才执行
        撤权；否则用例失败（避免把"认证阶段就 401"误当成锁后复核生效）。
        """
        from sqlalchemy import select
        from app.models import User

        doc_id = self._add_doc()
        holder = self.SessionLocal()
        holder.execute(text(
            "SELECT set_config('application_name', 'gov_auth_holder', false)"))
        holder.scalar(select(User).where(User.id == self.admin_id).with_for_update())
        holder_pid = holder.execute(text("SELECT pg_backend_pid()")).scalar_one()

        started = threading.Event()
        result = {}

        def call():
            # 只表示"已进入请求"；真正的等待证据来自 pg_stat_activity /
            # pg_blocking_pids，不能把线程启动当成已卡在锁上。
            started.set()
            try:
                result["response"] = self.client.patch(
                    f"{self.base}/{doc_id}/governance", headers=self._headers(4),
                    json={"expected_revision": 0, "maintainer": "x"})
            except Exception as exc:  # noqa: BLE001 - 记录后在主线程断言
                result["error"] = exc

        worker = threading.Thread(target=call, daemon=True)
        try:
            worker.start()
            self.assertTrue(started.wait(10), "请求线程未启动")
            blocked = self._wait_for_blocked_request(holder_pid)
            self.assertIsNotNone(blocked, "未观察到真实的 User 行锁等待，拒绝继续")
            self.assertNotEqual(blocked.pid, holder_pid)
            self.assertEqual(blocked.wait_event_type, "Lock")
            self.assertEqual(blocked.state, "active")
            # 只有确认请求确实卡在锁上之后，才在持锁事务里撤权并放锁。
            holder.execute(text("UPDATE users SET token_version = token_version + 1 "
                                "WHERE id = :uid").bindparams(uid=self.admin_id))
            holder.commit()
            worker.join(timeout=30)
            self.assertFalse(worker.is_alive(), "放锁后请求线程仍未结束")
        finally:
            # 任何失败路径都必须先放锁：否则阻塞中的请求会一直等下去。
            try:
                holder.rollback()
            except Exception:  # noqa: BLE001 - 清理失败不得掩盖原始断言
                pass
            if worker.is_alive():
                worker.join(timeout=30)
            holder.close()
        self.assertFalse(worker.is_alive(), "超时后仍有存活 worker，拒绝收尾")
        self.assertIn("response", result, repr(result.get("error")))
        self.assertEqual(result["response"].status_code, 401, result["response"].text)
        self.assertEqual(self._revision(doc_id), 0)

    def test_valid_snapshot_still_succeeds(self):
        doc_id = self._add_doc()
        response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                     headers=self._headers(4),
                                     json={"expected_revision": 0, "maintainer": "ops"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._revision(doc_id), 1)