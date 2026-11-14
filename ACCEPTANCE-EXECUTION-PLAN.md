# 业务质量与上线验收执行卡

本文件提供可执行步骤和记录口径；不声明真实业务、生产或容量验收通过。2026-11-13制定，按RELIABILITY-IMPLEMENTATION-PLAN.md在代码可靠性修复后执行。所有真实资料、令牌、证据和报告存仓库外，不能把敏感问题集提交到公开仓库。

## 1. 第一批业务质量基线

**当前状态：待业务输入。** 仓库已有合成问题、严格标注模板与核验工具；没有本轮获准使用的真实问题、语料快照、模型调用预算或目标实例。2026-11-13主代理通过隔离Python模块入口离线执行模板加`--require-reviewed`，在读取响应前拒绝两条`placeholder_case`，退出2且未生成报告；原始输出在仓库外ragkb-rel-business-strict-utf8-20261113-8d42.log。这只验证严格入口，没有真实问题或业务答案验收。

交付责任：业务资料所有者确认可用资料与时效；标注者给出标准要点/来源；核验者阅读本轮答案与引用。关键问题建议独立复核。工具执行者固定配置与身份，不代替业务判断。

首批建议20题（样本规模建议，不代表统计充分）：事实6、跨文档4、流程时效4、无答案4、澄清2；包含版本、编号、单位和过期口径。不得用模型自行生成的答案当标准答案。backend/evaluation/templates/business-questions.template.jsonl保留2题字段示例；新增business-baseline-20.template.jsonl提供20题空白采集结构，全部未核验且带占位符，须复制到仓库外由资料所有者标注。填写expected_outcome、expected_sources、必要/禁止要点、stratum、核验人代号与日期；unknowns全部解决后才reviewed=true。仅在相关来源完整标注、范围固定时source_labels_complete=true。澄清使用expected_outcome=clarify，stratum必须另选已登记代号；两个澄清槽位采用process_sla，因此该分层合计6题。

2026-11-14按BUSINESS-BASELINE-PREFLIGHT-PLAN.md完成独立离线输入预检。先在backend执行：`python -X utf8 -m scripts.preflight_evaluation --dataset PRIVATE_DIR/cases.jsonl --documents-dir PRIVATE_DIR/documents --config-file PRIVATE_DIR/config.json --index-manifest PRIVATE_DIR/index.json --max-cases 20`。PRIVATE_DIR必须替换为已核对的仓库外目录；索引清单参数可省略。stdout仅JSON：0=ready（本地声明的输入就绪）、5=blocked（缺准备项）、2=invalid（输入或I/O不合法）。检查完整批次，超过上限阻断而不切片；不提供隐式同级语料或自动示例配置。预检不读取业务.env、数据库、令牌、远端配置，不运行模型、不写报告；输出只含计数、稳定原因与输入摘要。输入就绪与本轮答案人工核验是不同状态；任何预检成功都不能代替资料授权、目标实例/预算确认、远端索引核对或业务质量验收。

主代理实际验收：相关7模块197项全部通过、无skip；Python3.13.15与Python3.12.10各27次真实CLI调用，共54次通过，覆盖就绪、缺项/模板、条件配置、超上限完整检查、HTTP负例分层及非法输入。合成3题的case/语料/config摘要与既有算法独立计算完全一致；子进程网络/敏感模块/.env/令牌/文件写入拦截均无事件。20题原模板保持blocked、unreviewed=20，并逐条返回placeholder_case；这验证准备工具，不计真实业务通过。原始命令和结果保存在仓库外ragkb-business-preflight-20261114-6c84目录；首次主审发现HTTP负例误跳过分层，已返回同一实施者修复并回归通过。

执行步骤（工作目录backend；以下PRIVATE_DIR、API_ORIGIN、KB_UUID均需替换成已核对的实际值，不能直接执行占位命令）：

