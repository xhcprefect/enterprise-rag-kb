# 企业知识库问答系统（RAG）

基于 **FastAPI + LangChain + pgvector** 的企业级检索增强生成（RAG）知识库问答平台，
前端使用 **Vue 3 + Pinia + Vite + Element Plus**。模型服务商可在 **OpenAI 兼容云端服务**
（如硅基流动、DeepSeek）与 **Ollama 本地**之间切换，向量与对话可分别配置。

> 最新的安全修复、数据库兼容升级、测试与备份维护说明见 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端框架 | FastAPI + Uvicorn |
| 编排框架 | LangChain |
| 向量数据库 | PostgreSQL 18 + pgvector 0.8 |
| 向量模型 | bge-m3（1024 维）— 硅基流动云端 / Ollama 本地 |
| 对话模型 | deepseek-flash（DeepSeek 官方） / qwen3.5:0.8b（Ollama 本地） |
| 认证 | JWT（HS256）+ bcrypt 密码哈希 |
| 前端 | Vue 3 + TypeScript + Pinia + Vite + Element Plus + Vue I18n |

## 功能

- **登录鉴权**：JWT 令牌、角色权限（管理员 / 普通用户）、路由守卫
- **知识库管理**：多知识库隔离，每个知识库独立向量集合
- **文档管理**：支持 PDF / Word / TXT / Markdown / Excel / CSV 上传，后台异步解析入库
- **智能问答**：SSE 流式输出、Markdown 渲染、引用溯源（显示来源文档与原文片段）
- **双模式对话**：同一页面可切换「知识库问答」（检索 + 引用）与「普通对话」（不检索、直接生成），
  模式按会话固定，两种会话历史分开；知识库为空时自动进入普通对话
- **会话管理**：多轮对话上下文、历史会话切换与删除、并发租约与断流结果恢复
- **普通对话摘要**：可选后台压缩完整历史轮次，查看覆盖水位、摘要来源与重置状态
- **本地项目索引**：授权目录手动/可选定时自动同步、不可变快照、内容搜索、固定快照问答与历史引用预览
- **用量与费用**：实际 Token 与人民币预估费用总览、按小时/天/月趋势、日期/模型/调用类型筛选；管理员全站统计与模型单价设置，普通用户仅查看本人
- **用户管理**：管理员可创建/编辑/禁用/删除用户、重置密码
- **批量导入用户**：上传 Excel / CSV 批量建号，每人生成随机初始密码；
  逐行校验、逐行报告失败原因，导入完成即可下载含初始密码的回执（xlsx），
  并提供带角色下拉的导入模板
- **审计日志**：记录登录、知识库与文档变更、问答等操作
- **主题切换**：浅色、深色、跟随系统，覆盖登录页、管理页面、聊天正文与组件弹层
- **中英文界面**：简体中文 / English 即时切换，组件语言、日期、数字与页面标题同步更新

## 目录结构

```
enterprise-rag-kb/
├── backend/
│   ├── app/
│   │   ├── api/v1/        # 认证、用户、知识库、项目、聊天、审计、运维接口
│   │   ├── core/          # 配置、安全（JWT、bcrypt）
│   │   ├── db/            # 会话、建表初始化
│   │   ├── models/        # SQLAlchemy 模型
│   │   ├── schemas/       # Pydantic 校验模型
│   │   ├── services/      # 解析、切分、向量化、检索、生成、用户导入
│   │   └── main.py
│   ├── scripts/           # 运维脚本（向量索引迁移）
│   ├── storage/uploads/   # 上传文件存放（开发环境）
│   ├── storage/projects/  # 项目内容寻址 blob（开发环境）
│   ├── requirements.txt   # 开发依赖（宽松下限）
│   ├── requirements.lock  # 生产依赖锁定，供镜像构建
│   ├── Dockerfile
│   ├── worker.py          # ingest / summary / project / regression 独立角色入口
│   └── run.py
├── frontend/
│   ├── src/
│   │   ├── api/           # axios 封装 + SSE 客户端
│   │   ├── layouts/       # 主布局
│   │   ├── router/        # 路由 + 权限守卫
│   │   ├── stores/        # Pinia
│   │   └── views/         # 页面
│   ├── Dockerfile
│   ├── nginx.conf         # 生产 Nginx 配置（HTTP）
│   └── nginx/
│       ├── tls.conf       # 可选 HTTPS 入口（由 docker-compose.https.yml 挂载覆盖）
│       └── snippets/      # 两套配置共用的 location / 代理 / TLS 参数
├── deploy/
│   ├── init.sh            # 首次部署检查
│   ├── backup.sh          # 备份（数据库 + 上传文件 + 项目 blob + 校验清单）
│   ├── restore.sh         # 恢复（1–3 个参数，校验清单后恢复）
│   └── tests/
│       └── restore-drill.sh   # 隔离恢复演练（专用临时 Compose 项目）
├── docker-compose.yml     # db / api / worker / web；summary、project、regression 可选 profile
├── docker-compose.https.yml   # 可选 HTTPS 覆盖（80 跳转 + 443 站点）
├── DEPLOY.md              # 生产部署手册
├── start-backend.bat
└── start-frontend.bat
```

## 前置条件

1. **Docker**：pgvector 容器运行中（本例映射到宿主 `9999` 端口）
2. **模型服务**（云端 / 本地，可按需混用）：
   - **云端**（默认）：向量走硅基流动、对话走 DeepSeek 官方，两处 API Key 分别填到 `.env` 的
     `EMBEDDING_API_KEY` 与 `LLM_API_KEY`。无需本地 GPU，`BAAI/bge-m3` 向量模型当前免费。
   - **Ollama 本地**：安装后拉取模型，并把 `EMBEDDING_PROVIDER` / `LLM_PROVIDER` 改成 `ollama`
     ```bash
     ollama pull bge-m3
     ollama pull qwen3.5:0.8b
     ```
3. **Python 3.12+**、**Node.js 22.18+**

## 启动

### 后端

```bash
cd backend
copy .env.example .env      # 首次运行，按需修改
start-backend.bat           # 或：python -m venv .venv && .venv\Scripts\pip install -r requirements.txt && .venv\Scripts\python run.py
```

启动后访问 <http://localhost:8000/docs> 查看接口文档。

### 前端

```bash
cd frontend
npm install
npm run dev
```

访问 <http://localhost:5173>。

### 独立后台任务

API 和前端启动后，还需在 `backend` 目录的独立终端启动入库 worker；启用摘要、项目或在线回归重跑时，再各自启动对应角色：

```bash
python worker.py --role ingest
python worker.py --role summary
python worker.py --role project
python worker.py --role regression
```

上面四条命令分别占用一个进程。`ingest` 是默认角色；没有相应 worker 时，文档入库、摘要、项目同步或在线回归作业会一直排队。Compose 默认启动入库角色；其余角色按需启用：

```bash
docker compose --profile summary --profile project --profile regression up -d
```

