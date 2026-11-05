#!/usr/bin/env bash
# 把 deploy/monitoring/alertmanager/*.in 渲染到运行目录，并准备 webhook URL 的容器副本。
#
# 与 render-prometheus.sh 同一套做法：宿主机显式渲染（Alertmanager 自身不展开
# 环境变量），产物落仓库外，compose 只读挂载。
#
# 用法：
#   RAGKB_ALERT_WEBHOOK_FILE=/srv/ragkb-secrets/alert_webhook_url \
#   OUT_DIR=/srv/ragkb-monitoring ./deploy/monitoring/render-alertmanager.sh
#   ... --check     # 只校验已渲染产物（不写、不联网、不向 webhook 发任何请求）
#
# webhook URL 只从仓库外的 RAGKB_ALERT_WEBHOOK_FILE 读取：
#   - 宿主机原件保持 0600（仅属主可读）；
#   - 这里只生成一份 0444 的容器副本，写到 $OUT_DIR/alertmanager/secrets/webhook_url，
#     不把 URL 写进 alertmanager.yml、compose 文件、命令行参数或 stdout；
#   - 校验失败时不回显 URL 内容，避免 secret 进日志/CI 输出。
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
repo=$(pwd)

check_only=0
if [[ "${1:-}" == "--check" ]]; then
  check_only=1
elif [[ $# -gt 0 ]]; then
  echo "用法: $0 [--check]" >&2; exit 2
fi

ENV_FILE=${ENV_FILE:-.env}
# 测试会显式传 ENV_FILE=/dev/null，避免读到真实 .env。
if [[ "$ENV_FILE" != /dev/null ]] && [[ -f "$ENV_FILE" ]]; then
  set -a; source <(grep -E '^[A-Z_]+=' "$ENV_FILE" | sed 's/[[:space:]]*$//'); set +a
fi
DATA_DIR=${DATA_DIR:-./data}
HOST_OUT=${OUT_DIR:-$DATA_DIR/monitoring}

die() { printf '%s\n' "$*" >&2; exit 1; }

# ---------- 取值校验（错误信息只提变量名/规则，不回显 URL） ----------
duration() { # 名称 值（Alertmanager 时长：数字+ms/s/m/h/d/w/y）
  [[ "$2" =~ ^[1-9][0-9]{0,5}(ms|s|m|h|d|w|y)$ ]] || {
    die "$1 必须是时长，例如 30s / 5m / 1h（当前: $2）";
  }
}
nonneg_int() { # 名称 值 上限
  [[ "$2" =~ ^0$|^[1-9][0-9]*$ ]] && (( $2 <= $3 )) || {
    die "$1 必须是 0-$3 的整数（当前: $2）";
  }
}

RAGKB_AM_RESOLVE_TIMEOUT=${RAGKB_AM_RESOLVE_TIMEOUT:-5m};   duration RAGKB_AM_RESOLVE_TIMEOUT "$RAGKB_AM_RESOLVE_TIMEOUT"
RAGKB_AM_GROUP_WAIT=${RAGKB_AM_GROUP_WAIT:-30s};            duration RAGKB_AM_GROUP_WAIT "$RAGKB_AM_GROUP_WAIT"
RAGKB_AM_GROUP_INTERVAL=${RAGKB_AM_GROUP_INTERVAL:-5m};     duration RAGKB_AM_GROUP_INTERVAL "$RAGKB_AM_GROUP_INTERVAL"
RAGKB_AM_REPEAT_INTERVAL=${RAGKB_AM_REPEAT_INTERVAL:-4h};   duration RAGKB_AM_REPEAT_INTERVAL "$RAGKB_AM_REPEAT_INTERVAL"
RAGKB_AM_WEBHOOK_TIMEOUT=${RAGKB_AM_WEBHOOK_TIMEOUT:-10s};  duration RAGKB_AM_WEBHOOK_TIMEOUT "$RAGKB_AM_WEBHOOK_TIMEOUT"
RAGKB_AM_MAX_ALERTS=${RAGKB_AM_MAX_ALERTS:-0};              nonneg_int RAGKB_AM_MAX_ALERTS "$RAGKB_AM_MAX_ALERTS" 10000

# repeat_interval 必须是 group_interval 的整数倍，否则 Alertmanager 会静默向上取整。
to_millis() { # 时长 -> 毫秒（仅本脚本白名单内的单位）；不取整、不掩盖精度
  local n unit
  [[ "$1" =~ ^([1-9][0-9]*)(ms|s|m|h|d|w|y)$ ]] || { printf '0'; return; }
  n=${BASH_REMATCH[1]}; unit=${BASH_REMATCH[2]}
  case "$unit" in
    ms) printf '%s' "$n" ;;
    s) printf '%s' "$(( n * 1000 ))" ;;
    m) printf '%s' "$(( n * 60000 ))" ;;
    h) printf '%s' "$(( n * 3600000 ))" ;;
    d) printf '%s' "$(( n * 86400000 ))" ;;
    w) printf '%s' "$(( n * 604800000 ))" ;;
    y) printf '%s' "$(( n * 31536000000 ))" ;;
    *) printf '0' ;;
  esac
}
gi_ms=$(to_millis "$RAGKB_AM_GROUP_INTERVAL")
ri_ms=$(to_millis "$RAGKB_AM_REPEAT_INTERVAL")
(( gi_ms > 0 && ri_ms > 0 )) || die '内部错误：时长换算失败'
(( ri_ms % gi_ms == 0 )) || die "RAGKB_AM_REPEAT_INTERVAL 必须是 RAGKB_AM_GROUP_INTERVAL 的整数倍"

