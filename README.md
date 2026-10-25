# 企业知识库问答系统（RAG）

基于 **FastAPI + LangChain + pgvector** 的企业级检索增强生成（RAG）知识库问答平台，
前端使用 **Vue 3 + Pinia + Vite + Element Plus**。模型服务商可在**硅基流动云端**
与 **Ollama 本地**之间自由切换（向量与对话可分别配置）。

> 最新的安全修复、数据库兼容升级、测试与备份维护说明见 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端框架 | FastAPI + Uvicorn |
| 编排框架 | LangChain |
| 向量数据库 | PostgreSQL 18 + pgvector 0.8 |
| 向量模型 | bge-m3（1024 维）— 硅基流动云端 / Ollama 本地 |
| 对话模型 | deepseek-v4.1-flash（sheapi.top） / qwen3.5:0.8b（Ollama 本地） |
| 认证 | JWT（HS256）+ bcrypt 密码哈希 |
| 前端 | Vue 3 + TypeScript + Pinia + Vite + Element Plus |

## 功能

- **登录鉴权**：JWT 令牌、角色权限（管理员 / 普通用户）、路由守卫
- **知识库管理**：多知识库隔离，每个知识库独立向量集合
- **文档管理**：支持 PDF / Word / TXT / Markdown / Excel / CSV 上传，后台异步解析入库
- **智能问答**：SSE 流式输出、Markdown 渲染、引用溯源（显示来源文档与原文片段）
- **双模式对话**：同一页面可切换「知识库问答」（检索 + 引用）与「普通对话」（不检索、直接生成），
  模式按会话固定，两种会话历史分开；知识库为空时自动进入普通对话
- **会话管理**：多轮对话上下文、历史会话切换与删除
- **用户管理**：管理员可创建/编辑/禁用/删除用户、重置密码
- **批量导入用户**：上传 Excel / CSV 批量建号，每人生成随机初始密码；
  逐行校验、逐行报告失败原因，导入完成即可下载含初始密码的回执（xlsx），
  并提供带角色下拉的导入模板
- **审计日志**：记录登录、知识库与文档变更、问答等操作

## 目录结构

```
enterprise-rag-kb/
├── backend/
│   ├── app/
│   │   ├── api/v1/        # 路由：auth / users / knowledge / chat / audit
│   │   ├── core/          # 配置、安全（JWT、bcrypt）
│   │   ├── db/            # 会话、建表初始化
│   │   ├── models/        # SQLAlchemy 模型
│   │   ├── schemas/       # Pydantic 校验模型
│   │   ├── services/      # 解析、切分、向量化、检索、生成、用户导入
│   │   └── main.py
│   ├── scripts/           # 运维脚本（向量索引迁移）
│   ├── storage/uploads/   # 上传文件存放（开发环境）
│   ├── requirements.txt   # 开发依赖（宽松下限）
│   ├── requirements.lock  # 生产依赖锁定，供镜像构建
│   ├── Dockerfile
│   ├── worker.py          # 独立入库 worker
│   └── run.py
├── frontend/
│   ├── src/
│   │   ├── api/           # axios 封装 + SSE 客户端
│   │   ├── layouts/       # 主布局
│   │   ├── router/        # 路由 + 权限守卫
│   │   ├── stores/        # Pinia
│   │   └── views/         # 页面
│   ├── Dockerfile
│   └── nginx.conf         # 生产 Nginx 配置
├── deploy/
│   ├── init.sh            # 首次部署检查
│   ├── backup.sh          # 备份（数据库 + 上传文件）
│   └── restore.sh         # 恢复
├── docker-compose.yml     # db / api / worker / web 编排
├── DEPLOY.md              # 生产部署手册
├── start-backend.bat
└── start-frontend.bat
```

## 前置条件

