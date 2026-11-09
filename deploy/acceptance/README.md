# 部署验收：只读检查与人工证据

本指南配合 `backend/scripts/verify_deployment.py` 使用。工具只做**有界、只读的配置校验与 GET 检查**，并校验一份由部署方填写的人工证据清单；它不修改目标服务或部署，只在显式指定时写本地报告与模板，也不替代真实业务验收。

## 1. 目的与边界

工具能做的：

- 默认 **dry-run**：只校验参数、URL 与本地输入/输出路径，**零网络请求**。
- `--live`：仅发起固定数量的只读 GET，检查 `/health`、运维接口、TLS 以及可选的 Prometheus/Alertmanager 就绪状态。
- 读取 `ragkb-deployment-acceptance/v1` 证据 JSON，校验声明的结构、`status`/`level` 枚举、必需 `kind`、production 声明的 `ref`（非纯空白）与 `captured_at`（可解析的 ISO 日期/时间），并在提供 `--expected-version` 时比对 `environment.version`。
- 输出结构化报告，逐项标记 `pass` / `fail` / `unknown`。
- 仅在显式给出 `--out` / `--emit-template` 时写本地文件；父目录必须已存在，默认拒绝覆盖已存在文件与 symlink。

工具**不能**做的：

- 不替代恢复演练、监控/告警回归或真实业务验收。
- 不执行恢复、重启、停机、故障注入、发送通知或修改 ACL/成员权限。
- **不自动核验**跨证据项的环境/版本一致性、时间顺序、观测时长或材料真实性；这些仍需人工复核。
- 不跟随重定向、不重试、只发 GET；超时与响应大小均有上限。
- 不读取 `.env`，不登录，不调用模型，不安装依赖。
- 不打印令牌、证据引用（`evidence[]`）、`notes` 或上游响应正文。

无论检查结果如何，报告中的 `acceptance` 恒为 `false`，`human_signoff_required` 恒为 `true`。**工具检查通过 ≠ 真实业务/生产验收通过**；未提供真实证据的项不能写成 `pass`，缺项保持 `unknown`，声明为 `fail` 的保持 `fail`。

## 2. 前置条件

| 项目 | 说明 |
| --- | --- |
| 目标 origin | 例如 `https://rag.example.com`；只接受 origin（不含路径、查询串、片段或 userinfo）。非回环地址必须 HTTPS，回环地址允许 HTTP。 |
| 部署版本 | 明确的版本号、镜像 digest 或提交号，用于填写证据中的 `environment.version` 与 `version_identity`。`/health` 不返回版本，不能从它推断。 |
| 环境 | 生产、预发布或本地等环境的唯一标识，以及部署时间与操作者。 |
| 管理员令牌 | 放在环境变量中，默认变量名 `RAG_DEPLOY_ADMIN_TOKEN`，供 `/api/v1/operations/summary` 使用。变量名可用 `--admin-token-env` 覆盖。 |
| metrics 令牌 | 放在环境变量中，默认变量名 `RAG_DEPLOY_METRICS_TOKEN`，必须与服务端独立配置的 `METRICS_TOKEN` 一致；管理员 JWT 不能替代。变量名可用 `--metrics-token-env` 覆盖。 |
| 可选就绪地址 | `--prometheus-url`、`--alertmanager-url`，只接受 origin；CLI 自动拼接并访问各自的 `/-/ready`。未提供时对应检查为 `unknown`，不是通过。 |

令牌只从环境变量读取（`--admin-token-env` / `--metrics-token-env` 只用于指定变量名，CLI 不接受直接把令牌值写在命令行），不要写进配置文件、仓库或证据文件。工具不会自动读取 `.env`。

## 3. 快速开始

以下命令从仓库根目录执行，示例域名 `https://rag.example.com` 仅作占位。

### 3.1 dry-run（零网络）

```bash
python backend/scripts/verify_deployment.py \
  --base-url https://rag.example.com \
  --json
```

dry-run 校验 URL、参数与本地输入/输出路径，报告中的 `target.origin` 是规范化后的目标 origin，`network_requests` 为 `0`，8 项 live 检查因未执行而均为 `unknown`；它不会解析域名或发起 HTTP。退出码 `0` 只表示本地配置校验通过。

