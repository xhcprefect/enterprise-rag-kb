#!/usr/bin/env bash
# 首次部署前的准备：建数据目录、检查配置是否还是模板值、确认密钥没漏。
# 只做检查与建目录，不改任何已存在的文件。
#
# 旧单环境：不设置路径变量时仍检查根 .env + backend/.env。
# 同机双环境：由 deploy/env.sh 显式导出 ENV_FILE / BACKEND_ENV_FILE，
# 本脚本只读写这两个文件及其对应模板，不触碰 legacy 配置，也不回显任何密钥值。
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."

RED=$'\033[31m'; YELLOW=$'\033[33m'; GREEN=$'\033[32m'; NC=$'\033[0m'
fail() { echo "${RED}✗ $*${NC}" >&2; }
warn() { echo "${YELLOW}! $*${NC}"; }
ok()   { echo "${GREEN}✓ $*${NC}"; }

# ---------- 1. 配置文件 ----------
# 必须成对：只给一个路径时，另一个若回退到 legacy 文件就会误检查/误生成旧配置。
env_specified=${ENV_FILE+x}
backend_specified=${BACKEND_ENV_FILE+x}
selected_environment=0
[[ "$env_specified" == x && -n "${RAGKB_DEPLOY_ENVIRONMENT:-}" ]] && selected_environment=1
if [[ "$env_specified" != "$backend_specified" ]]; then
  fail 'ENV_FILE 与 BACKEND_ENV_FILE 必须同时设置，或不设置（使用旧单环境路径）'
  exit 1
fi
if [[ -n "${RAGKB_DEPLOY_ENVIRONMENT:-}" && "$env_specified" != x ]]; then
  fail '已选择部署环境，但缺少 ENV_FILE / BACKEND_ENV_FILE；请通过 deploy/env.sh 调用'
  exit 1
fi

ENV_FILE=${ENV_FILE:-.env}
BACKEND_ENV_FILE=${BACKEND_ENV_FILE:-backend/.env}
# 对应模板只从目标文件名推导，不猜测环境目录以外的旧模板。
# 管理员也可用 RAGKB_ENV_TEMPLATE / RAGKB_BACKEND_ENV_TEMPLATE 显式指定。
ENV_TEMPLATE=${RAGKB_ENV_TEMPLATE:-${ENV_FILE}.example}
BACKEND_ENV_TEMPLATE=${RAGKB_BACKEND_ENV_TEMPLATE:-${BACKEND_ENV_FILE}.example}

ensure_env_file() { # 目标文件 模板 名称
  local target=$1 template=$2 label=$3 dir
  if [[ -e "$target" ]]; then
    [[ -f "$target" ]] || { fail "$label 路径不是普通文件: $target"; exit 1; }
    ok "$label 已存在，保持不变: $target"
    return 0
  fi
  [[ -f "$template" ]] || {
    fail "缺少 $label，且对应模板不存在: $template"
    fail '请提供与选定配置同目录的 .env.example，或显式设置模板路径；不会猜测 legacy 模板'
    exit 1
  }
  dir=$(dirname "$target")
  mkdir -p "$dir"
  cp "$template" "$target"
  chmod 600 "$target" 2>/dev/null || true
  warn "已从 $template 生成 $target，请填写其中的口令/密钥（日志不回显配置值）"
}

ensure_env_file "$ENV_FILE" "$ENV_TEMPLATE" '部署环境配置'
ensure_env_file "$BACKEND_ENV_FILE" "$BACKEND_ENV_TEMPLATE" '应用环境配置'