`regression` 角色消费管理员显式提交的反馈回归后台重跑作业，没有默认自动入队开关；未启动该进程时作业保持排队。管理员在反馈处理页显式提交、取消，并可凭原 `request_id` 只读找回提交结果（不重放写）；作业完成后可比较新旧答案/引用，人工核验收敛到具体 run 与 result 指纹。新作业 `status` 从 `queued` 开始，人工核验 `review.status` 初始为 `unknown`。**后台重跑会真实调用所配置的模型服务，产生新的模型调用与实际用量记录**；费用按当前单价配置计算，单价缺失时费用保持未知而不是 0。机器断言（答案/引用指纹、对比指标）只说明与旧结果是否一致，不等同于语义正确，两者仍需人工核验区分。旧 manual 轮次的准备/导出/导入保持兼容。在线回归的接口字段以对应后端版本为准。

每个 worker 进程都会在 `worker_heartbeats` 表中登记自己的实例 UUID，并用独立守护线程每 10 秒做一次短事务心跳——空闲、长任务和模型网络等待期间都有效。任务表里的 `heartbeat_at` 只证明“有任务被领取”，进程空闲时无法证明存活，因此不能替代实例心跳。心跳写入失败只记日志，不影响真实任务；退出时有界等待并尽力写入 `stopped_at`。

管理员可在「运维」页或 `GET /api/v1/operations/workers` 查看固定顺序 `ingest` / `summary` / `project` / `regression` 四个角色的实例心跳与任务队列：启用状态、`disabled`/`not_started`/`running`/`stale`/`stopped`/`unknown` 状态、存活实例数（45 秒内有新鲜心跳）、待处理/处理中/心跳超时任务数与最老等待、以及最近一次失败的安全错误码与更新时间。该接口需要管理员权限并返回 `Cache-Control: no-store`；查询失败时 `degraded=true` 且计数为 `null`，不会补零冒充“队列为空”。

普通对话摘要默认关闭。在 `backend/.env` 设置 `CHAT_MEMORY_ENABLED=true` 后启用；默认累计 8 个完整轮次或历史预算达到 75% 时触发，保留最近 4 轮。摘要只辅助历史上下文，原始消息仍保留，不提供跨会话记忆。生成、重置和旧任务收尾使用版本与租约校验，重置后迟到任务不能重新发布旧摘要。

项目索引默认没有授权根。管理员先配置 `PROJECT_ALLOWED_ROOTS`（例如 `{"source":"/srv/projects"}`），再在「项目索引」登记 `root_key` 与相对目录并手动同步。Compose 中 API 与项目 worker 必须把源目录只读挂载到相同路径，并共用 `PROJECT_STORAGE_DIR` 持久存储；示例见编排文件注释。项目库通过同步更新，不能走普通文档上传接口。

项目问答首次发送时选择 ready 快照；后续同步不会改变已有会话的绑定。显式切换通过 `scope_revision` 检查并发冲突；旧引用始终读取当时的快照和文件版本。读取引用及模型等待结束后均重新检查源状态、访问权和快照状态。

### 主题与界面语言

登录页右上方及登录后顶栏提供主题、语言两个菜单：

- 主题可选浅色、深色或跟随系统。首次使用跟随系统配色，手动选择优先；跟随系统时会响应系统配色变化。
- 语言可选简体中文和 English；首次按浏览器语言选择（英语使用 English，其余回退简体中文）。
- 偏好保存在当前浏览器的 `rag_theme` / `rag_locale` 中，刷新和重新登录后保留；浏览器禁用存储时仍可在当前页面切换。
- 切换不刷新页面、不清空正在编辑的表单、不重建上传任务或当前会话。文档、文件名、已有问题与回答、后端原始错误内容不会自动翻译。
- 翻译按页面维护在 `frontend/src/i18n/messages/`；`npm test` 会检查中英文键对齐、页面引用键存在、消息语法及首屏偏好初始化。

### 动态路由与权限

- 未登录时仅注册登录页和兜底路由。登录后根据 `/api/v1/auth/me` 返回的角色，使用 Vue Router 的 `addRoute` 动态注册业务页面；刷新页面时会重新获取用户信息并恢复路由。
- 普通用户可访问智能问答、知识库、文档、本人用量与费用和个人中心；管理员额外可访问用户管理和审计日志，并可查看全站用量、管理模型单价。未授权或不存在的地址统一回到智能问答页。
- 业务路由、角色限制和侧栏菜单统一维护在 `frontend/src/router/routes.ts`。`meta.roles` 声明允许的角色，`meta.menu` 声明侧栏入口；不配置菜单的页面仍可通过业务链接访问。
- 文档页通过 `meta.activeMenu` 高亮知识库菜单；菜单标题沿用中英文翻译键。退出登录、切换账号或更新角色时清理旧路由，避免权限残留。
- 未登录访问业务地址会保留原路径、查询参数和锚点，登录后回跳。前端路由仅控制页面访问，接口鉴权仍由后端执行；本次改造没有新增后台可编辑的菜单或权限接口。
- `npm test` 包含动态注册、角色过滤、深链接刷新、登录回跳和退出清理的路由回归测试。

### 前端构建与分包

- 页面继续按路由懒加载。Element Plus 通过 `unplugin-vue-components` 按需引入组件、指令和样式，入口不再全量注册组件库或加载完整样式。
- `frontend/build/chunks.ts` 将 Vue / Router / Pinia、国际化、Axios、图标、Markdown 和代码高亮拆为独立依赖包；其他依赖由 Rollup 按实际引用关系自动分包，不统一塞进一个 `vendor` 包。
- Element Plus 保留自动分包与未使用代码剔除，避免手动合并组件库总入口后带入未使用组件。消息提示、确认弹窗、基础样式和暗色变量显式保留，代码高亮样式随聊天页加载。
- `npm run build` 同时生成 `frontend/dist/.vite/manifest.json`，可用于检查静态依赖与异步页面的加载关系。
- `npm test` 包含真实配置的内存生产构建：检查单个 JavaScript 包不超过 500 kB、应用入口不超过 200 kB，并验证登录页不加载 Markdown、代码高亮、表格、上传和文件摘要依赖，以及 `v-loading` 与弹层样式仍然存在。

### 默认账号

| 用户名 | 密码 | 角色 |
|---|---|---|
| admin | Admin@123456 | 管理员 |

> ⚠️ 首次登录后请立即在「个人中心」修改密码；生产环境务必修改 `.env` 中的 `JWT_SECRET`。