1. 在仓库外建立受控问题集、完整语料快照、公开配置与索引清单；登记git提交、文档内容摘要/索引代次、时间与可比较范围。PRIVATE_DIR不得落在公开仓库。
2. 离线已有私有响应可先结构验证：`python -m scripts.evaluate_rag --dataset PRIVATE_DIR/cases.jsonl --responses PRIVATE_DIR/responses.jsonl --require-reviewed --documents-dir PRIVATE_DIR/documents --config-file PRIVATE_DIR/config.json --index-manifest PRIVATE_DIR/index.json --output PRIVATE_DIR/offline.json`。没有真实响应不伪造。
3. 在资料及调用预算确认后，令牌只存RAG_EVAL_TOKEN环境变量，以`--live --base-url API_ORIGIN --kb-id KB_UUID`替代`--responses`，首批不超过20题；运行明确会持久化会话/消息并产生模型费用。人工核验所需正文仅在受控目录用`--include-content`显式导出。
4. `python -m scripts.review_evaluation --report PRIVATE_DIR/live.json --create-template PRIVATE_DIR/live.reviews.json`。需要将报告已有正文复制到模板时显式`--template-include-content`；不手工构造run/identity/result指纹。
5. 逐条核对answer_correct、citation_supported、outcome_correct；未核验为null，不能计为正确。完成后`python -m scripts.review_evaluation --report PRIVATE_DIR/live.json --reviews PRIVATE_DIR/live.reviews.json --output PRIVATE_DIR/live.review-summary.json`。
6. 固定机器断言、来源排序与人工结果分别报告。事先登记各分层可接受门槛，未定义门槛不以默认值冒充业务标准。相同问题/语料/配置/索引身份下才比较基线；变化就重建，不直接比较。
7. 对判错样本按材料缺失、解析、检索排序、引用支持、拒答/澄清、生成错误分因；确认反馈后纳入现有在线回归，每次新结果重新绑定人工核验。

完成材料：样本分层及未核验数、答案/引用/处置核验计数、unknown数、失败案例分类、可比较身份与报告指纹、实际调用与费用覆盖。固定短语通过率不称语义准确率，小样本不宣称企业总体准确率。

## 2. 部署版本、备份与恢复证据

**当前状态：本地合成数据库/uploads恢复已两次通过；真实生产版本、备份和RTO/RPO未知，projects文件归档未覆盖。** 2026-11-13主代理用现有verify_deployment.py执行带模板的dry-run结构核验：network_requests=0、pass=0/fail=0/unknown=14、acceptance=false，按证据未知契约退出3；报告存仓库外ragkb-rel-deployment-dryrun-20261113-8d42.json。2026-11-14实际隔离演练先发现PG初始化socket就绪竞争并保留失败证据，修复为正式TCP探针后，两次全新owner/空卷演练均exit0：25行逐列、4文件SHA256及篡改负向检测通过，完整脚本18.015/20.519秒；主代理另行复核数据与文件，按精确归属清理全部演练Docker资源。结果仅为local-synthetic，未填写production pass或把脚本耗时当生产RTO。不对未获授权的业务DATA_DIR运行restore/backup命令。

1. 版本负责人在目标环境记录部署commit/image digest、环境名、部署时间、操作者和UTC时间；确保与问题集/监控证据属于同一版本。
2. 在隔离演练中使用`./deploy/tests/restore-drill.sh --isolated --keep`，完成后先读owner报告，按脚本精确owner清理；不得在有冲突时批量删Docker资源。当前脚本只覆盖合成PG数据库和uploads，保留恢复前后行内容、uploads SHA-256及完整脚本实测耗时；projects文件归档、真实备份数据窗口与production RTO/RPO仍需另行覆盖，不能填成production pass。
3. 真实恢复演练另由部署所有者书面批准维护窗口、目标备份、回退责任和数据写入冻结。演练前核对三份归档与清单、同一stamp、恢复目标与业务影响。记录开始/完成UTC时间、恢复点、SQL/文件校验、RTO/RPO和人工复核。失败时停止并按既定应急方案处置，不自动重试破坏状态。
4. 将`deploy/acceptance/deployment-acceptance.template.json`复制到仓库外，用脚本检查schema；version_identity和backup_restore_rto_rpo需production原始证据、人工核实后才能声明pass。其余缺证保持unknown。