1. **Docker**：pgvector 容器运行中（本例映射到宿主 `9999` 端口）
2. **模型服务**（云端 / 本地，可按需混用）：
   - **云端**（默认）：向量走硅基流动、对话走 sheapi.top，两处 API Key 分别填到 `.env` 的
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
| `LLM_BASE_URL` | 对话服务地址（OpenAI 兼容） | `https://www.sheapi.top/v1` |
| `LLM_API_KEY` | 对话服务 Key，**不要提交** | 空 |
| `LLM_TIMEOUT` | 对话请求超时（秒） | 180 |
| `OLLAMA_BASE_URL` | Ollama 服务地址 | `http://localhost:11434` |
| `EMBEDDING_MODEL` | 向量模型 | `BAAI/bge-m3` |
| `LLM_MODEL` | 对话模型 | `deepseek-v4.1-flash` |
| `CHUNK_SIZE` / `CHUNK_OVERLAP` | 文本切分粒度 | 800 / 120 |
| `RETRIEVAL_TOP_K` | 默认检索条数 | 6 |
| `RETRIEVAL_MAX_PER_DOC` | 单份文档最多占几条检索结果 | 3 |
| `MAX_UPLOAD_MB` | 单文件大小上限 | 50 |

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
  → bge-m3 向量化问题（每次查询只算一次向量，候选放大时复用）
  → pgvector 余弦相似度检索 Top-K
  → 按文档封顶（单份长文档最多占 RETRIEVAL_MAX_PER_DOC 条，
    避免条款类文档整片占满结果、挤掉其他来源）
  → 组装「参考资料 + 历史对话 + 问题」提示词
  → 对话模型流式生成（SSE 推送）
  → 保存消息、引用来源、审计日志

用户提问（普通对话模式）
  → 跳过向量化与检索，直接用通用提示词组装「历史对话 + 问题」
  → 对话模型流式生成（SSE 推送）
  → 保存消息（引用为 NULL）、审计日志
```

**历史对话的裁剪规则**（两种模式共用）：取最近 `HISTORY_MAX_TURNS` 轮（默认 10 轮，
user + assistant 各算一条），并受 `HISTORY_MAX_CHARS` 字符预算（默认 8000）约束。
超预算时从最新往回保留、丢掉最早的；截断后若首条是助手消息则一并去掉，
避免模型以自己上一轮的答案开头而顺着复述。

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

## 注意事项

- 默认对话模型为 sheapi.top 的 `deepseek-v4.1-flash`（首字约 5s，不输出思考过程）；
  本地模式下的 `qwen3.5:0.8b` 参数较小，复杂推理能力有限，生产建议换 7B 以上模型。
- 云端模式需要后端能访问 `api.siliconflow.cn` 与 `www.sheapi.top`；内网部署请把两个
  `*_PROVIDER` 改回 `ollama`。
- 扫描版 PDF 无法提取文字，需先 OCR。
- 文档入库由独立 worker 进程从数据库领取（`start-worker.bat`），支持断点续传与多开；
  worker 未运行时文档停在 `pending`，`/health` 会显示 `worker: stale`。
  `/health` 的 `worker` 有三种取值：`alive`（正在处理且有新鲜心跳）、
  `stale`（有排队文档但无人领取，通常是 worker 没启动）、`idle`（队列为空，无法观测）。
- 本地模式下首次检索会加载 bge-m3 到内存，响应较慢，之后正常；云端模式无此问题。

## 生产部署

- **从零开始**（买服务器 → 配安全组 → 装 Docker → 上线验收）：
  [DEPLOY-FROM-SCRATCH.md](DEPLOY-FROM-SCRATCH.md)
- **已有服务器、只想更新代码**：[DEPLOY.md](DEPLOY.md)

一条命令拉起全部组件：

```bash
cp .env.example .env && cp backend/.env.example backend/.env   # 填密钥
./deploy/init.sh
docker compose up -d --build
```

编排包含 4 个服务：`db`（pgvector）、`api`（uvicorn）、`worker`（入库）、
`web`（Nginx 静态托管 + 反代）。`api` 与 `worker` 是**同一个镜像**
（`rag-kb-backend`，只差启动命令），构建一次、两个容器共用，不会出现版本漂移。
数据统一落在 `${DATA_DIR}`，附备份/恢复脚本，适配可能被回收的竞价实例。
