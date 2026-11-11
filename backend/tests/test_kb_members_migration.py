"""A1 成员权限迁移：模拟旧 schema、幂等两遍初始化、真实 server default、保留原数据。

只使用本轮 owned 的 ``ragkb_next_test``（owner comment 精确匹配）；在任何
DDL/DELETE 前都走 ``tests._memory_project_test_gate``。

覆盖：

- 旧库（无 ``knowledge_base_members`` 表、无 ``knowledge_bases.membership_revision``）
  连续 init 两遍后：列存在且 NOT NULL DEFAULT 0；表存在；既有 KB/用户行不丢。
- 老行通过裸 SQL 插入/保留后得到 revision=0（真实 server default，不靠 ORM）。
- 复合主键、双 FK ``ON DELETE CASCADE``、role CHECK、``user_id`` 索引真实存在。
- role 非法值被数据库拒绝；重复 (kb_id,user_id) 被主键拒绝。
- 再跑一遍 init：不重复加列/不重建表、既有 revision 值不被重置。
"""
import unittest
import uuid

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from sqlalchemy import text

if TEST_URL:
    from app.db import session as db_session
    from app.db.init_db import init_db
    from app.db.migrations import ensure_foundation_schema, ensure_kb_membership_schema
    from app.models import KnowledgeBase, User

    SessionLocal, engine = db_session.SessionLocal, db_session.engine

_MEMBER_COLUMNS = {
    "kb_id", "user_id", "role",
}


