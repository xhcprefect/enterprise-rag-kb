# 生产部署手册

面向 Ubuntu 22.04 LTS、2 核 4 GiB 的云服务器。全部组件用 Docker Compose 编排，
对外只暴露一个 Nginx 端口。

## 架构

```
浏览器
  │  HTTP :80
  ▼
┌─────────────────────────────┐
│ web (Nginx)                 │  SPA 静态托管 + 反代 /api 与 /health
└──────────────┬──────────────┘
               │ 内部网络
       ┌───────┴────────┐
       ▼                ▼
┌────────────┐   ┌────────────┐
│ api        │   │ worker     │   同镜像 rag-kb-backend，不同启动命令
│ uvicorn    │   │ 轮询入库   │
└─────┬──────┘   └─────┬──────┘
      └────────┬───────┘
               ▼
        ┌─────────────┐
        │ db          │  pgvector/pgvector:pg18
        └─────────────┘

共享卷：${DATA_DIR}/uploads（api 与 worker 必须共用）、${DATA_DIR}/logs、${DATA_DIR}/pgdata
```

**外部依赖**：向量化与对话都走云端 API（硅基流动 + DeepSeek 官方），服务器本身不做推理，
所以不需要 GPU。

## 首次部署

### 1. 服务器准备

```bash
# 安装 Docker（官方脚本）
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# 时区（日志时间与本地一致，便于排查）
sudo timedatectl set-timezone Asia/Shanghai
```

### 2. 拉取代码

```bash
git clone https://github.com/xhcprefect/enterprise-rag-kb.git enterprise-rag-kb
cd enterprise-rag-kb
```

仓库里的 `deploy/*.sh` 已带执行位（`100755`）。若从压缩包或其他方式取得代码后
`./deploy/init.sh` 报 `Permission denied`，补一句 `chmod +x deploy/*.sh`。

### 3. 写配置

```bash
cp .env.example .env              # compose 变量
cp backend/.env.example backend/.env   # 应用配置
```

**必须改的**（`deploy/init.sh` 会检查，占位值不放行）：

| 文件 | 变量 | 说明 |
|---|---|---|
| `.env` | `POSTGRES_PASSWORD` | 数据库密码，只含字母数字，避免连接串转义问题；`testpw123`、`123456` 这类本地开发口令也会被拦下 |
| `.env` | `JWT_SECRET` | `python3 -c "import secrets;print(secrets.token_urlsafe(48))"` |
| `backend/.env` | `JWT_SECRET` | 同上，**两个文件保持一致** |
| `backend/.env` | `ADMIN_PASSWORD` | 初始管理员密码 |
| `backend/.env` | `EMBEDDING_API_KEY` / `LLM_API_KEY` | 云端密钥 |

其余保持默认即可：`EMBEDDING_PROVIDER=cloud`、`LLM_PROVIDER=cloud`、
`DATABASE_URL` 等由 compose 自动覆盖，**不用手改**。

### 4. 初始化并启动

```bash
./deploy/init.sh                  # 检查配置、建数据目录、探测构建源
docker compose up -d --build      # 首次构建约 5-8 分钟（依赖多）
```

### 国内网络加速（必看）

服务器在国内时，`FROM` 之外的三个下载源都在境外，实测不加加速**首次构建无法完成**：

| 源 | 未加速时 | 已默认换成 |
|---|---|---|
| Docker Hub | `dial tcp ...: i/o timeout`，镜像拉不下来 | 由 `/etc/docker/daemon.json` 的 `registry-mirrors` 处理 |
| Debian apt | 9.7MB 的 Packages 索引要拉 210 秒以上 | `mirrors.cloud.tencent.com` |
| PyPI（98 个包 ≈400MB） | 极慢 | `mirrors.cloud.tencent.com` |
| npm（约 150MB） | — | `package-lock.json` 里已全指向 `registry.npmmirror.com`，无需处理 |

**Docker Hub 加速**（先做这一步，否则镜像都拉不下来）：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://mirror.ccs.tencentyun.com",
    "https://docker.m.daocloud.io",
    "https://dockerproxy.net"
  ],
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker
docker info | grep -A4 "Registry Mirrors"
```

`mirror.ccs.tencentyun.com` 是腾讯云**内网**地址，仅 CVM 可用、不计流量费。
`log-opts` 限制容器日志大小，避免长期运行写满磁盘。

**apt / pip 已换成腾讯云源**，由 Dockerfile 里的 `ARG` 控制，可覆盖：

```bash
# 回退官方源（挂了代理的机器）
docker compose build \
  --build-arg APT_MIRROR=deb.debian.org \
  --build-arg PIP_INDEX=https://pypi.org/simple api

