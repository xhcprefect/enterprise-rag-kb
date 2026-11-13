"""T3-A 纯 schema/生命周期判定测试（不连数据库）。

覆盖：schema 坏输入/合法输入（含 +08:00 转 UTC）、治理字段必需的 DTO、
lifecycle 的缺失安全字段与坏值 fail closed、半开区间/优先级边界、
SQL 谓词编译与对象/SQL 一致性。
"""
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from app.schemas.knowledge import (
    DocumentGovernanceUpdate,
    DocumentRead,
    DocumentReenableRequest,
    DocumentSupersedeRequest,
)
from app.services.document_lifecycle import (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_DISABLED,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_SCHEDULED,
    LIFECYCLE_SUPERSEDED,
    is_retrievable,
    lifecycle_state,
    retrievable_conditions,
)

UTC = timezone.utc
BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
DOC_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
KB_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


class FakeDocument:
    """真实 Document 行字段形状的替身；缺字段测试用删属性实现。"""

    def __init__(self, **kwargs: Any) -> None:
        self.status = "ready"
        self.cancel_requested = False
        self.index_version = 1
        self.enabled = True
        self.superseded_at = None
        self.superseded_by_id = None
        self.valid_from = None
        self.valid_until = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class LifecycleStateTests(unittest.TestCase):
    def test_priority_superseded_over_disabled(self):
        doc = FakeDocument(enabled=False, superseded_at=BASE)
        self.assertEqual(lifecycle_state(doc), LIFECYCLE_SUPERSEDED)

    def test_priority_disabled_over_scheduled(self):
        doc = FakeDocument(enabled=False, valid_from=BASE + timedelta(days=1))
        self.assertEqual(lifecycle_state(doc), LIFECYCLE_DISABLED)

    def test_scheduled_before_window(self):
        doc = FakeDocument(valid_from=BASE + timedelta(seconds=1))
        self.assertEqual(lifecycle_state(doc, now=BASE), LIFECYCLE_SCHEDULED)

    def test_active_at_window_start(self):
        doc = FakeDocument(valid_from=BASE)
        self.assertEqual(lifecycle_state(doc, now=BASE), LIFECYCLE_ACTIVE)

    def test_active_inside_window(self):
        doc = FakeDocument(valid_from=BASE, valid_until=BASE + timedelta(days=1))
        self.assertEqual(lifecycle_state(doc, now=BASE + timedelta(hours=1)), LIFECYCLE_ACTIVE)

    def test_expired_at_until_boundary_half_open(self):
        doc = FakeDocument(valid_until=BASE + timedelta(days=1))
        self.assertEqual(lifecycle_state(doc, now=BASE + timedelta(days=1)), LIFECYCLE_EXPIRED)

    def test_not_expired_one_microsecond_before_until(self):
        doc = FakeDocument(valid_until=BASE + timedelta(days=1))
        self.assertEqual(
            lifecycle_state(doc, now=BASE + timedelta(days=1) - timedelta(microseconds=1)),
            LIFECYCLE_ACTIVE,
        )

    def test_superseded_by_id_alone_counts(self):
        self.assertEqual(lifecycle_state(FakeDocument(superseded_by_id=DOC_ID)), LIFECYCLE_SUPERSEDED)

    def test_active_no_window(self):
        self.assertEqual(lifecycle_state(FakeDocument()), LIFECYCLE_ACTIVE)

    def test_non_utc_offset_input_converted(self):
        # 2026-01-02 08:00+08:00 == 2026-01-02 00:00 UTC
        doc = FakeDocument(valid_from=datetime(2026, 1, 2, 8, 0, 0, tzinfo=timezone(timedelta(hours=8))))
        self.assertEqual(lifecycle_state(doc, now=datetime(2026, 1, 1, 23, 59, 59, tzinfo=UTC)), LIFECYCLE_SCHEDULED)
        self.assertEqual(lifecycle_state(doc, now=datetime(2026, 1, 2, 0, 0, 0, tzinfo=UTC)), LIFECYCLE_ACTIVE)


