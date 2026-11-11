"""A1 知识库读取/维护权限公共 helper（真实 PostgreSQL + 纯 SQL 结构断言）。

覆盖冻结接口：

- ``visible_kb_predicate(user, kb_model)``：SQL 谓词；相关 EXISTS；可用于
  ``aliased(KnowledgeBase)``；不活跃恒 false，管理员恒 true，其余
  public/owner/显式成员（reader 或 maintainer）。
- ``can_read_kb(db, kb, user)``：显式 Session；拒绝 disabled；兼容
  admin/public/owner；接受显式成员；跨库隔离；查询不触发 autoflush。
- ``can_manage_documents(db, kb, user)``：仅 upload + admin/显式 maintainer；
  owner/public/reader/项目库均拒绝；查询不触发 autoflush。
- ``lock_current_user(db, user_id, auth_token_version)``：FOR UPDATE +
  populate_existing + no_autoflush；严格 int 版本、非负、精确匹配；bool/None/
  字符串/负数/缺失用户/禁用用户 -> 401；失败路径不提前碰锁。

数据库门沿用 ``tests._memory_project_test_gate``（无配置 skip、坏配置硬失败）。
"""
import unittest
import uuid

from types import SimpleNamespace

from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import aliased

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from app.models import (
    KB_MEMBER_ROLE_MAINTAINER,
    KB_MEMBER_ROLE_READER,
    KnowledgeBase,
    KnowledgeBaseMember,
    User,
)
from app.services import kb_access

if TEST_URL:
    from app.db import session as db_session
    from app.db.init_db import init_db

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


class VisibleKbPredicateSqlTests(unittest.TestCase):
    """不需要数据库：谓词的 SQL 形状与分支（false/true/相关 EXISTS）。"""

    def _user(self, **overrides):
        values = dict(id=uuid.uuid4(), role="user", is_active=True)
        values.update(overrides)
        return SimpleNamespace(**values)

    def _compiled(self, predicate) -> str:
        return str(predicate.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        ))

    def test_inactive_user_is_false_and_admin_is_true(self):
        disabled_admin = self._user(role="admin", is_active=False)
        self.assertIn("false", self._compiled(
            kb_access.visible_kb_predicate(disabled_admin)).lower())
        active_admin = self._user(role="admin", is_active=True)
        self.assertIn("true", self._compiled(
            kb_access.visible_kb_predicate(active_admin)).lower())

    def test_member_branch_is_correlated_exists(self):
        user = self._user()
        sql = self._compiled(kb_access.visible_kb_predicate(user))
        self.assertIn("EXISTS", sql.upper())
        self.assertIn("knowledge_base_members.kb_id = knowledge_bases.id", sql)
        self.assertIn("knowledge_base_members.user_id", sql)
        # 角色白名单保留（未来扩展角色不会自动获得读取权）。
        self.assertIn("'reader'", sql)
        self.assertIn("'maintainer'", sql)

    def test_predicate_correlates_to_aliased_knowledge_base(self):
        """A2/A3 常常对 aliased(KnowledgeBase) 过滤；EXISTS 必须引用别名，而不是再开基表。"""
        user = self._user()
        kb_alias = aliased(KnowledgeBase)
        predicate = kb_access.visible_kb_predicate(user, kb_alias)
        stmt = select(kb_alias.id).where(kb_alias.id == uuid.uuid4(), predicate)
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.assertIn("knowledge_bases AS knowledge_bases_1", sql)
        # 相关谓词必须指向外层别名（kb_id = knowledge_bases_1.id）。
        self.assertIn("knowledge_base_members.kb_id = knowledge_bases_1.id", sql)
        # 子查询自身只 FROM 成员表，不能把基表 knowledge_bases 再拼进来（笛卡尔/放大）。
        exists_sql = sql[sql.upper().index("EXISTS"):]
        self.assertNotIn("FROM knowledge_bases ", exists_sql)


