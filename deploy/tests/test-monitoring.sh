#!/usr/bin/env bash
# 有限、隔离的监控回归：严格 umask、nobody 读取、真实 promtool/启动与 lifecycle。
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
image=prom/prometheus:v3.1.0
command -v docker >/dev/null
command -v curl >/dev/null
python_bin=${PYTHON:-python3}
command -v "$python_bin" >/dev/null
docker image inspect "$image" >/dev/null 2>&1 || { printf '缺少测试镜像 %s\n' "$image" >&2; exit 1; }
work=$(mktemp -d "${TMPDIR:-/tmp}/ragkb-monitoring-test.XXXXXX")
owner=$(basename "$work")
container_id=''
host_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
cleanup() {
  local code=$? actual
  if [[ -n "$container_id" ]]; then
    actual=$(docker inspect -f '{{index .Config.Labels "io.ragkb.monitoring-test.owner"}}' "$container_id" 2>/dev/null || true)
    if [[ "$actual" == "$owner" ]]; then
      if (( code != 0 )); then docker logs "$container_id" >&2 || true; fi
      docker rm -f "$container_id" >/dev/null || code=1
    else
      printf '监控测试容器归属无法核对，保留 %s\n' "$container_id" >&2
      code=1
    fi
  fi
  if (( code == 0 )); then rm -rf "$work"; else printf '监控回归现场保留在 %s\n' "$work" >&2; fi
  exit "$code"
}
trap cleanup EXIT
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
chmod 0755 "$work"
(umask 077; printf 'monitoring-regression-only\n' > "$work/source-token")
(
  umask 077
  ENV_FILE=/dev/null OUT_DIR="$work/rendered" RAGKB_METRICS_TOKEN_FILE="$work/source-token" \
    bash "$repo/deploy/monitoring/render-prometheus.sh"
)
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) printf 'skip: NTFS/Git Bash 不提供真实的源文件 0600 语义；Linux CI 会断言该权限\n' ;;
  *)
    [[ $(stat -c '%a' "$work/source-token") == 600 ]]
    chmod 0644 "$work/source-token"
    if ENV_FILE=/dev/null OUT_DIR="$work/bad-permissions" RAGKB_METRICS_TOKEN_FILE="$work/source-token" \
        bash "$repo/deploy/monitoring/render-prometheus.sh" >/dev/null 2>&1; then
      printf 'Linux 上 0644 令牌源文件应被拒绝\n' >&2; exit 1
    fi
    chmod 0600 "$work/source-token"
    ;;

esac
# 旧副本为 0444，重复渲染应能更新而非 Permission denied。
ENV_FILE=/dev/null OUT_DIR="$work/rendered" RAGKB_METRICS_TOKEN_FILE="$work/source-token" \
  bash "$repo/deploy/monitoring/render-prometheus.sh" >/dev/null
[[ $(stat -c '%a' "$work/rendered/secrets/metrics_token") == 444 ]]
for directory in "$work/rendered" "$work/rendered/prometheus" "$work/rendered/prometheus/rules" "$work/rendered/secrets"; do
  [[ $(stat -c '%a' "$directory") == 755 ]]
done
mounts=(-v "$(host_path "$work/rendered/prometheus"):/etc/prometheus:ro" -v "$(host_path "$work/rendered/secrets"):/etc/prometheus-secrets:ro")
docker run --rm --network none --user 65534:65534 "${mounts[@]}" --entrypoint sh "$image" \
  -c 'test -r /etc/prometheus/prometheus.yml && test -r /etc/prometheus-secrets/metrics_token'
docker run --rm --network none --user 65534:65534 "${mounts[@]}" --entrypoint promtool "$image" \
  check config /etc/prometheus/prometheus.yml
for rule in "$work/rendered/prometheus/rules/"*.yml; do
  docker run --rm --network none --user 65534:65534 "${mounts[@]}" --entrypoint promtool "$image" \
    check rules "/etc/prometheus/rules/$(basename "$rule")"
done
# 真实执行规则单元测试，规则文件数变化时不需要改硬编码计数。
mkdir -p "$work/rendered/prometheus/tests"
chmod 0755 "$work/rendered/prometheus/tests"
for rule_test in "$repo"/deploy/monitoring/prometheus/tests/*.yml; do
  cp "$rule_test" "$work/rendered/prometheus/tests/"
  chmod 0644 "$work/rendered/prometheus/tests/$(basename "$rule_test")"
  docker run --rm --network none --user 65534:65534 "${mounts[@]}" --entrypoint promtool "$image" \
    test rules "/etc/prometheus/tests/$(basename "$rule_test")"
done
# 从实际监控 Compose 读取参数，避免测试命令漏掉生产中新增的无效开关。
printf 'networks:\n  internal: {}\n' > "$work/network.yml"
: > "$work/empty.env"
RAGKB_PROMETHEUS_CONFIG_DIR="$(host_path "$work/rendered/prometheus")" \
RAGKB_PROMETHEUS_SECRET_DIR="$(host_path "$work/rendered/secrets")" \
  docker compose --env-file "$(host_path "$work/empty.env")" -f "$(host_path "$repo/docker-compose.monitoring.yml")" -f "$(host_path "$work/network.yml")" \
  config --format json > "$work/compose.json"
"$python_bin" -I -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1], encoding="utf-8"))["services"]["prometheus"]["command"]))' \
  "$(host_path "$work/compose.json")" > "$work/command.txt"
mapfile -t prom_args < "$work/command.txt"
# Windows Python 输出 CRLF，参数本身不带回车。
for index in "${!prom_args[@]}"; do prom_args[$index]=${prom_args[$index]%$'\r'}; done
# create 成功即登记精确 ID，start 失败也由 EXIT trap 收回本轮容器。
container_id=$(docker create --user 65534:65534 --read-only --add-host backend:127.0.0.1 \
  --label "io.ragkb.monitoring-test.owner=$owner" --tmpfs /prometheus:rw,uid=65534,gid=65534,mode=0755 \
  --publish 127.0.0.1::9090 "${mounts[@]}" "$image" "${prom_args[@]}")
docker start "$container_id" >/dev/null
published=$(docker port "$container_id" 9090/tcp)
port=${published##*:}
port=${port//$'\r'/}
ready=0
for ((attempt=0; attempt<80; attempt++)); do
  if curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:$port/-/ready" >/dev/null 2>&1; then ready=1; break; fi
  [[ $(docker inspect -f '{{.State.Running}}' "$container_id") == true ]] || break
  sleep 0.25
done
[[ "$ready" == 1 ]] || { printf 'Prometheus 未就绪\n' >&2; exit 1; }
status=$(curl --noproxy '*' -sS --max-time 3 -o "$(host_path "$work/reload-body")" -w '%{http_code}' -X POST "http://127.0.0.1:$port/-/reload")
[[ "$status" == 403 ]] || { printf 'lifecycle 应关闭，实际 HTTP %s\n' "$status" >&2; exit 1; }
docker stop --time 10 "$container_id" >/dev/null
[[ $(docker inspect -f '{{.State.ExitCode}}' "$container_id") == 0 ]]
printf 'monitoring regression: nobody permissions, rules, startup and lifecycle passed\n'
