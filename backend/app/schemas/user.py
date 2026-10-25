import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=128)
    display_name: str = Field(default="", max_length=128)
    email: str | None = None
    role: str = Field(default="user", pattern=r"^(admin|user)$")


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=128)
    email: str | None = None
    role: str | None = Field(default=None, pattern=r"^(admin|user)$")
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6, max_length=128)


class UserRead(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str
    email: str | None
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class ChangePassword(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6, max_length=128)


class ImportedUser(BaseModel):
    """导入成功的一行。``password`` 是明文初始密码，只在这个响应里出现一次。"""

    row: int
    username: str
    display_name: str
    email: str | None
    role: str
    password: str


class ImportFailureItem(BaseModel):
    row: int
    username: str
    reason: str


class ImportResult(BaseModel):
    total: int
    created: list[ImportedUser]
    failed: list[ImportFailureItem]
    receipt_filename: str
    # 回执 xlsx 的 base64。放在 JSON 里而不是另起一条下载接口，
    # 是因为回执只在导入那一刻存在（明文密码不落库），分开下载会多一次
    # 「这次导入对应哪份回执」的状态管理，容易拿错人的密码。
    receipt_base64: str
