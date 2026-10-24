#!/usr/bin/env bash
# 首次部署前的准备：建数据目录、检查配置是否还是模板值、确认密钥没漏。
# 只做检查与建目录，不改任何已存在的文件。
set -euo pipefail

cd "$(dirname "$0")/.."

RED=$'\033[31m'; YELLOW=$'\033[33m'; GREEN=$'\033[32m'; NC=$'\033[0m'
fail() { echo "${RED}✗ $*${NC}" >&2; }
warn() { echo "${YELLOW}! $*${NC}"; }
ok()   { echo "${GREEN}✓ $*${NC}"; }

# ---------- 1. 配置文件 ----------
if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    warn "已从 .env.example 生成 .env，请填写其中的 POSTGRES_PASSWORD"
  else
    fail "缺少 .env"; exit 1
  fi
fi

if [[ ! -f backend/.env ]]; then
  if [[ -f backend/.env.example ]]; then
    cp backend/.env.example backend/.env
    warn "已从 backend/.env.example 生成 backend/.env，请填写 API Key"
  else
    fail "缺少 backend/.env"; exit 1
  fi
fi

# ---------- 2. 占位值检查 ----------
# 这些值一旦带上生产环境就是安全事故，宁可启动失败也不要静默放行
problems=0
check_placeholder() { # 文件 键 占位值
  local value
  value=$(grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- || true)
  if [[ -z "$value" || "$value" == "$3" ]]; then
    fail "$1 的 $2 仍是占位值"
    problems=$((problems + 1))
  fi
}
check_placeholder .env POSTGRES_PASSWORD "change-me-strong-password"
check_placeholder .env JWT_SECRET "change-me-to-a-random-string"
check_placeholder backend/.env JWT_SECRET "please-change-this-secret-in-production-0123456789"
check_placeholder backend/.env ADMIN_PASSWORD "Admin@123456"

if grep -qE '^(EMBEDDING_PROVIDER|LLM_PROVIDER)=cloud' backend/.env; then
  for key in EMBEDDING_API_KEY LLM_API_KEY; do
    value=$(grep -E "^${key}=" backend/.env | tail -1 | cut -d= -f2-)
    if [[ -z "$value" ]]; then
      fail "backend/.env 的 $key 为空（provider=cloud 时必须填写）"
      problems=$((problems + 1))
    fi
  done
fi

if [[ $problems -gt 0 ]]; then
  fail "有 $problems 项配置未就绪，修正后再执行部署"
  exit 1
fi
ok "配置检查通过"

# ---------- 3. 数据目录 ----------
# shellcheck disable=SC1091
set -a; source <(grep -E '^[A-Z_]+=' .env | sed 's/[[:space:]]*$//'); set +a
DATA_DIR=${DATA_DIR:-./data}

mkdir -p "$DATA_DIR"/{pgdata,uploads,logs,backups}
chmod 700 "$DATA_DIR/pgdata"

# 容器内 appuser 的 uid 是 1000，上传/日志卷需要可写
if command -v chown >/dev/null 2>&1 && [[ $(id -u) -eq 0 ]]; then
  chown -R 1000:1000 "$DATA_DIR/uploads" "$DATA_DIR/logs"
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
if command -v curl >/dev/null 2>&1; then
  check_source "Debian apt 源" "https://mirrors.cloud.tencent.com/debian/dists/trixie/Release"
  check_source "PyPI 源"       "https://mirrors.cloud.tencent.com/pypi/simple/"
  check_source "npm 源"        "https://registry.npmmirror.com/vue"
fi

# ---------- 5. 端口占用 ----------
WEB_PORT=$(grep -E '^WEB_PORT=' .env | tail -1 | cut -d= -f2- || echo 80)
if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :${WEB_PORT}" 2>/dev/null | grep -q LISTEN; then
  warn "端口 ${WEB_PORT} 已被占用，改 .env 里的 WEB_PORT 或先停掉占用进程"
fi

echo
echo "下一步：docker compose up -d --build"
echo "首次启动后检查：curl http://localhost:${WEB_PORT}/health"
