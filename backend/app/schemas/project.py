"""本地项目文档/源码索引的 API 契约（Q 独占）。

写接口仅管理员；读接口沿用知识库 ACL（私有 project KB，root_key 映射不暴露绝对根）。
全部字段均以 P/F 既有模型与纯服务为准，绝不接收任意绝对路径。
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from app.services.project_paths import split_relative_path

MAX_RELATIVE_DIR = 1024
MAX_PATH_QUERY = 1024
MAX_SEARCH_QUERY = 2000
MAX_TOP_K = 20
MAX_PREVIEW_CHARS = 20000

# 可选定时自动同步：默认 300 秒检查一次；60 秒 ~ 24 小时。
AUTO_SYNC_DEFAULT_INTERVAL_SECONDS = 300
AUTO_SYNC_MIN_INTERVAL_SECONDS = 60
AUTO_SYNC_MAX_INTERVAL_SECONDS = 86400


def _validate_relative_dir(value: str, info) -> str:
    """先拒绝原始路径的绝对/盘符/UNC/反斜杠形式，再逐组件校验。

    绝不先把非法绝对路径 strip 成"看起来是相对"再放行；相对子目录只接受
    ``a/b`` 这类纯相对路径，组件复用 P 的 split_relative_path（拒绝 .. / ADS /
    设备名等 Windows 安全规则）。
    """
    raw = value or ""
    if "\\" in raw:
        raise ValueError("relative_dir 不允许反斜杠，请使用 /")
    if raw.startswith("/") or (len(raw) >= 2 and raw[1] == ":"):
        raise ValueError("relative_dir 必须是相对路径，不接受绝对/盘符/UNC 路径")
    value = raw.strip("/")
    if len(value) > MAX_RELATIVE_DIR:
        raise ValueError("relative_dir 过长")
    if not value:
        return ""
    try:
        split_relative_path(value)
    except ValueError as exc:
        raise ValueError(f"relative_dir 含非法路径组件: {exc}") from exc
    return value


# ---------------- 源（管理员写，读沿 KB ACL） ----------------

class ProjectSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # 服务端 PROJECT_ALLOWED_ROOTS 里的键；命中不到即拒绝，绝不接受绝对路径。
    root_key: str = Field(min_length=1, max_length=200)
    # 授权根下的相对子目录；可空 = 授权根本身。逐组件校验（拒绝 .. / ADS / 设备名等）。
    relative_dir: str = Field(default="", max_length=MAX_RELATIVE_DIR)

    @field_validator("root_key")
    @classmethod
    def _clean_root_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("root_key 不能为空")
        return value

    @field_validator("relative_dir")
    @classmethod
    def _clean_relative_dir(cls, value: str) -> str:
        return _validate_relative_dir(value, None)


class ProjectSourceRead(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    name: str
    root_key: str
    relative_dir: str
    # 仅回显 root_key，绝不暴露绝对根路径（root_fingerprint 不对外）。
    active_snapshot_id: uuid.UUID | None = None
    revoked_at: datetime | None = None
    # 自动同步配置与最近一次结果（错误消息只含固定安全文案，不含路径/正文）。
    auto_sync_enabled: bool = False
    auto_sync_interval_seconds: int = AUTO_SYNC_DEFAULT_INTERVAL_SECONDS
    auto_sync_next_check_at: datetime | None = None
    auto_sync_last_checked_at: datetime | None = None
    auto_sync_last_error_code: str | None = None
    auto_sync_last_error_message: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProjectAutoSyncUpdate(BaseModel):
    """PATCH 自动同步开关与间隔；间隔硬界由 schema 先校验（422）。"""

    enabled: bool
    interval_seconds: int = Field(
        default=AUTO_SYNC_DEFAULT_INTERVAL_SECONDS,
        ge=AUTO_SYNC_MIN_INTERVAL_SECONDS,
        le=AUTO_SYNC_MAX_INTERVAL_SECONDS,
    )


# ---------------- 快照 / 同步任务 ----------------

class ProjectSnapshotRead(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    status: str
    trigger_source: str = "manual"
    base_snapshot_id: uuid.UUID | None = None
    progress: int
    file_count: int
    total_bytes: int
    manifest_fingerprint: str | None = None
    config_fingerprint: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    cancel_requested: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------- 文件版本 / 源内容 ----------------

class ProjectFileRead(BaseModel):
    """源内一个文件版本（结果与文件列表共用）。"""

    file_version_id: uuid.UUID
    source_id: uuid.UUID
    snapshot_id: uuid.UUID | None = None
    relative_path: str
    content_sha256: str
    size: int
    language: str | None = None
    parse_status: str
    chunk_count: int
    char_count: int
    symbol: str | None = None
    symbol_kind: str | None = None
    created_at: datetime | None = None


# ---------------- 检索结果 ----------------

class ProjectSearchHit(BaseModel):
    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    file_version_id: uuid.UUID
    relative_path: str
    language: str | None = None
    symbol: str | None = None
    symbol_kind: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    chunk_index: int
    snippet: str = ""
    score: float | None = None
    parse_status: str = "ready"


class ProjectSearchRead(BaseModel):
    results: list[ProjectSearchHit] = Field(default_factory=list)
    total: int = 0
    snapshot_id: uuid.UUID


# ---------------- 快照原文范围读取 ----------------

class ProjectSourceChunkRead(BaseModel):
    chunk_index: int
    content: str
    line_start: int | None = None
    line_end: int | None = None


class ProjectSourcePreviewRead(BaseModel):
    """一次快照原文范围预览（与源元数据 ProjectSourceRead 区分命名）。"""

    source_id: uuid.UUID
    snapshot_id: uuid.UUID
    file_version_id: uuid.UUID
    relative_path: str
    language: str | None = None
    parse_status: str = "ready"
    chunk_start: int = 0
    chunk_end: int = 0
    chunks: list[ProjectSourceChunkRead] = Field(default_factory=list)
    truncated: bool = False
