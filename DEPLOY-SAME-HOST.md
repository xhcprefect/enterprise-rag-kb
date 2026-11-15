# 单机 production / staging 双环境部署手册

适用于**同一台 Linux 服务器**同时运行两套隔离部署：production 对外服务，staging 供候选版本验收。基于当前磁盘实现：统一入口 `deploy/env.sh`、环境模板 `deploy/environments/{production,staging}/`、共享资源覆盖 `docker-compose.host.yml`。旧单环境（根 `.env` + `backend/.env`）继续按 [DEPLOY.md](DEPLOY.md) 与 [DEPLOY-FROM-SCRATCH.md](DEPLOY-FROM-SCRATCH.md) 操作，本入口不接管旧的 `rag-kb` Compose 项目。

## 1. 隔离边界与共享宿主局限

| 项 | production | staging |
|---|---|---|
| Compose project | `ragkb-prod` | `ragkb-staging` |
| 默认 Web | `0.0.0.0:80`（HTTPS 可选 `443`） | `127.0.0.1:18080` |
| 配置 | `deploy/environments/production/.env` + `backend.env` | `deploy/environments/staging/.env` + `backend.env` |
| 模板数据路径（可改） | `/srv/ragkb-data/production` | `/srv/ragkb-data/staging` |
| 备份 | 本环境 `${DATA_DIR}/backups` | 另一个 `${DATA_DIR}/backups` |
| 应用模式 | `ENVIRONMENT=production` | 同样是 `ENVIRONMENT=production`（只有部署身份不同） |

两套各有独立 network、db、uploads/projects/logs、管理员/JWT/模型密钥、端口与镜像标签。入口对身份、`COMPOSE_PROJECT_NAME`、绝对路径、数据目录嵌套、端口重复、共用 `backend.env` 做 fail-closed 校验。**共享**的是宿主 CPU、内存、磁盘、Docker daemon 与宿主故障域：不是双机高可用，也不承诺吞吐；一套 OOM、磁盘写满或宿主机宕机都会影响另一套。staging 不新增共享公网反代，默认只经 SSH 隧道访问。

## 2. 部署入口与配置

```text
bash deploy/env.sh <production|staging> <init|compose|backup|restore|remote-backup|render-prometheus|render-alertmanager> [参数...]
```
（首行是用法示意，尖括号参数必须替换成实际值后才可执行。）

第一个参数**必须显式选择环境**，没有默认值；`compose` 后直接跟 Compose 子命令。入口自动加载所选 `.env`，固定 `--env-file`、`-p` 与 `COMPOSE_FILE`，**不要 `source .env`**（会绕过校验，且父进程变量可能改变目标），也不要传 `-p` / `-f` / `--env-file` 覆盖。`compose config` 可能输出密钥，仅管理员在安全输出通道显式执行。

- 默认配置根 `deploy/environments`；`RAGKB_ENV_ROOT=/abs/path` 可指向管理员维护的稳定配置根，结构为 `root/production/{.env,backend.env}` 与 `root/staging/{.env,backend.env}`（只需部署账号可读写该目录，0600 的实际配置由首次 `init` 生成）。模板**始终取当前 checkout 的受控文件** `deploy/environments/<环境>/.env.example` 与 `backend.env.example`，自定义根无需、也不应自行预置模板。配置目录与文件不能是符号链接，控制变量 `ENV_FILE`、`BACKEND_ENV_FILE`、`RAGKB_ENV_ROOT`、`RAGKB_ENV_SELECTED` 不能写进环境 `.env`。
- `DATA_DIR` 可选任意**绝对独立目录**，不要求 `/srv`；两环境不得相同或嵌套，不得是 `/`、`/etc`、`/var` 等系统目录。`BACKUP_DIR` 必须解析为所选环境 `${DATA_DIR}/backups`。
- `COMPOSE_FILE` 默认 `docker-compose.yml:docker-compose.host.yml`，可在**本环境 `.env`** 追加 `docker-compose.https.yml`、`docker-compose.monitoring.yml`、`docker-compose.alerting.yml`；前两项顺序不可变。告警必须同时带监控覆盖；入口自动追加对应的 `docker-compose.host.monitoring.yml` / `docker-compose.host.alerting.yml`，不用手填配额覆盖。TLS、监控、告警端口与目录必须按环境分开；HTTPS 未填 `RAGKB_ACME_WEBROOT` 时使用本环境 `${DATA_DIR}/acme`。
- `BACKEND_IMAGE` / `FRONTEND_IMAGE` 必须由管理员填**不可变版本 tag 或 `@sha256` 摘要**（如 `rag-kb-backend:1.2.3`、`rag-kb-web:1.2.3`）。loader 会拒绝空值、模板里的 `REPLACE_WITH_*_TAG`、`:latest` 以及没有显式标签/摘要的镜像名；生产引用 staging 已验收的**同一后端/前端产物**，不在生产重新 build。