### 3.2 live（只读 GET）

先在本机安全终端或密钥管理器中设置令牌（示例不写令牌值）：

```bash
export RAG_DEPLOY_ADMIN_TOKEN='<在终端中设置>'
export RAG_DEPLOY_METRICS_TOKEN='<在终端中设置>'
```

首次 live 前先运行 dry-run，确认报告中的 `target.origin`；`--confirm-target` 必须与 `--base-url` 规范化后的 origin **完全一致**：

```bash
python backend/scripts/verify_deployment.py \
  --base-url https://rag.example.com \
  --confirm-target https://rag.example.com \
  --live \
  --prometheus-url https://prometheus.example.com \
  --alertmanager-url https://alertmanager.example.com \
  --expected-version 2026.11.08 \
  --out /srv/ragkb-acceptance/verification.json
```

- live 只做有限次只读 GET，不跟随重定向、不重试。
- `--prometheus-url` / `--alertmanager-url` 只接受 origin；CLI 自动拼接 `/-/ready`。带路径、查询串或其他非 origin 形式会在输入阶段以退出码 `2` 拒绝，且不发起请求。
- 未提供 `--prometheus-url` / `--alertmanager-url` 时，对应就绪检查为 `unknown`。
- 报告默认不包含令牌或响应正文；不要用包含敏感信息的 URL 作为就绪地址。

### 3.3 生成证据模板

仓库内的 `deploy/acceptance/deployment-acceptance.template.json` 是工具生成的**全 `unknown`** 模板。请复制到仓库外填写，不要把真实证据提交到仓库：

```bash
cp deploy/acceptance/deployment-acceptance.template.json /srv/ragkb-acceptance/evidence.json
```

也可以直接生成到指定位置。输出父目录必须已存在；默认拒绝覆盖已存在文件与 symlink，确需替换时加 `--overwrite`：

```bash
python backend/scripts/verify_deployment.py \
  --emit-template /srv/ragkb-acceptance/evidence.json
```

### 3.4 离线校验证据

```bash
python backend/scripts/verify_deployment.py \
  --evidence /srv/ragkb-acceptance/evidence.json \
  --expected-version 2026.11.08 \
  --json
```

不带 `--live` 时不会发起网络请求。工具只做声明结构、必需项与 ISO 日期/时间格式校验；材料是否真实、是否由独立人员复核，以及跨项是否属于同一环境/版本、时间顺序与观测时长是否合理，仍需人工确认。

## 4. live 检查项

固定 8 项 live 检查，名称与含义如下。

| 检查 | 证明什么 | 不能证明什么 |
| --- | --- | --- |
| `health` | `/health` 返回 `status: "ok"`，且可算出 worker 状态。证明 API 进程可响应。 | 不证明部署版本、模型可用性、检索质量或业务正确性。`status` 是静态字段；若数据库不可用导致接口报错，检查会失败。 |
| `worker` | 入库 worker 的心跳状态：`alive`、`stale` 或 `idle`。 | 不证明摘要/项目 worker 也在运行；`idle` 不代表 worker 存活。 |
| `metrics_auth` | 使用**本次提供的** metrics 令牌请求一次 `/api/v1/operations/metrics`，HTTP 200 即 `pass`。 | 不额外测试无令牌、错误令牌或管理员 JWT 的负例；这些鉴权边界由既有后端回归（如 `backend/tests/test_operations.py`）和人工验收覆盖。也不证明 Prometheus 已采集或指标内容完整。 |
| `metrics_database` | 指标响应中 `ragkb_db_available=1` 且存在队列指标 `ragkb_ingest_queued_documents` 时 `pass`。 | 数据库不可用（`0`）会 `fail`；缺失、不可解析或缺少队列指标为 `unknown`，**不能把缺失读成队列为 0**；也不证明队列长期健康。 |
| `admin_summary` | 管理员 JWT 可读取摘要，且 `degraded=false`、`database.status=ok`、`queue` 不为 `null`。 | 不证明业务数据正确、三个 worker 角色持续运行或告警已送达。 |
| `tls` | 目标 HTTPS 的证书链与主机名校验（按客户端默认信任库）。回环 HTTP 目标没有 TLS 可验证，记为 `unknown`。 | 不证明 HSTS、密码套件策略、反向代理/CDN 或内网链路配置。 |
| `prometheus_readiness` | 配置的 Prometheus `/-/ready` 返回 HTTP 200。 | 只证明进程就绪；不证明抓取目标 UP、规则求值成功或告警已发送。 |
| `alertmanager_readiness` | 配置的 Alertmanager `/-/ready` 返回 HTTP 200。 | 只证明进程就绪；**ready ≠ 通知送达**，不证明路由、接收器、重试或回执。 |

