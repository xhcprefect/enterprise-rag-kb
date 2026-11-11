import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2000)
    is_public: bool = True


class KnowledgeBaseUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_public: bool | None = None


class KnowledgeBaseRead(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    collection_name: str
    is_public: bool
    document_count: int = 0
    created_at: datetime
    # 资源类型：upload / project。普通上传分支保持不变（默认 upload）；
    # 项目源对应 kind='project' 的专用私有 KB（写仅管理员，不经正常 KB 创建）。
    kind: str = "upload"
    # 项目源只读状态字段（kind='project' 时才有）；普通 KB 恒为 None/False。
    # 供前端 kbApi.list 识别 project 并展示同步状态；读取沿 KB ACL。
    project_source_id: uuid.UUID | None = None
    project_active_snapshot_id: uuid.UUID | None = None
    project_ready: bool = False
    project_revoked: bool = False
    # ---- A3 成员能力（冻结契约）----
    # membership_revision 来自 knowledge_bases 行；成员写/删除用户会递增。
    membership_revision: int = Field(default=0, ge=0)
    # 服务端按"当前用户 + 当前库"实时判定，不接受客户端按角色兜底：
    # - can_manage_documents：仅 kind='upload' 且 admin 或显式 maintainer；
    # - can_manage_members：仅 active admin（项目库管理员也可管理成员）。
    can_manage_documents: bool = False
    can_manage_members: bool = False

    model_config = {"from_attributes": True}


class DocumentRead(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    filename: str
    file_ext: str
    file_size: int
    status: str
    error_message: str | None
    chunk_count: int
    char_count: int
    progress: int
    cancel_requested: bool
    created_at: datetime
    # ---- 治理字段（冻结契约：所有 list/detail/upload/reindex/cancel/complete 路径）----
    # 全部 REQUIRED：缺失/None 会直接校验失败，绝不靠宽松默认放行伪装成治理值。
    # 派生字段由 Document 的 property（document_lifecycle 模块）提供。
    index_version: int = Field(ge=0)
    maintainer: str = Field(max_length=200)
    last_reviewed_at: datetime | None
    valid_from: datetime | None
    valid_until: datetime | None
    enabled: bool
    superseded_at: datetime | None
    superseded_by_id: uuid.UUID | None
    governance_revision: int = Field(ge=0)
    lifecycle_state: Literal["active", "scheduled", "expired", "disabled", "superseded"]
    retrievable: bool

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("created_at", "last_reviewed_at", "valid_from", "valid_until", "superseded_at")
    def _serialize_aware(self, value: datetime | None) -> str | None:
        """统一输出 ISO；aware 值规范到 UTC，naive 值不捏造时区（原样输出）。

        契约要求 aware 输出；naive 说明上游数据不规范，本层不替它补 ``Z``。
        """
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            return value.isoformat()
        return value.astimezone(timezone.utc).isoformat()


# 请求体治理时间字段的统一验证：只接受"显式时区的完整 ISO 字符串"或
# aware datetime。Pydantic 宽松解析（数字、数字字符串、naive、无秒等）之前
# 先在本层拒绝；null 只用于显式清空（omitted 不变）。
_ZONE_SUFFIX_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def _validate_governance_date(value: Any) -> datetime | None | Any:
    """field_validator 模式：非法日期输入直接抛 ValueError。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("必须包含显式时区（UTC offset），拒绝 naive datetime")
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text or not _ZONE_SUFFIX_RE.search(text):
            raise ValueError("必须是带显式时区的完整 ISO 8601 字符串（如 2026-01-01T00:00:00+08:00 或 ...Z）")
        if not re.search(r"\d{2}:\d{2}:\d{2}", text):
            raise ValueError("必须包含秒级时间（HH:MM:SS），拒绝无秒的日期/时间字符串")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("不是合法的 ISO 8601 日期时间") from error
        if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
            raise ValueError("必须包含显式时区（UTC offset），拒绝 naive 时间")
        return parsed.astimezone(timezone.utc)
    raise ValueError("只接受带时区的 ISO 8601 字符串或 aware datetime")


class _GovernanceDateMixin(BaseModel):
    last_reviewed_at: datetime | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    # mode="before" 很关键：必须在 Pydantic 宽松解析（数字时间戳、无秒、naive）
    # 之前拿到原始输入，否则非法值已经被转成 datetime 而无法拒绝。
    @field_validator("last_reviewed_at", "valid_from", "valid_until", mode="before")
    @classmethod
    def _strict_date(cls, value: Any) -> datetime | None:
        return _validate_governance_date(value)


class DocumentGovernanceUpdate(_GovernanceDateMixin):
    """PATCH /documents/{id}/governance 的请求体。

    契约：拒绝额外字段；``maintainer``/``enabled`` 不接受 null（显式 null 拒绝，
    omitted 才是"不变"）；时间字段显式时区 ISO 或 aware datetime 或 null（清除）；
    数字/数字字符串/naive/无秒/坏日期一律拒绝；空补丁（无任何可变字段）拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True, ge=0)
    # 默认 None 只表示"未提供"；显式 null 由下面的 before 校验器直接拒绝，
    # 因此业务层读到 None 时可能对应"未提供"，必须以 model_fields_set 判断。
    maintainer: Annotated[str | None, Field(strict=True, max_length=200)] = None
    enabled: Annotated[bool | None, Field(strict=True)] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "maintainer" in data and data["maintainer"] is None:
                raise ValueError("maintainer 不接受 null；清除请省略该字段")
            if "enabled" in data and data["enabled"] is None:
                raise ValueError("enabled 不接受 null；清除请省略该字段")
        return data

    @model_validator(mode="after")
    def _reject_empty_patch(self) -> "DocumentGovernanceUpdate":
        # 按"显式出现"判空：日期显式 null 是合法的清除意图，不能和非 None 混为一谈。
        provided = self.model_fields_set & set(self.changed_field_names())
        if not provided:
            raise ValueError("空补丁：至少提供一个可变字段")
        return self

    @staticmethod
    def changed_field_names() -> tuple[str, ...]:
        return ("maintainer", "last_reviewed_at", "valid_from", "valid_until", "enabled")

    def changed_fields(self) -> dict[str, Any]:
        """把"omitted 与显式提供"区分开：omitted 字段不出现在结果里。

        显式 null 的日期会以 ``None`` 出现在结果中（表示清除）；``exclude_unset``
        语义与 ``model_fields_set`` 一致。
        """
        return {
            name: getattr(self, name)
            for name in self.changed_field_names()
            if name in self.model_fields_set
        }


class DocumentSupersedeRequest(BaseModel):
    """POST /documents/{id}/supersede 请求体。

    ``comparison_fingerprint`` 可选：来自差异预览的 64 位小写十六进制指纹，
    仅用于检测预览后资料是否变化；不传时保持原有替代行为。
    """

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True, ge=0)
    replacement_document_id: uuid.UUID
    comparison_fingerprint: Annotated[
        str | None, Field(strict=True, pattern=r"^[a-f0-9]{64}$")
    ] = None