# 换清华源
docker compose build \
  --build-arg APT_MIRROR=mirrors.tuna.tsinghua.edu.cn \
  --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple api
```

> `mirror.ccs.tencentyun.com` 只在腾讯云内网可达；非腾讯云的机器会超时，
> 改用 `docker.m.daocloud.io` 或 `dockerproxy.net`。

**先单独验后端**（30 秒内就能确认 apt 换源是否生效，不用等前端）：

```bash
docker compose build api
```

日志里 `[worker 2/7] RUN ... apt-get update` 那一步应该 **10 秒内**完成。若仍要几分钟，
说明换源没生效，检查 `docker compose config | grep -i mirror` 或直接用 `--build-arg` 指定。

### 5. 验证

```bash
curl http://localhost/health
# {"status":"ok","worker":"idle"}    ← 只有这两个字段，公网也不怕

docker compose ps                 # api 应为 (healthy)
docker compose logs -f api        # 看启动日志
```



浏览器打开 `http://<服务器IP>/`，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。

**完整验收清单**（这几条过了才算部署成功）：

1. 登录成功，能进问答页
2. 新建知识库 → 上传一个 txt → 状态从 `pending` → `processing` → `ready`
3. **建 HNSW 索引**（见下方说明，漏了不报错，只是检索会退化成全表扫描）
4. 针对该文档提问，能返回答案并带 `[1]` 引用
5. `curl http://localhost/health` 里 `worker` 不是 `stale`
6. 重启后数据还在（见下节）
7. 「用户管理 → 批量导入」下载模板、导入一个两行的 xlsx、能下载回执
   （若这一步 404，说明 web 容器还是旧前端产物，重新 `docker compose up -d --build web`）

> **第 3 步不能省**：入库表由 `langchain-postgres` 自己建，`init_db()` 不管它，
> 所以新库、老库都要手工跑一次。**第一个文档入库之后**执行：
>
> ```bash
> docker compose exec api python -m scripts.migrate_vector_index --dry-run   # 只看
> docker compose exec api python -m scripts.migrate_vector_index             # 执行
> ```
>
> 数据量小的时候建不建都一样快，等文档多了才发现是全表扫描，排查成本高。
> 索引用 `CREATE INDEX CONCURRENTLY` 建，不阻塞读写；重复执行是幂等的。

关键词检索使用另一个 GIN 索引，新版 API 完成表结构迁移后即可建立：

```bash
docker compose exec api python -m scripts.migrate_keyword_index --dry-run
docker compose exec api python -m scripts.migrate_keyword_index
```

这一步只建立索引，不填充旧分块。旧文档仍可向量检索，在页面重新索引后获得关键词、
页码、章节与表格行信息；重新索引期间该文档暂不参与问答。扫描 PDF 仍需预先 OCR。

- **备份与升级注意事项**：备份现在会短暂停止 API/worker，以保证数据库和上传文件一致；升级必须同步更新 API、worker 与前端，见 [OPTIMIZATION.md](OPTIMIZATION.md)。

## 数据与备份

所有数据在 `${DATA_DIR}`（默认 `./data`）：

```
data/
├── pgdata/      # PostgreSQL 数据目录
├── uploads/     # 上传的原始文档
├── logs/        # 应用日志
└── backups/     # 备份产物
```

### 为什么这样设计

若机器是**竞价实例（抢占式）**，随时可能被回收。把数据放在独立目录，
换机器时把整个 `DATA_DIR` 拷到新机器再 `docker compose up -d`，服务即恢复。

**建议把 `DATA_DIR` 指向独立数据盘**：编辑 `.env` 的 `DATA_DIR=/data/rag-kb`，
并在 `/etc/fstab` 挂载数据盘。

### 手动备份

```bash
./deploy/backup.sh
# 2026-10-24 14:12:00 备份完成: 18K + 493B -> ./data/backups
```

产物：`ragdb-<时间戳>.sql.gz` + `uploads-<时间戳>.tar.gz`，默认保留 7 天。

脚本会**实际解压并检查内容**，不会因为磁盘满而留下"看起来成功"的空备份。

### 定时备份

**必须配**，尤其在这类竞价实例上——机器被回收时手动备份等于没有备份。

