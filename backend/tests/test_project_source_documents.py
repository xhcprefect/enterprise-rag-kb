"""ProjectSource 原文范围读取（checkpoint A）：真实 PG（独占 memory_project_test）。

本文件自身即安全隔离验收：
- 不在导入期改任何环境变量，不硬写 URL / 凭据 / owner marker。
- TEST_DATABASE_URL 与 TEST_DATABASE_OWNER 全部取自启动环境；未配置则整组跳过并说明，
  绝不仅凭 `_test` 后缀授权任何破坏性操作。
- 每次破坏性操作（建表 DDL / 写 fixture）前，先核实际应用 engine.url == 调用方
  TEST_DATABASE_URL，且 PG shobj_description 库 comment == TEST_DATABASE_OWNER；
  坏配置 / 实际 engine 不符 / owner 不符 → 立即硬失败。
- fixture 用“单事务内 flush + tearDown rollback”合成，不用 drop_all，不给共享库留残留。

覆盖（服务层，真实专属库，不 spawn 服务）：
- 凭 file_version UUID + 明确授权 ready 快照映射；源未撤销 / KB ACL / 同 source。
- 未授权 -> ProjectSourceNotFound（404）；源已撤销 -> ProjectSourceRevoked（410）。
- ACL 先于 revoke：私有已 revoke 源对无权限 stranger 抛 404（NotFound），仅授权者见 410。
- 不传 sid：只选实际映射该文件的 ready 快照，旧文件版本绝不冒称 active。
- 显式 sid：必须 ready、同 source、且包含该文件，否则 ProjectSnapshotNotReady。
- 范围有界（48 chunk / 20k 字符），超出明确 truncated，不返回伪完整/垃圾 tail。
- 原文 FV 可受 expected_source_id 绑定：不同源文件在别源路由下读取 -> 404。

运行（用本仓库 venv，先核 owner 再跑；URL/owner 由启动环境提供）：
  TEST_DATABASE_URL=... TEST_DATABASE_OWNER=... python -m unittest \\
      tests.test_project_source_documents -v
"""
from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime, timezone

from tests import _memory_project_test_gate as gate

# 导入期绝不改 env / 不硬写 URL/owner；全部取自已设好的启动环境。
TEST_URL_RAW = gate.TEST_DATABASE_URL
TEST_OWNER_RAW = gate.TARGET_OWNER

# 按既有数据库用例同款：导入 db.session 模块并在使用时核/绑，不从其过早捕获陈旧 engine。
from app.db import session as db_session  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import (  # noqa: E402
    KNOWLEDGE_BASE_KIND_PROJECT,
    KnowledgeBase,
    ProjectChunk,
    ProjectFileParseStatus,
    ProjectFileVersion,
    ProjectSnapshot,
    ProjectSnapshotFile,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
    UserRole,
)
from app.services.project_source_documents import (  # noqa: E402
    ProjectSnapshotNotReady,
    ProjectSourceNotFound,
    ProjectSourceRevoked,
    read_project_source,
    resolve_file_preview_snapshot,
)

engine = db_session.engine


def _verify_test_env() -> None:
    """破坏性测试总开关（共享门）：双缺 -> SkipTest；部分配置/坏 URL/owner 标签格式/
    engine 不符/库 comment 不符 -> 硬失败（ConfigError）。"""
    gate.verify(TEST_URL_RAW, TEST_OWNER_RAW, db_session.engine)


def _kb(name: str, owner: User, *, public: bool = False) -> KnowledgeBase:
    return KnowledgeBase(
        name=name,
        description="",
        collection_name=f"proj_{uuid.uuid4().hex}",
        owner_id=owner.id,
        is_public=public,
        kind=KNOWLEDGE_BASE_KIND_PROJECT,
    )


def _snap(source: ProjectSource, status: str = ProjectSnapshotStatus.READY.value) -> ProjectSnapshot:
    return ProjectSnapshot(
        source_id=source.id, status=status, progress=0, file_count=0, total_bytes=0
    )


def _fv(source: ProjectSource, path: str) -> ProjectFileVersion:
    return ProjectFileVersion(
        source_id=source.id,
        relative_path=path,
        content_sha256=uuid.uuid4().hex,
        blob_path=f"blob-{uuid.uuid4().hex}",
        size=0,
        language="python",
        parse_status=ProjectFileParseStatus.READY.value,
        parse_done=True,
        chunk_count=0,
        char_count=0,
    )


def _chunk(fv: ProjectFileVersion, index: int, content: str, ls: int, le: int) -> ProjectChunk:
    return ProjectChunk(
        file_version_id=fv.id,
        chunk_index=index,
        content=content,
        search_text=content,
        source_metadata={},
        line_start=ls,
        line_end=le,
        embedded=True,
    )


