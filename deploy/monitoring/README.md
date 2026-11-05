# 可选监控与告警通知

基础部署不启动 Prometheus 或 Alertmanager。`docker-compose.monitoring.yml` 开启采集与规则求值；再叠加 `docker-compose.alerting.yml`，并渲染 `RAGKB_ALERTMANAGER_TARGET`，才会发送通知。固定镜像为 `prom/prometheus:v3.1.0`、`prom/alertmanager:v0.28.1`。

本文命令从仓库根目录执行，沿用同一个 Compose 项目名。已经启用 HTTPS 的部署，在下文所有 Compose 命令中同时加入 `-f docker-compose.https.yml`。

## 准备运行目录与采集令牌

运行目录与原始 secret 都在仓库外。原件在 Linux 上必须仅属主可读；渲染器生成 0444 的容器副本，让 UID/GID `65534:65534` 可读取只读挂载。副本的权限不等同于原件的 0600：本例把它们放在 0700 的上级目录里，限制其他宿主机帐号访问。

```bash
sudo install -d -m 700 -o "$(id -un)" -g "$(id -gn)" /srv/ragkb-runtime
export RAGKB_RUNTIME_ROOT=/srv/ragkb-runtime
install -d -m 700 "$RAGKB_RUNTIME_ROOT/secrets"
export RAGKB_MONITORING_OUT="$RAGKB_RUNTIME_ROOT/monitoring"
export RAGKB_METRICS_TOKEN_FILE="$RAGKB_RUNTIME_ROOT/secrets/metrics_token"
install -m 600 /dev/null "$RAGKB_METRICS_TOKEN_FILE"
vi "$RAGKB_METRICS_TOKEN_FILE"   # 填入与 API 的 METRICS_TOKEN 相同的单行令牌

export RAGKB_PROMETHEUS_CONFIG_DIR="$RAGKB_MONITORING_OUT/prometheus"
export RAGKB_PROMETHEUS_SECRET_DIR="$RAGKB_MONITORING_OUT/secrets"
export RAGKB_ALERTMANAGER_CONFIG_DIR="$RAGKB_MONITORING_OUT/alertmanager"
export RAGKB_ALERTMANAGER_SECRET_DIR="$RAGKB_MONITORING_OUT/alertmanager/secrets"
```

API 的 `METRICS_TOKEN` 在部署配置中独立设置，修改后需用原部署的 Compose 文件重新创建 API 容器。接口不接受管理员 JWT 作为采集令牌；未设置令牌返回 503，令牌错误返回 401。不要把真实令牌写进仓库或命令行明文参数。

上面的目录变量需在执行维护命令的终端中设置。Git Bash/NTFS 无法用 POSIX mode 位证明 0600，权限拒绝分支由 Linux 部署与 CI 验证，Windows 上需自行设置 ACL。

## 只开启采集

```bash
export RAGKB_ALERTMANAGER_TARGET=''
ENV_FILE=/dev/null OUT_DIR="$RAGKB_MONITORING_OUT" \
  bash deploy/monitoring/render-prometheus.sh

docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d prometheus
curl --noproxy '*' -fsS http://127.0.0.1:9090/-/ready
```

`ENV_FILE=/dev/null` 表示渲染器只使用本终端显式设置的变量；它不读取项目环境文件。默认抓取 `api:8000/api/v1/operations/metrics`，采集周期 30s、超时 10s；调整时必须满足超时不大于周期。

Prometheus 默认只绑定 `127.0.0.1:9090`，`/-/reload` 生命周期接口关闭，实际返回 403。通过宿主机或 SSH 隧道检查 Targets，确认 `ragkb-api` 为 UP。API 指标是单进程累计值，`ragkb_metrics_process_local=1`，进程重启会清零，不能直接当作多副本的共享总数。

## 开启通知

接收器必须支持 **Alertmanager generic webhook v4 JSON**，其中含 `version`、`status`、`groupLabels`、`alerts` 等字段。飞书、钉钉、企业微信机器人的原生消息协议不同，需要部署适配器后填适配器地址。

先在仓库外准备 URL 文件，使用编辑器填入实际地址，避免含 token 的 URL 进入命令历史：

```bash
export RAGKB_ALERT_WEBHOOK_FILE="$RAGKB_RUNTIME_ROOT/secrets/alert_webhook_url"
install -m 600 /dev/null "$RAGKB_ALERT_WEBHOOK_FILE"
vi "$RAGKB_ALERT_WEBHOOK_FILE"

export RAGKB_ALERTMANAGER_TARGET=alertmanager:9093
ENV_FILE=/dev/null OUT_DIR="$RAGKB_MONITORING_OUT" \
  bash deploy/monitoring/render-prometheus.sh
ENV_FILE=/dev/null OUT_DIR="$RAGKB_MONITORING_OUT" \
  bash deploy/monitoring/render-alertmanager.sh

docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
  -f docker-compose.alerting.yml up -d prometheus alertmanager
# Prometheus 已运行时，配置文件内容变化未必触发容器重建，显式重读配置。
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
  -f docker-compose.alerting.yml restart prometheus
curl --noproxy '*' -fsS http://127.0.0.1:19093/-/ready
```