### 判定细则

- **worker**：`alive` 为 `pass`；`stale` 为 `fail`（有排队文档但无新鲜心跳，通常 worker 未启动）；`idle` 为 `unknown`（队列为空，无法观测 worker 是否存活）。若没有其他 `fail`，仅因 `idle` 等 unknown 导致退出码 `3`。
- **metrics_auth**：未设置 `RAG_DEPLOY_METRICS_TOKEN` 或变量值不可用时为 `unknown`。设置后 CLI 只用该令牌 GET 一次：HTTP 200 为 `pass`；`503`（采集未启用）、`401`/`403`（令牌无效）或其他状态、TLS/传输错误均为 `fail`。CLI 不测试无令牌或管理员 JWT 的负例。
- **metrics_database**：`ragkb_db_available` 必须存在且可解析为 0/1。`1` 且响应中存在 `ragkb_ingest_queued_documents` 队列指标为 `pass`；`0` 为 `fail`；指标缺失、无法解析或缺少队列指标为 `unknown`，不能猜测数据库健康。metrics 请求本身未成功时本项也是 `unknown`。
- **admin_summary**：未设置 `RAG_DEPLOY_ADMIN_TOKEN` 或变量值不可用时为 `unknown`。HTTP 401/403 为 `fail`。HTTP 200 不足以下结论：只有 `degraded=false`、`database.status=ok` 且 `queue` 不为 `null` 才是 `pass`；`degraded` 缺失/不为 `false`、`database.status` 不为 `ok` 或 `queue=null` 均判 `fail`，不能当作“队列为空”。
- **就绪检查**：未配置地址为 `unknown`；已配置时只有 HTTP 200 为 `pass`，其他状态、超时、连接失败或重定向均为 `fail`。`/-/ready` 成功不证明规则触发或通知送达。
- **超时/连接失败**：已配置的 HTTP 检查判 `fail`；缺令牌、未配置地址或 TLS 不适用（回环 HTTP）判 `unknown`。单次检查成功也不证明长时间稳定。

## 5. 证据清单

证据文件 schema 为 `ragkb-deployment-acceptance/v1`，顶层包含：

- `environment`：`name`、`version`、`deployed_at`、`operator`（工具只读取并回显这些顶层字段，不按证据项分别记录环境）；
- `items`：固定六项，每项包含 `status`（`pass`/`fail`/`unknown`）、`level`（`production`/`staging`/`local-synthetic`/`unknown`）、`evidence[]`、`notes`。每条 `evidence` 至少包含 `kind`、`ref`、`captured_at`，也可带 `sha256`。

工具会校验 JSON 结构、枚举值、必需 `kind`，以及 production 声明中每条 evidence 的 `ref` 非纯空白、`captured_at` 为可解析的 ISO 日期/时间；它**不自动核验**跨项环境/版本一致性、时间顺序、观测时长或材料真实性。

六项证据及最低要求：

| 证据项 | 至少需要的真实材料 |
| --- | --- |
| `version_identity` | `version` 证据（镜像 digest、提交号或构建产物标识），且 `environment.version` 非空且非纯空白。证明“本次验收的目标确实运行该版本”。 |
| `backup_restore_rto_rpo` | `restore_drill` + `checksum`：同一版本/环境下的备份点与恢复点、实测 RTO/RPO、数据库行数与文件 SHA-256 等校验结果、复核结论。 |
| `alert_delivery_firing_resolved` | `firing` + `resolved` + `receipt`：真实接收器收到的触发与恢复通知、唯一关联标识、接收回执与时间戳。 |
| `member_acl_allow_deny` | `allow` + `deny`：同一批真实成员/角色在指定资源上的允许与拒绝结果，含预期 HTTP 状态。 |
| `soak_24h` | `soak`：连续 24 小时观测记录，含起止时间、缺口策略、三个 worker 角色状态与异常记录。 |
| `soak_72h` | `soak`：连续 72 小时观测记录，要求同上。 |

