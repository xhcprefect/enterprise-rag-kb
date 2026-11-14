# 后续可靠性与业务验收实施计划

基线：origin/main fb1f759；2026-11-13开始实施，2026-11-14完成本地验收。初始实施阶段未提交、推送或合并；2026-11-14用户进一步授权执行正式交付：提交、推送、创建PR、跟进云端CI，检查通过后合并。远程结果以对应提交的实际检查为准。不修改业务 .env，不连接业务数据库，不调用真实/付费模型，不安装本地依赖；沿用规划、实施子代理、主代理审查验收流程。

## 顺序、目标与完成证据

1. **P0 摘要来源标签与 CI 失败**：定位 main CI #38 的摘要 empty facts。已用固定数字段 UUID 复现：正文凭据清洗误伤 message_id 的数字段，字母 UUID 对照正常。修复真实来源标识的保留，正文凭据仍清洗；固定回归必须在旧代码失败、新代码通过，摘要真实应用发布/查看/重置通过。main 云端 CI 仍需要后续提交推送后复验，本地通过不冒充 main 已全绿。
2. **P1 worker 数据库故障恢复**：可恢复领取/恢复扫描错误有界退避、不结束消费循环，非恢复错误明确退出。停机可及时打断退避，已领取任务的租约与任务失败预算不变。验证一次故障后恢复消费、持续故障不忙循环、停机、不可恢复错误。
3. **P1 管理员列表响应乱序**：用户/审计列表由最新请求拥有数据及 loading 写权；筛选/翻页/离开时隔离旧响应，错误恢复可用。用可控响应顺序回归和真实应用浏览器成功/失败路径、截图验证。
4. **P1 真实业务质量基线**：核查既有模板与严格模式，先准备可执行的私有样本采集、标注和结果核验任务卡。真实问题、语料授权、业务目标与可调用环境缺失时标为待外部输入，不伪造业务通过，不擅自真实调用。
5. **P1 部署恢复与告警证据**：复用现有工具准备版本/环境/RTO/RPO/告警/ACL验收任务卡，可做独占合成环境验证；缺真实部署、备份与接收器时保持 unknown，不操作现有生产资源。
6. **P1 容量及24/72h持续运行**：准备目标、采集与退出条件，复用现有有界工具；缺目标规模与独立环境时保持待执行，不放宽硬上限或把短回归当soak。
7. **P2 审计事件目录**：补齐成员及在线回归事件筛选/中英文名称，增加契约覆盖；记录保持存在，前端可筛选，真实浏览器验证。
8. **P2 体积与CI环境维护**：按页面拆分翻译，保留入口200000字节/单JS500000字节及登录依赖门禁并得到余量；已核main #38及官方发布记录：workflow重复出现Node 20弃用和ubuntu-latest迁移至Ubuntu 26通知，当前失败不是已证实的runtime兼容故障。Action升级与runner镜像迁移分开评估验证；项目Node22/Python3.12版本保持不变，检查artifact隐藏文件实际上传内容，主代理验证构建与工作流，不以本地检查冒充云端CI。

## 当前任务卡与所有权

| 任务 | 唯一写入所有者 | 可写文件 | 前置与验证 |
| --- | --- | --- | --- |
| P0 | 已冻结，主审与真实浏览器通过 | backend/app/services/memory_prompt.py；memory_worker.py仅传递可信来源身份；backend/tests/test_memory_prompt.py、test_memory_worker_prompt.py、test_e2e_harness.py；frontend/e2e/project-memory-recovery.spec.ts仅摘要用例 | 120后端测试、原失败摘要E2E本地1/1、桌面/390px创建查看重置通过；main云端未重跑 |
| P1 worker恢复 | 已冻结，主审通过 | backend/worker.py；新backend/tests/test_worker_database_recovery.py；必要时backend/tests/test_worker_shutdown.py；README.md的worker故障恢复说明 | 主代理关联回归及完整PG回归通过，平台跳过明确记录 |
| P1列表竞态 | 已冻结，主审与真实浏览器通过 | frontend/src/views/UsersView.vue、AuditView.vue；新utils/adminList.ts与tests/admin-list.test.ts；新e2e/admin-lists.spec.ts；api/index.ts仅userApi.list增加signal | 417前端测试、两类typecheck、build、六个真实E2E与桌面/390px截图通过 |
| P2审计事件目录 | 已冻结，主审与菜单/真实筛选通过 | AuditView.vue事件映射；新auditActions.ts、audit-actions.test.ts和test_audit_action_catalog.py | 421前端+2后端纯契约通过；14次双语真实筛选通过；无新事件历史行，行渲染只作实际函数契约验证并记录限制 |
| P2运维文案按需加载 | 已冻结，2026-11-14主审与真实浏览器通过 | messages/index.ts、operations.ts、OperationsView.vue仅翻译入口、i18n.test.ts、operations.test.ts | 84条文案逐字保留；独立424前端测试/两类typecheck/build/diff检查通过；入口196228 bytes/余3772；首次进入、双语、网络失败/刷新及390px通过，邻接8项真实E2E通过 |
| P2 CI Action运行时 | 已冻结，独立静态主审通过 | .github/workflows/checks.yml仅10处uses版本 | checkout7.0.1/setup-node7.1.0/setup-python7.1.0/upload-artifact7.0.2；PyYAML全文结构对照仅版本变化；项目版本/门禁/权限/产物白名单保持，云端未执行 |
| 计划与验收证据索引 | 主代理 | 本文件；仓库外本轮临时目录 | 不写实现代码 |

