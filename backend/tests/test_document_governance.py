"""文档治理：生命周期派生、CAS、替代/重启用、权限与真实 PostgreSQL 并发。

破坏性读写前先走共享门 ``tests._memory_project_test_gate``（loopback / ``_test``
后缀 / owner 标签 / 实际 engine 一致 / ``pg_database`` comment 精确匹配 owner）；
任何不符一律硬 fail，绝不 skip 掩盖。仅 ``TRUNCATE`` owned 的表。

真实权限路径：本文件用**真 FastAPI app + 真 JWT + 真 require_admin** 覆盖
``/governance``、``/supersede``、``/reenable``；只复用应用自身的 ``SessionLocal``
（已绑到测试库），不 mock 业务、服务、权限或数据库。
"""
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from tests import _memory_project_test_gate as gate
from tests._env import TEST_URL

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine, func, select, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

if TEST_URL:
    from app.db import session as db_session

    _target = make_url(TEST_URL)
    if db_session.engine.url != _target:
        db_session.engine.dispose()
        db_session.engine = create_engine(TEST_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
        db_session.SessionLocal.configure(bind=db_session.engine)

    from app.core.security import create_access_token, hash_password
    from app.db.init_db import init_db
    from app.models import (
        AuditLog, Conversation, Document, DocumentChunk, DocumentStatus, KnowledgeBase,
        Message, ModelUsage, User,
    )
    from app.schemas.knowledge import (
        DocumentGovernanceUpdate, DocumentReenableRequest, DocumentSupersedeRequest,
    )
    from app.services import document_governance
    from app.services.document_lifecycle import (
        LIFECYCLE_ACTIVE, LIFECYCLE_DISABLED, LIFECYCLE_EXPIRED,
        LIFECYCLE_SCHEDULED, LIFECYCLE_SUPERSEDED,
        is_retrievable, lifecycle_state, retrievable_conditions,
    )

    SessionLocal, engine = db_session.SessionLocal, db_session.engine


_TABLES = ("users, knowledge_bases, documents, document_chunks, upload_sessions, "
           "audit_logs, conversations, messages, model_usage, usage_tracking_state")

_FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
_PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _iso(dt):
    return None if dt is None else dt.isoformat()


def _reset():
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))


