#!/usr/bin/env bash
# 有限、隔离的告警投递回归：
#   renderer 校验（目标/URL/secret/时长）-> 真实 Prometheus 规则由可切换合成指标触发
#   -> Alertmanager -> 本地 generic webhook 收到 firing 与 resolved 两条通知。
#
# 只使用自有 Docker 网络与容器（带 owner label，create 后立即登记再 start），
# 宿主端口全部 127.0.0.1 随机，不连接业务 api/模型，不向任何外部 webhook 发请求。
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
prom_image=prom/prometheus:v3.1.0
am_image=prom/alertmanager:v0.28.1
py_image=python:3.12-slim

command -v docker >/dev/null
command -v curl >/dev/null
for image in "$prom_image" "$am_image" "$py_image"; do
  docker image inspect "$image" >/dev/null 2>&1 || {
    printf '缺少测试镜像 %s（CI 会先 docker pull；这里不自动拉取）\n' "$image" >&2; exit 1; }
done

# 显式挑选 Python：CI 是 python3；Git Bash 可能是 D:/python/python.exe 或
# backend/.venv/Scripts/python.exe。读取外部文件时一律加 -I。
if [[ -n "${PYTHON:-}" ]]; then
  python_bin=$PYTHON
elif [[ -x "$repo/backend/.venv/Scripts/python.exe" ]]; then
  python_bin="$repo/backend/.venv/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
  python_bin=python3
elif command -v python >/dev/null 2>&1; then
  python_bin=python
else
  printf '缺少 Python（可用 PYTHON 指定）\n' >&2; exit 1
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/ragkb-alerting-test.XXXXXX")
owner=$(basename "$work")
owner_label=io.ragkb.alerting-test.owner
network="ragkb-alerting-test-$owner"
network_id=''
container_ids=()
host_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
# Windows 上 Git Bash 会把容器内路径改写成本机路径；统一禁用。
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

cleanup() {
  local code=$? actual
  for container in "${container_ids[@]}"; do
    [[ -n "$container" ]] || continue
    actual=$(docker inspect -f "{{index .Config.Labels \"$owner_label\"}}" "$container" 2>/dev/null || true)
    if [[ "$actual" == "$owner" ]]; then
      if (( code != 0 )); then docker logs "$container" >&2 || true; fi
      docker rm -f "$container" >/dev/null || code=1
    else
      printf '告警测试容器归属无法核对，保留 %s\n' "$container" >&2
      code=1
    fi
  done
  if [[ -n "$network_id" ]]; then
    actual=$(docker network inspect -f "{{index .Labels \"$owner_label\"}}" "$network_id" 2>/dev/null || true)
    if [[ "$actual" == "$owner" ]]; then
      docker network rm "$network_id" >/dev/null || code=1
    else
      printf '告警测试网络归属无法核对，保留 %s\n' "$network_id" >&2
      code=1
    fi
  fi
  if (( code == 0 )); then rm -rf "$work"; else printf '告警回归现场保留在 %s\n' "$work" >&2; fi
  exit "$code"
}
trap cleanup EXIT

# create -> 登记 -> start：即使 start 因挂载/端口失败，清理也能按精确 ID 收到容器。
# 必须直接调用（不能放进 $(...) 子 shell），否则 container_ids 的修改不会回到主
# shell，EXIT trap 就看不到本轮资源。ID 通过全局 owned_id 返回。
run_owned() { # 名称 及 docker run 参数
  local name=$1; shift
  owned_id=$(docker create --name "$name" --label "$owner_label=$owner" "$@")
  container_ids+=("$owned_id")
  docker start "$owned_id" >/dev/null
}

chmod 0755 "$work"
mkdir -p "$work/out"

webhook_url='http://fixture:18080/webhook'
(umask 077; printf '%s\n' "$webhook_url" > "$work/webhook-url")
chmod 600 "$work/webhook-url" 2>/dev/null || true
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) printf 'note: NTFS/Git Bash 不提供源文件 0600 语义\n' ;;
  *) [[ $(stat -c '%a' "$work/webhook-url") == 600 ]] ;;
esac