```bash
# 一条命令装好（幂等，重复执行不会叠加）
(crontab -l 2>/dev/null | grep -v "deploy/backup.sh"; \
 echo '20 3 * * * cd $HOME/enterprise-rag-kb && ./deploy/backup.sh >> $HOME/enterprise-rag-kb/data/logs/backup.log 2>&1') | crontab -

crontab -l                                    # 确认已装
```

两个容易踩的坑，上面的写法已经避开：

- **日志不要写 `/var/log`**：普通用户 `ubuntu` 对它没有写权限，重定向会直接失败，
  而 cron 的报错只会进系统邮件（基本看不到）。写 `data/logs/` 下。
- **不要写死绝对路径**：用 `$HOME` 或 `cd` 进项目目录，换机器、换用户名都不用改。

装完**务必模拟 cron 的精简环境实测一次**，`cron` 能跑不代表 `crontab` 里那条能跑：

```bash
env -i HOME=$HOME PATH=/usr/bin:/bin SHELL=/bin/sh \
  /bin/sh -c 'cd $HOME/enterprise-rag-kb && ./deploy/backup.sh'
# 应输出「备份完成: ...」，exit 0
```

`cron` 不继承登录 shell 的 PATH / 环境变量，是这类任务最常见的静默失败原因。

### 恢复

```bash
./deploy/restore.sh data/backups/ragdb-20261024-141159.sql.gz \
                    data/backups/uploads-20261024-141159.tar.gz
```

会先停 api/worker 再覆盖数据，有二次确认。

## 日常运维

### 日志

```bash
docker compose logs -f api        # API
docker compose logs -f worker     # 入库任务
tail -f data/logs/app.log         # 落盘的滚动日志
```

### 更新代码

```bash
./deploy/backup.sh                # 本次有数据库结构变更时才需要
git pull
docker compose up -d --build      # 只重建有变化的容器
```

`git pull` **不会**碰 `.env`、`backend/.env` 和 `data/`——它们在 `.gitignore` 里，
既不被跟踪也不会被覆盖。生产配置只存在于服务器上，这是有意的。

数据库结构变更由启动时的 `init_db()` 自动处理，不需要手动迁移。

> `init_db()` 只会**新增**列/表，外加少量固定的可空性变更（如
> `conversations.kb_id DROP NOT NULL`，为普通对话模式放开）。改字段类型、删列、
> 改默认值它都做不到，而且不会报错，只会表现为"配置改了但行为没变"。涉及这类变更要手工写 SQL。
>
> 新增列与 `DROP NOT NULL` 在 PG11+ 都是仅元数据操作，很快，但会短暂取
> `ACCESS EXCLUSIVE` 锁，建议避开访问高峰部署。

#### 会话并发、混合检索与原文定位版本升级

本次新增 `chat_turns` 请求记录、会话生成租约、消息的问答关联与状态，以及分块的
结构位置和关键词列。`init_db()` 幂等迁移，保留已有会话、消息与文档；无需清库。
为了避免旧 API 绕过会话租约、旧 worker 写入缺少新字段的分块，请安排维护窗口，
先备份并停止旧进程，再统一更新三个服务：

```bash
./deploy/backup.sh
docker compose stop api worker
git pull
docker compose up -d --build
# 等 API 健康后，在新镜像内建立关键词索引
docker compose exec api python -m scripts.migrate_keyword_index --dry-run
docker compose exec api python -m scripts.migrate_keyword_index
```

`HYBRID_RETRIEVAL_ENABLED=true` 默认启用关键词与向量排名融合。
`RERANK_ENABLED=false` 默认关闭外部重排；启用前配置 `RERANK_BASE_URL`、
`RERANK_MODEL` 和服务所需密钥，再通过固定问题集比较质量、延迟和费用。
原文预览及 PDF 读取都需要登录，并重新验证当前知识库权限与索引代次。

验收时用同一会话在两个标签页同时提交，后一请求应返回 409；复用已经完成的
`request_id` 应重放原答案；停止后重新提交应使用新请求 ID。删除会话后旧请求 ID
仍不能重新创建会话。`CHAT_LEASE_SECONDS=300` 控制崩溃后的过期恢复，正常生成会续期。

自动化回归和付费模型联调分开执行。真实数据库测试必须指向可清空的专用 `_test` 库，
业务问题集与合成样例的运行方法见 [评测说明](backend/evaluation/README.md)。
旧文档按需重新索引以获得结构信息，旧引用遇到文档重建后会提示失效。

