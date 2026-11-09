"""文档生命周期与可检索性的唯一判定来源（对象与 SQL 两套实现）。

冻结契约：优先级为
  已被替代（superseded_at 或 superseded_by_id 任一非空） -> superseded
  未启用（enabled=False）                                -> disabled
  now < valid_from                                       -> scheduled
  now >= valid_until                                     -> expired
  其余                                                    -> active

effective window 为半开区间 [valid_from, valid_until)。缺失审核时间只告警，
绝不参与可检索判定。对象级 helper 不访问数据库/网络；缺失安全字段（status、
cancel_requested、index_version、治理字段等）或字段类型非法（enabled=None、
naive 日期、负数/布尔 index_version 等）一律判定为不可检索——绝不静默放行。
非法输入在 ``lifecycle_state`` 中抛 ``ValueError``，``is_retrievable`` 捕获后
闭合为 False；真实 Document 行不受影响。

所有 SQL 谓词都从这里导出；``app/services/document_governance.py`` 与检索侧
（后续 T3-B/C）都必须引用同一实现，不允许各写一份条件。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.sql.elements import ColumnElement

logger = logging.getLogger(__name__)

# 生命周期状态取值；同时是 API 输出与 SQL 谓词的常量来源。
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SCHEDULED = "scheduled"
LIFECYCLE_EXPIRED = "expired"
LIFECYCLE_DISABLED = "disabled"
LIFECYCLE_SUPERSEDED = "superseded"
LIFECYCLE_STATES = (
    LIFECYCLE_ACTIVE,
    LIFECYCLE_SCHEDULED,
    LIFECYCLE_EXPIRED,
    LIFECYCLE_DISABLED,
    LIFECYCLE_SUPERSEDED,
)

_MISSING = object()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_datetime(value: Any, *, field: str = "datetime") -> datetime | None:
    """把 ORM 返回的 datetime 规范为 aware UTC。

    ``None`` 表示"该侧不限"。其它输入（naive datetime、数字、数字字符串等）
    不是显式时区的时间，宁可报错也不放大权限——由调用方决定 fail closed。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(f"文档治理时间字段 {field} 是 naive datetime，拒绝判定")
        return value.astimezone(timezone.utc)
    raise ValueError(f"文档治理时间字段 {field} 类型异常（{type(value).__name__}），拒绝判定")


def _get(document: Any, name: str) -> Any:
    """读取对象字段；缺失即无效输入（绝不给宽松默认）。"""
    value = getattr(document, name, _MISSING)
    if value is _MISSING:
        raise ValueError(f"对象缺少治理/安全字段 {name}，拒绝判定")
    return value


def _is_superseded(document: Any) -> bool:
    superseded_at = _get(document, "superseded_at")
    superseded_by_id = _get(document, "superseded_by_id")
    if superseded_at is not None:
        _coerce_datetime(superseded_at, field="superseded_at")
        return True
    # 链接字段非 None 即视为已被替代（非法 UUID 类型也是非 None，方向 fail closed）。
    return superseded_by_id is not None


def lifecycle_state(document: Any, *, now: datetime | None = None) -> str:
    """按冻结优先级派生生命周期状态。``now`` 缺省为当前 aware UTC。

    非法输入（缺字段、enabled 非 bool、naive 时间、``now`` 非 aware datetime）
    抛 ``ValueError``，绝不静默返回 active。
    """
    if _is_superseded(document):
        return LIFECYCLE_SUPERSEDED
    enabled = _get(document, "enabled")
    if enabled is not True and enabled is not False:
        raise ValueError("文档 enabled 字段必须是严格布尔值")
    if enabled is False:
        return LIFECYCLE_DISABLED
    moment = utc_now() if now is None else _coerce_datetime(now, field="now")
    valid_from = _coerce_datetime(_get(document, "valid_from"), field="valid_from")
    valid_until = _coerce_datetime(_get(document, "valid_until"), field="valid_until")
    if valid_from is not None and moment < valid_from:
        return LIFECYCLE_SCHEDULED
    if valid_until is not None and moment >= valid_until:
        return LIFECYCLE_EXPIRED
    return LIFECYCLE_ACTIVE


def is_retrievable(document: Any, *, now: datetime | None = None) -> bool:
    """可检索 = lifecycle_state==active 且入库侧全部就绪。

    入库侧与现有 ``source_documents.ready_document`` 的语义一致：
    ``status=='ready'`` 且 ``cancel_requested`` 为严格 False，且
    ``index_version`` 是非负整数（bool 不算）。缺失/类型非法一律 False。
    """
    try:
        if _get(document, "status") != "ready":
            return False
        if _get(document, "cancel_requested") is not False:
            return False
        index_version = _get(document, "index_version")
        if isinstance(index_version, bool) or not isinstance(index_version, int):
            return False
        if index_version < 0:
            return False
        return lifecycle_state(document, now=now) == LIFECYCLE_ACTIVE
    except (AttributeError, TypeError, ValueError):
        return False


def retrievable_conditions(*, now: datetime | None = None) -> tuple:
    """SQLAlchemy Document 行级谓词元组，与对象级 ``is_retrievable`` 同义。

    返回可直接 ``stmt.where(*retrievable_conditions())`` 的条件序列；
    ``now`` 缺省取 Python aware UTC（``utc_now()``），**不是** PG 语句时间。
    锁等待之后必须由调用者重新取 ``now`` 再判定，避免用陈旧时刻放行已过期行。
    """
    from app.models import Document

    moment = utc_now() if now is None else _coerce_datetime(now, field="now")
    conditions: list[ColumnElement] = [
        Document.status == "ready",
        Document.cancel_requested.is_(False),
        # index_version 必须非负：旧列没有 CHECK 约束，历史/异常行可能是负数，
        # 对象级 is_retrievable 已 fail closed，SQL 侧必须同样排除。
        Document.index_version >= 0,
        # superseded：任一字段非空即不可检索
        Document.superseded_at.is_(None),
        Document.superseded_by_id.is_(None),
        # disabled
        Document.enabled.is_(True),
        # scheduled 排除：valid_from 已到（moment >= valid_from）才可检索
        (Document.valid_from.is_(None)) | (moment >= Document.valid_from),
        # expired 排除：半开区间 [valid_from, valid_until)，now >= valid_until 即过期
        (Document.valid_until.is_(None)) | (moment < Document.valid_until),
    ]
    return tuple(conditions)