class LifecycleBadInputTests(unittest.TestCase):
    def _assert_value_error(self, doc, now=None):
        with self.assertRaises(ValueError):
            lifecycle_state(doc, now=now)

    def test_missing_governance_field_rejected(self):
        doc = FakeDocument()
        del doc.enabled
        self._assert_value_error(doc)

    def test_missing_superseded_field_rejected(self):
        doc = FakeDocument()
        del doc.superseded_at
        self._assert_value_error(doc)

    def test_enabled_none_rejected(self):
        self._assert_value_error(FakeDocument(enabled=None))

    def test_enabled_bad_type_rejected(self):
        self._assert_value_error(FakeDocument(enabled="yes"))

    def test_naive_date_rejected(self):
        self._assert_value_error(FakeDocument(valid_from=datetime(2026, 1, 1)))

    def test_bad_date_type_rejected(self):
        self._assert_value_error(FakeDocument(valid_from="2026-01-01T00:00:00+00:00"))

    def test_naive_now_rejected(self):
        self._assert_value_error(FakeDocument(), now=datetime(2026, 1, 1))

    def test_bad_now_type_rejected(self):
        self._assert_value_error(FakeDocument(), now=1735689600)

    def test_naive_superseded_at_rejected(self):
        self._assert_value_error(FakeDocument(superseded_at=datetime(2026, 1, 1)))


class IsRetrievableTests(unittest.TestCase):
    def test_retrievable_normal_document(self):
        self.assertIs(is_retrievable(FakeDocument(), now=BASE), True)

    def test_retrievable_not_ready_status(self):
        self.assertIs(is_retrievable(FakeDocument(status="processing"), now=BASE), False)

    def test_retrievable_missing_status(self):
        doc = FakeDocument()
        del doc.status
        self.assertIs(is_retrievable(doc, now=BASE), False)

    def test_retrievable_missing_cancel_requested(self):
        doc = FakeDocument()
        del doc.cancel_requested
        self.assertIs(is_retrievable(doc, now=BASE), False)

    def test_retrievable_missing_index_version(self):
        doc = FakeDocument()
        del doc.index_version
        self.assertIs(is_retrievable(doc, now=BASE), False)

    def test_retrievable_bool_index_version(self):
        self.assertIs(is_retrievable(FakeDocument(index_version=True), now=BASE), False)

    def test_retrievable_negative_index_version(self):
        self.assertIs(is_retrievable(FakeDocument(index_version=-1), now=BASE), False)

    def test_retrievable_missing_enabled_fails_closed(self):
        doc = FakeDocument()
        del doc.enabled
        self.assertIs(is_retrievable(doc, now=BASE), False)

    def test_retrievable_disabled(self):
        self.assertIs(is_retrievable(FakeDocument(enabled=False), now=BASE), False)

    def test_retrievable_superseded(self):
        self.assertIs(is_retrievable(FakeDocument(superseded_at=BASE), now=BASE), False)

    def test_retrievable_expired(self):
        self.assertIs(is_retrievable(FakeDocument(valid_until=BASE), now=BASE), False)

    def test_retrievable_scheduled(self):
        self.assertIs(is_retrievable(FakeDocument(valid_from=BASE + timedelta(days=1)), now=BASE), False)

    def test_retrievable_cancel_requested_none_fails_closed(self):
        self.assertIs(is_retrievable(FakeDocument(cancel_requested=None), now=BASE), False)

    def test_retrievable_missing_all_security_fields(self):
        class Empty:
            pass

        self.assertIs(is_retrievable(Empty(), now=BASE), False)

    def test_retrievable_real_document_defaults_match_lifecycle(self):
        from app.models import Document

        doc = Document(kb_id=KB_ID, filename="f.txt", file_ext=".txt", file_size=1, stored_path="/x")
        # 未 flush 的 ORM 对象尚未应用列默认值：这里显式给出真实行会有的字段。
        doc.status = "pending"
        doc.cancel_requested = False
        doc.index_version = 0
        doc.enabled = True
        doc.governance_revision = 0
        self.assertIs(is_retrievable(doc, now=BASE), False)  # pending 不可检索
        doc.status = "ready"
        self.assertIs(is_retrievable(doc, now=BASE), True)
        self.assertEqual(lifecycle_state(doc, now=BASE), LIFECYCLE_ACTIVE)