# ---------- 2. 占位值检查 ----------
# 只比较变量名和已知弱值，不输出任何配置内容。宁可启动失败也不要静默放行。
problems=0
read_env_value() { # 文件 键 -> 原始值（去掉行尾空白，不执行 shell 展开）
  local file=$1 key=$2 value
  value=$(grep -E "^${key}=" "$file" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  value=${value%$'\r'}
  while [[ "$value" == *[[:space:]] ]]; do value=${value%?}; done
  if [[ ${#value} -ge 2 ]]; then
    case "$value" in
      \"*\") value=${value#\"}; value=${value%\"} ;;
      \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
  fi
  printf '%s' "$value"
}
check_placeholder() { # 文件 键 [弱值...]
  # 除了模板里的占位值，把仓库里出现过的弱口令也一并拦下。应用运行时的
  # JWT/ADMIN_PASSWORD 长度策略由后端负责；这里不额外变更认证规则。
  local file=$1 key=$2 value weak
  value=$(read_env_value "$file" "$key")
  if [[ -z "$value" ]]; then
    fail "$file 的 $key 为空"
    problems=$((problems + 1))
    return
  fi
  shift 2
  for weak in "$@"; do
    if [[ "$value" == "$weak" ]]; then
      fail "$file 的 $key 仍是占位/弱值，请替换为强随机值"
      problems=$((problems + 1))
      return
    fi
  done
}
check_placeholder "$ENV_FILE" POSTGRES_PASSWORD "change-me-strong-password" "testpw123" "123456"
# 旧单环境根 .env 仍含 JWT_SECRET；选定环境的部署模板只把它放在 backend.env。
if [[ $selected_environment -eq 0 || -n $(read_env_value "$ENV_FILE" JWT_SECRET) ]]; then
  check_placeholder "$ENV_FILE" JWT_SECRET "change-me-to-a-random-string"
fi
check_placeholder "$BACKEND_ENV_FILE" JWT_SECRET \
  "please-change-this-secret-in-production-0123456789" "change-me-to-a-random-string"
# ADMIN_PASSWORD 沿用旧单环境首次初始化检查；建号后应用是否仍读取它属于
# 应用配置语义，init 不扩展认证策略。
check_placeholder "$BACKEND_ENV_FILE" ADMIN_PASSWORD "Admin@123456" "change-me-on-first-login"

for provider in EMBEDDING_PROVIDER LLM_PROVIDER; do
  [[ $(read_env_value "$BACKEND_ENV_FILE" "$provider") == cloud ]] || continue
  for key in EMBEDDING_API_KEY LLM_API_KEY; do
    if [[ -z $(read_env_value "$BACKEND_ENV_FILE" "$key") ]]; then
      fail "$BACKEND_ENV_FILE 的 $key 为空（provider=cloud 时必须填写）"
      problems=$((problems + 1))
    fi
  done
  break
done

WEB_PORT=$(read_env_value "$ENV_FILE" WEB_PORT); WEB_PORT=${WEB_PORT:-80}
WEB_BIND_IP=$(read_env_value "$ENV_FILE" WEB_BIND_IP); WEB_BIND_IP=${WEB_BIND_IP:-0.0.0.0}
if [[ ! "$WEB_PORT" =~ ^[1-9][0-9]{0,4}$ ]] || (( WEB_PORT > 65535 )); then
  fail "$ENV_FILE 的 WEB_PORT 必须是 1-65535 的正整数"
  problems=$((problems + 1))
fi
if [[ -z "$WEB_BIND_IP" ]]; then
  fail "$ENV_FILE 的 WEB_BIND_IP 不能为空"
  problems=$((problems + 1))
fi

if [[ $problems -gt 0 ]]; then
  fail "有 $problems 项配置未就绪，修正后再执行部署"
  exit 1
fi
ok "配置检查通过（仅检查选定配置文件）"

# ---------- 3. 数据目录 ----------
# 已选环境：只读取 KEY=VALUE 的字面赋值并导出；入口规范的控制字段优先，
# 避免选中 .env 的相对/过期值覆盖入口。旧单环境保留原 source 语义，让
# ${DATA_DIR} 等变量引用按 shell 规则展开。
load_env_file() { # 文件
  local file=$1 line key value
  if [[ $selected_environment -eq 0 ]]; then
    # shellcheck disable=SC1090
    set -a; source <(grep -E '^[A-Z_]+=' "$file" | sed 's/[[:space:]]*$//'); set +a
    return
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    line=${line%$'\r'}
    [[ "$line" =~ ^[A-Z_][A-Z0-9_]*= ]] || continue
    key=${line%%=*}
    case "$key" in
      ENV_FILE|BACKEND_ENV_FILE|RAGKB_DEPLOY_ENVIRONMENT|COMPOSE_PROJECT_NAME|COMPOSE_FILE|DATA_DIR|BACKUP_DIR)
        continue ;;
    esac
    value=${line#*=}
    while [[ "$value" == *[[:space:]] ]]; do value=${value%?}; done
    if [[ ${#value} -ge 2 ]]; then
      case "$value" in
        \"*\") value=${value#\"}; value=${value%\"} ;;
        \'*\') value=${value#\'}; value=${value%\'} ;;
      esac
    fi
    declare -gx "$key=$value"
  done < "$file"
}
load_env_file "$ENV_FILE"
DATA_DIR=${DATA_DIR:-./data}
BACKUP_DIR=${BACKUP_DIR:-$DATA_DIR/backups}
COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-}
export ENV_FILE BACKEND_ENV_FILE ENV_TEMPLATE BACKEND_ENV_TEMPLATE
export DATA_DIR BACKUP_DIR WEB_PORT WEB_BIND_IP
[[ -n "$COMPOSE_PROJECT_NAME" ]] && export COMPOSE_PROJECT_NAME
[[ -n "${RAGKB_DEPLOY_ENVIRONMENT:-}" ]] && export RAGKB_DEPLOY_ENVIRONMENT

mkdir -p "$DATA_DIR"/{pgdata,uploads,projects,logs} "$BACKUP_DIR"
chmod 700 "$DATA_DIR/pgdata"

# 容器内 appuser 的 uid 是 1000：上传/日志/项目 blob 卷需要可写
# （项目 blob 由 project worker 写入、API 读取，必须与宿主目录同属主）。
if command -v chown >/dev/null 2>&1 && [[ $(id -u) -eq 0 ]]; then
  chown -R 1000:1000 "$DATA_DIR/uploads" "$DATA_DIR/logs" "$DATA_DIR/projects"
fi
ok "数据目录就绪: $DATA_DIR"

# ---------- 4. 构建源连通性 ----------
# 仅提示不阻断：源不通不会让部署失败，只是构建会慢到不可用，提前告知便于换源。
# 注意 npm 源必须用具体包路径探测——mirrors.cloud.tencent.com/npm/ 根路径固定 404，
# 用根路径会误报"不可达"。
check_source() { # 名称 探测URL
  if curl -m 6 -sI "$2" -o /dev/null 2>/dev/null; then
    ok "$1 可达"
  else
    warn "$1 不可达，构建可能非常慢（见 DEPLOY.md「国内网络加速」）"
  fi
}
# 离线/回归环境可用 RAGKB_INIT_SKIP_SOURCE_CHECK=1 明确跳过，或用命令替身
# 模拟 curl；不会访问真实公网或把探测结果当作启动前置条件。
if [[ "${RAGKB_INIT_SKIP_SOURCE_CHECK:-0}" == 1 ]]; then
  :
elif command -v curl >/dev/null 2>&1; then
  check_source "Debian apt 源" "https://mirrors.cloud.tencent.com/debian/dists/trixie/Release"
  check_source "PyPI 源"       "https://mirrors.cloud.tencent.com/pypi/simple/"
  check_source "npm 源"        "https://registry.npmmirror.com/vue"
fi

# ---------- 5. 端口占用（按选定端口与绑定地址） ----------
check_web_port() {
  command -v ss >/dev/null 2>&1 || return 0
  local listeners
  listeners=$(ss -ltn "sport = :${WEB_PORT}" 2>/dev/null || true)
  [[ -n "$listeners" ]] || return 0
  if printf '%s\n' "$listeners" | awk -v bind="$WEB_BIND_IP" '
    NR == 1 && $1 == "State" { next }
    {
      addr=$4
      sub(/:[0-9]+$/, "", addr)
      sub(/^\[/, "", addr); sub(/\]$/, "", addr)
      if (addr == "0.0.0.0" || addr == "::" || addr == "*" || addr == bind ||
          (bind == "0.0.0.0" && addr != "")) found=1
    }
    END { exit !found }'; then
    warn "端口 ${WEB_PORT} 在 ${WEB_BIND_IP} 上可能已被占用；请修改选中环境的 WEB_PORT/WEB_BIND_IP 或先停掉占用进程"
  fi
}
check_web_port

echo
if [[ -n "${RAGKB_DEPLOY_ENVIRONMENT:-}" ]]; then
  case "$RAGKB_DEPLOY_ENVIRONMENT" in
    production|staging)
      echo "下一步（环境入口）：bash deploy/env.sh $RAGKB_DEPLOY_ENVIRONMENT compose up -d --no-build"
      echo "生产镜像按已验收的不可变 tag 发布；请勿在启动时重新构建"
      echo "首次启动后检查：curl http://127.0.0.1:${WEB_PORT}/health（绑定 ${WEB_BIND_IP}）"
      ;;
    *)
      warn "未知的 RAGKB_DEPLOY_ENVIRONMENT 值，请通过 deploy/env.sh 显式选择 production/staging"
      echo "下一步：docker compose up -d --build"
      echo "首次启动后检查：curl http://127.0.0.1:${WEB_PORT}/health"
      ;;
  esac
else
  echo "下一步：docker compose up -d --build"
  echo "首次启动后检查：curl http://127.0.0.1:${WEB_PORT}/health"
fi
