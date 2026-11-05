# 固定问题集评测

本目录是公开合成样例，不是业务质量基准。`responses.example.jsonl` 是人工构造的离线响应，6/6 断言通过只验证评测工具自身，不能证明真实模型准确率。

## 目录

| 路径 | 用途 |
| --- | --- |
| `cases.example.jsonl` / `responses.example.jsonl` | 6 条基础合成回放（事实、追问、换话题、拒答、澄清、缺引用） |
| `cases.boundaries.example.jsonl` | 来源失效与权限边界 |
| `cases.scopes.example.jsonl` | 文档范围与多轮发布验收（真实执行，无预置响应） |
| `cases.platform.example.jsonl` / `responses.platform.example.jsonl` | 8 条扩展合成回放：真多分块定位、跨文档、拒答、追问、换话题、文档范围、旧索引代次 |
| `responses.platform.regressed.example.jsonl` | 断言退步样例（两条引用落到错误片段/旧代次） |
| `responses.platform.slow.example.jsonl` | 断言不变、延迟显著变慢样例 |
| `responses.platform.missing-metrics.example.jsonl` | 缺少 `first_token_ms` 的缺指标样例 |
| `baseline.platform.example.json` | 固定 run ID 的离线基线报告（无正文），用于比较门禁 |
| `thresholds.example.json` | 延迟/费用的相对阈值示例 |
| `templates/` | 真实业务问题标注模板与人工核验流程（仅模板，无真实资料） |

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

扩展输出：

- 每个 case 延迟记为 `first_token_ms`、`total_ms`；报告新增 `first_token_ms_p50` / `p95`、`total_ms_p50` / `p95`，以及 `*_samples` 样本数。样本为 0 时保持 null，不补 0；只覆盖部分 case 时不参与比较。
- `expected_sources` 新增可选 `index_version`：旧索引代次的引用即使文件名和片段号相同也判失败。
- `document_ids`（1–5 个 UUID）只允许出现在 `mode=rag` 且没有 `conversation_id` 的新会话 case；真实模式只在首轮传入，续聊沿用会话已保存的范围。`mode=chat` 携带范围会在校验阶段被拒绝。

## 数据集身份与基线比较

每份报告新增 `identity`：

- `dataset.sha256`：按 case id 排序后对问题、断言和 `document_ids` 的规范 JSON 取摘要；`dataset.case_count` 为用例数。
- `dataset.documents.sha256`：对问题集实际引用、且存在于 `documents/` 下的公开文档字节取摘要（`files`、`missing` 为计数）。不会写入文件名或正文。
- `config`：仅 `mode`、`model`、`embedding_model`、`chunk_size`、`chunk_overlap`、`retrieval_top_k`、`retrieval_rewrite`，可来自 `--config-file`；含未知键（如 `token`、`base_url`）直接拒绝，值含 URL 或账号信息也拒绝。
- `metrics_version`：指标口径版本；口径变化会递增并让旧基线不可比。

比较使用 `--baseline`（另可用 `--comparison-output` 单独导出比较结果）：

```powershell
# 通过：同数据集、同配置，退出码 0
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset evaluation/cases.platform.example.jsonl --responses evaluation/responses.platform.example.jsonl --output evaluation-results/current.json --baseline evaluation/baseline.platform.example.json

# 退步：断言/引用下降，退出码 1（断言失败优先于比较结论）
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset evaluation/cases.platform.example.jsonl --responses evaluation/responses.platform.regressed.example.jsonl --output evaluation-results/regressed.json --baseline evaluation/baseline.platform.example.json

# 退步：断言不变但延迟超出阈值，退出码 3
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset evaluation/cases.platform.example.jsonl --responses evaluation/responses.platform.slow.example.jsonl --output evaluation-results/slow.json --baseline evaluation/baseline.platform.example.json --thresholds evaluation/thresholds.example.json

# 缺指标：门槛覆盖的延迟指标缺测，退出码 5，不当作 0 或通过
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset evaluation/cases.platform.example.jsonl --responses evaluation/responses.platform.missing-metrics.example.jsonl --output evaluation-results/missing.json --baseline evaluation/baseline.platform.example.json --thresholds evaluation/thresholds.example.json

# 不可比：换成另一个问题集，退出码 4
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset evaluation/cases.example.jsonl --responses evaluation/responses.example.jsonl --output evaluation-results/other.json --baseline evaluation/baseline.platform.example.json
```

旧报告没有 `identity` 时比较结论固定为 `incomparable`（退出码 4），不会崩溃，也不会说成“无退步”。