class SqlPredicateTests(unittest.TestCase):
    @staticmethod
    def _sql_predicates(now):
        conditions = retrievable_conditions(now=now)
        assert len(conditions) == 8
        return {
            str(condition.compile(compile_kwargs={"literal_binds": True}))
            for condition in conditions
        }

    def test_sql_conditions_compile_with_bound_now(self):
        sql = self._sql_predicates(BASE)
        self.assertTrue(any("documents.status = 'ready'" in s for s in sql))
        self.assertTrue(any("cancel_requested" in s for s in sql))
        self.assertTrue(any("superseded_at IS NULL" in s for s in sql))
        self.assertTrue(any("superseded_by_id IS NULL" in s for s in sql))
        self.assertTrue(any("enabled" in s for s in sql))
        self.assertTrue(any("valid_from" in s for s in sql))
        self.assertTrue(any("valid_until" in s for s in sql))

    def test_sql_requires_nonnegative_index_version(self):
        sql = self._sql_predicates(BASE)
        version_clause = [s for s in sql if "index_version" in s]
        self.assertEqual(len(version_clause), 1, sql)
        self.assertIn("index_version >= 0", version_clause[0])
        # 对象级同一负例也必须失败闭合
        self.assertIs(is_retrievable(FakeDocument(index_version=-1), now=BASE), False)

    def test_sql_and_object_agree_boundaries(self):
        sql = self._sql_predicates(BASE + timedelta(days=1))
        # 与对象级边界相同的时刻：到期即不可检索，窗口开始即可检索
        self.assertIs(
            is_retrievable(FakeDocument(valid_until=BASE + timedelta(days=1)), now=BASE + timedelta(days=1)),
            False,
        )
        self.assertIs(
            is_retrievable(FakeDocument(valid_from=BASE + timedelta(days=1)), now=BASE + timedelta(days=1)),
            True,
        )
        self.assertTrue(any("2026-01-02" in s for s in sql))


def _read_payload(**overrides):
    payload = {
        "id": DOC_ID,
        "kb_id": KB_ID,
        "filename": "f.txt",
        "file_ext": ".txt",
        "file_size": 1,
        "status": "ready",
        "error_message": None,
        "chunk_count": 0,
        "char_count": 0,
        "progress": 0,
        "cancel_requested": False,
        "ingest_attempts": 0,
        "next_attempt_at": None,
        "last_error_code": None,
        "created_at": BASE,
        "index_version": 1,
        "maintainer": "ops",
        "last_reviewed_at": None,
        "valid_from": None,
        "valid_until": None,
        "enabled": True,
        "superseded_at": None,
        "superseded_by_id": None,
        "governance_revision": 0,
        "lifecycle_state": "active",
        "retrievable": True,
    }
    payload.update(overrides)
    return payload


