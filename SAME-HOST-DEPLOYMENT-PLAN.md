# 单机准生产 / 生产部署隔离计划

## 目标与验收标准

在一台 Linux 云服务器上运行 staging 与 production 两套独立部署。保留现有根 `.env` + `backend/.env` 的单环境部署兼容性，不修改业务接口、数据库结构或页面。

- 两套渲染出的 Compose project/network、数据库与 uploads/projects/logs/backups 宿主路径、应用 env_file 和发布端口不同。
- 同一环境的 API 与所有 worker 使用相同版本的后端镜像与应用配置；前端镜像也显式指定版本。
- 生产默认保留公网入口，准生产默认绑定 127.0.0.1:18080；提供 SSH 隧道验收说明。
- 共享宿主覆盖配置为全部服务设置运行时 CPU、内存和 Docker 日志上限；资源值可配置，不承诺这些限制等于全站容量或构建资源限制。
- 统一入口必须显式选择 staging 或 production；未知/缺失环境、复制错环境身份、共用或嵌套的数据路径等在操作前拒绝。
- 初始化、Compose 操作、备份、恢复、异地备份及监控渲染沿用选定环境，不回退到根配置；维护只停启该环境实际运行的 writer。
- 新增隔离回归，旧维护/异地备份/恢复演练守卫回归仍通过；用真实 Compose CLI 渲染两套合成配置验证隔离。
- 不使用真实 API Key，不连接远程机器、不发布、不提交或推送。

## 方案与接口

1. 基础 Compose 增加 BACKEND_IMAGE、FRONTEND_IMAGE、BACKEND_ENV_FILE、WEB_BIND_IP 的兼容默认值；HTTPS 发布地址同样可选。
2. 新增 docker-compose.host.yml 作为两套同机部署共同的资源限制覆盖文件。
3. 配置模板放在 deploy/environments/{production,staging}/，每环境包含 .env.example（Compose/维护变量）与 backend.env.example（应用变量）。实际 .env、backend.env 均不纳入版本控制。
4. 新入口：bash deploy/env.sh <production|staging> <init|compose|backup|restore|remote-backup|render-prometheus|render-alertmanager> [参数]。
5. 新增 deploy/environment.sh 供入口加载与验证选择的环境；RAGKB_ENV_ROOT 可指向测试/管理员维护的独立配置根。默认配置根为 deploy/environments。
6. 新增 deploy/compose-command.sh，提供 ragkb_compose()：显式使用 ENV_FILE、COMPOSE_PROJECT_NAME 及冒号分隔 COMPOSE_FILE；不打印配置中的密钥。维护调用统一通过此函数。环境模板 COMPOSE_FILE 默认 docker-compose.yml:docker-compose.host.yml，可追加既有 HTTPS/监控/告警覆盖。
7. deploy/init.sh 接受 ENV_FILE 与 BACKEND_ENV_FILE，默认行为仍为旧路径；所选文件不存在时仅从相应模板生成占位文件并要求填值，不覆盖已有文件。
8. 两套应用继续 ENVIRONMENT=production，环境身份由部署入口与独立配置表达。

## 子任务、依赖与文件所有权

### A：隔离部署入口与 Compose