## 3. 最短完整步骤（新装）

在服务器选定一个**固定 checkout 目录**（下文用 `/opt/ragkb/current` 示例），安装 Docker Compose v2 或更新版本，准备两套独立数据目录。若数据路径在 `/srv` 等需 root 的目录，先由管理员建好目录并授权给部署账号；之后用同一个能访问 Docker 的部署账号执行所有入口命令，确保能读取 0600 配置。`init` 会建 `DATA_DIR/{pgdata,uploads,projects,logs,backups}`、检查占位值、探测构建源连通性（每个源最多 6 秒，仅告警、不阻断）。离线时可在选定环境 `.env` 设置 `RAGKB_INIT_SKIP_SOURCE_CHECK=1`；所有双环境操作仍从 `deploy/env.sh` 进入。

```bash
cd /opt/ragkb/current

# 1) 首次 init：只生成 0600 占位文件并以非零退出，这是设计行为，不是失败
bash deploy/env.sh production init     # 预期 exit 1
bash deploy/env.sh staging init        # 预期 exit 1

# 2) 编辑两个部署 .env，各自填 POSTGRES_PASSWORD 与已验收/候选镜像 tag
#    deploy/environments/production/.env 和 deploy/environments/staging/.env
#    COMPOSE_PROJECT_NAME、DATA_DIR、BACKUP_DIR、WEB_* 保持模板值（或改成两个不冲突的绝对路径/端口）
#    示例：BACKEND_IMAGE=rag-kb-backend:1.2.3、FRONTEND_IMAGE=rag-kb-web:1.2.3

# 3) 编辑两个 backend.env，填各自独立的 JWT_SECRET / ADMIN_USERNAME / ADMIN_PASSWORD /
#    EMBEDDING_API_KEY / LLM_API_KEY（cloud provider 时必填），不得复用 staging 凭据
#    两套模板均 ENVIRONMENT=production；DATABASE_URL 等由 Compose 覆盖，不用手改
#    现有 init 要求 ADMIN_PASSWORD 填一个非占位的初始化值：init 只用于首次部署检查，
#    建号成功后可删除该行（之后改密码请走「个人中心」）；删除后不要再跑 init，
#    其占位检查会因该行为空而拒绝通过，日常只用 compose / backup 等入口命令。

# 4) 再次 init：正式检查配置并建数据目录/权限（仅首次部署）
bash deploy/env.sh staging init
bash deploy/env.sh production init
```

### staging：构建候选版本一次、验收

只在 staging checkout 构建一次指定版本的后端与前端镜像，`build` 阶段不受 `docker-compose.host.yml` 运行时配额约束；构建产物由本机 Docker daemon 保存，两个 project 都能引用同一 tag。构建时两套镜像引用 `BACKEND_IMAGE` / `FRONTEND_IMAGE`，其中后端 api 与全部 worker 共用同一个镜像。

```bash
bash deploy/env.sh staging compose build api web    # 候选 tag 只需构建一次
bash deploy/env.sh staging compose up -d --no-build # ingest worker 默认必有
bash deploy/env.sh staging compose ps
bash deploy/env.sh staging compose logs --tail=50 api web worker
curl -fsS http://127.0.0.1:18080/health
```

从笔记本建立隧道后浏览器访问 `http://127.0.0.1:18080`（本地端口与服务器回环端口对应）：

```bash
ssh -N -L 18080:127.0.0.1:18080 user@server
```

按 [部署验收指南](deploy/acceptance/README.md) 完成真实业务验收（登录、上传/入库、问答、失败路径与截图），并确认版本与 tag 记录。验收不通过就修代码、打新 tag、重新走 staging，不动生产。