完成标准：恢复数据库、上传文件和项目存储内容一致；量化RTO/RPO满足组织目标；材料属于同版本/同环境且时序合理；独立复核完成。隔离演练只记local-synthetic。

## 3. 实际告警投递与ACL证据

**当前状态：CI验证的是Prometheus/Alertmanager到本地fixture的合成投递；无真实接收器回执或真实成员ACL材料。**

1. 监控负责人和通知接收方确认接收器类型、告警联系人、触发及恢复规则、测试时间与去重ID；secret只存仓库外权限受控文件，不复制到报告/工单/仓库。
2. 在获准的staging/production通过实际接收器触发一次warning/critical和恢复事件，保存脱敏后的事件ID、接收回执、状态、UTC时间和重试情况；不要用ready、规则测试、HTTP 200替代回执。
3. 由资源所有者选一组真实授权成员/角色，在同一知识库/资源上验证允许和拒绝的最小读写动作，核对预期HTTP状态及审计记录；不扩大权限、不留存个人信息正文。
4. 分别把真实firing/resolved/receipt及allow/deny证据写到仓库外模板；同一环境与部署版本由第二人核对后，才更新验收状态。

完成标准：接收端确实收到触发与恢复，失败/重试可解释；授权边界的允许和拒绝与角色设计一致；日志和证据不含秘密或不必要个人数据。

## 4. 容量目标与持续运行观察

**当前状态：工具仅支持单实例、小样本，企业目标规模和24/72h记录未知。** 2026-11-13主代理以仓库benchmark.example.json执行dry-run：plan-valid、actual_network_requests=0、measured=false、capacity_acceptance=false，exit0；仓库外报告ragkb-rel-capacity-dryrun-20261113-8d42.json。只校验配置与预算，没有执行负载。`planned_scale`只是声明，不代表创建了对应用户或文档。工具每次live会真实改变允许的测试会话/消息，故障时费用可能无法撤销。

1. 业务/平台负责人先提供文档量、用户数、峰值在线、并发、SLA（TTFT、完成耗时、错误率）、费用预算、模型/embedding/rerank服务配额与数据库连接上限；没有这些数值不启动真实负载。
2. 先在本地模型替身上验证链路和观测记录；该结果只做功能基线。真实模型小批次必须用专用隔离部署、专用用户/知识库、明确费用预算和环境变量令牌，人工确认目标origin及写入清理方案。
3. 当前`benchmark_rag.py`硬上限：并发8、40问答、单请求30s、每次总运行120s、HTTP300次；它不能做10/30/50/100的越界压力测试，也不覆盖摘要/项目embedding。不得修改常量冒充授权容量测试。
4. 超出硬上限的容量验收需单独设计受控压测器/环境并通过组织审批，不对共享/生产服务直接增加负载。设定停止条件（错误率、费用、连接池/队列、安全事件）并有现场负责人。
5. 24h和72h soak在独立目标环境连续采集四角色心跳、pending/processing/stale/最老等待、错误码、API/DB资源、模型可用性、告警触发/恢复及采集缺口。任何停采区间按事先规则处理，不拼接不连续窗口伪称完整。

完成标准：有获批的容量目标、范围/版本/模型/数据身份、费用与实际token覆盖、分位数/错误率及资源曲线；达到目标且异常经过人工判定。soak各自需连续真实窗口和完整证据，短时CI不能抵扣。

## 5. 执行边界与阶段状态

以上四节均需要业务或部署所有者输入。当前只读入库回归不得接触业务数据库/真实资料，不应通过读取或沿用`.env`获得目标、身份或凭据。人工标注、生产恢复、真实接收器调用和真实模型负载在范围、凭据与费用明确且得到书面授权后单独执行；不得因“按顺序开干”把缺失环境默认为同意外部副作用。