所有本地长期服务采用脱离工具生命周期的PowerShell启动；使用现有e2e_env.py分配独占数据库与端口，先核对owner/nonce再使用或清理。后端全量测试使用另一独占测试库，设置TEST_DATABASE_URL/TEST_DATABASE_OWNER并通过共享gate，不能与应用环境并发清空同一库。

## 回归与风险控制

- 摘要修复不能将伪造标签、任意UUID样式正文当作可信元数据；不能对整个正文关闭凭据清洗。新实现仍执行既有来源白名单校验。
- worker只对明确可恢复故障退避，不能吞掉编程/配置异常；保留取消、信号、停机及租约隔离。
- 列表和翻译改动保持权限、双语、路由懒加载与原交互；未知错误不得继续显示属于旧请求的数据。
- 最终前端typecheck、全量npm test、build、typecheck:e2e；后端相应模块及完整PG回归；真实前端改动需实际浏览器操作并亲自看截图。
- 发现越界或验收失败交回原实施者；仅撤销本轮精确改动，保留用户和其他任务修改。代码回退不能清库或恢复整个工作区。

## 当前状态

- 已创建feat/reliability-acceptance，实施前工作区干净。
- P0：实施与主审通过。后端指定3模块120项通过，E2E类型检查通过；独占PG/API/四worker/生产前端的本机Edge实际创建、查看、取消重置、网络故障重置失败后恢复、成功重置通过。主代理已查看截图，确认事实内容与来源ID可见，重置后版本为null且页面显示尚无摘要。浏览器证据位于本轮harness输出目录acceptance/p0；只注入重置请求网络失败，成功数据来自真实API。
- P1 worker恢复：实施与主审通过。首轮14纯测、PG相关6模块88项后，主审额外复现真实错误密码sqlstate=None被误重试，已由原实施者修正为保守分类并说明libpq诊断限制。最终主代理独立6模块93项（91通过，Windows不支持POSIX SIGTERM的2项明确跳过）；真实错误密码立即退出、无凭据日志、真实连接不可用诊断、事务回滚、提交确认未知与租约隔离均通过。后端全量PG discover已由主代理独立完成：2169项，2166通过/3项平台跳过（1个POSIX目录符号链接、2个真实SIGTERM进程），492.104秒，exit0；没有失败。原始stderr在仓库外ragkb-rel-full-final-20261113-8d42.log.stderr，专库其它连接数在退出后为0。
- P2 审计事件目录：主审通过，34类实际后端事件已全部显式映射；新增7类双语按审计页懒加载，未知事件保持原值。此阶段主代理421前端测试、2后端纯契约、typecheck/typecheck:e2e/build、审计3项E2E通过；实际14次双语筛选HTTP200、桌面/390px菜单截图已查看。隔离资源无新增7类事件历史行，未造数据/执行额外成员或回归操作；真实行与未知行截图缺口明确，行标签/回退由执行实际代码契约覆盖。证据为2026-11-13独占环境acceptance/audit-catalog/acceptance.md；此阶段入口199742/余258，最终入口见下方2026-11-14结果。新增2项后端纯契约也于2026-11-14单独复跑通过，不与此前2169项全量混算。
- P1列表竞态：主审及真实浏览器通过，代码冻结。此阶段独立前端417/417、typecheck、typecheck:e2e、build、diff检查通过；新增E2E首轮5/6，修正下拉组件点击目标后主代理6/6通过（32.3s）。主代理另行真实操作users/audit/mobile成功、乱序、失败刷新和语言切换并亲自查看截图；390px下documentWidth/mainScrollWidth均390。此阶段入口199742 bytes/余258，证据为2026-11-13独占环境acceptance/admin-lists/acceptance.md；最终文案拆分后六项再次通过，见下方。其他P1真实业务/生产/soak执行卡已准备，缺外部输入保持待执行。
- P2 运维文案：2026-11-14完成主审及真实应用验收。中英文各42条文案与原目录逐字一致，严格静态键检查覆盖懒加载目录、条件字面量、实际动态状态与失败键；真实composer首次注册、重复调用幂等、locale切换通过。主审发现持久测试依赖旧HEAD源码格式会在提交后失败，已由同原实施者改为自包含键集/文案摘要并重新冻结。主代理独立424/424前端测试（0失败/跳过，9679.236ms）、typecheck、typecheck:e2e、build与git diff --check通过；入口及最大JS为196228 bytes，较199742减少3514，距200000余3772；仍高于建议195000共1228，未放宽硬门槛。55个JS分包，运维/审计保持动态入口，入口/登录静态图无两个页面文案目录及受限重依赖。
- 2026-11-14最终浏览器：全新独占PG/API/四worker/回环替身/生产前端，首次UI进入运维页、四角色真实运行数据、中英文切换、一次GET网络失败清旧数据、错误文案切语种、刷新恢复、390×844通过；主代理查看5张截图。随后运维2项+管理员列表6项真实E2E全部通过（37.0s，0失败/重试/跳过），英文深色390px及列表恢复截图已查看。成功数据来自真实API；一次网络中断及延迟真实响应仅用于故障/竞态验证。证据为2026-11-14独占环境acceptance/operations和acceptance/final-regression；14个冻结实现/测试文件指纹在主验前后均一致。
- P2 CI Actions：10处固定版本更新静态主审通过，PyYAML对照基线仅允许指定uses变化，2026-11-14复核通过。Node22/Python3.12、测试命令、owner/清理门禁、权限、产物路径白名单与保留期不变。Action自身Node24运行、托管runner版本和实际artifact上传仍待云端验证。
- 隔离资源：2026-11-13后端测试专库已按精确ID清理。旧应用环境恢复时report为failed且无STOPPED收据，旧容器已不存在、复用的无关PID未触碰，失败历史和证据保留。2026-11-14新应用环境按owner/nonce执行down返回0，STOPPED精确归属、report=stopped、原容器不存在；七个子进程与orchestrator均已退出，web/API/stub端口均ECONNREFUSED，全部验收材料保留。没有关闭Docker或清理其他资源。
- 云端main CI #38原记录：backend/frontend/maintenance成功，browser32/33通过；summary worker publishes and resets real conversation memory失败。本地相同Playwright摘要用例随后1/1通过。本地验收完成时尚未提交推送；以下记录2026-11-14正式交付后的实际结果，前文“待云端”保留为当时的阶段状态。