### production：同 tag 上线

把生产 `.env` 的 `BACKEND_IMAGE` / `FRONTEND_IMAGE` 填成**staging 已验收的同一 tag**，然后启动，不执行 `build`、不用 `--build`：

```bash
bash deploy/env.sh production compose up -d --no-build
bash deploy/env.sh production compose ps
curl -fsS http://127.0.0.1/health       # 仅未启用 HTTPS 的 HTTP 内网验证
```
启用 HTTPS 后 80 端口只做 301 跳转，`curl -f http://.../health` 不再是成功判据；按下一节配好域名证书后，用 `curl -fsSL https://<真实域名>/health` 验证（预期 `{"status":"ok",...}`），并确认 301 的 Location 指向该固定域名。

**未配置 HTTPS 的 production 只是 HTTP，仅可在内网/维护窗口验证**；对外公网服务必须按 [DEPLOY.md HTTPS](DEPLOY.md#https) 追加配置：在生产 `.env` 设置 `COMPOSE_FILE=docker-compose.yml:docker-compose.host.yml:docker-compose.https.yml`、固定域名 `RAGKB_SERVER_NAME`、仓库外证书目录 `RAGKB_TLS_CERT_DIR`（含 `fullchain.pem`/`privkey.pem`）、`RAGKB_TLS_MODE=https`，域名解析与证书就绪后再执行 `bash deploy/env.sh production compose up -d --no-build`。HTTPS 覆盖把 `frontend/nginx/tls.conf` 与 `frontend/nginx/snippets/` 从**当前 checkout 源码只读挂载**进容器，因此生产运行的 checkout 必须保持不可变：不要在给生产提供 bind 文件的目录里 `git switch` / `git checkout` 到别的版本，否则容器读到的 nginx 配置随之变化。证书与 nginx 片段更新按旧手册 `restart web`（入口形式 `bash deploy/env.sh production compose restart web`）；改 tag 走独立 release checkout。

### 不同版本部署与稳定配置根

推荐每个发布版本一份独立 release checkout（例如 `/opt/ragkb/releases/1.2.3`），由**同一个部署账号**维护一个不随 checkout 切换的配置根；该账号要能读写 0600 配置、数据目录并访问 Docker（root 建的目录必须先 chown 给部署账号，或全部命令都以 root 执行）：

```bash
sudo install -d -m 700 /etc/ragkb/env/production /etc/ragkb/env/staging
sudo chown -R deploy:deploy /etc/ragkb/env          # deploy 换成实际部署账号

# 推荐：用同一账号让入口从当前 checkout 的受控模板生成占位（无需复制模板到 custom root）
RAGKB_ENV_ROOT=/etc/ragkb/env bash deploy/env.sh production init   # 首次 exit 1，生成 0600 占位
RAGKB_ENV_ROOT=/etc/ragkb/env bash deploy/env.sh staging init      # 同上
# 编辑 /etc/ragkb/env/production/.env、backend.env 与 staging 下的两个文件后，再次执行两次 init；
# 也可选择手工从 deploy/environments/<name>/*.example 复制并填好（保持 0600），入口不会覆盖已有文件。
# 后续 compose / backup 也由同一账号执行：
RAGKB_ENV_ROOT=/etc/ragkb/env bash deploy/env.sh production compose up -d --no-build
RAGKB_ENV_ROOT=/etc/ragkb/env bash deploy/env.sh staging   compose ps
```

切换版本时用新 checkout 执行同一条入口命令，配置与 `DATA_DIR` 保持指向稳定路径；给生产读过 bind 文件的 checkout 保持原版本不变，直到新版本验收完成、确认没有容器再引用它的 nginx 文件。所有命令都必须显式带 `<production|staging>`，cron 与脚本里亦然。

## 4. 可选 profile、观测与维护

默认 `db + api + worker(ingest) + web` 必定启动；`worker-summary`（`--profile summary`）、`worker-project`（`--profile project`）、`worker-regression`（`--profile regression`）**只在对应功能启用时**才按环境启动，否则任务会停在 pending/queued 或空转。例如：

```bash
bash deploy/env.sh production compose --profile summary up -d --no-build
bash deploy/env.sh staging   compose --profile project up -d --no-build
bash deploy/env.sh staging   compose --profile summary --profile project --profile regression stop
```

监控/告警渲染也走环境入口。先按本环境模板的注释设置仓库外的独立 `OUT_DIR`、`RAGKB_*_CONFIG_DIR` / `RAGKB_*_SECRET_DIR`，准备 0600 的 `RAGKB_METRICS_TOKEN_FILE`（与本环境 `backend.env` 的 `METRICS_TOKEN` 一致）；告警还需独立的 `RAGKB_ALERT_WEBHOOK_FILE` 与 `RAGKB_ALERTMANAGER_TARGET=alertmanager:9093`。配置完成后再执行 `bash deploy/env.sh production render-prometheus` 或 `bash deploy/env.sh staging render-alertmanager`，确认输出目录与 Compose 挂载一致后启动对应服务。完整变量、文件权限和启停要求见 [监控与告警说明](deploy/monitoring/README.md)；其中旧命令的配置入口应替换成本文的显式环境入口。

常用检查（`ps`/`logs`/`stats`/health 均带环境参数）：

```bash
bash deploy/env.sh production compose ps
bash deploy/env.sh production compose logs --tail=100 api worker
bash deploy/env.sh staging   compose stats --no-stream
curl -fsS http://127.0.0.1/health                  # 仅 production 未启用 HTTPS 时的 HTTP 内网验证
curl -fsS http://127.0.0.1:18080/health           # staging 经隧道/服务器本机
# 启用 HTTPS 后改用：curl -fsSL https://<真实域名>/health（HTTP 会 301）
```

备份与恢复也按环境隔离，`backup` / `restore` 会打印 `[维护目标]` 行（环境、project、数据/备份目录），执行前先核对：

```bash
bash deploy/env.sh production backup              # 产物在本环境 ${DATA_DIR}/backups
bash deploy/env.sh staging   backup
bash deploy/env.sh production restore \
  '/srv/ragkb-data/production/backups/ragdb-<stamp>.sql.gz' \
  '/srv/ragkb-data/production/backups/uploads-<stamp>.tar.gz' \
  '/srv/ragkb-data/production/backups/projects-<stamp>.tar.gz'
```

示例中的 `<stamp>` 必须先替换成实际时间标识再执行（引号避免 `<` 被 shell 当成重定向；不替换则文件不存在，restore 会在校验阶段拒绝）。`restore` 的参数为 1–3 个同 stamp 产物，第三项可由 `manifest-<stamp>.sha256` 自动发现；输入 `yes` 会**覆盖目标环境数据库与目录**，不要放进无人值守脚本，也不要自动执行。旧两件套备份兼容；恢复演练与清单规则见 [DEPLOY.md 数据与备份](DEPLOY.md#数据与备份) 及 [DEPLOY.md 恢复演练](DEPLOY.md#恢复演练隔离不影响生产)。异地备份从 `bash deploy/env.sh production remote-backup '已核验的 manifest 实际绝对路径'` 进入（对 staging 改第一个参数），默认关闭。先在本环境 `.env` 配好独立的 `RCLONE_CONFIG`、`RCLONE_REMOTE` 与开关，变量示例见本环境模板，权限与 crypt 要求见旧手册。

cron 示例（绝对路径 + 显式环境；日志目录由 init 建好且可写，路径按实际 `DATA_DIR` 调整）：

```cron
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
20 3 * * * cd /opt/ragkb/current && /usr/bin/bash deploy/env.sh production backup >> /srv/ragkb-data/production/logs/backup.log 2>&1
40 3 * * * cd /opt/ragkb/current && RAGKB_ENV_ROOT=/etc/ragkb/env /usr/bin/bash deploy/env.sh staging backup >> /srv/ragkb-data/staging/logs/backup.log 2>&1
```

## 5. 资源预算（来自实际模板）

`docker-compose.host.yml` 的值是**单容器运行时上限**，可由各环境 `.env` 的 `*_CPUS` / `*_MEM_LIMIT` 覆盖；不是内存预留，不限制 `docker compose build`，也不等于全站容量。下表的内存按模板字面值相加（M 按 MiB 记）：

| 服务 | production | staging |
|---|---:|---:|
| db | 1536 MiB | 512 MiB |
| api | 1024 MiB | 384 MiB |
| worker (ingest) | 768 MiB | 512 MiB |
| web | 128 MiB | 64 MiB |
| 核心合计 | **3456 MiB** | **1472 MiB** |

两套核心合计 **4928 MiB ≈ 4.81 GiB**。三个可选 worker 各为 prod 256 MiB / staging 192 MiB，合计 **1344 MiB**；两环境全部 worker 启用约 **6.125 GiB**。监控与告警也有单容器上限：production 的 Prometheus 为 0.50 CPU / 256 MiB、Alertmanager 为 0.25 CPU / 128 MiB；staging 分别为 0.25 CPU / 192 MiB 和 0.25 CPU / 64 MiB，合计再加 **640 MiB**。全部容器的内存上限相加约 **6.75 GiB**，仍不含 OS、page cache、构建过程与 Docker 自身开销。所有容器都使用本环境的滚动日志上限。4C8G 只是低负载基线：内存限额接近实际用量会出现 OOM 或性能抖动，必须按 `compose stats` 与宿主监控观察后调整；CPU 每容器限额相加允许大于物理核数，那是抢占上限而非总预算。不承诺吞吐或高可用。staging 不验收时用本节的三个显式 profile 参数执行 `stop`，一并停掉可选角色以释放资源。

## 6. 回滚与旧单环境迁移

**同机新装回滚**：把目标环境 `.env` 的镜像 tag 改回上一个不可变版本并 `bash deploy/env.sh <env> compose up -d --no-build`。镜像可回滚，但数据库 schema **不一定向后兼容**：含迁移的升级先备份，回滚前确认旧镜像能读当前库；必要时用同版本备份恢复，不能只靠换镜像。备份不含镜像，请保留旧 tag 的镜像或可重建的发布 checkout。

**接管现有单环境 `rag-kb`**：新入口不会接管旧容器、旧数据或旧部署的 `PROJECT_ALLOWED_ROOTS` 项目索引授权目录。迁移要在维护窗口内进行，步骤要点如下，细节以 [DEPLOY.md 数据与备份](DEPLOY.md#数据与备份) 的备份/恢复与隔离演练章节为准，不在本文堆危险长命令：

1. 完整备份旧部署（记录当前镜像/commit tag、`ragdb` + `uploads` + `projects` 三件套与清单），核对旧 `backend/.env` 的 `PROJECT_ALLOWED_ROOTS` 项目索引授权目录（容器内路径与只读挂载是否在新环境仍有效），以及旧凭据存放位置；备份产物单独留存。
2. 停旧 `api`、**全部已启用的 worker 角色**和旧 `web`，释放 80/443；确认没有写库进程后再操作，**不要在线 `cp` pgdata**。
3. 新 production 用同 tag 完成 `init`，短暂 `up -d --no-build` 让 db/api 完成初始化后立即停新 writer（含 profile 角色），保持新库静止。
4. `bash deploy/env.sh production restore` 加旧完整三件套的实际路径，明确输入 `yes` 覆盖新环境，再启动服务并验收 `/health`、文档、项目来源与问答；验收通过后才退出旧部署。
5. staging 的测试账号、JWT、管理员与模型 keys 与生产不同，**不把 staging 数据/账号晋升为生产**；迁移前后分别核对两套配置的 `COMPOSE_PROJECT_NAME`、`DATA_DIR` 与 `BACKUP_DIR` 没有交叉。

## 7. 待确认与限制

- 现有 `deploy/init.sh` 会要求 `ADMIN_PASSWORD` 填初始化值（占位/弱值直接拒绝）；`init` 只用于首次部署检查与生成占位/建目录。建号后删除该行是既有机制推荐行为，但**删除后再跑 `init` 会因该值为空而失败**；日常升级、重启、备份恢复只用显式环境的 `compose` / `backup` / `restore` 命令，不要重复 `init`。此限制不自行改实现。
- 本手册不记录任何真实密钥、域名或服务器地址；所有示例 tag、路径、stamp 均为占位。
- `RAGKB_ENV_ROOT` 指向自定义配置根时，只需保证部署账号对该目录有写权限；首次 `init` 会用当前 checkout 的受控 `.example` 生成 `root/<环境>/.env` 与 `backend.env`（0600），无需在自定义根预置模板，也不会覆盖已有配置。
- 公网 production 未启用 HTTPS 前只能内网验证；staging 永远保持回环绑定，不提供共享公网反代。
