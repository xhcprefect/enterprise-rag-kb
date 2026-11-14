# 业务基线离线输入预检任务卡

2026-11-14；基于 main 327ecfd，在 feat/business-baseline-preflight 上实施。当前没有获准真实语料、已标注问题集、目标实例和调用预算。本阶段准备本地工具和空白采集材料，不运行真实业务评测。

## 目标与验收

新增独立 CLI `python -m scripts.preflight_evaluation`，只读取明确指定的本地文件，在没有响应文件、令牌、服务地址和数据库的条件下检查首批业务基线输入。此工具不创建评测报告、不生成答案、不自动修改标注、不自动把任何结果判成人工通过。状态为本地输入就绪 `ready` / 缺准备项 `blocked`，格式或 I/O 不合法为错误；就绪不代表资料授权、目标实例、预算、远端索引一致性、业务质量或上线验收通过。

可验证标准：

- 复用 evaluation.py 的 JSONL、case schema、严格人工标注、完整语料树、公开配置校验与身份算法；不建立另一套 case schema、不改变已有 dataset/metrics/指纹口径。
- CLI 参数仅为 `--dataset`（必填）、`--documents-dir`、`--config-file`、`--index-manifest`、`--max-cases`（默认20，合法1–1000）；JSON 结果输出 stdout，退出0=inputs-ready、5=blocked、2=输入错误。不提供 live/token/base-url/responses/自动输出文件或 overwrite 选项。
- 完整验证全部 case；超上限阻断而不切片；空集、占位符、未核验/未决、未明确预期处置（HTTP负例除外）、未分类、RAG answer 未标注来源均不能 ready。单条反馈回归协议不属于此批次入口，显式标记要拒绝。序号只用1起始序号，不能回显原case ID。
- 显式指定的语料目录必须合法且非空；未指定或空目录为 blocked；不存在、符号链接或不可读为2。不自动读取业务配置或问题集旁目录。语料只做本地身份声明，不证明实际远端装载一致。
- 公开配置缺失/缺 live 必填项为 blocked；已给值非法为2。开关 on 才要求对应 rerank/hybrid 字段，沿用现有白名单和 config_identity，不放宽 URL/凭据及数值检查。公开配置键的 allowlist 与旧评测入口共用，旧入口行为保持兼容。
- 可选 index manifest 复用现有严格结构校验；不提供不阻断，提供空清单要 blocked；不进行远端查询或宣称已核对索引。
- JSON 只含稳定 schema、ready/blocked 状态、明确未执行/未验收标记、完整case计数/上限/合法分层与处置/核验状态计数、稳定阻断原因码及1起始序号、可取得的本地摘要/计数。不得输出问题/答案/来源文件名或ID/实际模型名/核验人/unknowns正文/文件路径/任意未知键或异常正文。不输出类似真实评测报告的完整 identity；摘要字段明确为输入指纹。
- 缺配置、缺语料与标注问题能在一次有效结构输入预检中一起呈现。非法输入输出稳定的 error code，不能 Traceback 或原异常字符串泄露正文；结构错误后不虚构剩余检查结论。
- 新增20题空白采集模板：事实6、跨文档4、流程时效4、无答案4、澄清2；澄清使用 `expected_outcome=clarify`，stratum 为既有合法代号。模板全部 `reviewed=false`、核验人/日期null、unknowns非空、source_labels_complete=false；不编写真实业务问题/答案或假造标准。现有2题字段模板保留。
- 文档提供仓库外私有输入的实际 CLI 用法，解释上限和0/2/5口径、20题建议不等于统计充分、问题标注与答案核验不同，live继续要另行核对范围/预算；不引导直接运行模板的live。

## 子任务与文件所有权

单一实施任务 A；API/工具/测试/模板和使用说明互相依赖，由同一个 general-purpose 实施子代理串行完成，不再平行分拆。

| 所有者 | 可写文件 | 工作 |
| --- | --- | --- |
| A 实施子代理 | backend/app/services/evaluation.py | 增加共用公开配置键常量及 live 配置缺项 helper，保留现有算法与行为 |
| A 实施子代理 | backend/scripts/evaluate_rag.py | 仅将既有 CONFIG_KEYS 接到共用常量，不重构或改变评测运行逻辑 |
| A 实施子代理 | backend/scripts/preflight_evaluation.py | 独立纯本地预检 CLI；只 stdout，无输出覆盖问题 |
| A 实施子代理 | backend/tests/test_evaluation_preflight.py | 有意义的成功、阻断、错误、脱敏与零外部副作用回归 |
| A 实施子代理 | backend/evaluation/templates/business-baseline-20.template.jsonl | 20题空白采集结构 |
| A 实施子代理 | backend/evaluation/templates/README.md、backend/evaluation/README.md | CLI及模板用法、状态与结论边界 |
| 主代理 | 本任务卡、ACCEPTANCE-EXECUTION-PLAN.md | 计划、实际主审和执行卡记录 |

