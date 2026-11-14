# 真实业务问题标注与人工核验

本目录只提供 [2 题字段模板](business-questions.template.jsonl)、[20 槽位首批采集模板](business-baseline-20.template.jsonl) 和 [结果核验 JSON 模板](result-reviews.template.json)，不含真实业务资料。模板采用合法的分层代号和空核验元数据，便于先做结构校验；其中的 `REPLACE-*` 和中文占位符仍必须替换。开启 `--require-reviewed` 后，原始问题集模板会在任何真实调用前被拒绝。结果核验模板只是格式示例，必须用 `scripts.review_evaluation --create-template` 从本轮报告生成，不能手工伪造 `run_id`/指纹绑定。

合成问题和预置响应只验证评测工具，不能据此宣称真实业务准确率或拒答质量。

## 首批 20 槽位采集模板与本地输入预检

`business-baseline-20.template.jsonl` 是**空白采集结构**：事实 6、跨文档 4、流程时效 4、无答案拒答 4、需澄清 2。全部 `reviewed=false`、核验人和日期为 `null`、`unknowns` 非空、`source_labels_complete=false`，并带明显 `REPLACE-*` / 中文占位符；不含真实业务标准或答案。20 题只是首批可操作规模的建议，**不是统计充分性证明**，不能把填满模板当作正式抽样、人工核验或业务质量通过。

把模板复制到仓库外的受控私有目录，由资料所有者填写真实问题、来源身份、预期处置和固定断言，再逐条人工核验、清空 `unknowns`、填写核验人代号与日期并设 `reviewed=true`。问题集里的 `reviewed`/`verified_by`/`verified_at` 只表示**问题标注**已核验；本轮模型答案是否正确仍须按下文的结果核验流程逐条判断，两者不能互相替代。

