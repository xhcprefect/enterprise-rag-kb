"""可选定时自动同步：纯函数单测 + 真实隔离 PostgreSQL 验收。

纯函数部分（不触 DB）：间隔硬界收敛、固定安全错误码/文案。
数据库部分只在显式 owned ``*_test`` 库运行（与 test_project_pipeline 相同的门），
覆盖：PATCH 开关/权限/410/404、SKIP LOCKED 领取、幂等入队、首次 auto ready、
第二次 unchanged（不解析/不嵌入）、历史默认过滤、空清单保护、关闭自动同步只取消
auto 任务、撤销/取消不误报成功、失败只写安全错误码。

未配置 TEST_DATABASE_URL / TEST_DATABASE_OWNER 时整类 SkipTest，不连接任何库。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings
from sqlalchemy import select, update

from tests import _memory_project_test_gate as gate

from app.core.config import settings
from app.core.security import create_access_token
from app.db import session as db_session
from app.db.init_db import init_db
from app.models import (
    KnowledgeBase,
    ProjectSnapshot,
    ProjectSnapshotStatus,
    ProjectSource,
    User,
)
from app.services import (
    project_auto_sync,
    project_index,
    project_sources,
    vector_store,
)
from app.services.project_types import ScanReport
from app.schemas.project import (
    AUTO_SYNC_DEFAULT_INTERVAL_SECONDS,
    AUTO_SYNC_MAX_INTERVAL_SECONDS,
    AUTO_SYNC_MIN_INTERVAL_SECONDS,
)


class FixtureEmbeddings(Embeddings):
    """确定性替身：不调用任何付费/本地模型。"""

    def embed_documents(self, texts):
        return [[1.0] + [0.0] * (vector_store.EMBEDDING_DIM - 1) for _ in texts]

    def embed_query(self, text):
        return [1.0] + [0.0] * (vector_store.EMBEDDING_DIM - 1)


# ---------------- 纯函数单测（无 DB / 无模型） ----------------

class AutoSyncUnitTests(unittest.TestCase):
    def test_interval_clamp_uses_contract_bounds(self) -> None:
        self.assertEqual(AUTO_SYNC_DEFAULT_INTERVAL_SECONDS, 300)
        self.assertEqual(AUTO_SYNC_MIN_INTERVAL_SECONDS, 60)
        self.assertEqual(AUTO_SYNC_MAX_INTERVAL_SECONDS, 86400)
        cases = {
            0: 60,
            -5: 60,
            59: 60,
            60: 60,
            300: 300,
            86400: 86400,
            99999: 86400,
            "bad": AUTO_SYNC_DEFAULT_INTERVAL_SECONDS,
            None: AUTO_SYNC_DEFAULT_INTERVAL_SECONDS,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(project_auto_sync._clamp_interval(raw), expected)

    def test_non_positive_limit_short_circuits_without_db(self) -> None:
        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=0), 0)
        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=-3), 0)

    def test_fixed_error_codes_and_messages(self) -> None:
        self.assertEqual(project_auto_sync.ENQUEUE_ERROR_CODE, "auto-enqueue-failed")
        self.assertEqual(project_auto_sync.ENQUEUE_ERROR_MESSAGE, "自动同步入队失败")
        self.assertEqual(
            project_sources.AUTO_SYNC_CANCELLED_CODE, "cancelled-auto-disabled"
        )
        self.assertEqual(
            project_sources.AUTO_SYNC_CANCELLED_MESSAGE, "自动同步已关闭，任务取消"
        )
        self.assertEqual(project_index.AUTO_EMPTY_CODE, "auto-empty")
        self.assertEqual(project_index.AUTO_UNCHANGED_CODE, "unchanged")
        self.assertEqual(project_index.AUTO_FAILED_CODE, "auto-sync-failed")
        self.assertEqual(
            project_index.AUTO_FAILED_MESSAGE, "自动同步失败，请查看同步历史"
        )

    def test_auto_stability_retry_seconds_quiet_window(self) -> None:
        now = time.time_ns()
        recent = ScanReport(newest_mtime_ns=now - 2_000_000_000)  # 2 秒前刚写入
        old = ScanReport(newest_mtime_ns=now - 3_600_000_000_000)  # 1 小时前
        future = ScanReport(newest_mtime_ns=now + 60_000_000_000)  # 远未来（时钟偏差）
        empty = ScanReport(newest_mtime_ns=None)

        with patch.object(settings, "PROJECT_AUTO_SYNC_QUIET_SECONDS", 0):
            # window=0 表示关闭去抖。
            self.assertIsNone(project_index._auto_stability_retry_seconds(recent))

        with patch.object(settings, "PROJECT_AUTO_SYNC_QUIET_SECONDS", 30):
            self.assertIsNone(project_index._auto_stability_retry_seconds(old))
            self.assertIsNone(project_index._auto_stability_retry_seconds(empty))
            retry = project_index._auto_stability_retry_seconds(recent)
            self.assertIsNotNone(retry)
            self.assertGreaterEqual(retry, 1)
            self.assertLessEqual(retry, 30)
            # 远未来 mtime 不参与去抖，避免自动同步被永久饿死。
            self.assertIsNone(project_index._auto_stability_retry_seconds(future))


# ---------------- 真实隔离数据库验收 ----------------

@unittest.skipIf(
    gate.classify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER) == "missing_both",
    "需要显式 owned PostgreSQL 测试库",
)
class ProjectAutoSyncDBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        init_db(create_admin=False)
        with patch("app.core.logging.setup_logging"):
            from app.main import app
        cls.app = app

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, db_session.engine)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.notes = self.root / "notes.md"
        self.notes.write_text(
            "# 项目说明\n\nAUTO_SYNC_MARKER：后台任务执行规则。\n", encoding="utf-8"
        )
        self.embeddings = FixtureEmbeddings()
        for patcher in (
            patch.object(settings, "PROJECT_ALLOWED_ROOTS", {"fixture": str(self.root)}),
            patch.object(settings, "PROJECT_STORAGE_DIR", str(Path(self.temp.name) / "blobs")),
            patch.object(settings, "PROJECT_AUTO_SYNC_QUIET_SECONDS", 0),
            patch.object(settings, "RETRIEVAL_REWRITE_ENABLED", False),
            patch.object(settings, "HYBRID_RETRIEVAL_ENABLED", False),
            patch.object(settings, "RERANK_ENABLED", False),
            patch.object(vector_store, "_vector_engine", return_value=db_session.engine),
            patch.object(vector_store, "get_embeddings", return_value=self.embeddings),
            patch.object(project_index, "get_embeddings", return_value=self.embeddings),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        vector_store._store.cache_clear()
        self.addCleanup(vector_store._store.cache_clear)

        with db_session.SessionLocal() as db:
            self.user = User(
                username="auto_" + uuid.uuid4().hex,
                role="admin",
                is_active=True,
                password_hash="test-only-no-login",
            )
            db.add(self.user)
            db.flush()
            source, kb = project_sources.create_project_source(
                db,
                self.user,
                name="auto_" + uuid.uuid4().hex,
                root_key="fixture",
                relative_dir="",
            )
            db.commit()
            self.user_id = self.user.id
            self.source_id = source.id
            self.kb_id = kb.id
            self.kb_ids = [kb.id]
            self.collections = [kb.collection_name]

        self.addCleanup(self._cleanup_rows)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {
            "Authorization": "Bearer " + create_access_token(str(self.user_id), "admin")
        }

    # ---- fixture / helpers ----
    def _cleanup_rows(self) -> None:
        try:
            with db_session.SessionLocal() as db:
                db.execute(
                    ProjectSource.__table__.delete().where(
                        ProjectSource.id == self.source_id
                    )
                )
                if self.kb_ids:
                    db.execute(
                        KnowledgeBase.__table__.delete().where(
                            KnowledgeBase.id.in_(self.kb_ids)
                        )
                    )
                db.execute(User.__table__.delete().where(User.id == self.user_id))
                db.commit()
        finally:
            for collection in self.collections:
                try:
                    vector_store.get_vector_store(collection).delete_collection()
                except Exception:  # noqa: BLE001 - 清理尽力而为
                    pass

    def _make_source(self, relative_dir: str = "") -> uuid.UUID:
        with db_session.SessionLocal() as db:
            source, kb = project_sources.create_project_source(
                db,
                db.get(User, self.user_id),
                name="auto_extra_" + uuid.uuid4().hex,
                root_key="fixture",
                relative_dir=relative_dir,
            )
            db.commit()
            self.kb_ids.append(kb.id)
            self.collections.append(kb.collection_name)
            return source.id

    def _patch_auto(self, source_id: uuid.UUID, enabled: bool, interval: int | None = None):
        body = {"enabled": enabled}
        if interval is not None:
            body["interval_seconds"] = interval
        return self.client.patch(
            f"/api/v1/projects/{source_id}/auto-sync", headers=self.headers, json=body
        )

    def _force_due(self, source_id: uuid.UUID) -> None:
        with db_session.SessionLocal() as db:
            db.execute(
                update(ProjectSource)
                .where(ProjectSource.id == source_id)
                .values(
                    auto_sync_next_check_at=datetime.now(timezone.utc)
                    - timedelta(seconds=1)
                )
            )
            db.commit()

    def _claim_and_run(self, expected_source_id: uuid.UUID):
        claim = project_index.claim_next_project_snapshot()
        self.assertIsNotNone(claim)
        self.assertEqual(claim.source_id, expected_source_id)
        status = project_index.run_project_sync(claim, threading.Event())
        return claim.snapshot_id, status

    def _sync_manual(self, source_id: uuid.UUID | None = None):
        source_id = source_id or self.source_id
        with db_session.SessionLocal() as db:
            snap = project_index.enqueue_project_sync(
                db, source_id, db.get(User, self.user_id)
            )
            db.commit()
            snapshot_id = snap.id
        claimed_id, status = self._claim_and_run(source_id)
        self.assertEqual(claimed_id, snapshot_id)
        return snapshot_id, status

    def _run_due_auto(self, source_id: uuid.UUID):
        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 1)
        return self._claim_and_run(source_id)

    # ---- PATCH：权限/校验/404/410/禁用 ----
    def test_patch_auto_sync_admin_validation_404_and_410(self):
        with db_session.SessionLocal() as db:
            viewer = User(
                username="auto_viewer_" + uuid.uuid4().hex,
                role="user",
                is_active=True,
                password_hash="test-only",
            )
            db.add(viewer)
            db.commit()
            viewer_id = viewer.id
        viewer_headers = {
            "Authorization": "Bearer " + create_access_token(str(viewer_id), "user")
        }
        try:
            denied = self.client.patch(
                f"/api/v1/projects/{self.source_id}/auto-sync",
                headers=viewer_headers,
                json={"enabled": True, "interval_seconds": 120},
            )
            self.assertEqual(denied.status_code, 403, denied.text)
        finally:
            with db_session.SessionLocal() as db:
                db.execute(User.__table__.delete().where(User.id == viewer_id))
                db.commit()

        for interval in (AUTO_SYNC_MIN_INTERVAL_SECONDS - 1, AUTO_SYNC_MAX_INTERVAL_SECONDS + 1):
            with self.subTest(interval=interval):
                invalid = self._patch_auto(self.source_id, True, interval)
                self.assertEqual(invalid.status_code, 422, invalid.text)

        missing = self._patch_auto(uuid.uuid4(), True, 120)
        self.assertEqual(missing.status_code, 404, missing.text)

        enabled = self._patch_auto(self.source_id, True, 120)
        self.assertEqual(enabled.status_code, 200, enabled.text)
        data = enabled.json()["data"]
        self.assertTrue(data["auto_sync_enabled"])
        self.assertEqual(data["auto_sync_interval_seconds"], 120)
        self.assertIsNotNone(data["auto_sync_next_check_at"])
        first_next = datetime.fromisoformat(data["auto_sync_next_check_at"])

        # 已启用且改间隔：next_check_at 重新按新间隔顺延。
        changed = self._patch_auto(self.source_id, True, 600)
        self.assertEqual(changed.status_code, 200, changed.text)
        changed_data = changed.json()["data"]
        self.assertEqual(changed_data["auto_sync_interval_seconds"], 600)
        changed_next = datetime.fromisoformat(changed_data["auto_sync_next_check_at"])
        self.assertGreater(changed_next, first_next)

        with db_session.SessionLocal() as db:
            source = db.get(ProjectSource, self.source_id)
            source.auto_sync_last_error_code = "previous"
            source.auto_sync_last_error_message = "previous-message"
            db.commit()

        disabled = self._patch_auto(self.source_id, False)
        self.assertEqual(disabled.status_code, 200, disabled.text)
        data = disabled.json()["data"]
        self.assertFalse(data["auto_sync_enabled"])
        self.assertIsNone(data["auto_sync_next_check_at"])
        self.assertIsNone(data["auto_sync_last_error_code"])
        self.assertIsNone(data["auto_sync_last_error_message"])
        # 禁用后调度不再入队。
        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 0)

        revoked = self.client.delete(
            f"/api/v1/projects/{self.source_id}", headers=self.headers
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        gone = self._patch_auto(self.source_id, True, 120)
        self.assertEqual(gone.status_code, 410, gone.text)

    # ---- 到期领取：SKIP LOCKED / 幂等 / 撤销 ----
    def test_due_claim_skip_locked_idempotent_and_revoked(self):
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)

        # 另一个 worker 持有源行锁时，SKIP LOCKED 必须跳过而不是阻塞/重复领取。
        with db_session.SessionLocal() as db:
            db.execute(
                select(ProjectSource)
                .where(ProjectSource.id == self.source_id)
                .with_for_update()
            )
            self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 0)
            db.rollback()

        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 1)
        # 领取时已推进 next_check_at：同轮再次调用不会再入队。
        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 0)

        with db_session.SessionLocal() as db:
            active = db.scalars(
                select(ProjectSnapshot).where(
                    ProjectSnapshot.source_id == self.source_id,
                    ProjectSnapshot.status.in_(
                        [
                            ProjectSnapshotStatus.PENDING.value,
                            ProjectSnapshotStatus.PROCESSING.value,
                        ]
                    ),
                )
            ).all()
            self.assertEqual(len(active), 1, "同源不得出现多个活动同步")
            self.assertEqual(active[0].trigger_source, "auto")
            active_id = active[0].id

        # 直接 enqueue 也必须复用同一活动任务（重启/并发反复调度不重复建）。
        with db_session.SessionLocal() as db:
            again = project_index.enqueue_project_sync(
                db, self.source_id, None, trigger_source="auto"
            )
            db.commit()
            self.assertEqual(again.id, active_id)
            self.assertEqual(again.trigger_source, "auto")

        cancelled = self.client.post(
            f"/api/v1/projects/{self.source_id}/sync/cancel", headers=self.headers
        )
        self.assertEqual(cancelled.status_code, 200, cancelled.text)

        # 撤销后即使 next_check 到期也不得再被领取。
        with db_session.SessionLocal() as db:
            db.execute(
                update(ProjectSource)
                .where(ProjectSource.id == self.source_id)
                .values(
                    auto_sync_enabled=True,
                    auto_sync_next_check_at=datetime.now(timezone.utc)
                    - timedelta(seconds=1),
                )
            )
            db.commit()
        revoked = self.client.delete(
            f"/api/v1/projects/{self.source_id}", headers=self.headers
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 0)

    # ---- 入队异常：安全码 + 不中断其它源 ----
    def test_enqueue_exception_records_safe_error_and_continues(self):
        failing_id = self.source_id
        healthy_id = self._make_source()
        for source_id in (failing_id, healthy_id):
            self.assertEqual(self._patch_auto(source_id, True, 300).status_code, 200)

        real_enqueue = project_index.enqueue_project_sync

        def flaky_enqueue(
            db,
            source_id,
            user=None,
            *,
            trigger_source="manual",
            require_auto_sync_enabled=False,
        ):
            if source_id == failing_id:
                raise RuntimeError("enqueue failed: C:/very/secret/path")
            return real_enqueue(
                db,
                source_id,
                user,
                trigger_source=trigger_source,
                require_auto_sync_enabled=require_auto_sync_enabled,
            )

        with patch.object(
            project_auto_sync.project_index, "enqueue_project_sync",
            side_effect=flaky_enqueue,
        ):
            self.assertEqual(project_auto_sync.enqueue_due_sources(limit=2), 1)

        with db_session.SessionLocal() as db:
            failing = db.get(ProjectSource, failing_id)
            healthy = db.get(ProjectSource, healthy_id)
            self.assertEqual(
                failing.auto_sync_last_error_code,
                project_auto_sync.ENQUEUE_ERROR_CODE,
            )
            self.assertEqual(
                failing.auto_sync_last_error_message,
                project_auto_sync.ENQUEUE_ERROR_MESSAGE,
            )
            self.assertNotIn(str(self.root), failing.auto_sync_last_error_message or "")
            self.assertNotIn("secret", failing.auto_sync_last_error_message or "")
            self.assertIsNone(healthy.auto_sync_last_error_code)
        with db_session.SessionLocal() as db:
            pending = db.scalars(
                select(ProjectSnapshot).where(
                    ProjectSnapshot.source_id == healthy_id,
                    ProjectSnapshot.status == ProjectSnapshotStatus.PENDING.value,
                )
            ).all()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].trigger_source, "auto")

    # ---- 入队前窗口：源行锁内复核 auto_sync_enabled ----
    def test_enqueue_requires_auto_sync_enabled_inside_source_lock(self):
        # 默认关闭的新源：内部要求启用时入队必须拒绝，且不产生任何快照/触发变更。
        with db_session.SessionLocal() as db:
            with self.assertRaises(project_sources.AutoSyncDisabled):
                project_index.enqueue_project_sync(
                    db,
                    self.source_id,
                    None,
                    trigger_source="auto",
                    require_auto_sync_enabled=True,
                )
            db.rollback()
        with db_session.SessionLocal() as db:
            snapshots = db.scalars(
                select(ProjectSnapshot).where(
                    ProjectSnapshot.source_id == self.source_id
                )
            ).all()
            self.assertEqual(snapshots, [])

        # 启用后同一参数可正常入队（正向路径不回归），随后清理活动任务。
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)
        with db_session.SessionLocal() as db:
            snap = project_index.enqueue_project_sync(
                db,
                self.source_id,
                None,
                trigger_source="auto",
                require_auto_sync_enabled=True,
            )
            db.commit()
            snapshot_id = snap.id
        with db_session.SessionLocal() as db:
            snapshot = db.get(ProjectSnapshot, snapshot_id)
            self.assertEqual(snapshot.status, ProjectSnapshotStatus.PENDING.value)
            self.assertEqual(snapshot.trigger_source, "auto")
        cleanup = self.client.post(
            f"/api/v1/projects/{self.source_id}/sync/cancel", headers=self.headers
        )
        self.assertEqual(cleanup.status_code, 200, cleanup.text)

    def test_scheduler_race_disable_before_real_enqueue_creates_no_snapshot(self):
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)
        real_enqueue = project_index.enqueue_project_sync

        def disable_then_enqueue(
            db,
            source_id,
            user=None,
            *,
            trigger_source="manual",
            require_auto_sync_enabled=False,
        ):
            # 模拟竞态：调度器已领取到期源，管理员在真正入队前关闭自动同步。
            with db_session.SessionLocal() as other:
                project_sources.update_auto_sync(
                    other, source_id, enabled=False, interval_seconds=300
                )
                other.commit()
            return real_enqueue(
                db,
                source_id,
                user,
                trigger_source=trigger_source,
                require_auto_sync_enabled=require_auto_sync_enabled,
            )

        with patch.object(
            project_auto_sync.project_index,
            "enqueue_project_sync",
            side_effect=disable_then_enqueue,
        ):
            self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 0)

        with db_session.SessionLocal() as db:
            snapshots = db.scalars(
                select(ProjectSnapshot).where(
                    ProjectSnapshot.source_id == self.source_id
                )
            ).all()
            self.assertEqual(snapshots, [], "关闭竞态下不得自动建快照")
            source = db.get(ProjectSource, self.source_id)
            self.assertFalse(source.auto_sync_enabled)
            self.assertIsNone(source.auto_sync_next_check_at)
            self.assertIsNone(source.auto_sync_last_error_code)
            self.assertIsNone(source.auto_sync_last_error_message)

    # ---- 首次 ready；第二次 unchanged；历史默认过滤 ----
    def test_first_auto_ready_then_unchanged_and_history_filter(self):
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)

        first_id, status = self._run_due_auto(self.source_id)
        self.assertEqual(status, "ready")
        with db_session.SessionLocal() as db:
            first = db.get(ProjectSnapshot, first_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(first.status, ProjectSnapshotStatus.READY.value)
            self.assertEqual(first.trigger_source, "auto")
            self.assertEqual(source.active_snapshot_id, first_id)
            self.assertIsNone(source.auto_sync_last_error_code)

        self._force_due(self.source_id)
        with patch.object(project_index, "_process_one_file") as process, \
             patch.object(project_index, "_embed_file") as embed:
            second_id, status = self._run_due_auto(self.source_id)
        process.assert_not_called()
        embed.assert_not_called()
        self.assertEqual(status, "unchanged")

        with db_session.SessionLocal() as db:
            second = db.get(ProjectSnapshot, second_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(second.status, ProjectSnapshotStatus.UNCHANGED.value)
            self.assertEqual(second.error_code, project_index.AUTO_UNCHANGED_CODE)
            self.assertEqual(second.file_count, 1)
            # active 不推进，旧版本继续可用。
            self.assertEqual(source.active_snapshot_id, first_id)
            self.assertIsNone(source.auto_sync_last_error_code)
            self.assertIsNone(source.auto_sync_last_error_message)

        default_list = self.client.get(
            f"/api/v1/projects/{self.source_id}/snapshots", headers=self.headers
        )
        self.assertEqual(default_list.status_code, 200, default_list.text)
        default_ids = {item["id"] for item in default_list.json()["data"]}
        self.assertIn(str(first_id), default_ids)
        self.assertNotIn(str(second_id), default_ids)

        full_list = self.client.get(
            f"/api/v1/projects/{self.source_id}/snapshots",
            headers=self.headers,
            params={"include_unchanged": "true"},
        )
        self.assertEqual(full_list.status_code, 200, full_list.text)
        full = {item["id"]: item for item in full_list.json()["data"]}
        self.assertIn(str(second_id), full)
        self.assertEqual(full[str(second_id)]["status"], "unchanged")
        self.assertEqual(full[str(second_id)]["trigger_source"], "auto")
        self.assertEqual(full[str(first_id)]["trigger_source"], "auto")

    # ---- 安静窗口：刚写入的文件延后重试，绝不发布半成品 ----
    def test_auto_quiet_window_defers_until_file_stable(self):
        ready_id, status = self._sync_manual()
        self.assertEqual(status, "ready")
        # 模拟编辑器分步保存：文件刚被写入，mtime 仍在安静窗口内。
        self.notes.write_text(
            "# 项目说明\n\nAUTO_SYNC_MARKER：安静窗口后的稳定内容。\n",
            encoding="utf-8",
        )
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)

        with patch.object(settings, "PROJECT_AUTO_SYNC_QUIET_SECONDS", 30), \
             patch.object(project_index, "_process_one_file") as process, \
             patch.object(project_index, "_embed_file") as embed:
            deferred_id, status = self._run_due_auto(self.source_id)
            self.assertEqual(status, "pending")
            # 半成品绝不进入解析/嵌入。
            process.assert_not_called()
            embed.assert_not_called()

        with db_session.SessionLocal() as db:
            deferred = db.get(ProjectSnapshot, deferred_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(deferred.status, ProjectSnapshotStatus.PENDING.value)
            self.assertEqual(deferred.trigger_source, "auto")
            self.assertIsNone(deferred.lease_id)
            self.assertIsNone(deferred.heartbeat_at)
            # 延期不是失败尝试：attempts 回退到 0，available_at 顺延到未来。
            self.assertEqual(deferred.attempts, 0)
            self.assertIsNotNone(deferred.available_at)
            self.assertGreater(deferred.available_at, datetime.now(timezone.utc))
            # manifest 未落库，下次领取必须重扫（不得复用半成品）。
            self.assertIsNone(deferred.manifest_fingerprint)
            self.assertEqual(source.active_snapshot_id, ready_id)
            self.assertIsNone(source.auto_sync_last_error_code)
            self.assertIsNone(source.auto_sync_last_error_message)

        # 文件静置超过窗口后，把延后的快照置为可领取：重扫应正常发布。
        stable_ns = time.time_ns() - 3600 * 1_000_000_000
        os.utime(self.notes, ns=(stable_ns, stable_ns))
        with db_session.SessionLocal() as db:
            db.execute(
                update(ProjectSnapshot)
                .where(ProjectSnapshot.id == deferred_id)
                .values(available_at=datetime.now(timezone.utc))
            )
            db.commit()

        with patch.object(settings, "PROJECT_AUTO_SYNC_QUIET_SECONDS", 30):
            claim = project_index.claim_next_project_snapshot()
            self.assertIsNotNone(claim)
            self.assertEqual(claim.snapshot_id, deferred_id)
            status = project_index.run_project_sync(claim, threading.Event())
        self.assertEqual(status, "ready")
        with db_session.SessionLocal() as db:
            snap = db.get(ProjectSnapshot, deferred_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(snap.status, ProjectSnapshotStatus.READY.value)
            self.assertEqual(source.active_snapshot_id, deferred_id)
            # 延后与成功全程都不写源错误字段。
            self.assertIsNone(source.auto_sync_last_error_code)
            self.assertIsNone(source.auto_sync_last_error_message)

    def test_manual_sync_ignores_quiet_window(self):
        # setUp 刚写入 notes.md：即便安静窗口极大，手动同步也必须立即发布。
        with patch.object(settings, "PROJECT_AUTO_SYNC_QUIET_SECONDS", 3600):
            manual_id, status = self._sync_manual()
        self.assertEqual(status, "ready")
        with db_session.SessionLocal() as db:
            snap = db.get(ProjectSnapshot, manual_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(snap.status, ProjectSnapshotStatus.READY.value)
            self.assertEqual(snap.trigger_source, "manual")
            self.assertEqual(source.active_snapshot_id, manual_id)

    # ---- 关闭自动同步：只取消 auto 活动任务，终态/手动任务不动 ----
    def test_disable_cancels_only_auto_active_and_keeps_manual(self):
        ready_id, status = self._sync_manual()
        self.assertEqual(status, "ready")
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)
        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 1)

        with db_session.SessionLocal() as db:
            auto_active = db.scalar(
                select(ProjectSnapshot).where(
                    ProjectSnapshot.source_id == self.source_id,
                    ProjectSnapshot.status == ProjectSnapshotStatus.PENDING.value,
                )
            )
            self.assertIsNotNone(auto_active)
            self.assertEqual(auto_active.trigger_source, "auto")
            auto_id = auto_active.id

        disabled = self._patch_auto(self.source_id, False)
        self.assertEqual(disabled.status_code, 200, disabled.text)
        self.assertIsNone(disabled.json()["data"]["auto_sync_next_check_at"])
        with db_session.SessionLocal() as db:
            auto_snap = db.get(ProjectSnapshot, auto_id)
            self.assertEqual(auto_snap.status, ProjectSnapshotStatus.CANCELLED.value)
            self.assertEqual(
                auto_snap.error_code, project_sources.AUTO_SYNC_CANCELLED_CODE
            )
            self.assertEqual(
                auto_snap.error_message, project_sources.AUTO_SYNC_CANCELLED_MESSAGE
            )
            self.assertEqual(auto_snap.lease_id, None)
            self.assertEqual(
                db.get(ProjectSnapshot, ready_id).status,
                ProjectSnapshotStatus.READY.value,
            )
            self.assertEqual(
                db.get(ProjectSource, self.source_id).active_snapshot_id, ready_id
            )

        # 已启用状态下再建一个手动 pending，然后关闭自动同步：手动任务必须保留。
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)
        manual = self.client.post(
            f"/api/v1/projects/{self.source_id}/sync", headers=self.headers
        )
        self.assertEqual(manual.status_code, 200, manual.text)
        manual_id = uuid.UUID(manual.json()["data"]["id"])
        self.assertEqual(manual.json()["data"]["trigger_source"], "manual")

        self.assertEqual(self._patch_auto(self.source_id, False).status_code, 200)
        with db_session.SessionLocal() as db:
            manual_snap = db.get(ProjectSnapshot, manual_id)
            self.assertEqual(manual_snap.status, ProjectSnapshotStatus.PENDING.value)
            self.assertFalse(manual_snap.cancel_requested)
            self.assertEqual(manual_snap.trigger_source, "manual")
        cleanup = self.client.post(
            f"/api/v1/projects/{self.source_id}/sync/cancel", headers=self.headers
        )
        self.assertEqual(cleanup.status_code, 200, cleanup.text)

    # ---- 空清单保护：自动保留旧版本，手动不受影响 ----
    def test_auto_empty_keeps_active_and_manual_empty_still_publishes(self):
        ready_id, status = self._sync_manual()
        self.assertEqual(status, "ready")
        self.notes.unlink()

        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)
        empty_id, status = self._run_due_auto(self.source_id)
        self.assertEqual(status, "failed")
        with db_session.SessionLocal() as db:
            empty = db.get(ProjectSnapshot, empty_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(empty.status, ProjectSnapshotStatus.FAILED.value)
            self.assertEqual(empty.error_code, project_index.AUTO_EMPTY_CODE)
            self.assertEqual(empty.error_message, project_index.AUTO_EMPTY_MESSAGE)
            self.assertEqual(empty.file_count, 0)
            # 旧 active 保留。
            self.assertEqual(source.active_snapshot_id, ready_id)
            self.assertEqual(
                db.get(ProjectSnapshot, ready_id).status,
                ProjectSnapshotStatus.READY.value,
            )
            # 源上只记录稳定码与固定安全文案。
            self.assertEqual(
                source.auto_sync_last_error_code, project_index.AUTO_EMPTY_CODE
            )
            self.assertEqual(
                source.auto_sync_last_error_message, project_index.AUTO_FAILED_MESSAGE
            )
            self.assertNotIn(str(self.root), source.auto_sync_last_error_message or "")
            self.assertNotIn(str(self.root), source.auto_sync_last_error_code or "")

        self.assertEqual(self._patch_auto(self.source_id, False).status_code, 200)
        # 手动同步同为空清单：按现有行为发布为合法空快照并切 active。
        manual_id, status = self._sync_manual()
        self.assertEqual(status, "ready")
        with db_session.SessionLocal() as db:
            manual = db.get(ProjectSnapshot, manual_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(manual.trigger_source, "manual")
            self.assertEqual(manual.status, ProjectSnapshotStatus.READY.value)
            self.assertEqual(manual.file_count, 0)
            self.assertEqual(source.active_snapshot_id, manual_id)

    # ---- 自动失败：稳定安全码，不写绝对路径/正文到源字段 ----
    def test_auto_failure_records_safe_code_without_paths(self):
        sub = self.root / "broken"
        sub.mkdir()
        (sub / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        source_id = self._make_source(relative_dir="broken")
        shutil.rmtree(sub)

        self.assertEqual(self._patch_auto(source_id, True, 300).status_code, 200)
        snap_id, status = self._run_due_auto(source_id)
        self.assertEqual(status, "failed")
        with db_session.SessionLocal() as db:
            snap = db.get(ProjectSnapshot, snap_id)
            source = db.get(ProjectSource, source_id)
            self.assertEqual(snap.status, ProjectSnapshotStatus.FAILED.value)
            self.assertEqual(snap.error_code, "root-error")
            self.assertIsNone(source.active_snapshot_id)
            self.assertEqual(source.auto_sync_last_error_code, "root-error")
            self.assertEqual(
                source.auto_sync_last_error_message, project_index.AUTO_FAILED_MESSAGE
            )
            for value in (
                source.auto_sync_last_error_code or "",
                source.auto_sync_last_error_message or "",
            ):
                self.assertNotIn(str(self.root), value)
                self.assertNotIn(str(self.temp.name), value)

    # ---- 撤销/取消不误报成功 ----
    def test_revoked_auto_task_does_not_clear_previous_error(self):
        _ready_id, status = self._sync_manual()
        self.assertEqual(status, "ready")
        self.assertEqual(self._patch_auto(self.source_id, True, 300).status_code, 200)
        with db_session.SessionLocal() as db:
            source = db.get(ProjectSource, self.source_id)
            source.auto_sync_last_error_code = "previous"
            source.auto_sync_last_error_message = "previous-message"
            db.commit()

        self.assertEqual(project_auto_sync.enqueue_due_sources(limit=1), 1)
        claim = project_index.claim_next_project_snapshot()
        self.assertIsNotNone(claim)
        self.assertEqual(claim.source_id, self.source_id)

        revoked = self.client.delete(
            f"/api/v1/projects/{self.source_id}", headers=self.headers
        )
        self.assertEqual(revoked.status_code, 200, revoked.text)
        status = project_index.run_project_sync(claim, threading.Event())
        self.assertEqual(status, "cancelled")

        with db_session.SessionLocal() as db:
            snap = db.get(ProjectSnapshot, claim.snapshot_id)
            source = db.get(ProjectSource, self.source_id)
            self.assertEqual(snap.status, ProjectSnapshotStatus.CANCELLED.value)
            # 撤销/取消绝不把上一次错误当成功清空。
            self.assertEqual(source.auto_sync_last_error_code, "previous")
            self.assertEqual(source.auto_sync_last_error_message, "previous-message")


if __name__ == "__main__":
    unittest.main()