class _RollbackFixtureBase(unittest.TestCase):
    """单事务 rollback fixture：每次破坏性操作前核实际 URL+owner，fixture 不提交。

    每个用例开一个自己的链接事务，add 后 flush（本会话可见），tearDown rollback，
    不给共享专属库留任何残留，也不必 drop_all。
    """

    def setUp(self):
        _verify_test_env()  # 破坏性（建表/写 fixture）前的总开关：核 URL + owner
        Base.metadata.create_all(bind=engine)
        SessionLocal = db_session.SessionLocal
        self.db = SessionLocal(autoflush=True)
        self.addCleanup(self._teardown_db)

        # 每个用例用唯一名字：既不会与历史已提交行（旧版格式遗留）撞唯一约束，
        # 也无需 drop_all/TRUNCATE 清表——fixture 走单事务 rollback 不留新残留。
        self._tag = uuid.uuid4().hex[:12]
        self.admin = User(username=f"q-admin-{self._tag}", role=UserRole.ADMIN.value, password_hash="x")
        self.owner = User(username=f"q-owner-{self._tag}", role=UserRole.USER.value, password_hash="x")
        self.stranger = User(username=f"q-stranger-{self._tag}", role=UserRole.USER.value, password_hash="x")
        self.db.add_all([self.admin, self.owner, self.stranger])
        self.db.flush()

        self.kb = _kb(f"qsource-demo-{self._tag}", self.owner, public=False)
        self.db.add(self.kb)
        self.db.flush()

        self.source = ProjectSource(
            kb_id=self.kb.id,
            name=f"qsource-demo-{self._tag}",
            root_key="default",
            relative_dir="src",
            root_fingerprint="f" * 64,
            created_by=self.owner.id,
        )
        self.db.add(self.source)
        self.db.flush()

        # 两个 ready 快照：S1（旧）与 active S2。
        self.s1 = _snap(self.source)
        self.s2 = _snap(self.source)
        self.s1.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.s2.created_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
        self.db.add_all([self.s1, self.s2])
        self.db.flush()

        # FV1 只在旧快照 S1 中；active S2 不包含它（旧文件版本）。
        self.fv1 = _fv(self.source, "legacy/old.py")
        self.db.add(self.fv1)
        self.db.flush()
        self.db.add(ProjectSnapshotFile(snapshot_id=self.s1.id, relative_path="legacy/old.py", file_version_id=self.fv1.id))
        self.db.add(_chunk(self.fv1, 0, "def legacy():\n    return 1\n", 1, 2))

        # FV2 在 active S2（也复用在 S1）。
        self.fv2 = _fv(self.source, "src/new.py")
        self.db.add(self.fv2)
        self.db.flush()
        self.db.add(ProjectSnapshotFile(snapshot_id=self.s2.id, relative_path="src/new.py", file_version_id=self.fv2.id))
        self.db.add(_chunk(self.fv2, 0, "def new():\n    return 2\n", 1, 2))

        self.source.active_snapshot_id = self.s2.id
        self.db.add(self.source)
        self.db.flush()

    def _teardown_db(self):
        # 单事务 rollback：放弃所有未提交 fixture，不给共享库留残留。
        try:
            self.db.rollback()
        finally:
            self.db.close()

    def seed_many_chunks(self, count=60, content_len=300):
        """清空 FV1 的分块，改造出一个多分块 / 超长文件版本（仍只在 S1）。"""
        self.db.execute(ProjectChunk.__table__.delete().where(ProjectChunk.file_version_id == self.fv1.id))
        for i in range(count):
            self.db.add(_chunk(self.fv1, i, f"line{i}\n" + ("x" * content_len), i * 10 + 1, i * 10 + 10))
        self.fv1.chunk_count = count
        self.fv1.char_count = count * (content_len + 6)
        self.db.flush()