## 配置说明（backend/.env）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DATABASE_URL` | pgvector 连接串 | `postgresql+psycopg://postgres:123456@localhost:9999/ragdb` |
| `JWT_SECRET` | JWT 签名密钥，**生产必须修改** | 示例值 |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | 令牌有效期（分钟） | 720 |
| `EMBEDDING_PROVIDER` | 向量服务商：`cloud` / `ollama` | `cloud` |
| `LLM_PROVIDER` | 对话服务商：`cloud` / `ollama` | `cloud` |
| `EMBEDDING_BASE_URL` | 向量服务地址（OpenAI 兼容） | `https://api.siliconflow.cn/v1` |
| `EMBEDDING_API_KEY` | 向量服务 Key，**不要提交** | 空 |
| `EMBEDDING_TIMEOUT` | 向量请求超时（秒） | 120 |
| `EMBEDDING_BATCH_SIZE` | 单次向量化请求的文本条数 | 16 |
| `LLM_BASE_URL` | 对话服务地址（OpenAI 兼容） | `https://api.deepseek.com` |
| `LLM_API_KEY` | 对话服务 Key，**不要提交** | 空 |
| `LLM_TIMEOUT` | 对话请求超时（秒） | 180 |
| `LLM_STREAM_INCLUDE_USAGE` | 请求云端流结束后的实际用量；不支持 `stream_options` 的兼容网关可关闭 | `true` |
| `LLM_REASONING_EFFORT` | DeepSeek 设为 `none` 关闭思考；不支持该参数的服务留空 | `none`（示例配置） |
| `OLLAMA_BASE_URL` | Ollama 服务地址 | `http://localhost:11434` |
| `EMBEDDING_MODEL` | 向量模型 | `BAAI/bge-m3` |
| `LLM_MODEL` | 对话模型 | `deepseek-flash` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 文本切分粒度 | 800 / 120 |
| `RETRIEVAL_TOP_K` | 默认检索条数（问答页滑块初值；调用方未显式指定时生效） | 6 |
| `RETRIEVAL_MAX_PER_DOC` | 单份文档最多占几条检索结果 | 3 |
| `MAX_UPLOAD_MB` | 单文件大小上限 | 50 |
| `INGEST_MAX_ATTEMPTS` | 可恢复的文档入库总尝试次数（含首次） | 3 |
| `INGEST_RETRY_BASE_SECONDS` / `INGEST_RETRY_MAX_SECONDS` | 任务级指数退避的起始/最大秒数 | 30 / 600 |

### 文档入库失败重试

入库 worker 对明确可恢复的网络、模型上游、限流、存储及数据库故障进行有界重试，等待以 `pending` 任务留在数据库队列，不占 worker 执行线程。`INGEST_MAX_ATTEMPTS` 是任务级总尝试次数，包含首次处理；默认最多 3 次。每次失败等待时间按 `INGEST_RETRY_BASE_SECONDS × 2^(已失败次数-1)` 增长，并封顶于 `INGEST_RETRY_MAX_SECONDS`，默认退避为 30 秒、60 秒。

不支持的格式、无可提取内容、损坏或加密文件、源文件缺失及明确的模型鉴权拒绝不会自动重试；请修复配置或文件后重新上传，或对可恢复的源文档执行“重建索引”。权限/磁盘等存储错误仍受最大尝试次数限制。运维接口和指标分别暴露等待退避与终结失败数量；文档列表用安全的错误码映射显示重试阶段、剩余等待和下一步操作，不展示任意上游异常正文。

`ingest_attempts` 记录当前索引代次内失败的任务级尝试次数（永久失败也记一次），不是单次上游 HTTP 请求次数。未知异常也受同一次数上限约束，不会无限重试。成功入库、明确取消或手工重建索引会清空计数与退避时间；重试沿用已经完整保存的解析/向量批次检查点。进程停机与租约超时属于任务交回，不消耗失败次数。退避中的取消请求可立即被领取清理，不必等倒计时结束；运维的 `retry_waiting` 排除这些取消请求，既有 `queued` 仍包含全部 pending/processing 文档。

Ingest worker 启动初始化、stale 恢复扫描与领取短事务遇到已建立连接失效、连接池超时、驱动明确的 `ConnectionTimeout` 或 PostgreSQL 连接/事务瞬态 SQLSTATE 时，会用新 Session/事务重新执行，失效连接由连接池处理；进程级退避从 2 秒指数增长，封顶 30 秒，可由 SIGTERM/SIGINT 即时打断。每次成功扫描或领取（包括队列为空）都会清零连续故障退避。认证、schema、SQL/约束、编程错误等永久数据库错误会立即使 worker 失败退出，交由 supervisor/Compose 的进程重启策略处理；异常日志只包含阶段、类型和安全 SQLSTATE，不包含 SQL、连接 URL 或异常正文。若 claim 的提交确认丢失，worker 不会重放旧任务对象或执行任务；新事务重新查询，已提交但确认未知的租约留给 stale 恢复扫描，数据库领取故障不会占用文档失败预算。

psycopg/libpq 在建连失败时可能只返回无 SQLSTATE 的 `OperationalError`，真实密码认证失败也属于此类。`pgconn.needs_password` / `used_password` 不能区分临时网络故障与永久配置错误，因此普通无 SQLSTATE 建连失败保守退出，不按异常正文判断；连接拒绝若只提供这种不明确诊断，也不会在进程内重试。只有明确的 `ConnectionTimeout`、SQLAlchemy 确认的已建立连接失效、连接池超时或白名单 SQLSTATE 会触发恢复。

### DeepSeek 关闭思考模式