可写：docker-compose.yml、docker-compose.https.yml、docker-compose.host.yml（新）、deploy/env.sh（新）、deploy/environment.sh（新）、deploy/compose-command.sh（新）、deploy/environments/**（模板）、deploy/tests/test-environments.sh（新）、deploy/tests/environment-*（新测试辅助文件，如需要）、.gitignore、.github/workflows/checks.yml。

负责环境身份、配置与路径隔离校验、命令派发、镜像/端口参数、资源限制、新回归与 CI。不得修改真实配置/运行时数据。

### B：环境感知的初始化与维护

可写：deploy/init.sh、deploy/common.sh、deploy/backup.sh、deploy/restore.sh、deploy/tests/test-maintenance.sh、deploy/tests/fake-docker.sh、deploy/tests/test-remote-backup.sh。

负责遵循 ENV_FILE/BACKEND_ENV_FILE 与 ragkb_compose() 的维护兼容性、输出可辨识目标环境、双 project 停写选择回归。不改备份格式和既有确认语义。

A 与 B 按上面固定接口并行；共同文件不得交叉编辑。B 的最终集成验证依赖 A 的 compose-command.sh 落盘。

### C：部署手册

A/B 完成并审查接口后再分派。可写：DEPLOY-SAME-HOST.md（新）、DEPLOY.md、DEPLOY-FROM-SCRATCH.md、README.md、.env.example、backend/.env.example。只记录实际存在且经过验证的接口，提供配置、构建、验收、上线、备份恢复和现有单环境迁移步骤。

## 验证与主代理审查

- 主代理查看全部 diff（含新增文件）、检查文件所有权及实际命令输出。
- bash 语法检查涵盖 deploy、monitoring 与 tests 脚本。
- 在临时 Linux 容器中运行新环境测试以及 test-maintenance.sh、test-remote-backup.sh、test-drill-guard.sh；替身 Docker 不接触当前服务。
- 用宿主真实 Docker Compose 渲染 legacy、production、staging（包含所有可选 worker），断言 project、mount、ports、镜像、env_file 和资源限制。
- 新增入口不得将全量配置/凭据输出到日常日志；config 仅管理员显式请求时输出，文档提示含密钥。
- 如修改影响恢复路径，运行现有真实隔离恢复演练。纯部署改动不运行无关前后端业务测试与浏览器验收，汇报说明范围。

## 风险与回滚

- 同机共享 CPU、RAM、磁盘、Docker 守护进程和宿主故障；运行时配额不限制镜像构建，重任务错峰执行。内存限额不是预留，启用全部角色前重新核算预算。
- 镜像按不可变版本发布，准生产验收后生产引用同一产物；应用升级涉及数据库迁移时，旧镜像不能替代数据库恢复。
- 不自动接管现有 rag-kb 容器或移动真实数据。迁移须先完整备份、记录版本，在维护窗口停止旧 writer 后显式恢复到新生产环境，验收通过才退出旧部署。
- 原单环境命令继续有效。新增文件可撤回，实际环境配置和数据不由本次代码变更删除。

## 2026-11-15 首轮主审与修复任务

主代理已亲跑 shell 语法、diff 检查、四套独占无网络 Linux 容器回归、宿主真实 Compose 环境回归及实际入口 core/full 四套配置渲染。真实隔离 PostgreSQL 三件套恢复 exit 0（25.937 秒），独立 SQL/归档/文件/blob/双篡改复核通过，owner/project/完整 ID 核验后资源清理为 0；结论仅 local-synthetic。

首轮结论（历史记录）：需修复，当时尚未完成最终主审。下方记录最终修复与验收结果。

A 的本轮修复范围沿用原所有权，允许额外新增 `docker-compose.host.monitoring.yml` 与 `docker-compose.host.alerting.yml`（如确有必要），用于仅在对应可选覆盖启用时补齐资源限制；不能因此让默认核心部署自动创建监控/告警服务，也不能悄悄改变 legacy 单环境配额。

- 完整叠加 monitoring/alerting 后，Prometheus 和 Alertmanager 缺少 CPU、内存和 Docker 滚动日志上限，需补齐并用真实 Compose 断言两环境的全部服务。
- HTTPS 启用但未配置 RAGKB_ACME_WEBROOT 时，两环境共用 checkout/data/acme；同机入口需派生本环境独立绝对路径或要求显式独立路径，保留 legacy 默认行为，并覆盖跨环境冲突负例。
- 新增回归应从实际环境入口加载所选模板渲染，不只手工复制一组环境变量。纯核心与各可选覆盖组合都要能渲染；缺少真实 CLI 的容器回归明确标注 skip，主代理另用宿主 CLI 补验。

首轮审查时，入口/Compose、维护集成及文档三个只读审查分别进行；文档审查返回后由主代理核实建议。其余两项只读审查和限定实施代理被中断并标记 stopped，没有完成报告。用户要求继续后，恢复原代理的调用被工具明确拒绝，且要求不得在没有用户明确请求时重开代理。主代理因此接手限定修复、补读代码与 diff 并亲自验收；这是对原“主代理不写跨文件实现”角色流程的偏离，不能算作原审查代理已完成。

未跟踪的 `pelican-bicycle.html` 不属于部署文件所有权，保留原样，不自动删除或纳入交付。仍禁止提交、推送、真实配置/数据库/模型、远程主机和部署发布。

## 2026-11-15 最终本地主审

结论：通过本轮本地合成部署验收；代码、模板、手册与回归已完成，未执行真实 production/staging 启动或生产上线。验收不代表真实业务容量、生产备份可恢复性或 RTO/RPO 达标。

已完成修复：

- 两份专属 host 覆盖仅在对应监控/告警覆盖启用时自动追加，补齐 Prometheus、Alertmanager 的 CPU、内存与 json-file 滚动日志上限。默认核心服务集合及 legacy 加载方式保持兼容。
- 同机 HTTPS 的缺省 ACME 路径派生为本环境 DATA_DIR/acme，且两环境的有效默认值均参与跨字段路径冲突检查；legacy 默认路径保留。
- Alertmanager 缺省端口与既有覆盖的 19093 对齐；端口以十进制数值比较，拒绝 80/080/00080 等重复配置。
- 手册修正初始化入口、环境变量传递、可选输出/secret 目录、cron PATH、资源预算及旧单环境导航。通用 backend.env ignore 规则覆盖仓库内自定义配置根。

主代理已亲读最终输出：

- review-3：全部 shell 语法、git diff --check 及四套独占无网络 Linux 回归均 exit 0。符号链接负例在 Linux 实际执行；容器缺真实 Compose CLI 的两段明确 skip。
- host-4：最终代码经宿主真实 Compose CLI 完整环境回归 exit 0（174.870 秒）。legacy 兼容，以及 production/staging × core、https、monitoring、alerting、full 共十份实际入口配置均通过；含所有可选 worker，逐服务核对配额/日志、镜像、应用配置、数据库、路径、网络与端口隔离。Windows 的两项符号链接测试由前述 Linux 回归补验。
- render-3：另存 core/full 四份独立实际入口渲染，全部 missingLimits=[]，两环境 ACME 独立。宿主 Windows 的路径映射仅属 config 渲染观察，没有启动相应部署。
- 真实隔离 PostgreSQL 恢复演练：数据库、uploads、projects 三件套恢复及独立逐列 SQL/归档/文件/blob/篡改负例复核通过；完整 ID、owner 与 project 核验后清理本轮容器/卷/网络，既有容器状态未变。结论仅 local-synthetic。
- 实际 render-prometheus/render-alertmanager 入口及 --check：两环境合成 secret、独立输出、只读副本字节与权限通过；跨环境输出路径在写入前拒绝且原输出字节未变。无网络、无 Docker socket；promtool/amtool 因镜像不可用明确跳过，未验收真实告警 firing/resolved。
- 实际 backup/restore/remote-backup 入口：在独占无网络容器中使用 Docker/rclone 替身，两环境均完成调用、产物与所选配置检查；只停启本 project 的 API 与全部四类 worker，另一 project 未被停启。该项不是实际远端上传或双环境 Docker 运行验收。

驱动路径转换、mem_limit JSON 类型与恢复独立复核器的路径分隔符失败均保留原始记录；不能将这些驱动失败算作产品通过，也未抹去首轮已确认缺陷。最终证据索引和本地主审报告保留在本机临时验收目录，未纳入仓库。

交付应包含入口/loader/Compose 命令模块、三份 host 覆盖、两环境模板、维护改动、回归与 CI、手册，不能漏掉两份新可选 host 覆盖后单独提交 loader。无关 HTML 保持原样。纯部署改动未重复无关前后端业务测试或浏览器验收；未提交、推送、触发云端 CI、安装依赖、调用模型或操作业务配置/数据库。