class ProjectPreviewPgTests(_RollbackFixtureBase):
    # ---- 授权：未授权 404，撤销 410（ACL 先于 revoke）----

    def test_stranger_is_denied_404(self):
        with self.assertRaises(ProjectSourceNotFound):
            resolve_file_preview_snapshot(self.db, self.fv1.id, self.stranger, snapshot_id=self.s1.id)
        with self.assertRaises(ProjectSourceNotFound):
            read_project_source(self.db, self.fv2.id, self.stranger)

    def test_admin_and_owner_are_allowed(self):
        fv_, snap = resolve_file_preview_snapshot(self.db, self.fv2.id, self.owner)
        self.assertEqual(snap.id, self.s2.id)
        _fv2, snap_a = resolve_file_preview_snapshot(self.db, self.fv2.id, self.admin)
        self.assertEqual(snap_a.id, self.s2.id)
        self.assertEqual(fv_.id, self.fv2.id)
        self.assertEqual(_fv2.id, self.fv2.id)

    def test_revoked_source_authorized_is_410(self):
        # 授权（owner/admin）读到已 revoke 源 -> 410。
        self.source.revoked_at = datetime.now(timezone.utc)
        self.db.flush()
        with self.assertRaises(ProjectSourceRevoked):
            read_project_source(self.db, self.fv1.id, self.owner)
        with self.assertRaises(ProjectSourceRevoked):
            read_project_source(self.db, self.fv1.id, self.admin)

    def test_revoked_private_source_stranger_is_404_not_410(self):
        # ACL 先于 revoke：私有已 revoke 源对无权限 stranger 必须 404，而非 410。
        self.source.revoked_at = datetime.now(timezone.utc)
        self.db.flush()
        with self.assertRaises(ProjectSourceNotFound):
            read_project_source(self.db, self.fv1.id, self.stranger)
        with self.assertRaises(ProjectSourceNotFound):
            resolve_file_preview_snapshot(self.db, self.fv1.id, self.stranger, snapshot_id=self.s1.id)

    # ---- 快照映射：旧版本不冒称当前，只取实际映射 ----

    def test_no_sid_old_file_resolves_to_its_real_snapshot_not_active(self):
        # active S2 不含 FV1；不得把 active 当 FV1 的当前快照返回。
        fv_, snap = resolve_file_preview_snapshot(self.db, self.fv1.id, self.owner)
        self.assertEqual(snap.id, self.s1.id, "旧文件必须解析到实际映射它的快照 S1，而非 active S2")

    def test_no_sid_current_file_resolves_to_active(self):
        _fv, snap = resolve_file_preview_snapshot(self.db, self.fv2.id, self.owner)
        self.assertEqual(snap.id, self.s2.id)

    def test_explicit_sid_old_snapshot_reads_old_original(self):
        result = read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=self.s1.id)
        self.assertEqual(result.snapshot_id, self.s1.id)
        self.assertEqual(result.relative_path, "legacy/old.py")
        self.assertEqual(result.language, "python")
        self.assertEqual(result.parse_status, "ready")
        self.assertEqual(len(result.chunks), 1)
        self.assertIn("legacy", result.chunks[0].content)

    def test_explicit_ready_sid_not_mapping_file_is_rejected(self):
        # S2 ready 但 FV1 不在其中：同 source 也不得通过。
        with self.assertRaises(ProjectSnapshotNotReady):
            read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=self.s2.id)

    def test_explicit_sid_from_another_source_is_rejected(self):
        other_kb = _kb("qsource-other", self.owner)
        self.db.add(other_kb)
        self.db.flush()
        other_source = ProjectSource(
            kb_id=other_kb.id, name="qsource-other", root_key="default",
            relative_dir=".", root_fingerprint="e" * 64, created_by=self.owner.id,
        )
        self.db.add(other_source)
        self.db.flush()
        other_snap = _snap(other_source)
        self.db.add(other_snap)
        self.db.flush()
        other_fv = _fv(other_source, "other.py")
        self.db.add(other_fv)
        self.db.flush()
        self.db.add(ProjectSnapshotFile(snapshot_id=other_snap.id, relative_path="other.py", file_version_id=other_fv.id))
        self.db.flush()
        # 用 FV1 查别的源的快照：不同源 -> 拒绝。
        with self.assertRaises(ProjectSnapshotNotReady):
            read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=other_snap.id)

    def test_explicit_non_ready_sid_is_rejected(self):
        pending = _snap(self.source, ProjectSnapshotStatus.PENDING.value)
        self.db.add(pending)
        self.db.flush()
        with self.assertRaises(ProjectSnapshotNotReady):
            read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=pending.id)

    # ---- 原文 FV 与路由 source 绑定（expected_source_id）----

    def test_fv_must_bind_to_expected_route_source_else_404(self):
        # FV1 属于 self.source；用另一个源的 id 作 expected_source_id -> 404（不同路由）。
        other = uuid.uuid4()
        with self.assertRaises(ProjectSourceNotFound):
            read_project_source(
                self.db, self.fv1.id, self.owner, snapshot_id=self.s1.id,
                expected_source_id=other,
            )

    def test_fv_binds_ok_to_its_own_route_source(self):
        result = read_project_source(
            self.db, self.fv1.id, self.owner, snapshot_id=self.s1.id,
            expected_source_id=self.source.id,
        )
        self.assertEqual(result.file_version_id, self.fv1.id)
        self.assertEqual(result.source_id, self.source.id)

    # ---- 原文范围有界 + truncated ----

    def test_preview_is_bounded_to_48_chunks(self):
        self.seed_many_chunks(count=60)
        result = read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=self.s1.id)
        self.assertLessEqual(len(result.chunks), 48)
        # 48 chunk 远小于 20k 字符（每块约 306B）→ 触达 chunk 上限而非字符上限。
        self.assertEqual(result.chunk_end, result.chunks[-1].chunk_index)
        self.assertLessEqual(result.chunk_end - result.chunk_start + 1, 48)

    def test_preview_truncates_at_char_cap_explicitly(self):
        self.seed_many_chunks(count=2, content_len=30000)
        result = read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=self.s1.id)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.chunks), 1)
        self.assertEqual(len(result.chunks[0].content), 20000)  # 首块被裁剪到 MAX_PREVIEW_CHARS
        self.assertNotIn("00", result.chunks[0].content)  # 无垃圾 tail（裁剪后不足标志位长度）

    def test_preview_char_cap_marks_truncated_with_multiple_chunks(self):
        # 每块 ~1500B，10 块 ~15000 在限内；再造超限 >20k 的组合。
        self.seed_many_chunks(count=3, content_len=8000)  # 每块约 8006B 共 ~24000 > 20000
        result = read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=self.s1.id)
        self.assertTrue(result.truncated)
        total = sum(len(c.content) for c in result.chunks)
        self.assertLessEqual(total, 20000)

    def test_source_path_line_are_db_trusted(self):
        self.seed_many_chunks(count=1, content_len=5)
        result = read_project_source(self.db, self.fv1.id, self.owner, snapshot_id=self.s1.id)
        self.assertEqual(result.source_id, self.source.id)
        self.assertEqual(result.relative_path, "legacy/old.py")
        self.assertEqual(result.chunks[0].line_start, 1)
        self.assertEqual(result.chunks[0].line_end, 10)
        self.assertEqual(result.chunk_end, result.chunks[0].chunk_index)