基线输入按 fail-closed 校验：`results` 必须是列表且与 `cases` 计数一致，`passed` 与各指标必须是有限的非负数（含超出 float 范围的超大整数，不会被当作有效数字），`*_samples` 必须是 0–cases 且样本为 0 时不得带指标值，`identity` 的摘要必须是 SHA-256、`case_count` 必须与 cases 一致。`identity.config` 的每个可比字段还会按写入时同样的规则校验类型与取值（如 `mode` 必须是 `offline`/`live`，`chunk_overlap < chunk_size`），不会把列表/对象当字典键。任一不符返回退出码 2，不会把字符串/对象拿去做减法而抛栈。两侧都缺 `dataset.sha256` 时不会因 `None == None` 判成同一数据集，而是 `dataset_identity_missing`。

`--live` 会在任何 HTTP 请求前完成问题集、阈值、配置身份和基线文件的全部校验；`chunk_overlap >= chunk_size`、`model` 含 URL/账号、基线结构损坏等都会先返回退出码 2，不会先产生模型费用再报错。

### 比较状态与退出码

| 退出码 | 含义 |
| --- | --- |
| 0 | 断言全通过，且比较结论为 `comparable/no_regression`（未提供基线时即断言全通过） |
| 1 | 固定断言失败（优先于任何比较结论） |
| 2 | 输入或配置错误（含未知阈值指标、未知配置键、坏 JSON、HTTP 地址不合法） |
| 3 | 断言全通过，但比较判定退步（`comparable/regression`） |
| 4 | 不可比：数据集/文档/关键配置变化、旧基线缺身份（`incomparable`） |
| 5 | 断言全通过且可比，但被门槛覆盖的指标缺测或缺覆盖（`unknown`） |

比较字段：

- `status`：`comparable` / `incomparable` / `unknown`；`verdict`：`no_regression` / `regression` / `incomparable` / `unknown`。
- `metrics.<name>.delta = current - baseline`，`relative_delta` 仅在 baseline > 0 时给出；方向由指标本身固定（越高越好 / 越低越好），调用方无法反转。
- `regressions` / `improvements` / `unknown_metrics` / `not_applicable_metrics` / `reasons` 分别列出结果。
- 不可比的原因使用稳定标识：`dataset_changed`、`documents_changed`、`documents_identity_missing`、`config_changed:<字段>`、`config_unverifiable:<字段>`、`baseline_identity_missing`、`metrics_version_mismatch`。
- 缺指标：任一侧缺值 → `unknown`；两侧都未测量且未设门槛 → `not_applicable`（不参与结论）；配置了门槛却测不到 → `unknown` 并阻断“无退步”。
- 覆盖不明：延迟/费用指标有数值但缺 `*_samples` 计数时判 `unknown`（`detail=coverage_unverifiable`），不默认全覆盖；`0 < samples < cases` 同样 `unknown`（`partial_samples`）。

### 阈值

`--thresholds` 接受 JSON 对象，键为指标名，值为 `{"max_absolute_change": 非负数}` 和/或 `{"max_relative_change": 非负数}`：

- 默认门槛已覆盖断言通过率与三项来源召回、拒答匹配（绝对变化 0）。
- 延迟与费用默认不设门槛（需要显式给出，避免环境抖动被当退步）；`thresholds.example.json` 给出 p95 与费用的相对阈值示例。
- `max_relative_change`：baseline 为 0 时不计算比例，任何变差都算超出；基数为 0 且当前仍为 0 视为不变。
- 同一指标在 `--thresholds` 中给出时完整覆盖默认规则，不会暗中保留默认限制。
- 未知指标名、未知规则名、负值、布尔或非有限值一律返回退出码 2。

## 真实服务评测（显式启用）

