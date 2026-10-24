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


# ---------------- 分片上传 ----------------

class UploadSessionCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    file_size: int = Field(gt=0)


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


class UploadChunkRead(BaseModel):
    """单片上传回执：created=false 表示该片此前已收到（幂等跳过）。"""

    index: int
    created: bool
    received: int