## 2026-11-14正式交付与main复验

- db2e427提交完整可靠性修复；首次云端前端423通过/1失败，暴露operations SSR测试依赖主机默认语言。原实施者只固定fixture初始zh-CN并在finally恢复，6af3550提交该三行修复，产品默认语言逻辑保持。主代理独立模拟en-US和zh-CN，各运行7项运维测试及424项前端全量，均无失败或跳过。
- 准确6af3550的push和pull_request两组共8项检查全部成功，PR #4合并为main提交f567994b43eb98bc5312483621e6e8116f267932。合并后main独立run43557201762四项检查全部成功；实际日志后端2171项、179.937秒、OK(skipped=17)，17项为Linux上的Windows专属检查，目录symlink及两项真实SIGTERM实际通过。前端424/424、0失败/跳过；完整浏览器39/39、8.0分钟，原摘要发布/重置用例通过，harness正常清理。
- 实际runner2.338.2、Ubuntu24.04镜像20261111.127.1，固定Action版本及其Node24运行时已在云端执行。main诊断包14098864144共89个文件：71个.artifacts、8个.report、10个harness日志/报告，均在既有白名单内，没有manifest、env、browser-state或token/secret命名文件。该核验覆盖归档路径与文件名，不等同于逐文件正文秘密扫描。
- 原feature分支保留。准确SHA检查报告、原始日志摘要、实际artifact目录核验和本地证据均保留在仓库外；不会为阶段时间状态反复新增提交。

## 额外隔离恢复演练

- 首次运行现有restore-drill.sh --isolated --keep失败，exit2：默认socket pg_isready在bootstrap临时PostgreSQL上提前报告healthy，首个psql遇到临时服务关闭而得到database system is shutting down。该次不计通过，失败report与日志保留。
- 失败owner drill-20261114121624-1989-246724248的完整容器ID、卷和网络先核对owner/project，再精确删除，项目资源0残留；无关容器未触碰，仓库外失败现场目录保留。drill.env只含合成凭据，不提交或上传。
- 后续修复独立分支fix/restore-drill-readiness：deploy/tests/drill-compose.yml仅将pg_isready改为显式-h 127.0.0.1，保留现有超时、标签、端口与卷；test-drill-guard.sh新增读取实际probe的三阶段回归。主代理独立8项守卫通过，旧socket配置在bootstrap阶段被准确拒绝、exit1，新TCP配置bootstrap/shutdown失败而ready成功。执行参数经shlex解析后subprocess(argv)运行，不用eval。
- 主代理用两个全新owner/空数据卷实际执行恢复，两次均exit0，完整脚本耗时18.015秒和20.519秒（含启动/备份/恢复/校验，不能当production RTO）。每次25行逐列一致、4文件SHA256一致、单字节篡改检测有效；另外独立查询恢复后的owner及25行、重算源文件SHA256，并核对恢复文件仅预期篡改项多一字节。第一次额外核验器漏识别Windows sha256sum二进制标记，已修仓库外核验器并对保留证据复核通过；原诊断保留。两次容器/卷/网络均按精确ID与owner清理，项目0残留，原始报告和归档留仓库外。
- 该合成脚本覆盖测试数据库及uploads，未覆盖production备份、RTO/RPO或projects文件归档。真实业务质量、生产恢复/告警/ACL、容量与24/72h持续运行仍按ACCEPTANCE-EXECUTION-PLAN.md待外部输入，离线/零网络预检不计实际通过。
