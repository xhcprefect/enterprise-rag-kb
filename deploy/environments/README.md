# 同机环境配置模板

只提交 `.example` 模板。实际配置由管理员复制到同目录：

```
production/.env           # Compose / 维护变量
production/backend.env    # 应用变量
staging/.env
staging/backend.env
```

统一入口 `bash deploy/env.sh <production|staging> init` 会在文件不存在时复制模板并设置 0600，不覆盖已有文件；随后填写独立凭据和不可变镜像标签，再重新执行 `init`。`RAGKB_ENV_ROOT` 可指向另一份独立配置根用于临时验收，但不能把两套环境指向同一个目录或互相嵌套的数据目录。

两套环境的 `COMPOSE_PROJECT_NAME`、`DATA_DIR`、`BACKUP_DIR`、数据库名、应用凭据、发布端口和可选 TLS/监控端口均独立。生产默认公网 80，staging 仅 `127.0.0.1:18080`，可通过 SSH 隧道验收；不提供共享公网反代。应用都使用 `ENVIRONMENT=production`，部署身份由目录与 Compose project 表达。

`COMPOSE_FILE` 默认 `docker-compose.yml:docker-compose.host.yml`；可追加 `docker-compose.https.yml`、`docker-compose.monitoring.yml`、`docker-compose.alerting.yml`。告警必须同时加载监控覆盖；入口为已启用的监控/告警自动追加专属 host 配额覆盖，核心默认不新增这些服务。资源值均为**单容器**运行时上限，不约束镜像构建，也不是整个环境的总 CPU/内存硬隔离；启用可选 worker profile 前请重新核算 4C8G 主机预算。监控/告警的可选宿主发布端口在模板中分别为 production `9090/9093` 与 staging `19090/19093`，渲染目录必须使用本环境独立绝对路径。可选配置和仓库外 secret 路径示例见各环境模板的注释；HTTPS 的 ACME 默认目录为本环境 `${DATA_DIR}/acme`，也参与跨环境冲突校验。

`docker compose config` 可能输出配置中的密钥，仅管理员显式请求时执行；日常入口不会打印配置内容。旧单环境根 `.env` + `backend/.env` 命令保持兼容，不通过此入口管理。
