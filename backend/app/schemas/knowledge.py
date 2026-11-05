import uuid
from datetime import datetime

from pydantic import BaseModel, Field


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

    model_config = {"from_attributes": True}


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
