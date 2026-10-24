# 从零到上线：云服务器购买到系统可用全流程

本文是一条**线性可执行**的路径：从在云控制台点下"购买"，到浏览器里能登录问答，
中间每一步都给出命令和**验收标准**。

> 已有服务器、只想更新代码的，看 [DEPLOY.md](DEPLOY.md) 的「日常运维」。
> 本文假设**一台全新的空机器**。

---

## 0. 开始之前

### 0.1 你需要准备什么

| 项目 | 说明 | 是否必需 |
|---|---|---|
| 云账号 | 已完成实名认证 | 必需 |
| 本地终端 | Xshell / PuTTY / Windows Terminal（`ssh` 命令） | 必需 |
| 向量模型 Key | 硅基流动 <https://cloud.siliconflow.cn/account/ak>，`BAAI/bge-m3` 当前免费 | 必需 |
| 对话模型 Key | 任意 OpenAI 兼容服务（本项目默认 sheapi.top） | 必需 |
| 域名 | 想用 HTTPS / 好记的地址时需要 | 可选 |
| 备案 | 中国大陆地域用域名访问 80 端口**必须**先备案 | 条件必需 |

**没有 GPU 也能跑**：向量化与对话都调云端 API，服务器只做编排和存储。

### 0.2 整条链路长什么样

```
浏览器 ──:80──▶ web(Nginx) ──┬──▶ api(FastAPI)  ──┐
                             │                    ├──▶ db(pgvector)
                             └──▶ /health          │
                                                  │
                    worker(入库进程) ─────────────┘
```

四个容器，只有 `web` 对外暴露 80。数据库、API、worker 全在 Docker 内网里，
公网不可达。

---

## 1. 购买云服务器

以腾讯云 CVM 为例，其他云厂商同理。

### 1.1 计费模式

| 模式 | 特点 | 建议 |
|---|---|---|
| 包年包月 | 价格固定，长期便宜 | 长期稳定运行选这个 |
| **竞价实例**（抢占式） | 便宜很多，但**随时可能被回收**，回收后数据可能丢失 | 测试/预算敏感时用，**必须**配好备份和独立数据盘 |