### 等级与通过条件

- `pass` 必须同时满足 `level=production`、带有对应证据类别，且每条 evidence 的 `ref` 非纯空白、`captured_at` 为可解析的 ISO 日期/时间；缺少必需 `kind` 或声明不完整会判 `fail`。
- `staging` 或 `local-synthetic` 的 `pass` 会被工具降为 `unknown`，不能当作真实验收。
- **人工复核要求**所有证据项来自同一环境与同一版本，且时间戳为带时区的 UTC、观测窗口完整、按时间顺序、不晚于提交时间。工具只校验 `captured_at` 的 ISO 日期/时间格式，不核验跨项时间顺序、观测时长或未来时间；它也没有 per-item 环境字段，只会在 `version_identity` 达到 production 校验条件时比对顶层 `environment.version` 与 `--expected-version`。混用环境/版本、时间倒置、时长不足或未来时间不会被工具自动发现。
- `evidence[]` 是材料引用/标识，工具只校验类别与结构，不读取材料正文，也不会打印引用或 `notes`。**人工填写即人工担责**，不是工具独立核验。

## 6. 填写与判定

1. 复制全 `unknown` 模板到仓库外（见 3.3），先填写 `environment`，确保 `name`、`version`、`deployed_at`、`operator` 完整。
2. 逐项填写 `status`、`level`、`evidence[]`、`notes`。真实材料齐全且已复核才可填 `pass`；未取得材料填 `unknown`；观察到明确失败（如恢复校验不一致、通知未送达）填 `fail`。
3. 不要预填 `pass` 再补材料，也不要因为 CI 或隔离演练通过就填 `level=production`。
4. `notes` 不要写令牌、webhook URL、内部主机名或完整证据路径。
5. 使用 `--expected-version` 时，只有在 `version_identity` 已声明为 production 且证据完整的情况下，工具才比对 `environment.version`；不一致、为空或纯空白会使该项 `fail`。其他情况下不会触发该比较，不能反过来用期望值推断实际部署版本。
6. 人工复核六项是否来自同一环境/版本，并检查观测时间完整、有序且不晚于提交时间；工具不会自动发现这些不一致。

状态含义：

- `pass`：已取得规定的真实证据并通过人工复核。
- `fail`：有证据表明未达到要求（或同一环境/版本出现矛盾）。
- `unknown`：未取得、无法取得或证据不完整；**不得当作通过**。

## 7. 退出码与报告

| 退出码 | 含义 |
| --- | --- |
| `0` | dry-run 配置校验通过（未提供证据时，8 项 live 检查保持 `unknown` 也返回 `0`）；仅校验证据且六项均 `pass` 时返回 `0`，但 live 检查仍未执行；live 的全部检查与六项证据均 `pass` 也返回 `0`。仍不代表业务/生产验收通过。 |
| `1` | 任一检查或证据项 `fail`，或运行被中断/意外失败。 |
| `2` | 参数、URL、文件或 schema 输入错误，或报告/模板写入失败。输入错误在发起任何网络请求前返回；输出写入失败发生在检查之后，也可能返回 `2`。 |
| `3` | 没有 `fail`，但存在 `unknown`：live 模式下缺令牌、worker `idle`、未配置 readiness、缺少真实证据等；或提供证据的 dry-run 中证据项为 `unknown`。未提供证据的 dry-run 不因检查未执行而返回 `3`。 |

注意：仅校验证据时，六项证据均 `pass` 会返回 `0`，但 8 项 live 检查仍未执行（`unknown`），`summary.unknown` 不为 0；不能把退出码 `0` 理解为整体全部通过。

报告 schema 为 `ragkb-deployment-verification/v1`，包含：