1. 使用隔离的测试部署；等待数据库和模型服务可用。以管理员身份把 `documents` 下两份合成文档上传至测试知识库，等待入库完成。
2. 将测试管理员的访问令牌放入当前终端的 `RAG_EVAL_TOKEN` 环境变量；令牌不要写进问题集、命令历史中的明文参数或报告。
3. 使用实际的测试知识库 UUID：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --live --dataset evaluation/cases.example.jsonl --base-url http://127.0.0.1:8000 --kb-id YOUR_TEST_KB_UUID --output evaluation-results/live.json
```

真实模式串行执行，默认最多 20 个 case，不自动上传、删除、重建任何文档，不自动重试模型调用。它会创建会话并产生模型费用。远程 HTTP 明文传输令牌需要额外的 `--allow-insecure-http`，优先使用 HTTPS。脚本的 `--timeout` 是 HTTP 读写超时。

`--live` 必须显式指定，任何离线流程都不会自动回落到真实调用。真实基线比较时建议用 `--config-file` 声明 `model`、`embedding_model`、`chunk_size`、`chunk_overlap`、`retrieval_top_k`、`retrieval_rewrite`；缺少这些身份会被判为 `incomparable`，因为换模型后“无退步”没有意义。费用按实际 token 用量估算，缺用量时保持 null。

### 扩展合成问题集（`cases.platform.example.jsonl`）

`documents/合成数据平台操作手册.md` 与 `documents/合成安全响应预案.md` 在默认 `CHUNK_SIZE=800` 下各切成 5 块，样例覆盖：

- 编号定位：问题指向特定章节，期望片段为 `chunk_index: 3/4`，同文档但错片段会失败；
- 跨文档：一条问题同时要求两份文档的来源；
- 拒答：问题超出资料范围，要求 `refuse` 且无引用；
- 追问与话题切换：先问 P2 时效再问“那 P1 呢”，以及“换个话题”后回到另一文档；
- 文档范围：`document_ids` 限定在指南，禁止出现手册的四级口径；
- 旧代次边界：`index_version` 不匹配即失败。

离线回放（`responses.platform.example.jsonl`）8/8 通过只验证评测工具，不代表真实模型质量。

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

## 文档范围与多轮发布验收

`cases.scopes.example.jsonl` 提供四条完整多轮用例：选择文件后继续回答分级、检索此前未引用的外发正文、所选文件未提供细节时拒答、明确换话题后恢复全库。请在隔离测试知识库上传 `合成数据安全指南.md`、`合成内部管理手册.md` 和 `合成错误手册.md`，等待就绪后显式执行：

```powershell
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --live --dataset evaluation/cases.scopes.example.jsonl --base-url http://127.0.0.1:8000 --kb-id YOUR_TEST_KB_UUID --output evaluation-results/scopes-live.json
```

这里没有预制“成功响应”：真实模式会产生模型调用费用，结果必须实际核验。脚本的断言只检查最后一轮；人工还需逐轮检查引用原文，确认三级指南和四级手册没有混用。数据库自动回归使用模拟模型与向量，验证真实 SQL、跨请求状态、迁移和并发行为，不代表真实模型语义准确率。

发布前在页面完成以下检查：

1. 先问“合成数据安全资料中，数据分几级？→具体点呢→合成内部管理手册→内部级能否外发？”。范围条应在明确选择后显示手册，最后一问仅引用手册中的审批、脱敏原文。
2. 刷新页面，确认范围仍为手册；再问审批系统网址，应说明没有依据且不引用其他指南。随后问一次手册已覆盖的问题，确认无引用的上一轮没有清除范围。
3. 在有输入草稿时切换到指南，再清除范围，确认消息和草稿保留，服务端返回的范围与页面一致。明确说“换个话题”成功完成后，也应恢复全库。
4. 选择器至少准备 21 份就绪文档，确认可加载后续页且最多选择 5 份。切换知识库、模式或新建会话会重置未发送的选择；普通对话不显示范围条。
5. 在两个页面打开同一会话，一页修改后，另一页的旧版本发送/保存应返回 409，不覆盖新范围；生成中调整范围应被禁用且后端同样拒绝。
6. 分别删除、取消、重新索引所选文件。下一轮应提示重新选择或清除，不能检索其他文件；重新选择后绑定最新索引代次。生成过程中发生这些变更时，禁止保存成成功回答。
7. 重试已经完成的请求：答案仍为原结果，范围应保持当前最新状态，不因重放倒退。停止请求、切换会话后，迟到流事件也不得覆盖页面范围。

## 仅检查模型连通性

下面的命令不需要业务数据库，只发送固定合成文本：

```powershell
.\.venv\Scripts\python.exe -m scripts.smoke_models --live --timeout 15
```

可用 `--only llm` / `--only embedding` 单独检查；LLM 默认最大输出 256 token，可用 `--max-tokens` 调整（最多 512）。检查不重试，不输出 API Key、生成正文或向量内容。连通性通过不代表完整问答链路或业务准确率通过。

## 人工检查与故障场景

固定断言不验证所有事实。请人工抽查“每项结论是否被引用原文支持”，并在专用测试库验证：同一会话并发、请求重放、断连/取消、文档删除或重新索引后的引用失效、私有知识库权限。真实数据库回归通过 `TEST_DATABASE_URL` 启用，数据库名必须以 `_test` 结尾且可清空；测试会清理该库数据。

## 真实业务问题与人工核验

`templates/business-questions.template.jsonl` 与 `templates/README.md` 给出真实业务问题的字段模板和人工核验流程：人工正确性、引用支持性、业务分层、可回答率与拒答正确率。该目录只有模板，不含真实资料。

- 现有合成样例**不能**用来宣称真实业务准确率、可回答率或拒答率。
- 业务分层结果必须同时给出样本量与未核验数量；未核验样本不计入正确率。
- 增减业务样本会改变数据集身份，比较会判为 `incomparable`，需要重建基线。
- 含个人信息或密钥的样本不得提交，报告默认不导出问题、答案和引用正文；确需人工核验时显式加 `--include-content` 并自行保护报告。

## 结果脱敏

默认报告只有 case ID、断言结果、召回比例、延迟、样本数和可用用量；不写问题、答案、引用片段。`error` 只记录异常类型名或 `http_error` 状态码，不回显上游错误正文、URL、请求头或令牌。`identity` 不含文件名、文档正文、令牌、base URL 或数据库地址。
