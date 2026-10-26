# 固定问题集评测

本目录是公开合成样例，不是业务质量基准。`responses.example.jsonl` 是人工构造的离线响应，6/6 断言通过只验证评测工具自身，不能证明真实模型准确率。

## 离线回放

在 `backend` 目录运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset evaluation/cases.example.jsonl --responses evaluation/responses.example.jsonl --output evaluation-results/offline.json
```

输入为一行一个 JSON 对象。问题集每行包含：
- `id`：唯一标识；`question` 或 `turns`：单个问题或最多 10 轮顺序提问。
- `mode`：`rag`（默认）或 `chat`；`kb_id` 可由命令行覆盖；`top_k` 默认 6。
- `expected_outcome`：`answer`、`refuse` 或 `clarify`。
- `expected_sources`：来源身份列表，使用 `filename` / `document_id`，可选 `chunk_index`（从 0 开始）。合并相邻片段时匹配其实际范围。
- `required_phrases`、`forbidden_phrases`、`expect_no_citations`：可重复的固定断言。
- `conversation_id`：可选的预置会话 UUID，用于人工准备过期引用等状态；默认每个 case 新建会话。
- `actor`：`admin`（默认）或 `member`。普通用户用例使用 `RAG_EVAL_MEMBER_TOKEN`，不会请求管理员专属诊断，避免把诊断接口拒绝误判成知识库权限隔离。
- `expected_http_status`：可选的预期 4xx 状态，用于单请求权限用例；401 与预期的 403 严格区分，成功返回业务正文会判失败。

回放会拒绝重复/缺失身份、错误的来源结构和 NaN 等非法 JSON 数值。报告分别记录检索候选命中、实际装入生成上下文的来源命中、最终引用命中，三者不能互相替代；缺少对应诊断时记为 null，不伪造召回结果。

离线响应包含 `id`、`answer`、`citations`，可选 `trace`、`first_token_ms`、`total_ms`。缺失响应、空答案、来源不匹配或断言失败会使退出码为 1；输入或配置错误返回 2。

## 真实服务评测（显式启用）

1. 使用隔离的测试部署；等待数据库和模型服务可用。以管理员身份把 `documents` 下两份合成文档上传至测试知识库，等待入库完成。
2. 将测试管理员的访问令牌放入当前终端的 `RAG_EVAL_TOKEN` 环境变量；令牌不要写进问题集、命令历史中的明文参数或报告。
3. 使用实际的测试知识库 UUID：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --live --dataset evaluation/cases.example.jsonl --base-url http://127.0.0.1:8000 --kb-id YOUR_TEST_KB_UUID --output evaluation-results/live.json
```

真实模式串行执行，默认最多 20 个 case，不自动上传、删除、重建任何文档，不自动重试模型调用。它会创建会话并产生模型费用。远程 HTTP 明文传输令牌需要额外的 `--allow-insecure-http`，优先使用 HTTPS。脚本的 `--timeout` 是 HTTP 读写超时。

报告记录 `run_id`；恢复同一轮评测时传 `--run-id` 复用请求标识，后端对已完成请求重放结果、对处理中或失败的同键请求返回 409，不再次调用模型。改变问题集或准备新一轮独立评测时使用新的 run ID。需要管理员令牌是因为请求会启用 `include_trace=true`；普通用户不能读取检索诊断。

`--input-price-per-million` 与 `--output-price-per-million` 可提供模型单价。只有服务返回实际 token 用量才计算费用估算，缺失用量时为 null，绝不把上下文的 UTF-8 字节估算当作计费用量。费用仅覆盖回答生成；追问补全、向量化、重排、缓存折扣和服务商额外收费不包含在内。OpenAI 兼容服务支持时可在后端启用 `LLM_STREAM_INCLUDE_USAGE=true`。

默认报告只有用例 ID、断言结果、来源匹配比例、延迟与可用的用量，业务问题、答案和引用片段不会导出。确需人工核验时显式传 `--include-content`，并自行保护报告。`evaluation-results/` 已被 Git 忽略。

## 来源失效与权限边界用例

`cases.boundaries.example.jsonl` / `responses.boundaries.example.jsonl` 是两条额外的合成回放：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset evaluation/cases.boundaries.example.jsonl --responses evaluation/responses.boundaries.example.jsonl --output evaluation-results/boundaries.json
```

真实执行前在专用测试部署准备以下状态，再运行 `--live`：

1. 由测试管理员把合成文档放进**私有**知识库并问一次，确认回答实际引用了来源 `[1]`，然后在页面重新索引或删除该来源文档。把问题集中的 `conversation_id` 占位 UUID 替换成这条会话的真实 UUID。来源失效用例会继续该会话，而不是自动修改文档。
2. 创建一个不拥有该私有库的普通测试用户，把其令牌放进 `RAG_EVAL_MEMBER_TOKEN`（可用 `--member-token-env` 换环境变量名）。普通用户请求应得到 403；令牌失效导致 401 会失败，不能算权限测试通过。
3. `RAG_EVAL_TOKEN` 仍是管理员令牌，`--kb-id` 指向同一个私有测试库。所有用例需要的令牌和知识库标识会在第一条请求前校验，配置不完整时不发起部分付费调用。

脚本不自动创建用户、重建或删除文档。边界回放通过同样只验证断言工具；真正的跨连接并发、迁移和权限行为另由隔离数据库回归验证。

## 仅检查模型连通性

下面的命令不需要业务数据库，只发送固定合成文本：

```powershell
.\.venv\Scripts\python.exe -m scripts.smoke_models --live --timeout 15
```

可用 `--only llm` / `--only embedding` 单独检查；LLM 默认最大输出 256 token，可用 `--max-tokens` 调整（最多 512）。检查不重试，不输出 API Key、生成正文或向量内容。连通性通过不代表完整问答链路或业务准确率通过。

## 人工检查与故障场景

固定断言不验证所有事实。请人工抽查“每项结论是否被引用原文支持”，并在专用测试库验证：同一会话并发、请求重放、断连/取消、文档删除或重新索引后的引用失效、私有知识库权限。真实数据库回归通过 `TEST_DATABASE_URL` 启用，数据库名必须以 `_test` 结尾且可清空；测试会清理该库数据。
