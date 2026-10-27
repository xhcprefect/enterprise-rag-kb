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

### 用量与费用升级

新版增加「用量与费用」页面，需要同步更新 API、worker 和 web，不能只替换前端静态文件。

1. 升级前备份数据库，停止旧 API / worker；启动新版后，`init_db` 会在已有初始化锁下幂等创建 `model_usage`、`model_prices`、`usage_tracking_state` 及计量索引，无需清空历史会话或重建文档向量。
2. 初始化只补录历史已完成问答诊断中明确返回的生成 Token；历史模型、价格、追问改写、向量化和重排缺少可靠信息时保持未知，不会用当前模型/价格倒算。
3. 管理员登录「用量与费用 → 模型单价」，按服务商、模型和调用类型填写**人民币/百万 Token**单价。确实免费时填 `0`，未确认价格时留空；缓存单价留空表示按普通输入价计算。单价仅对之后新建的调用快照生效，修改后不会重算历史费用。
4. `LLM_STREAM_INCLUDE_USAGE` 新默认值为 `true`，用于请求云端 SSE 的最终实际用量。若旧 `.env` 显式写了 `false`，需自行改成 `true` 后重建 API / worker；不支持 `stream_options` 的兼容网关可保留 `false`，相应用量可能未知。页面不读取 API Key，也不保存问题与答案正文。
5. 验收时确认管理员能切换全站/本人，普通用户只有本人数据且价格接口返回 403；完成一轮对话后刷新统计，重复同一个 `request_id` 不新增调用。测试真实服务可能产生费用，可先用本地模拟服务与隔离测试库验证。

统计记录的是应用内模型调用尝试，包含自动重试、文档/检索向量化、改写、重排与生成；已删除的会话不会扣减历史用量。失败或停止可能仍有费用；上游未报告的用量保持未知。统计是按配置单价计算的估算，不包含供应商折扣、套餐、税费、调用后继续产生的费用或应用外调用，不能代替供应商账单。详细口径见 [README 用量与费用](README.md#用量与费用)。

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
| 多人同时提问后整站变卡 | 主模型流已异步化，先检查供应商额度、同步检索/ORM 等待、连接池及向量索引，再按压测调整资源；见「并发能力」 |
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
通过 `CHAT_MAX_PER_USER` / `CHAT_MAX_CONCURRENT` 配置。当前仍不提供自动排队，
多进程的入口计数仍各自独立，不能将多开进程当成全站共享限流。

### 当前流式链路

主问答的云端与 Ollama 模型读取、RAG 正文处理、SSE 发送已经采用异步迭代。
等待模型 token 不再长期占用工作线程；同步 ORM、检索和追问改写仍在线程池中执行。
`THREAD_POOL_SIZE` 默认 200，只控制这些同步任务，不等于模型并发或已经验证的吞吐量。

- 模型流复用每个进程的有界异步 HTTP 连接池，`LLM_ASYNC_MAX_CONNECTIONS` 默认 64；
  断连和取消关闭本次响应，服务关闭时统一关闭连接池。
- 每路只保留一个正在等待的上游读取，没有无界生产者队列；慢客户端单次发送超过
  `CHAT_STREAM_SEND_TIMEOUT_SECONDS`（默认 15 秒）后释放本次连接和名额。
- 检索与写回使用短数据库会话，不在等待模型时长期持有连接；租约检查使用短事务。
- 同进程停止操作直接通知活跃流；跨进程停止由数据库轮询确认，默认每秒检查一次。
  同步检索已经开始后无法强杀其工作线程，但迟到结果不会进入后续生成或成功写回。
- 模型供应商额度、数据库连接总量、向量索引、CPU、内存和文件描述符仍限制实际并发。
  Compose 的 `nofile` 设置继续保留。

历史版本曾用同步流做过线程池和短输出压力测试；这些结果不代表当前版本通过了
100 路真实模型长回答的验收。上线前用模拟模型逐级测试 10/30/50/100 路，再用受控数量
真实请求验证供应商额度，记录首字时间、正文停顿、完整回答耗时、错误率和普通接口延迟。
多 API 进程部署前，还须解决共享准入与日志轮转，按全部进程合计数据库连接预算。

### 流式结束、截止时间与恢复

继续使用 SSE，Nginx 的响应缓冲应保持关闭。`meta` 下发 `stream_limits`，
`status` 事件表示 `retrieving`、`generating`、`saving` 阶段，`: ping` 注释是连接心跳。
首个非空白正文默认等待 60 秒，正文停顿默认 45 秒，整轮默认 300 秒；心跳、阶段事件
和模型思考字段都不能延长这些内容截止时间。浏览器同时执行独立保护，等待响应头也有界。

收到模型 `finish_reason` 后，正文停止，仅在 `LLM_STREAM_USAGE_GRACE_SECONDS`
（默认 0.5 秒）的绝对窗口内补收实际用量，然后主动关闭响应，不等待网关 EOF。
未返回的用量和费用继续记为未知；失败、取消前已经收到的实际用量保留。
`length` 结束保留已有内容，并通过 `done.truncated` 及历史消息字段提示长度截断；
内容过滤、异常断流不会保存成正常回答，系统也不会自动追加付费调用。

浏览器意外断线后只读 `GET /api/v1/chat/requests/{request_id}`：已完成则恢复原消息与引用，
处理中只做有界状态查询，失败/取消明确提示；绝不自动重新调用生成接口。
接口只允许请求本人读取，并重新校验当前会话与知识库权限；删除会话返回 410，
请求未知或属于他人返回 404。租约过期的遗留 processing 会被标为失败，不能一直假装在处理。
这项能力是完整结果恢复，不是中间事件断点续传；部分未完成的正文不作为成功结果返回。

单个事件和未解析缓冲默认限制 64 KiB，完整回答限制 131072 字符。协议字段、中文 UTF-8
跨字节分片、超时、尾部计量、取消、权限撤销、幂等重放和恢复均有回归测试。
配置模板见 `backend/.env.example`，调整后重启 API；前后端建议一起升级，不需要清库。

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
  上限还受入口准入、异步 HTTP 池、数据库与模型供应商额度共同限制，不能仅看线程数。
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
