import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

ChatMode = Literal["rag", "chat"]

# 摘要状态机：disabled（未启用）/ idle（已启用无任务）/ queued（有 pending）
# / processing（有 processing）/ ready（最新已发布）/ failed（最近一次失败）。
MemoryStateName = Literal["disabled", "idle", "queued", "processing", "ready", "failed"]


class ChatRequest(BaseModel):
    # 普通对话（mode=chat）不需要知识库
    kb_id: uuid.UUID | None = None
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    include_trace: bool = False
    # 省略时用服务端 RETRIEVAL_TOP_K；显式传入仍以调用方为准（前端滑块）。
    top_k: int | None = Field(default=None, ge=1, le=20)
    # 仅在新建会话时生效；续聊以会话里已存的模式为准
    mode: ChatMode = "rag"
    # 仅新会话接受草稿范围；已有会话通过带版本校验的 PATCH 更新。
    document_ids: list[uuid.UUID] | None = Field(default=None, max_length=5)
    scope_revision: int | None = Field(default=None, ge=0, strict=True)
    # 仅项目知识库（kind=project）使用：请求绑定的项目快照。与 document_ids 互斥
    # （任何显式，即便空列表，也不得同时指定）；已有会话须用快照切换 CAS 服务换 sid。
    project_snapshot_id: uuid.UUID | None = None


class RetrievalConfig(BaseModel):
    # 问答页滑块的初值。与 RETRIEVAL_TOP_K 同源，避免前后端各硬编码一个数字。
    top_k: int = Field(ge=1, le=20)


class DocumentScopeItem(BaseModel):
    document_id: str
    filename: str
    index_version: int = Field(ge=0)


class DocumentScopeState(BaseModel):
    document_scope: list[DocumentScopeItem] = Field(default_factory=list)
    scope_revision: int = 0
    # 项目知识库会话绑定的快照 id（会话固定，结果回放不借用后来快照）。非项目为 None。
    project_snapshot_id: uuid.UUID | None = None


class DocumentScopeUpdate(BaseModel):
    document_ids: list[uuid.UUID] = Field(max_length=5)
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


class ProjectSnapshotUpdate(BaseModel):
    """CAS 切换会话绑定的项目快照。

    ``expected_revision`` 严格非负且必给，必须等于会话当前 ``scope_revision`` 才有权
    切换；无变化相同快照幂等，不递增版本。
    """
    snapshot_id: uuid.UUID
    expected_revision: int = Field(ge=0, strict=True)

    model_config = {"extra": "forbid"}


class Citation(BaseModel):
    # 保留参考资料的原始编号，筛选后不能把 [3] 重新标成 [1]。
    source_index: int | None = Field(default=None, ge=1)
    # 用于跨轮引用重新校验；不修改表结构，保存在现有 JSON 引用字段中。
    index_version: int | None = Field(default=None, ge=0)
    # 来源类型：默认 'document'（普通上传文档）；项目检索为 'project'。旧数据无此字段按
    # document 解释，不破坏兼容。
    source_kind: str = "document"
    # 项目来源的身份字段（沿实际项目元数据，不造字段值）：源 / 快照 / 不可变文件版本 /
    # 相对路径 / 语言 / 符号与符号类型。
    source_id: str | None = None
    snapshot_id: str | None = None
    file_version_id: str | None = None
    relative_path: str | None = None
    language: str | None = None
    symbol: str | None = None
    symbol_kind: str | None = None
    # 快照的 manifest / 配置指纹（ProjectSnapshot.manifest_fingerprint / config_fingerprint），
    # 用于引用核对与配置换树拒绝。
    manifest_fingerprint: str | None = None
    config_fingerprint: str | None = None
    document_id: str | None = None
    filename: str | None = None
    chunk_index: int | None = None
    score: float | None = None
    snippet: str | None = None
    chunk_start: int | None = None
    chunk_end: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    line_start: int | None = None
    line_end: int | None = None


class MessageRead(BaseModel):
    id: uuid.UUID
    role: str
    status: str = "completed"
    turn_id: uuid.UUID | None = None
    reply_to_id: uuid.UUID | None = None
    content: str
    citations: list[Citation] | None = None
    latency_ms: int
    created_at: datetime
    # 来源为该轮 trace，不新增消息正文列；旧消息按未截断解释。
    truncated: bool = False
    finish_reason: str | None = None

    model_config = {"from_attributes": True}


