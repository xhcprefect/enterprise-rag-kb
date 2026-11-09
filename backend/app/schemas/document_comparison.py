"""资料更新差异预览（只读）的响应模型。

冻结契约：

- 响应字段全部显式列出，不提供任何 ``extra`` 透传；``reason`` 与 ``kind``
  使用 ``Literal`` 固定取值。
- ``comparison_fingerprint`` 只用于后续 supersede 的陈旧检测，**不是**授权凭证，
  也不包含任何正文。
- 所有比较对象都是 ``DocumentChunk`` 中已入库的文本；本模块不涉及原文件解析。
"""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ComparisonKind = Literal["unchanged", "added", "removed", "modified"]
ComparisonReason = Literal[
    "ok",
    "not_ready",
    "not_eligible",
    "incomplete",
    "empty_document",
    "too_many_chunks",
    "too_many_chars",
    "too_many_units",
]
ComparisonLifecycleState = Literal[
    "active", "scheduled", "expired", "disabled", "superseded"
]


class ComparisonUnitRead(BaseModel):
    """一个比较单元：某个 chunk 内的一行（``str.splitlines()`` 语义）。"""

    model_config = ConfigDict(extra="forbid")

    chunk_index: int
    line_index: int
    text: str
    location: dict


class ComparisonRowRead(BaseModel):
    """一行差异；``original``/``replacement`` 按 ``kind`` 取值。"""

    model_config = ConfigDict(extra="forbid")

    kind: ComparisonKind
    original: ComparisonUnitRead | None
    replacement: ComparisonUnitRead | None


class ComparisonDocumentRead(BaseModel):
    """参与比较的文档快照；不含正文、路径或分块内容。"""

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    filename: str
    index_version: int
    governance_revision: int
    chunk_count: int
    char_count: int
    lifecycle_state: ComparisonLifecycleState
    retrievable: bool


class ComparisonSummaryRead(BaseModel):
    """按比较单元数统计的差异汇总。"""

    model_config = ConfigDict(extra="forbid")

    added: int
    removed: int
    modified: int
    unchanged: int


class DocumentComparisonRead(BaseModel):
    """``GET /documents/{doc_id}/comparison`` 的只读响应。

    不可比较时：``comparable=false``、``reason`` 给出原因、``summary=null``、
    ``rows=[]``、``comparison_fingerprint=null``；绝不返回部分 diff。
    """

    model_config = ConfigDict(extra="forbid")

    comparison_version: int = 1
    basis: Literal["ingested_chunks"] = "ingested_chunks"
    comparable: bool
    reason: ComparisonReason
    original: ComparisonDocumentRead
    replacement: ComparisonDocumentRead
    summary: ComparisonSummaryRead | None
    rows: list[ComparisonRowRead]
    comparison_fingerprint: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )
