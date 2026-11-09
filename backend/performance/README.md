# RAG 有界性能 / 费用测量工具

`scripts/benchmark_rag.py` 是一个**独立、默认 dry-run、所有上限硬编码**的小样本测量工具。
它复用现有登录、问答流、知识库上传和精确用量接口。只有 `operations/summary` 与
`usage/measurement` 读接口是只读的；**live 聊天本身会通过现有 API 创建并持久化会话、
消息和计量记录，显式允许的上传也会创建文档**，工具不会自动删除或回滚这些数据，也不会
登录、自动创建/切换/删除知识库，或读取 `.env`。

> 它测量的是当前实例的本地计量快照与端到端延迟，**不是企业容量验收，不是业务验收，
> 也不是供应商账单**。报告中的 `acceptance` / `capacity_acceptance` 恒为 `false`。

## 构成

| 路径 | 作用 |
| --- | --- |
| `backend/app/services/capacity_benchmark.py` | 纯本地严格配置解析、URL/文件/输出路径校验、SSE 解析、百分位 |
| `backend/scripts/benchmark_rag.py` | dry-run / live CLI、有界并发、上传等待、费用门禁、报告 |
| `backend/app/api/v1/usage.py` | `POST /api/v1/usage/measurement`，仅管理员、只读 |
| `backend/app/services/usage_measurement.py` | 按 `(user_id, request_id)` / `document_id` 精确聚合实际尝试 |
| `backend/app/schemas/usage.py` | 测量请求/响应模型，严格拒绝未知字段与空选择 |
| `backend/performance/benchmark.example.json` | 可直接 dry-run 的 chat 模式示例 |

## 快速开始

```bash
cd backend

# 1. 默认 dry-run：只读配置和文件，零 HTTP，不需要真实 token。
./.venv/Scripts/python.exe -X utf8 scripts/benchmark_rag.py \
  --config performance/benchmark.example.json

# 2. 第一次 live 前，从 dry-run 报告读取 target_fingerprint，并显式确认目标与费用。
#    只允许一个规范化目标；不跟随重定向、不重试、不继承 HTTP(S)_PROXY。
./.venv/Scripts/python.exe -X utf8 scripts/benchmark_rag.py \
  --config performance/benchmark.example.json --live \
  --confirm-target 'sha256:<dry-run 输出的指纹>' \
  --accept-model-cost

# 3. 若配置包含 documents，还必须显式允许上传（会产生入库/向量化费用）。
./.venv/Scripts/python.exe -X utf8 scripts/benchmark_rag.py \
  --config performance/benchmark.rag.json --live \
  --confirm-target 'sha256:...' --accept-model-cost --allow-upload
```

输出始终打印到 stdout；`--out report.json` 额外写文件。默认**独占创建**，已存在即失败
（临时文件 + `os.link` 原子发布，不会先删除既有目标）；只有显式 `--overwrite` 才做原子替换
（临时文件 + `os.replace`）。写文件前会先做本地预检：拒绝 Windows 非法字符/保留设备名
（如 `CON`、`LPT1`）、只读目标（含 `--overwrite`）和不可写父目录；预检失败退出码 `2`，
HTTP 请求数为 `0`。预检只用本次自建的临时文件探测父目录，不触碰既有输出。运行结束后才
发现输出冲突（例如并发出现同名文件）属于运行期失败，退出码 `1` 且保留真实的已发送请求数。
配置、文档、硬链接/符号链接别名都不能作为输出，输入文件不会被截断或覆盖。

## 配置格式（version 1）

```json
{
  "version": 1,
  "api_base_url": "https://rag.example.com",
  "mode": "chat",
  "auth": {
    "user_token_env": "RAG_BENCH_USER_TOKEN",
    "admin_token_env": "RAG_BENCH_ADMIN_TOKEN"
  },
  "stages": [
    {"concurrency": 1, "requests": 2},
    {"concurrency": 2, "requests": 4}
  ],
  "total_requests": 6,
  "request_timeout_seconds": 20,
  "run_timeout_seconds": 90,
  "ready_timeout_seconds": 30,
  "planned_scale": {"documents": 6, "users": 2},
  "question": {"code": "baseline", "text": "请用一句话说明这份材料的核心结论。"},
  "cost": {"currency": "CNY", "max_cny": "0.50"},
  "documents": [],
  "limits": {
    "max_http_requests": 120,
    "max_upload_files": 5,
    "max_upload_bytes": 5242880,
    "max_stream_bytes": 4194304,
    "max_sse_events": 5000
  }
}
```

- `api_base_url`：单个目标。只接受正常证书校验的 HTTPS；明文 HTTP 仅允许回环
  （`localhost` / `127.0.0.1` / `::1`）。拒绝 URL userinfo、查询串、片段、非 HTTP(S)、
  非法端口和非法主机名。目标指纹是规范化 URL 的 SHA-256。
- `mode`：`chat` 或 `rag`。`rag` 必须提供 `kb_id`，且该知识库必须是已存在的 `upload`
  类型；工具不会创建/切换/删除知识库。
- `auth`：只从这两个环境变量名读取 token。不读取配置文件中的明文 token；空值、控制字符、
  超长值和 multipart 边界冲突值会被拒绝。
- `stages`：并发阶梯。并发值必须非递减；`sum(stage.requests) == total_requests`。
- `planned_scale`：只声明计划规模，不会真的创建这么多文档或用户；`users` 只是声明值，
  当前凭证只有一个 `user_token_env`（单个业务用户 token），不代表已测试多个业务用户。
  dry-run 只把它写进计划。
- `question`：问题正文只在 live 时发给被测 API，从不进入报告；报告只允许问题代码。
- `cost.max_cny`：`CNY`、非负、最多 6 位小数、硬上限 1,000,000。`0` 会在任何 HTTP 前停止。
- `documents`：仅 `rag` 且配置 `kb_id` 时允许；每项 `{path, code}`。文件必须是常规、非空、
  非符号链接，后缀格式合法且明确拒绝可执行/脚本/安装包后缀；代码与解析后路径不得重复；
  总数量/字节受限。
- `limits`：只能收紧，不能越过代码里的硬上限。HTTP 调用（含身份、运维快照、
  上传、轮询、费用检查）共享 `max_http_requests`。

严格 JSON 解析会拒绝重复键、`NaN`/`Infinity`、指数溢出、深层嵌套、超长字符串、控制字符、
孤立代理项、布尔冒充整数和超大文件；配置上限 256 KiB。

## 硬上限

单次运行最多 8 并发、40 个问答请求、10 个阶梯、每阶梯 20 个请求、300 次 HTTP、
5 个上传文件、5 MiB 上传总量、4 MiB/响应流、5000 个 SSE 事件、1 MiB/SSE 行。
单请求超时 ≤ 30 秒，整次运行 ≤ 120 秒，文档就绪等待 ≤ 60 秒。所有上限是代码常量，
配置不能放宽。

## 安全与费用语义

- 默认 dry-run：仅本地配置/文件/计划校验，`actual_network_requests` 恒为 `0`，
  `measured=false`；不构造可发请求的客户端。
- live 前校验全部配置、目标、凭证变量和输出路径；任何一步失败都在 HTTP 之前退出。
- 只接受一个规范化目标；`follow_redirects=False`、`trust_env=False`、无自动重试；
  3xx、429、HTTP 错误、缺失/畸形 `done`、流错误、截断、超大流、慢响应头、无限心跳
  都会失败或超时，不能伪装为成功。
- 共享 `asyncio` 单调时钟总期限覆盖等信号量、响应头、流读取、上传、就绪轮询和费用轮询；
  客户端读超时不是总期限。超时/取消会取消任务并关闭响应与客户端，不遗留后台请求。
- SSE 的 TTFT 只取第一个非空 `delta.content`；`meta`/`status` 等状态事件不会计入。
  只有合法 `done`（含 `truncated` 与 `finish_reason`）才算成功；`finish_reason=length`
  或 `truncated=true` 记为截断失败。收到合法 `done` 后只检查**已经收到**的缓冲尾部
  （有界解析），不会为等待未来的空行、心跳或 EOF 而继续读取；尾部若还有错误事件、
  重复 `done` 或其他事件则整次请求失败。
- `actual_network_requests` 只统计**真实开始发送**的请求；预算预留数不等于实际网络请求数。
  `sent=false` 的请求（例如发送前已到期限）会进入 `requests_not_sent`/`not_sent`，不计入
  成功率、延迟样本或工作数，也不会遗留后台任务。
- 延迟统计只覆盖已真实发送的请求。通用 `latency_p50/p95` 包含已结束的超时、错误等采样，
  不能称为“成功完整回答的 p95”；完整回答耗时请按逐请求记录的 `outcome=success` 筛选。
  `success_rate` 同样只基于已发送样本，`not_sent` 单独计数。百分位为最近秩
  （第 `ceil(p*n)` 个），不插值，空样本为 `null`。
- 每批新的潜在付费工作前后，调用精确计量接口检查累计费用。费用为未知或达到阈值时停止
  **下一批**；已经发出的并发请求费用不能保证回滚。该门禁不是供应商账单硬停。
- 上传串行、小批量，显式允许后才执行；会真实产生入库/向量化费用，工具不会自动删除文档，
  也不会回滚已发生费用。已去重的文档不重复计入新文档；就绪超时只记录真实新文档结果。
  只要上传请求**已经实际发出但未取得确认**（HTTP 错误、超时、连接错误、响应畸形或
  `document_id` 非法），该次上传就按未确认费用处理：不重试、不伪造 ID，立即停止后续上传
  和问答，最终 `total_cost` 保持 `null`/unknown，且不能被其他已确认文档的已知费用覆盖。
  报告分别给出 `declared`/`attempts`/`sent_attempts`/`confirmed_new`/`deduplicated`/
  `unconfirmed`，不会把未确认请求算作新文档。
- 采集`operations/summary` 时，队列/数据库降级保持 `null`，绝不补零；入库/队列未测量时
  也是 `null`/unknown。
- 后台摘要 `summary` 与项目索引 `project_embedding` 明确不在此工具/精确请求测量的范围内。
- 报告不包含问题/答案/引用正文、文件名/路径、token、用户 ID、request ID 或模型地址；
  只保留请求序号、稳定分类和脱敏金额。目标只用规范化指纹表示。

## 精确费用测量 API

`POST /api/v1/usage/measurement`，仅管理员。请求：

```json
{
  "start": "2026-06-15T00:00:00Z",
  "end": "2026-06-16T00:00:00Z",
  "requests": [
    {"user_id": "00000000-0000-0000-0000-000000000001",
     "request_id": "00000000-0000-0000-0000-000000000002"}
  ],
  "document_ids": ["00000000-0000-0000-0000-000000000003"]
}
```

- `start/end` 必须带时区，统一转 UTC，半开区间 `[start, end)`；最多 31 天。
- `requests` / `document_ids` 各自最多 50 项；至少选择一类；拒绝重复、空选择、未知字段。
- `(user_id, request_id)` 精确定位 `ChatTurn`，再按 `ChatTurn.id` 聚合该轮**所有实际尝试**
  （`generation`/`rewrite`/`query_embedding`/`rerank` 等），包含失败、取消、中断和
  已开始未完成；同一 `request_id` 在另一个账号下不会被混算。
- 文档只匹配本轮选中的 `document_id` + `document_embedding` + 窗口；旧文档历史不混入。
- 不读取 `ChatTurn.trace`，不做全局前后差值。
- 有 `turn` 但窗口内无计量、`turn`/文档不存在、用量字段缺失、未计价、未完成、关联用户
  不一致、币种冲突、结果截断，都显式表现为 `unknown`/`partial`；`total_cost` 仅在覆盖
  完整时存在，`known_cost_subtotal` 只是已知部分，绝不补零。
- 查询错误/语句超时返回 503 通用错误，不泄露 SQL、连接或异常正文。

响应结构（`data`）：

```json
{
  "schema": "rag-usage-measurement/v1",
  "generated_at": "2026-06-16T00:00:01Z",
  "window_start": "2026-06-15T00:00:00Z",
  "window_end": "2026-06-16T00:00:00Z",
  "currency": "CNY",
  "totals": {
    "attempts": 2, "succeeded_attempts": 1, "failed_attempts": 1,
    "input_tokens": 150, "output_tokens": 20, "cached_input_tokens": 0,
    "total_tokens": 170, "usage_missing_attempts": 0,
    "priced_attempts": 2, "unpriced_attempts": 0,
    "known_cost_subtotal": "0.0015000000",
    "total_cost": "0.0015000000"
  },
  "by_operation": [{"operation": "generation", "attempts": 1, "...": "..."}],
  "requests": [{
    "user_id": "...", "request_id": "...", "found": true,
    "turn_status": "completed", "attempts": 2,
    "operations": ["generation", "rewrite"],
    "known_cost_subtotal": "0.0015000000",
    "coverage_status": "complete", "reasons": []
  }],
  "documents": [],
  "coverage": {
    "status": "complete", "complete": true, "truncated": false,
    "missing_requests": 0, "missing_documents": 0,
    "attempts_missing_requests": 0, "attempts_missing_documents": 0,
    "usage_missing_attempts": 0, "unpriced_attempts": 0,
    "unfinished_attempts": 0, "association_mismatch_attempts": 0,
    "currency_mismatch": false, "currencies": ["CNY"],
    "background_operations_unmeasured": ["summary", "project_embedding"],
    "notes": []
  },
  "boundaries": ["request scope: ...", "document scope: ...", "window: ...", "measurement: ..."]
}
```

## 退出码

| 码 | 含义 |
| --- | --- |
| `0` | dry-run 计划校验通过；或 live 测量完成（仍不代表真实容量/业务验收） |
| `1` | 运行失败，或达到费用阈值而停止 |
| `2` | 本地配置/凭证变量/输出路径/显式确认错误；HTTP 未发出 |
| `3` | 结果未知：总期限、最终费用无法取得、覆盖不完整等 |

## 报告边界

`_live_report` 只输出单个目标指纹、请求序号、稳定错误分类、脱敏金额、聚合延迟
（仅已发送请求；p50/p95，样本不足为 `null`）、上传/入库的已就绪结果、费用检查阶段与硬上限。
它不会输出问题/答案/引用正文、文件名/路径、用户/请求身份、token、模型地址或原始响应体。
请求与费用身份只在内存中短暂关联，报告不带这些关联 ID。

## 测试

```bash
cd backend
./.venv/Scripts/python.exe -X utf8 -m unittest -v \
  tests.test_capacity_benchmark tests.test_usage_measurement
```

数据库集成用例只在 `TEST_DATABASE_URL` 指向库名以 `_test` 结尾的专用库时运行，否则整组
跳过（不算通过）。CLI 的 live 路径使用自持有回环/`httpx.MockTransport` 替代，不访问真实
API 或模型。

## 已知限制

- 本地小样本只给趋势，不证明企业并发容量、SLA 或生产可用性。
- 费用门禁只在批次边界检查；正在进行的并发请求和供应商延迟计费不可撤销。
- 最终费用查询需要剩余时间；无剩余时间时保持 unknown，不用 0 代替。
- 后台摘要/项目索引不测量；`operations/summary` 队列降级时保持 `null`。
- 请求在到达服务器前就失败时，精确测量通常表现为 `turn_not_found`/unknown，而非 0。
- 上传会真实改变目标知识库并可能产生费用，工具不会自动清理。