#### 服务器首次接入 git

若代码是早先用压缩包传上去的（目录里没有 `.git`），不需要重新克隆，
在现有目录里接入即可，配置和数据都会原地保留：

```bash
cd ~/enterprise-rag-kb
cp .env .env.bak && cp backend/.env backend/.env.bak   # 保险
git init
git remote add origin https://github.com/xhcprefect/enterprise-rag-kb.git
git fetch origin
git reset --hard origin/main

# 确认配置还在，三行都要有输出
grep -E '^POSTGRES_PASSWORD=' .env
grep -E '^(JWT_SECRET|ADMIN_PASSWORD)=' backend/.env
ls -d data/pgdata
```

`reset --hard` 只覆盖被跟踪的文件，`.gitignore` 里的 `.env` 与 `data/` 不受影响。

### 回滚

```bash
git checkout <上一个提交>
docker compose build && docker compose up -d
```

镜像带的是代码和已锁定的依赖，回滚到旧提交即回到旧版本。

### 重启单个组件

```bash
docker compose restart worker
docker compose up -d --force-recreate api
```

### worker 多开

`worker` 领取任务用 `FOR UPDATE SKIP LOCKED`，天然支持多副本：

```bash
docker compose up -d --scale worker=2
```

⚠️ 多副本前先把 `LOG_DIR` 改成各副本独立目录，或改成只输出 stdout——
`RotatingFileHandler` 不支持多进程同时切割同一个文件。当前配置下**先用单副本**。

## 排查

| 现象 | 排查方向 |
|---|---|
| `docker compose ps` 中 api 反复重启 | `docker compose logs api`；多半是 `backend/.env` 里密钥为空或数据库连不上 |
| 打开 `/docs` 是 404 | **正常**，生产默认关闭。要临时看文档见下方「接口文档」 |
| `/health` 里 `worker: stale` | worker 没运行或已挂：`docker compose logs worker` |
| 文档一直 `pending` | 同上；`pending` 意味着没有任何 worker 领取 |
| 文档 `failed` | 看 `error_message` 字段与 worker 日志；常见是文件格式不支持或云端 API 报错 |
| 问答报鉴权失败 | `backend/.env` 的 `*_API_KEY` 失效或余额不足，错误信息里会带服务商域名 |
| 页面 404 | 检查 Nginx 的 SPA fallback；或访问路径不在路由表内 |
| 上传卡住 | `client_max_body_size`、磁盘空间；`docker compose logs api` |
| 文档一多，问答明显变慢 | 大概率是缺 HNSW 索引，检索在走全表扫描：见下方检查命令，缺了就跑 `migrate_vector_index` |
| 多人同时提问后**整站**变卡（`/health` 也慢） | 线程池被流式请求占满，见「并发能力」一节；调大 `backend/.env` 的 `THREAD_POOL_SIZE` |
| 批量导入提示「表头里找不到用户名列」 | 首行必须是表头且含「用户名/账号/登录名/username」之一；如果文件前面有标题行或空行，先删掉 |
| 批量导入提示「Excel 文件无法解析」 | 文件其实是 `.xls` 或损坏。用 Excel 打开后「另存为 → Excel 工作簿(.xlsx)」 |
| 批量导入很慢 | 每行一次 bcrypt（约 55ms，已并发）。500 行约 30 秒属正常；再大请拆分 |

### 常用检查命令

```bash
# 数据库里有多少向量
docker compose exec db psql -U postgres -d ragdb \
  -c "select count(*) from langchain_pg_embedding"

# 文档状态分布
docker compose exec db psql -U postgres -d ragdb \
  -c "select status, count(*) from documents group by 1"

# 确认 HNSW 索引存在（没有输出 = 缺索引，跑 migrate_vector_index 补建）
docker compose exec db psql -U postgres -d ragdb \
  -c "\di ix_langchain_pg_embedding_hnsw"

# 确认检索确实走了索引（要看到 Index Scan using ix_langchain_pg_embedding_hnsw）
docker compose exec db psql -U postgres -d ragdb -c \
  "set enable_seqscan=off;
   explain (costs off) select id from langchain_pg_embedding
   order by embedding <=> (select embedding from langchain_pg_embedding limit 1) limit 6;"
```

## 并发能力

当前默认每用户最多 2 路、每个 API 进程最多 50 路问答，超限返回 429，
通过 `CHAT_MAX_PER_USER` / `CHAT_MAX_CONCURRENT` 配置。以下线程池数据是历史压力测试，
用于说明机制，不代表当前默认开放的并发配额。