render_prom() { # 输出目录 [target]
  local out=$1 target=${2:-}
  ENV_FILE=/dev/null OUT_DIR="$out" RAGKB_SCRAPE_INTERVAL=1s RAGKB_SCRAPE_TIMEOUT=1s \
    RAGKB_ALERTMANAGER_TARGET="$target" RAGKB_METRICS_TOKEN_FILE='' \
    bash "$repo/deploy/monitoring/render-prometheus.sh"
}
render_am() { # 输出目录
  ENV_FILE=/dev/null OUT_DIR="$1" RAGKB_ALERT_WEBHOOK_FILE="$work/webhook-url" \
    RAGKB_AM_GROUP_WAIT=1s RAGKB_AM_GROUP_INTERVAL=1s RAGKB_AM_REPEAT_INTERVAL=2s \
    RAGKB_AM_RESOLVE_TIMEOUT=10s RAGKB_AM_WEBHOOK_TIMEOUT=5s \
    bash "$repo/deploy/monitoring/render-alertmanager.sh"
}

# ---------- 1) 渲染：默认“只采集”，启用时才有 alerting 块 ----------
render_prom "$work/default" >/dev/null 2>&1
! grep -q '^alerting:' "$work/default/prometheus/prometheus.yml" || {
  printf '未设置告警目标时不应渲染 alerting 块\n' >&2; exit 1; }

render_prom "$work/rendered" alertmanager:9093 >/dev/null
grep -q '^alerting:' "$work/rendered/prometheus/prometheus.yml"
grep -q '^            - "alertmanager:9093"$' "$work/rendered/prometheus/prometheus.yml"

# 非法目标必须被拒：换行/注入、引号、端口越界、缺端口、空白、URL scheme、
# 连续/末尾逗号。错误信息不得回显原始值。
target_ok() { # 目标 期望(accept|reject)
  local err
  if err=$(ENV_FILE=/dev/null OUT_DIR="$work/bad-prom" RAGKB_ALERTMANAGER_TARGET="$1" \
      bash "$repo/deploy/monitoring/render-prometheus.sh" 2>&1); then
    [[ "$2" == accept ]] || { printf '非法告警目标未被拒绝（类别 %s）\n' "$3" >&2; exit 1; }
  else
    [[ "$2" == reject ]] || { printf '合法告警目标被拒绝（类别 %s）: %s\n' "$3" "$err" >&2; exit 1; }
    [[ "$err" != *"$1"* ]] || { printf '错误信息回显了目标值（类别 %s）\n' "$3" >&2; exit 1; }
  fi
}
target_ok 'alertmanager:9093' accept ok
target_ok 'am:9093,am2:9094' accept multi
target_ok $'alertmanager:9093\nevil: true' reject newline
target_ok '"x":9093' reject quote
target_ok 'alertmanager:99999' reject port
target_ok 'alertmanager' reject no-port
target_ok 'alert manager:9093' reject space
target_ok 'https://alertmanager:9093' reject scheme
target_ok 'alertmanager:9093,' reject trailing-comma
target_ok 'a:9093,,b:9094' reject empty-item

# ---------- 2) Alertmanager 渲染与 secret 校验 ----------
render_output=$(render_am "$work/rendered")
[[ "$render_output" != *"$webhook_url"* ]] || { printf '渲染输出泄露了 webhook URL\n' >&2; exit 1; }
! grep -q "$webhook_url" "$work/rendered/alertmanager/alertmanager.yml" || {
  printf 'alertmanager.yml 不应包含 webhook URL\n' >&2; exit 1; }
grep -q 'url_file: /etc/alertmanager-secrets/webhook_url' "$work/rendered/alertmanager/alertmanager.yml"
grep -q 'send_resolved: true' "$work/rendered/alertmanager/alertmanager.yml"
[[ $(stat -c '%a' "$work/rendered/alertmanager/secrets/webhook_url") == 444 ]]
for directory in "$work/rendered/alertmanager" "$work/rendered/alertmanager/secrets"; do
  [[ $(stat -c '%a' "$directory") == 755 ]]
done
# --check 只做本地 amtool 语法校验，不联网、不投递。
render_check=$(ENV_FILE=/dev/null OUT_DIR="$work/rendered" \
  bash "$repo/deploy/monitoring/render-alertmanager.sh" --check 2>&1)
[[ "$render_check" != *"$webhook_url"* ]]

