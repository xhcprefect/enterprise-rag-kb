#!/usr/bin/env bash
# 把 deploy/monitoring/prometheus/*.in 渲染到运行目录，并做静态校验。
#
# 为什么不用容器内 envsubst：prom/prometheus 镜像里没有 envsubst，Prometheus
# 自身也不会展开环境变量。这里在**宿主机**显式渲染，产物落在仓库外（默认
# $DATA_DIR/monitoring），由 docker-compose.monitoring.yml 只读挂载。
#
# 用法：
#   ./deploy/monitoring/render-prometheus.sh                    # 渲染到默认目录
#   ./deploy/monitoring/render-prometheus.sh --check            # 只校验已渲染产物（不写）
#   OUT_DIR=/srv/ragkb-monitoring ./deploy/monitoring/render-prometheus.sh
#
# 变量都有默认值，缺失不会静默变成空串；值经过白名单校验，避免把任意文本拼进 YAML。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
repo=$(pwd)

check_only=0
[[ "${1:-}" == "--check" ]] || [[ $# -eq 0 ]] || { echo "用法: $0 [--check]" >&2; exit 2; }
[[ "${1:-}" == "--check" ]] && check_only=1

ENV_FILE=${ENV_FILE:-.env}
if [[ -f "$ENV_FILE" ]]; then
  set -a; source <(grep -E '^[A-Z_]+=' "$ENV_FILE" | sed 's/[[:space:]]*$//'); set +a
fi
DATA_DIR=${DATA_DIR:-./data}
HOST_OUT=${OUT_DIR:-$DATA_DIR/monitoring}

# 令牌源文件：宿主机上保持 0600；这里只读取一次并生成容器可读副本。
# 未设置时明确跳过监控令牌准备（只渲染配置），避免把“没配令牌”误当成可用监控。
TOKEN_SRC=${RAGKB_METRICS_TOKEN_FILE:-}
token_ready=0
token_copy_made=0
prepare_token_copy() {
  local src=$1 dst_dir=$2 dst perms
  [[ -f "$src" ]] || { echo "RAGKB_METRICS_TOKEN_FILE 不存在: $src" >&2; exit 1; }
  # 必须在仓库外：镜像与仓库都不应包含令牌。
  case "$(realpath "$src")" in
    "$repo"|"$repo"/*) echo 'METRICS_TOKEN 文件不能在仓库内' >&2; exit 1 ;;
  esac
  # 源文件不允许组/其他用户可读。
  perms=$(stat -c '%a' "$src" 2>/dev/null || echo '')
  [[ -n "$perms" ]] || { echo "无法读取令牌文件权限: $src" >&2; exit 1; }
  (( (8#$perms & 077) == 0 )) || {
    echo "令牌文件权限过宽（$perms），请 chmod 600 $src" >&2; exit 1;
  }
  [[ -s "$src" ]] || { echo "令牌文件为空: $src" >&2; exit 1; }
  # 去掉行尾换行后非空，避免把多行/空令牌塞进 Authorization 头。
  local value
  value=$(tr -d '\r\n' < "$src")
  [[ -n "$value" ]] || { echo "令牌文件内容为空: $src" >&2; exit 1; }
  [[ "$value" =~ ^[A-Za-z0-9._~+/=-]+$ ]] || {
    echo '令牌只能包含 URL 安全的可见字符（不要换行/空格）' >&2; exit 1;
  }
  # 目录必须对 nobody(65534) 可遍历：调用方可能带 umask 077，mkdir 出来的
  # 0700 目录会让容器内即使文件 0444 也读不到。显式放宽到 0755（只读）。
  mkdir -p "$dst_dir"
  chmod 0755 "$dst_dir"
  dst="$dst_dir/metrics_token"
  # 直接写目标（umask 022 -> 0644），再降到 0444，避免权限窗口。
  ( umask 022; printf '%s\n' "$value" > "$dst" )
  chmod 0444 "$dst"
  [[ -r "$dst" ]] || { echo "生成的令牌副本不可读: $dst" >&2; exit 1; }
  token_copy_made=1
  token_ready=1
}

# ---------- 校验并导出渲染变量 ----------
positive_int() { # 名称 值 上限
  local name=$1 value=$2 max=$3
  [[ "$value" =~ ^[1-9][0-9]*$ ]] && (( value <= max )) || {
    echo "$name 必须是 1-$max 的整数（当前: $value）" >&2; exit 1;
  }
}
duration() { # 名称 值（Prometheus 时长：数字+ms/s/m/h/d）
  [[ "$2" =~ ^[1-9][0-9]{0,5}(ms|s|m|h|d)$ ]] || {
    echo "$1 必须是 Prometheus 时长，例如 30s / 5m / 1h（当前: $2）" >&2; exit 1;
  }
}
ratio() { # 名称 0-1 之间的小数
  [[ "$2" =~ ^0(\.[0-9]+)?$|^1(\.0+)?$ ]] || {
    echo "$1 必须是 0-1 之间的小数（当前: $2）" >&2; exit 1;
  }
}
label() { # 名称 值（Prometheus label 值：字母数字与 _ . -）
  [[ "$2" =~ ^[A-Za-z0-9_.-]{1,63}$ ]] || {
    echo "$1 只能是字母数字与 _ . -（当前: $2）" >&2; exit 1;
  }
}

RAGKB_SCRAPE_INTERVAL=${RAGKB_SCRAPE_INTERVAL:-30s};   duration RAGKB_SCRAPE_INTERVAL "$RAGKB_SCRAPE_INTERVAL"
RAGKB_SCRAPE_TIMEOUT=${RAGKB_SCRAPE_TIMEOUT:-10s};     duration RAGKB_SCRAPE_TIMEOUT "$RAGKB_SCRAPE_TIMEOUT"
RAGKB_INSTANCE_LABEL=${RAGKB_INSTANCE_LABEL:-ragkb-single-api}; label RAGKB_INSTANCE_LABEL "$RAGKB_INSTANCE_LABEL"

RAGKB_ALERT_FIRST_TOKEN_QUANTILE=${RAGKB_ALERT_FIRST_TOKEN_QUANTILE:-0.95}; ratio RAGKB_ALERT_FIRST_TOKEN_QUANTILE "$RAGKB_ALERT_FIRST_TOKEN_QUANTILE"
RAGKB_ALERT_HTTP_QUANTILE=${RAGKB_ALERT_HTTP_QUANTILE:-0.95};               ratio RAGKB_ALERT_HTTP_QUANTILE "$RAGKB_ALERT_HTTP_QUANTILE"
RAGKB_ALERT_FAILURE_RATIO=${RAGKB_ALERT_FAILURE_RATIO:-0.2};                ratio RAGKB_ALERT_FAILURE_RATIO "$RAGKB_ALERT_FAILURE_RATIO"
RAGKB_ALERT_HTTP_5XX_RATIO=${RAGKB_ALERT_HTTP_5XX_RATIO:-0.05};             ratio RAGKB_ALERT_HTTP_5XX_RATIO "$RAGKB_ALERT_HTTP_5XX_RATIO"

RAGKB_ALERT_FIRST_TOKEN_SECONDS=${RAGKB_ALERT_FIRST_TOKEN_SECONDS:-20};  positive_int RAGKB_ALERT_FIRST_TOKEN_SECONDS "$RAGKB_ALERT_FIRST_TOKEN_SECONDS" 3600
RAGKB_ALERT_STAGE_SECONDS=${RAGKB_ALERT_STAGE_SECONDS:-30};              positive_int RAGKB_ALERT_STAGE_SECONDS "$RAGKB_ALERT_STAGE_SECONDS" 3600
RAGKB_ALERT_HTTP_SECONDS=${RAGKB_ALERT_HTTP_SECONDS:-5};                 positive_int RAGKB_ALERT_HTTP_SECONDS "$RAGKB_ALERT_HTTP_SECONDS" 600
RAGKB_ALERT_IN_FLIGHT_MAX=${RAGKB_ALERT_IN_FLIGHT_MAX:-20};              positive_int RAGKB_ALERT_IN_FLIGHT_MAX "$RAGKB_ALERT_IN_FLIGHT_MAX" 100000
RAGKB_ALERT_HTTP_IN_FLIGHT_MAX=${RAGKB_ALERT_HTTP_IN_FLIGHT_MAX:-100};   positive_int RAGKB_ALERT_HTTP_IN_FLIGHT_MAX "$RAGKB_ALERT_HTTP_IN_FLIGHT_MAX" 100000
RAGKB_ALERT_FAILURE_MIN_COUNT=${RAGKB_ALERT_FAILURE_MIN_COUNT:-5};       positive_int RAGKB_ALERT_FAILURE_MIN_COUNT "$RAGKB_ALERT_FAILURE_MIN_COUNT" 100000
RAGKB_ALERT_HTTP_5XX_MIN_COUNT=${RAGKB_ALERT_HTTP_5XX_MIN_COUNT:-5};     positive_int RAGKB_ALERT_HTTP_5XX_MIN_COUNT "$RAGKB_ALERT_HTTP_5XX_MIN_COUNT" 100000
RAGKB_ALERT_QUEUE_BACKLOG=${RAGKB_ALERT_QUEUE_BACKLOG:-50};              positive_int RAGKB_ALERT_QUEUE_BACKLOG "$RAGKB_ALERT_QUEUE_BACKLOG" 1000000
RAGKB_ALERT_PENDING_AGE_SECONDS=${RAGKB_ALERT_PENDING_AGE_SECONDS:-1800}; positive_int RAGKB_ALERT_PENDING_AGE_SECONDS "$RAGKB_ALERT_PENDING_AGE_SECONDS" 604800
RAGKB_ALERT_POOL_SATURATION=${RAGKB_ALERT_POOL_SATURATION:-0.9};          ratio RAGKB_ALERT_POOL_SATURATION "$RAGKB_ALERT_POOL_SATURATION"

export RAGKB_SCRAPE_INTERVAL RAGKB_SCRAPE_TIMEOUT RAGKB_INSTANCE_LABEL
export RAGKB_ALERT_FIRST_TOKEN_QUANTILE RAGKB_ALERT_HTTP_QUANTILE
export RAGKB_ALERT_FAILURE_RATIO RAGKB_ALERT_HTTP_5XX_RATIO
export RAGKB_ALERT_FIRST_TOKEN_SECONDS RAGKB_ALERT_STAGE_SECONDS RAGKB_ALERT_HTTP_SECONDS
export RAGKB_ALERT_IN_FLIGHT_MAX RAGKB_ALERT_HTTP_IN_FLIGHT_MAX
export RAGKB_ALERT_FAILURE_MIN_COUNT RAGKB_ALERT_HTTP_5XX_MIN_COUNT
export RAGKB_ALERT_QUEUE_BACKLOG RAGKB_ALERT_PENDING_AGE_SECONDS RAGKB_ALERT_POOL_SATURATION

render() { # 源文件 目标文件
  local src=$1 dst=$2
  # 只替换 ${NAME} / ${NAME:-default} 形式的占位符，NAME 必须是上面显式 export
  # 的白名单变量；未知 NAME 直接报错而不是渲染成空串。PromQL 里的 {{ $value }}
  # 不是这个形式，不会被误伤。
  mkdir -p "$(dirname "$dst")"
  # 调用方可能带 umask 077：目录/文件默认 0700/0600，容器内 nobody(65534)
  # 读不到。渲染时就显式放宽（目录 0755 可遍历、文件 0644 可读），
  # 不依赖后续批量 chmod，避免中途失败留下不可读产物。
  chmod 0755 "$(dirname "$dst")"
  awk '
    {
      line=$0
      while (match(line, /\$\{[A-Z0-9_]+(:-[^}]*)?\}/)) {
        token=substr(line, RSTART, RLENGTH)
        name=token
        sub(/^\$\{/, "", name); sub(/(:-[^}]*)?\}$/, "", name)
        if (!(name in ENVIRON)) { printf "未知占位符: %s\n", token > "/dev/stderr"; exit 3 }
        val=ENVIRON[name]
        line=substr(line, 1, RSTART-1) val substr(line, RSTART+RLENGTH)
      }
      print line
    }' "$src" > "$dst.tmp"
  chmod 0644 "$dst.tmp"
  mv "$dst.tmp" "$dst"
}

if [[ $check_only -eq 0 ]]; then
  mkdir -p "$HOST_OUT/prometheus/rules" "$HOST_OUT/secrets"
  render deploy/monitoring/prometheus/prometheus.yml.in "$HOST_OUT/prometheus/prometheus.yml"
  for f in deploy/monitoring/prometheus/rules/*.yml.in; do
    base=$(basename "$f" .in)
    render "$f" "$HOST_OUT/prometheus/rules/$base"
  done
  if [[ -n "$TOKEN_SRC" ]]; then
    prepare_token_copy "$TOKEN_SRC" "$HOST_OUT/secrets"
  else
    echo "警告：未设置 RAGKB_METRICS_TOKEN_FILE，未生成容器可读令牌副本；" >&2
    echo "      此时启动监控覆盖会因 RAGKB_PROMETHEUS_SECRET_DIR 缺少令牌而采集失败。" >&2
  fi
  # 渲染产物要对容器内 nobody(65534) 可遍历/可读：调用方可能带 umask 077，
  # mkdir 默认 0700 会让 promtool/Prometheus 直接读不到配置。
  chmod 0755 "$HOST_OUT" "$HOST_OUT/prometheus" "$HOST_OUT/prometheus/rules"
  find "$HOST_OUT" -type f -name '*.yml' -exec chmod 0644 {} +
  # 渲染后在容器里做一次真实校验（需要 promtool；镜像缺失时明确跳过并提示）。
  if command -v docker >/dev/null 2>&1; then
    if docker image inspect prom/prometheus:v3.1.0 >/dev/null 2>&1; then
      # 宿主可能是 Git Bash：/tmp/... 这类路径要转成 Windows 形式，且禁止 MSYS
      # 把容器内路径（/m/...）改写成本机路径，否则 promtool 会找不到文件。
      host_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }
      promtool() {
        MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --network none \
          --user 65534:65534 \
          --entrypoint promtool \
          -v "$(host_path "$1"):/m:ro" \
          -v "$(host_path "$stub_dir"):/etc/prometheus-secrets:ro" \
          prom/prometheus:v3.1.0 "${@:2}"
      }
      # promtool 会检查 credentials_file 是否存在；用一次性占位**目录**做语法校验，
      # 真实令牌仍只在运行时提供，不进仓库也不进校验命令。必须挂目录而不是单个
      # 文件：credentials_file 指向 /etc/prometheus-secrets/metrics_token。
      stub_dir=$(mktemp -d "${TMPDIR:-/tmp}/ragkb-token-stub.XXXXXX")
      chmod 0755 "$stub_dir"
      printf 'promtool-syntax-check-only\n' > "$stub_dir/metrics_token"
      chmod 0644 "$stub_dir/metrics_token"
      trap 'rm -rf "$stub_dir"' EXIT
      promtool "$HOST_OUT" check config /m/prometheus/prometheus.yml >/dev/null \
        || { echo 'promtool 校验失败' >&2; exit 1; }
      for f in "$HOST_OUT"/prometheus/rules/*.yml; do
        promtool "$(dirname "$f")" check rules "/m/$(basename "$f")" \
          || { echo "规则校验失败: $f" >&2; exit 1; }
      done
      # 以 nobody 身份真实读取令牌副本（生产镜像同样是 65534），避免“可读”
      # 只是 root 视角。目录 0755 + 文件 0444 才应通过。
      if [[ -n "$TOKEN_SRC" ]]; then
        MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --network none \
          --user 65534:65534 \
          -v "$(host_path "$HOST_OUT/secrets"):/etc/prometheus-secrets:ro" \
          --entrypoint sh prom/prometheus:v3.1.0 \
          -c 'test -r /etc/prometheus-secrets/metrics_token' \
          || { echo "容器内 nobody 无法读取 $HOST_OUT/secrets/metrics_token" >&2; exit 1; }
      fi
      echo "渲染完成并通过 promtool: $HOST_OUT"
    else
      echo "渲染完成: $HOST_OUT（未找到 prom/prometheus:v3.1.0，跳过 promtool）"
    fi
  fi
else
  [[ -f "$HOST_OUT/prometheus/prometheus.yml" ]] || { echo "尚未渲染: $HOST_OUT" >&2; exit 1; }
  echo "已渲染产物存在: $HOST_OUT"
fi