### 瓶颈不是 CPU，是线程池槽位

问答接口是「同步生成器 + `StreamingResponse`」：Starlette 每吐一个 token 都要
`await anyio.to_thread.run_sync(next, ...)` 借一个线程，而线程要一直等到云端下一个
token 返回才释放。所以**每路正在生成的流都常年占着一个线程**，CPU 却接近空转。

anyio 默认线程池上限是 **40**。实测（与 `/chat/stream` 同构的同步流式端点，
每流 10s、不调模型）：

| 并发流 | 普通请求（`/health`、登录、列表） | 全部流耗时 |
|---|---|---|
| ≤ 40 | 3–60 ms | 基线 |
| 45 | 33–548 ms（开始抖动） | 12 s |
| 80 | **1530 ms** | 20 s（翻倍） |

80 路时线程池状态是 `borrowed=40, tasks_waiting=40`——40 个槽位全在等云端，其余排队。
**`/health` 也是同步路由，一样要排队**，所以监控会同时报慢或超时。

`backend/.env` 的 `THREAD_POOL_SIZE`（默认 200）就是把这个池放大。调到 200 后实测：

| 并发流 | 普通请求 | 全部流耗时 |
|---|---|---|
| 80 | 6 ms | 基线 |
| 150 | 7 ms | 12 s（基线 10s） |

启动日志里会打印实际生效的值：`服务已启动，... 线程池=200`。

### 一次真实问答的三个阶段，各自受什么限制

| 阶段 | 耗时 | 占用资源 | 上限来自 |
|---|---|---|---|
| 检索（向量化） | 实测约 **106 ms** | 线程 + 向量服务连接 | 共享连接池 `max_connections=16` |
| 生成（等云端 token） | 4.9–8.9 s | **一个线程全程占着** | `THREAD_POOL_SIZE` |
| 落库（答案 + 审计） | 几 ms | DB 连接（用完即还） | 不是瓶颈 |

阶段之间是串行的：先检索，再生成。所以一个人的一次提问会**先后**用掉一个线程，
而不是同时用掉两个。

实测「多人同时提问」的完整链路（真实检索耗时 + 12 个 token 的生成）：

| 同时提问 | 普通请求 | 最慢的回答 | 失败 |
|---|---|---|---|
| 30 人 | 3 ms | 5.1 s（单人基准 4.9 s） | 0 |
| 80 人 | 3 ms | 5.5 s | 0 |
| 150 人 | 2–5 ms | 6.4–6.9 s | 0 |

**结论：几十人同时问答完全没问题**，而且没有人被明显拖慢——因为大家在等的是云端
API，不是在抢 CPU。真正会先出问题的是**同时**提问超过线程池容量。

### 已排除的三个「看起来像瓶颈」的地方

- **数据库连接没被流式占住**：检索、历史查询、用户消息落库都在返回生成器**之前**
  `commit()`，SQLAlchemy 此时已把连接还回池。实测 10 路并发流期间
  `engine.pool.checkedout() == 0`，所以 `pool_size=10 + max_overflow=20` 不是瓶颈。
- **CPU / 内存很空**：api 容器约 106 MiB / 3.5 GiB，CPU 0.1%，机器 load 0.12。
- **文件描述符（这条以前是个隐患，已修）**：镜像默认 `nofile` 软限只有 **1024**，
  而每路流在生成期间要占一个客户端连接 + 一个上游连接 + 若干日志/DB fd。
  实测 250 并发时进程 fd 用到 364，再往上就会在 accept 阶段失败，客户端看到的是
  `Server disconnected without sending a response`——请求根本没进业务代码，
  应用日志里查不到任何东西。`docker-compose.yml` 已给 api 与 web 设
  `ulimits.nofile.soft: 65536`（硬限 524288 不动）。
  可用 `docker compose exec api sh -c "cat /proc/1/limits | grep 'open files'"` 确认。

### 再往上还有两档（当前不需要）

1. `uvicorn --workers 2`：**必须先改日志输出**——`RotatingFileHandler` 多进程切同一个
   文件会互相干扰（同「worker 多开」一节的限制）。
2. 把流式链路改成异步（`cloud_models.stream` → `rag.answer_stream` → `event_stream`
   三层都改成 `async def` + `httpx.AsyncClient`）：彻底不再需要「每路流一个线程」，
   并发上限只受内存和上游 API 限制。工作量大，等真的不够用再动。