export RAGKB_AM_RESOLVE_TIMEOUT RAGKB_AM_GROUP_WAIT RAGKB_AM_GROUP_INTERVAL
export RAGKB_AM_REPEAT_INTERVAL RAGKB_AM_WEBHOOK_TIMEOUT RAGKB_AM_MAX_ALERTS

# 显式白名单：只有这些变量允许出现在模板里。不能用 “name in ENVIRON” 判断，
# 宿主机导出的任意其它变量（例如 RAGKB_AM_* 之外的同名变量）也会被当成合法。
render_allowlist='RAGKB_AM_RESOLVE_TIMEOUT RAGKB_AM_GROUP_WAIT RAGKB_AM_GROUP_INTERVAL RAGKB_AM_REPEAT_INTERVAL RAGKB_AM_WEBHOOK_TIMEOUT RAGKB_AM_MAX_ALERTS'

render() { # 源文件 目标文件
  local src=$1 dst=$2
  # 只替换 ${NAME} / ${NAME:-default}；NAME 必须在显式白名单内，
  # 未知 NAME 直接报错而不是渲染成空串。
  mkdir -p "$(dirname "$dst")"
  chmod 0755 "$(dirname "$dst")"
  awk -v allowed="$render_allowlist" '
    BEGIN { allow = " " allowed " " }
    {
      line=$0
      while (match(line, /\$\{[A-Z0-9_]+(:-[^}]*)?\}/)) {
        token=substr(line, RSTART, RLENGTH)
        name=token
        sub(/^\$\{/, "", name); sub(/(:-[^}]*)?\}$/, "", name)
        if (index(allow, " " name " ") == 0) { printf "未知占位符: %s\n", token > "/dev/stderr"; exit 3 }
        if (!(name in ENVIRON)) { printf "未导出变量: %s\n", token > "/dev/stderr"; exit 3 }
        val=ENVIRON[name]
        line=substr(line, 1, RSTART-1) val substr(line, RSTART+RLENGTH)
      }
      print line
    }' "$src" > "$dst.tmp"
  chmod 0644 "$dst.tmp"
  mv "$dst.tmp" "$dst"
}