@unittest.skipIf(
    gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
    "需要显式 owned PostgreSQL 测试库",
)
class KbAccessDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        init_db(create_admin=False)

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

    def _cleanup(self):
        from sqlalchemy import delete

        with SessionLocal() as db:
            if self.created_kbs:
                db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(self.created_kbs)))
            if self.created_users:
                db.execute(delete(User).where(User.id.in_(self.created_users)))
            db.commit()

    def _user(self, db, *, admin=False, active=True) -> User:
        user = User(
            username="acl_" + uuid.uuid4().hex[:12],
            display_name="u",
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
            name="aclk_" + uuid.uuid4().hex[:12],
            description="",
            collection_name="aclc_" + uuid.uuid4().hex[:16],
            owner_id=owner.id,
            is_public=public,
            kind=kind,
        )
        db.add(kb)
        db.flush()
        self.created_kbs.append(kb.id)
        return kb

    def _member(self, db, kb: KnowledgeBase, user: User, role: str) -> None:
        db.add(KnowledgeBaseMember(kb_id=kb.id, user_id=user.id, role=role))
        db.flush()

    def _fixture(self):
        """一次性建好 admin/public库/私有库/各角色用户与成员关系。"""
        with SessionLocal() as db:
            admin = self._user(db, admin=True)
            owner = self._user(db)
            public_kb = self._kb(db, owner, public=True)
            private_kb = self._kb(db, owner, public=False)
            other_private_kb = self._kb(db, owner, public=False)
            project_kb = self._kb(db, owner, public=False, kind="project")
            reader = self._user(db)
            maintainer = self._user(db)
            stranger = self._user(db)
            disabled = self._user(db, active=False)
            self._member(db, private_kb, reader, KB_MEMBER_ROLE_READER)
            self._member(db, private_kb, maintainer, KB_MEMBER_ROLE_MAINTAINER)
            # 另一私有库：只有 reader，用于跨库隔离断言。
            self._member(db, other_private_kb, reader, KB_MEMBER_ROLE_READER)
            # 项目库成员：只能读，不能维护。
            self._member(db, project_kb, maintainer, KB_MEMBER_ROLE_MAINTAINER)
            self._member(db, private_kb, disabled, KB_MEMBER_ROLE_READER)
            db.commit()
            return SimpleNamespace(
                admin=admin, owner=owner, reader=reader, maintainer=maintainer,
                stranger=stranger, disabled=disabled,
                public_kb=public_kb, private_kb=private_kb,
                other_private_kb=other_private_kb, project_kb=project_kb,
            )

    # ---------------- can_read_kb ----------------

    def test_can_read_matrix(self):
        f = self._fixture()
        with SessionLocal() as db:
            def read(kb, user):
                return kb_access.can_read_kb(db, db.get(KnowledgeBase, kb.id),
                                             db.get(User, user.id))

            self.assertTrue(read(f.public_kb, f.stranger), "旧 public 读取语义应保持")
            self.assertTrue(read(f.private_kb, f.owner), "旧 owner 读取语义应保持")
            self.assertTrue(read(f.private_kb, f.admin))
            self.assertTrue(read(f.private_kb, f.reader))
            self.assertTrue(read(f.private_kb, f.maintainer))
            self.assertFalse(read(f.private_kb, f.stranger))
            self.assertFalse(read(f.private_kb, f.disabled), "disabled 成员也不得放行")
            self.assertFalse(read(f.other_private_kb, f.maintainer), "成员关系不得跨库生效")
            self.assertFalse(read(f.private_kb, SimpleNamespace(
                id=f.stranger.id, role="user", is_active=False)))

    def test_fixture_cleanup_does_not_leak_into_other_kb(self):
        f = self._fixture()
        with SessionLocal() as db:
            self.assertTrue(kb_access.can_read_kb(
                db, db.get(KnowledgeBase, f.other_private_kb.id), db.get(User, f.reader.id)))
            self.assertFalse(kb_access.can_read_kb(
                db, db.get(KnowledgeBase, f.other_private_kb.id),
                db.get(User, f.maintainer.id)))

    def test_helpers_reject_missing_session_even_for_admin_or_legacy_grants(self):
        """契约：无显式 Session 不放行——admin/public/owner 分支也一样，返回 False。"""
        kb = SimpleNamespace(id=uuid.uuid4(), is_public=True, owner_id=uuid.uuid4(),
                             kind="upload")
        admin = SimpleNamespace(id=uuid.uuid4(), role="admin", is_active=True)
        owner = SimpleNamespace(id=kb.owner_id, role="user", is_active=True)
        public_user = SimpleNamespace(id=uuid.uuid4(), role="user", is_active=True)
        member = SimpleNamespace(id=uuid.uuid4(), role="user", is_active=True)
        for user, label in ((admin, "admin"), (owner, "owner"), (public_user, "public"),
                            (member, "member")):
            with self.subTest(user=label):
                self.assertFalse(kb_access.can_read_kb(None, kb, user))
                self.assertFalse(kb_access.can_manage_documents(None, kb, user))

    # ---------------- visible_kb_predicate ----------------

    def test_visible_predicate_matches_sql_rows_for_each_role(self):
        f = self._fixture()
        with SessionLocal() as db:
            users = [db.get(User, uid) for uid in (
                f.admin.id, f.owner.id, f.reader.id, f.maintainer.id,
                f.stranger.id, f.disabled.id)]
            kbs = {db.get(KnowledgeBase, kid).name: kid for kid in (
                f.public_kb.id, f.private_kb.id, f.other_private_kb.id, f.project_kb.id)}
            for user in users:
                visible = set(db.scalars(
                    select(KnowledgeBase.name)
                    .where(KnowledgeBase.id.in_(list(kbs.values())))
                    .where(kb_access.visible_kb_predicate(user))
                ).all())
                if not user.is_active:
                    self.assertEqual(visible, set())
                    continue
                if user.role == "admin":
                    self.assertEqual(visible, set(kbs))
                    continue
                expected = set()
                if user.id == f.owner.id:
                    expected = set(kbs)
                else:
                    expected.add(f.public_kb.name)
                if user.id in (f.reader.id, f.maintainer.id):
                    expected.add(f.private_kb.name)
                if user.id == f.reader.id:
                    expected.add(f.other_private_kb.name)
                if user.id == f.maintainer.id:
                    expected.add(f.project_kb.name)
                self.assertEqual(visible, expected, f"user={user.username}")

    def test_visible_predicate_with_aliased_model_returns_same_rows(self):
        f = self._fixture()
        with SessionLocal() as db:
            reader = db.get(User, f.reader.id)
            kb_alias = aliased(KnowledgeBase)
            rows = set(db.scalars(
                select(kb_alias.name)
                .where(kb_alias.id.in_([f.public_kb.id, f.private_kb.id,
                                        f.other_private_kb.id, f.project_kb.id]))
                .where(kb_access.visible_kb_predicate(reader, kb_alias))
            ).all())
            self.assertEqual(rows, {f.public_kb.name, f.private_kb.name,
                                    f.other_private_kb.name})

    def test_predicate_not_applied_after_limit_in_real_query(self):
        """谓词用于 LIMIT 之前：陌生人分页第一页仍是自己的库，不因先取页再过滤而空。"""
        f = self._fixture()
        with SessionLocal() as db:
            stranger = db.get(User, f.stranger.id)
            all_ids = [f.public_kb.id, f.private_kb.id, f.other_private_kb.id, f.project_kb.id]
            rows = db.scalars(
                select(KnowledgeBase.id)
                .where(KnowledgeBase.id.in_(all_ids))
                .where(kb_access.visible_kb_predicate(stranger))
                .order_by(KnowledgeBase.id)
                .limit(1)
            ).all()
            self.assertEqual(len(rows), 1)

    # ---------------- can_manage_documents ----------------

    def test_can_manage_documents_matrix(self):
        f = self._fixture()
        with SessionLocal() as db:
            def manage(kb, user):
                return kb_access.can_manage_documents(
                    db, db.get(KnowledgeBase, kb.id), db.get(User, user.id))

            self.assertTrue(manage(f.private_kb, f.admin))
            self.assertTrue(manage(f.private_kb, f.maintainer))
            self.assertFalse(manage(f.private_kb, f.reader))
            self.assertFalse(manage(f.private_kb, f.owner), "owner 只保留读取，不自动获得维护")
            self.assertFalse(manage(f.public_kb, f.stranger), "public 不自动获得维护")
            self.assertFalse(manage(f.private_kb, f.disabled))
            self.assertFalse(manage(f.private_kb, f.stranger))
            self.assertFalse(manage(f.other_private_kb, f.reader))
            self.assertFalse(manage(f.project_kb, f.admin), "项目库维护仍属项目配置，不由此放行")
            self.assertFalse(manage(f.project_kb, f.maintainer), "项目库成员也不能获得文档维护")

    # ---------------- no_autoflush ----------------

    def test_read_helpers_do_not_flush_pending_objects(self):
        f = self._fixture()
        with SessionLocal() as db:
            pending = User(
                username="acl_pending_" + uuid.uuid4().hex[:12],
                display_name="p",
                role="user",
                is_active=True,
                password_hash="test-only-no-login",
            )
            db.add(pending)
            self.assertIn(pending, db.new)
            kb_access.can_read_kb(db, db.get(KnowledgeBase, f.private_kb.id),
                                  db.get(User, f.reader.id))
            kb_access.can_manage_documents(db, db.get(KnowledgeBase, f.private_kb.id),
                                           db.get(User, f.maintainer.id))
            self.assertIn(pending, db.new, "权限只读判定触发了一次隐式 flush")
            db.rollback()

    # ---------------- lock_current_user ----------------

    def test_lock_current_user_rejects_bad_snapshots_without_touching_db(self):
        f = self._fixture()
        with SessionLocal() as db:
            pending = User(
                username="acl_pending2_" + uuid.uuid4().hex[:12], role="user",
                is_active=True, password_hash="test-only-no-login",
            )
            db.add(pending)
            for version in (None, True, False, "0", 1.5, -1):
                with self.subTest(version=version):
                    with self.assertRaises(Exception) as fault:
                        kb_access.lock_current_user(db, f.reader.id, version)
                    self.assertEqual(getattr(fault.exception, "status_code", None), 401)
            self.assertIn(pending, db.new, "参数校验失败前不应查询/隐式 flush")
            db.rollback()

    def test_lock_current_user_rejects_missing_or_disabled_user(self):
        f = self._fixture()
        with SessionLocal() as db:
            with self.assertRaises(Exception) as missing:
                kb_access.lock_current_user(db, uuid.uuid4(), 0)
            self.assertEqual(missing.exception.status_code, 401)
            db.rollback()
            with self.assertRaises(Exception) as disabled:
                kb_access.lock_current_user(db, f.disabled.id, 0)
            self.assertEqual(disabled.exception.status_code, 401)
            db.rollback()

    def test_lock_current_user_detects_token_version_changed_while_waiting(self):
        f = self._fixture()
        with SessionLocal() as db:
            stale = db.get(User, f.reader.id)
            self.assertEqual(stale.token_version, 0)
        # 另一个事务先递增版本（模拟排队期间改密/禁用/降权）。
        with SessionLocal() as writer:
            writer.execute(
                text("UPDATE users SET token_version = token_version + 1 WHERE id = :uid"),
                {"uid": f.reader.id},
            )
            writer.commit()
        with SessionLocal() as db:
            with self.assertRaises(Exception) as fault:
                kb_access.lock_current_user(db, f.reader.id, 0)
            self.assertEqual(fault.exception.status_code, 401)
            db.rollback()

    def test_lock_current_user_returns_fresh_row_and_does_not_flush(self):
        f = self._fixture()
        with SessionLocal() as db:
            reader = db.get(User, f.reader.id)
            # 会话里的旧对象 + 未提交脏改：锁后必须被 populate_existing 覆盖成库值。
            reader.display_name = "dirty-not-committed"
            pending = KnowledgeBase(
                name="acl_pending3_" + uuid.uuid4().hex[:12], description="",
                collection_name="aclc_" + uuid.uuid4().hex[:16],
                owner_id=f.reader.id, is_public=False,
            )
            db.add(pending)
            fresh = kb_access.lock_current_user(db, f.reader.id, 0)
            self.assertEqual(fresh.display_name, "u", "锁后应读已提交值而不是脏对象")
            self.assertIn(pending, db.new, "lock_current_user 不应隐式 flush 会话")
            db.rollback()

    def test_lock_current_user_uses_for_update_and_populate_existing(self):
        f = self._fixture()
        with SessionLocal() as db:
            captured = {}
            real_scalar = db.scalar

            def spy(statement, *args, **kwargs):
                captured["statement"] = statement
                return real_scalar(statement, *args, **kwargs)

            db.scalar = spy
            kb_access.lock_current_user(db, f.reader.id, 0)
            sql = str(captured["statement"].compile(dialect=postgresql.dialect()))
            self.assertTrue(sql.endswith("FOR UPDATE"), sql)
            self.assertTrue(
                captured["statement"].get_execution_options().get("populate_existing"))


if __name__ == "__main__":
    unittest.main()