在 `backend` 目录可对仓库外私有输入做纯本地预检，命令只读取显式给出的文件，不写报告、不执行评测：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.preflight_evaluation --dataset C:/private/cases.jsonl --documents-dir C:/private/corpus --config-file C:/private/public-config.json --index-manifest C:/private/index-manifest.json
```

- 参数仅 `--dataset`（必填）、`--documents-dir`、`--config-file`、`--index-manifest`、`--max-cases`（默认 20，合法 1–1000）；没有 responses/live/token/base-url/输出文件/overwrite 开关。典型命令还可设 `RAG_DISABLE_DOTENV=1`、`LOG_STDOUT_ONLY=1`。
- 退出码 `0` 是**本地输入就绪**，`5` 是缺准备项，`2` 是输入或 I/O 不合法；stdout 只有固定 JSON。完整批次一律校验，超限只阻断、不切片。
- 阻断项包括空集、占位符/未完成人工核验、未明确预期处置（预期 HTTP 4xx 负例除外）、未分类分层、RAG 应答案例缺 `expected_sources`、公开配置缺 live 必填或开关开启时的条件字段、缺/空语料、空索引清单及超过上限。原因只使用稳定代号和 1 起始序号，不回显原 case ID 或正文。
- 显式语料目录必须合法且非空；不指定为阻断，不存在、符号链接或不可读为退出码 2。不自动读取问题集旁的 `documents/`，不自动使用 [公开配置样例](../config.live.example.json)。索引清单可选，省略不阻断。
- 输出明确标记 `measured=false`、`business_acceptance=false` 及零网络/模型/数据库/令牌/服务地址/配置模块/.env/文件写入访问，并给出可取得的本地输入指纹；`config` 指纹只含摘要，不含模型名或参数值。语料指纹只是读取时的本地快照声明，不证明远端索引一致。
- `ready` 不核对资料授权、目标实例、调用预算、远端索引装载状态或人工审批，也不验证本轮答案质量；不能据此宣称上线验收通过。`live` 真实评测仍须另行明确授权范围、可用实例、预算和资料权限，并使用独立评测入口执行。

## 抽样与标注

1. 按业务分层抽样，记录抽样来源、时间范围与每层样本量。优先覆盖真实问题的原始说法、编号、单位、文档版本和时点。
2. 根据原文标注 `expected_outcome`：有充分依据为 `answer`，资料明确不支持为 `refuse`，需要补充条件为 `clarify`。不要用模型自己生成的答案作标准答案。
3. 为可回答问题标注来源身份及必要的原文要点；逐项检查引用是否支持结论，核对时效、单位与版本。标记排序指标前先固定检索范围：登记知识库、文档范围、索引代次与抽样时间，并确认相关来源已完整列出后才设 `source_labels_complete: true`。语料或范围变化后必须重新标注；未完整标注的样本不能计算 precision@K。敏感问题或资料不得提交到公开仓库。
4. 至少一人核验；关键样本建议双人独立标注并裁定分歧。使用核验人代号和日期，未解决的问题保留在 `unknowns`，全部解决后设为空列表。
5. 将 `reviewed` 设为 `true`，填写有效的核验人代号与日期，然后运行严格模式。`reviewed: false` 即使带有旧核验信息仍表示未核验。

## 字段与报告

| 字段 | 约定 |
| --- | --- |
| `id` | 稳定、脱敏的抽样代号；会出现在报告中，不要直接使用含个人信息的工单标题 |
| `question` / `turns` | 脱敏后的真实问题或最多 10 轮提问 |
| `expected_outcome` | `answer` / `refuse` / `clarify` |
| `expected_sources` | 核验过的来源，可含 `chunk_index` 与 `index_version` |
| `source_labels_complete` | 严格布尔值，缺省 `false`；只有确认固定检索范围/固定语料快照下相关来源已完整列入 `expected_sources` 时才设为 `true`。显式 `[]` + `true` 表示确认没有相关来源 |
| `required_phrases` / `forbidden_phrases` | 必须出现的要点与禁止出现的错误口径；仍是固定断言 |
| `stratum` | 登记的分层代号；缺省为 `unclassified` |
| `verified_by` | 1–64 位 ASCII 代号，字母数字起始，可含 `.`、`_`、`-`；不填姓名或邮箱 |
| `verified_at` | 有效的 `YYYY-MM-DD` 日期 |
| `unknowns` | 最多 50 条未解决问题，每条非空、最多 500 字符；严格模式要求为空 |
| `reviewed` | 可选布尔值；显式 `false` 会阻断严格模式 |

分层代号：`fact`（事实）、`cross_doc`（跨文档）、`process_sla`（流程与时效）、`access_compliance`（权限与合规）、`unanswerable`（无答案）；也可用 `platform`、`hr`、`finance`、`operations`、`security`、`compliance` 或 `unclassified`。使用固定代号，避免把部门或个人名称带入分组报告。

报告保留每层样本数、断言通过率、`reviewed` / `unreviewed`、`review_complete` / `review_incomplete` 与失败原因。缺少核验人或日期为 `unreviewed`；核验人和日期齐全但 `unknowns` 非空为 `incomplete`。默认报告不输出核验人原值、问题、答案或引用正文。

核验元数据齐全只说明问题集的标注状态，不能说明本轮模型答案已经人工判为正确。固定断言通过率、来源匹配率和人工事实正确率应分别报告。人工结果还需记录评审样本数、未核验数和分歧处理；未核验回答不能算正确。

## 本轮结果人工核验

问题集里的 `reviewed`/`verified_by`/`verified_at` 只描述**问题集标注**是否完成，不能说明本轮模型答案正确。本轮结果必须使用独立核验文件逐条判断；两者在报告中分别汇总为 `annotation_review_state` 与 `human_review`。

### 核验文件格式

统一 schema 为 `rag-eval-result-reviews/v1`：

```json
{
  "schema": "rag-eval-result-reviews/v1",
  "run_id": "REPORT-RUN-ID",
  "identity_sha256": "报告 identity 的完整规范 JSON SHA-256",
  "result_fingerprint_version": 1,
  "mode": "offline",
  "reviews": [
    {
      "id": "CASE-ID",
      "result_sha256": "报告该条 result_sha256",
      "answer_correct": null,
      "citation_supported": null,
      "outcome_correct": null,
      "reviewer": null,
      "reviewed_at": null,
      "unknowns": []
    }
  ]
}
```

- 顶层只允许上述六个键；`mode` 可省略，若出现必须与报告一致，且报告 `mode` 必须与 `identity.config.mode` 一致。不得把 `offline` 改成 `live` 升级验收结论。
- 每条只允许 `id`、`result_sha256`、三个判断、`reviewer`、`reviewed_at`、`unknowns`，以及模板显式附带的 `content`。案例 ID 不得重复或缺失，也不得加入报告之外的案例。
- 三个判断必须是 `true` / `false` / `null`。`null` 表示未核验或不适用，**绝不计为正确**。可回答问题三项都必须明确；拒答/澄清可把答案正确性与引用支持填 `null`（N/A），但处置合理性必须核验。预期 HTTP 负例三项均为 N/A，机器状态另行精确核对。
- `reviewer` 为 1–64 位 ASCII 代号（字母数字起始，可含 `.`、`_`、`-`），不写姓名或邮箱；`reviewed_at` 为有效 `YYYY-MM-DD`。`unknowns` 最多 50 条，每条非空、最多 500 字符；有未决项时不能通过；缺少核验人或日期时即使填了 `true` 也只计未核验。
- `content` 只能来自 `--include-content` 报告，并由 `--template-include-content` 显式复制；常规核验摘要不回显 `content`、`reviewer` 或 `unknowns` 正文。若核验条目携带 `content`，报告也必须携带对应正文且内容严格一致，否则按不可比拒绝。
- 报告和核验文件中的字符串及对象键必须是合法 Unicode：孤立 UTF-16 代理（如 `\ud800`）按输入错误退出 `2`，不生成输出、不回显原值；合法的代理对（emoji）会规范化为实际字符后正常接受。

### 流程

1. 先运行评测，确认报告 `mode` 与本轮一致；`offline` 只是本地回放，不能称真实业务/生产验收通过。
2. 生成空模板，所有判断保持 `null`，不自动通过：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.review_evaluation --report C:/private/report.json --create-template C:/private/report.reviews.json
```