class _GovernanceHelpers:
    """fixture/服务层调用工具；所有服务调用都先拿 KB 行锁（冻结锁序）。"""

    def add(self, filename="doc.txt", *, status="ready", kb_id=None, **governance):
        with SessionLocal() as db:
            doc = Document(kb_id=kb_id or self.kb_id, filename=filename, file_ext=".txt",
                           file_size=1, stored_path=f"/tmp/{uuid.uuid4().hex}.txt",
                           status=status, **governance)
            db.add(doc)
            db.commit()
            return doc.id

    def add_chunks(self, doc_id, contents, *, embedded=True, metadata=None):
        with SessionLocal() as db:
            for index, content in enumerate(contents):
                db.add(DocumentChunk(
                    document_id=doc_id, chunk_index=index, content=content,
                    source_metadata=dict(metadata or {}), embedded=embedded,
                ))
            db.commit()

    def add_with_chunks(self, filename="doc.txt", contents=("hello",), *,
                        kb_id=None, embedded=True, status="ready", **governance):
        contents = tuple(contents)
        doc_id = self.add(
            filename, status=status, kb_id=kb_id,
            chunk_count=len(contents), char_count=sum(len(item) for item in contents),
            **governance,
        )
        self.add_chunks(doc_id, contents, embedded=embedded)
        return doc_id

    def document_state(self, doc_id):
        """治理字段 + 分块数的快照，用于断言预览/失败替代没有副作用。"""
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            return {
                "status": doc.status,
                "index_version": doc.index_version,
                "governance_revision": doc.governance_revision,
                "maintainer": doc.maintainer,
                "enabled": doc.enabled,
                "valid_from": doc.valid_from,
                "valid_until": doc.valid_until,
                "superseded_at": doc.superseded_at,
                "superseded_by_id": doc.superseded_by_id,
                "stored_path": doc.stored_path,
                "chunk_count": doc.chunk_count,
                "char_count": doc.char_count,
                "chunks": db.scalar(
                    select(func.count()).select_from(DocumentChunk)
                    .where(DocumentChunk.document_id == doc_id)),
            }

    def write_counts(self):
        with SessionLocal() as db:
            return {
                "audit": db.scalar(select(func.count()).select_from(AuditLog)),
                "conversations": db.scalar(select(func.count()).select_from(Conversation)),
                "messages": db.scalar(select(func.count()).select_from(Message)),
                "model_usage": db.scalar(select(func.count()).select_from(ModelUsage)),
                "chunks": db.scalar(select(func.count()).select_from(DocumentChunk)),
            }

    def fresh(self, doc_id):
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            db.refresh(doc)
            return doc

    def _locked_kb(self, db):
        return document_governance.require_governance_kb(db, self.kb_id)

    def gov_patch(self, doc_id, body, *, kb_id=None):
        with SessionLocal() as db:
            from app.schemas.knowledge import DocumentGovernanceUpdate

            kb = document_governance.require_governance_kb(db, kb_id or self.kb_id)
            return document_governance.update_governance(
                db, kb, doc_id, DocumentGovernanceUpdate(**body))

    def supersede(self, doc_id, replacement_id, expected_revision, *, kb_id=None):
        with SessionLocal() as db:
            kb = document_governance.require_governance_kb(db, kb_id or self.kb_id)
            return document_governance.supersede_document(
                db, kb, doc_id, replacement_id, expected_revision)

    def reenable(self, doc_id, expected_revision, *, kb_id=None):
        with SessionLocal() as db:
            kb = document_governance.require_governance_kb(db, kb_id or self.kb_id)
            return document_governance.reenable_document(db, kb, doc_id, expected_revision)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DocumentGovernanceTests(_GovernanceHelpers, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        _reset()
        with SessionLocal() as db:
            self.admin = User(username="gov_admin", role="admin", is_active=True,
                              password_hash="x", token_version=3)
            self.member = User(username="gov_member", role="user", is_active=True,
                               password_hash="x")
            db.add_all([self.admin, self.member])
            db.flush()
            self.kb = KnowledgeBase(name="gov-kb", collection_name="g_" + uuid.uuid4().hex,
                                    owner_id=self.admin.id, is_public=True, kind="upload")
            self.project_kb = KnowledgeBase(name="gov-project", collection_name="g_" + uuid.uuid4().hex,
                                            owner_id=self.admin.id, is_public=False, kind="project")
            self.other_kb = KnowledgeBase(name="gov-other", collection_name="g_" + uuid.uuid4().hex,
                                          owner_id=self.admin.id, is_public=True, kind="upload")
            db.add_all([self.kb, self.project_kb, self.other_kb])
            db.commit()
            self.kb_id, self.project_kb_id = self.kb.id, self.project_kb.id
            self.other_kb_id, self.admin_id, self.member_id = self.other_kb.id, self.admin.id, self.member.id

    # ---- 生命周期派生（对象与 SQL 同义） ----
    def test_state_priority_superseded_over_all(self):
        doc_id = self.add(enabled=False, valid_from=_PAST, valid_until=_PAST, superseded_at=_PAST)
        self.assertEqual(lifecycle_state(self.fresh(doc_id)), LIFECYCLE_SUPERSEDED)

    def test_state_disabled_and_window_states(self):
        self.assertEqual(lifecycle_state(self.fresh(self.add())), LIFECYCLE_ACTIVE)
        self.assertEqual(lifecycle_state(self.fresh(self.add(enabled=False))), LIFECYCLE_DISABLED)
        self.assertEqual(lifecycle_state(self.fresh(self.add(valid_from=_FUTURE))), LIFECYCLE_SCHEDULED)
        self.assertEqual(lifecycle_state(self.fresh(self.add(valid_until=_PAST))), LIFECYCLE_EXPIRED)

    def test_state_object_and_sql_agree_on_fresh_rows(self):
        ids = [self.add(), self.add(enabled=False), self.add(valid_from=_FUTURE),
               self.add(valid_until=_PAST), self.add(superseded_at=_PAST),
               self.add(status="failed"), self.add(cancel_requested=True)]
        now = datetime.now(timezone.utc)
        conditions = retrievable_conditions(now=now)
        with SessionLocal() as db:
            rows = {row.id: row for row in db.scalars(select(Document)).all()}
            hit = set(db.scalars(
                select(Document.id).where(Document.kb_id == self.kb_id, *conditions)).all())
            self.assertEqual(len(rows), len(ids))
        for doc_id in ids:
            self.assertEqual(doc_id in hit, is_retrievable(rows[doc_id], now=now), str(doc_id))

    def test_retrievable_requires_ingestion_ready_and_governance(self):
        self.assertFalse(is_retrievable(self.fresh(self.add(status="pending"))))
        self.assertFalse(is_retrievable(self.fresh(self.add(cancel_requested=True))))
        self.assertTrue(is_retrievable(self.fresh(self.add())))

    def test_missing_security_fields_fail_closed(self):
        from app.services.document_lifecycle import is_retrievable as r

        class Bare:  # 旧 mock fixture：缺安全字段
            enabled = True
            superseded_at = None
            superseded_by_id = None
            valid_from = None
            valid_until = None
        self.assertFalse(r(Bare()))
        self.assertFalse(r(None))

    def test_naive_now_is_rejected_not_silently_trusted(self):
        from app.services.document_lifecycle import lifecycle_state as state

        doc = self.fresh(self.add(valid_until=_PAST))
        # naive 时刻不能被静默当作 UTC 使用：fail closed 抛 ValueError
        with self.assertRaises(ValueError):
            state(doc, now=datetime(2020, 1, 1))
        # aware 时刻正常判定
        self.assertEqual(state(doc, now=datetime(2020, 1, 1, tzinfo=timezone.utc)), LIFECYCLE_EXPIRED)

    # ---- DTO ----
    def test_dto_exposes_all_governance_fields(self):
        from app.schemas.knowledge import DocumentRead

        doc_id = self.add(maintainer="m", valid_from=_PAST, valid_until=_FUTURE)
        data = DocumentRead.model_validate(self.fresh(doc_id))
        self.assertEqual(data.index_version, 0)
        self.assertEqual(data.maintainer, "m")
        self.assertEqual(data.governance_revision, 0)
        self.assertTrue(data.enabled)
        self.assertIsNone(data.superseded_at)
        self.assertIsNone(data.superseded_by_id)
        self.assertEqual(data.lifecycle_state, LIFECYCLE_ACTIVE)
        self.assertTrue(data.retrievable)

    def test_dto_superseded_paths_and_utc_iso(self):
        from app.schemas.knowledge import DocumentRead

        doc_id = self.add(superseded_at=_PAST, status="pending")
        data = DocumentRead.model_validate(self.fresh(doc_id))
        self.assertEqual(data.lifecycle_state, LIFECYCLE_SUPERSEDED)
        self.assertFalse(data.retrievable)
        self.assertTrue(data.superseded_at.isoformat().endswith("+00:00"))

    # ---- PATCH /governance（服务层） ----
    def test_patch_merges_and_increments_revision(self):
        doc_id = self.add(maintainer="old")
        result = self.gov_patch(doc_id, {"expected_revision": 0, "enabled": False,
                                         "last_reviewed_at": _iso(_PAST), "valid_until": _iso(_FUTURE)})
        self.assertEqual(result.governance_revision, 1)
        self.assertFalse(result.enabled)
        self.assertEqual(result.last_reviewed_at, _PAST)
        self.assertEqual(result.maintainer, "old")  # omitted 不变
        self.assertEqual(lifecycle_state(self.fresh(doc_id)), LIFECYCLE_DISABLED)

    def test_patch_explicit_null_clears_dates_only(self):
        doc_id = self.add(valid_from=_PAST, valid_until=_FUTURE)
        result = self.gov_patch(doc_id, {"expected_revision": 0, "valid_from": None})
        self.assertIsNone(result.valid_from)
        self.assertEqual(result.valid_until, _FUTURE)
        self.assertEqual(result.governance_revision, 1)

    def test_patch_cas_mismatch_409_and_no_write(self):
        doc_id = self.add()
        with self.assertRaises(HTTPException) as error:
            self.gov_patch(doc_id, {"expected_revision": 7, "enabled": False})
        self.assertEqual(error.exception.status_code, 409)
        doc = self.fresh(doc_id)
        self.assertTrue(doc.enabled)
        self.assertEqual(doc.governance_revision, 0)

    def test_patch_rejects_null_and_bad_types_without_partial_write(self):
        from pydantic import ValidationError

        doc_id = self.add(maintainer="orig")
        for body in (
            {"expected_revision": 0, "maintainer": None},
            {"expected_revision": 0, "enabled": None},
            {"expected_revision": 0, "maintainer": "x" * 201},
            {"expected_revision": -1},
            {"expected_revision": 0, "unknown_field": 1},
            {"expected_revision": 0},
        ):
            with self.assertRaises((HTTPException, ValidationError), msg=str(body)):
                self.gov_patch(doc_id, body)
        doc = self.fresh(doc_id)
        self.assertEqual(doc.maintainer, "orig")
        self.assertEqual(doc.governance_revision, 0)

    def test_patch_rejects_naive_numeric_and_timestamp_dates(self):
        from pydantic import ValidationError

        doc_id = self.add()
        for bad in ("2026-01-01T00:00:00", "1767225600", "2026-01-01T00:00:00Zx", 1767225600, True):
            with self.assertRaises((HTTPException, ValidationError), msg=repr(bad)):
                self.gov_patch(doc_id, {"expected_revision": 0, "last_reviewed_at": bad})
        with self.assertRaises((HTTPException, ValidationError)):
            self.gov_patch(doc_id, {"expected_revision": 0, "valid_until": "2026-01-01T00:00:00"})

    def test_patch_merged_window_validation(self):
        doc_id = self.add()
        with self.assertRaises(HTTPException) as error:
            self.gov_patch(doc_id, {"expected_revision": 0, "valid_from": _iso(_FUTURE),
                                    "valid_until": _iso(_PAST)})
        self.assertEqual(error.exception.status_code, 422)
        # 失败请求不能留下半更新行：窗口未变、revision 未动
        unchanged = self.fresh(doc_id)
        self.assertIsNone(unchanged.valid_from)
        self.assertIsNone(unchanged.valid_until)
        self.assertEqual(unchanged.governance_revision, 0)
        # merged 窗口：omitted valid_until 与新 valid_from 冲突也拒绝
        result = self.gov_patch(doc_id, {"expected_revision": 0, "valid_until": _iso(_FUTURE)})
        self.assertEqual(result.governance_revision, 1)
        with self.assertRaises(HTTPException):
            self.gov_patch(doc_id, {"expected_revision": 1, "valid_from": _iso(_FUTURE)})
        doc = self.fresh(doc_id)
        self.assertIsNone(doc.valid_from)
        self.assertEqual(doc.valid_until, _FUTURE)

    def test_patch_cannot_clear_supersession(self):
        from pydantic import ValidationError

        doc_id = self.add(superseded_at=_PAST)
        with self.assertRaises(ValidationError):
            self.gov_patch(doc_id, {"expected_revision": 0, "superseded_at": None})
        doc = self.fresh(doc_id)
        self.assertIsNotNone(doc.superseded_at)
        self.assertEqual(doc.governance_revision, 0)

    def test_enabled_true_cannot_bypass_supersession(self):
        doc_id = self.add(superseded_at=_PAST)
        self.gov_patch(doc_id, {"expected_revision": 0, "enabled": True})
        self.assertEqual(lifecycle_state(self.fresh(doc_id)), LIFECYCLE_SUPERSEDED)
        self.assertFalse(is_retrievable(self.fresh(doc_id)))

    # ---- supersede ----
    def test_supersede_sets_link_and_preserves_original(self):
        original_id = self.add()
        replacement_id = self.add("replacement.txt")
        result = self.supersede(original_id, replacement_id, 0)
        self.assertEqual(result.superseded_by_id, replacement_id)
        self.assertIsNotNone(result.superseded_at)
        self.assertEqual(result.governance_revision, 1)
        self.assertEqual(lifecycle_state(self.fresh(original_id)), LIFECYCLE_SUPERSEDED)
        self.assertFalse(is_retrievable(self.fresh(original_id)))

    def test_supersede_rejects_self_cross_kb_and_inactive_replacement(self):
        doc_id = self.add()
        with self.assertRaises(HTTPException) as error:  # 自己
            self.supersede(doc_id, doc_id, 0)
        self.assertEqual(error.exception.status_code, 400)
        with self.assertRaises(HTTPException) as error:  # 异库
            self.supersede(doc_id, self.add("other.txt", kb_id=self.other_kb_id), 0)
        self.assertEqual(error.exception.status_code, 404)
        for kwargs in ({"status": "pending"}, {"status": "failed"}, {"cancel_requested": True},
                       {"enabled": False}, {"valid_until": _PAST}, {"valid_from": _FUTURE},
                       {"superseded_at": _PAST}):
            with self.assertRaises(HTTPException) as error:
                self.supersede(doc_id, self.add("bad.txt", **kwargs), 0)
            self.assertEqual(error.exception.status_code, 409, str(kwargs))
        self.assertEqual(self.fresh(doc_id).governance_revision, 0)

    def test_supersede_rejects_already_superseded(self):
        a, b, c = self.add("a.txt"), self.add("b.txt"), self.add("c.txt")
        self.supersede(a, b, 0)
        with self.assertRaises(HTTPException) as error:
            self.supersede(a, c, 1)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.fresh(a).superseded_by_id, b)

    def test_supersede_rejects_cycle(self):
        a, b, c = self.add("a.txt"), self.add("b.txt"), self.add("c.txt")
        self.supersede(b, c, 0)
        self.supersede(c, a, 0)
        with self.assertRaises(HTTPException) as error:  # a -> b 会闭合 b->c->a
            self.supersede(a, b, 0)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIsNone(self.fresh(a).superseded_by_id)

    def test_supersede_cas_mismatch_409(self):
        doc_id = self.add()
        with self.assertRaises(HTTPException) as error:
            self.supersede(doc_id, self.add("r.txt"), 5)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIsNone(self.fresh(doc_id).superseded_at)

    def test_supersede_preserves_file_chunks_and_index_generation(self):
        doc_id = self.add(index_version=3, chunk_count=2)
        with SessionLocal() as db:
            db.add_all([DocumentChunk(document_id=doc_id, chunk_index=0, content="c0", embedded=True),
                        DocumentChunk(document_id=doc_id, chunk_index=1, content="c1", embedded=True)])
            db.commit()
            stored_path = db.get(Document, doc_id).stored_path
        self.supersede(doc_id, self.add("r.txt"), 0)
        with SessionLocal() as db:
            doc = db.get(Document, doc_id)
            self.assertEqual(doc.index_version, 3)       # 代次不变
            self.assertEqual(doc.chunk_count, 2)
            self.assertEqual(doc.file_size, 1)
            self.assertEqual(doc.stored_path, stored_path)  # 原文件引用不变
            self.assertEqual(doc.status, DocumentStatus.READY.value)
            self.assertEqual(db.scalar(select(func.count()).select_from(DocumentChunk).where(
                DocumentChunk.document_id == doc_id)), 2)

    def test_supersede_concurrent_cas_only_one_wins(self):
        doc_id = self.add()
        replacement_id = self.add("r.txt")
        barrier = Barrier(2)

        def attempt(_):
            barrier.wait()
            try:
                self.supersede(doc_id, replacement_id, 0)
                return "ok"
            except HTTPException as exc:
                return str(exc.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        self.assertEqual(sorted(results), ["409", "ok"])
        doc = self.fresh(doc_id)
        self.assertEqual(doc.governance_revision, 1)
        self.assertEqual(doc.superseded_by_id, replacement_id)

    def test_kb_lock_wait_timeout_is_not_silently_retried(self):
        """持有 KB 行锁时，另一事务必须在其显式 lock_timeout 上失败（不无限等待）。"""
        doc_id = self.add()
        replacement_id = self.add("r.txt")
        holder = SessionLocal()
        try:
            document_governance.require_governance_kb(holder, self.kb_id)  # 持锁不提交
            with self.assertRaises(OperationalError):
                with SessionLocal() as waiter:
                    waiter.execute(text("SET LOCAL lock_timeout = '150ms'"))
                    document_governance.require_governance_kb(waiter, self.kb_id)
        finally:
            holder.rollback()
            holder.close()
        # 超时失败没有产生任何治理写入
        doc = self.fresh(doc_id)
        self.assertIsNone(doc.superseded_at)
        self.assertEqual(doc.superseded_by_id, None)
        self.assertIsNotNone(self.fresh(replacement_id))

    # ---- reenable ----
    def test_reenable_clears_supersession_and_enables(self):
        doc_id = self.add(superseded_at=_PAST)
        result = self.reenable(doc_id, 0)
        self.assertIsNone(result.superseded_at)
        self.assertIsNone(result.superseded_by_id)
        self.assertTrue(result.enabled)
        self.assertEqual(result.governance_revision, 1)
        self.assertEqual(lifecycle_state(self.fresh(doc_id)), LIFECYCLE_ACTIVE)

    def test_reenable_preserves_window_and_rejects_scheduled_expired(self):
        scheduled = self.add(superseded_at=_PAST, valid_from=_FUTURE)
        expired = self.add(superseded_at=_PAST, valid_until=_PAST)
        for doc_id in (scheduled, expired):
            with self.assertRaises(HTTPException) as error:
                self.reenable(doc_id, 0)
            self.assertEqual(error.exception.status_code, 409)
            doc = self.fresh(doc_id)
            self.assertIsNotNone(doc.superseded_at)  # 不静默抹除期限
            self.assertEqual(doc.governance_revision, 0)
        inwindow = self.add(superseded_at=_PAST, valid_from=_PAST, valid_until=_FUTURE)
        result = self.reenable(inwindow, 0)
        self.assertEqual(result.valid_from, _PAST)
        self.assertEqual(result.valid_until, _FUTURE)
        self.assertEqual(result.governance_revision, 1)

    def test_reenable_redundant_and_cas(self):
        doc_id = self.add()
        with self.assertRaises(HTTPException) as error:
            self.reenable(doc_id, 0)
        self.assertEqual(error.exception.status_code, 400)
        disabled = self.add(enabled=False)
        with self.assertRaises(HTTPException) as error:
            self.reenable(disabled, 9)
        self.assertEqual(error.exception.status_code, 409)

    def test_reenable_has_no_indexing_side_effects(self):
        doc_id = self.add(superseded_at=_PAST, index_version=2, status="ready")
        self.reenable(doc_id, 0)
        doc = self.fresh(doc_id)
        self.assertEqual(doc.index_version, 2)
        self.assertEqual(doc.status, "ready")

    # ---- 删除替代文档：FK SET NULL，原文档不复活 ----
    def test_deleting_replacement_nulls_link_but_keeps_superseded(self):
        original_id = self.add()
        replacement_id = self.add("r.txt")
        self.supersede(original_id, replacement_id, 0)
        with SessionLocal() as db:
            db.delete(db.get(Document, replacement_id))
            db.commit()
        doc = self.fresh(original_id)
        self.assertIsNone(doc.superseded_by_id)          # FK SET NULL
        self.assertIsNotNone(doc.superseded_at)          # superseded_at 保留
        self.assertEqual(lifecycle_state(doc), LIFECYCLE_SUPERSEDED)
        self.assertFalse(is_retrievable(doc))

    # ---- 重索引/去重不复活治理 ----
    def test_reindex_never_resets_governance(self):
        doc_id = self.add(valid_until=_PAST, index_version=1, maintainer="m")
        with SessionLocal() as db:
            from app.services.ingest import reset_document_for_reindex
            reset_document_for_reindex(db, db.get(Document, doc_id))
        doc = self.fresh(doc_id)
        self.assertEqual(doc.valid_until, _PAST)
        self.assertEqual(doc.maintainer, "m")
        self.assertEqual(doc.index_version, 2)  # 重索引只推进代次
        self.assertEqual(lifecycle_state(doc), LIFECYCLE_EXPIRED)

    def test_dedup_of_inactive_canonical_rejected_not_revived(self):
        doc_id = self.add(superseded_at=_PAST)
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                document_governance.ensure_active_governance_document(db.get(Document, doc_id))
            self.assertEqual(error.exception.status_code, 409)
        doc = self.fresh(doc_id)
        self.assertIsNotNone(doc.superseded_at)
        self.assertEqual(doc.governance_revision, 0)

    def test_dedup_canonical_missing_is_noop(self):
        document_governance.ensure_active_governance_document(None)  # 不抛

    # ---- 权限/项目库（服务层） ----
    def test_project_and_unknown_kind_rejected(self):
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                document_governance.require_governance_kb(db, self.project_kb_id)
            self.assertEqual(error.exception.status_code, 400)
        with SessionLocal() as db:
            weird = KnowledgeBase(name="weird", collection_name="w_" + uuid.uuid4().hex,
                                  owner_id=self.admin_id, is_public=True, kind="custom")
            db.add(weird)
            db.commit()
            weird_id = weird.id
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as error:
                document_governance.require_governance_kb(db, weird_id)
            self.assertEqual(error.exception.status_code, 400)

    def test_project_document_is_invisible_to_service(self):
        doc_id = self.add(kb_id=self.project_kb_id)
        # 直接指定 project KB：require_governance_kb 在读文档前就 400 拒绝
        with self.assertRaises(HTTPException) as error:
            self.supersede(doc_id, doc_id, 0, kb_id=self.project_kb_id)
        self.assertEqual(error.exception.status_code, 400)
        # 用另一个 upload KB 指向项目文档：先做 self 替代校验（400），不跨库命中
        with self.assertRaises(HTTPException) as error:
            self.supersede(doc_id, doc_id, 0, kb_id=self.other_kb_id)
        self.assertEqual(error.exception.status_code, 400)
        # 用非自身 UUID + 异库 KB：文档行按 (id, kb_id) 查不到 → 404
        with self.assertRaises(HTTPException) as error:
            self.supersede(doc_id, uuid.uuid4(), 0, kb_id=self.other_kb_id)
        self.assertEqual(error.exception.status_code, 404)

    def test_lock_order_documented_user_kb_document(self):
        """同一 KB 内并发替代不同目标：KB 行锁串行化，均成功且无死锁。"""
        a, b = self.add("a.txt"), self.add("b.txt")
        ra, rb = self.add("ra.txt"), self.add("rb.txt")
        barrier = Barrier(2)

        def run(args):
            barrier.wait()
            try:
                self.supersede(*args)
                return "ok"
            except HTTPException as exc:
                return str(exc.status_code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, [(a, ra, 0), (b, rb, 0)]))
        self.assertEqual(results, ["ok", "ok"])

    def test_audit_detail_has_no_body_or_maintainer_text(self):
        doc_id = self.add(maintainer="秘密负责人张三")
        detail = document_governance.audit_detail(self.fresh(doc_id), ["maintainer"])
        self.assertEqual(set(detail), {"document_id", "governance_revision", "fields"})
        self.assertEqual(detail["fields"], ["maintainer"])
        self.assertNotIn("秘密负责人张三", str(detail))


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DocumentGovernanceHttpTests(_GovernanceHelpers, unittest.TestCase):
    """真实 HTTP + 真 JWT + 真 require_admin：管理员通过、member 拒绝、项目库拒绝。"""

    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.v1 import knowledge as knowledge_api
        from app.db.session import get_db

        cls.app = FastAPI()
        cls.app.include_router(knowledge_api.router, prefix="/api/v1")
        # 只替换会话工厂（本就绑定测试库）；权限仍是真实的 get_current_user /
        # require_admin / JWT 校验，业务与治理服务也全部真实执行。
        def _session_override():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        cls.app.dependency_overrides[get_db] = _session_override
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        _reset()
        with SessionLocal() as db:
            self.admin = User(username="gov_http_admin", role="admin", is_active=True,
                              password_hash=hash_password("AdminPass123"), token_version=4)
            self.member = User(username="gov_http_member", role="user", is_active=True,
                               password_hash=hash_password("MemberPass123"))
            db.add_all([self.admin, self.member])
            db.flush()
            self.kb = KnowledgeBase(name="gov-http-kb", collection_name="gh_" + uuid.uuid4().hex,
                                    owner_id=self.admin.id, is_public=True, kind="upload")
            self.project_kb = KnowledgeBase(name="gov-http-project",
                                            collection_name="gh_" + uuid.uuid4().hex,
                                            owner_id=self.admin.id, is_public=False, kind="project")
            db.add_all([self.kb, self.project_kb])
            db.commit()
            self.kb_id, self.project_kb_id = self.kb.id, self.project_kb.id
            self.admin_id, self.member_id = self.admin.id, self.member.id
        self.admin_headers = self._headers(self.admin_id, "admin", 4)
        self.member_headers = self._headers(self.member_id, "user", 0)
        self.base = f"/api/v1/knowledge-bases/{self.kb_id}/documents"

    @staticmethod
    def _headers(user_id, role, version):
        token = create_access_token(str(user_id), role, token_version=version)
        return {"Authorization": f"Bearer {token}"}

    def test_admin_can_patch_supersede_and_reenable(self):
        original = self.add()
        replacement = self.add("r.txt")
        response = self.client.patch(f"{self.base}/{original}/governance",
                                     headers=self.admin_headers,
                                     json={"expected_revision": 0, "maintainer": "ops"})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        for field in ("index_version", "maintainer", "last_reviewed_at", "valid_from",
                      "valid_until", "enabled", "superseded_at", "superseded_by_id",
                      "governance_revision", "lifecycle_state", "retrievable"):
            self.assertIn(field, data)
        self.assertEqual(data["governance_revision"], 1)
        self.assertEqual(data["lifecycle_state"], "active")
        self.assertTrue(data["retrievable"])

        response = self.client.post(f"{self.base}/{original}/supersede",
                                    headers=self.admin_headers,
                                    json={"expected_revision": 1,
                                          "replacement_document_id": str(replacement)})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["lifecycle_state"], "superseded")
        self.assertFalse(data["retrievable"])
        self.assertEqual(data["superseded_by_id"], str(replacement))

        response = self.client.post(f"{self.base}/{original}/reenable",
                                    headers=self.admin_headers,
                                    json={"expected_revision": 2})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["lifecycle_state"], "active")
        self.assertTrue(data["retrievable"])

    def test_cas_conflict_returns_409_over_http(self):
        doc_id = self.add()
        response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                     headers=self.admin_headers,
                                     json={"expected_revision": 9, "enabled": False})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertTrue(self.fresh(doc_id).enabled)

    def test_member_is_forbidden_by_real_admin_dependency(self):
        doc_id = self.add()
        calls = (
            ("patch", lambda: self.client.patch(f"{self.base}/{doc_id}/governance",
                                                headers=self.member_headers,
                                                json={"expected_revision": 0, "enabled": False})),
            ("supersede", lambda: self.client.post(f"{self.base}/{doc_id}/supersede",
                                                   headers=self.member_headers,
                                                   json={"expected_revision": 0,
                                                         "replacement_document_id": str(uuid.uuid4())})),
            ("reenable", lambda: self.client.post(f"{self.base}/{doc_id}/reenable",
                                                  headers=self.member_headers,
                                                  json={"expected_revision": 0})),
        )
        for label, call in calls:
            response = call()
            self.assertEqual(response.status_code, 403, label + ": " + response.text)
        self.assertEqual(self.fresh(doc_id).governance_revision, 0)

    def test_missing_or_stale_token_is_rejected(self):
        doc_id = self.add()
        response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                     json={"expected_revision": 0, "enabled": False})
        self.assertEqual(response.status_code, 401)
        stale = self._headers(self.admin_id, "admin", 3)  # token_version 4
        response = self.client.patch(f"{self.base}/{doc_id}/governance", headers=stale,
                                     json={"expected_revision": 0, "enabled": False})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self.fresh(doc_id).enabled)

    def test_project_kb_governance_is_rejected_over_http(self):
        project_base = f"/api/v1/knowledge-bases/{self.project_kb_id}/documents"
        doc_id = self.add(kb_id=self.project_kb_id)
        calls = (
            ("patch", lambda: self.client.patch(f"{project_base}/{doc_id}/governance",
                                                headers=self.admin_headers,
                                                json={"expected_revision": 0, "enabled": False})),
            ("supersede", lambda: self.client.post(f"{project_base}/{doc_id}/supersede",
                                                   headers=self.admin_headers,
                                                   json={"expected_revision": 0,
                                                         "replacement_document_id": str(uuid.uuid4())})),
            ("reenable", lambda: self.client.post(f"{project_base}/{doc_id}/reenable",
                                                  headers=self.admin_headers,
                                                  json={"expected_revision": 0})),
        )
        for label, call in calls:
            response = call()
            self.assertEqual(response.status_code, 400, label + ": " + response.text)
        self.assertEqual(self.fresh(doc_id).governance_revision, 0)

    def test_invalid_governance_payloads_are_422(self):
        doc_id = self.add()
        for body in ({"expected_revision": 0},
                     {"expected_revision": 0, "maintainer": None},
                     {"expected_revision": 0, "enabled": None},
                     {"expected_revision": 0, "unknown": 1},
                     {"expected_revision": 0, "valid_from": "2026-01-01T00:00:00"},
                     {"expected_revision": -1}):
            response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                         headers=self.admin_headers, json=body)
            self.assertEqual(response.status_code, 422, str(body) + ": " + response.text)
        self.assertEqual(self.fresh(doc_id).governance_revision, 0)

    def test_explicit_null_clears_date_over_http(self):
        doc_id = self.add(valid_from=_PAST, valid_until=_FUTURE)
        response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                     headers=self.admin_headers,
                                     json={"expected_revision": 0, "valid_from": None})
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertIsNone(data["valid_from"])
        self.assertIsNotNone(data["valid_until"])

    def test_audit_record_has_ids_and_field_names_only(self):
        doc_id = self.add()
        response = self.client.patch(f"{self.base}/{doc_id}/governance",
                                     headers=self.admin_headers,
                                     json={"expected_revision": 0, "maintainer": "秘密负责人"})
        self.assertEqual(response.status_code, 200, response.text)
        with SessionLocal() as db:
            log = db.scalar(select(AuditLog).where(AuditLog.action == "document_governance_update"))
        self.assertIsNotNone(log)
        self.assertEqual(log.target, str(doc_id))
        self.assertEqual(set(log.detail or {}), {"document_id", "governance_revision", "fields"})
        self.assertNotIn("秘密负责人", str(log.detail))

    # ---- 资料更新差异预览 + 预览指纹确认 ----
    def _pair(self, original_contents=("第一行\n旧内容", "尾巴"),
              replacement_contents=("第一行\n新内容", "尾巴")):
        original = self.add_with_chunks("old.txt", original_contents)
        replacement = self.add_with_chunks("new.txt", replacement_contents)
        return original, replacement

    def _compare(self, doc_id, replacement_id, *, headers=None, base=None):
        return self.client.get(
            f"{base or self.base}/{doc_id}/comparison",
            headers=headers or self.admin_headers,
            params={"replacement_document_id": str(replacement_id)},
        )

    def _fingerprint(self, doc_id, replacement_id):
        response = self._compare(doc_id, replacement_id)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertTrue(data["comparable"], response.text)
        self.assertRegex(data["comparison_fingerprint"], r"^[a-f0-9]{64}$")
        return data["comparison_fingerprint"]

    def _new_kb(self, name):
        with SessionLocal() as db:
            kb = KnowledgeBase(name=name, collection_name="gh_" + uuid.uuid4().hex,
                               owner_id=self.admin_id, is_public=True, kind="upload")
            db.add(kb)
            db.commit()
            return kb.id

    def test_admin_preview_returns_comparable_rows_and_no_store(self):
        original, replacement = self._pair(
            original_contents=("第一行\n<script>alert(1)</script>", "旧内容"),
            replacement_contents=("第一行\n<script>alert(1)</script>改", "新内容"),
        )
        response = self._compare(original, replacement)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        data = response.json()["data"]
        self.assertEqual(data["comparison_version"], 1)
        self.assertEqual(data["basis"], "ingested_chunks")
        self.assertTrue(data["comparable"])
        self.assertEqual(data["reason"], "ok")
        self.assertRegex(data["comparison_fingerprint"], r"^[a-f0-9]{64}$")
        self.assertEqual(data["original"]["id"], str(original))
        self.assertEqual(data["replacement"]["id"], str(replacement))
        self.assertEqual(data["original"]["lifecycle_state"], "active")
        self.assertTrue(data["original"]["retrievable"])
        self.assertTrue(data["summary"]["unchanged"] >= 1)
        counts = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}
        for row in data["rows"]:
            self.assertEqual(set(row), {"kind", "original", "replacement"})
            counts[row["kind"]] += 1
        self.assertEqual(data["summary"], counts)
        texts = [row["original"]["text"] for row in data["rows"] if row["original"]]
        texts += [row["replacement"]["text"] for row in data["rows"] if row["replacement"]]
        self.assertIn("<script>alert(1)</script>", texts)  # XSS 文本原样保留，不转义不丢弃

    def test_preview_member_cross_kb_self_and_project_are_rejected(self):
        original, replacement = self._pair()
        member = self._compare(original, replacement, headers=self.member_headers)
        self.assertEqual(member.status_code, 403, member.text)
        self.assertEqual(self.document_state(original)["governance_revision"], 0)

        other_kb = self._new_kb("gov-http-other")
        foreign = self.add_with_chunks("foreign.txt", ["x"], kb_id=other_kb)
        cross = self._compare(original, foreign)
        self.assertEqual(cross.status_code, 404, cross.text)

        same = self._compare(original, original)
        self.assertEqual(same.status_code, 400, same.text)

        project_base = f"/api/v1/knowledge-bases/{self.project_kb_id}/documents"
        project_doc = self.add_with_chunks("p.txt", ["x"], kb_id=self.project_kb_id)
        project_other = self.add_with_chunks("p2.txt", ["y"], kb_id=self.project_kb_id)
        project = self._compare(project_doc, project_other, base=project_base)
        self.assertEqual(project.status_code, 400, project.text)

        missing = self.client.get(f"{self.base}/{original}/comparison",
                                  headers=self.admin_headers)
        self.assertEqual(missing.status_code, 422, missing.text)

    def test_preview_is_read_only(self):
        original, replacement = self._pair()
        before = {
            "original": self.document_state(original),
            "replacement": self.document_state(replacement),
            "writes": self.write_counts(),
        }
        for _ in range(2):  # 重复预览也不得产生任何写入
            response = self._compare(original, replacement)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["data"]["comparable"])
        after = {
            "original": self.document_state(original),
            "replacement": self.document_state(replacement),
            "writes": self.write_counts(),
        }
        self.assertEqual(after, before)

    def test_preview_non_comparable_reasons_return_200_without_partial_diff(self):
        empty = self.add(chunk_count=0, char_count=0)
        replacement = self.add_with_chunks("r.txt", ["新内容"])
        response = self._compare(empty, replacement)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        data = response.json()["data"]
        self.assertFalse(data["comparable"])
        self.assertEqual(data["reason"], "empty_document")
        self.assertIsNone(data["summary"])
        self.assertEqual(data["rows"], [])
        self.assertIsNone(data["comparison_fingerprint"])

        pending = self.add_with_chunks("pending.txt", ["内容"], status="pending")
        data = self._compare(pending, replacement).json()["data"]
        self.assertEqual((data["comparable"], data["reason"]), (False, "not_ready"))

        disabled = self.add_with_chunks("disabled.txt", ["内容"], enabled=False)
        data = self._compare(replacement, disabled).json()["data"]
        self.assertEqual((data["comparable"], data["reason"]), (False, "not_eligible"))

        unembedded = self.add_with_chunks("unembedded.txt", ["内容"], embedded=False)
        data = self._compare(unembedded, replacement).json()["data"]
        self.assertEqual((data["comparable"], data["reason"]), (False, "incomplete"))

    def test_preview_allows_superseded_original(self):
        replacement = self.add_with_chunks("r.txt", ["新内容"])
        original = self.add_with_chunks("old.txt", ["旧内容"], superseded_at=_PAST)
        response = self._compare(original, replacement)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertTrue(data["comparable"])
        self.assertEqual(data["original"]["lifecycle_state"], "superseded")
        self.assertFalse(data["original"]["retrievable"])

    def test_preview_then_supersede_with_fingerprint_preserves_original_artifacts(self):
        original, replacement = self._pair()
        fingerprint = self._fingerprint(original, replacement)
        original_before = self.document_state(original)
        replacement_before = self.document_state(replacement)

        response = self.client.post(
            f"{self.base}/{original}/supersede", headers=self.admin_headers,
            json={"expected_revision": 0, "replacement_document_id": str(replacement),
                  "comparison_fingerprint": fingerprint},
        )
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertEqual(data["lifecycle_state"], "superseded")
        self.assertFalse(data["retrievable"])
        self.assertEqual(data["superseded_by_id"], str(replacement))
        self.assertEqual(data["governance_revision"], 1)

        original_after = self.document_state(original)
        self.assertEqual(original_after["status"], original_before["status"])
        self.assertEqual(original_after["index_version"], original_before["index_version"])
        self.assertEqual(original_after["stored_path"], original_before["stored_path"])
        self.assertEqual(original_after["chunk_count"], original_before["chunk_count"])
        self.assertEqual(original_after["chunks"], 2)
        self.assertEqual(original_after["char_count"], original_before["char_count"])
        self.assertIsNotNone(original_after["superseded_at"])
        self.assertEqual(original_after["superseded_by_id"], replacement)
        # 替代目标本身不被改写
        replacement_after = self.document_state(replacement)
        replacement_before.pop("chunks")
        replacement_after.pop("chunks")
        self.assertEqual(replacement_after, replacement_before)

    def test_preview_fingerprint_rejects_changed_governance_without_write(self):
        original, replacement = self._pair()
        fingerprint = self._fingerprint(original, replacement)
        patch = self.client.patch(
            f"{self.base}/{replacement}/governance", headers=self.admin_headers,
            json={"expected_revision": 0, "maintainer": "ops"},
        )
        self.assertEqual(patch.status_code, 200, patch.text)
        self.assertEqual(patch.json()["data"]["governance_revision"], 1)

        response = self.client.post(
            f"{self.base}/{original}/supersede", headers=self.admin_headers,
            json={"expected_revision": 0, "replacement_document_id": str(replacement),
                  "comparison_fingerprint": fingerprint},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("资料版本已变化，请重新预览差异后再确认", response.text)
        self.assertIsNone(self.fresh(original).superseded_at)
        self.assertEqual(self.fresh(original).governance_revision, 0)

        # 原文档自身 revision 变化同样应让旧指纹失效（CAS 用新 revision 也不能绕过）
        original2, replacement2 = self._pair(
            original_contents=("甲\n乙",), replacement_contents=("甲\n丙",))
        fingerprint2 = self._fingerprint(original2, replacement2)
        patch2 = self.client.patch(
            f"{self.base}/{original2}/governance", headers=self.admin_headers,
            json={"expected_revision": 0, "maintainer": "ops"},
        )
        self.assertEqual(patch2.status_code, 200, patch2.text)
        response = self.client.post(
            f"{self.base}/{original2}/supersede", headers=self.admin_headers,
            json={"expected_revision": 1, "replacement_document_id": str(replacement2),
                  "comparison_fingerprint": fingerprint2},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIsNone(self.fresh(original2).superseded_at)
        self.assertEqual(self.fresh(original2).governance_revision, 1)

    def test_preview_fingerprint_rejects_reindexed_original_without_write(self):
        original, replacement = self._pair()
        fingerprint = self._fingerprint(original, replacement)
        replacement_before = self.document_state(replacement)

        with SessionLocal() as db:
            from app.services.ingest import reset_document_for_reindex
            reset_document_for_reindex(db, db.get(Document, original))

        response = self.client.post(
            f"{self.base}/{original}/supersede", headers=self.admin_headers,
            json={"expected_revision": 0, "replacement_document_id": str(replacement),
                  "comparison_fingerprint": fingerprint},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("资料版本已变化，请重新预览差异后再确认", response.text)
        original_after = self.document_state(original)
        self.assertIsNone(original_after["superseded_at"])
        self.assertEqual(original_after["governance_revision"], 0)
        self.assertEqual(original_after["status"], "pending")
        self.assertEqual(original_after["index_version"], 1)
        self.assertEqual(original_after["chunks"], 0)
        self.assertEqual(self.document_state(replacement), replacement_before)

    def test_preview_fingerprint_rejects_changed_chunk_content(self):
        original, replacement = self._pair()
        fingerprint = self._fingerprint(original, replacement)
        with SessionLocal() as db:
            row = db.scalar(select(DocumentChunk).where(
                DocumentChunk.document_id == replacement,
                DocumentChunk.chunk_index == 0))
            row.content = "第一行\n被并发修改的内容"
            db.commit()

        response = self.client.post(
            f"{self.base}/{original}/supersede", headers=self.admin_headers,
            json={"expected_revision": 0, "replacement_document_id": str(replacement),
                  "comparison_fingerprint": fingerprint},
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("资料版本已变化，请重新预览差异后再确认", response.text)
        self.assertIsNone(self.fresh(original).superseded_at)
        self.assertEqual(self.fresh(original).governance_revision, 0)

    def test_supersede_without_fingerprint_remains_compatible(self):
        original, replacement = self._pair()
        response = self.client.post(
            f"{self.base}/{original}/supersede", headers=self.admin_headers,
            json={"expected_revision": 0, "replacement_document_id": str(replacement)},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["lifecycle_state"], "superseded")
        self.assertEqual(self.fresh(original).superseded_by_id, replacement)

    def test_supersede_rejects_invalid_fingerprint_shape(self):
        original, replacement = self._pair()
        for bad in ("A" * 64, "a" * 63, "g" * 64, 123):
            response = self.client.post(
                f"{self.base}/{original}/supersede", headers=self.admin_headers,
                json={"expected_revision": 0, "replacement_document_id": str(replacement),
                      "comparison_fingerprint": bad},
            )
            self.assertEqual(response.status_code, 422, f"{bad!r}: {response.text}")
        self.assertIsNone(self.fresh(original).superseded_at)


@unittest.skipUnless(TEST_URL, "设置 TEST_DATABASE_URL 才运行隔离数据库测试")
class DocumentGovernanceMigrationTests(unittest.TestCase):
    """治理列在新/旧库上 init 两遍幂等，默认值与自引用 FK 行为正确。"""

    @classmethod
    def setUpClass(cls):
        if engine.url != make_url(TEST_URL):
            raise RuntimeError("应用引擎未指向专用测试库，拒绝运行")
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        init_db(create_admin=False)

    @classmethod
    def tearDownClass(cls):
        engine.dispose()

    def setUp(self):
        gate.verify(gate.TEST_DATABASE_URL, gate.TARGET_OWNER, engine)
        _reset()

    def columns(self):
        with engine.begin() as conn:
            return {
                row[0] for row in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = 'documents'"
                ))
            }

    def _fixture(self, tag, count=1):
        with SessionLocal() as db:
            user = User(username=f"govmig_{tag}", role="admin", is_active=True, password_hash="x")
            db.add(user)
            db.flush()
            kb = KnowledgeBase(name=f"govmig-{tag}", collection_name="gm_" + uuid.uuid4().hex,
                               owner_id=user.id, is_public=True)
            db.add(kb)
            db.flush()
            ids = []
            for index in range(count):
                doc = Document(kb_id=kb.id, filename=f"{tag}-{index}.txt", file_ext=".txt",
                               file_size=1, stored_path=f"/tmp/{tag}-{index}.txt")
                db.add(doc)
                db.flush()
                ids.append(doc.id)
            db.commit()
            return kb.id, ids

    def test_governance_columns_and_defaults(self):
        expected = {"maintainer", "last_reviewed_at", "valid_from", "valid_until",
                    "enabled", "superseded_at", "superseded_by_id", "governance_revision"}
        self.assertTrue(expected <= self.columns())
        _, ids = self._fixture("defaults")
        with SessionLocal() as db:
            row = db.get(Document, ids[0])
            self.assertEqual(row.maintainer, "")
            self.assertTrue(row.enabled)
            self.assertEqual(row.governance_revision, 0)
            self.assertIsNone(row.superseded_at)
            self.assertIsNone(row.superseded_by_id)

    def test_double_init_idempotent(self):
        init_db(create_admin=False)
        init_db(create_admin=False)
        self.assertTrue({"superseded_by_id", "governance_revision"} <= self.columns())

    def test_legacy_documents_survive_migration_with_defaults(self):
        _, ids = self._fixture("legacy", 3)
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE documents DROP COLUMN governance_revision"))
            conn.execute(text("ALTER TABLE documents DROP COLUMN superseded_by_id"))
            conn.execute(text("ALTER TABLE documents DROP COLUMN enabled"))
        init_db(create_admin=False)
        init_db(create_admin=False)
        with SessionLocal() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(Document)), 3)
            rows = {row.id: row for row in db.scalars(select(Document)).all()}
        for doc_id in ids:
            row = rows[doc_id]
            self.assertTrue(row.enabled)
            self.assertEqual(row.governance_revision, 0)

    def test_superseded_by_fk_is_set_null_on_target_delete(self):
        kb_id, ids = self._fixture("fk", 2)
        original_id, replacement_id = ids
        with SessionLocal() as db:
            original = db.get(Document, original_id)
            original.superseded_at = _PAST
            original.superseded_by_id = replacement_id
            db.commit()
        # 直接 SQL 删除目标行，验证 DB 级 ON DELETE SET NULL（不走 ORM 缓存）
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM documents WHERE id = :rid"), {"rid": replacement_id})
        with engine.begin() as conn:
            row = conn.execute(text(
                "SELECT superseded_at, superseded_by_id FROM documents WHERE id = :oid"
            ), {"oid": original_id}).one()
        self.assertIsNone(row.superseded_by_id)
        self.assertIsNotNone(row.superseded_at)  # 保持不可检索，不复活
        del kb_id

    def test_fk_constraint_exists_in_catalog(self):
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conrelid = 'documents'::regclass AND contype = 'f' "
                "AND conkey = ARRAY["
                "(SELECT attnum::smallint FROM pg_attribute "
                " WHERE attrelid = 'documents'::regclass AND attname = 'superseded_by_id')]"
            )).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].confdeltype, "n")  # n = set null