class DocumentReenableRequest(BaseModel):
    """POST /documents/{id}/reenable 请求体。"""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(strict=True, ge=0)


class DocumentUploadRead(DocumentRead):
    """小文件上传回执：在文档字段之外标明本次是否复用了同库已有内容。"""

    deduplicated: bool = False


class SourceChunkRead(BaseModel):
    chunk_index: int
    content: str
    location: dict


class SourceRead(BaseModel):
    document_id: uuid.UUID
    filename: str
    file_ext: str
    index_version: int
    chunk_index: int
    chunk_start: int
    chunk_end: int
    location: dict
    chunks: list[SourceChunkRead]
    truncated: bool


# ---------------- 分片上传 ----------------

class UploadSessionCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    file_size: int = Field(gt=0)
    # 老客户端不提供摘要时只新建、不自动复用；新客户端用完整文件摘要安全续传。
    content_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class UploadSessionRead(BaseModel):
    """会话状态：前端据此决定从第几片继续。"""

    upload_id: uuid.UUID
    filename: str
    file_size: int
    chunk_size: int
    total_chunks: int
    received: list[int]
    received_bytes: int
    status: str
    document_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class UploadCompleteRead(BaseModel):
    upload_id: uuid.UUID
    document: DocumentRead
    # true = 服务端按真实内容摘要复用了同知识库已有文档，未新建、未改名、未改旧引用。
    deduplicated: bool = False


class UploadChunkRead(BaseModel):
    """单片上传回执：created=false 表示该片此前已收到（幂等跳过）。"""

    index: int
    created: bool
    received: int