除此之外只读；需要新增其他可写文件时报告，不扩大范围。

## 验证与主审

在 backend 使用现有 .venv Python，设置 RAG_DISABLE_DOTENV=1、LOG_STDOUT_ONLY=1；不安装依赖，不使用任何业务.env/数据库或模型：

1. `python -X utf8 -m unittest tests.test_evaluation_preflight -v`。
2. `python -X utf8 -m unittest tests.test_evaluation tests.test_evaluation_baseline tests.test_evaluation_identity_v2 tests.test_evaluation_ranking tests.test_evaluation_reviews tests.test_feedback_regression_evaluation -v`。最后一项是实际存在的纯本地单条反馈协议/CLI预检回归，无DB/网络；用于确认共享配置常量后旧入口保持兼容。
3. 实施者用临时纯合成输入直接运行 CLI 的 ready/blocked/invalid 路径；上限边界、条件配置、空/缺语料、缺项并列、未标注/占位及20题模板必须验证。
4. 无副作用测试对 HTTP 客户端及 socket 创建设置失败陷阱；用独立子进程验证没有加载应用配置/数据库/模型客户端模块、没有读取令牌和.env。不存在为了实现预检而运行应用服务器的需要。
5. 主代理读完整diff确认文件所有权和旧入口兼容，亲自跑上述相关测试及真实 CLI 命令。用临时合成语料独立比较 preflight 摘要与现有 build_identity；修改输入必须改变相应摘要。
6. 用已安装的Python 3.12.10独立运行同一CLI成功/失败路径，核对与本地.venv Python 3.13.15一致；不安装另一套依赖。
7. `git diff --check`；既无前端也无API变化，不重启应用或重复旧浏览器验收。
8. 参数错误（例如不支持的参数或非法上限）也返回固定JSON/退出2，不回显原参数；`--help`保持常规帮助。合法JSON里的畸形Unicode/过深结构和结构类型错误稳定拒绝，不能Traceback；仅在新增入口保护，不扩大修改旧评测协议。

## 风险和回滚

- 输入标注和公开配置真实性由资料/部署所有者核对；工具只校验声明与结构，不能自动证明真实业务标准。输出固定保留未执行/未验收标记。
- 准备工具不识别所有可能的自然语言占位写法；复用既有明确占位规则，人工核验不可省略。
- 本地语料指纹仅代表读取时的快照；之后变更必须重跑，本地预检无远端证明力。
- 不修改既有配置比较、评测/人审输出协议、数据库/API/前端/依赖。出问题只回退本任务限定diff，保留原有与他人改动。
- 实施子代理不commit/push。主代理完成实际审查后按当前交付授权处理后续交付；不得把代码交付授权扩成真实模型/业务库/生产操作。

## 当前阶段

2026-11-14实施冻结后主代理审查并实际验收通过。首轮197项之前的196项回归通过，但实际CLI复现HTTP403无stratum仍ready，确认实现错误豁免了分层要求；返回同一实施者修复并补回归。模板中“勿填写真实标准”引导改为“请在仓库外填写经人工核验的问题与标准”，保留所有占位和未核验字段。

最终7模块197项全部通过（1.827秒、无skip），其中新增预检24项；6类既有评测/身份/排序/人审/反馈协议173项。主代理以实际CLI驱动Python3.13.15与已安装Python3.12.10各27场景，共54次通过；每次记录JSON/退出码及访问陷阱结果。case/语料/config摘要与预先独立计算的旧算法值一致；完整20题模板blocked5、unreviewed=20；上限外第3题未核验同时报告review_incomplete_case:3和case_limit_exceeded，证明未切片。网络、敏感配置/DB/模型模块、.env、凭据环境变量及文件写入审计均零事件，输出无合成敏感哨兵、原值或Traceback。

实际语料symlink检查在本机可执行且通过；没有把平台skip当成功。无前端/API改动，本轮不重复旧应用浏览器验收。git diff --check通过；evaluate_rag.py提示既有CRLF将规范成LF，不涉及内容行为变化。改动仅任务卡范围的7个实施文件与主代理两份计划；无业务.env/业务DB/真实模型/依赖安装。

仓库外证据目录ragkb-business-preflight-20261114-6c84包含main-review-tests-final.log、cli-acceptance.json、54组独立stdout/stderr、expected-fingerprints.json、review-round1.md与验收驱动。首轮驱动文件名处理错误导致路径缩短/覆盖，已修仓库外日志命名并完整重跑；保留首轮记录，不将其计为产品失败。代码正式交付及准确云端结果随后另行记录，不在本阶段提前声明。
