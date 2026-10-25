"""应用配置：全部来自环境变量 / .env，便于容器化与多环境部署。"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    # 应用
    APP_NAME: str = "Enterprise RAG Knowledge Base"
    ENVIRONMENT: Literal["development", "production"] = "development"
    CHAT_MAX_PER_USER: int = Field(default=2, ge=1)
    CHAT_MAX_CONCURRENT: int = Field(default=50, ge=1)
    # 控制接口文档（/docs、/redoc、/openapi.json）是否注册。
    # 默认关闭：/openapi.json 会把全部接口、参数与响应结构公开，等于给扫描器一份说明书。
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # 线程池槽位上限（anyio 默认 40）。
    # 问答接口是 "同步生成器 + StreamingResponse"，Starlette 每吐一个 token 都要
    # 借一个线程执行 next()，整段流式期间该线程一直被占着等云端返回。
    # 40 个并发提问就把池占满，之后连 /health、登录都要排队——表现为"整个站点变卡"。
    # 实测 40→200 后 150 路并发流仍不拖慢普通请求（见 DEPLOY.md「并发能力」）。
    THREAD_POOL_SIZE: int = 200

    # 数据库
    DATABASE_URL: str = "postgresql+psycopg://postgres:123456@localhost:9999/ragdb"

    # 认证
    JWT_SECRET: str = "please-change-this-secret-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 720
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "Admin@123456"

    # 模型服务商：ollama（本地） / cloud（任意 OpenAI 兼容服务）
    EMBEDDING_PROVIDER: str = "ollama"
    LLM_PROVIDER: str = "ollama"

    # Ollama（两种 provider 都设为 ollama 时使用）
    OLLAMA_BASE_URL: str = "http://localhost:11434"

    # 云端（provider=cloud 时使用）。向量与对话各自独立配置，
    # 因此可以用 A 家的向量模型配 B 家的对话模型。
    EMBEDDING_BASE_URL: str = "https://api.siliconflow.cn/v1"
    EMBEDDING_API_KEY: str = ""
    EMBEDDING_TIMEOUT: int = 120        # 单次请求超时（秒）；大文档向量化会变长
    EMBEDDING_BATCH_SIZE: int = 16      # 单次请求最多的文本条数

    LLM_BASE_URL: str = "https://www.sheapi.top/v1"
    LLM_API_KEY: str = ""
    LLM_TIMEOUT: int = 180              # 长回答 + 思考模型可能较慢，留足余量

    # 模型名（当前生效的那个；切换服务商时同步改这里）
    EMBEDDING_MODEL: str = "bge-m3:latest"
    LLM_MODEL: str = "qwen3.5:0.8b"
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=2048, ge=1, le=32768)

    # RAG
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 120
    RETRIEVAL_TOP_K: int = 6
    # 默认不改变现有召回阈值；在固定问题集评测后再按需提高。
    RETRIEVAL_MIN_SIMILARITY: float = Field(default=0.0, ge=0.0, le=1.0)
    # 单份文档最多占几条检索结果：长文档的分块彼此相似，不封顶会挤掉其他资料来源
    RETRIEVAL_MAX_PER_DOC: int = 3
    # 多轮记忆：最多带多少轮历史（user + assistant 各算一条，所以实际条数是 2 倍）
    HISTORY_MAX_TURNS: int = 10
    # 历史总字符预算。超出时从最新往回保留，丢掉最早的，防止上下文过长拖慢或撑爆请求
    HISTORY_MAX_CHARS: int = 8000
    MAX_UPLOAD_MB: int = 50
    UPLOAD_DIR: str = "storage/uploads"

    # 分片上传 / 断点续传
    UPLOAD_CHUNK_SIZE: int = 4 * 1024 * 1024   # 每片 4MB，由服务端下发
    UPLOAD_SESSION_TTL_HOURS: int = 24         # 未完成会话的保留时长

    # 入库任务
    INGEST_BATCH_SIZE: int = 8                 # 每批向量化的分块数
    INGEST_STALE_SECONDS: int = 300            # 心跳超时判定（大于单批耗时的数倍）
    CHUNK_PERSIST_BATCH: int = 500             # 分块落库批大小

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 日志
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    LOG_MAX_BYTES: int = 10 * 1024 * 1024   # 单文件 10MB
    LOG_BACKUP_COUNT: int = 5               # 保留 5 份历史
    LOG_SLOW_REQUEST_MS: int = 3000         # 超过该耗时的请求按 WARNING 记录

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.CHUNK_SIZE <= 0 or not 0 <= self.CHUNK_OVERLAP < self.CHUNK_SIZE:
            raise ValueError("CHUNK_OVERLAP 必须大于等于 0 且小于 CHUNK_SIZE")
        if self.MAX_UPLOAD_MB <= 0 or not 1 <= self.UPLOAD_CHUNK_SIZE <= 16 * 1024 * 1024:
            raise ValueError("上传大小配置不合法")
        if self.INGEST_STALE_SECONDS < 3 or self.THREAD_POOL_SIZE < 1:
            raise ValueError("入库超时或线程池配置不合法")
        if self.ENVIRONMENT == "production" and (
            len(self.JWT_SECRET) < 32 or "please-change" in self.JWT_SECRET or "change-me" in self.JWT_SECRET
        ):
            raise ValueError("生产环境必须配置至少 32 位的随机 JWT_SECRET，不能使用模板值")
        return self

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