class DocumentReadSchemaTests(unittest.TestCase):
    def test_document_read_accepts_complete_payload(self):
        dto = DocumentRead.model_validate(_read_payload())
        self.assertEqual(dto.index_version, 1)
        self.assertIs(dto.retrievable, True)

    def test_document_read_governance_fields_required(self):
        for missing in ("index_version", "maintainer", "enabled", "lifecycle_state", "retrievable"):
            payload = _read_payload()
            del payload[missing]
            with self.assertRaises(ValidationError, msg=f"缺少 {missing} 的 DocumentRead 应校验失败"):
                DocumentRead.model_validate(payload)

    def test_document_read_nullable_still_required(self):
        payload = _read_payload()
        del payload["superseded_by_id"]
        with self.assertRaises(ValidationError):
            DocumentRead.model_validate(payload)

    def test_document_read_negative_index_version_rejected(self):
        with self.assertRaises(ValidationError):
            DocumentRead.model_validate(_read_payload(index_version=-1))

    def test_document_read_invalid_lifecycle_state_rejected(self):
        with self.assertRaises(ValidationError):
            DocumentRead.model_validate(_read_payload(lifecycle_state="pending"))

    def test_document_read_governance_revision_negative_rejected(self):
        with self.assertRaises(ValidationError):
            DocumentRead.model_validate(_read_payload(governance_revision=-1))

    def test_document_read_retry_fields_default_when_absent(self):
        """重试字段是新增的运维信息，不是治理契约：旧数据缺失时给确定默认，而不是 500。"""
        payload = _read_payload()
        for key in ("ingest_attempts", "next_attempt_at", "last_error_code"):
            payload.pop(key)
        dto = DocumentRead.model_validate(payload)
        self.assertEqual(dto.ingest_attempts, 0)
        self.assertIsNone(dto.next_attempt_at)
        self.assertIsNone(dto.last_error_code)

    def test_document_read_negative_ingest_attempts_rejected(self):
        with self.assertRaises(ValidationError):
            DocumentRead.model_validate(_read_payload(ingest_attempts=-1))

    def test_document_read_next_attempt_at_serializes_as_utc(self):
        """与既有时间字段一致：aware 值规范到 UTC 后再输出，naive 不补时区。"""
        dto = DocumentRead.model_validate(_read_payload(
            next_attempt_at=datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        ))
        dumped = dto.model_dump(mode="json")
        self.assertEqual(dumped["next_attempt_at"], "2026-01-01T00:00:00+00:00")

    def test_document_read_naive_date_not_fabricated(self):
        dto = DocumentRead.model_validate(_read_payload(valid_from=datetime(2026, 1, 1)))
        self.assertEqual(dto.valid_from, datetime(2026, 1, 1))  # 不替 naive 捏造时区
        serialized = dto.model_dump(mode="json")
        self.assertEqual(serialized["valid_from"], "2026-01-01T00:00:00")  # 不补 Z

    def test_document_read_offset_date_serialized_aware(self):
        dto = DocumentRead.model_validate(_read_payload(valid_until="2026-02-01T08:00:00+08:00"))
        self.assertEqual(dto.valid_until, datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC))
        # 序列化沿用既有 +00:00 形式（不改变原非治理字段的兼容输出）
        self.assertEqual(dto.model_dump(mode="json")["valid_until"], "2026-02-01T00:00:00+00:00")


