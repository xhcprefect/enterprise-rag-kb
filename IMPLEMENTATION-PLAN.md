# 后续建设实施计划

用户已授权按建议顺序逐项实施。主代理负责规划、审查和真实验收；general-purpose 子代理负责实现。无需阶段性确认，不提交或推送，不修改业务 .env，不调用收费模型，不连接或清理业务数据库。现有 frontend/src/styles/main.css 的本地修改必须保留。

## 交付顺序与可观察验收

1. **文本编码修复**：TXT/MD/CSV 在 64 KiB ASCII 前缀后出现 GBK/GB18030 中文时仍保留原文；UTF-8/BOM/采样截断兼容；不可支持编码明确报错，不静默替换正文；全文件验证流式有界。
2. **worker 运维页**：管理员可查看 ingest/summary/project 的启用状态、独立心跳、任务队列、最老等待和最近失败；未启用、未启动、运行、心跳失效及查询失败可区分；普通用户访问被拒绝；已有运维字段兼容；中英文、窄屏和真实成功/失败路径验收。
3. **知识库成员权限**：管理员管理每库成员的读取/维护权限；普通用户可使用被授权的私有上传库与项目库；所有列表、检索、原文、历史与生成发布统一校验；移除授权生效；维护者仅能管理授权库文档，不能获得全站管理员权限；老库访问语义保持兼容。
4. **评测增强**：增加正确来源排名与前 K 相关率，数据缺失/未标注保持未知；对排序退化有回归断言；沿用已有人工语义核验，提供可操作的真实业务基线流程，不伪造真实数据或准确率。
5. **PDF 表格解析**：保留表头、行列与物理页码，表格文本不重复进入正文；多页和混合正文用例；识别失败可控回退；扫描 PDF 给出清晰 OCR 提示。OCR 是否接入以本机可用且已授权的本地能力为准，不引入未经授权的外部服务。
6. **反馈回归在线化**：管理员可在页面发起有界后台重跑、查看状态及新旧结果并人工确认；保留导出导入；支持批量和限定文档范围；项目会话按固定快照校验后执行；幂等、取消、权限与费用未知语义保留；仅用确定性本地模型替身验收。

## 任务卡与文件所有权

每阶段实施前补充精确文件清单与接口。阶段串行交付，独立前后端子任务只有在接口冻结、文件互不重叠时并行。

| 阶段 | 文件所有权 | 依赖 |
| --- | --- | --- |
| E 编码 | backend/app/services/parser.py、structured_documents.py、user_import.py；编码/结构化测试 | 无 |
| O 运维后端 | 新 worker 模型/服务/schema；worker.py、models/__init__.py、operations.py、运维和 worker 测试；监控规则 | 与 E 最后边界修复无文件或接口依赖，可接续启动；O 验收前 E 须通过 |
| O 运维前端 | 新 OperationsView/api/i18n/tests/e2e；router/routes.ts、导航翻译（与后端文件互斥） | O 接口冻结后与后端并行 |
| A 成员 | 新成员模型/权限服务/schema；相关 API/服务/前端成员管理/测试 | O 验收后 |
| Q 评测 | evaluation.py、评测测试、示例/说明 | A 验收后 |
| P PDF | structured_documents.py、PDF 结构/来源测试与说明 | Q 验收后 |
| R 在线回归 | 回归模型/schema/service/API、worker 与运维扩展、反馈组件/api/i18n/tests/e2e | P 验收后 |

## 验证与环境