3. 在受控目录逐条阅读实际答案与引用原文，填写判断、核验人、日期和未决项。真实资料是否允许导出、留存和送审由资料所有者书面授权；禁止把真实业务正文提交到公开仓库或外发。
4. 运行核验：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m scripts.review_evaluation --report C:/private/report.json --reviews C:/private/report.reviews.json --output C:/private/report.review-summary.json
```

退出码：`0` 为 `reviewed_pass`（机器断言与人工逐条核验均通过，仍不是自动语义证明或准确率）；`1` 为人工判错/机器断言失败；`2` 为输入或结构错误；`4` 为 run/identity/mode/指纹/案例集合不可比；`5` 为缺核验、`null` 或未决项。机器断言失败不能由人工 `true` 覆盖。

### 指纹失效与基线

报告的每条 `result_sha256` 覆盖实际答案、全部实际引用（含 `index_version`、实际片段与范围）、实际 HTTP 状态和错误码，不覆盖耗时。答案、引用、来源代次或 HTTP 状态任一改变，旧核验文件即失效，必须重新生成模板并核验。缺指纹的旧报告、重复/缺失/额外案例、错误 `run_id` 或 `identity_sha256` 都会 fail closed。

完成本轮核验后，才用相同问题集、完整语料快照、公开配置和显式索引清单重建基线并比较。未核验、未知和未决项不会进入正确率；固定断言通过率、来源匹配率和人工结果必须分开报告。

## 运行严格模式

将完成标注的样本另存为仓库外的私有问题集，私有响应也保存在仓库外。在 `backend` 目录执行，例如：

```powershell
# 离线回放，不访问模型
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset C:/private/cases.jsonl --responses C:/private/responses.jsonl --require-reviewed --output C:/private/report.json
```

真实评测需显式加入 `--live`、知识库和服务地址，并在指定环境变量中准备令牌。按 [评测说明](../README.md) 声明实际公开配置、语料快照和可选索引清单；真实模式会产生调用费用。严格模式检查发生在创建 HTTP 客户端之前，但不会替代部署方对资料授权和人工标注内容的核查。

## 单条反馈回归

单条反馈回归复用本目录的核验原则，但只针对一条已核实反馈：准备与导入只保存稳定 ID、revision、摘要和校验结论，不持久化问题、答案、引用正文或完整报告；报告正文仅在本次校验中使用。管理员导入的默认无正文报告只证明报告内部一致（`consistency_only`），不能证明远端执行真实；显式 `--include-content` 报告会用当前 case 重算问题序列与固定断言（`content_regraded`），但“断言复算通过”仍待人工复核，不等于语义正确或已修复，也不会自动改变原反馈的 verified/rejected 状态。

## 基线与指标解释

- 修改问题、断言、范围、`source_labels_complete` 或样本数量会改变数据集身份，需要重建相同问题集的基线。`stratum`、`verified_by`、`verified_at`、`unknowns`、`reviewed` 是纯核验元数据，不改变问题集摘要。
- 当前指标口径为 `metrics_version=2`；新增的 MRR / precision@K 是排序观测，不是语义正确率，也不改变旧结果/机器指纹。`metrics_version=1` 的旧报告比较固定为 `incomparable`，必须用真实离线 CLI 重新生成基线，不能手填结论。
- 排序指标按稳定 case ID cohort 比较：应测但缺 trace/response 的样本为 `unknown`，不补 0；零 eligible 表示该维度不适用。显式设置排序门槛时，无适用样本不能宣称通过。人工核验仍需逐条阅读答案与引用原文，排序变好不代表引用语义支持正确。
- 每层均报告样本量和未核验数量。资料可回答率可按人工标为 `answer` 的样本占比统计；模型答案正确率与拒答正确率需依据逐条人工核验结果计算，不能从固定断言通过率推导。
- 延迟、费用只有样本覆盖完整时才参与比较；缺测保持 `unknown`，不补零。费用仅覆盖返回实际 token 用量的回答生成。
- 默认不使用 `--include-content`；确需人工核验时在受控目录导出，并依照自己的资料保留与访问制度管理。
