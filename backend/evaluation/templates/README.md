# 真实业务问题标注与人工核验

本目录只提供 [字段模板](business-questions.template.jsonl)，不含真实业务资料。模板采用合法的分层代号和空核验元数据，便于先做结构校验；其中的 `REPLACE-*` 和中文占位符仍必须替换。开启 `--require-reviewed` 后，原始模板会在任何真实调用前被拒绝。

合成问题和预置响应只验证评测工具，不能据此宣称真实业务准确率或拒答质量。

## 抽样与标注

1. 按业务分层抽样，记录抽样来源、时间范围与每层样本量。优先覆盖真实问题的原始说法、编号、单位、文档版本和时点。
2. 根据原文标注 `expected_outcome`：有充分依据为 `answer`，资料明确不支持为 `refuse`，需要补充条件为 `clarify`。不要用模型自己生成的答案作标准答案。
3. 为可回答问题标注来源身份及必要的原文要点；逐项检查引用是否支持结论，核对时效、单位与版本。敏感问题或资料不得提交到公开仓库。
4. 至少一人核验；关键样本建议双人独立标注并裁定分歧。使用核验人代号和日期，未解决的问题保留在 `unknowns`，全部解决后设为空列表。
5. 将 `reviewed` 设为 `true`，填写有效的核验人代号与日期，然后运行严格模式。`reviewed: false` 即使带有旧核验信息仍表示未核验。

## 字段与报告

| 字段 | 约定 |
| --- | --- |
| `id` | 稳定、脱敏的抽样代号；会出现在报告中，不要直接使用含个人信息的工单标题 |
| `question` / `turns` | 脱敏后的真实问题或最多 10 轮提问 |
| `expected_outcome` | `answer` / `refuse` / `clarify` |
| `expected_sources` | 核验过的来源，可含 `chunk_index` 与 `index_version` |
| `required_phrases` / `forbidden_phrases` | 必须出现的要点与禁止出现的错误口径；仍是固定断言 |
| `stratum` | 登记的分层代号；缺省为 `unclassified` |
| `verified_by` | 1–64 位 ASCII 代号，字母数字起始，可含 `.`、`_`、`-`；不填姓名或邮箱 |
| `verified_at` | 有效的 `YYYY-MM-DD` 日期 |
| `unknowns` | 最多 50 条未解决问题，每条非空、最多 500 字符；严格模式要求为空 |
| `reviewed` | 可选布尔值；显式 `false` 会阻断严格模式 |

分层代号：`fact`（事实）、`cross_doc`（跨文档）、`process_sla`（流程与时效）、`access_compliance`（权限与合规）、`unanswerable`（无答案）；也可用 `platform`、`hr`、`finance`、`operations`、`security`、`compliance` 或 `unclassified`。使用固定代号，避免把部门或个人名称带入分组报告。

报告保留每层样本数、断言通过率、`reviewed` / `unreviewed`、`review_complete` / `review_incomplete` 与失败原因。缺少核验人或日期为 `unreviewed`；核验人和日期齐全但 `unknowns` 非空为 `incomplete`。默认报告不输出核验人原值、问题、答案或引用正文。

核验元数据齐全只说明问题集的标注状态，不能说明本轮模型答案已经人工判为正确。固定断言通过率、来源匹配率和人工事实正确率应分别报告。人工结果还需记录评审样本数、未核验数和分歧处理；未核验回答不能算正确。

## 运行严格模式

将完成标注的样本另存为仓库外的私有问题集，私有响应也保存在仓库外。在 `backend` 目录执行，例如：

```powershell
# 离线回放，不访问模型
.\.venv\Scripts\python.exe -m scripts.evaluate_rag --dataset C:/private/cases.jsonl --responses C:/private/responses.jsonl --require-reviewed --output C:/private/report.json
```

真实评测需显式加入 `--live`、知识库和服务地址，并在指定环境变量中准备令牌。按 [评测说明](../README.md) 声明实际公开配置、语料快照和可选索引清单；真实模式会产生调用费用。严格模式检查发生在创建 HTTP 客户端之前，但不会替代部署方对资料授权和人工标注内容的核查。

## 基线与指标解释

- 修改问题、断言、范围或样本数量会改变数据集身份，需要重建相同问题集的基线。`stratum`、`verified_by`、`verified_at`、`unknowns`、`reviewed` 是纯核验元数据，不改变问题集摘要。
- 每层均报告样本量和未核验数量。资料可回答率可按人工标为 `answer` 的样本占比统计；模型答案正确率与拒答正确率需依据逐条人工核验结果计算，不能从固定断言通过率推导。
- 延迟、费用只有样本覆盖完整时才参与比较；缺测保持 `unknown`，不补零。费用仅覆盖返回实际 token 用量的回答生成。
- 默认不使用 `--include-content`；确需人工核验时在受控目录导出，并依照自己的资料保留与访问制度管理。