# 错误 URL 必须被拒；错误信息不得回显 URL 值。fixture 采用 0600，确保在 Linux
# 上先通过权限检查、真正走到 URL 校验。
write_url() { # 内容（用 %b 解释 \n 等转义）
  rm -f "$work/bad-url"
  (umask 077; printf '%b' "$1" > "$work/bad-url")
  chmod 600 "$work/bad-url" 2>/dev/null || true
}
bad_url_cases=(
  'ftp://receiver.invalid/hook'
  'https://user:pass@receiver.invalid/hook'
  'https://receiver.invalid/hook\nsecond-line'
  'https://receiver.invalid/hook\n\n'
  'https://receiver.invalid/hook\rinside'
  'https://receiver.invalid/hook\000inside'
  $'https://receiver.invalid/hook\001ctrl'
  'https://receiver.invalid/hook with space'
  ''
  'not-a-url'
  'https://receiver.invalid:99999/hook'
  'https://receiver.invalid:/hook'
  'https://receiver.invalid:8443:9/hook'
)
for bad in "${bad_url_cases[@]}"; do
  write_url "$bad"
  if err=$(ENV_FILE=/dev/null OUT_DIR="$work/bad-am" RAGKB_ALERT_WEBHOOK_FILE="$work/bad-url" \
      bash "$repo/deploy/monitoring/render-alertmanager.sh" 2>&1); then
    printf '非法 webhook URL 未被拒绝: %q\n' "$bad" >&2; exit 1
  fi
  # 空串没有可回显的内容，跳过该断言。
  if [[ -n "$bad" ]]; then
    [[ "$err" != *"$bad"* ]] || { printf '错误信息回显了 URL（已脱敏输出）\n' >&2; exit 1; }
  fi
  # 错误信息不应包含任何 URL 片段。
  [[ "$err" != *'receiver.invalid'* ]] || { printf '错误信息泄露了 webhook 主机\n' >&2; exit 1; }
done
# repeat_interval 非 group_interval 整数倍必须被拒（含 ms 精度不取整）。
for pair in '5m 7m' '1s 1500ms'; do
  read -r group repeat <<< "$pair"
  if ENV_FILE=/dev/null OUT_DIR="$work/bad-am" RAGKB_ALERT_WEBHOOK_FILE="$work/webhook-url" \
      RAGKB_AM_GROUP_INTERVAL="$group" RAGKB_AM_REPEAT_INTERVAL="$repeat" \
      bash "$repo/deploy/monitoring/render-alertmanager.sh" >/dev/null 2>&1; then
    printf 'repeat/group 非整数倍未被拒绝: %s / %s\n' "$group" "$repeat" >&2; exit 1
  fi
done
# 仓库内的已有文件必须在读取内容前被拒。
if err=$(ENV_FILE=/dev/null OUT_DIR="$work/bad-am" RAGKB_ALERT_WEBHOOK_FILE="$repo/docker-compose.alerting.yml" \
    bash "$repo/deploy/monitoring/render-alertmanager.sh" 2>&1); then
  printf '仓库内 webhook 文件未被拒绝\n' >&2; exit 1
fi
[[ "$err" == *'webhook URL 文件不能在仓库内'* ]]

# 一个 LF 或 CRLF 行尾可以接受，不能静默吞掉内部 CR/LF/NUL。
write_url 'http://fixture:18080/webhook\r\n'
ENV_FILE=/dev/null OUT_DIR="$work/crlf-am" RAGKB_ALERT_WEBHOOK_FILE="$work/bad-url" \
  bash "$repo/deploy/monitoring/render-alertmanager.sh" >/dev/null
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) printf 'skip: webhook 原件 0644 拒绝由 Linux CI 验证\n' ;;
  *)
    chmod 0644 "$work/bad-url"
    if ENV_FILE=/dev/null OUT_DIR="$work/bad-permissions" RAGKB_ALERT_WEBHOOK_FILE="$work/bad-url" \
        bash "$repo/deploy/monitoring/render-alertmanager.sh" >/dev/null 2>&1; then
      printf 'Linux 上 0644 webhook 原件应被拒绝\n' >&2; exit 1
    fi
    chmod 0600 "$work/bad-url"
    ;;
esac