# ---------- webhook URL：只校验，从不回显值 ----------
# 规则：
#   - 单行、去行尾空白后非空；
#   - 必须以 http:// 或 https:// 开头，主机非空，端口合法（若写了）；
#   - 不允许 userinfo（scheme://user:pass@host），避免把凭据塞进 URL；
#   - 不允许空白、控制字符、换行或反斜杠（防止拼进 shell/文件名）。
prepare_webhook_copy() { # 源文件 目标目录
  local src=$1 dst_dir=$2 dst value
  [[ -f "$src" ]] || die "RAGKB_ALERT_WEBHOOK_FILE 不存在: $src"
  case "$(realpath "$src" 2>/dev/null || printf '%s' "$src")" in
    "$repo"|"$repo"/*) die 'webhook URL 文件不能在仓库内' ;;
  esac
  # 源文件不允许组/其他用户可读。Git Bash 无法表达 POSIX 位（chmod 后 stat 仍是
  # 644，NTFS 由 ACL 决定），与 render-prometheus.sh 一致，在 MINGW 下跳过该项、
  # 由 Linux 生产机/CI 强制执行。
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*) : ;;
    *)
      local perms
      perms=$(stat -c '%a' "$src" 2>/dev/null || echo '')
      [[ -n "$perms" ]] || die "无法读取 webhook URL 文件权限: $src"
      (( (8#$perms & 077) == 0 )) || die "webhook URL 文件权限过宽（$perms），请 chmod 600 $src"
      ;;
  esac
  # 逐字节读取：command substitution 会静默删除 NUL、把内部换行变成尾随换行，
  # 必须先拒绝，再解析。规则：
  #   - 只允许一个可选的行尾 \n（\r\n 也接受），其它位置出现 CR/LF 一律拒绝；
  #   - 不允许 NUL 与其它控制字符（0x00-0x1F、0x7F）；
  #   - 不允许行首/行尾空白。
  local bytes len
  bytes=$(wc -c < "$src")
  (( bytes > 0 )) || die 'webhook URL 为空'
  (( bytes <= 4097 )) || die 'webhook URL 过长（>4096 字节）'
  # 尾部允许一个 \n 或 \r\n；先算出“去掉行尾”后的长度，再对该区间逐字节检查。
  local -a octets=() row=()
  while read -r -a row; do
    ((${#row[@]})) && octets+=("${row[@]}")
  done < <(od -An -v -tu1 "$src")
  len=${#octets[@]}
  (( len == bytes )) || die '无法读取 webhook URL 字节'
  if (( len >= 2 )) && [[ ${octets[len-1]} == 10 && ${octets[len-2]} == 13 ]]; then
    len=$(( len - 2 ))
  elif (( len >= 1 )) && [[ ${octets[len-1]} == 10 ]]; then
    len=$(( len - 1 ))
  fi
  (( len > 0 )) || die 'webhook URL 为空'
  local octet
  for ((i=0; i<len; i++)); do
    octet=${octets[i]}
    if (( octet == 0 )); then die 'webhook URL 不能包含 NUL'; fi
    if (( octet == 10 || octet == 13 )); then die 'webhook URL 必须是单行'; fi
    if (( (octet < 32 && octet != 9) || octet == 127 )); then die 'webhook URL 不能包含控制字符'; fi
  done
  # 从已校验的字节重建字符串（此时不含 NUL/CR/LF，command substitution 安全）。
  value=$(head -c "$len" "$src")
  [[ -n "$value" ]] || die 'webhook URL 为空'
  [[ "$value" != [[:space:]]* && "$value" != *[[:space:]] ]] || die 'webhook URL 不能有首尾空白'
  [[ "$value" != *[[:space:]]* ]] || die 'webhook URL 不能包含空白字符'
  [[ "$value" != *\\* ]] || die 'webhook URL 不能包含反斜杠'
  [[ "$value" =~ ^https?://[^/?#]+[^[:space:]]*$ ]] || die 'webhook URL 必须是 http:// 或 https:// 开头的完整地址'
  local authority=${value#*://}; authority=${authority%%[/?#]*}
  [[ "$authority" != *@* ]] || die 'webhook URL 不能包含 userinfo（user:pass@）'
  # 主机可带端口；写了端口就必须是 1-65535。IPv6 必须写成 [addr]:port。
  # 多冒号（非 IPv6 方括号形式）与空端口（host:）一律拒绝。
  local host port=''
  if [[ "$authority" == \[* ]]; then
    [[ "$authority" =~ ^(\[[0-9A-Fa-f:.]+\])(:([0-9]{1,5}))?$ ]] \
      || die 'webhook URL 主机名/端口不合法'
    host=${BASH_REMATCH[1]}; port=${BASH_REMATCH[3]}
  else
    [[ "$authority" =~ ^([A-Za-z0-9]([A-Za-z0-9_.-]{0,252}))(:([0-9]{1,5}))?$ ]] \
      || die 'webhook URL 主机名/端口不合法'
    host=${BASH_REMATCH[1]}; port=${BASH_REMATCH[4]}
    [[ "$authority" != *:* || -n "$port" ]] || die 'webhook URL 端口不能为空'
  fi
  [[ -n "$host" ]] || die 'webhook URL 主机名为空'
  if [[ -n "$port" ]]; then
    (( 10#$port >= 1 && 10#$port <= 65535 )) || die 'webhook URL 端口必须是 1-65535'
  fi

  # 目录必须对 nobody(65534) 可遍历；文件只读。与 Prometheus 令牌副本同一约定。
  mkdir -p "$dst_dir"
  chmod 0755 "$dst_dir"
  dst="$dst_dir/webhook_url"
  # 重复渲染时旧副本是 0444，直接覆盖会 Permission denied；先删再建。
  rm -f "$dst"
  ( umask 022; printf '%s\n' "$value" > "$dst" )
  chmod 0444 "$dst"
  [[ -r "$dst" ]] || die "生成的 webhook 副本不可读: $dst"
}

host_path() { cygpath -w "$1" 2>/dev/null || printf '%s' "$1"; }

# --check：只在已渲染产物存在时做语法校验；不写文件、不联网、不投递。
if [[ $check_only -eq 1 ]]; then
  [[ -f "$HOST_OUT/alertmanager/alertmanager.yml" ]] || die "尚未渲染: $HOST_OUT/alertmanager/alertmanager.yml"
  if command -v docker >/dev/null 2>&1 && docker image inspect prom/alertmanager:v0.28.1 >/dev/null 2>&1; then
    # amtool 会加载 url_file；用一次性占位文件做语法校验，真实 URL 不参与。
    stub=$(mktemp -d "${TMPDIR:-/tmp}/ragkb-am-check.XXXXXX")
    chmod 0755 "$stub"
    printf 'http://127.0.0.1:19093/check-only\n' > "$stub/webhook_url"
    chmod 0444 "$stub/webhook_url"
    trap 'rm -rf "$stub"' EXIT
    # MSYS 路径改写会让容器内路径变成 Windows 路径，必须禁用。
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --network none \
      --user 65534:65534 --entrypoint amtool \
      -v "$(host_path "$HOST_OUT/alertmanager"):/etc/alertmanager:ro" \
      -v "$(host_path "$stub"):/etc/alertmanager-secrets:ro" \
      prom/alertmanager:v0.28.1 check-config /etc/alertmanager/alertmanager.yml >/dev/null \
      || die 'amtool check-config 校验失败'
    echo "已渲染产物存在且通过 amtool: $HOST_OUT/alertmanager"
  else
    echo "已渲染产物存在: $HOST_OUT/alertmanager（未找到 prom/alertmanager:v0.28.1，跳过 amtool）"
  fi
  exit 0
fi

# ---------- 渲染 ----------
webhook_src=${RAGKB_ALERT_WEBHOOK_FILE:-}
[[ -n "$webhook_src" ]] || die '需要 RAGKB_ALERT_WEBHOOK_FILE 指向仓库外的 webhook URL 文件（0600）'

mkdir -p "$HOST_OUT/alertmanager/secrets"
chmod 0755 "$HOST_OUT" "$HOST_OUT/alertmanager"
render deploy/monitoring/alertmanager/alertmanager.yml.in "$HOST_OUT/alertmanager/alertmanager.yml"
prepare_webhook_copy "$webhook_src" "$HOST_OUT/alertmanager/secrets"
chmod 0755 "$HOST_OUT/alertmanager" "$HOST_OUT/alertmanager/secrets"
chmod 0644 "$HOST_OUT/alertmanager/alertmanager.yml"

if command -v docker >/dev/null 2>&1 && docker image inspect prom/alertmanager:v0.28.1 >/dev/null 2>&1; then
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --network none \
    --user 65534:65534 --entrypoint amtool \
    -v "$(host_path "$HOST_OUT/alertmanager"):/etc/alertmanager:ro" \
    -v "$(host_path "$HOST_OUT/alertmanager/secrets"):/etc/alertmanager-secrets:ro" \
    prom/alertmanager:v0.28.1 check-config /etc/alertmanager/alertmanager.yml >/dev/null \
    || die 'amtool check-config 校验失败'
  # 以 nobody 身份真实读取 webhook 副本，避免“可读”只是 root 视角。
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm --network none \
    --user 65534:65534 --entrypoint sh \
    -v "$(host_path "$HOST_OUT/alertmanager/secrets"):/etc/alertmanager-secrets:ro" \
    prom/alertmanager:v0.28.1 -c 'test -r /etc/alertmanager-secrets/webhook_url' \
    || die "容器内 nobody 无法读取 $HOST_OUT/alertmanager/secrets/webhook_url"
  echo "渲染完成并通过 amtool: $HOST_OUT/alertmanager"
else
  echo "渲染完成: $HOST_OUT/alertmanager（未找到 prom/alertmanager:v0.28.1，跳过 amtool）"
fi