`RAGKB_ALERTMANAGER_TARGET` 只接受逗号分隔的 `host:port`，最多五个；不接受 URL、空白或缺失端口。不要给这个变量填 webhook URL。真正的 webhook URL 仅由 Alertmanager 的 `url_file` 从只读 secret 副本读取，不进入配置正文、Compose 参数或普通渲染输出；校验错误不回显其内容。URL 文件允许一个可选 LF/CRLF 行尾，拒绝内部 CR/LF、NUL、控制字符、userinfo 和非法端口。

默认通知参数：

| 变量 | 默认值 | 含义 |
| --- | --- | --- |
| `RAGKB_AM_GROUP_WAIT` | `30s` | 新组首次通知前等待 |
| `RAGKB_AM_GROUP_INTERVAL` | `5m` | 同组后续变化通知间隔 |
| `RAGKB_AM_REPEAT_INTERVAL` | `4h` | 持续告警重复通知间隔，必须是 group interval 的整数倍 |
| `RAGKB_AM_RESOLVE_TIMEOUT` | `5m` | 未提供结束时间的告警恢复超时 |
| `RAGKB_AM_WEBHOOK_TIMEOUT` | `10s` | 单次请求超时，实际还受 group interval 限制 |
| `RAGKB_AM_MAX_ALERTS` | `0` | 0 表示不截断组内告警 |

`send_resolved: true` 会发送恢复通知。Prometheus 规则恢复后发送结束时间，不能把停止 Prometheus 当成规则正常恢复的替代。基础 `internal` 网络是普通 bridge，允许出网；接收地址仍需能从 Alertmanager 容器访问，并配置正确的 DNS、TLS 和防火墙。

Alertmanager 管理 API 默认绑定宿主机 `127.0.0.1:19093`，包含 silences 等管理操作，不能向公网直接发布。`--cluster.listen-address=` 只关闭集群 gossip；`--web.external-url` 只影响外部链接，不会关闭管理 API。

## 更新配置与关闭通知

修改规则、目标或通知参数后，重新运行对应渲染器，再显式重启读取配置的服务：

```bash
ENV_FILE=/dev/null OUT_DIR="$RAGKB_MONITORING_OUT" \
  bash deploy/monitoring/render-prometheus.sh
ENV_FILE=/dev/null OUT_DIR="$RAGKB_MONITORING_OUT" \
  bash deploy/monitoring/render-alertmanager.sh

docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
  -f docker-compose.alerting.yml restart prometheus alertmanager
```

`restart` 不会加载新的 Compose 环境或挂载配置。改了端口、目录或命令参数时先 `up -d` 让 Compose 更新容器；仅修改挂载配置文件的内容仍需 `restart`。渲染器可重复更新原来的 0444 secret 副本。Prometheus 的 `--check` 只检查产物存在，正常渲染时才运行可用的 promtool；Alertmanager 的 `--check` 会在本地镜像可用时运行 amtool。校验不发送通知，缺少本地校验镜像会提示跳过，不能据此认定真实链路已通过。

恢复到只采集模式时，先显式停止 Alertmanager，再清空目标、重新渲染并重启 Prometheus：

```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
  -f docker-compose.alerting.yml stop alertmanager
export RAGKB_ALERTMANAGER_TARGET=''
ENV_FILE=/dev/null OUT_DIR="$RAGKB_MONITORING_OUT" \
  bash deploy/monitoring/render-prometheus.sh
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml restart prometheus
```

只从命令中去掉 `docker-compose.alerting.yml` 再 `up`，不会停止此前已创建的 Alertmanager。上述流程保留其持久卷、silences 和配置，方便以后重新启用。

## 审计失败与验证

API 的 `ragkb_audit_write_failures_total` 是无业务标签的进程内计数，初始零值也输出；管理员摘要的 `data.audit.write_failures` 与它同源，数据库查询降级时仍保留。审计失败只记录固定事件与净化后的异常类型，不输出 SQL、异常正文或 traceback。

[审计规则](prometheus/rules/ragkb-audit.yml.in) 对最近 5 分钟的新增失败触发 warning，对持续失败触发 critical。[规则测试](prometheus/tests/audit.yml) 覆盖孤立失败、持续失败和恢复。

下面两个脚本是有限的隔离回归，只连接自己创建并带 owner 标签的容器和本地 webhook，不读取业务配置，不向真实接收器发送内容。它们按精确资源 ID 核对归属并清理；缺镜像会失败，需先准备：

```bash
docker pull prom/prometheus:v3.1.0
docker pull prom/alertmanager:v0.28.1
docker pull python:3.12-slim
bash deploy/tests/test-monitoring.sh
bash deploy/tests/test-alerting.sh
```

第一项检查 UID 65534 的配置读取、真实 promtool 规则单元测试、启动和关闭的 lifecycle。第二项通过合成指标从 1 降到 0，让真实 Prometheus → Alertmanager → 本地 fixture 投递同一探针的 firing 与 resolved；另行制造连接拒绝，通过通知失败计数确认已发生投递失败，再检查该版本失败日志是否暴露合成 URL 标记。此项实测不代表所有上游版本、所有错误路径都能自动脱敏，升级镜像时必须重新验证。

两项回归均已接入 [CI](../../.github/workflows/checks.yml)。本地回归通过只证明合成链路；上线仍需部署方用真实接收器验收触发、恢复、重试与值班响应。