class GovernanceUpdateSchemaTests(unittest.TestCase):
    def test_governance_update_minimal(self):
        update = DocumentGovernanceUpdate.model_validate({"expected_revision": 0, "enabled": True})
        self.assertEqual(update.changed_fields(), {"enabled": True})

    def test_governance_update_plus_8_converted_to_utc(self):
        update = DocumentGovernanceUpdate.model_validate(
            {"expected_revision": 1, "valid_from": "2026-02-01T08:00:00+08:00"}
        )
        self.assertEqual(update.valid_from, datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC))
        self.assertEqual(update.changed_fields(), {"valid_from": datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)})

    def test_governance_update_explicit_null_clears_date(self):
        # 契约：三个日期字段任意一个显式 null 都是合法的"清除"，不能当空补丁拒绝。
        for name in ("last_reviewed_at", "valid_from", "valid_until"):
            update = DocumentGovernanceUpdate.model_validate({"expected_revision": 1, name: None})
            self.assertEqual(update.changed_fields(), {name: None})

    def test_governance_update_omitted_date_not_in_changed(self):
        update = DocumentGovernanceUpdate.model_validate({"expected_revision": 1, "maintainer": "a"})
        self.assertNotIn("valid_from", update.changed_fields())

    def test_governance_update_rejects_empty_patch(self):
        with self.assertRaises(ValidationError):
            DocumentGovernanceUpdate.model_validate({"expected_revision": 0})

    def test_governance_update_rejects_null_maintainer_and_enabled(self):
        for key in ("maintainer", "enabled"):
            with self.assertRaises(ValidationError, msg=f"{key} 显式 null 必须拒绝"):
                DocumentGovernanceUpdate.model_validate({"expected_revision": 0, key: None})

    def test_governance_update_rejects_extra_fields(self):
        with self.assertRaises(ValidationError):
            DocumentGovernanceUpdate.model_validate({"expected_revision": 0, "enabled": True, "status": "ready"})

    def test_governance_update_rejects_bad_expected_revision(self):
        for bad in (True, 1.0, -1):
            with self.assertRaises(ValidationError, msg=f"expected_revision={bad!r} 必须拒绝"):
                DocumentGovernanceUpdate.model_validate({"expected_revision": bad, "enabled": True})

    def test_governance_update_rejects_non_strict_enabled(self):
        for bad in ("true", 1):
            with self.assertRaises(ValidationError, msg=f"enabled={bad!r} 必须拒绝"):
                DocumentGovernanceUpdate.model_validate({"expected_revision": 0, "enabled": bad})

    def test_governance_update_rejects_numeric_maintainer(self):
        with self.assertRaises(ValidationError):
            DocumentGovernanceUpdate.model_validate({"expected_revision": 0, "maintainer": 42})

    def test_governance_update_rejects_too_long_maintainer(self):
        with self.assertRaises(ValidationError):
            DocumentGovernanceUpdate.model_validate({"expected_revision": 0, "maintainer": "x" * 201})

    def test_governance_update_rejects_bad_dates(self):
        bad_dates = [
            1735689600,
            "1735689600",
            "2026-01-01",                 # naive，无时间无时区
            "2026-01-01T00:00:00",        # naive 无时区
            "2026-01-01T00:00Z",          # 无秒
            "2026-01-01T00:00:00Z00:00",  # 坏时区
            "not-a-date",
        ]
        for value in bad_dates:
            with self.assertRaises(ValidationError, msg=f"非法日期 {value!r} 必须拒绝"):
                DocumentGovernanceUpdate.model_validate({"expected_revision": 0, "valid_from": value})

    def test_governance_update_accepts_full_iso_and_aware_object(self):
        update = DocumentGovernanceUpdate.model_validate(
            {"expected_revision": 0, "last_reviewed_at": "2026-01-01T00:00:00Z"}
        )
        self.assertEqual(update.last_reviewed_at, datetime(2026, 1, 1, tzinfo=UTC))
        update2 = DocumentGovernanceUpdate.model_validate(
            {"expected_revision": 0, "valid_until": datetime(2026, 6, 1, tzinfo=UTC)}
        )
        self.assertEqual(update2.valid_until, datetime(2026, 6, 1, tzinfo=UTC))


class SupersedeReenableSchemaTests(unittest.TestCase):
    def test_supersede_request_strict(self):
        request = DocumentSupersedeRequest.model_validate(
            {"expected_revision": 2, "replacement_document_id": str(DOC_ID)}
        )
        self.assertEqual(request.replacement_document_id, DOC_ID)
        for bad in (True, 1.0, -1):
            with self.assertRaises(ValidationError, msg=f"supersede expected_revision={bad!r} 必须拒绝"):
                DocumentSupersedeRequest.model_validate(
                    {"expected_revision": bad, "replacement_document_id": str(DOC_ID)}
                )

    def test_reenable_request_strict_and_extra_forbid(self):
        self.assertEqual(DocumentReenableRequest.model_validate({"expected_revision": 0}).expected_revision, 0)
        with self.assertRaises(ValidationError):
            DocumentReenableRequest.model_validate({"expected_revision": 0, "enabled": True})


if __name__ == "__main__":
    unittest.main()