@unittest.skipIf(
    gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
    "需要显式 owned PostgreSQL 测试库",
)
class KbMembersMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        self.engine = db_session.engine
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, self.engine)
        self.created_users: list[uuid.UUID] = []
        self.created_kbs: list[uuid.UUID] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        from sqlalchemy import delete

        with SessionLocal() as db:
            db.execute(delete(KnowledgeBase).where(KnowledgeBase.name.like("raw_%")))
            if self.created_kbs:
                db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(self.created_kbs)))
            if self.created_users:
                db.execute(delete(User).where(User.id.in_(self.created_users)))
            db.commit()

    def _user(self, db, *, admin=False) -> User:
        user = User(
            username="kbm_mig_" + uuid.uuid4().hex[:12], display_name="m",
            role="admin" if admin else "user", is_active=True,
            password_hash="test-only-no-login",
        )
        db.add(user)
        db.flush()
        self.created_users.append(user.id)
        return user

    def _kb(self, db, owner: User, *, public=True) -> KnowledgeBase:
        kb = KnowledgeBase(
            name="kbm_mig_kb_" + uuid.uuid4().hex[:12], description="保留我",
            collection_name="kbm_mig_col_" + uuid.uuid4().hex[:14],
            owner_id=owner.id, is_public=public, kind="upload",
        )
        db.add(kb)
        db.flush()
        self.created_kbs.append(kb.id)
        return kb

    def _columns(self, table):
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ), {"t": table}).all()
            return {row[0]: {"nullable": row[1], "default": row[2]} for row in rows}

    def _tables(self):
        with self.engine.connect() as conn:
            return {row[0] for row in conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"))}

    def _member_constraints(self):
        with self.engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT c.conname, c.contype, c.confdeltype "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "JOIN pg_namespace n ON n.oid = t.relnamespace "
                "WHERE t.relname = 'knowledge_base_members' AND n.nspname = current_schema()"
            )).all()
        return {(row[0], row[1], row[2]) for row in rows}

    def _member_indexes(self):
        with self.engine.connect() as conn:
            return {row[0] for row in conn.execute(text(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'knowledge_base_members'"
            ))}

    def _simulate_old_schema(self):
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS knowledge_base_members CASCADE"))
            conn.execute(text(
                "ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS membership_revision"))

    # ---------------- 旧 schema -> 两遍 init ----------------

    def test_old_schema_two_pass_init_preserves_data_and_adds_default_zero(self):
        with SessionLocal() as db:
            owner = self._user(db)
            kb = self._kb(db, owner)
            db.commit()
            kb_id, owner_id, name = kb.id, owner.id, kb.name
        self._simulate_old_schema()
        self.assertNotIn("knowledge_base_members", self._tables())
        self.assertNotIn("membership_revision", self._columns("knowledge_bases"))

        init_db(create_admin=False)
        init_db(create_admin=False)

        self.assertIn("knowledge_base_members", self._tables())
        column = self._columns("knowledge_bases")["membership_revision"]
        self.assertEqual(column["nullable"], "NO")
        self.assertIsNotNone(column["default"])
        self.assertIn("0", column["default"])
        with SessionLocal() as db:
            fresh = db.get(KnowledgeBase, kb_id)
            self.assertIsNotNone(fresh, "老知识库行不得因迁移丢失")
            self.assertEqual(fresh.name, name)
            self.assertEqual(fresh.description, "保留我")
            self.assertEqual(fresh.owner_id, owner_id)
            self.assertEqual(fresh.membership_revision, 0, "既有行必须回填 0")
            self.assertIsNotNone(db.get(User, owner_id))

    def test_raw_sql_insert_without_column_gets_server_default(self):
        with SessionLocal() as db:
            owner = self._user(db)
            kb = self._kb(db, owner)
            db.commit()
            kb_id, owner_id = kb.id, owner.id
        self._simulate_old_schema()
        init_db(create_admin=False)
        # 绕过 ORM 明确写列名：删除列需要 DEFAULT 才能通过；同时验证存量 UPDATE 后不回填。
        with self.engine.begin() as conn:
            conn.execute(
                text("INSERT INTO knowledge_bases "
                     "(id, name, description, collection_name, owner_id, is_public, kind, "
                     " created_at, updated_at) VALUES "
                     "(:id, :name, '', :col, :owner, true, 'upload', now(), now())"),
                {"id": uuid.uuid4(), "name": "raw_" + uuid.uuid4().hex[:10],
                 "col": "raw_col_" + uuid.uuid4().hex[:10], "owner": owner_id},
            )
            row = conn.execute(text(
                "SELECT membership_revision FROM knowledge_bases WHERE id = :id"),
                {"id": kb_id}).scalar()
            raw = conn.execute(text(
                "SELECT membership_revision FROM knowledge_bases WHERE name LIKE 'raw_%' "
                "ORDER BY created_at DESC LIMIT 1")).scalar()
        self.assertEqual(row, 0)
        self.assertEqual(raw, 0)

    def test_member_table_has_composite_pk_cascade_check_and_user_index(self):
        init_db(create_admin=False)
        constraints = self._member_constraints()
        names = {name for name, _, _ in constraints}
        self.assertIn("knowledge_base_members_pkey", names)
        # 两个 FK 都是 CASCADE（confdeltype = 'c'）。
        fk_rules = sorted(rule for _, kind, rule in constraints if kind == "f")
        self.assertEqual(fk_rules, ["c", "c"])
        self.assertTrue(any(kind == "c" for _, kind, _ in constraints),
                        "缺少 role CHECK 约束")
        self.assertIn("ix_knowledge_base_members_user_id", self._member_indexes())
        # 复合主键确实是 (kb_id, user_id) 两列。
        with self.engine.connect() as conn:
            key_columns = conn.execute(text(
                "SELECT a.attname FROM pg_constraint c "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
                "WHERE c.conname = 'knowledge_base_members_pkey' ORDER BY a.attnum"
            )).all()
        self.assertEqual([row[0] for row in key_columns], ["kb_id", "user_id"])
        self.assertTrue(_MEMBER_COLUMNS <= set(self._columns("knowledge_base_members")))

    def test_constraints_reject_bad_role_and_duplicate_member(self):
        with SessionLocal() as db:
            owner = self._user(db)
            member = self._user(db)
            kb = self._kb(db, owner)
            second_kb = self._kb(db, owner)
            db.commit()
            kb_id, owner_id, member_id = kb.id, owner.id, member.id
            second_kb_id = second_kb.id
        init_db(create_admin=False)
        with self.engine.begin() as conn:
            with self.assertRaises(Exception):
                conn.execute(text(
                    "INSERT INTO knowledge_base_members (kb_id, user_id, role) "
                    "VALUES (:kb, :user, 'admin')"),
                    {"kb": kb_id, "user": member_id})
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO knowledge_base_members (kb_id, user_id, role) "
                "VALUES (:kb, :user, 'reader')"), {"kb": kb_id, "user": member_id})
        with self.engine.begin() as conn:
            with self.assertRaises(Exception):
                conn.execute(text(
                    "INSERT INTO knowledge_base_members (kb_id, user_id, role) "
                    "VALUES (:kb, :user, 'maintainer')"), {"kb": kb_id, "user": member_id})
        # 删除 KB / 删除用户都通过 FK CASCADE 清理成员行。
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM knowledge_bases WHERE id = :kb"), {"kb": kb_id})
        with self.engine.connect() as conn:
            self.assertIsNone(conn.execute(text(
                "SELECT 1 FROM knowledge_base_members WHERE kb_id = :kb"), {"kb": kb_id}).first())
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO knowledge_base_members (kb_id, user_id, role) "
                "VALUES (:kb, :user, 'reader'), (:kb, :owner, 'maintainer')"),
                {"kb": second_kb_id, "user": member_id, "owner": owner_id})
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :user"), {"user": owner_id})
        with self.engine.connect() as conn:
            self.assertIsNone(conn.execute(text(
                "SELECT 1 FROM knowledge_base_members WHERE user_id = :user"),
                {"user": owner_id}).first())
            self.assertIsNone(conn.execute(text(
                "SELECT 1 FROM knowledge_base_members WHERE kb_id = :kb"), {"kb": kb_id}).first())

    def test_second_pass_does_not_reset_existing_revision_or_duplicate_columns(self):
        with SessionLocal() as db:
            owner = self._user(db)
            kb = self._kb(db, owner)
            db.commit()
            kb_id = kb.id
        init_db(create_admin=False)
        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE knowledge_bases SET membership_revision = 7 WHERE id = :kb"),
                {"kb": kb_id})
        init_db(create_admin=False)
        init_db(create_admin=False)
        with SessionLocal() as db:
            self.assertEqual(db.get(KnowledgeBase, kb_id).membership_revision, 7)
        columns = self._columns("knowledge_bases")
        self.assertEqual(sum(1 for name in columns if name == "membership_revision"), 1)

    def test_ensure_helpers_are_idempotent_when_called_directly(self):
        with SessionLocal() as db:
            owner = self._user(db)
            kb = self._kb(db, owner)
            db.commit()
            kb_id = kb.id
        for _ in range(3):
            with self.engine.begin() as conn:
                ensure_foundation_schema(conn)
                ensure_kb_membership_schema(conn)
        with SessionLocal() as db:
            self.assertEqual(db.get(KnowledgeBase, kb_id).membership_revision, 0)
        self.assertIn("knowledge_base_members", self._tables())

    def test_helper_skips_absent_tables_without_error(self):
        """迁移 helper 不能假定表一定存在（空库/部分初始化时不抛 UndefinedTable）。"""
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS knowledge_base_members CASCADE"))
            conn.execute(text(
                "ALTER TABLE knowledge_bases DROP COLUMN IF EXISTS membership_revision"))
        with self.engine.begin() as conn:
            ensure_kb_membership_schema(conn)  # 不建表，只补列
            columns = conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'knowledge_bases' "
                "AND column_name = 'membership_revision'")).first()
        self.assertIsNotNone(columns, "helper 应给已有 knowledge_bases 补列")
        self.assertNotIn("knowledge_base_members", self._tables(),
                         "helper 不负责建表（create_all 负责）")
        init_db(create_admin=False)


if __name__ == "__main__":
    unittest.main()