# 复制脚本到本轮目录，用故意损坏的模板验证显式白名单。未知变量即使已 export
# 也必须在运行 promtool/amtool 前被拒，不能把整个 ENVIRON 当成白名单。
guard_repo="$work/template-guard"
mkdir -p "$guard_repo/deploy/monitoring/prometheus/rules" "$guard_repo/deploy/monitoring/alertmanager"
for renderer in prometheus alertmanager; do
  cp "$repo/deploy/monitoring/render-$renderer.sh" "$guard_repo/deploy/monitoring/"
  printf '%s\n' '# ${RAGKB_UNDECLARED_TEST_VARIABLE}' > "$guard_repo/deploy/monitoring/$renderer/$renderer.yml.in"
  if err=$(ENV_FILE=/dev/null OUT_DIR="$work/template-guard-out" \
      RAGKB_ALERT_WEBHOOK_FILE="$work/webhook-url" RAGKB_UNDECLARED_TEST_VARIABLE=synthetic-template-secret \
      bash "$guard_repo/deploy/monitoring/render-$renderer.sh" 2>&1); then
    printf '%s 模板接受了白名单之外的导出变量\n' "$renderer" >&2; exit 1
  fi
  [[ "$err" == *'未知占位符'* && "$err" != *'synthetic-template-secret'* ]] || {
    printf '%s 未在模板白名单处拒绝或回显了变量值\n' "$renderer" >&2; exit 1; }
done

# ---------- 3) 测试专用采集与规则（不改仓库规则文件） ----------
mkdir -p "$work/rendered/secrets"
chmod 0755 "$work/rendered/secrets"
if [[ ! -f "$work/rendered/secrets/metrics_token" ]]; then
  (umask 022; printf 'alerting-test-only\n' > "$work/rendered/secrets/metrics_token")
fi
chmod 0444 "$work/rendered/secrets/metrics_token"

# 追加对本地 fixture 的采集（scrape_configs 是最后一个顶层键，追加一项即可）。
cat >> "$work/rendered/prometheus/prometheus.yml" <<'YAML'
  - job_name: alerting-fixture
    metrics_path: /metrics
    static_configs:
      - targets: ["fixture:18081"]
YAML
cat > "$work/rendered/prometheus/rules/alerting-synthetic.yml" <<'YAML'
groups:
  - name: ragkb-alerting-test
    rules:
      # 由 fixture 的 /control/set 切换 0/1：1 -> firing，0 -> 规则恢复，
      # Prometheus 自己在下一次 evaluation 发送带 EndsAt 的 resolved。
      - alert: RagkbAlertingDeliveryProbe
        expr: ragkb_alerting_fixture_probe > 0
        for: 0s
        labels:
          severity: test
        annotations:
          summary: Alerting delivery probe (synthetic)
YAML
chmod 0644 "$work/rendered/prometheus/rules/alerting-synthetic.yml"

# ---------- 4) 从实际 compose 文件取容器命令 ----------
printf 'networks:\n  internal: {}\n' > "$work/network.yml"
: > "$work/empty.env"
RAGKB_PROMETHEUS_CONFIG_DIR="$(host_path "$work/rendered/prometheus")" \
RAGKB_PROMETHEUS_SECRET_DIR="$(host_path "$work/rendered/secrets")" \
RAGKB_ALERTMANAGER_CONFIG_DIR="$(host_path "$work/rendered/alertmanager")" \
RAGKB_ALERTMANAGER_SECRET_DIR="$(host_path "$work/rendered/alertmanager/secrets")" \
  docker compose --env-file "$(host_path "$work/empty.env")" \
    -f "$(host_path "$repo/docker-compose.monitoring.yml")" \
    -f "$(host_path "$repo/docker-compose.alerting.yml")" \
    -f "$(host_path "$work/network.yml")" \
    config --format json > "$work/compose.json"
! grep -q "$webhook_url" "$work/compose.json" || { printf 'compose 配置泄露了 URL\n' >&2; exit 1; }
"$python_bin" -I -c '
import json,sys
services=json.load(open(sys.argv[1], encoding="utf-8"))["services"]
for name in ("prometheus","alertmanager"):
    with open(sys.argv[2] + "/" + name + ".args", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(services[name]["command"]))
' "$(host_path "$work/compose.json")" "$(host_path "$work")"
mapfile -t prom_args < "$work/prometheus.args"
mapfile -t am_args < "$work/alertmanager.args"
# Windows Python 的文本模式会带 CRLF；参数本身不能带回车。
for index in "${!prom_args[@]}"; do prom_args[$index]=${prom_args[$index]%$'\r'}; done
for index in "${!am_args[@]}"; do am_args[$index]=${am_args[$index]%$'\r'}; done
[[ " ${am_args[*]} " == *" --cluster.listen-address= "* ]] || {
  printf 'Alertmanager 命令应显式禁用集群监听\n' >&2; exit 1; }