- 每阶段主代理检查实际 git diff，亲自运行受影响模块测试。
- 后端集成只使用回环、_test 后缀、带归属标记的可清空数据库；按项目 test gate 准备。
- 最终后端全量 unittest；前端 typecheck、npm test、build；真实浏览器走新功能成功与关键失败路径并查看截图。
- 本地 API、四个独立 worker（R加入regression角色）和前端使用脱离工具生命周期的进程；优先复用项目一次性 E2E harness 的独占数据库和模型替身。停止/清理只针对能核实归属的本轮资源。
- 新增前端测试自动纳入现有通配符 test 脚本（已核实 package.json 当前是 tests/*.test.ts）。

## 风险与回滚

- 编码探测的完整验证会增加一次流式读取，优先保证文本正确性和有界内存。
- 新表由 create_all 幂等创建；迁移只新增，保持老字段/接口。权限撤销必须覆盖历史和生成发布。
- 心跳与观测失败不能使真实任务失败；未知不能伪装为健康或零。
- 新的外部/付费调用及真实生产操作不在本轮执行范围；评测结论以实际证据区分。
- 回滚代码需保留已有文档治理和权限读取门禁；必要时停写后回退，避免授权数据重新暴露。

## 当前进度

- 最终状态（2026-11-11）：E/O/A/Q/P/R已完成本轮本地合成环境主验。R1b传递来源窄修通过136项回归及独立PG/API驱动；R2b主代理执行/worker23项通过，后端完整discover共2103项（2100通过、3项Windows平台跳过），测试期间源码稳定。R3最终前端387项全通过、生产build及E2E类型检查通过；真实API/PostgreSQL/四个独立worker/生产前端/回环模型替身的Edge最终浏览器10例全部通过（1.9m、零skip），重点覆盖queued/running取消、取消回执丢失禁用旧CAS、切换面板隔离、原UUID只读恢复、来源禁用409隐藏与恢复200。主代理另实际操作共享浏览器的人审、运行中取消及反馈切换，并读取服务端终态确认迟到结果未发布。真实业务准确率、OCR、Linux信号及24/72h soak等既有未验证限制继续保留，不把本地合成验收当生产签收。下列早期状态属历史记录，以本条及R节最终记录为准。

- 规划与约束核对完成；已复现 ASCII 前缀导致中文编码误判。
- E 编码已通过主审：完整文件流式严格校验、内存 CSV 显式真实 EOF 校验，UTF-8/BOM/GBK/GB18030 兼容；主代理首轮 76 项通过，边界修复后 24 项通过（含新 GBK 残尾用例，无跳过）。
- O 运维后端已通过主审。主代理在62603专用库亲跑 test_worker_status/test_operations/test_worker_roles/test_worker_shutdown：Ran 71 tests in 11.951s，OK (skipped=2)，两项均为Windows不能验证的真实POSIX SIGTERM子进程用例。首次提交失败恢复、stop只set+有界join、日志故障隔离、测试库owner门禁、纯单测不启动真实心跳、ingest无租约统计均已修复并复核。告警改为真实任务数量在左侧、完整标签匹配同采集目标；本机无promtool/Prometheus镜像，未执行真实promtool解析（保留为明确验证限制，不下载镜像）。主代理另以现有rag-e2e-backend:local只读挂载当前源码、network none探测Linux环境：Python3.12.10可用，但缺langchain_core，不能运行这两项信号测试；未安装依赖/下载镜像，探测容器已--rm退出，两项继续明确记为未验证。
- O 前端 aec95382269f5e0bd 产品代码、路由与纯 helper 已由主代理审查；主代理亲跑 typecheck、生产 build 和前端全量 360 项测试通过（0失败/跳过）。真实 API E2E 首次2项通过；新增主题截图交互曾因 menuitemradio 定位错误1失败/1通过，退回原代理改为实际 menuitem 后主代理最终复跑2项通过（5.6s）。已在共享浏览器手动检查真实刷新、菜单往返、请求阻断后0卡和可重试提示、恢复后3卡、英文与深色模式；已创建合成普通用户，确认无运维菜单、直接地址回到/chat、真实接口403。共享浏览器截图CDP超时，改用本机Edge运行仓库既有Playwright用例，主代理已Read并查看中文桌面/390px、失败/恢复、英文深色390px截图；不以通过摘要代替视觉检查。当前独占验收环境 owner=ops-20261110-k6d1，web=127.0.0.1:62664，三独立worker均真实running/alive>=1/有心跳，模型仅回环替身。随后用真实心跳表锁触发观测超时，确认HTTP200/degraded=true且三角色状态/计数为未知；发现心跳和最近失败在降级时误报“未记录/暂无失败”，退回原代理修复后主代理重新build并加载OperationsView-CdiQ709G.js，真实故障复验所有观测值显示Unknown、解锁后恢复Running。最新生产构建最终E2E再次2项通过（5.4s），typecheck:e2e通过。O整体通过，进入A；主代理未直接编写产品实现代码。
- 当前分支 feat/knowledge-quality-and-operations；测试容器 ragkb-next-tests-20261110-c4f2（标签 io.ragkb.task=next-20261110-c4f2），当前回环端口59010，数据库ragkb_next_test，owner owned-test:next-20261110-c4f2。canonical专库归主代理，实施者只可使用R节正式独占分配的其他测试库，不得并发清理；旧62603端口已失效。
- O独占E2E环境已通过harness down按owner/nonce完成清理，浏览器tab已关闭；docker检查确认O容器消失，原charming_mcnulty/redis及后续专用测试容器仍running；截图与日志保留在本轮owned临时目录。
- A4首轮报告后，主代理亲跑typecheck、npm test、build、typecheck:e2e全通过：377项、0失败/跳过，生产入口199.70kB；日志C:/Users/Administrator/AppData/Local/Temp/ragkb-a4-main-review-20261110-k6d1.log。已实际复审旧删除确认/未知写入revision失效/暂存complete与单文档404新鲜能力复核修复。再发现true→unknown→known false不一定stopMaintenance，退回原代理后已修复：任何新鲜known documents=false均停止维护，sawManageCapability仅用于区分初始只读/撤权文案。主代理已实际复读控制流并复跑typecheck及成员/能力/上传相关22项（0失败/跳过）；unknown→true的有界complete回执恢复保留。A4产品代码审查通过并冻结，真实浏览器E2E仍待A2/A3，不能把类型/构建通过当浏览器通过。同一A4代理已仅在members.spec.ts补两条真实时序验收（现共5例）：延迟真实complete并阻断KB GET，分别验证unknown→true回执恢复与unknown→known false停止队列；不mock业务成功、不扩脚手架。主代理亲跑typecheck:e2e通过，审读后退回两处测试可靠性问题并已修复：uniqueNonce现给时间/序号/随机尾预留长度，同harness重跑不再截掉随机性；首个complete预先监听requestfinished/requestfailed，移除400/800ms固定等待，放行后有界等待真实终态再验状态。主代理已实际复读并再次亲跑typecheck:e2e通过；A4产物（含5个E2E用例）冻结。真实E2E仍未运行，后续主代理在owned环境执行并审图。后续R前端需要留意入口200kB上限，不应靠持续压缩文案掩盖全量消息装入入口的问题；在R所有权内选择必要的消息按需加载。
- A1（aea559e4871c970c3）已交接基础产物。主代理亲跑test_kb_access/test_kb_members/test_kb_members_migration/test_governance_deletion_locks：58项、16.624s、OK，无跳过；实际检查schema/helper/CRUD/CAS/用户删除diff。日志C:/Users/Administrator/AppData/Local/Temp/ragkb-a1-main-review-20261110-k6d1.log，末尾仍有3条连接ResourceWarning（不计为失败，也不隐去）。新并发删除测试原先未执行删除，退回后现已用真实delete_user竞争补齐：删除侧按UUID预锁admin+target，成员写真实等待，删除提交后PUT为404、成员不重建、KB revision从1到2；list_members前置注释也已更正。A1再次交接后主代理亲跑deletion-locks：18项中该真实竞争通过，但整模块2失败+2错误（14通过）；两项上传用户删除竞态在新阶段一门禁401后遗留tmp，两项纯锁序fixture缺user.id，已交A3处理，不能记为全绿。日志C:/Users/Administrator/AppData/Local/Temp/ragkb-a1-contention-main-20261110-k6d1.log。另发现A3清理SELECT后unlink未与同session完成串行，有token0失败清理晚于token1成功收编的TOCTOU，已一并退回A3加真实PG覆盖。A1产品与测试冻结，canonical库归主代理；A1自建临时worktree已清理。已将A1报告的test_database scope撤权响应404/原契约403交A2保持兼容，不通过宽化A3测试掩盖。A4产品代码审查通过并冻结，等待A2/A3集成及真实浏览器。公共helper早前16项真实验收已通过且签名冻结；A整体尚未完成。

- A2产品diff已由主代理逐项审读，亲跑16模块363项、87.457s、OK，无跳过。日志C:/Users/Administrator/AppData/Local/Temp/ragkb-a2-main-review-20261110-k6d1.log，末尾有本地11434探活socket ResourceWarning。SQL可见性在total/LIMIT前、本人会话撤权保留403、项目ACL先于revoke、历史/重放/发布接入均已确认。补充的真实SSE用例现已交接并由主代理复读和亲跑：两个新增模块30项、16.604s、OK，无跳过；日志C:/Users/Administrator/AppData/Local/Temp/ragkb-a2-stream-main-20261110-k6d1.log（末尾仍有3条数据库连接与1条本地探活socket ResourceWarning）。模型async generator通过Event进入等待，真实PG提交成员撤权后，独立watch_lease发出lease_lost并取消generator；断言无done、无成功assistant、turn为cancelled。请求Session在消费流前关闭，不持DB连接等模型。publication末项说明已改为仅覆盖提交后的新鲜读取。A2读取/发布部分主审通过，产品与测试均冻结，acl_read库交回主代理。主代理有限查找未见相关load_rag_history/complete_turn等直接以SessionLocal()临时调用的剩余模式，不扩散清理未涉文件。A3已首次交接，主代理亲跑维护/上传/删除锁序合计264项、119.460s、OK，无跳过，末尾有3条连接ResourceWarning；此前2失败+2错误均已恢复。主代理实际核对test_database:1067仍为既有403断言，未以改断言掩盖权限契约。失败清理已取同会话锁把引用查询与unlink串行，活账号可恢复上传保留，阶段一仅清理同KB且created_by为NULL的无归属会话。审读另发现_write_chunk释放根锁后等待session/慢写期间撤权仍能200，已退回原A3补写盘后按User→KB→session最终新鲜校验，禁止根锁跨慢IO或session→User逆序；并要求真实PG锁等待与真正进入阶段二completed再鉴权分支的用例。A3原acl_maint库重新独占分配该代理；主代理在canonical库运行整套后端基线。前端最新主验收typecheck、377项全量测试（0fail/skip）、生产build（入口199.70kB）、E2E类型全部通过；真实浏览器将使用A3最终冻结产品版本。完整后端基线已跑1932项、410.234s：1fail、3skip。唯一失败test_kb_member_reading会话分页夹具将public_conv与105条填充统一时间戳，按UUID排序时可能随机落在前100之外；已仅交回A2该测试文件，产品冻结，要求明确三条关注会话的时间戳并真实PG连跑20次及30项模块回归。3skip为Windows上的目录符号链接与两项真实POSIX SIGTERM。浏览器已在owned m2a7环境实际添加reader、阻断成员GET、重开显示版本未知/无旧成员、恢复重试并改为maintainer；主代理已查看共享浏览器失败与恢复截图，控制台仅有主动阻断的网络错误。A3三阶段分片最终产品再次落盘：根门禁释放→仅session锁写/读校验并释放→User/KB/session最终权限及原token复核；取消根锁内整片hash，产品冻结、测试仍在收口。m2a7环境已按owner/nonce清理并关tab，已启动m2b8新环境确保最终API加载新产品；待5项真实E2E、审图、维护者/reader手动完整路径与新后端回归。A整体仍未通过。

- A最终整合验收进行中：A3已交最终三阶段产品与4个真实PG新增用例（模块18→22），代理报告268项OK，主代理已审读最终实现与4用例，正在canonical库亲跑完整回归以核验。A2仅改分页夹具三档时间戳，主代理已读实际修改，代理报告单例20/20及30项OK；产品未改。m2b8最终API环境5条members E2E主代理亲跑全部通过（27.7s），10张截图均已Read实际查看，含390px英文深色、真实409、能力未知/恢复/撤权。共享浏览器进一步通过真实文件输入触发合成Markdown分片上传、worker ready、维护者成员API403、真实CAS降为reader、原文200/有效上传403、真实移除成员后刷新404/无KB/0旧文档/无上传入口。扩展不能读本地文件，采用页面内生成File并触发原生input change，不调整扩展权限。截图发现只读/撤权空表仍提示点击上传，已仅交回A4 DocumentsView和documents中英文文案作条件空态修正，不改权限状态机；最终后端已由主代理亲跑完整discover：Ran 1936 tests in 398.905s，OK(skipped=3)，即1933通过、3个Windows平台跳过，无失败/错误；末尾仍有本地11434探活socket ResourceWarning。日志C:/Users/Administrator/AppData/Local/Temp/ragkb-a-full-final-20261110-k6d1.log。另共享浏览器已确认撤权后已知文档source也404。待新build后主代理重跑5例并复看空态。

- A最终通过：A4空态修正复用已有中英文能力文案，最终只改DocumentsView的computed与empty绑定，不新增翻译/改变状态机。主代理亲跑前端全量377项（0失败/跳过）、build内vue-tsc及包体积/懒加载门禁、typecheck:e2e均通过，入口199.70kB；日志C:/Users/Administrator/AppData/Local/Temp/ragkb-a-frontend-final-20261110-m2b8.log。最终生产构建5条members E2E再次全部通过（27.8s），日志C:/Users/Administrator/AppData/Local/Temp/ragkb-members-e2e-final-20261110-m2b8.log；主代理重看本次只读、撤权、能力未知截图。共享浏览器确认已加载index-gUDB1Nhi.js/DocumentsView-BuTvkxug.js，英文深色空表显示Read-only; upload and maintenance disabled.且无上传入口；阻断KB GET后显示权限无法确认，恢复真实200后仍为只读，0行/0上传按钮。控制台仅含预期403/404与主动阻断错误，无额外应用异常；补拍未知态共享截图曾CDP超时，不计为成功截图，已有最新E2E故障图已实际查看。十张最终E2E截图已独立保存在C:/Users/Administrator/AppData/Local/Temp/ragkb-members-evidence-20261110-m2b8-final，桌面实际1280×720、窄屏390px，不按文件名误称1440。tab867074271已关闭，m2b8按harness owner/nonce执行down并收到orchestrator完成清理；长期62603测试资源保留。A整体正式通过，1936项完整后端结果为Q改动前基线；Q已按上方阶段衔接约束开始，仍须独立主审与亲验。

## O 运维接口冻结契约

新增 GET /api/v1/operations/workers（仅管理员，统一 envelope、Cache-Control: no-store）。现有 /summary、/metrics 和 /health 字段兼容；新增固定 role 的 Prometheus 指标。

返回 data：

- generated_at: UTC ISO 时间；stale_after_seconds: 45；degraded: boolean。
- roles: 固定 ingest、summary、project 顺序。
- 每项：role；enabled（ingest=true，summary=CHAT_MEMORY_ENABLED，project=bool(PROJECT_ALLOWED_ROOTS)）；state（disabled/not_started/running/stale/stopped/unknown）；alive_count: int|null；last_heartbeat_at: ISO|null；pending/processing/stale_tasks: int|null；oldest_pending_age_seconds: number|null；last_failure: {updated_at: ISO, code: string}|null。
- 数据查询失败：degraded=true，启用角色 state=unknown、计数=null；不得补零。disabled 只说明当前 API 配置未启用，仍可显示确实观测到的心跳。
- 最近失败 code 只用稳定白名单/固定通用原因，不含原始异常、路径、文档名、用户信息。updated_at 表示失败记录的更新时间，避免冒充精确完成时间。
- 新 WorkerHeartbeat 表：每实例 UUID、role、started_at、heartbeat_at、stopped_at；独立线程短事务每 10 秒续期，覆盖空闲/长任务，写入失败不影响任务，退出有界等待并尽力标记 stopped。无任务时不能用任务心跳推断进程存活。
- 角色任务统计直接来自 Document / SummaryJob / ProjectSnapshot，尊重各自 pending/processing 与租约过期语义；不复制任务正文。
- 运维页是可访问的状态表/详情列表，无新图表或配色；沿用主题 token、状态图标+文字，中英文，窄屏可读。失败时保留可重试错误提示并隐藏/标记旧数据，迟到请求不得覆盖较新状态。

## A 成员权限设计（O 验收后实施）

- 新 KnowledgeBaseMember（kb_id/user_id 复合唯一与 CASCADE FK，role=reader/maintainer）及知识库 membership_revision 默认 0 的幂等增量列。老库保持管理员/公开/所有者读取规则；显式成员补充私有读取权。维护者管理授权上传库文档，不获得全站权限；项目源路径授权/同步配置/撤销继续由管理员管理，项目成员可读搜索、固定快照问答和历史原文。
- GET /knowledge-bases/{kb_id}/members?page=1&page_size=20：仅管理员，返回 {revision,total,items:[{user_id,username,display_name,is_active,role}]}，有界分页。
- PUT /knowledge-bases/{kb_id}/members/{user_id}：仅管理员，body {role,expected_revision}；DELETE 同路径 query expected_revision。两者成功返回 {revision,member}（PUT 的 member 与列表元素同形，DELETE 为 null）。变更 CAS，冲突409；无变化幂等且仍核对 expected_revision，不把 disabled 用户作为新授权目标。PUT 目标用户不存在为404，DELETE 无成员记录可按无变化处理。写事务在 User（涉及用户按UUID稳定排序）→KB→成员的顺序取锁，并复核认证时 token_version 快照；审计只存稳定ID、角色、revision，不存正文。
- 知识库列表每项新增 membership_revision、can_manage_documents、can_manage_members；不以客户端角色或文档 maintainer 自由文本决定授权。
- 抽出统一读取/维护权限服务：提供SQL可见谓词和当前数据库判定。已定位内联ACL：knowledge API、chat API、chat_turns、answer_sources、answer_history、answer_feedback、project_sources、project_retrieval（含排名前 SQL 谓词）、project_source_documents；feedback_regression_evaluation 内管理员校验保留或统一。
- 特别覆盖：撤权后会话详情与请求重放不能返回历史答案/引用；会话列表不泄露已无权的私有标题；新检索、模型等待后复核和最终发布均生效。权限写入与现有 KB 根锁串行，不能缓存成员授权跨请求。
- 删除用户也会通过 FK CASCADE 移除成员记录：users.delete_user 需在既有 UUID 稳定 User 锁后锁定“所属 KB ∪ 成员 KB”，只为真正存在成员记录的 KB 递增 membership_revision，并保留后续 owned_kbs 的原资源边界；避免成员列表已变但 revision 不变。此处加入 A 后端所有权与回归覆盖。
- 上传维护涉及小文件/分片 create/status/chunk/complete/abort、reindex/cancel/delete、quality/chunks/comparison/governance/supersede/reenable。分片慢写不持 User/KB 锁，最终提交与重放必须再次校验当前权限和原 token_version；保留上传会话仅创建者访问语义。maintainer 不可创建/删除KB、管理成员/用户/价格或读全站审计。
- 前端成员管理由知识库卡片管理员菜单进入；中英文、分页、用户搜索（沿用仅管理员用户列表）、角色设置/移除、冲突刷新与错误提示；DocumentsView 按服务端 can_manage_documents 控制维护入口，未知时收敛权限，后端独立校验。
- 验收用本轮专用库与本地模型替身，覆盖两个私有库隔离、reader/maintainer区别、项目源读、撤权（含生成期间）、HTTP鉴权/CAS/禁用/旧令牌与迁移幂等；真实浏览器添加/修改/移除和维护成功/拒绝路径，主代理看截图。

### A 分步所有权与接入边界

O 主审通过后先交付 A1 基础；A4 前端按已冻结的 HTTP 契约可与 A1 并行。A2/A3 须拿到 A1 已落盘并审查的公共 helper 后并行接入；A 全部验收后才进入 Q。

- A1 基础与成员管理：新 models/knowledge_base_member.py、services/kb_access.py、schemas/kb_members.py、services/kb_members.py、api/v1/kb_members.py；models/document.py（只增 revision）、models/__init__.py、db/migrations.py、api/v1/__init__.py（路由注册）、api/v1/users.py（删除级联 revision）；新成员/ACL/迁移测试及 test_governance_deletion_locks.py。此时不改 knowledge.py、chat.py、上传服务和前端。
- A1 公共接口冻结为 visible_kb_predicate(user, kb_model=KnowledgeBase)、can_read_kb(db,kb,user)、can_manage_documents(db,kb,user)、lock_current_user(db,user_id,auth_token_version)。前三者必须拒绝 disabled 用户；SQL谓词用相关 EXISTS，不能先 LIMIT 再过滤，不能跨请求缓存成员角色。maintain 仅 admin 或显式 maintainer 且 kind=upload；owner/public 仅保持读取兼容。lock_current_user 取 User 锁并核对原始 token_version，调用方随后按 User→KB 顺序锁并复核具体能力；成员写涉及多个 User 时一次按 UUID 排序锁，不能先锁 admin 后反向锁 target。
- A2 读取接入：api/v1/chat.py；services/chat_turns.py、answer_sources.py、answer_history.py、answer_feedback.py、project_sources.py、project_retrieval.py、project_source_documents.py；相应读取/撤权/生成中权限回归测试。保留会话本人隔离、固定项目快照和原来源复核，不把成员关系当成其他用户私人会话授权。
- A3 文档维护与知识库列表：api/v1/knowledge.py、schemas/knowledge.py、services/upload.py；上传/完成锁/治理/内容去重相关现有测试及新维护权限测试。保持 _lock_governance_admin 严格管理员语义，另建维护根门禁，不把创建/删除KB与检索诊断扩权。列表能力随当前用户计算；小文件去重、分片写入和 complete 重放也复核当前维护权与原 token。
- A4 前端：src/api/index.ts、新 api/kbMembers.ts、components/KnowledgeBaseMembersDialog.vue、utils/kbMembers.ts、utils/kbCapabilities.ts，views/KnowledgeView.vue、DocumentsView.vue；i18n/messages/knowledge.ts、documents.ts；相关 tests 与新 e2e/members.spec.ts。不改既有 main.css、依赖或运维文件。成员请求使用取消/请求序号，切库/关闭/失败不保留旧列表或 revision；维护能力未知即禁用，轮询与队列动作不得继续使用撤销前能力。
- A2已确认：Conversation.kb_id是ON DELETE CASCADE，真实删除上传库会级联会话，不需要为被删库保留“无KB的RAG历史”旁路；普通mode=chat/kb_id=null保持兼容。stream_events有独立权限/租约轮询，check_lease经chat_turns._lock_roots，成员ACL需接入此根校验及最终发布，不能只改检索入口。会话所有者隔离仍独立于KB成员授权。_get_owned_conversation也供memory状态/重置、元数据和范围入口使用，撤权后不得借这些读取路径继续访问；本人取消/清理在途任务的语义单独保留，不能因此放开内容读取。test_conversation_management.py应归A2，test_database.py仅归A3；tests/fake_turns.py如需适配仅归A2，A3不得共同修改。
- A3重点分支：complete第二阶段发现session已completed时不能拿Session锁后反向查User/KB，也不能直接旧权限重放；先释放该锁，再按User→KB→session新鲜验证重放。唯一摘要冲突rollback后根锁已经释放，必须重新门禁再锁session/查canonical，不能session→User逆序或在撤权后返回canonical。小文件dedup与IntegrityError复用分支亦须新鲜门禁。初始token/user_id用不可变原始快照，不从提交后可能刷新的ORM对象反推。
- A1交接冻结后，test_governance_deletion_locks.py中仅DocumentDeletionLockSqlTests的两项fixture/锁序断言适配权已转A3；其余A1测试不得改动。A3在自己的acl_maint库验证该模块。上传阶段一拒绝和迟到清理必须同时保持请求确有所有权与并发canonical保护，不按任意未授权session_id删除目录，不用一次引用查询代替串行保障。
- 数据库测试不并发共享清空夹具：A1 使用现有专用库；已在同一已核实归属的测试容器内创建A2独立库ragkb_acl_read_20261110_k6d1_test（owner owned-test:acl-read-20261110-k6d1）和A3独立库ragkb_acl_maint_20261110_k6d1_test（owner owned-test:acl-maint-20261110-k6d1），均回环62603、_test后缀，经共享gate --prepare-owner得到owner-comment-created；主代理先在A2库验收公共helper16项通过，随后已交接A2（a4974909d132aa439）；A3库已交接A3（a944ba5f4d8b4531c），两者开始分别读取/维护接入。每个代理只可运行自己获分配的库；主代理后续验收等该代理交接后运行。

## Q 评测口径设计（A 验收后实施）

- 在现有候选来源/提供给模型来源/引用来源的身份匹配之上，增加首个已标注正来源的倒数排名（MRR）与按 case.top_k 的相关率；序列次序直接来自实际 trace，不重新按分数排序。
- MRR 只回答已知正确来源出现多早；没有 expected_sources 或没有对应 trace 时保持 null。空的真实检索序列有明确观测，存在正标注时记 0。
- 前 K 相关率只在 case 显式声明来源相关性标注完整时计算；未声明则未命中来源仍是未标注，不能当作不相关。该声明影响数据集身份；分母口径和返回不足 K 的行为在实现任务卡中固定。
- 新增指标各自报告有效样本数及可计算样本数；报告比较需检验指标覆盖一致，防止漏测难例后均值虚高。版本不兼容/旧报告缺失字段要明确不可比或 unknown，不隐式补零。
- 排名指标纳入阈值注册和可选回归门槛，补充“召回完全相同、排序变差”的确定性测试；保留现有结果/人工审核指纹语义，不把来源身份命中描述为语义支持证据。
- 使用合成 case/response 与既有离线 baseline 验证；完善真实业务抽样、来源/版本标注、人工复核、未知项和基线更新流程。没有获授权的业务样本时不产出真实准确率。
- 主代理已核对现有比较与反馈导入：METRICS_VERSION=1，make_report/compare_reports只为延迟和费用维护覆盖计数；新排名指标必须加入各自有效/可计算case ID cohort的比较，不能只比较数量。feedback_regression_evaluation._recompute_result直接使用当前grade_case、再比machine_sha256；evaluation_reviews.inspect_report也限定当前机器/结果指纹版本。实施Q时需要显式保留既有导出/导入和人工审核语义：结果正文指纹v1不因新增指标无故变化，若新指标进入机器指纹则同时提供声明版本的重算/旧报告兼容，不能直接升版本后悄悄废掉旧回归报告。当前只核对依赖，没有更改产品实现。

### Q 实施前冻结口径

- 新字段 `source_labels_complete` 为严格 bool、缺省 false：只有标注者明确确认 fixed scope 下相关来源已完整列入 `expected_sources` 才允许计算前 K 相关率；显式空列表表示确认没有相关来源。普通 chat / 预期 HTTP 负例不参与排名质量分母。
- 新指标 `candidate_source_mrr`、`supplied_source_mrr` 使用各自 trace 列表原顺序，单 case 值为首个已知正来源的倒数排名，聚合为宏平均；无正来源标注或对应 trace 缺失为 null，有正标注且真实空检索为 0。
- `candidate_source_precision_at_k`、`supplied_source_precision_at_k` 以 case.top_k（缺省6）为 K，固定分母 K、只检查前 K 个实际位置，未返回的位置记未命中；只有完整标注声明才可计算。该口径须在文档和不足K的回归中写清，不把未完整标注的候选当作不相关。
- 每个排名指标记录 eligible case 与可计算 case 的数量、稳定ID cohort摘要；有应测但缺 trace/响应的case时比较为 unknown，相同数量但替换了被测case也不能可比。零 eligible 与未测量要区分，null 不补零；新增指标进入阈值白名单，显式门槛可使“召回不变但排序下降”退出3。
- METRICS_VERSION提升到2；RESULT_FINGERPRINT_VERSION和MACHINE_FINGERPRINT_VERSION保持1，新增排名指标为观测字段，不改变既有机器断言/答案引用的指纹口径。原有人工审核与单条反馈导入继续验证现有绑定，旧指标报告比较明确incomparable；若实现确有理由修改指纹版本须先报告主代理并处理旧导入兼容。
- 主代理补查validate_cases/dataset_identity：现验证器返回原case且不注入默认值，dataset身份覆盖除核验元数据外的原字段。因此“缺省false”应通过get读取，不给既有case自动补source_labels_complete=false；否则会无故改变旧反馈回归case/dataset/binding摘要。新增声明实际进入case时影响身份，未声明旧case及其机器/结果指纹须保持兼容，并以旧case绑定回归覆盖。
- 代码/测试所有权待A通过后交单个Q代理：evaluation.py、相关evaluation/feedback-report测试、scripts/evaluate_rag.py（仅必要适配）、backend/evaluation的示例与说明。新的排序退化合成样例与LF语料基线都必须由真实CLI离线回放生成验证，不手填成功结论。
- Q实施前主代理已留存当前离线CLI基线：python -m scripts.evaluate_rag 对cases.platform/responses.platform和baseline.platform比较，8/8固定断言通过、comparable/no_regression、退出0；报告C:/Users/Administrator/AppData/Local/Temp/ragkb-q-preflight-platform-20261110-k6d1-report.json。另6条基础样例也全部断言通过；曾将其与8条platform基线比较，正确拒为dataset_changed、退出4，这不代表platform基线失效。不调用模型或数据库；这些均为合成数据工具验证。

### Q 任务卡补充（A主体通过后启用，孤立空态文案并行收口）

阶段衔接调整：A权限产品、完整后端1936项（1933通过/3平台跳过）、5条真实E2E、10图审阅和共享浏览器完整上传/只读/撤权路径均已通过；仅余DocumentsView与documents.ts的空态提示小修。该修正不改变权限/上传状态机或后端协议，且与Q文件完全互斥，因此Q可开始独立离线实施，A4仍由主代理在新build后重跑浏览器并复审，最终交付不得遗漏。此处仅放宽孤立文案的等待，不扩大业务范围、依赖安装或外部调用授权。

- 单一Q实施代理，可写 app/services/evaluation.py、scripts/evaluate_rag.py（仅必要适配）、tests/test_evaluation*.py、tests/test_feedback_regression_evaluation.py（旧绑定兼容用例）、新 tests/test_evaluation_ranking.py、evaluation/ 内说明/模板/独立排序样例与由CLI生成的基线；其余文件只读，不碰A/worker/前端与业务.env。
- 现有platform八条case不自动加入新字段，保留原数据集身份；完整标注与排序退化用独立ranking样例表达，候选/提供来源顺序变化而召回、答案和引用指纹不变，真实CLI比较应退出3。平台baseline因metrics_version=2由相同run ID的真实离线CLI重新生成，旧指标报告比较仍明确不可比。
- 新指标范围严格0–1、类型严格有限数；覆盖计数不得接受bool或越界，ID cohort须稳定且能从结果核对，不能只信相同样本数。应测而缺trace/response为unknown；eligible=0时值为null且与未测量区分，无显式门槛可not_applicable，显式门槛不能宣称通过。源匹配沿用既有身份/片段范围规则，不声称语义支持正确。
- 主代理保留的Q前旧平台报告为C:/Users/Administrator/AppData/Local/Temp/ragkb-q-preflight-platform-20261110-k6d1-report.json，可核对八条旧case的dataset/result/machine摘要未因缺省声明改变；run_id及identity.metrics_version变化单独解释，不继承旧人工核验。既有反馈导出/导入与人工核验模块继续跑通。
- 必跑：tests.test_evaluation、tests.test_evaluation_baseline、tests.test_evaluation_identity_v2、tests.test_evaluation_reviews、tests.test_feedback_regression_evaluation、tests.test_evaluation_ranking；离线CLI platform基线/退化/缺指标、ranking正常/排序退化/缺trace与旧报告不可比。全部只用合成离线输入，不连真实模型，不安装依赖。产品冻结后通知主代理，主代理读diff、亲跑验证及真实CLI后才进入P。

### Q 主代理首轮审查（尚未通过）

- 主代理亲跑6个要求模块：Ran 162 tests in 1.471s，OK，无跳过；日志C:/Users/Administrator/AppData/Local/Temp/ragkb-q-main-tests-20261110-m2b8.log。已审读evaluation.py实际diff、排序测试、旧反馈绑定兼容和说明/模板，未看到platform八case或语料被新排序字段改动。
- 已实际复现两处返修：_ranking_coverage_from_results把任意单项摘要不一致扩散为四项全部unknown，导致其余供给来源MRR从0.75降到0.4166667的已确认退步被掩盖（退出3变5）；_compare_ranking_metric正常计算分支丢掉baseline_coverage/current_coverage。已交原Q代理逐指标核验并保留正常分支覆盖字段，其他产品/基线仍冻结。主代理已准备真实CLI矩阵驱动，待修复冻结后亲跑，不提前把Q记为通过。

- Q最终通过：两处回修已由主代理复读实际实现与3项独立回归，正常分支保留baseline/current覆盖，单项摘要错误仅影响该指标，已确认的其他退步仍优先。主代理亲跑6模块166项、1.527s、OK，无跳过；日志C:/Users/Administrator/AppData/Local/Temp/ragkb-q-main-final-tests-20261110-m2b8.log。实际以子进程调用CLI共10次：platform/ranking同run ID重生各0且完整JSON等于仓库基线；platform正常0、断言退化1（6/8）、缺指标5、纯延迟退化3；ranking正常0、纯顺序退化3、缺trace5；旧metrics1报告4。CLI证据C:/Users/Administrator/AppData/Local/Temp/ragkb-q-cli-main-20261110-m2b8/receipts.json及各报告/比较/日志，LF副本仅在自有临时目录，未改原语料。旧preflight与新platform的dataset/config完全一致，8项result/machine摘要逐项不变；ranking正常/退化的召回及result/machine摘要也逐项相同。metrics_version仅升2，两指纹版本保持1。产品及全部测试冻结，进入P；仍未测真实业务准确率。

## P PDF 能力核对（Q 验收后实施）

- 当前后端已安装 PyMuPDF 2.0，Page.find_tables 可用，无需新增依赖。已查询官方文档：Table.extract 的内容必须在 page 生命周期内复制；header.external 为 true 时表头不在 extract 的第一行，需单独纳入。
- 优先使用真实表格识别结果及单元格/行坐标；仅在某表成功抽取后才排除对应原文区域，失败时保留正文。按页内位置合并正文与表格，不跨物理页拼造坐标；重复表头按现有 chunk 前缀机制保留。
- 本机 rapidocr 包不存在；tesseract 不在 PATH，PyMuPDF.get_tessdata 探测返回 RuntimeError，探测过程还暴露第三方 subprocess 的中文 Windows 输出解码告警。因此本轮不自动启用 OCR/下载模型/安装引擎；扫描件明确提示需要 OCR 后重传，不把空页当作成功入库。
- 实现时用 PyMuPDF 程序生成的独立临时合成 PDF 覆盖表格、空单元格、混合正文、多页、无表格、识别异常与纯图页；核对来源 metadata/schema 可实际承载的坐标，不声称尚未入 schema 的字段已经可在 UI 展示。
- 主代理已读现有_pdf_blocks/_table_blocks：PDF现仅get_text逐行，现成表格块会重复表头、保留空单元格与row范围。新实现必须先完整物化单个表，再决定排除其文字；检测或抽取异常均回退保留正文，不能失败后仍删bbox内文本。同一行仅部分位于表格区域时保留区域外文字，不整行删除。表格和正文按页内(y,x)合并，成功表头（含external header）不再重复当正文；外部表头加入后的逻辑行号须在说明中定义，物理页号始终以原PDF为准。
- 扫描/图像页与真正空白页区分：含未可提取文本的扫描图像应给清晰OCR提示，混合文档不能悄悄忽略该页后标ready；合法空白页不影响后续页号。无可索引文本的全空文件明确失败。仍不安装或下载OCR能力。
- P文件所有权待Q验收后冻结：structured_documents.py、独立PDF表格测试、必要既有结构化/来源测试和PDF说明；编码严格读取成果与A维护权限不得回退。

### P 实施任务卡与最终验收（已通过）

- 已派发唯一实施代理 ad830335087db1e84，任务“实现 PDF 表格与扫描页边界”；未收到冻结通知前不启动验收环境、不并发修改其文件。acl_maint 专库已独占分配给该代理；主代理不得同时运行清空该库的测试。浏览器环境的 p3c9 脱离生命周期启动脚本已准备，尚未执行；待产品冻结后加载最终代码。
- 目标：文本 PDF 真正保留表格行列与重复表头，正文不因表格识别异常丢失；无可提取文字的可见图像页不能静默跳过后发布 ready。主代理已复核本机PyMuPDF=2.0、find_tables/get_image_info均可用；不新增依赖。官方get_image_info只列实际展示图像（含inline），不加载图像二进制；不以资源字典get_images单独判断扫描页。
- 单一P实施者可写services/structured_documents.py、可选新services/pdf_documents.py、services/parser.py的PDF专用路径、services/ingest.py的安全解析错误映射；tests/test_pdf_documents.py与tests/test_pdf_ingest.py新文件、tests/test_structured_documents.py和tests/test_source_documents.py中直接受影响的PDF断言，以及根README的PDF能力说明。编码分支、ACL/上传/worker/前端、评测文件只读，不顺带重构。
- 表格完整复制extract/header信息后再排除对应原文，单表失败保留其正文；表格检测整体失败保留全页正文。external header只加入一次，不用fill_empty填造合并格或空值。正文与成功表格按实际页内位置合并；同一行/片段仅部分落表内时保留外侧文字。元数据保留物理页号、table_index、逻辑row_start/end，清楚定义外部表头行号；不虚构bbox前端高亮。所有现有chunk预算、prefix截断、章节和不跨页/表分组继续成立。
- 真正空白页可略过且不重编号；可见图像且无可提取文字的页明确提示页号及OCR后重传，覆盖文本页+扫描页混合PDF，整份不发布ready。全空PDF明确失败。错误仅允许专用类型/固定白名单安全文案进入Document.error_message，第三方异常仍用原通用提示；不能直接回显str(exc)、路径或正文。失败时parse_done保持false、无向量发布、不能检索半成品。
- 验收：真实生成临时PDF覆盖格线表格/空单元格/多表/外部表头/跨页/正文与表混排/同一行外侧文字/无表格/检测异常/单表抽取异常/图像页/空白页。异常可故障注入，但正常表格识别必须调用本机真实find_tables。真实PG+确定性向量替身验证ready来源位置，以及混合扫描件failed/OCR可见提示/不调用embedding/不可检索；验证兼容parser入口与结构化入口。主代理再亲跑并在owned本地应用上传合成PDF查看结果和关键失败提示。
- 数据库暂定复用已交回的acl_maint专库（62603，ragkb_acl_maint_20261110_k6d1_test，owner owned-test:acl-maint-20261110-k6d1），派发时独占给P；主代理canonical库不并发与该库混用。必跑新PDF模块、structured/source_documents/text_encoding、database与ingest_shutdown相关回归；实际模块与完整命令派发时确认。仍不安装OCR、不下载模型、不改.env、不commit/push；产品冻结后由主代理审查验收。
- 主代理独立浏览器资料已生成在Temp/ragkb-pdf-assets-20261110-p3c9，真实find_tables确认页内表格数量[2,0,1]、Beta空单元格为空；另有纯扫描、文字+扫描、全空与旋转局部扫描PDF。不用业务资料或真实模型。p3c9 harness仍未启动。
- 提前审读初稿pdf_documents.py并实际复现三处边界，已由同一实施代理修复：默认rawdict含图像字节（本机flags199包含bit4，扫描样例实际10357字节）；get_image_info异常返回false会让混合文档只yield页1且不报错；未旋转图像bbox直接与旋转page.rect相交，90度局部扫描图实际可见但_shown_image=false。最终显式关闭图像位、探测异常上抛失败、先旋转图像bbox再与page.rect相交；真实90/270度、cropbox、真实PG无embedding回归均已覆盖。编辑过程的临时构造器不一致不算产品缺陷。
- 主代理首轮亲跑7模块131项/85.982s全通过、0 skip（canonical owned库），日志Temp/ragkb-p-main-final-20261110-p3c9.log。随后实际复现requirements.txt支持的旧版find_tables签名因use_layout新参数被吞错而永久回退正文，交回原代理，仅pdf_documents.py/test_pdf_documents.py回修。最终增加窄适配：只对明确unexpected keyword use_layout退回一次无参调用；内部TypeError仍一次全文回退。主代理再亲跑5模块75项/7.201s全通过、0 skip，日志Temp/ragkb-p-main-compat-final-20261110-p3c9.log；独立真实旧签名包装复核非空页调用2次、真实表格3张，90度局部扫描页正确报物理页2/OCR。未安装真实1.25运行时，旧签名验证是接口注入转调本机真实finder。
- 共享浏览器首轮p3c9已完成真实成功及失败：表格PDF上传至ready，7块/3表/7向量，Beta空单元格、SIDE及物理页[1,3]正确、Alpha/Beta/Gamma/Delta各一次、原文接口第三页位置正确；纯扫描/文字扫描混合/全空均failed、parse_done=false、embedded=0，原文按既有契约409拒绝。替身始终embedding_calls=1/vectors=7、chat_calls=0，失败件没有额外调用；已实际查看表格与混合OCR提示截图。中途一次验收脚本误用token键触发401、误期望failed原文404后按真实契约改为409，均非产品缺陷。扩展不能读本地文件，未改权限；改为在页面内新建已知合成PDF经现有input change上传。
- P最终通过。p3c9页867074285已关闭、owner/nonce清理完成；最终代码p4d0环境独立启动三个worker后，主代理亲跑真实浏览器第三轮验收exit0，证据Temp/ragkb-pdf-browser-evidence-20261110-p4d0-r3。前两轮临时驱动getByRole精确定位登录控件失败，截图证明页面正常；改用项目既有uiLogin的placeholder定位后完整通过，未改产品。实际六图均已Read查看：空单元格、第三页物理位置、纯扫描/混合/全空三种错误提示及最终四文档状态。ready为7块/304字符/3表/7向量/页[1,3]，三失败parse_done=false/embedded=0/source=409。替身由0变embedding_calls=1/vectors=7、chat_calls=0，失败前后计数完全相同，console数组为空。p4d0已通过harness down按owner/nonce完成清理，不复用旧端口/PID。仅本机PyMuPDF2.0真实运行，旧签名通过窄包装转调真实finder验证；旋转文字页目前完整正文回退，未启用OCR。75项为131项中的最终修复覆盖子集，不累加为206项独立用例。

## R 在线回归只读规划输入（P 验收后实施，尚无代码）

- 只读 Plan 代理 a51c5d4bde8a0689c 已核对相关服务/API/worker/前端与测试。推荐新增 regression 独立角色与 feedback_regression_jobs 持久作业表；保留现有 FeedbackRegressionRun 及 prepare/export/import 兼容路径。原三角色职责不变；届时扩充运维模型/前端角色联合类型/告警和 E2E harness，而不把模型重跑塞入入库、摘要或项目队列。
- 复用 feedback_regression_evaluation 的 case/绑定/报告重算，feedback_regression 的 CAS/幂等，rag/project_retrieval/document_scopes 的实际检索、生成、固定范围与来源复核，evaluation 的评分/比较，usage_tracking 的实际用量记录。不能复用同 run_id 的 run_live_case 请求ID生成来“重跑”，否则可能重放旧 ChatTurn 答案；执行器不污染用户会话历史。
- 建议新增固定 /feedback/regression/jobs 与 /batches 路径：批量上限20、状态分页、单个/批量取消、每回归最多一个活动作业、SKIP LOCKED+租约+有界退避；写入鉴权先于资源锁，发布重新检查原 token_version、当前权限、来源版本、lease/cancel。不得持锁等待模型，迟到结果不得覆盖取消终态。
- 项目与限定文档范围需要替换现有两处 unsupported 门禁为真正的 snapshot/file-version/document-index 绑定校验；不能简单删除校验。case/scope/配置变化应显式409或终止，不能暗中改成全库或最新快照。
- 主代理审查规划时保留三项修正：A 已先于 R 实施，R 必须直接复用已交付的统一成员 ACL，不能新增只检查 admin/public/owner 的临时兜底；实际已知 token 用量与未知价格须分开，不能因费用未知抹掉已知 token；页面需能核对实际新旧答案/引用并把人工确认绑定到具体 run/result 指纹，只比较hash/机器断言且沿用旧人工结论不足以验收。
- 新结果正文如为人工比较而持久化，需有硬尺寸界、当前权限和来源复核、级联清理与既有导出隐私边界；审计/运维/失败信息仍不得记录正文。具体模型、API响应与文件所有权在 P 验收后基于届时实际代码冻结，不将此只读建议当作实现成果。

### R 主代理补充设计输入（P 实施期间只读核对，尚未派发）

- 拆为R1持久作业/API/固定来源与scope基础，R2实际执行/独立worker/运维和启动集成，R3前端作业与人工比较。R1通过并冻结接口后，R2与R3可按不重叠所有权并行；R整体须真实模型替身与浏览器成功/失败验收后才通过。R1新表和独立job服务负责作业租约/发布，R2不得另造第二套作业状态机。
- 已核对现feedback_regression和adapter两层均拒project/scope，adapter还含旧admin/public/owner临时检查；须替换成真实固定snapshot/文档代次与统一kb_access校验，不能单删unsupported。普通旧case无新字段时原binding必须保持，source_kind/project身份等只对新能力显式扩展。原prepare/export/import保持兼容，新scope不能借旧live CLI默认active/最新代次执行；若CLI不能承载固定范围须在调用前明确拒绝该组合，不悄悄扩大。
- 执行主体须明确：发起管理员的原token_version/角色/active决定作业是否合法，原反馈所有者的当前active/KB ACL决定按谁的权限重跑；不能以管理员身份让已撤权成员的样例继续读库。需多User行按UUID稳定排序，再KB/ProjectSource/ProjectSnapshot/Conversation/Message/Document及反馈/回归/job下层锁；批量先规划全部根再按类别锁定，不能逐条跨KB→User反序。claim/recover/heartbeat可独立短事务，但不能拿job锁再反取根锁。
- 建议POST /feedback/regression/jobs（单条）、POST /feedback/regression/batches（最多20条，固定请求UUID幂等），GET jobs及job详情/比较，POST job/batch取消，PUT job人工核验。单回归一active job，DB唯一约束和CAS共同保证；同键重放也复核原token/ACL，不把旧revision失败自动重放为新任务。批量的全部或部分语义须在R1接口冻结时明确，不留前端猜测。
- Job执行上下文在短事务内构造后脱离Session，只存ID/版本/配置摘要，问题序列等正文从当前可核实原消息临时重建。复用followup.plan_retrieval、rag.retrieve/prepare_answer/answer_astream/finalize_citations及project_retrieval，不创建或污染用户Conversation/ChatTurn/Message；多轮历史为本次重跑生成的临时历史，问题≤10。固定document_scope在每轮生效，topic switch不得放宽已登记范围；项目始终原snapshot。发布复核候选/供给/历史依赖的实际来源与job lease/cancel，迟到结果无效。
- 后台有界重试与stop协作需保留已实际发生的token，不把费用未知当用量未知。UsageContext可用原用户/KB及独立operation，不依赖会话写入；trace、日志、审计、运维仅ID/安全码/计数，不含问题/答案/引用正文。新结果body如持久化采用硬JSON字节限额并挂反馈级联；旧正文不复制，比较时重新读并核对原指纹，来源/权限失效后禁止继续返回存储body。
- 人工核验单独绑定job/run/result_sha256和CAS，初值unknown，不沿用原反馈verified或上一轮核验。页面需要可读的新旧答案和实际引用、机器断言与用量/费用未知展示；轮次变化/切反馈/失败后不得迟到回填。列表批选有界，单次与批量取消都应可达。
- 前端新增大段消息在懒加载反馈组件内使用局部i18n或明确按需加载，避免199.70kB入口超过200kB；不得通过持续删短文案掩盖架构问题。运维新增regression第四独立角色需同步backend schema/status、frontend角色/标签、告警与E2Eharness；原三角色职责不变。部署compose/local bat/维护脚本按真实角色清单同步，是否新增feature开关在R1确定；不改业务.env。
- 主代理补查发现evaluation._matches当前只比filename/document_id/index_version/chunk范围，不能直接证明project snapshot/manifest/config身份一致。R固定项目来源需要严格扩展项目来源校验/匹配，同时保留所有旧上传case的规范化与v1结果/机器指纹；不得将新项目字段无条件注入旧case。R1冻结时应把这部分适配和evaluation旧兼容回归纳入明确文件所有权。
- 用量关联初步选用ModelUsage新增可空regression_job_id UUID（无级联外键），UsageContext传播同名可选字段并由for_operation自然保留；旧调用缺省None，已发生用量在反馈/job删除后仍保留。需在init_db的_ADDED_COLUMNS及显式索引步骤幂等迁移；不能把整个用量行级联删掉，不能仅凭新答案最后一次generation帧冒充全部rewrite/embedding/rerank/generation成本。R1先冻结该关联，R2才接入，测试真实token已知但价格缺失时费用仍null。
- deploy/common.sh当前按容器标签识别写服务，候选列表是api/worker/worker-summary/worker-project，并有*worker*通配；第四角色仍应更新明确清单、注释和维护夹具/测试。不能改成compose config --services而漏掉profile服务。以上均是待实施设计，不代表R已交付。
- 在线job须有明确的run关联与执行种类，避免旧“待导入”界面把在线执行当成可人工导入的轮次。旧prepare在同回归存在活动job时应冲突；旧export/import对online轮次明确拒绝，不能覆盖worker即将发布的结果。普通manual轮次的旧下载/导入/绑定保持兼容。R1冻结时需给R3明确execution_kind与可导入能力字段。
- 批量拟采用原子语义：1–20条唯一反馈、每条expected_revision，全部当前权限/来源/CAS通过才一次提交；一条无效整体零入队。request_id和规范化请求摘要绑定；同键同内容返回原批次/作业，同键异内容409；重放仍重查原认证版本与当前权限。页面必须说明整批冲突，不能展示未提交条目为已排队。
- Job的运行重试次数与FeedbackRegressionRun.attempt_no分开：前者是同job的有界worker重试，后者每次用户新执行递增。租约heartbeat不应持续抬升面向UI的CAS revision，否则取消/人工核验会无谓冲突；发布以job_id+lease_id+状态+cancel与绑定复核为准，heartbeat只更新租约时间。终态与人工核验才推进可见revision。
- 人工核验请求拟包含expected_revision、run_id、result_sha256及verified/rejected/unknown；只允许成功且可复核当前来源的具体结果，notes有界，不记审计正文。新的job始终unknown。删除原feedback须级联删除job及结果正文；已发生ModelUsage只保留UUID关联，不受级联影响。
- 上传库“全库”在线执行也需冻结入队时可检索文档ID/代次清单并有明确硬上限，超限显式拒绝，不能只固定expected_sources却让新版本/新文档在排队期间混入。限定范围按原document_scope固定；全库无文档产生显式空manifest，检索必须空而不能退成全库。项目使用不可变snapshot，不能用upload索引代次替代其manifest/config身份。旧manual case摘要不因此注入新默认字段，在线执行manifest可单独加入job绑定摘要。
- chat._retrieve_plan第401行现有active_scope or None把[]折成全库，且document_followup会更新selected；R不能原样复用后声称固定空范围得到保持。R2公共检索适配必须明确None/[]语义、原有followup锚点和临时收窄均受固定manifest最终约束；topic switch不得重置为None，空manifest不发全库检索。现普通chat行为不在本阶段顺手改变。

### R1a 固定来源与上下文基础任务卡（P已通过，先实施本卡）

- R1按文件依赖拆成R1a固定来源/作用域/统一权限基础与R1b持久job/API/租约/用量关联。R1a主审通过并冻结接口后才派R1b；R1b接口主审通过后再并行R2执行与R3前端。当前不派R2/R3、不启动第四worker，不把可读上下文误记为在线重跑已交付。
- R1a已派唯一实施代理ad3ff5e8fa3780129，可写：backend/app/services/feedback_regression_context.py（新）、feedback_regression_evaluation.py、feedback_regression.py、evaluation.py（只扩项目来源及固定范围验证/匹配，保留Q口径）、backend/app/api/v1/feedback_regression.py（仅原认证快照与服务接入）；backend/tests/test_feedback_regression_context.py（新）、test_feedback_regression.py、test_feedback_regression_evaluation.py。除此之外只读；根计划归主代理。无新持久模型/设置/worker/前端改动。
- 新上下文服务提供可供R1b/R2复用的批量根规划/加锁及执行上下文构建；批量1–20条先收集全部根，同类型UUID序，User→KB→ProjectSource→Snapshot→Conversation→ChatTurn→Message→Document→Feedback，其后调用方才可取regression/job。重读根规划身份，不一致失败关闭；不持下层锁再回取根。管理员原token/role/active及反馈所有者当前active/KB成员ACL都复核；不以管理员权限替代原用户。上下文是可脱离Session的有限数据，服务自身不commit、不调用模型/网络，不创建会话消息。
- 上传执行范围冻结当前可检索Document ID/index_version清单；原有限定范围时先按其语义验证。清单上限256，超限明确阻断，绝不截断；显式空manifest保持空。全库清单单独供job绑定，不向旧manual case无条件加字段，不因新增清单改变旧上传case/dataset/binding/result/machine指纹。固定项目使用原snapshot/source/file版本与manifest/config，不切当前active快照。项目来源严格验证真实chunk及快照成员关系，字段规范采用answer_sources权威形态。
- 替换两层project/scope unsupported及adapter旧临时ACL，真正支持来源解析与当前权限；新增scope/project case只在确有需要时显式加入身份字段。普通旧prepare/export/import保持兼容；旧live CLI不能承载固定scope/project时，在准备/导出前显式409拒绝该组合，不能生成看似可执行却会查全库/新快照的JSONL。offline新case可以正常规范化与校验；online路径留给R1b/R2独立执行。
- 旧enroll/prepare/import及会导出正文的重放路径接入统一根门禁；API显式捕获request.state.auth_token_version并传入，不能等待后从已刷新的ORM反推。rollback后的唯一冲突重放亦须重新根鉴权。原功能与合理状态码保持；新增能力对应原unsupported测试应变为严格成功及失效拒绝回归，不放宽断言、不删原兼容golden。
- 必须覆盖真实PG：显式成员私库、owner撤权/禁用、admin token等锁后变更、固定upload代次、限定范围越界/空manifest/256上限、项目真实snapshot/file/chunk/manifest/config错配及active切换、批量跨用户/KB稳定锁序；不mock正常表/来源成功。纯评测补项目同文件不同snapshot不命中，并保证Q与旧上传golden不变。
- 独占测试库分配给R1a（现已交回；2026-11-11恢复后端口59010）：127.0.0.1:59010/ragkb_acl_maint_20261110_k6d1_test，owner owned-test:acl-maint-20261110-k6d1，合成账号postgres/regression_only_password；必须RAG_DISABLE_DOTENV=1、LOG_STDOUT_ONLY=1并通过共享test gate。主代理canonical库与此库互斥。必跑tests.test_feedback_regression_context、test_feedback_regression、test_feedback_regression_evaluation、test_evaluation、test_evaluation_baseline、test_evaluation_identity_v2、test_evaluation_reviews、test_evaluation_ranking，输出日志到owned Temp。执行前确认现测试模块存在。
- 报告实际文件/diff、公共函数精确签名与返回数据、临时/持久数据边界、错误码及新scope的manual/live规则、所有测试原文与skip/未完成项，然后冻结并交回文件和专库。主代理亲读diff、在canonical库亲跑后才通过。仍不安装下载、不修改业务.env、不commit/push、不启动收费模型。
- 已补充给R1a：提供已冻结manifest的显式输入/验证接口，None代表首次冻结、[]代表固定空范围；全库入队后新增无关文档不纳入，也不必因此拒绝合法作业。每个原定文档仍须当前可检索且代次一致。
- R1a首轮交付后主代理亲跑上述八模块：201 tests in 24.737s，OK，零skip。真实日志C:/Users/Administrator/AppData/Local/Temp/ragkb-r1a-main-review-20261110-k6d1.log。随后独立反例确认需修复，尚未通过：257份可检索文档误触在线清单上限，导致旧manual GET不可用、prepare/enroll重放409；项目actual的document/file版本、relative_path/filename、公开/metadata指纹别名冲突仍命中，且upload expected可误匹配project actual、项目负数chunk标注仍通过校验。实证脚本与日志为Temp/ragkb-r1a-main-boundaries-20261110-k6d1.py/.log；临时驱动两次取错prepare响应revision键后已纠正，不计产品缺陷。已一次性发回原代理修复并补真实PG大库纳入/准备/导出/导入兼容、项目严格身份反例；在线256硬限与None/[]语义继续保持。另要求准确标注ExecutionContext.to_dict含原问题、只能临时使用，不能整体持久化为job摘要。R1b仍未派发。
- 主审补充核对：现有rag.to_citations对project公开引用也生成index_version=0，answer_sources项目解析会忽略该兼容字段；actual带0本身不属于缺陷，已及时修正给原代理，不修改rag/来源解析器。要求新增真实to_citations→评测项目匹配正例，避免严格校验误伤现有公开引用；expected project仍不携带上传代次。

- 2026-11-11恢复：上次会话中断使同一R1a代理修复任务停止；磁盘仅有manual freeze_manifests部分修改，项目匹配及新增测试未完成。已恢复原代理ad3ff5e8fa3780129继续同一所有权。Docker Desktop原未运行，主代理启动已安装应用并仅启动已核实标签的测试容器5aab97439cff15d2baa66ff5bd2fbaa6ced6c1fb68c4d5e358d47df8fdebff6a（io.ragkb.task=next-20261110-c4f2）；实际新端口127.0.0.1:59010→5432，旧62603失效。三测试库comment已用容器内psql亲查与原owner一致。后续所有测试采用59010；原业务PG/redis未作配置或数据修改。R1a仍待修复交回及主验。

- 2026-11-11 R1a主审通过并冻结：主代理在canonical隔离库亲跑八模块，207 tests in 33.187s，OK，零skip（Temp/ragkb-r1a-main-final-20261111-k6d1.log）；独立真实API验收全部通过（Temp/ragkb-r1a-main-acceptance-20261111-k6d1.py/.log），覆盖257文档下手工GET/纳入/同键重放/准备/导出/内容复算导入、新反馈纳入，在线仍拒manifest_overflow；真实rag.to_citations项目零代次引用与offline内容导入成功，四类身份冲突、跨来源类型及非法chunk拒绝。最终context新增summary()只含白名单绑定身份；to_dict()明确含问题，仅临时使用。后续只调用冻结接口。
- R1a交付方另报告2037全量测试通过（3 skip），但误用主代理canonical库而非分配的专库，属于测试库所有权偏离，不能记作主代理最终全量验收；当时主代理未同时写该库，独立验收已重新使用owner gate并清理合成数据。后续实施任务只可使用任务卡明确分配的专库，禁止自行切到canonical或扩大为未分配库的全量运行。

### R1b 持久作业与API任务卡（公共契约冻结，传递来源窄修主验通过）

- 2026-11-11 history窄修已交回并由主代理亲验：只改规范结果来源函数与job回归，将扁平history包装为assistant严格v1 source_provenance后交既有helper解析；合法历史保留到body/比较/后续传递，畸形或清单外来源拒绝且不写body。主代理七模块136 tests in 70.243s，OK/零skip，15个交付文件测试前后SHA256一致（Temp/ragkb-r1b-main-history-suite-20261111.log及receipt）；实际五项发布边界、跨操作/重试用量与旧全局约束迁移三个独立驱动再次全部exit0（Temp/ragkb-r1b-history-main-probes-20261111-receipt.json）。service SHA256为16ce65b4e4e09d3a8793e0fe86bcc7aa76d412516779f3ce1a537733d2ba2933。acl_read已由原代理按gate清理交回；不得继续写文件/库。R2收尾者确认其快照修复落盘后，主代理追加运行Temp/ragkb-r-main-history-acceptance-20261111.py，实际临时v1快照保留历史、真实发布/比较保留两来源、持久双指纹重算和不可核实来源拒绝均exit0（Temp/ragkb-r-history-main-probes-20261111-receipt.json）；这只验快照/发布边界，R2执行/worker仍待完整交回，不把R1b通过记作整个在线链路完成。

- 2026-11-11 R2初稿复审新增实证：_canonical_result_sources把扁平history_sources传入build_source_snapshot的assistant历史参数，合法A/B来源得到history=0、actual=1，历史B在最终验证前被丢弃；executor._snapshot同样误用。主代理纯函数驱动Temp/ragkb-r2-main-history-boundary-20261111.py复现（无DB读写/模型调用）；首次驱动缺chunk_index被严格解析拒绝，补合法字段后确认产品缺陷。R1b只把规范来源函数及新job回归窄修交原a5960ad8f5971a138，专库重新分配acl_read（59010，owner owned-test:acl-read-20261110-k6d1），禁止再用已转R2的acl_maint；R2自己的临时v1快照由R2修。公共签名/DTO不变，但该来源边界修复和主验前不能称完整R1b安全验收结束。

- 2026-11-11最终主验通过：收尾代理a5960ad8f5971a138交付后，主代理亲跑既定7模块134 tests in 75.955s，OK/零skip（Temp/ragkb-r1b-main-suite-20261111-v2.log）；SHA256回执确认测试期间及交付后源码完全一致。另亲跑三个独立真实PG/API验收：五项原反例全部修复、实际ModelCall跨操作及重试5行保留已知59 input/9 output与未知总量/费用且取消/列表/原UUID找回一致、旧全局request唯一约束真实迁移到admin+UUID并重复初始化保持既有行（Temp/ragkb-r1b-main-{final,usage,migration}-acceptance-20261111.log）。diff --check通过（仅既有CRLF提示）。R1b全部文件及实际claim/recover/heartbeat/check_lease/load_execution/complete/fail/release契约现冻结，R2b/R3b可正式派发。
- JobRead顶层usage冻结为schema中的22字段，含known_input_tokens/known_output_tokens、覆盖条数、单币种已知小计与完整费用；所有状态和创建/详情/列表/批次/找回/取消/比较/核验路径均返回，一页或一批一次分组查询。任一调用缺测时总量null但已知列小计保留，缺价完整费用null，多币种不混加。比较/人审同时复核旧答案严格v1实际来源和新结果来源；失效409且不返回old/new正文。
- R1b已按gate清理并交回acl_maint专库，主代理再次亲验owner与主要业务/作业/用量/项目表全零；后续正式转交R2b独占，R1b代理不得继续写库或文件。主代理canonical库的独立验收亦已清理。以下保留历史问题和原任务标准，以本节最终冻结状态为准。

- 2026-11-11最初派实施代理a4473c03ecf9cd98a，R1a冻结的接口与主验日志已交付。该代理后续连续两轮交付中断、只返回未执行工具标记，现已completed且无子任务；按两轮失败换策略，由新唯一R1b实施者a5960ad8f5971a138接手磁盘已有五项回修及未接完的job_usage/job_read调用点，原代理不再拥有文件。禁止重复实现状态机或扩大自检。仅可使用127.0.0.1:59010/ragkb_acl_maint_20261110_k6d1_test，owner owned-test:acl-maint-20261110-k6d1；禁止切canonical/其他库，不跑未分配库的全量discover。专库归现R1b独占。
- 唯一所有权：新models/feedback_regression_job.py、schemas/feedback_regression_jobs.py、services/feedback_regression_jobs.py、api/v1/feedback_regression_jobs.py；models/feedback_regression.py（run执行种类）、models/usage.py、models/__init__.py、db/init_db.py、core/config.py、services/usage_tracking.py、api/v1/__init__.py；R1a交回后的services/feedback_regression.py及schemas/feedback_regression.py（旧manual与online互斥/响应能力）；新tests/test_feedback_regression_jobs*.py、tests/test_feedback_regression.py、tests/test_usage.py/test_usage_database.py/test_usage_measurement.py直接相关用例。冻结R1a公共context/adapter/evaluation，只调用不改；超出这些文件先报依赖。README/worker/运维/compose/harness留R2，前端全留R3。
- 单条POST /feedback/regression/jobs：feedback_id、expected_revision（已纳入regression的CAS）、request_id UUID；批量POST /feedback/regression/batches：request_id、1–20唯一feedback_id+expected_revision。已纳入且当前可用才入队，不隐式猜原因分类。全批先根规划并校验，再一次提交；一条失败整个零入队。同一管理员请求UUID与规范化摘要绑定，路由种类也绑定；同键同内容返回既有批/作业，同键异内容409，重放复核当前权限/绑定。单条也可内部使用一项batch，但HTTP响应明确给job。
- 作业状态固定queued/running/succeeded/failed/cancelled；DB部分唯一约束保证每regression至多一queued/running；每job唯一关联一次新的online run，不能借旧prepare的内部commit做批量。新提交递增run.attempt_no；同job worker retry另计attempt_count。run.execution_kind缺省manual且真实幂等补列，旧prepare在active job期间409，旧export/import对online轮次409，普通manual全部兼容。schema给出明确execution_kind/can_import及online状态，R3不猜。
- GET jobs有界分页/feedback或batch过滤，且提供按当前admin+原request_id的有界只读找回能力（可用额外过滤或等价查询，返回实际batch/job及请求种类）；真实提交后响应丢失时前端先读回，不能重放写或猜“最新job”。GET job详情不含结果正文；GET job比较才返回当前核实的原答案/原引用与有界新答案/新引用、机器断言与指纹。原文重读原Message并核对反馈指纹，不复制旧答案。新body JSON UTF-8硬限1 MiB，引用/trace有限且来源身份严格解析复核，反馈删除级联清理body/job/run。日志/审计/列表/运维不含正文；异常只白名单码。读取比较使用当前管理员与原owner的ACL、当前case/来源绑定，失效不回显旧body。
- POST job/batch取消有CAS；取消立即使lease失效并阻止迟到发布，已终态无新执行。取消仅改作业元数据、不返回正文，当前管理员原token复核即可，不因owner撤权或文档失效阻止管理员停止任务；可使用User→job的有序锁子集，不能拿job再取根。批次成员不可变，batch revision仅显式批操作推进，状态计数从实际jobs推导。
- worker公共接口负责claim(SKIP LOCKED)/recover/heartbeat/load_execution/check_lease/complete/fail/release，精确签名在R1b交付冻结；所有模型工作在外，Session/锁不能跨等待。claim/recover/heartbeat仅短job事务；load/发布先R1a全部根再regression/run/job，验证原发起admin token/role/active、原owner ACL、case/manifest绑定、lease、cancel和deadline。最终机器评分由服务对真实新内容重算；执行完成但断言不通过仍是succeeded+passed=false，模型/权限等执行失败为failed。
- 有界参数集中Settings：job lease默认300秒（30–3600）、poll默认2秒（0.1–30）、最多3次（1–10）、retry delay默认5秒（0–300）、整job总执行超时默认600秒（30–3600）。不新增默认自动入队，也不新增不必要feature开关；regression角色与ingest同样由显式启动/显式管理员提交驱动，未启动运维明确not_started。heartbeat不涨UI revision；终态/人工核验涨job revision。重试仅显式可恢复错误，保留全部已发生用量。
- ModelUsage新增可空无FK的regression_job_id及显式幂等索引；UsageContext将该可选字段追加，保留原positional/for_operation语义。聚合包括rewrite/embedding/rerank/generation及全部重试，已知token单独保留；缺usage或价格显示unknown/null，可提供known部分及测量覆盖，绝不把unknown补0。旧调用仍None，删除反馈/job不删用量。
- PUT job人工核验请求含expected_revision/run_id/result_sha256/status(verified/rejected/unknown)/有界notes；仅成功且当前可比较结果，新job始终unknown，不沿用旧反馈或旧run核验。核验与取消都不可自动重放未知失败写入。
- R1b接口衔接细化：在线run沿用requested_mode/report_mode=live，与新增execution_kind=online区分（不擅自把online塞进既有offline/live协议）；完成时grade_case默认include_content=False，不能把其content.questions或ExecutionContext.to_dict整体落库。新结果正文单独白名单保存answer/citations/有限trace，原问题与原答案均重读。UsageContext归发起admin、关联KB及regression_job_id，不绑定原conversation/turn，避免把后台重跑混进原会话用量；资料读取另按原owner权限。第一次claim建立整个job的started_at/deadline，重试/heartbeat不得续总期限；队列等待不计执行时限。比较与核验允许仍符合当前绑定的历史成功job，不因后来准备了新一轮就永久禁看；最终发布仍须该job持有当前活动轮次与有效租约。
- 必跑真实PG任务/API/迁移/租约与并发回归：原子批量失败零入队、两并发同键只一批、异内容409、单active竞争、取消/过期/retry迟到结果不能发布、owner撤权和原admin旧token不能执行或发布、实际项目/文档固定范围、旧manual准备导入/导出不受影响、online拒人工导入、真实usage保留与价格未知、反馈级联与用量孤UUID保留。测试库在R1a交回后重新独占分配，不与主代理canonical混用。R1b主验前不启动R2b实际执行或R3作业前端；无接口依赖的R2a角色/部署接线可按下卡并行。
- 2026-11-11 R1b首次正式交付报告既定7模块123项OK/零skip、新job模块重复15项OK（Temp/ragkb_r1b_final_20261111.log等），但尚未通过主审。主代理在canonical owned库独立真实PG/API复现五项：两个admin相同UUID不同feedback仍A200/B409；评分期间跨deadline/lease仍succeeded；raw trace评分与持久显式来源trace不一致导致机器断言/hash不同；301字符snippet裁300后result指纹无法从持久body复算；原DocA停用、纠正DocB新执行合法成功后，比较仍返回失效DocA旧答案/引用。驱动及日志Temp/ragkb-r1b-main-boundaries-20261111.py/.log，合成资料已按gate清理。五项已一次性发回原代理修复，要求admin命名空间真实约束迁移、发布末端复核、规范结果统一评分保存、比较/核验旧实际来源门禁；旧错误来源失效不得反向阻断纠正执行。R2b/R3b继续等待主审冻结。主审另确认JobRead/ResultRead完全没有用量汇总、既有usage API也无job过滤，R3无法展示全调用/重试费用；已要求同一R1b在现有schema/service内按ModelUsage.regression_job_id增加有界批量汇总并冻结DTO，保留已知部分、缺测/缺价null和多币种边界，不以最后generation trace冒充全部用量。

### R2 实际执行与第四角色任务卡（最终主验通过）

- 2026-11-11最终主验：主代理完整后端2103项（2100通过、3项Windows平台skip；Temp/ragkb-r-main-backend-full-20261111-receipt.json）。真实四角色心跳running/alive1；项目active快照切换后，两次在线job仍取原snapshot、分别产生新run与新模型调用，原会话完整JSON不变（Temp/ragkb-r-live-project-20261111-s8n4-receipt.json）。canonical专库经owner门禁核验清理后29表全零（Temp/ragkb-r-final-integration-20261111-receipt.json）。完整测试之后只修改worker.py/e2e_env.py的过时注释，未修改执行行为；使用项目.venv解释器核验，不使用缺SQLAlchemy的系统Python。

- 2026-11-11 R2b收尾者交回并冻结：12模块276 tests in 112.691s，OK/skipped=2（Windows无POSIX SIGTERM）；原engine未重绑、2489次owner gate、源码SHA256前后一致，22张合成业务/job/usage/project/heartbeat表清零。主代理已亲读executor/worker/队列观测与新增行为测试，亲跑新两模块23 tests in 36.775s，OK/零skip，源码稳定（Temp/ragkb-r2b-main-execution-20261111-receipt.json），diff --check通过。独立history验收另补继承文档发布后停用：比较/人审均409、不回正文（Temp/ragkb-r-history-main-revocation-20261111-receipt.json）。后端全量后续已在canonical完成：2103项，2100通过、3项Windows平台skip，测试期间源码稳定，最终回执见本节顶部。
- 2026-11-11用户反馈推进耗时长后调整串行依赖：后端代码已冻结，先与前端边界收尾并行启动owned四角色应用，用已有dist暂时承载preview，仅验后端真实HTTP链路；前端最终交回后必须在该API目标上重新生产构建，之后才能做最终浏览器验收。自有环境owner=r-online-20261111-s8n4，out=Temp/ragkb-r-e2e-20261111-s8n4，runner logs=Temp/ragkb-r-e2e-launch-20261111-s8n4，PowerShell Start-Process脱离任务生命周期启动（初始orchestrator PID22464仅记录、不按PID盲杀），skip frontend build已记入launch receipt；有界owner/nonce/代理/四角色readiness运行中。后端全量与此应用使用不同数据库，不并发清理，业务容器/配置未动。

- 2026-11-11 R1b契约冻结后正式派R2b原实施者a8c157ef0a42655de；第一轮服务返回客户端不支持（0工具），恢复后一轮落盘executor/worker/四角色观测和部分既有测试，但只返回分析片段，没有新executor/worker测试或正式验证日志。原代理现已停止、无写权，按两轮未交付换策略，唯一收尾实施者改为fork a069b041c3718350c，在已有实现上补实际测试及实测修复，不重做状态机。acl_maint专库（59010，owner owned-test:acl-maint-20261110-k6d1）独占转交该收尾者；R1b窄修另用acl_read，主代理canonical独立。其余仍按Temp/ragkb-r2b-task-card-20261111.md白名单，R1b/检索基础/R2a/前端只读；主代理统一负责后续owned环境与真实浏览器验收。

- 2026-11-11依赖调整：将不依赖job表或worker服务签名的角色CLI/部署/harness接线独立为R2a，与R1b并行；R2b执行器、真实队列观测仍须R1b主审通过。R2a不启动新harness，不把四角色接线单元测试当作真实回归执行验收。R3b作业前端仍等待R1b真实API冻结，R3a既有运维角色接线独立。
- R2a已派唯一实施代理ac1538f7dffc83e7b，禁止使用任何测试数据库或启动环境。唯一所有权：backend/worker.py（仅角色注册/动态导入路径/帮助与相关注释，不改ingest生命周期）、backend/start-worker.bat、backend/scripts/e2e_env.py、backend/tests/test_worker_roles.py、backend/tests/test_e2e_harness.py；根docker-compose.yml、README.md的角色/启动/在线回归说明、DEPLOY.md与deploy/acceptance/README.md的角色/停写说明、deploy/common.sh、deploy/tests/test-maintenance.sh及直接夹具、deploy/monitoring/prometheus/rules/ragkb-worker.yml.in的角色说明。没有测试数据库分配，只跑不依赖DB的相应模块和维护shell/fixture compose静态验证；不得使用canonical或R1b专库。新regression模块路径固定app.services.regression_worker，run(stop_event)，由R2b提供。
- R2b独占剩余执行与观测文件及新executor/worker测试、backend/.env.example的最终配置说明；R2a交回前不改其文件。需要集成修复时由原所有者处理，避免与R1b/R3交叉。
- R2检索基础补卡（不依赖R1b、可独立先做）：主代理发现R1a在线manifest上限256，而rag.retrieve→document_scopes.scope_pairs/contains/scope_filter现统一硬限5，直接把冻结清单送入会永久失败。仅将内部检索身份校验明确支持有界256，与用户会话选择上限5分开；scope_ids/resolve_scope/validate_scope及scope_pairs默认仍5，旧manual/adapter/context默认语义不变，显式[]继续false SQL且不发全库查询。所有SQL筛选在排名/LIMIT前，不能拆成多次模型查询或检索后过滤。独立实施者只可写services/document_scopes.py、rag.py（仅检索范围验证，不改生成/普通chat控制流）、tests/test_document_scopes.py及新tests/test_regression_manifest_retrieval.py；vector_store/retrieval_candidates只读（它们已调用scope_filter，若确需改先报）。专库独占分配acl_read：127.0.0.1:59010/ragkb_acl_read_20261110_k6d1_test，owner owned-test:acl-read-20261110-k6d1，合成账号postgres/regression_only_password，RAG_DISABLE_DOTENV=1/LOG_STDOUT_ONLY=1/共享gate。验证6、256成功/257拒绝、非法/重复/版本类型、SQL rank/LIMIT排除范围外强匹配、空清单无网络；跑本卡模块及document_scopes/retrieval_candidates/hybrid_search/context/adapter相关回归，不discover全量，不写其他库/业务.env/worker/UI。R2b须等本卡与R1b均主验冻结后使用实际接口。
- 检索基础已由a647b3b8dfbe556d9交回并主验通过：亲跑8模块89 tests in 12.163s，OK/零skip（Temp/ragkb-r2-manifest-main-20261111.log）；复查要求删除新测试自动重绑engine的配置掩盖分支、每次清理前verify gate后，同一实施者完成，主代理亲读并重跑两模块20 tests in 5.121s，OK/零skip（Temp/ragkb-r2-manifest-main-final-20261111.log），diff --check通过。产品仅document_scopes/rag的检索身份上限适配，默认用户scope_pairs仍5，内部MAX_INTERNAL_SCOPE_DOCUMENTS=256；rag.retrieve/supplement接受ExecutionContext的tuple manifest，[]/()不发embedding；真实vector/keyword SQL在LIMIT前排除范围外强匹配。新增测试与专库acl_read已冻结交回，R2b只调用接口、不并发修改。
- 2026-11-11 R2a接线主验：主代理亲跑roles/harness两模块100 tests in 10.525s，OK/零skip（Temp/ragkb-r2a-main-unit-20261111.log）；Compose显式fixture.env+合成密码+--no-env-resolution只读渲染、bash -n与diff --check通过。Git Bash缺flock，实施者原shell验收被环境阻塞；主代理使用本机已有pgvector:pg18镜像，独占--rm、无网络/无Docker socket、仓库只读挂载、/tmp独立tmpfs，在真实flock与fake Docker下完整维护回归通过（Temp/ragkb-r2a-main-maintenance-linux-20261111.log），覆盖regression在跑及未跑、双目录恢复/回滚、故障关闭；首次tmpfs默认noexec造成fixture不可执行，改exec后通过，未改产品/安装依赖。真实promtool仍缺失。小主审发现bat用/i接受大小写却把原值传给严格argparse，原代理已在白名单命中时规范为固定小写；主代理亲读最终文件并重跑101 tests in 10.596s，OK/零skip（Temp/ragkb-r2a-main-final-20261111.log）。R2a接线通过、文件冻结；最终R2b后仍由原代理同步README/worker/harness/告警的阶段性缺口说明。
- 下游自包含任务卡已准备在Temp/ragkb-r2b-task-card-20261111.md与Temp/ragkb-r3b-task-card-20261111.md，仅待R1b主审通过后派发，不代表已实施。脱离生命周期启动脚本Temp/ragkb-r-launch-harness-20261111.ps1及有界owner/nonce/四角色/代理归属就绪检查Temp/ragkb-r-wait-harness-20261111.py已备好且语法检查通过，尚未启动新harness。
- R2合计范围：新backend/app/services/feedback_regression_executor.py和regression_worker.py；backend/worker.py、app/services/worker_status.py、app/schemas/operations.py、app/api/v1/operations.py（仅第四角色）、scripts/e2e_env.py、start-worker.bat、backend/.env.example（只说明新可选参数，不碰业务.env）；根docker-compose.yml、README.md中的角色/在线回归说明、DEPLOY.md与deploy/acceptance/README.md中现有三角色/停写服务说明（仅同步第四角色，不扩展验收或宣称真实soak完成）、deploy/common.sh、deploy/monitoring/prometheus/rules/ragkb-worker.yml.in、deploy/tests/test-maintenance.sh及其直接夹具；新tests/test_feedback_regression_executor.py和test_regression_worker.py，tests/test_worker_roles.py/test_worker_status.py/test_worker_shutdown.py/test_operations.py/test_e2e_harness.py直接相关断言。R1b模型/schema/job服务/用量配置接口与所有前端只读。如确有必要改模型替身，先报主代理，不扩大默认成功行为。
- 独立regression进程消费R1b持久job，只复用其claim/recover/heartbeat/load/publish/fail/release契约，不另造状态机或当前会话写入。worker.py动态导入run(stop_event)，默认ingest兼容。API/ingest/summary/project/regression各自独立，角色心跳不依赖任务状态。停止协作交回租约，取消/权限丢失/总超时关闭异步模型流，迟到结果不得发布；同步检索/短改写沿用现有有界网络时限，不持DB连接/锁等待。
- 实际执行复用followup.plan_retrieval、rag.retrieve/prepare_answer/answer_astream/finalize_citations、project_retrieval。每轮临时history仅含本job之前新答案及真实provenance，不采用旧答案；问题最多10轮。普通chat可重跑但无引用。固定upload manifest每次检索/补充/锚点均生效、SQL排名/LIMIT前约束，空manifest直接空检索，topic switch不能放开；项目始终原snapshot+可选固定file范围。不能借chat_stream/run_live_case创建用户会话或重放旧Turn。
- rag.retrieve的trace是阶段计数，不含candidate_sources；执行器应从实际返回pairs按顺序构建candidate_sources，并从prepare_answer裁剪后的prepared.pairs构建supplied_sources，使用answer_sources严格身份。保留planning_sources/history依赖并在发布处复核；rag.to_citations的公开project字段与原metadata字段名不同，不能混用。token/成本以独立job UsageContext关联全部调用，不能只读answer_astream最后generation帧。
- 整job总时限与每轮首正文/空闲/总时限有界，沿用StreamState的内容期限语义，不让heartbeat续正文等待；输出长度/最终JSON界由既有stream限制及R1b共同约束。权限/租约watch在模型等待期间真实运行，异常不能伪装成功，最终服务复核仍权威。新执行遇到空正文、流中断或明确finish_reason=length等截断，不能当成干净完成，返回安全执行错误并保留实际用量，补相应用例。
- 多轮来源接入使用answer_sources.build_source_snapshot生成source_snapshot_version/supplied_sources/history_sources，并放到每个临时assistant的source_provenance；planning_sources与最终prepare_answer.history_sources都须纳入真实传递依赖。不能只保留被引用的来源，也不能用旧消息ID去回读旧回答。后续发布验证可使用R1a已锁定根及binding、answer_sources.validate_sources(lock=False,binding=held)，不得在job锁后为新来源反取根锁。
- 第四角色端到端同步schema/status/安全码白名单/Prometheus既有role规则注释与真实任务计数、compose regression profile、同镜像/存储配置、本地bat角色参数（旧无参数ingest不变）、harness子进程manifest/nonce归属和维护写服务候选。不能硬杀旧PID、漏停profile写进程或用compose config --services代替运行标签识别。前端角色与E2E计数归R3，不越界修改。
- 必跑真实PG worker执行：成功新答案/引用与实际ModelUsage、无Conversation/Turn/Message变更、两次提交真实新执行、多轮新history、空/限定/新增文档不扩大、项目active切换不漂移、模型等待期间owner撤权/admin token/取消/超时/lease过期不发布、retry费用保留、停止及stale恢复。另跑worker/operations/harness模块、bash deploy/tests/test-maintenance.sh、fixture env只读compose config（不up业务容器）；真实promtool若仍不可用继续明确未测，不下载。独占库待派发，主代理亲审后再启动新owned四角色E2E。

### R3 页面重跑与人工比较任务卡（最终真实浏览器主验通过）

- 收口已完成：正式harness down按owner/nonce退出并生成Temp/ragkb-r-e2e-20261111-s8n4/STOPPED.json；owned应用容器已移除，原业务PG/Redis及既有专用测试容器仍运行。两共享标签页关闭、临时拦截移除，15张最终截图与日志保留。完整结果与证据索引：Temp/ragkb-r-browser-acceptance-20261111.md。没有commit/push、业务.env或依赖锁改动。

- 2026-11-11最终浏览器主验：Temp/ragkb-r-browser-20261111-s8n4-final.log，10/10通过、零skip、1.9m。验收对象为owner=r-online-20261111-s8n4的真实API/PG/四worker/生产dist；本机Edge，只有模型为回环替身。覆盖新调用与原历史不变、绑定人审和新job review=unknown、queued/running取消、批次原子409零入队与整批取消、创建回执丢失后的原UUID只读恢复、取消回执丢失后封锁旧CAS且GET恢复、写后挂起与迟到列表跨反馈隔离、执行失败与断言失败区分、来源禁用409隐藏正文/核验与恢复200、四角色运维，以及390px英文深色无横向溢出。主代理已实际查看最终取消/隔离/失败/恢复/窄屏等截图，15张原始图留在对应final目录。
- 主代理共享浏览器实际点击人审并收到PUT200；真实运行中job 4c768317-1ff4-46fb-8a57-8d7a1d09d20b从running/revision2经页面取消变cancelled/revision3。数分钟后仍result=null、比较409、实际usage为cancelled且未知token/cost保持null；切换另一反馈显示无作业且无前job残留。独立回执Temp/ragkb-r-manual-browser-20261111-s8n4-receipt.json记录最终服务端事实。共享CDP中途一次超时未算通过证据，最终全套使用Edge复跑通过。
- 验收期间修正的是E2E契约：原窄修者修可见radio/textarea定位、stub.payload.user_head和真实outcome文案。主代理按小修例外直接改了该spec内单行文案、RAG反馈前提改核对user气泡、治理helper三处knowledge-bases路径、来源禁用后关闭重开比较以触发真实GET（健康态没有错误重读按钮）；每项均小于10行局部修正，未改产品实现、未删安全断言或使用force。最后typecheck:e2e通过，前端产品保持已验证的387项/生产构建版本；入口与最大JS均199686bytes。README三角色陈旧说明已纠正为四角色，明确job初始queued、review初始unknown。

- 2026-11-11 R3b首次静态交付后，主代理亲跑typecheck、前端387 tests/pass387/fail0/skip0、typecheck:e2e、build均exit0（Temp/ragkb-r3b-main-{typecheck,tests,e2e-types,build}-20261111.log）。尚未真实浏览器。主审同轮发回原a033716dfd1b56ac9：普通chat夹具使来源失效用例必然skip，须真RAG/文档；chat_fail_next=1会被两层重试恢复；length应执行失败而不是succeeded+passed=false，后者需正常生成+真实缺失短语断言；cancel未知且后续GET失败要禁用旧CAS，单条/批次均不能继续旧revision；写/恢复后的await重读期间切反馈/关闭要重核generation/controller并防finally覆盖新状态；原UUID未确认时优先只读恢复。文件仍归原R3b，新增真实时序E2E但不得提前声称跑过。类型/构建通过不代替这些边界验收。

- 2026-11-11 R1b主验冻结后已正式派R3b唯一实施者a033716dfd1b56ac9，读取Temp/ragkb-r3b-task-card-20261111.md与实际冻结schema/API；frontend/e2e/fixtures.ts现从R3a正式转交R3b，其余运维产品文件仍冻结。没有分配DB或旧环境访问权；先交UI/helper及typecheck/test/build/E2E类型检查，真实浏览器必须等主代理统一提供新owned四角色manifest，不得宣称已跑。

- R3a已由唯一实施代理a57da47caab386883交回，并经主代理静态验收通过：typecheck、typecheck:e2e、npm test 377/377（零skip）与build全部exit0；亲测HTML中type=module的真实入口199786 bytes，余214 bytes，全部单JS低于500000。Temp/ragkb-r3a-main-{typecheck,e2e-types,tests,build}-20261111.log为主验日志。第一次临时量测误取preferences.js后已修正，只采用模块入口数据。实际四角色浏览器尚待R2b，不计为运行验收。文件已冻结，可在R3b启动时正式转交fixture。独立接线：只扩展现有运维WorkerRoleName为regression、对应中英文标签及现有E2E角色清单/manifest类型，与R1b/R2a并行。不猜job接口、不启动旧三角色harness、不把静态测试当浏览器验收。其唯一所有权为frontend/src/api/operations.ts、src/views/OperationsView.vue（仅确需角色相关调整）、src/utils/operations.ts（仅确需角色相关调整）、src/i18n/messages/operations.ts、tests/operations.test.ts及e2e/operations.spec.ts、e2e/fixtures.ts、e2e/answer-feedback.spec.ts、e2e/project-memory-recovery.spec.ts、e2e/feedback-regression-acceptance.spec.ts中的精确角色断言、playwright.config.ts中的角色说明。没有后端或DB所有权。
- R3b只负责下列反馈作业前端与新增helper/E2E；R3a未交回前不动其文件，后续需要fixture扩展再明确转交。R3b仍等待R1b真实API主审冻结；真实四角色浏览器统一等R2b可运行后执行。
- R3合计范围：新frontend/src/api/feedbackRegressionJobs.ts、新components/FeedbackRegressionJobsPanel.vue及批量/比较子组件、新utils/feedbackRegressionJobs.ts、新i18n/messages/feedbackRegressionJobs.ts（仅由懒加载反馈组件引入）；既有FeedbackRegressionPanel.vue、views/FeedbackView.vue、api/index.ts（仅run类型兼容）、utils/feedbackRegression.ts（manual/online可导入判断与R1a/R1b新增稳定阻断码映射，新增中英文仍由反馈懒加载块提供）；api/operations.ts、OperationsView.vue、i18n/messages/operations.ts与utils/operations.ts的第四角色相关改动；对应helper测试、新e2e/feedback-regression-jobs.spec.ts、e2e/operations.spec.ts、e2e/fixtures.ts及answer-feedback/project-memory-recovery/feedback-regression-acceptance中精确的角色列表断言，playwright.config.ts仅角色说明。不改后端、全局main.css、依赖或构建门槛。
- 单条已纳入反馈可明确发起后台新执行、轮询状态、停止、查看历史job与新旧答案/实际引用/机器断言、人工verified/rejected/unknown并展示对应run/result指纹。用户可区分作业执行完成但断言未通过、执行失败与人工尚未核验。已知token/部分测量与未知费用分开，不把空值当0，不声称机器断言等于语义准确。
- 列表批选最多20条；提交前读取各反馈当前regression revision，未纳入/失效应明确列出并阻止提交，不能隐式替用户填写原因分类。批次原子失败不给任何项“已入队”；成功后展示实际批次/作业与批量取消。刷新/筛选/分页/关闭/切反馈时清旧选择、revision与正文，AbortController+generation隔离迟到响应；未知写失败使旧revision失效，先重读，不自动重放写。
- 原manual准备JSONL/导入保持；online run不出现人工导入入口，active job时manual准备按钮相应禁用且409能恢复。新scope/project的manual live不支持应清楚显示后端能力/稳定理由，不假称可查最新全库。原权限错误或比较不可用时立即收起旧答案/引用及人工核验按钮。
- 新大段翻译只进懒加载反馈块，入口≤200kB、每JS≤500kB、业务页继续动态导入；不得降低门槛或破坏main.css既有本机字体。第四角色中文/英文/深色/390px可读，状态图标+文字，不新造图表。主代理核对当前package.json的npm test已使用tests/*.test.ts通配，新增helper测试可直接纳入，不需要为技能历史提示改包脚本或依赖。
- 必跑typecheck、全部npm test、build、typecheck:e2e。新E2E正常成功用真实API/四worker/本地模型替身，不mock业务成功；故障可阻断网络，CAS用真实并发API。覆盖新执行与原Conversation历史不变、实际新旧比较及人工确认不继承、queued/running取消、批次成功/一条冲突整体失败、价格未知、权限/资料失效、网络失败后不回填旧正文/恢复、原manual导入兼容、第四角色运维与窄屏英文深色。R2冻结前不自行启动旧harness声称最终验收；主代理最终亲跑并查看真实截图。
- 真实E2E的确定性路径：让一个已观察到模型请求的慢job占住唯一regression worker，再提交另一反馈的job并验证queued取消，随后验证running取消，不靠抢2秒轮询窗口或停业务进程。批次CAS冲突可暂挂实际POST，在另一真实API调用推进其中一条regression revision后继续原POST，确认整批零入队。未知写失败可先让真实POST提交，再阻断浏览器收到的响应，确认只发一次写请求、刷新读回真实job；不能mock成功或直接伪造409。