- `mode`、`target.origin`（规范化后的目标 origin）、`network_requests`；
- 固定 8 个 `checks[]`：`health`、`worker`、`metrics_auth`、`metrics_database`、`admin_summary`、`tls`、`prometheus_readiness`、`alertmanager_readiness`，每项带 `status` 与 `reason`；
- `evidence.environment`、`evidence.items`（固定六项）、`evidence.real_server_evidence`（报告标记，不改变 `acceptance=false`）；
- `summary`：对 8 项检查与 6 项证据的 `pass`/`fail`/`unknown` 计数，`acceptance: false` 与 `human_signoff_required: true`。输入错误时报告还会带 `error`。

`--json` 输出完整 JSON；`--out PATH` 原子写入 JSON 报告（父目录必须已存在，默认拒绝覆盖已存在文件与 symlink，`--overwrite` 才替换）；未指定 `--json` 时 stdout 打印人类摘要（即使同时给了 `--out`）。报告只反映本次可观测的只读检查和你提供的证据声明，不证明证据本身真实，也不证明容量、业务或生产环境长期稳定。

## 8. 安全与隐私

- 工具只写你显式指定的本地报告/模板；目标服务与部署不被修改。父目录必须已存在，默认拒绝覆盖已存在文件与 symlink，`--overwrite` 才允许替换。
- 证据与报告可能包含内部主机名、人员、时间和路径，按内部材料保管，不要提交到仓库或公开分享。
- 令牌只放环境变量；不要写进命令历史、脚本、证据或工单。
- 工具不读取 `.env`，不自动修复配置，不自动恢复、停机或重试。
- 不要为了“让检查通过”而放宽 TLS 校验、关闭鉴权或把 metrics 接口匿名开放。
- 报告打印前已按约定过滤令牌、证据引用、`notes` 与响应正文；仍建议在分享前人工检查。

## 9. 仍需真实环境才能取得的证据（保持 `unknown`）

以下项目在本地仓库、CI 或隔离回归中**不可能**取得，除非部署方提供了真实环境材料，否则必须保持 `unknown`：

- 真实业务资料与业务问答质量（真实语料、人工核验问题集与可接受答案）。
- 目标规模下的容量、并发、延迟与费用（真实模型、真实数据量）。
- 生产服务器、网络、域名、TLS 证书链与安全组/代理配置。
- 真实生产备份的恢复演练、实测 RTO/RPO、数据与文件校验。
- 真实通知接收器收到的 firing/resolved 及回执。
- 24 小时与 72 小时连续运行（soak）记录，以及三个 worker 角色的持续状态。
- 真实成员/角色的 ACL 允许与拒绝结果。
- 生产部署版本/环境身份（镜像 digest、提交号与部署记录）。

工具默认生成全 `unknown` 模板。任何一项没有真实材料时都不得写成 `pass`，应保持 `unknown`。退出码取决于运行方式与结果：只有 unknown、没有 `fail` 时，live 或提供证据的运行为 `3`，未提供证据的 dry-run 为 `0`；存在 `fail` 时为 `1`。

## 10. 与现有回归的关系

仓库现有的隔离回归只证明合成/隔离链路，不能替代上面的真实证据：

- [`deploy/tests/restore-drill.sh`](../tests/restore-drill.sh)：在独立临时 Compose 项目中执行真实 `pg_dump`/`psql` 恢复，并核对测试行内容与测试文件 SHA-256。它使用合成数据与隔离资源，不接触生产 `DATA_DIR`，不能证明生产备份可恢复或生产 RTO/RPO。
- [`deploy/tests/test-monitoring.sh`](../tests/test-monitoring.sh)：验证配置权限、规则语法/单元测试、Prometheus 启动与关闭 lifecycle。它使用独占测试容器和合成令牌，不证明生产抓取目标、告警规则或通知链路。
- [`deploy/tests/test-alerting.sh`](../tests/test-alerting.sh)：用合成指标触发真实 Prometheus → Alertmanager → 本地 webhook fixture，证明 firing/resolved 投递与日志脱敏。接收者只是本地 fixture，**不代表真实接收器已收到通知**。

这些脚本由 [CI](../../.github/workflows/checks.yml) 在隔离环境运行。它们通过只说明合成链路可用；生产验收仍需填写本指南的证据模板并完成人工签收。监控与告警的启用步骤见 [监控与告警说明](../monitoring/README.md)。