class NoConfigIsolationTests(unittest.TestCase):
    """无配置隔离验收：不改 env、不连接、整组 skip（由真实运行的三态演示佐证）。

    这里只验证校验函数在无 URL 时的行为（不会对真实库做任何破坏性操作）。
    """

    def test_verify_skips_when_no_url_no_owner(self):
        prev_url = os.environ.get("TEST_DATABASE_URL")
        prev_owner = os.environ.get("TEST_DATABASE_OWNER")
        try:
            os.environ.pop("TEST_DATABASE_URL", None)
            os.environ.pop("TEST_DATABASE_OWNER", None)
            # 直接调用同样的校验闸门：改不了模块级常量，故用参数化副本断言行为。
            from unittest import SkipTest
            with self.assertRaises(SkipTest):
                _verify_gate("", "")
        finally:
            _restore_env(prev_url, prev_owner)

    def test_verify_hard_fails_when_url_mismatch_engine(self):
        # 坏配置：提供了 URL/owner，但 engine.url 不是它 -> 硬失败（不重绑、不连接 prod）。
        prev_url = os.environ.get("TEST_DATABASE_URL")
        prev_owner = os.environ.get("TEST_DATABASE_OWNER")
        try:
            os.environ.pop("TEST_DATABASE_URL", None)
            os.environ.pop("TEST_DATABASE_OWNER", None)
            with self.assertRaises(RuntimeError):
                _verify_gate("postgresql+psycopg://u:p@127.0.0.1:1/whatever_test", "m:1")
        finally:
            _restore_env(prev_url, prev_owner)


def _verify_gate(url: str, owner: str) -> None:
    """等价校验闸门：双缺 skip、部分/坏配置/owner 标签/engine 不符硬失败。

    直接复用共享门 ``_memory_project_test_gate``（纯配置部分不连接），保证本文件的
    离线断言与实际破坏性测试走同一套判定；真实破坏性测试仍走 ``_verify_test_env``。
    """
    gate.verify(url, owner, db_session.engine)


def _restore_env(url, owner) -> None:
    if url is None:
        os.environ.pop("TEST_DATABASE_URL", None)
    else:
        os.environ["TEST_DATABASE_URL"] = url
    if owner is None:
        os.environ.pop("TEST_DATABASE_OWNER", None)
    else:
        os.environ["TEST_DATABASE_OWNER"] = owner


if __name__ == "__main__":
    unittest.main(verbosity=2)