示例配置使用 DeepSeek 官方 `https://api.deepseek.com`、`deepseek-flash` 模型，并设置
`LLM_REASONING_EFFORT=none`。该值作为 Chat Completions 的 `reasoning_effort` 发送，
按照[官方接口说明](https://api-docs.deepseek.com/api/create-chat-completion)关闭模型端思考，
而非仅在界面隐藏思考内容。知识库问答、普通对话、非流式调用和追问补全共用这一配置。

- 配置留空时不发送此字段；切换到不支持该参数的其他兼容服务时应留空。
- `low` / `high` / `max` 会开启思考，不能用来关闭；拼写错误会在后端启动时被配置校验拦截。
- 修改后需重启 API。Compose 部署需重建容器才能载入 `.env` 的变化；代码也有变化时需重新构建后端镜像。
- 可在 `backend` 目录运行 `.\.venv\Scripts\python.exe -m scripts._probe_llm --live` 做单次核验。
  此命令仅发送固定公开合成文本，最多生成 64 token，可能产生少量费用；
  只报告响应长度、用量和耗时，不输出密钥、正文或思考内容。`reasoning_tokens=null` 表示服务未报告该字段，不能解读为零。

## 接口返回规范

除 SSE 流式问答与探活接口外，`/api/v1` 下所有接口统一返回：

**成功**

```json
{ "code": 0, "success": true, "message": "ok", "data": { } }
```

**失败**（HTTP 状态码同时保留语义，如 401/403/404/409/422/500）

```json
{ "code": 404, "success": false, "message": "知识库不存在", "data": null }
```

| 字段 | 说明 |
|---|---|
| `code` | 成功恒为 `0`；失败等于 HTTP 状态码 |
| `success` | 前端只需判断它即可区分成败 |
| `message` | 可直接展示给用户的中文提示；参数校验失败时为「参数校验失败 —— 字段: 原因」 |
| `data` | 业务数据；无返回内容的操作（删除、退出）为 `null`。参数校验失败时为脱敏后的校验明细数组（不含原始 input） |

分页接口的 `data` 为 `{"total": 100, "items": [...]}`。

**例外**

- `POST /api/v1/chat/stream`：返回 `text/event-stream`，事件为 `meta` / `delta` / `done` / `error`。
  `meta.citations` 固定为空数组；候选资料不提前作为引用公开。`done` 返回 `latency_ms` 和最终 `citations`，前端仅在完整收到 `done` 后显示引用。
- `GET /health`、`GET /`：探活/入口信息，保持扁平结构，便于监控直接取字段。
- `GET /api/v1/users/import/template`：直接返回 xlsx 二进制（`Content-Disposition: attachment`），
  不套 `data` 字段。

各接口在 <http://localhost:8000/docs> 中的响应模型即为上述结构。
前端 `src/api/client.ts` 的拦截器会自动解包 `data`，业务代码仍按 `const { data } = await api.x()` 取值。

## RAG 流程

```
上传文档
  → 解析为纯文本（PyMuPDF / python-docx / pandas）
  → RecursiveCharacterTextSplitter 按中文标点切分
  → bge-m3 生成 1024 维向量
  → 写入 pgvector（collection 按知识库隔离）

用户提问（知识库问答模式）
  → 常见指代/省略追问：结合最近完整轮次补全检索问题；独立问题直接检索
    （明确追问上一轮来源时，按文档、分块、索引代次重新核验原文）
  → bge-m3 向量化检索问题（每次查询只算一次向量，候选放大时复用）
  → pgvector 向量检索 + PostgreSQL 中文片段/编号/型号关键词检索，按两路排名融合
  → 可选外部重排（默认关闭，超时保留原排序）
  → 按文档封顶（单份长文档最多占 RETRIEVAL_MAX_PER_DOC 条，候选不足时回填）
  → 按需补充同结构相邻片段，保留页码/章节/表格行范围
  → 统一预算内组装「参考资料 + 完整历史轮次 + 原始问题」提示词
  → 对话模型流式生成（SSE 推送；无法确定指代时先请求澄清）
  → 保存消息、最终引用来源、审计日志

用户提问（普通对话模式）
  → 跳过向量化与检索，直接用通用提示词组装「历史对话 + 问题」
  → 对话模型流式生成（SSE 推送）
  → 保存消息（引用为 NULL）、审计日志
```

**引用来源规则**：检索候选不等于回答依据。回答完成后，仅保留正文中以 `[1]`、`[2]` 等编号明确引用的候选片段；未引用的资料不会全部列出。资料不足时，模型按提示词以“根据现有知识库无法回答”开头拒答，并返回空引用；代码、思考内容、数字链接和无效编号不作为引用。来源包含 `source_index` 原始编号，筛选后不会把正文中的 `[3]` 重新标成 `[1]`。新消息只保存最终引用，旧会话读取时按相同规则规范化响应，无需迁移或清空历史。中途停止、异常或未收到完整 `done` 的回答不展示来源。编号校验只确认引用了哪条资料，不替代对答案事实正确性的核验。

### 多轮上下文

- **按需补全检索问题**：例如先问“盗窃罪如何量刑”，再问“那未成年人呢？”，知识库模式会用最近最多 3 个完整轮次、3000 字符补齐主题，再对补全结果做向量检索。原始问题仍用于页面显示、消息存储和最终回答。独立问题、明确换话题和普通对话跳过补全；常见追问由启发式规则识别，并非所有自然语言指代都能识别。
- **澄清后继续原问题**：用户答复文件名、明确选项或简短条件时，继续处理澄清前的主题。文件选择会绑定历史引用的文档身份，重新核验并读取该文件原文，不把文件名当成新问题去检索目录。支持完整名称、省略年份的唯一名称，以及澄清选项中的“第二份、后者”等；同名或版本仍不明确时才再次确认。
- **展开已有依据**：用户只说“具体点、详细一点”时，重新核验已有引用，并在当前限定文件中补充检索未引用过的正文；不同文件的口径分别说明，不强制二选一。只使用本轮实际读取的原文；失效来源不会用其他文档或旧答案中的结论替代。
- **跨请求主题衔接**：解析后的主题随已完成请求保存在既有诊断 JSON 中，后续只读取当前会话的主题字段。刷新页面或只保留最近一轮历史后仍可继续；不需要新增数据库列，也不把历史答案当作事实证据。
- **延迟和回退**：补全复用当前对话模型，默认最多输出 256 token，单次总截止时间为 5 秒且不重试；超时、服务错误或无效 JSON 通常回退原问题，明确的澄清答复则保留原主题并拼接用户补充条件。没有完整历史或模型无法唯一确定指代时，先返回澄清问题，不猜测主题。可用 `RETRIEVAL_REWRITE_ENABLED=false` 关闭模型补全；明确的文件选择、已有引用展开和历史来源校验仍保留。
- **统一输入预算**：系统提示、原始问题、补全提示、当前资料和历史消息合计受 `LLM_CONTEXT_WINDOW_TOKENS - LLM_MAX_OUTPUT_TOKENS - CONTEXT_TOKEN_SAFETY_MARGIN` 约束，默认分别为 32768、2048、512。当前使用 UTF-8 字节数及固定消息开销进行保守估算，不依赖特定模型的 tokenizer，也不代表实际计费 token。窗口必须按模型实际能力配置；Ollama 的 `num_ctx` 同步使用该配置，调大可能增加内存/显存占用。
- **完整轮次裁剪**：两种模式共用 `HISTORY_MAX_TURNS=10` 和 `HISTORY_MAX_CHARS=8000`，一轮优先按已完成消息的 `reply_to_id` 关联识别，旧消息兼容相邻、非空的 user + assistant 配对。优先保留最近完整轮次，舍弃孤立、失败或未回答的消息；最新一轮过长时，问题与回答成对截短，不只留下助手回复。RAG 先为历史预留至多三分之一的剩余额度，再装入资料，剩余空间回收给历史。设置历史轮数或字符上限为 0 可禁用生成阶段的历史消息（追问补全有独立的历史字符上限；轮数为 0 时不读取历史）。原始当前问题不会静默截断，超预算时返回可读错误。
- **历史引用隔离与核验**：历史回复中的来源编号改为独立标记，并明确声明不是本轮证据，代码和链接保持原样。本轮只能引用实际装入提示词的资料。对于“解释刚才第 3 条来源”，从最近完整回答的最终引用中定位，重新读取当前知识库内仍就绪、未取消、索引代次一致的原文，再分配本轮编号；不会用旧片段或无关检索结果替代。来源已删除、被重建、编号不存在或轮次不明确时请求澄清。旧引用缺少 `index_version` 时仅接受第 0 代且原文前缀匹配的资料。引用代次本身保存在既有 JSON 字段；本次会话与分块的表结构迁移见下文。

普通对话可按上文启用后台摘要；知识库问答继续使用完整轮次裁剪与来源核验。调整上述参数后重启 API 及相关 worker；Compose 部署时应用配置写在 `backend/.env`，根目录的环境文件用于编排变量。

回归测试：在 `backend` 目录运行 `python -m unittest discover -s tests -t .`。真实 PostgreSQL/pgvector 用例只使用回环地址、库名以 `_test` 结尾的可清空专用库。设置 `TEST_DATABASE_URL` 和带 `owned-test:`、`disposable-test:` 或 `ci-disposable:` 前缀的 `TEST_DATABASE_OWNER`，再运行 `python -m tests._memory_project_test_gate --prepare-owner` 写入库归属 comment。已有其他 owner 时拒绝覆盖；两项均缺失时跳过，部分配置或归属不符时硬失败。测试会清理专用库的数据。

### 会话文档范围

- 知识库聊天顶部可选择最多 5 份已就绪文档，支持逐项移除、切换或恢复全库；引用卡片提供“仅此文档”快捷入口。选择器可继续加载后续页，不只显示前 20 份。范围调整保留历史消息和输入草稿；普通对话不使用范围。
- 范围随会话保存为 `document_scope` 和递增的 `scope_revision`。刷新、跨请求以及上一轮没有引用时仍保留。新会话的首次请求可传 `document_ids`；已有会话用 `PATCH /api/v1/chat/conversations/{id}/scope` 提交 `{document_ids, expected_revision}`，空数组恢复全库。续聊传 `scope_revision` 检查页面快照；会话详情及 SSE `meta` / `done` 返回当前范围。
- 用户通过名称或明确选项选定已引用文件时，核验来源后将其保存为会话范围；“具体点”本身只补充证据，不重新固定已清除的范围。明确说“换个话题、另一个问题”等时，在该轮成功完成后取消原范围。范围中未包含的历史来源需要先切换或取消范围，不能绕过限制读取。
- 向量与关键词两路都在 SQL 排名、截断前按文档 ID 与索引代次过滤；关键词降级和同结构邻居仍受约束。选定文件或要求展开时，保留已核验原文锚点，同时在相应文件中检索新的正文片段，再去重并套用统一上下文预算。文件名、旧答案和旧片段摘要都不充当新证据。
- 文档删除、取消、非就绪或重新索引后会提示重新选择或清除范围，不自动扩大到全库；生成前和完成写回时均校验。范围修改与生成共用会话行锁，生成期间不能调整；旧页面、并发修改和迟到工作线程不能覆盖较新的范围。幂等重放返回原答案，同时报告会话当前范围，不倒退到原请求的旧范围。
- 升级时 `init_db` 幂等补充两列；老会话默认全库、版本 0，无需清空消息或重新导入文件。请同时更新后端和前端，生产升级前先备份数据库。

### 会话并发与请求去重

- 新客户端每次提交生成一个 UUID `request_id`；网络重试应复用同一个值。相同用户、相同请求 ID 只能认领一次：处理中返回 HTTP 409，完成后重放原来的 `meta` / `delta` / `done`，不再次生成；同键改变问题、参数或目标会话会被拒绝。失败/取消后主动发起新尝试需要新 ID。删除会话会清除消息与请求诊断，仅保留去重身份；迟到的同键请求返回 HTTP 410，不能重新建会话。旧客户端可省略该字段，服务端生成 ID，但无法保证旧客户端跨请求重试的去重。
- 同一会话的生成租约保存在 PostgreSQL，跨 API 进程生效，不在网络等待期间持有数据库连接或行锁。默认 `CHAT_LEASE_SECONDS=300`，后台续期；进程崩溃后到期可被新请求替代，旧请求即使晚到也不能写回。不同会话仍可并行。
- 消息新增明确的 `turn_id` / `reply_to_id` 关联与 `processing` / `completed` / `failed` / `cancelled` 状态，新历史优先按关联配对，旧历史继续兼容相邻轮次。未完成问题不进入生成记忆。
- 「停止」会取消浏览器读取并调用当前用户的 `/chat/requests/{request_id}/cancel`；断连清理与租约到期提供兜底。上游是否及时停止执行、是否继续计费仍取决于服务商。

### 混合检索、重排与原文定位

- `HYBRID_RETRIEVAL_ENABLED=true` 将向量结果与 PostgreSQL 全文关键词结果按排名融合。关键词保留英文型号/错误码、中文二三字片段，并统一中文/阿拉伯条款编号，不依赖新增搜索服务。两路都限定当前知识库、就绪且未取消的文档；关键词读取当前分块，向量与历史来源检查索引代次。某一路失败时尽量使用另一有效路；服务故障且没有可用候选时返回错误，而不是伪装成资料不足。
- `RERANK_ENABLED=false` 默认不额外调用模型。配置 `RERANK_BASE_URL`、`RERANK_MODEL` 和相应密钥后可启用兼容 `/rerank` 的服务，最多处理 40 条有界文本，默认总截止时间 3 秒、不重试。失败或非法结果回退原顺序。关键词/融合/重排分数不会伪装成余弦相似度；仅关键词命中的来源不显示相似度百分比。
- 新入库文档保留 PDF 实际页码、Markdown/中文章节条款、DOCX 原始段落与表格顺序、CSV/Excel 工作表及行位置；分块重复保留章节前缀；Markdown、CSV、Excel 和 DOCX 表格重复保留表头（CSV/Excel/DOCX 以首个非空行为表头），过长前缀有界截短。`RETRIEVAL_NEIGHBOR_CHUNKS=1` 只补同结构相邻片段；预算不足时先退回命中的中心片段，并收窄引用范围，避免把未实际供给模型的邻居计作依据。
- 可提取文字的 PDF 使用 PyMuPDF 的真实表格识别：带格线的表格按行列还原，空单元格保留为空，表头（含位于表格上方、被识别为「外部表头」的情况）只加入一次并在分块前缀重复；表内逻辑行号 `row_start`/`row_end` 以物化后的表格行为准（表头恒为第 1 行、正文从第 2 行起），`page_start`/`page_end` 始终是 PDF 物理页号。正文与表格按页内纵/横坐标合并，同一行或同一文本片段只有部分落在表格内时保留表格外的文字；只有完整识别并复制成功的表才会从正文中排除，单表抽取失败保留该表原文、整体识别失败保留整页原文，因此不会因识别异常丢正文。旋转页（`/Rotate` 非 0）的表格区域与文字坐标不同源，这类页整体按正文保留，宁可重复也不误删。真正空白页跳过且不重编号；含可见图像却没有可提取文字的页（含旋转页与 inline 图像，只按实际展示位置判断，不按资源清单）会让整份文档失败并提示页号与「先 OCR 后重新上传」，混合文档不会只入库文字页就标记就绪。OCR 引擎与模型不在本仓库内提供，也不会自动下载。
- 引用的「查看原文」重新检查知识库访问权、就绪状态与索引代次，显示完整分块及可用位置；PDF 通过带鉴权的接口读取，并定位到实际页码。文本按纯文本显示，不执行文档中的 HTML。文档删除/更新后旧来源会提示失效；旧索引不猜页码，重新索引后获得结构信息。扫描 PDF 仍需预先 OCR。

### 本次升级与验证

1. 先备份，停止旧 API / worker，再统一部署 API、worker、web，避免旧代码绕过新租约或写入旧分块格式。
2. `init_db` 启动时幂等创建 `chat_turns`，补充会话租约、消息关联/状态、分块 `source_metadata` / `search_text` 及去重索引。保留历史数据，旧消息关联为空时走兼容逻辑；无需清空业务库。旧分块默认没有结构位置和关键词文本，不自动从分块序号推测页码；按需重新索引后补齐。会话删除后的请求去重记录保留，但含问题与来源的诊断被清除。本次有表结构迁移，不能只更新前端。
3. 启动新版 API 完成迁移后，可在 `backend` 执行 `python -m scripts.migrate_keyword_index --dry-run` 检查，再运行 `python -m scripts.migrate_keyword_index` 并发建立关键词 GIN 索引；脚本不改源文档或分块正文。旧就绪文档仍能向量检索，按需重新索引以补齐关键词及结构信息；重新索引期间该文档暂不参与检索。
4. 在 `backend` 运行 `python -m unittest discover -s tests -t .`，在 `frontend` 运行 `npm test` 和 `npm run build`。真实数据库回归需先按上文核验专用库 URL 与 owner；测试会清理该专用库的数据。

### 真实浏览器与维护回归

浏览器用例使用真实 API、PostgreSQL、四个独立角色 worker（`ingest`/`summary`/`project`/`regression`）与前端生产构建，只有模型由回环确定性替身提供。先安装锁定依赖、Playwright Chromium，并显式准备 `pgvector/pgvector:pg18` 测试镜像；harness 不会自动拉取镜像。在独立终端运行：

```bash
python -I backend/scripts/e2e_env.py up --out /tmp/ragkb-browser-run --owner local-browser --keep
```

输出目录必须在仓库外且不存在或为空。等待目录出现 `READY` 后，在另一终端运行（Windows 换为本机绝对临时路径）：

```bash
export RAG_E2E_MANIFEST=/tmp/ragkb-browser-run/manifest.json
npm --prefix frontend run typecheck:e2e
npm --prefix frontend run test:e2e
python -I backend/scripts/e2e_env.py down --out /tmp/ragkb-browser-run
```

每轮测试独占数据库、端口、合成项目与上传目录；API 和模型替身必须通过 owner/nonce 校验。`down` 只清理已核实归属的进程和容器，`--keep` 保留诊断目录。用例覆盖项目登记/同步/搜索、固定快照与并发切换、旧引用预览、摘要发布/重置、断流恢复锁、合并阶段取消上传后的队列推进，以及 `frontend/e2e/answer-feedback.spec.ts` 的答案反馈流程。替身输出只验证系统链路，不代表真实模型质量。

CI 分别运行后端、前端、浏览器和维护检查。维护回归包括 `deploy/tests/test-maintenance.sh`、`test-remote-backup.sh`、`test-drill-guard.sh`、`test-monitoring.sh` 和 `test-alerting.sh`；前四者使用合成数据、命令替身或独占监控容器，告警检查使用合成指标驱动真实 Prometheus → Alertmanager → 本地 webhook fixture。它们验证权限、规则、启动/关闭热重载及合成 firing/resolved 投递，不恢复业务数据、不向真实接收器发送通知，也不代表生产验收。

固定问题集、离线回放、显式真实服务评测、模型连通性检查和费用估算边界见 [评测说明](backend/evaluation/README.md)。`include_trace=true` 仅管理员可用；普通问答不公开检索诊断。短追问补全与主问答一样通过 SSE 请求云端，兼容返回完整 JSON 的服务，避免某些网关在非流式模式只返回用量而丢失正文。

## 用量与费用

入口：登录后侧栏「用量与费用」。页面上方显示调用、实际输入/输出 Token 和人民币预估费用，下方显示 Token 与费用时间趋势，并提供模型/调用类型明细。

- **权限与区间**：管理员默认查看全站，可切换「仅本人」并管理模型单价；普通用户只能查看本人，接口同样限制。日期按 `Asia/Shanghai` 自然日计算，起止日均包含；支持快捷和自定义区间，最长 366 天，小时粒度最多 31 天，另有天/月粒度。刷新可查看新发生的用量。
- **计量对象**：一次模型调用尝试计一次，不等于用户提问次数。包含回答生成、追问改写、检索向量化、文档入库向量化、可选重排、会话摘要压缩与项目源码/文档向量化；向量批次和自动重试分别计数。请求重放不重新调用模型，也不新增记录。摘要归属触发压缩的会话（提问者），项目向量化归属发起同步的管理员；文档向量化归属上传者，检索/改写/生成/重排归属提问者。
- **实际 Token**：仅采纳上游返回的有效用量，兼容 OpenAI / DeepSeek / LangChain 常见字段。输出 Token 已包含的思考 Token 不再重复相加，缓存输入是总输入的子集。上下文预算的字节估算不作为用量。部分或全部缺失会显示覆盖不足/未知；Ollama 向量接口仅返回向量、没有 Token 数时同样保持未知。
- **费用估算**：管理员按服务商、模型、类型配置人民币/百万 Token 的输入、输出和可选缓存输入单价，最多 6 位小数。输入/输出同时留空为未配置，明确免费需填 `0`；向量/重排输出单价为 `0`。缓存价留空按普通输入价处理；配置不同缓存价而上游未报告缓存量时，费用保持未知，不猜命中率。
- **价格快照**：每次调用在开始时保存价格版本，历史金额不会随改价变化。并发改价使用版本校验，过期页面返回 409 并需刷新。汇总费用仅合计可计价调用，并同时报告计价覆盖率；完全未知用 `null` 表示，不显示成零元，趋势中的未知费用也不补零连接。
- **失败与停止**：失败、取消或断流仍可能发生消耗，已收到的实际用量会保留；未收到则未知。开始记录和最终写回分开持久化，进程异常退出时保留尚未完成的调用。上游在停止后继续执行或遗漏的用量无法从应用侧推算。
- **历史与保留**：升级自动创建独立计量、单价和跟踪起始表，并只补录旧诊断中明确保存的已完成生成用量。历史模型以 `historical-unknown` 标记，费用不倒算；旧改写/向量化/重排也不猜。删除会话或文档不会删除已发生用量；删除用户/知识库会清除计量记录中的相应关联，但管理员总量仍保留。计量表不保存问题、答案、文档正文或 API Key。

云端默认发送 `LLM_STREAM_INCLUDE_USAGE=true`，以获取流结束后的实际用量；已有配置显式为 `false` 时不会自动覆盖。兼容网关不支持 `stream_options` 时可关闭，但实际用量可能缺失。此统计仅覆盖应用内已记录调用，是配置价格下的**估算**，不包含供应商折扣、套餐、税费或应用外调用，不能代替供应商账单；价格未核实时保持未配置比填假价格更准确。

接口：`GET /api/v1/usage/statistics` 支持 `start`、`end`、`granularity`、`scope`、`model`、`operation`；`GET/PUT /api/v1/usage/prices` 仅管理员可用。金额和单价以十进制字符串返回，缺失金额为 `null`。生产升级说明见 [DEPLOY.md](DEPLOY.md#用量与费用升级)。

## 批量导入用户

入口：「用户管理」页面右上角「批量导入」（仅管理员可见）。

### 表格格式

第一行是表头，之后每行一个用户。表头支持中英文，列顺序随意，多余列会被忽略：

| 列 | 必填 | 可用的表头写法 |
|---|---|---|
| 用户名 | ✅ | `用户名` `账号` `帐号` `登录名` `username` `account` |
| 姓名 | | `姓名` `显示名称` `真实姓名` `display_name` `name` |
| 邮箱 | | `邮箱` `电子邮件` `email` `mail` |
| 角色 | | `角色` `权限` `role`；值可写 `普通用户` / `管理员`（也认 `user` / `admin`），留空为普通用户 |

用户名规则与「新建用户」完全一致：2–64 位，只能是字母、数字、下划线、点、横线，
**区分大小写**（登录时用 `ZhangSan` 能对上，`zhangsan` 对不上）。

支持 `.xlsx` 与 `.csv`。CSV 的 UTF-8（含/不含 BOM）、GBK 都能识别，
逗号 / 分号 / 制表符分隔都能读。旧版 `.xls` 不支持——在 Excel 里另存为 `.xlsx` 即可。
单次最多 500 行、文件不超过 5MB。

### 密码与回执

每人生成一个 14 位随机初始密码（去掉了 `0O1lI` 这类易混字符，保证大小写字母与数字
都有），**密码不存明文**——库里只有 bcrypt 哈希，所以：

> 回执只在导入完成的那一刻提供一次，关闭窗口后无法再次导出。
> 请当场下载，并提醒本人登录后立即修改。

回执是一个 xlsx，三张表：`导入成功`（含初始密码）、`导入失败`（含表格行号与原因）、
`说明`（源文件、时间、成功/失败数）。行号是**用户在 Excel 里看到的真实行号**，
空行不会让它前移。

### 部分成功是正常的

导入是「尽力而为」：某一行不合格不影响其它行。常见失败原因与处理：

| 失败原因 | 处理 |
|---|---|
| `用户名已存在，已跳过` | 不会覆盖已有账号；要改这些人的资料请单独编辑 |
| `与第 N 行的用户名重复` | 表内重复，留一行即可 |
| `用户名只能包含字母、数字、下划线、点、横线` | 去掉空格、中文、`@` 等字符 |
| `用户名长度需在 2-64 之间` | 单字符用户名建不了 |
| `角色「X」无法识别` | 改成 `普通用户` / `管理员`，或留空 |

表内重复的判定在写库之前完成，落库又是逐行 SAVEPOINT，
所以两个人同时导入同一批用户名也不会互相拖垮——只有撞车的那一行失败。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/v1/users/import` | `multipart/form-data`，字段名 `file`；返回逐行结果 + 回执 base64 |
| `GET` | `/api/v1/users/import/template` | 下载模板（表头 + 角色下拉 + 填写说明，无示例数据） |

导入会写一条 `user_import` 审计，记录总行数、成功数、失败数与角色分布——
**不记录密码**。

## 模型服务商切换

`EMBEDDING_PROVIDER` 与 `LLM_PROVIDER` 各自独立，可组合成四种方式。切换只需改 `.env`
并重启后端与 worker，代码里没有硬编码的服务商。

云端模式下**向量与对话的服务地址、Key、超时、模型都是分开配置的**，所以可以像当前默认
配置这样：向量用硅基流动（免费、稳定），对话用 sheapi.top 的 `deepseek-v4.1-flash`。
想换任意其他 OpenAI 兼容服务，改 `*_BASE_URL` / `*_API_KEY` / `*_MODEL` 三项即可。
报错信息里会带上出问题的域名（如 `www.sheapi.top 鉴权失败（401）`），便于定位是哪一侧。

**向量切换不影响已有数据**：硅基流动的 `BAAI/bge-m3` 与 Ollama 的 `bge-m3:latest`
是同一份权重。实测同一段文本在两侧的余弦相似度为 **0.9992~0.9999**（不同文本之间只有
0.53~0.61），属于同一向量空间，因此**历史向量无需重建**，检索结果保持可比。
注意此结论只对 bge-m3 成立；换成别的向量模型（维度或权重不同）必须重建索引。

云端调用使用 `httpx` 直接实现 OpenAI 兼容协议（`app/services/cloud_models.py`），
没有引入 `openai` / `langchain-openai` 依赖。失败策略：429 与 5xx 自动退避重试 3 次，
401/402/422 直接抛出带中文说明的错误。连接按 base_url 复用，批量向量化时省掉反复握手。

## 普通对话摘要压缩（记忆）

长对话默认只要塞得进上下文就不压缩，仅在显式开启后才对旧轮次做结构化摘要，让空出的
预算回到最新的几轮。**这是有损压缩**：被压缩的旧轮次不再作为完整上下文参与回答，
只保留生成的结构化记忆。开启方式与行为如下：

- **开关**：`CHAT_MEMORY_ENABLED=true` 后重启后端与 worker。默认 `false`，即完全回到
  现有窗口裁剪（过长的旧轮次整体丢弃），与升级前行为一致。
- **触发**：历史轮次达到阈值才压缩。触发的两个条件——轮数
  （`MEMORY_TRIGGER_TURNS`，默认 8 轮）与历史 token 占比
  （`MEMORY_TRIGGER_RATIO`，默认 0.75）——**任一满足即触发**；触发后
  `MEMORY_HISTORY_TOKEN_BUDGET`（默认 8000）控制参与语义折叠的历史预算。
- **保留**：最近 `MEMORY_KEEP_RECENT_TURNS`（默认 4 轮）不参与折叠、保持原样，但仍受整体
  硬输入预算约束——预算不足时仍会被裁剪，不保证无条件完整；摘要文本上限为
  `MEMORY_SUMMARY_MAX_TOKENS`（默认 1200）。
- **状态与重置**：只有后台摘要成功发布后，会话状态的已覆盖轮次序号才会前移，界面据此显示
  「已覆盖至第 N 轮 / 仍有历史待整理 / 已就绪可查看」等状态；可查看结构化摘要与前缀来源
  范围，并可对单条会话发起重置（带版本号，过期返回 409 需刷新，绝不覆盖较新的状态）。
- **异步与代次**：压缩由独立 worker 异步领取（见下），前端轮询该会话的状态；切换会话
  或长任务结束都会重新拉取，避免迟到响应覆盖到别的会话。
- **失败重试**：压缩失败会保留调用与重试上限（`MEMORY_MAX_ATTEMPTS`），并暴露可查看的
  失败原因；仍保留原窗口裁剪作为兜底，不会因摘要失败丢失提问入口。

## 项目源码管理（独立内容检索 / 项目问答）

在普通文档知识库之外，系统还支持把**本地项目源码/文档**作为一个独立内容源检索，并可在
对话中把项目纳入 RAG 用项目的向量索引作答。负责处理项目索引的后端 worker 与文档入库
worker 相互独立，可单独启动。

- **根目录绑定**：管理员通过 `PROJECT_ALLOWED_ROOTS` 绑定若干「根名 → 磁盘路径」，
  只允许在这些根目录下登记项目，杜绝任意路径访问。
- **存储与限额**：登记的项目清单与元数据存放在 `PROJECT_STORAGE_DIR`（默认
  `storage/projects`）。单个项目文件数上限 `PROJECT_MAX_FILES`、单文件字节上限
  `PROJECT_MAX_FILE_BYTES`、总字节上限 `PROJECT_MAX_TOTAL_BYTES`；项目索引向量使用独立
  前缀（`PROJECT_VECTOR_PREFIX`，默认 `proj_`），与普通文档向量互不干扰。
- **忽略与清理**：项目内跳过二进制、`.git`、构建产物等目录，遵循项目的忽略规则，避免把
  生成物与密钥索引进去。
- **解析与同步**：支持常见源码与文档格式，`PROJECT_PARSE_BATCH` 控制批次；同步为手动
  触发、后台增量执行，增量只处理变更。项目页面可查看同步进度/状态并触发展开或全量重建。
- **可选自动同步**：管理员可在项目源上开启定时检查（默认关闭，间隔 60 秒–24 小时）。
  到期源由 project worker 原子领取；内容与当前活动快照一致时只记录检查、不发布新版本；
  扫描为空或失败时保留旧版本继续可检索。纳入索引的文件在最近
  `PROJECT_AUTO_SYNC_QUIET_SECONDS`（默认 10 秒，0 表示关闭）内仍被写入时，本次自动同步
  延后重试，避免编辑器分步保存的半成品被立即发布；手动同步不受安静窗口影响。
  自动发布成功后页面提示「新版本已可检索」，
  由用户显式切换；已有会话继续固定原快照，新会话使用新版本。
- **访问控制**：登记的项目带可访问者范围；用户在对话中选择项目（`kind='project'` 的知识
  库）时，只会检索并引用其有权访问的项目内容，来源预览会路由到项目内文件与行号/符号。

## 注意事项

- 默认对话模型为 sheapi.top 的 `deepseek-v4.1-flash`（首字约 5s，不输出思考过程）；
  本地模式下的 `qwen3.5:0.8b` 参数较小，复杂推理能力有限，生产建议换 7B 以上模型。
- 云端模式需要后端能访问 `api.siliconflow.cn` 与 `www.sheapi.top`；内网部署请把两个
  `*_PROVIDER` 改回 `ollama`。
- 扫描版 PDF 无法提取文字，需先 OCR。整份只有图片、或文字页与扫描页混合时，入库会明确失败并列出需要 OCR 的物理页号（不列出文件路径或原文），修好后重新上传即可；系统不会自动安装或调用 OCR 引擎。
- 文档入库由独立 worker 进程从数据库领取（`start-worker.bat`），支持断点续传与多开；
  worker 未运行时文档停在 `pending`，`/health` 会显示 `worker: stale`。
  `/health` 的 `worker` 有三种取值：`alive`（正在处理且有新鲜心跳）、
  `stale`（有排队文档但无人领取，通常是 worker 没启动）、`idle`（队列为空，无法观测）。
- worker 支持 `--role` 参数四个角色：`ingest`（文档入库，默认，即 `start-worker.bat` 的行为）、
  `summary`（普通对话摘要压缩）、`project`（项目源码/文档索引与手动/自动同步）、
  `regression`（反馈回归的后台重跑，消费管理员显式提交的在线作业）。
  **四个角色相互独立，需各自单独启动**，没有单一脚本能一次拉起全部：
  ```bash
  python worker.py                              # 入库（默认）
  python worker.py --role summary               # 对话摘要
  python worker.py --role project               # 项目索引
  python worker.py --role regression            # 反馈回归后台重跑
  ```
  本地也可用 `start-worker.bat [ingest|summary|project|regression]` 启动单个角色；
  不带参数仍是入库。只跑 `ingest` 时，摘要与会话记忆保持在已触发/排队状态、项目同步与
  在线回归作业停在待处理状态——文档/摘要/项目/回归的领取循环互不占用，长任务不会互相饿死。
  在线回归没有默认自动入队；每次后台重跑都会真实调用模型并产生用量记录，机器断言与
  人工核验是两件事。
  除 `/health` 的队列推断外，管理员还可用 `/api/v1/operations/workers`（或「运维」页）看到四个角色
  自己的实例心跳与任务队列；Prometheus 采集会输出固定 `role` 标签的 `ragkb_worker_enabled`、
  `ragkb_worker_alive`、`ragkb_worker_{pending,processing,stale}_tasks` 等指标，并对“已启用但连续
  5 分钟没有存活实例”的角色告警。采集失败时这些指标整体缺失（`ragkb_workers_available=0`），
  不会显示为 0 个实例。
- 本地模式下首次检索会加载 bge-m3 到内存，响应较慢，之后正常；云端模式无此问题。

## 生产部署

- **从零开始**（买服务器 → 配安全组 → 装 Docker → 上线验收）：
  [DEPLOY-FROM-SCRATCH.md](DEPLOY-FROM-SCRATCH.md)
- **已有服务器、只想更新代码**：[DEPLOY.md](DEPLOY.md)
- **部署验收（只读检查 + 人工证据）**：[deploy/acceptance/README.md](deploy/acceptance/README.md)

一条命令拉起全部组件：

```bash
cp .env.example .env && cp backend/.env.example backend/.env   # 填密钥
./deploy/init.sh
docker compose up -d --build
```

编排默认启动 4 个服务：`db`（pgvector）、`api`（uvicorn）、`worker`（入库）、
`web`（Nginx 静态托管 + 反代）；另有 3 个按 profile 启用的角色服务：
`worker-summary`（`--profile summary`）、`worker-project`（`--profile project`）与
`worker-regression`（`--profile regression`，反馈回归后台重跑）。
`api`、`worker` 和三个 profile 角色 worker 是**同一个镜像**（`rag-kb-backend`，只差启动命令），
构建一次、多个容器共用；`web` 单独构建，不会出现后端版本漂移。
数据统一落在 `${DATA_DIR}`，附备份/恢复脚本，适配可能被回收的竞价实例。

部署验收使用只读 CLI，默认 dry-run（零网络），`--live` 才做有限只读 GET；令牌只从环境变量读取：

```bash
python backend/scripts/verify_deployment.py --base-url https://rag.example.com --json

export RAG_DEPLOY_ADMIN_TOKEN='<在终端中设置>'
export RAG_DEPLOY_METRICS_TOKEN='<在终端中设置>'
python backend/scripts/verify_deployment.py \
  --base-url https://rag.example.com \
  --confirm-target https://rag.example.com \
  --live --out /srv/ragkb-acceptance/verification.json
```

工具通过不等于业务/生产验收通过；缺失真实证据的项保持 `unknown`，详见
[部署验收指南](deploy/acceptance/README.md)。

备份产出同一 stamp 的 `ragdb`、`uploads`、`projects` 三份归档和最后发布的 `manifest-*.sha256` 清单；
恢复按清单校验后覆盖，旧的两件套备份仍兼容（不改动现有 `projects/`）。日常维护入口见
[DEPLOY.md](DEPLOY.md#数据与备份)，恢复能力用 `deploy/tests/restore-drill.sh` 在隔离环境演练。
可选的 HTTPS 用 `docker-compose.https.yml` 覆盖启用，见 [DEPLOY.md](DEPLOY.md#https)。

评测工具支持完整本地语料、公开检索配置和可选索引清单的身份校验，旧身份基线会明确判为不可比；业务问题集可用 `--require-reviewed` 检查人工核验状态，并输出分层样本数与固定断言结果。用法见 [评测说明](backend/evaluation/README.md) 与 [业务标注模板](backend/evaluation/templates/README.md)。

审计写入失败通过脱敏日志、无标签指标和管理员摘要可见；可选的 Prometheus → Alertmanager → 通用 webhook 通知链路及启停步骤见 [监控与告警说明](deploy/monitoring/README.md)。worker 角色心跳同样导出为固定 `role` 标签的指标，规则见 `deploy/monitoring/prometheus/rules/ragkb-worker.yml.in`。上线仍需准备真实接收器和经人工核验的业务样本。