class ConversationRead(DocumentScopeState):
    id: uuid.UUID
    kb_id: uuid.UUID | None = None
    mode: str = "rag"
    title: str
    # 旧响应与旧数据可能没有这个字段；null 表示未置顶。
    pinned_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationMetadataUpdate(BaseModel):
    """PATCH /chat/conversations/{id} 的唯一合法字段：title 和/或 pinned。

    额外字段（mode/kb_id/document_scope/租约等）一律拒绝，避免借元数据接口改状态。
    """
    title: str | None = None
    # strict=True：1/0、"true" 之类的宽松转换一律拒绝，只接受真正的 JSON 布尔。
    pinned: Annotated[bool, Field(strict=True)] | None = None

    model_config = {"extra": "forbid"}

    @field_validator("title")
    @classmethod
    def _normalize_title(cls, value: str | None) -> str:
        # 显式 null 非法；trim 后必须是 1–200 个字符。
        if not isinstance(value, str):
            raise ValueError("title 必须是字符串")
        trimmed = value.strip()
        if not 1 <= len(trimmed) <= 200:
            raise ValueError("title 长度必须在 1–200 个字符之间")
        return trimmed

    @field_validator("pinned")
    @classmethod
    def _strict_bool(cls, value: bool | None) -> bool:
        # Field(strict=True) 已经拦住 1/0/"true"；这里再拒绝显式 null。
        if type(value) is not bool:
            raise ValueError("pinned 必须是布尔值")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "ConversationMetadataUpdate":
        if not self.model_fields_set & {"title", "pinned"}:
            raise ValueError("至少需要提供 title 或 pinned 中的一项")
        return self


class ConversationDetail(ConversationRead):
    messages: list[MessageRead] = []


class ChatRequestState(DocumentScopeState):
    """只返回请求本人在当前权限下可读取的结果，不返回诊断或部分生成正文。"""
    request_id: uuid.UUID
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    status: Literal["processing", "completed", "failed", "cancelled"]
    message: MessageRead | None = None
    truncated: bool = False
    finish_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class MemoryStatus(BaseModel):
    """GET /chat/conversations/{id}/memory 返回；只反映持久化事实，不猜状态。

    ``state`` 只来自会话当前启停与数据库任务状态：
      - disabled  未启用摘要（mode 非 chat 或功能开关关闭）
      - idle      已启用，无 pending / processing，可接受新摘要
      - queued    存在 pending 任务
      - processing 存在 processing 任务（此时不能并发刷新水位）
      - ready     已有最新已发布摘要
      - failed    最近一次任务失败且无更新的 ready
    ``structured`` 仅在该会话确有已发布摘要时提供完整 JSON，否则为 None。
    """
    enabled: bool
    state: MemoryStateName = "disabled"
    epoch: int = 0
    # 已发布摘要的 version（既有 ready 时为非 None）。
    version: int | None = None
    # 摘要水位：已发布摘要真正覆盖到的最大完整轮次 seq。
    covered_turn_seq: int = 0
    # 会话下一个可分配轮次 seq（用于前端展示"已覆盖至第 N 轮"）。
    next_turn_seq: int = 0
    # 尚未纳入摘要的成功完整轮次数（已启用且计数可信时为
    # memory_completed_turns - covered_turns；legacy 无法证明配对的轮次归为 None）。
    # 注意 next_turn_seq 是"已分配最高序号"，失败/取消会有空号，不能拿
    # next-covered 冒充"剩余完整轮数"；前端"还有几轮未记住"请用本字段。
    uncovered_completed_turns: int | None = None
    # 已发布的结构化摘要（来自最近 ready 版本）；未就绪返回 None。
    structured: dict | None = None
    # 读取历史时因预算/未就绪发生截断或落后，前端据此显示"已记住全部"以外的状态。
    lag: bool = False
    truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None
    updated_at: datetime | None = None


class MemoryResetRequest(BaseModel):
    """POST /chat/conversations/{id}/memory/reset 的 CAS 载荷。

    expected_version 必须等于当前已发布摘要的 version（无摘要时为 0）才有权递增
    epoch；版本不符返回 409。

    "会话空闲" 只指没有**正在生成的问答**（无 ChatTurn processing / 有效 active
    lease）。后台摘要任务 pending/processing **不阻塞** reset：重置会原子地作废该
    会话的 pending/processing 摘要 job 与 lease、递增 epoch，原始消息保留；此后
    统计与水位可重新摘要，旧摘要模型即使迟到也不得再发布（epoch 不匹配拒写）。
    """
    expected_version: int = Field(ge=0, strict=True)

    model_config = {"extra": "forbid"}


class MemoryResetResult(BaseModel):
    """重置成功结果：epoch 已递增，旧结果不再复活，统计与水位可重新摘要。"""
    epoch: int
    version: int | None = None
    covered_turn_seq: int = 0
    reset: bool = True