本项目在竞价实例上验证过可以跑，但要接受"机器随时消失"的前提——
数据放在 `${DATA_DIR}`，换机器时把目录拷走即可恢复（见 [§10](#10-数据备份与恢复)）。

### 1.2 配置选择

| 项 | 推荐值 | 说明 |
|---|---|---|
| 地域 | 离你近的（如南京、上海、广州） | 决定网络延迟；镜像加速也依赖地域 |
| 机型 | 2 核 4 GiB | **下限**。1 核 2G 构建时会 OOM |
| 系统盘 | 50 GiB 起 | Docker 镜像本身就要几个 GB |
| 镜像 | **Ubuntu 22.04 LTS 64 位** | 本项目按此验证；Debian 12 亦可 |
| 带宽计费 | **按流量计费** | 见下方说明 |
| 带宽上限 | 5 Mbps 够用 | 按流量计费时，上限只是**限速**，调高不额外收费 |
| 登录方式 | **密钥对**（推荐）或密码 | 密钥更安全；丢了很麻烦，注意保存 |

> **为什么推荐"按流量计费"**
> 按带宽计费是把 5 Mbps 包月买断，调高就要加钱；按流量计费只按实际出网流量收钱，
> 带宽上限只是速率上限。知识库系统流量很小（主要是文本），按流量通常几分钱。

### 1.3 安全组（最容易配错的一步）

安全组是**白名单**：没显式放行的端口一律拒绝，不是"默认允许"。

| 方向 | 端口 | 来源 | 用途 |
|---|---|---|---|
| 入站 | 22 | 你的公网 IP（最好）/ 0.0.0.0/0 | SSH 登录 |
| 入站 | 80 | 0.0.0.0/0 | HTTP 访问 |
| 入站 | 443 | 0.0.0.0/0 | HTTPS（有域名时再加） |

**绝对不要放行**：`5432`（数据库）、`8000`（API）。它们在 Docker 内网里，
放行等于把数据库暴露到公网。

**怎么区分"安全组拦了"和"服务没起"**：看连接失败的方式。

| 现象 | 含义 |
|---|---|
| **连接被拒绝**（`Connection refused`，RST） | 包到达了机器，但该端口没有进程监听 → **安全组是通的**，只是服务没起 |
| **超时**（`Connection timed out`，无响应） | 包被安全组丢弃 → **安全组没放行** |

所以**部署前**在本地做端口探测。任选一种：

**PowerShell（Windows 自带，最省事）**

```powershell
Test-NetConnection -ComputerName <服务器公网IP> -Port 22    # TcpTestSucceeded : True
Test-NetConnection -ComputerName <服务器公网IP> -Port 5432  # 应为 False，且选项受限
```

**Git Bash（有 `nc` 时）**

```bash
nc -zvw5 <服务器公网IP> 22      # 成功则输出 "succeeded" 或 "open"
nc -zvw5 <服务器公网IP> 5432    # 应超时/失败
```

> **80 端口在部署前一定是"拒绝"**（Nginx 还没跑），这是正常的，
> 不能据此判断安全组配置对错。等 [§8.4](#84-从外网验证) 部署完再验。

### 1.4 购买后立即做

1. 记下**公网 IP**
2. 把密钥文件（`.pem`）放到本地安全位置，`chmod 600`
3. 确认实例状态是"运行中"

---

## 2. 首次登录与系统初始化

### 2.1 连接

```bash
ssh -i /path/to/your-key.pem ubuntu@<服务器公网IP>
```

首次连接会问 `Are you sure you want to continue connecting?`，输入 `yes`。

> 腾讯云 Ubuntu 镜像默认用户是 `ubuntu`，不是 `root`。
> 若用密码登录，`ssh ubuntu@<IP>` 后输入控制台设置的密码。

### 2.2 系统更新与时区

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo timedatectl set-timezone Asia/Shanghai
date                       # 应显示 CST 时间
```

> Ubuntu 22.04 会提示 `*** System restart required ***`。如果内核有更新，
> 建议 `sudo reboot` 一次再继续，避免后面 Docker 出奇怪问题。

### 2.3 加 Swap（4 GiB 内存必做）

构建前端时 `vite build` 和 `vue-tsc` 很吃内存，4 GiB 机器不加 swap 有一定概率被 OOM killer 干掉。

```bash
# 创建 2 GiB swap
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# 开机自动挂载
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

# 验证
free -h                    # Swap 那一行应显示 2.0Gi
```

再加一条降低 swap 倾向（内存够时尽量用物理内存）：

```bash
echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

### 2.4 【可选】apt 换国内源

服务器在国内时，Ubuntu 官方源可能较慢。腾讯云内网源最快：

```bash
# 先备份
sudo cp /etc/apt/sources.list /etc/apt/sources.list.bak

# Ubuntu 22.04 (jammy) 换腾讯云源
sudo sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.cloud.tencent.com/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://mirrors.cloud.tencent.com/ubuntu|g' /etc/apt/sources.list

sudo apt-get update
```

> 这一步只影响**宿主机**的 apt。Docker 构建**容器内**的 apt/pip 换源已经在
> `backend/Dockerfile` 里用 `ARG` 做好了，不需要你操心。

---

## 3. 安装 Docker 与镜像加速

### 3.1 安装 Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
```

若该脚本下载很慢，用国内镜像：

```bash
curl -fsSL https://get.docker.com | sudo sh -s -- --mirror Aliyun
```

加入 docker 组，避免每条命令都要 `sudo`：

```bash
sudo usermod -aG docker $USER
newgrp docker
```

验证：

```bash
docker version
docker compose version        # 需要 v2，输出应为 Docker Compose version v2.x
```

> `docker compose`（带空格）是 v2 插件；`docker-compose`（带横线）是 v1，本项目的
> `docker-compose.yml` 用了 `depends_on.condition` 和 `name:`，**必须用 v2**。

### 3.2 配置镜像加速（国内必做）

不配这个，`docker pull` 会直接报 `dial tcp ...: i/o timeout`。

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

两个细节：

- `mirror.ccs.tencentyun.com` 是腾讯云**内网**地址，仅 CVM 可用、**不计流量费**。
  非腾讯云机器会超时，用后面两个。
- `log-opts` 限制单个容器日志最大 10 MB、最多 3 个文件。**不加的话**，
  长期运行后 `/var/lib/docker/containers/` 会把磁盘写满。

验证加速生效：

```bash
docker pull hello-world && docker run --rm hello-world
```

### 3.3 设置开机自启

```bash
sudo systemctl enable docker
systemctl is-enabled docker     # 应输出 enabled
```

---

## 4. 获取代码

```bash
cd ~
git clone https://github.com/xhcprefect/enterprise-rag-kb.git enterprise-rag-kb
cd enterprise-rag-kb
```

**验收**：`ls` 能看到 `backend/ deploy/ frontend/ docker-compose.yml`。

```bash
# 确认脚本有执行权限（仓库里已是 100755）
test -x deploy/init.sh && echo "ok"
```

若报 `Permission denied`，说明检出方式丢了执行位，补一句：

```bash
chmod +x deploy/*.sh
```

---

## 5. 写配置

两个配置文件，职责不同，都要创建：

```bash
cp .env.example .env                     # 给 docker compose 用
cp backend/.env.example backend/.env     # 给应用用
```

### 5.1 根目录 `.env`（compose 变量）

只有这几项真正生效：`DATA_DIR`、`WEB_PORT`、`POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`。

**必须改的只有一项**：

```bash
# 生成一个强密码（只用字母数字，避免连接串转义问题）
NEWPW=$(python3 -c "import secrets;print(secrets.token_urlsafe(24).replace('-','').replace('_',''))")
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=${NEWPW}|" .env
grep '^POSTGRES_PASSWORD=' .env
```

> **为什么强调"只用字母数字"**：这个密码会被拼进
> `postgresql+psycopg://user:pass@db:5432/ragdb` 连接串。密码里含 `@ : / ? #`
> 会破坏 URL 解析，导致连接失败且报错信息晦涩。

> **用 `sed` 而不是 `nano`**：交互式编辑器在慢速 SSH 下容易产生控制字符，
> 曾出现过文件开头被写入乱码导致 `unexpected character` 的情况。改配置优先 `sed`。

#### `DATA_DIR`：默认别动

**单块系统盘的机器保持 `./data` 就好**，不要改成 `/data/...`。
`/data` 在根分区下、属于 root，`init.sh` 里的 `mkdir` 会直接
`Permission denied`。

确认是否挂了独立数据盘：

```bash
lsblk                              # 除了系统盘是否还有一块
df -h | grep -E '/data|/mnt'       # 独立盘通常已挂在 /data 或 /mnt
```

- **没有独立盘** → 保持 `DATA_DIR=./data`，什么都不用做。
- **有独立盘且要用**：

```bash
sudo mkdir -p /data/rag-kb
sudo chown -R ubuntu:ubuntu /data/rag-kb     # 这步不能省
sed -i 's|^DATA_DIR=.*|DATA_DIR=/data/rag-kb|' .env
```

> 为什么要 `chown`：容器内的进程以 **uid 1000** 运行。`init.sh` 会自动
> `chown 1000:1000` 上传与日志目录，但**只在以 root 身份运行时**才做。
> 你以 `ubuntu` 跑脚本时这一步被跳过；而 root 建出来的 `/data` 属主是 root，
> uid 1000 写不进去。后果是「文档记录能创建、上传却失败」，比 mkdir 报错难查得多。
>
> Ubuntu 云镜像的 `ubuntu` 用户 uid 通常就是 1000，所以 `./data` 天然正确。

（本文后续命令假设使用默认 `./data`，若改成绝对路径，备份/恢复脚本会自动跟随。）

### 5.2 `backend/.env`（应用配置）

需要改四项。**在同一个 shell 里连续执行**——`$JWT` 是 shell 变量，
断开 SSH 或换窗口就没了，此时 `sed` 会把值写成空字符串，效果等同没填：

```bash
cd ~/enterprise-rag-kb        # 注意：sed 用的是相对路径，必须在项目根目录

# ① JWT 密钥（64 字符随机串），两处一起写
JWT=$(python3 -c "import secrets;print(secrets.token_urlsafe(48))")
sed -i "s|^JWT_SECRET=.*|JWT_SECRET=${JWT}|" .env backend/.env

# ② 管理员密码
sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=你的强密码|" backend/.env

# ③ 向量模型 Key
sed -i "s|^EMBEDDING_API_KEY=.*|EMBEDDING_API_KEY=sk-你的硅基流动Key|" backend/.env

# ④ 对话模型 Key
sed -i "s|^LLM_API_KEY=.*|LLM_API_KEY=sk-你的对话服务Key|" backend/.env

unset JWT
```

写完后**立刻验证**（只输出长度，不打印密钥，避免密钥进 shell 历史和屏幕录制）：

```bash
for f in .env backend/.env; do
  v=$(grep -E '^JWT_SECRET=' "$f" | tail -1 | cut -d= -f2-)
  [ ${#v} -ge 32 ] && echo "ok  $f  (${#v} 字符)" || echo "!!  $f  不合格（${#v} 字符）"
done
```

两行都显示 `ok ... (64 字符)` 才算完成。

> 两个 `.env` 里的 `JWT_SECRET` **不必相同**：真正生效的是 `backend/.env`，
> 应用读它签发 token；根 `.env` 那份 compose 根本不读，只有 `deploy/init.sh`
> 拿它做占位检查。设成一样只是为了省心。

**其余保持默认**，特别注意这几项**不要手改**：

| 变量 | 为什么不能改 |
|---|---|
| `DATABASE_URL` | compose 会用 `db:5432` 覆盖成容器内地址 |
| `UPLOAD_DIR` / `LOG_DIR` | compose 会覆盖成 `/data/uploads`、`/data/logs` |
| `EMBEDDING_MODEL` | `bge-m3` 是 1024 维，向量维度写死在代码里，换模型必须重建索引 |
| `CORS_ORIGINS` | 生产走 Nginx 同源反代，浏览器不跨域，保留 localhost 即可 |

### 5.3 验收

一次性体检 `init.sh` 会检查的全部项（**不打印任何值**，只报告哪个还空着/还是占位值）：

```bash
cd ~/enterprise-rag-kb
chk() {  # 文件 键 占位值
  local v; v=$(grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2-)
  if   [ -z "$v" ];          then echo "  空      $1  $2"
  elif [ "$v" = "$3" ];      then echo "  占位值  $1  $2"
  fi
}
chk .env         POSTGRES_PASSWORD "change-me-strong-password"
chk .env         JWT_SECRET        "change-me-to-a-random-string"
chk backend/.env JWT_SECRET        "please-change-this-secret-in-production-0123456789"
chk backend/.env ADMIN_PASSWORD    "Admin@123456"
chk backend/.env EMBEDDING_API_KEY ""
chk backend/.env LLM_API_KEY       ""
echo "（以上无输出 = 全部就绪）"
```

> 这份清单与 `deploy/init.sh` 的校验**逐项对应**。根 `.env` 里的
> `ADMIN_PASSWORD` / `EMBEDDING_API_KEY` / `LLM_API_KEY` 是模板自带的装饰项，
> compose 不读、`init.sh` 也不检查，**空着没有影响**，不用管。

哪个出现在输出里就补哪个，补法同 §5.2。

> ⚠️ `backend/.env` 与 `.env` 都在 `.gitignore` 里，**永远不会**被 `git pull` 覆盖。
> 这既是好事（升级不丢配置），也意味着**换机器时必须手动迁移**。

---

## 6. 初始化检查

```bash
./deploy/init.sh
```

这个脚本做五件事：

| 步骤 | 内容 | 失败会怎样 |
|---|---|---|
| 1 | 检查 `.env` / `backend/.env` 是否存在 | 缺失则从 `.example` 复制并提示 |
| 2 | **占位值检查** | 仍是 `change-me-*` / `Admin@123456` 则**直接退出** |
| 3 | 建数据目录 `data/{pgdata,uploads,logs,backups}` | 若无权限会失败 |
| 4 | 探测 apt / pip / npm 源连通性 | **仅警告**，不阻断 |
| 5 | 检查 `WEB_PORT` 是否被占用 | 仅警告 |

**验收**：最后一行输出 `✓ 配置检查通过`，并提示下一步命令。

若第 2 步失败，说明有占位值没换，按提示改完再跑一次。

---

## 7. 构建与启动

### 7.1 先单独验后端（可选，但很省时间）

想确认换源是否生效，可以只构建后端，30 秒内就能看出来：

```bash
docker compose build api
```

看这一层耗时：

```
=> [worker 2/7] RUN for f in ... apt-get update && apt-get install ...
```

**应在 10 秒内完成**。若仍要两三分钟，说明 apt 镜像没生效，检查
`docker compose config` 或显式指定：

```bash
docker compose build --build-arg APT_MIRROR=mirrors.cloud.tencent.com api
```

### 7.2 全量构建

```bash
docker compose up -d --build
```

首次构建约 **5–8 分钟**（拉基础镜像 + 98 个 Python 包 + 前端 npm 依赖）。
后续只改业务代码时，依赖层命中缓存，通常 1 分钟内完成。

构建期间你会看到 `exporting to image` / `resolving provenance for metadata file`——
**这不是卡死**，是 buildx 在写元数据，等它走完即可。

### 7.3 启动顺序

compose 会自动按依赖启动，你不需要干预：

```
db (pg_isready 健康) → api (curl /health 健康) → worker
                                              └→ web
```

`worker` 特意排在 `api` 之后，是因为**初始管理员由 api 创建**。
若 worker 先起，它会抢着建表建管理员，导致 `backend/.env` 里的
`ADMIN_PASSWORD` 不生效——这是个容易被忽略的坑。

---

## 8. 上线验收

### 8.1 命令行检查

```bash
# ① 四个容器都在跑，api 是 (healthy)
docker compose ps

# ② 健康检查（WEB_PORT 改过的话把 80 换掉）
curl -s http://localhost/health
```

`/health` 返回形如：

```json
{
  "status": "ok",
  "app": "Enterprise RAG Knowledge Base",
  "llm": "deepseek-v4.1-flash",
  "llm_provider": "cloud",
  "embedding": "BAAI/bge-m3",
  "embedding_provider": "cloud",
  "worker": "idle"
}
```

`worker` 字段有三种值，含义要分清：

| 值 | 含义 | 是否正常 |
|---|---|---|
| `idle` | 队列空，无法观测 worker 是否活着 | 正常（空库时就是它） |
| `alive` | 有文档在 processing 且心跳新鲜 | 正常（正在入库） |
| `stale` | **有 pending 但没有新鲜心跳** | **异常**——worker 大概率没起来 |

> 注意 `idle` **不等于**"worker 活着"。这是有意设计的：老实现把空闲当存活，
> 结果 worker 挂了 `/health` 还报正常。要确认 worker 真活着，用下面的办法。

### 8.2 确认 worker 真的在跑

```bash
docker compose ps worker                    # 状态应为 Up
docker compose logs worker --tail 20        # 应看到启动日志
```

启动日志形如 `worker 启动：batch=8, stale=300s, chunk=800`。

> worker 空闲时**不打日志**（每 2 秒轮询一次，只在领到任务时输出
> `领取任务: <id>`）。所以"日志没有新内容"是正常的，不能据此判断它挂了。
> 判断依据看 `docker compose ps` 的状态，而不是日志是否滚动。

### 8.3 浏览器端完整验收

打开 `http://<服务器公网IP>/`，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录。

| # | 操作 | 期望结果 |
|---|---|---|
| 1 | 登录 | 进入问答页，右上角显示用户名 |
| 2 | 新建知识库 | 列表出现新库 |
| 3 | 上传一个 `.txt` / `.md` | 文档状态 `pending` |
| 4 | 等待数秒 | 状态变 `processing` → `ready` |
| 5 | 针对该文档提问 | 返回答案，且带 `[1]` 引用角标 |
| 6 | 点击引用 | 能看到原文片段 |
| 7 | `docker compose restart` 后再访问 | 数据仍在（验证持久化） |

**第 5 步过了之后，补建向量索引**（原因见 [§12.4](#124-向量索引默认没有需要手工补)）：

```bash
docker compose exec api python -m scripts.migrate_vector_index
```

必须在**至少入库过一份文档之后**跑——表是随首次入库创建的。

**第 4 步卡在 `pending` 不动**：worker 没工作，看 `docker compose logs worker`。
**第 4 步变 `failed`**：看文档的 `error_message` 字段和 worker 日志，常见是
API Key 无效或余额不足。

### 8.4 从外网验证

在**本地**机器执行（不是服务器上）：

```bash
curl -s http://<服务器公网IP>/health
```

能返回 JSON 说明安全组 + Nginx + api 三层都通了。

---

## 9. 【可选】域名与 HTTPS

### 9.1 前置条件

| 条件 | 说明 |
|---|---|
| 域名 | 已完成实名 |
| **ICP 备案** | **中国大陆地域，用域名访问 80 端口必须备案**，否则会被拦截 |
| DNS A 记录 | 指向服务器公网 IP |

> 备案通常要 1–3 周。没有备案就先直接用 IP 访问，功能完全不受影响。

### 9.2 配置步骤

1. DNS 加 A 记录：`kb.你的域名.com → <服务器公网IP>`
2. 安全组放行 443
3. 签发证书（webroot 模式）：

```bash
sudo apt-get install -y certbot
sudo certbot certonly --webroot -w /var/www/certbot -d kb.你的域名.com
```

4. 编辑 `frontend/nginx.conf`，取消文件末尾 HTTPS 段的注释，填入域名与证书路径
5. 重建前端：

```bash
docker compose build web && docker compose up -d web
```

6. 把 `backend/.env` 的 `CORS_ORIGINS` 改成 `https://kb.你的域名.com`

> `frontend/nginx.conf` 同时被复制进镜像（`frontend/Dockerfile` 的 `COPY nginx.conf`），
> 所以**改完必须重新 build**，光 `restart` 不生效。

---

## 10. 数据备份与恢复

### 10.1 数据都在哪

```
data/
├── pgdata/      # PostgreSQL 数据目录（向量、文档元数据、用户）
├── uploads/     # 上传的原始文件
├── logs/        # 应用日志
└── backups/     # 备份产物
```

**竞价实例必须做这一步**，否则机器被回收 = 数据全丢。

### 10.2 手动备份

```bash
./deploy/backup.sh
# 2026-10-24 14:12:00 备份完成: 18K + 493B -> ./data/backups
```

产物是两个文件：`ragdb-<时间戳>.sql.gz` + `uploads-<时间戳>.tar.gz`。
脚本会自动删除 **7 天前**的旧备份（`KEEP_DAYS`，可用环境变量覆盖），
即至少保留最近一周，不是"只留 7 份"。

脚本会**实际解压并校验内容**（不只看文件大小），所以不会出现"看起来成功"的空备份。

### 10.3 定时备份

```bash
crontab -e
```

加入一行（注意换成你的实际路径）：

```
20 3 * * * /home/ubuntu/enterprise-rag-kb/deploy/backup.sh >> /var/log/rag-backup.log 2>&1
```

验证 cron 生效：

```bash
tail -f /var/log/rag-backup.log
ls -lh data/backups/
```

### 10.4 恢复

```bash
./deploy/restore.sh data/backups/ragdb-20261024-141159.sql.gz \
                    data/backups/uploads-20261024-141159.tar.gz
```

会先停 `api` / `worker`，恢复后再启动，中间有 `yes` 二次确认。

### 10.5 换机器怎么迁

```bash
# 旧机器
./deploy/backup.sh
# 把 data/ 整个目录拷到新机器同名位置（scp / rsync / 对象存储均可）

# 新机器
git clone https://github.com/xhcprefect/enterprise-rag-kb.git
cd enterprise-rag-kb
# 手工重建 .env 与 backend/.env（它们不在 git 里！）
./deploy/init.sh
docker compose up -d --build
```

---

## 11. 安全加固（上线后尽快做）

按当前代码，以下几项**默认是敞开的**，建议部署验收通过后立即收紧：

| 项 | 现状 | 建议 |
|---|---|---|
| `/docs`、`/redoc`、`/openapi.json` | 公网可直接访问，暴露全部接口结构 | 生产环境关闭或加鉴权 |
| `/health` | 返回模型名称与服务商 | 生产只保留 `status` 与 `worker` |
| `DEBUG` | `config.py` 里有定义但**代码从未读取**，是死配置 | 可忽略，或补上实际用途 |
| `backend/.tok` | 本地调试遗留的 JWT 文件 | 服务器上不存在（未提交），本地应删除 |
| 管理员密码 | 首次登录前是 `.env` 里的值 | 登录后立即在个人中心修改 |

**其余安全基线（已满足，确认即可）**：

- [x] 数据库端口未暴露公网（compose 里只有 `expose`，无 `ports`）
- [x] API 端口未暴露公网（只在内网，由 Nginx 反代）
- [x] `.env` / `backend/.env` 未提交到 git（`.gitignore` 已覆盖）
- [x] 密码用 bcrypt 哈希存储
- [x] JWT 用 HS256，密钥为 64 字符随机串

---

## 12. 日常运维

### 12.1 更新代码

```bash
cd ~/enterprise-rag-kb
./deploy/backup.sh          # 本次有数据结构变更时才需要
git pull
docker compose up -d --build
```

`git pull` **不会**碰 `.env`、`backend/.env`、`data/`。

### 12.2 常用命令

```bash
docker compose ps                      # 状态
docker compose logs -f api             # API 日志（实时）
docker compose logs -f worker          # 入库日志
docker compose logs --tail 100 web     # Nginx 访问日志
tail -f data/logs/app.log              # 落盘的滚动日志

docker compose restart worker          # 重启单个服务
docker compose up -d --force-recreate api   # 重建容器（重新读 env_file）
docker compose down                    # 停止并删除容器（数据保留）
```

> **关于 `docker compose down -v`**：本项目的持久化全部走 **bind mount**
> （`${DATA_DIR}/pgdata`、`uploads`、`logs`），compose 里**没有定义任何命名卷**，
> 所以 `-v` 对这个编排**不会删掉你的数据**。但这不代表可以随手用——
> 真正的风险是误删宿主机上的 `data/` 目录。
>
> 要彻底重来（清空数据库重装）应当显式操作：
> ```bash
> docker compose down
> sudo rm -rf data/pgdata        # 这一步才会真的丢数据
> ```
> 执行前先 `./deploy/backup.sh`。

### 12.3 进数据库查状态

```bash
# 向量条数
docker compose exec db psql -U postgres -d ragdb \
  -c "select count(*) from langchain_pg_embedding"

# 文档状态分布
docker compose exec db psql -U postgres -d ragdb \
  -c "select status, count(*) from documents group by 1"

# 向量列的类型（应为 vector(1024)）
docker compose exec db psql -U postgres -d ragdb \
  -c "\d langchain_pg_embedding" | grep embedding
```

### 12.4 向量索引：默认没有，需要手工补

这一条容易被忽略，值得单独说明。

`langchain-postgres 0.0.19` 的 `PGVector` **只建表和集合，从不建向量索引**。
即使传了 `embedding_length=1024`（本项目传了），它只影响列类型是
`vector(1024)` 还是无维度的 `vector`，**不会创建 HNSW 索引**。

结果：**检索走全表顺序扫描**。数据量小时无感，几万条向量后每次问答都会明显变慢。

补建索引：

```bash
# 先看会做什么，不改任何东西
docker compose exec api python -m scripts.migrate_vector_index --dry-run

# 确认无误后执行（幂等，可重复跑）
docker compose exec api python -m scripts.migrate_vector_index
```

脚本用 `CREATE INDEX CONCURRENTLY` 建 `ix_langchain_pg_embedding_hnsw`（余弦距离），
不阻塞读写；已在索引存在时跳过。**表还不存在**（还没入库过任何文档）时会提示
"无需迁移"并正常退出——先上传一份文档再跑。

验证：

```bash
docker compose exec db psql -U postgres -d ragdb \
  -c "\di ix_langchain_pg_embedding_hnsw"
```

> **什么时候必须跑**：只要知识库规模超过几千个分块就该建。
> **新库也不用跳过**——它不是自动建的。

### 12.5 回滚

```bash
git log --oneline -5
git checkout <上一个提交>
docker compose up -d --build
```

镜像里带的是代码和锁定的依赖，回滚到旧提交即回到旧版本。
**注意**：`init_db()` 只增不减，回滚后数据库里多出来的列不会自动删除（无害）。

---

## 13. 故障排查速查表

| 现象 | 最可能的原因 | 处理 |
|---|---|---|
| `docker pull` 报 `i/o timeout` | 未配镜像加速 | 见 [§3.2](#32-配置镜像加速国内必做) |
| 构建卡在 `apt-get update` 数分钟 | apt 镜像未生效 | `docker compose build --build-arg APT_MIRROR=mirrors.cloud.tencent.com api` |
| `npm ci` 报 `can only install with an existing package-lock.json` | Dockerfile 缺 `COPY package.json package-lock.json ./` | 拉最新代码，仓库已修复 |
| `./deploy/init.sh` 报 `Permission denied` | 执行位丢失 | `chmod +x deploy/*.sh` |
| `init.sh` 输出多行 `mkdir: Permission denied` | `DATA_DIR` 指向了无写权限的路径（如 `/data`，属 root） | 改回 `DATA_DIR=./data`，或 `sudo chown -R ubuntu:ubuntu /data/rag-kb` |
| 上传成功但文档 `failed`，日志提示写入失败 | `DATA_DIR` 属主不是 uid 1000 | `sudo chown -R 1000:1000 <DATA_DIR>/{uploads,logs}` |
| 数据量大后问答明显变慢 | 没有向量索引，走全表扫描 | 见 [§12.4](#124-向量索引默认没有需要手工补) |
| `init.sh` 报 `JWT_SECRET 仍是占位值` | 根 `.env` 未改 | 见 [§5.2](#52-backendenv应用配置)，两处都要改 |
| `failed to read backend/.env: unexpected character` | 文件被编辑器写入控制字符 | `cp .env.bak .env` 恢复，改用 `sed` 修改 |
| `api` 反复重启 | 密钥为空 / 数据库连不上 | `docker compose logs api` |
| `/health` 里 `worker: stale` | worker 没起或已挂 | `docker compose logs worker` |
| 文档一直 `pending` | 同上，没有 worker 领取 | 同上 |
| 文档 `failed` | 格式不支持 / 云端 API 报错 | 看 `error_message` 字段 |
| 问答报鉴权失败 | API Key 失效或余额不足 | 错误信息里会带服务商域名 |
| 页面 404 | SPA fallback 失效 | 检查 `frontend/nginx.conf` 的 `try_files` |
| 上传失败 | 超过 `MAX_UPLOAD_MB`（默认 50）或 Nginx 限制（100 MB） | 调大 `.env` 的值并重建 |
| 改了 `.env` 但没生效 | `restart` **不会**重读 `env_file` | `docker compose up -d --force-recreate api` |
| 磁盘满 | 容器日志堆积 | 检查 `daemon.json` 的 `log-opts` 是否配了 |

### 13.1 一条命令收集现场

出问题时先跑这个，把输出发给别人也够用：

```bash
cd ~/enterprise-rag-kb
{
  echo "===== 系统 ====="; uname -a; free -h; df -h /
  echo "===== 容器 ====="; docker compose ps
  echo "===== 健康 ====="; curl -s "http://localhost:${WEB_PORT:-80}/health"
  echo "===== api 日志 ====="; docker compose logs api --tail 50
  echo "===== worker 日志 ====="; docker compose logs worker --tail 50
} 2>&1 | tee /tmp/diagnose-$(date +%Y%m%d-%H%M%S).log
```

---

## 附录 A：端口一览

| 端口 | 容器 | 是否对外 | 说明 |
|---|---|---|---|
| 80 | web | **是** | 唯一对外入口，由 `${WEB_PORT}` 控制 |
| 443 | web | 可选 | 配 HTTPS 后 |
| 8000 | api | 否 | 仅 Docker 内网，Nginx 反代 |
| 5432 | db | 否 | 仅 Docker 内网 |

## 附录 B：配置项对照

| 文件 | 变量 | 作用 | 谁改 |
|---|---|---|---|
| `.env` | `DATA_DIR` | 数据根目录 | 你 |
| `.env` | `WEB_PORT` | 对外端口 | 你（默认 80） |
| `.env` | `POSTGRES_PASSWORD` | 数据库密码 | **必须改** |
| `.env` | `JWT_SECRET` | 仅用于 `init.sh` 占位检查 | **必须改**（compose 不读它） |
| `backend/.env` | `JWT_SECRET` | **实际生效**的 JWT 密钥 | **必须改** |
| `backend/.env` | `ADMIN_PASSWORD` | 初始管理员密码 | **必须改** |
| `backend/.env` | `EMBEDDING_API_KEY` | 向量服务 Key | **必须改** |
| `backend/.env` | `LLM_API_KEY` | 对话服务 Key | **必须改** |
| `backend/.env` | `MAX_UPLOAD_MB` | 单文件上限 | 可选 |
| `backend/.env` | `RETRIEVAL_TOP_K` | 检索返回条数 | 可选 |
| `backend/.env` | `CHUNK_SIZE` / `CHUNK_OVERLAP` | 分块参数 | 可选（改后需重新入库） |

## 附录 C：时间预估

| 阶段 | 耗时 |
|---|---|
| 购买 + 开机 | 3 分钟 |
| 系统初始化 + Docker | 5 分钟 |
| 克隆 + 配置 | 5 分钟 |
| 首次构建 | 5–8 分钟 |
| 验收 | 5 分钟 |
| **合计** | **约 25 分钟**（不含备案） |

## 附录 D：成本参考（腾讯云南京，2026 年）

| 项 | 规格 | 大致月费 |
|---|---|---|
| 竞价实例 | 2 核 4 GiB | 几十元 |
| 按流量计费 | 月出网 10 GB 内 | 几元 |
| 云硬盘 | 50 GiB | 十元级 |
| **模型 API** | 向量 `bge-m3` 免费；对话按量 | 视用量 |

> 具体价格以控制台为准。向量化免费是选择 `BAAI/bge-m3` 的主要原因。

---

*本文与仓库实际配置同步维护。若命令与代码不一致，以代码为准并提 issue。*