# ---------- 5) 起自有网络、fixture、Alertmanager、Prometheus ----------
network_id=$(docker network create --label "$owner_label=$owner" "$network")

run_owned "$owner-fixture" --network "$network" --network-alias fixture \
  --publish 127.0.0.1::18080 --publish 127.0.0.1::18081 \
  -v "$(host_path "$repo/deploy/tests/alerting_fixture.py"):/tests/alerting_fixture.py:ro" \
  -v "$(host_path "$work/out"):/out:rw" \
  "$py_image" python -I /tests/alerting_fixture.py \
    --out /out/webhooks.jsonl --webhook-port 18080 --metrics-port 18081
fixture_id=$owned_id
port_of() { local p; p=$(docker port "$1" "$2"); p=${p##*:}; printf '%s' "${p//$'\r'/}"; }
fixture_webhook_port=$(port_of "$fixture_id" 18080/tcp)
fixture_metrics_port=$(port_of "$fixture_id" 18081/tcp)
for ((attempt=0; attempt<40; attempt++)); do
  curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:$fixture_metrics_port/ready" >/dev/null 2>&1 && break
  [[ $(docker inspect -f '{{.State.Running}}' "$fixture_id") == true ]] || break
  sleep 0.25
done
curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:$fixture_metrics_port/ready" >/dev/null

run_owned "$owner-alertmanager" --network "$network" --network-alias alertmanager \
  --user 65534:65534 --read-only --tmpfs /alertmanager:rw,uid=65534,gid=65534,mode=0755 \
  --publish 127.0.0.1::9093 \
  -v "$(host_path "$work/rendered/alertmanager"):/etc/alertmanager:ro" \
  -v "$(host_path "$work/rendered/alertmanager/secrets"):/etc/alertmanager-secrets:ro" \
  "$am_image" "${am_args[@]}"
am_id=$owned_id
am_port=$(port_of "$am_id" 9093/tcp)

run_owned "$owner-prometheus" --network "$network" --network-alias prometheus \
  --user 65534:65534 --read-only --tmpfs /prometheus:rw,uid=65534,gid=65534,mode=0755 \
  --publish 127.0.0.1::9090 \
  -v "$(host_path "$work/rendered/prometheus"):/etc/prometheus:ro" \
  -v "$(host_path "$work/rendered/secrets"):/etc/prometheus-secrets:ro" \
  "$prom_image" "${prom_args[@]}"
prom_id=$owned_id
prom_port=$(port_of "$prom_id" 9090/tcp)

wait_ready() { # 容器ID URL 标签
  local id=$1 url=$2 name=$3
  for ((attempt=0; attempt<80; attempt++)); do
    curl --noproxy '*' -fsS --max-time 2 "$url" >/dev/null 2>&1 && return 0
    [[ $(docker inspect -f '{{.State.Running}}' "$id") == true ]] || break
    sleep 0.25
  done
  printf '%s 未就绪\n' "$name" >&2; return 1
}
wait_ready "$am_id" "http://127.0.0.1:$am_port/-/ready" Alertmanager
wait_ready "$prom_id" "http://127.0.0.1:$prom_port/-/ready" Prometheus

# ---------- 6) firing -> resolved 全链路 ----------
records() {
  [[ -f "$work/out/webhooks.jsonl" ]] || return 0
  "$python_bin" -I -c '
import json,sys
for line in open(sys.argv[1], encoding="utf-8"):
    line=line.strip()
    if line:
        for probe in json.loads(line).get("probe_alerts", []):
            print(probe["status"])
' "$(host_path "$work/out/webhooks.jsonl")"
}
wait_status() { # status 秒数
  local want=$1 limit=$2
  for ((attempt=0; attempt<limit; attempt++)); do
    records | grep -qx "$want" && return 0
    sleep 0.5
  done
  printf '未在 %ss 内收到 %s 通知\n' "$limit" "$want" >&2
  return 1
}
# fixture 默认 probe=1，Prometheus 首次抓取后规则即触发。
wait_status firing 60
# 把合成指标降到 0：规则恢复，Prometheus 自己发带 EndsAt 的 resolved。
curl --noproxy '*' -fsS --max-time 3 \
  "http://127.0.0.1:$fixture_metrics_port/control/set?v=0" >/dev/null
wait_status resolved 60

"$python_bin" -I -c '
import json,sys
rows=[json.loads(l) for l in open(sys.argv[1], encoding="utf-8") if l.strip()]
events=[(r["seq"], p["status"], p.get("fingerprint")) for r in rows for p in r.get("probe_alerts", [])]
assert any(status=="firing" and isinstance(fp,str) and fp and any(
    later>seq and state=="resolved" and same_fp==fp for later,state,same_fp in events)
    for seq,status,fp in events), "同一探针没有先触发再恢复"
' "$(host_path "$work/out/webhooks.jsonl")"

# secret 不得出现在告警容器日志里（成功路径兜底）。
[[ "$(docker logs "$am_id" 2>&1)" != *"$webhook_url"* ]] || {
  printf 'Alertmanager 成功路径日志泄露了 URL\n' >&2; exit 1; }

# ---------- 7) webhook 失败路径：日志是否泄露 path/query ----------
# 另起一个同配置的 AM，指向不可达端口，强制投递失败，然后检查日志里是否出现
# path/query（这里用带明显标记的路径 token 探测）。
fail_dir="$work/fail-am"
mkdir -p "$fail_dir/secrets"
chmod 0755 "$fail_dir/secrets"
sed 's#^\(\s*\)url_file:.*#\1url_file: /etc/alertmanager-secrets/webhook_url#' \
  "$work/rendered/alertmanager/alertmanager.yml" > "$fail_dir/alertmanager.yml"
fail_token='leakprobe-path-9f3c'
# 127.0.0.1:1 在容器内必然连接被拒，路径与 query 只出现在请求目标里。
(umask 022; printf 'http://127.0.0.1:1/%s?token=%s\n' "$fail_token" "$fail_token" > "$fail_dir/secrets/webhook_url")
chmod 0444 "$fail_dir/secrets/webhook_url"
run_owned "$owner-alertmanager-fail" --network "$network" \
  --user 65534:65534 --read-only --tmpfs /alertmanager:rw,uid=65534,gid=65534,mode=0755 \
  --publish 127.0.0.1::9093 \
  -v "$(host_path "$fail_dir"):/etc/alertmanager:ro" \
  -v "$(host_path "$fail_dir/secrets"):/etc/alertmanager-secrets:ro" \
  "$am_image" "${am_args[@]}"
fail_am_id=$owned_id
fail_am_port=$(port_of "$fail_am_id" 9093/tcp)
wait_ready "$fail_am_id" "http://127.0.0.1:$fail_am_port/-/ready" Alertmanager-fail

now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
curl --noproxy '*' -fsS --max-time 3 -X POST \
  -H 'Content-Type: application/json' \
  -d "[{\"labels\":{\"alertname\":\"RagkbFailureProbe\",\"severity\":\"test\"},\"annotations\":{},\"startsAt\":\"$now\"}]" \
  "http://127.0.0.1:$fail_am_port/api/v2/alerts" >/dev/null
# 先证明投递确实失败，不能把“没有观察到投递”当成日志脱敏通过。
failed=0
for ((attempt=0; attempt<40; attempt++)); do
  if curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:$fail_am_port/metrics" | \
      "$python_bin" -I -c 'import sys; sys.exit(0 if any(line.startswith("alertmanager_notifications_failed_total{") and "integration=\"webhook\"" in line and float(line.rsplit(" ",1)[1])>0 for line in sys.stdin) else 1)'; then
    failed=1
    break
  fi
  sleep 0.5
done
[[ "$failed" == 1 ]] || { printf '未观察到 webhook 投递失败，不能验证失败日志\n' >&2; exit 1; }
fail_logs=$(docker logs "$fail_am_id" 2>&1)
[[ "$fail_logs" == *'Notify for alerts failed'* || "$fail_logs" == *'notify retry canceled'* ]] || {
  printf '失败指标已增加，但未观察到对应通知失败日志\n' >&2; exit 1; }
if [[ "$fail_logs" == *"$fail_token"* ]]; then
  printf 'Alertmanager 失败日志泄露了 webhook path/query token\n' >&2
  exit 1
fi

printf 'alerting regression: renderer guards, secret handling, firing+resolved delivery, failure-log redaction passed\n'
