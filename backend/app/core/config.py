"""应用配置：全部来自环境变量 / .env，便于容器化与多环境部署。"""
import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator

from pydantic_settings import BaseSettings, SettingsConfigDict

# 测试/离线环境显式禁用 .env：开关必须在导入本模块之前设置（SettingsConfigDict
# 在类构造时求值）。正常运行时保持旧行为，仍然读取工作目录下的 .env。
_TRUTHY = {"1", "true", "yes", "on"}
DOTENV_DISABLED = os.environ.get("RAG_DISABLE_DOTENV", "").strip().lower() in _TRUTHY
_ENV_FILE = None if DOTENV_DISABLED else ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    # 应用
    APP_NAME: str = "Enterprise RAG Knowledge Base"
    ENVIRONMENT: Literal["development", "production"] = "development"
    CHAT_MAX_PER_USER: int = Field(default=2, ge=1)
    CHAT_MAX_CONCURRENT: int = Field(default=50, ge=1)
    CHAT_LEASE_SECONDS: int = Field(default=300, ge=30, le=3600)
    # 控制接口文档（/docs、/redoc、/openapi.json）是否注册。
    # 默认关闭：/openapi.json 会把全部接口、参数与响应结构公开，等于给扫描器一份说明书。
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    # 同步 ORM、检索、上传使用的线程池；主问答的模型读取已原生异步化。
    # 此值不是模型并发上限，也不是当前版本已经压测验证的吞吐量。
    THREAD_POOL_SIZE: int = 200

    # 流式生命周期：首字/正文停顿/总时长互相独立，心跳不续正文截止时间。
    CHAT_STREAM_HEARTBEAT_SECONDS: float = Field(default=10.0, gt=0, le=60)
    CHAT_STREAM_FIRST_TOKEN_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=600)
    CHAT_STREAM_IDLE_TIMEOUT_SECONDS: float = Field(default=45.0, gt=0, le=600)
    CHAT_STREAM_TOTAL_TIMEOUT_SECONDS: float = Field(default=300.0, gt=0, le=3600)
    CHAT_STREAM_SEND_TIMEOUT_SECONDS: float = Field(default=15.0, gt=0, le=120)
    CHAT_STREAM_CANCEL_POLL_SECONDS: float = Field(default=1.0, gt=0, le=10)
    CHAT_STREAM_MAX_FRAME_BYTES: int = Field(default=65536, ge=1024, le=1048576)
    CHAT_STREAM_MAX_ANSWER_CHARS: int = Field(default=131072, ge=1024, le=1048576)
    LLM_STREAM_USAGE_GRACE_SECONDS: float = Field(default=0.5, ge=0, le=5)
    LLM_ASYNC_MAX_CONNECTIONS: int = Field(default=64, ge=1, le=1024)

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

    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_API_KEY: str = ""
    LLM_TIMEOUT: int = 180              # 长回答 + 思考模型可能较慢，留足余量
    LLM_STREAM_INCLUDE_USAGE: bool = True  # 请求流末尾实际用量；不支持 stream_options 的服务可显式关闭
    # DeepSeek 官方 Chat Completions 的 none 会关闭思考，low/high/max 会开启思考。
    # 默认留空、不发送该字段，兼容不支持 reasoning_effort 的其他服务；别名取决于服务商。
    LLM_REASONING_EFFORT: Literal["", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"] = ""

    # 模型名（当前生效的那个；切换服务商时同步改这里）
    EMBEDDING_MODEL: str = "bge-m3:latest"
    LLM_MODEL: str = "qwen3.5:0.8b"
    LLM_TEMPERATURE: float = 0.2
    LLM_MAX_OUTPUT_TOKENS: int = Field(default=2048, ge=1, le=32768)
    # 按所接模型的真实窗口配置；输入预算会预留最大输出和安全余量。
    LLM_CONTEXT_WINDOW_TOKENS: int = Field(default=32768, ge=1024, le=2097152)
    CONTEXT_TOKEN_SAFETY_MARGIN: int = Field(default=512, ge=0)

    # RAG
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 120
    RETRIEVAL_TOP_K: int = Field(default=6, ge=1, le=20)
    # 默认不改变现有召回阈值；在固定问题集评测后再按需提高。
    RETRIEVAL_MIN_SIMILARITY: float = Field(default=0.0, ge=0.0, le=1.0)
    # 单份文档最多占几条检索结果：长文档的分块彼此相似，不封顶会挤掉其他资料来源
    RETRIEVAL_MAX_PER_DOC: int = 3
    HYBRID_RETRIEVAL_ENABLED: bool = True
    KEYWORD_CANDIDATES: int = Field(default=60, ge=1, le=200)
    RETRIEVAL_NEIGHBOR_CHUNKS: int = Field(default=1, ge=0, le=2)
    RERANK_ENABLED: bool = False
    RERANK_BASE_URL: str = ""
    RERANK_API_KEY: str = ""
    RERANK_MODEL: str = ""
    RERANK_TIMEOUT: float = Field(default=3.0, gt=0, le=30)
    RERANK_MAX_CHARS: int = Field(default=2000, ge=128, le=8000)
    # 仅对依赖历史的追问补全检索问题；独立问题和普通对话不额外调用模型。
    RETRIEVAL_REWRITE_ENABLED: bool = True
    RETRIEVAL_REWRITE_TIMEOUT: float = Field(default=5.0, gt=0, le=60)
    RETRIEVAL_REWRITE_MAX_TOKENS: int = Field(default=256, ge=32, le=2048)
    RETRIEVAL_REWRITE_HISTORY_TURNS: int = Field(default=3, ge=1, le=10)
    RETRIEVAL_REWRITE_HISTORY_CHARS: int = Field(default=3000, ge=128, le=12000)

    # ===== 反馈回归在线作业（R1b 持久作业租约与重试）=====
    # 单次 claim 的租约；heartbeat 只续租约，不延长整个作业的 deadline。
    FEEDBACK_REGRESSION_JOB_LEASE_SECONDS: int = Field(default=300, ge=30, le=3600)
    # worker 轮询间隔（由 R2 使用）。
    FEEDBACK_REGRESSION_JOB_POLL_SECONDS: float = Field(default=2.0, ge=0.1, le=30)
    # 同一 job 的 worker 尝试上限；与 run.attempt_no（管理员新提交）独立。
    FEEDBACK_REGRESSION_JOB_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    # 仅显式可恢复错误在重试前的有界退避。
    FEEDBACK_REGRESSION_JOB_RETRY_DELAY_SECONDS: float = Field(default=5.0, ge=0, le=300)
    # 整个作业从第一次 claim 开始的总时限；排队等待不计。
    FEEDBACK_REGRESSION_JOB_TOTAL_TIMEOUT_SECONDS: int = Field(default=600, ge=30, le=3600)
    # 多轮记忆：只保留完整的 user + assistant 轮次。
    HISTORY_MAX_TURNS: int = Field(default=10, ge=0, le=100)
    HISTORY_MAX_CHARS: int = Field(default=8000, ge=0)
    MAX_UPLOAD_MB: int = 50
    UPLOAD_DIR: str = "storage/uploads"

    # 分片上传 / 断点续传
    UPLOAD_CHUNK_SIZE: int = 4 * 1024 * 1024   # 每片 4MB，由服务端下发
    UPLOAD_SESSION_TTL_HOURS: int = 24         # 未完成会话的保留时长

    # 入库任务
    INGEST_BATCH_SIZE: int = 8                 # 每批向量化的分块数
    INGEST_STALE_SECONDS: int = 300            # 心跳超时判定（大于单批耗时的数倍）
    CHUNK_PERSIST_BATCH: int = 500             # 分块落库批大小
    # 任务级重试：传输层（cloud_models._post）已经对 429/5xx/超时/网络错误
    # 内部重试 3 次；这里的次数覆盖"整轮尝试都失败"的情况，避免一次上游抖动
    # 就把文档永久钉在 failed、只能靠人工 reindex 恢复。
    # 只对可恢复错误排队重试（见 ingest_failures.classify_failure）；永久失败仍计一次失败。
    INGEST_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=10)
    # 退避基数：第 n 次失败后等待 RETRY_BASE * 2**(n-1) 秒（1→30s，2→60s…），
    # 上限 INGEST_RETRY_MAX_SECONDS。等待发生在队列里（status=pending +
    # next_attempt_at），不占用 worker 线程，也不阻塞其他文档。
    INGEST_RETRY_BASE_SECONDS: int = Field(default=30, ge=1, le=3600)
    INGEST_RETRY_MAX_SECONDS: int = Field(default=600, ge=1, le=86400)

    # ===== 普通对话摘要压缩（默认关闭，不新增后台模型调用）=====
    CHAT_MEMORY_ENABLED: bool = False          # false = 完全回到现有窗口裁剪
    # 已发布摘要 + 未覆盖完整轮次可占用的输入 token 预算；不超过可用输入预算。
    MEMORY_HISTORY_TOKEN_BUDGET: int = Field(default=8000, ge=256, le=524288)
    # 达到该比例或未归档完整轮次数达到阈值即入队摘要。
    MEMORY_TRIGGER_RATIO: float = Field(default=0.75, gt=0.0, le=1.0)
    MEMORY_TRIGGER_TURNS: int = Field(default=8, ge=1, le=1000)
    # 摘要后保留的最近完整轮次原文。
    MEMORY_KEEP_RECENT_TURNS: int = Field(default=4, ge=0, le=100)
    # 单次摘要输出的 token 上限与超时。
    MEMORY_SUMMARY_MAX_TOKENS: int = Field(default=1200, ge=64, le=32768)
    MEMORY_SUMMARY_TIMEOUT_SECONDS: float = Field(default=60.0, gt=0, le=3600)
    # 失败有限退避：最大尝试次数与重试等待。
    MEMORY_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=20)
    # 摘要任务租约与轮询间隔。
    MEMORY_JOB_LEASE_SECONDS: int = Field(default=300, ge=30, le=86400)
    MEMORY_POLL_SECONDS: float = Field(default=2.0, gt=0, le=120)
    # 单次摘要输入最多纳入的完整轮次数（硬界，防止无界 batch）。
    MEMORY_MAX_INPUT_TURNS: int = Field(default=200, ge=1, le=10000)

    # ===== 本地项目文档/源码索引 =====
    # 授权根目录映射 {root_key: 绝对路径}；默认空 = 不创建任何项目源。
    # 永远不写入本机真实授权目录，仅显式配置的键可用。
    PROJECT_ALLOWED_ROOTS: dict[str, str] = Field(default_factory=dict)
    # 受控 blob/快照存储目录（系统拥有），绝不把源路径当打开句柄。
    PROJECT_STORAGE_DIR: str = "storage/projects"
    # 单次同步硬界：文件数、单文件字节、总字节。
    PROJECT_MAX_FILES: int = Field(default=5000, ge=1, le=1000000)
    PROJECT_MAX_FILE_BYTES: int = Field(default=4 * 1024 * 1024, ge=1024)
    PROJECT_MAX_TOTAL_BYTES: int = Field(default=512 * 1024 * 1024, ge=1024)
    # 解析/embedding 批与超时；租约/轮询与摘要独立。
    PROJECT_PARSE_BATCH: int = Field(default=8, ge=1, le=1000)
    PROJECT_EMBEDDING_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0, le=3600)
    PROJECT_JOB_LEASE_SECONDS: int = Field(default=300, ge=30, le=86400)
    PROJECT_POLL_SECONDS: float = Field(default=2.0, gt=0, le=120)
    PROJECT_STALE_SECONDS: int = Field(default=300, ge=3, le=86400)
    PROJECT_MAX_ATTEMPTS: int = Field(default=3, ge=1, le=20)
    # 自动同步安静窗口秒数：纳入索引的文件在最近 N 秒内被修改则延后重试，
    # 避免编辑器分步保存的半成品被立即发布；0 表示关闭去抖。
    PROJECT_AUTO_SYNC_QUIET_SECONDS: int = Field(default=10, ge=0, le=3600)
    # 项目 KB 的向量 collection 前缀；同一源/配置共享，跨配置不误复用。
    PROJECT_VECTOR_PREFIX: str = "proj_"

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # 日志
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    LOG_MAX_BYTES: int = 10 * 1024 * 1024   # 单文件 10MB
    LOG_BACKUP_COUNT: int = 5               # 保留 5 份历史
    LOG_SLOW_REQUEST_MS: int = 3000         # 超过该耗时的请求按 WARNING 记录
    # 容器/云平台场景只保留 stdout；默认仍写按 component 分文件的滚动日志。
    LOG_STDOUT_ONLY: bool = False

    # ===== 运维观测 =====
    # Prometheus 采集令牌：空值表示不开放采集（/operations/metrics 不可用）。
    # 它是独立的采集凭据，管理员 JWT 不能代替。
    METRICS_TOKEN: str = ""
    # 运维摘要查询数据库的硬超时；超时返回明确的不可用状态，不阻塞请求。
    OPERATIONS_DB_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=30)

    @field_validator("LLM_REASONING_EFFORT", mode="before")
    @classmethod
    def normalize_reasoning_effort(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_runtime(self):
        if self.LLM_CONTEXT_WINDOW_TOKENS - self.LLM_MAX_OUTPUT_TOKENS - self.CONTEXT_TOKEN_SAFETY_MARGIN < 512:
            raise ValueError("模型上下文窗口必须为输入保留至少 512 token，请调大窗口或调小最大输出和安全余量")
        if self.CHUNK_SIZE <= 0 or not 0 <= self.CHUNK_OVERLAP < self.CHUNK_SIZE:
            raise ValueError("CHUNK_OVERLAP 必须大于等于 0 且小于 CHUNK_SIZE")
        if self.MAX_UPLOAD_MB <= 0 or not 1 <= self.UPLOAD_CHUNK_SIZE <= 16 * 1024 * 1024:
            raise ValueError("上传大小配置不合法")
        if self.INGEST_STALE_SECONDS < 3 or self.THREAD_POOL_SIZE < 1:
            raise ValueError("入库超时或线程池配置不合法")
        if self.INGEST_RETRY_MAX_SECONDS < self.INGEST_RETRY_BASE_SECONDS:
            raise ValueError("INGEST_RETRY_MAX_SECONDS 不能小于 INGEST_RETRY_BASE_SECONDS")
        if self.CHAT_MEMORY_ENABLED and self.MEMORY_HISTORY_TOKEN_BUDGET > (
            self.LLM_CONTEXT_WINDOW_TOKENS - self.LLM_MAX_OUTPUT_TOKENS - self.CONTEXT_TOKEN_SAFETY_MARGIN
        ):
            raise ValueError("MEMORY_HISTORY_TOKEN_BUDGET 必须不超过可用输入预算"
                             "（窗口 - 最大输出 - 安全余量）")
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