## 接口文档

`/docs`、`/redoc`、`/openapi.json` 只在 `backend/.env` 的 `DEBUG=true` 时注册，
生产默认 `false` → 三条路由**根本不存在**，由 Nginx 的 SPA fallback 返回首页。

`/openapi.json` 会把全部接口、参数名、响应结构和字段类型公开，等于给扫描器一份
说明书，所以不要为了"方便看一眼"长期打开。

临时需要看文档时：

```bash
# 方式一：改配置后重建（记得看完改回去）
sed -i 's/^DEBUG=.*/DEBUG=true/' backend/.env
docker compose up -d --force-recreate api
# 看完：改回 false 再执行同一条命令

# 方式二：不对外暴露，直接在容器里导出
docker compose exec api python -c \
  "import json;from app.main import app;print(json.dumps(app.openapi(),ensure_ascii=False,indent=2))" \
  > /tmp/openapi.json
```

## HTTPS

当前按 HTTP 部署，`frontend/nginx.conf` 里已留好注释块。拿到域名后：

1. 域名 A 记录指向服务器 IP，放行 80/443
2. 用 certbot 签发证书（webroot 模式）
3. 取消 `nginx.conf` 里 HTTPS 段的注释，填上域名和证书路径
4. `docker compose build web && docker compose up -d web`

同时把 `backend/.env` 的 `CORS_ORIGINS` 改成 `https://你的域名`。

## 安全清单

上线前确认：

- [ ] `JWT_SECRET` 已换成随机值（`.env` 与 `backend/.env` 两处）
- [ ] `POSTGRES_PASSWORD` 已改，且数据库端口**没有**暴露到公网（当前配置只在内网）
- [ ] `admin` 密码已改（首次登录后到个人中心修改）
- [ ] 改完后把 `backend/.env` 的 `ADMIN_PASSWORD` **整行删掉**——它只在建号那一次
      生效，留着只会让后来人误以为改它就能改密码（密码对不上时启动日志会告警提示）
- [ ] 定时备份已配置且在跑：`crontab -l` 有输出、`data/logs/backup.log` 有记录
- [ ] `backend/.env` 没有提交到 git（`.gitignore` 已包含）
- [ ] 云服务器安全组只放行 80（以及要用的 443），**不要**放行 8000 / 5432
- [ ] `backend/.env` 的 `DEBUG=false`（默认已是；为 true 时 `/docs`、`/redoc`、
      `/openapi.json` 会公开整套接口与字段结构）
- [ ] 公网验证一次：`curl -o /dev/null -w "%{http_code}\n" http://<IP>/openapi.json`
      应返回 **404**
- [ ] 定期检查备份是否正常产出（`ls -lh data/backups`）
- [ ] 批量导入后的回执（含明文初始密码）不要放在共享目录，分发完即删；
      系统不会保留，也不会写进审计日志

## 已知限制

- **日志用文件轮转，不支持多副本共享**；需要多 worker 时先改日志输出方式。
- **`uvicorn` 单进程**：当前 `CMD` 没带 `--workers`。2 核机器上单进程足够
  （瓶颈在云端 API 延迟），需要时再调；但**先读「并发能力」一节**——
  单进程的并发上限由 `THREAD_POOL_SIZE` 决定，不是 CPU。
- **向量维度假定为 1024**（`vector_store.py` 的 `EMBEDDING_DIM`）。
  换成其他维度的向量模型时必须重建索引，否则写入会因类型不匹配失败。
- **HNSW 索引不在 `init_db()` 里**：入库表由 `langchain-postgres` 自建，应用不管它的结构，
  索引要手工跑 `scripts/migrate_vector_index`（新库上线验收清单第 3 条）。
- **备份不含 Docker 镜像**，回滚依赖重新 `build`。
- **批量导入的密码只在响应里出现一次**：库里只存 bcrypt 哈希，回执（xlsx）随导入响应
  返回 base64，**不再落盘、不再提供二次下载**。管理员关掉对话框前必须下载，
  否则只能走「重置密码」逐个处理。回执本身是明文密码文件，请勿留在共享目录。
- **批量导入是同步请求**：每行一次 bcrypt（约 200ms，4 线程并发后约 55ms/行），
  500 行约 30 秒。跑满时需要占用一个线程池槽位（`THREAD_POOL_SIZE` 默认 200，
  影响可忽略）。超过 500 行请拆分。
