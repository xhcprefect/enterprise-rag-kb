#!/usr/bin/env bash
# 同机双环境入口隔离回归。只在临时目录里创建合成配置与 fake docker，不读真实
# .env / backend/.env，不启动现有服务，不调用真实模型或远端。
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/ragkb-env-test.XXXXXX")
trap 'rm -rf "$work"' EXIT
export RAGKB_ENV_ROOT="$work/env"
bin="$work/bin"
real_docker=$(command -v docker || true)
original_path=$PATH
mkdir -p "$bin" "$work/env/production" "$work/env/staging" "$work/data/production" "$work/data/staging"
fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }
python_bin=python3
"$python_bin" -I -c 'import sys' >/dev/null 2>&1 || python_bin=python
"$python_bin" -I -c 'import sys' >/dev/null 2>&1 || fail '需要 Python 运行合成断言'

# fake docker：只记录 argv，让参数保真/global flag guard 不接触 Docker daemon。
cat > "$bin/docker" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_DOCKER_LOG:?}"
if [[ ${FAKE_DOCKER_FAIL:-0} == 1 ]]; then exit 91; fi
EOF
chmod +x "$bin/docker"
export PATH="$bin:$PATH"
export FAKE_DOCKER_LOG="$work/docker.log"
: > "$FAKE_DOCKER_LOG"

make_env() { # name data_dir project port backend_image port_prefix
  local name=$1 data=$2 project=$3 port=$4 image=$5 prefix=$6 dir
  dir="$RAGKB_ENV_ROOT/$name"
  cp "$root/deploy/environments/$name/.env.example" "$dir/.env"
  cp "$root/deploy/environments/$name/backend.env.example" "$dir/backend.env"
  # 用 shell 直接替换 DATA_DIR：Windows Python 会把 argv 里的 /tmp 变成 C:\...
  sed -i 's|^DATA_DIR=.*|DATA_DIR='"$data"'|' "$dir/.env"
  $python_bin -I - "$dir/.env" "$project" "$port" "$image" "$prefix" <<'PY'
import sys
path, project, port, image, prefix = sys.argv[1:]
with open(path, 'r', encoding='utf-8', newline='') as f:
    text = f.read()
replacements = {
    'BACKEND_IMAGE=rag-kb-backend:REPLACE_WITH_RELEASE_TAG': 'BACKEND_IMAGE=' + image,
    'BACKEND_IMAGE=rag-kb-backend:REPLACE_WITH_CANDIDATE_TAG': 'BACKEND_IMAGE=' + image,
    'FRONTEND_IMAGE=rag-kb-web:REPLACE_WITH_RELEASE_TAG': 'FRONTEND_IMAGE=rag-kb-web:' + prefix,
    'FRONTEND_IMAGE=rag-kb-web:REPLACE_WITH_CANDIDATE_TAG': 'FRONTEND_IMAGE=rag-kb-web:' + prefix,
    'POSTGRES_PASSWORD=change-me-strong-password': 'POSTGRES_PASSWORD=test-only-' + prefix,
}
if project:
    replacements['COMPOSE_PROJECT_NAME=ragkb-prod'] = 'COMPOSE_PROJECT_NAME=' + project
    replacements['COMPOSE_PROJECT_NAME=ragkb-staging'] = 'COMPOSE_PROJECT_NAME=' + project
if port:
    replacements['WEB_PORT=80'] = 'WEB_PORT=' + port
    replacements['WEB_PORT=18080'] = 'WEB_PORT=' + port
for old, new in replacements.items():
    text = text.replace(old, new)
with open(path, 'w', encoding='utf-8', newline='') as f:
    f.write(text)
PY
  # 测试用业务配置不连接任何模型：本地 provider，占位 JWT/密码仅供 loader 读取路径。
  sed -i -E 's/^(EMBEDDING_PROVIDER|LLM_PROVIDER)=.*/\1=ollama/' "$dir/backend.env"
  sed -i -E 's/^JWT_SECRET=.*/JWT_SECRET=test-only-'"$prefix"'/;s/^ADMIN_PASSWORD=.*/ADMIN_PASSWORD=test-only-'"$prefix"'/' "$dir/backend.env"
}

expect_fail() { # 描述 命令...
  local label=$1 out
  shift
  out="$work/failure.log"
  if "$@" >"$out" 2>&1; then
    cat "$out" >&2
    fail "$label 意外成功"
  fi
  # 错误消息不得回显合成配置里的 secret。
  grep -q 'test-only-prod\|test-only-stg' "$out" && fail "$label 回显了配置值"
  [[ ! -s "$out" ]] || grep -q . "$out" || fail "$label 没有错误提示"
  ok "$label 已拒绝"
}

make_env production "$work/data/production" ragkb-prod 80 backend:prod prod
make_env staging "$work/data/staging" ragkb-staging 18080 backend:candidate stg

echo '== 1. 严格环境和命令选择 =='
expect_fail '缺失环境参数' bash "$root/deploy/env.sh"
expect_fail '未知环境' bash "$root/deploy/env.sh" development compose ps
expect_fail '未知命令' bash "$root/deploy/env.sh" production deploy
expect_fail '缺少 compose 子命令' bash "$root/deploy/env.sh" production compose

echo '== 2. 首次 init：仅复制模板，不覆盖，失败要求填值 =='
rm -f "$RAGKB_ENV_ROOT/production/.env" "$RAGKB_ENV_ROOT/production/backend.env"
expect_fail '首次 init 生成占位后停止' bash "$root/deploy/env.sh" production init
[[ -f "$RAGKB_ENV_ROOT/production/.env" && -f "$RAGKB_ENV_ROOT/production/backend.env" ]] || fail 'init 未生成模板'
printf 'MARKER=keep\n' >> "$RAGKB_ENV_ROOT/production/.env"
expect_fail '第二次 init 应检查占位而非成功' bash "$root/deploy/env.sh" production init
grep -q '^MARKER=keep$' "$RAGKB_ENV_ROOT/production/.env" || fail 'init 覆盖了已有配置'
# 恢复合成配置，后续用例从稳定状态开始。
make_env production "$work/data/production" ragkb-prod 80 backend:prod prod
if [[ "$OSTYPE" == linux* ]]; then
  [[ $(stat -c '%a' "$RAGKB_ENV_ROOT/production/.env") == 600 ]] || fail 'init 配置权限不是 0600'
fi
ok '首次复制/后续检查/已有文件不覆盖'

echo '== 2b. init 环境目录 symlink 必须在写模板前拒绝 =='
symlink_probe="$work/symlink-probe"
printf 'probe\n' > "$work/symlink-probe-target"
if ln -s "$work/symlink-probe-target" "$symlink_probe" 2>/dev/null && \
   [[ -L "$symlink_probe" ]]; then
  symlink_root="$work/init-symlink-env"
  symlink_target="$work/init-symlink-target"
  mkdir -p "$symlink_root" "$symlink_target/production" "$symlink_target/staging"
  ln -s "$symlink_target/production" "$symlink_root/staging"
  expect_fail 'init 环境目录指向另一环境' env RAGKB_ENV_ROOT="$symlink_root" \
    bash "$root/deploy/env.sh" staging init
  [[ ! -e "$symlink_target/production/.env" && ! -e "$symlink_target/production/backend.env" ]] || \
    fail 'init 沿目录 symlink 写入了另一环境'
  rm -rf "$symlink_root" "$symlink_target" "$symlink_probe" "$work/symlink-probe-target"
  ok '目录 symlink 拒绝且目标未写文件'
else
  rm -f "$symlink_probe"
  rmdir "$symlink_probe" 2>/dev/null || true
  echo 'skip: 当前文件系统不支持目录 symlink 检测'
fi

echo '== 3. 参数保真与禁止绕过 =='
: > "$FAKE_DOCKER_LOG"
bash "$root/deploy/env.sh" production compose --profile summary --profile project --profile regression \
  up -d --no-build --remove-orphans >/dev/null
line=$(tail -1 "$FAKE_DOCKER_LOG")
expected="compose --env-file $RAGKB_ENV_ROOT/production/.env -p ragkb-prod -f docker-compose.yml -f docker-compose.host.yml --profile summary --profile project --profile regression up -d --no-build --remove-orphans"
[[ "$line" == "$expected" ]] || fail "compose argv 不符: $line"
# exec 内部 -f / --file 是应用参数，不得被 global guard 误伤。
bash "$root/deploy/env.sh" staging compose exec -T worker python worker.py -f /data/x --file=y >/dev/null
grep -Fq -- "exec -T worker python worker.py -f /data/x --file=y" "$FAKE_DOCKER_LOG" || fail 'exec 内部 -f 被误拦截'
for args in '-p other ps' '--project-name other ps' '--project-name=other ps' \
            '-f other.yml ps' '--file=other.yml ps' '--env-file /tmp/x ps' \
            '--project-directory /tmp ps' '--project-directory=/tmp ps' '-p=other ps'; do
  # shellcheck disable=SC2086
  expect_fail "拒绝 $args" bash "$root/deploy/env.sh" production compose $args
done
expect_fail '拒绝全局 prune' bash "$root/deploy/env.sh" production compose system prune --all
ok '显式 project/env-file/file、多 profile 与 exec 内部参数'

echo '== 4. ambient 冲突不能改变选定环境 =='
: > "$FAKE_DOCKER_LOG"
# 删除 BACKEND_ENV_FILE 冲突项：恶意父进程值由步骤 5 单独拒绝，不混入常规 ambient 用例。
COMPOSE_PROJECT_NAME=evil COMPOSE_FILE=evil.yml DATA_DIR=/tmp/evil BACKEND_IMAGE=evil:latest \
FRONTEND_IMAGE=evil:latest WEB_PORT=9999 RAGKB_TLS_MODE=https \
DB_MEM_LIMIT=8G API_CPUS=99 RAGKB_PROMETHEUS_PUBLISH=0.0.0.0 \
OUT_DIR=/tmp/evil-monitoring JWT_SECRET=ambient LLM_API_KEY=ambient-secret POSTGRES_PASSWORD=ambient \
bash "$root/deploy/env.sh" staging compose config >/dev/null 2>"$work/config-warning.log"
line=$(tail -1 "$FAKE_DOCKER_LOG")
[[ "$line" == *"-p ragkb-staging -f docker-compose.yml -f docker-compose.host.yml config"* ]] || fail "ambient 改变了 compose argv: $line"
grep -q '提示: compose config' "$work/config-warning.log" || fail 'config 未提示密钥输出风险'
# 精简 loader 的渲染变量，断言 ambient 不能覆盖选定值。
bash -c 'source "$1"; ragkb_load_environment staging || exit 1; printf "%s|%s|%s|%s|%s|%s|%s|%s\n" "$COMPOSE_PROJECT_NAME" "$DATA_DIR" "$BACKEND_IMAGE" "$BACKEND_ENV_FILE" "${OUT_DIR:-}" "$RAGKB_TLS_MODE" "$DB_MEM_LIMIT" "$API_CPUS"' \
  _ "$root/deploy/environment.sh" > "$work/loader.out"
expected="ragkb-staging|$work/data/staging|backend:candidate|$RAGKB_ENV_ROOT/staging/backend.env||http|512M|0.75"
[[ "$(cat "$work/loader.out")" == "$expected" ]] || fail "loader 字段被污染: $(cat "$work/loader.out")"
BACKEND_ENV_FILE=./backend/.env expect_fail '拒绝父进程跨环境 BACKEND_ENV_FILE' \
  bash "$root/deploy/env.sh" staging compose ps
ok 'COMPOSE_*/DATA_DIR/镜像/密钥/OUT_DIR 冲突'

echo '== 5. 身份/路径/配置 fail closed =='
cp "$RAGKB_ENV_ROOT/production/.env" "$work/prod.env.bak"
cp "$RAGKB_ENV_ROOT/staging/.env" "$work/stg.env.bak"
sed -i 's/^BACKEND_IMAGE=.*/BACKEND_IMAGE=backend:latest/' "$RAGKB_ENV_ROOT/production/.env"
expect_fail 'latest 镜像标签' bash "$root/deploy/env.sh" production compose ps
sed -i 's/^BACKEND_IMAGE=.*/BACKEND_IMAGE=rag-kb-backend:REPLACE_WITH_RELEASE_TAG/' "$RAGKB_ENV_ROOT/production/.env"
expect_fail '未替换的模板镜像' bash "$root/deploy/env.sh" production compose ps
sed -i 's/^BACKEND_IMAGE=.*/BACKEND_IMAGE=registry.example:5000\/team\/backend/' "$RAGKB_ENV_ROOT/production/.env"
expect_fail 'registry 端口被误当镜像标签' bash "$root/deploy/env.sh" production compose ps
sed -i 's|^BACKEND_IMAGE=.*|BACKEND_IMAGE=registry.example:5000/team/backend:1.2.3|' "$RAGKB_ENV_ROOT/production/.env"
bash -c 'source "$1"; ragkb_load_environment production >/dev/null' \
  _ "$root/deploy/environment.sh" || fail '带 registry 端口及显式标签的镜像被误拒绝'
mv "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
sed -i 's/^WEB_TLS_BIND_IP=.*/WEB_TLS_BIND_IP=0.0.0.0/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail 'staging HTTPS 公网绑定' bash "$root/deploy/env.sh" staging compose ps
sed -i 's/^WEB_TLS_BIND_IP=.*/WEB_TLS_BIND_IP=127.0.0.1/' "$RAGKB_ENV_ROOT/staging/.env"
for publish_key in RAGKB_PROMETHEUS_PUBLISH RAGKB_ALERTMANAGER_PUBLISH; do
  sed -i "s/^${publish_key}=.*/${publish_key}=0.0.0.0/" "$RAGKB_ENV_ROOT/staging/.env"
  expect_fail "staging ${publish_key} 公网发布" bash "$root/deploy/env.sh" staging compose ps
  sed -i "s/^${publish_key}=.*/${publish_key}=127.0.0.1/" "$RAGKB_ENV_ROOT/staging/.env"
done
sed -i 's/^RAGKB_DEPLOY_ENVIRONMENT=production/RAGKB_DEPLOY_ENVIRONMENT=staging/' "$RAGKB_ENV_ROOT/production/.env"
expect_fail '拷错环境身份' bash "$root/deploy/env.sh" production compose ps
sed -i 's/^RAGKB_DEPLOY_ENVIRONMENT=.*/RAGKB_DEPLOY_ENVIRONMENT=production/' "$RAGKB_ENV_ROOT/production/.env"
sed -i 's|^DATA_DIR=.*|DATA_DIR='"$work"'/data|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '数据目录与另一环境嵌套' bash "$root/deploy/env.sh" staging compose ps
sed -i 's|^DATA_DIR=.*|DATA_DIR='"$work"'/data/production|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '数据目录相同' bash "$root/deploy/env.sh" production compose ps
sed -i 's|^DATA_DIR=.*|DATA_DIR=/|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '根目录数据路径' bash "$root/deploy/env.sh" staging compose ps
mv "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '缺少 host 资源覆盖' bash "$root/deploy/env.sh" staging compose ps
mv "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env" 2>/dev/null || true
# 重新生成 staging 保证缺文件/模板替换用例有完整基线。
make_env staging "$work/data/staging" ragkb-staging 18080 backend:candidate stg
rm -f "$RAGKB_ENV_ROOT/staging/backend.env"
expect_fail '缺少 backend.env' bash "$root/deploy/env.sh" staging compose ps
make_env staging "$work/data/staging" ragkb-staging 18080 backend:candidate stg
# 已有文件优先，不受测试文件系统是否支持 symlink 影响。
cp -f "$RAGKB_ENV_ROOT/production/.env" "$RAGKB_ENV_ROOT/staging/backend.env"
expect_fail 'backend.env 混入另一环境配置' bash "$root/deploy/env.sh" staging compose ps
if ln -s "$RAGKB_ENV_ROOT/production/backend.env" "$RAGKB_ENV_ROOT/staging/shared" 2>/dev/null && \
   [[ -L "$RAGKB_ENV_ROOT/staging/shared" ]]; then
  rm "$RAGKB_ENV_ROOT/staging/backend.env"
  mv "$RAGKB_ENV_ROOT/staging/shared" "$RAGKB_ENV_ROOT/staging/backend.env"
  expect_fail 'backend.env 为跨环境 symlink' bash "$root/deploy/env.sh" staging compose ps
  rm "$RAGKB_ENV_ROOT/staging/backend.env"
else
  rm -f "$RAGKB_ENV_ROOT/staging/shared"
  echo 'skip: 当前文件系统不支持 symlink 检测'
fi
cp "$root/deploy/environments/staging/backend.env.example" "$RAGKB_ENV_ROOT/staging/backend.env"
ok '身份/相同嵌套路径/缺文件/覆盖组合/symlink'

echo '== 5b. 可选 TLS/监控/告警/异地路径跨环境隔离 =='
# 显式独立路径：字段都出现但两环境互不重叠，应正常通过 loader。
separate_paths() { # 环境目录 前缀
  cat >> "$1/.env" <<EOF
OUT_DIR=$2
RAGKB_PROMETHEUS_CONFIG_DIR=$2/prometheus
RAGKB_PROMETHEUS_SECRET_DIR=$2/prometheus-secrets
RAGKB_ALERTMANAGER_CONFIG_DIR=$2/alertmanager
RAGKB_ALERTMANAGER_SECRET_DIR=$2/alertmanager-secrets
RAGKB_TLS_CERT_DIR=$2/certs
RAGKB_ACME_WEBROOT=$2/acme
RAGKB_METRICS_TOKEN_FILE=$2/metrics_token
RAGKB_ALERT_WEBHOOK_FILE=$2/alert_webhook_url
RCLONE_CONFIG=$2/rclone.conf
EOF
}
cp "$RAGKB_ENV_ROOT/production/.env" "$work/prod.env.bak"
cp "$RAGKB_ENV_ROOT/staging/.env" "$work/stg.env.bak"
separate_paths "$RAGKB_ENV_ROOT/production" "$work/data/production/opt"
separate_paths "$RAGKB_ENV_ROOT/staging" "$work/data/staging/opt"
bash -c 'source "$1"; ragkb_load_environment staging >/dev/null' \
  _ "$root/deploy/environment.sh" || fail '独立可选路径被误拒绝'
# 目录相同 / 祖先-后代关系均拒绝。
sed -i 's|^OUT_DIR=.*|OUT_DIR='"$work"'/shared|' "$RAGKB_ENV_ROOT/production/.env"
sed -i 's|^OUT_DIR=.*|OUT_DIR='"$work"'/shared|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '两环境 OUT_DIR 相同' bash "$root/deploy/env.sh" staging compose ps
sed -i 's|^OUT_DIR=.*|OUT_DIR='"$work"'/shared|' "$RAGKB_ENV_ROOT/production/.env"
sed -i 's|^OUT_DIR=.*|OUT_DIR='"$work"'/shared/staging|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '两环境 OUT_DIR 嵌套' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
# 文件相同，以及 token 文件落入另一环境 OUT_DIR 都拒绝。
separate_paths "$RAGKB_ENV_ROOT/production" "$work/data/production/opt"
separate_paths "$RAGKB_ENV_ROOT/staging" "$work/data/staging/opt"
sed -i 's|^RAGKB_METRICS_TOKEN_FILE=.*|RAGKB_METRICS_TOKEN_FILE='"$work"'/shared/metrics_token|' "$RAGKB_ENV_ROOT/production/.env"
sed -i 's|^RAGKB_METRICS_TOKEN_FILE=.*|RAGKB_METRICS_TOKEN_FILE='"$work"'/shared/metrics_token|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '两环境 metrics token 文件相同' bash "$root/deploy/env.sh" staging compose ps
sed -i 's|^RAGKB_METRICS_TOKEN_FILE=.*|RAGKB_METRICS_TOKEN_FILE='"$work"'/data/production/opt/metrics_token|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail 'staging token 文件落入 production OUT_DIR' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
# ${DATA_DIR} 展开后指向同一个共享目录也必须拒绝（各自 DATA_DIR 不同）。
printf 'OUT_DIR=${DATA_DIR}/../shared\n' >> "$RAGKB_ENV_ROOT/production/.env"
printf 'OUT_DIR=${DATA_DIR}/../shared\n' >> "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '两环境 OUT_DIR 展开后同名' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
# 不同目录字段指向同一路径（production OUT_DIR == staging Prometheus config dir）。
separate_paths "$RAGKB_ENV_ROOT/production" "$work/data/production/opt"
separate_paths "$RAGKB_ENV_ROOT/staging" "$work/data/staging/opt"
sed -i 's|^OUT_DIR=.*|OUT_DIR='"$work"'/data/staging/opt/prometheus|' "$RAGKB_ENV_ROOT/production/.env"
expect_fail '不同目录字段同路径' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
# production token 文件落入 staging OUT_DIR（反向 file->dir）。
separate_paths "$RAGKB_ENV_ROOT/production" "$work/data/production/opt"
separate_paths "$RAGKB_ENV_ROOT/staging" "$work/data/staging/opt"
sed -i 's|^RAGKB_METRICS_TOKEN_FILE=.*|RAGKB_METRICS_TOKEN_FILE='"$work"'/data/staging/opt/metrics_token|' "$RAGKB_ENV_ROOT/production/.env"
expect_fail 'production token 落入 staging OUT_DIR' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
# 不同文件字段指向同一绝对路径（production token == staging webhook）。
separate_paths "$RAGKB_ENV_ROOT/production" "$work/data/production/opt"
separate_paths "$RAGKB_ENV_ROOT/staging" "$work/data/staging/opt"
sed -i 's|^RAGKB_METRICS_TOKEN_FILE=.*|RAGKB_METRICS_TOKEN_FILE='"$work"'/shared/secret-file|' "$RAGKB_ENV_ROOT/production/.env"
sed -i 's|^RAGKB_ALERT_WEBHOOK_FILE=.*|RAGKB_ALERT_WEBHOOK_FILE='"$work"'/shared/secret-file|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '不同文件字段同路径' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
ok '可选路径 equal/ancestor/descendant/文件落对方目录拒绝'

echo '== 5b2. 可选配额覆盖与 HTTPS 有效默认目录 =='
for name in production staging; do
  sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.host.yml:docker-compose.https.yml|' "$RAGKB_ENV_ROOT/$name/.env"
  bash -c 'source "$1"; ragkb_load_environment "$2"; printf "%s\n" "$RAGKB_ACME_WEBROOT"' \
    _ "$root/deploy/environment.sh" "$name" > "$work/$name-acme.out"
  [[ "$(cat "$work/$name-acme.out")" == "$work/data/$name/acme" ]] || fail "$name 未派生独立 ACME 默认目录"
done
# 另一环境字段指向所选环境的有效默认路径，及反向组合，均须在 Docker 前拒绝。
printf 'RAGKB_ACME_WEBROOT=%s\n' "$work/data/staging/acme" >> "$RAGKB_ENV_ROOT/production/.env"
expect_fail '显式 ACME 与所选环境默认路径冲突' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
printf 'OUT_DIR=%s\n' "$work/data/staging/acme" >> "$RAGKB_ENV_ROOT/production/.env"
expect_fail '另一字段目录与默认 ACME 冲突' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.host.yml:docker-compose.https.yml|' "$RAGKB_ENV_ROOT/production/.env"
printf 'RAGKB_ACME_WEBROOT=%s\n' "$work/data/production/acme" >> "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '所选显式 ACME 与另一环境默认路径冲突' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.host.yml:docker-compose.alerting.yml|' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '告警缺少监控覆盖' bash "$root/deploy/env.sh" staging compose ps
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.host.yml:docker-compose.monitoring.yml:docker-compose.alerting.yml|' "$RAGKB_ENV_ROOT/staging/.env"
: > "$FAKE_DOCKER_LOG"
bash "$root/deploy/env.sh" staging compose ps >/dev/null
grep -Fq -- '-f docker-compose.host.monitoring.yml -f docker-compose.host.alerting.yml ps' "$FAKE_DOCKER_LOG" || fail '启用监控/告警时未追加专属配额覆盖'
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
ok '核心默认不新增服务、可选配额自动追加、有效默认 ACME 参与隔离'

echo '== 5c. 发布端口范围与全局唯一 =='
sed -i 's/^WEB_PORT=.*/WEB_PORT=0/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '端口 0' bash "$root/deploy/env.sh" staging compose ps
sed -i 's/^WEB_PORT=.*/WEB_PORT=65536/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '端口大于 65535' bash "$root/deploy/env.sh" staging compose ps
sed -i 's/^WEB_PORT=.*/WEB_PORT=18080/' "$RAGKB_ENV_ROOT/staging/.env"
sed -i 's/^WEB_TLS_PORT=.*/WEB_TLS_PORT=18080/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '同一环境内端口重复' bash "$root/deploy/env.sh" staging compose ps
sed -i 's/^WEB_TLS_PORT=.*/WEB_TLS_PORT=18443/' "$RAGKB_ENV_ROOT/staging/.env"
sed -i 's/^RAGKB_PROMETHEUS_PORT=.*/RAGKB_PROMETHEUS_PORT=80/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '跨字段跨环境端口冲突' bash "$root/deploy/env.sh" staging compose ps
# 可选端口省略时按 Compose 默认值（443/9090/9093）参与冲突比较。
sed -i 's/^WEB_PORT=.*/WEB_PORT=443/' "$RAGKB_ENV_ROOT/staging/.env"
sed -i '/^WEB_TLS_PORT=/d;/^RAGKB_PROMETHEUS_PORT=/d;/^RAGKB_ALERTMANAGER_PORT=/d' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '省略可选端口仍与 production 默认 443 冲突' bash "$root/deploy/env.sh" staging compose ps
sed -i 's/^WEB_PORT=.*/WEB_PORT=9090/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '省略可选端口仍与 production 默认 9090 冲突' bash "$root/deploy/env.sh" staging compose ps
# production 删除 PROMETHEUS_PORT 时仍按 Compose 默认 9090 参与跨环境冲突。
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
sed -i '/^RAGKB_PROMETHEUS_PORT=/d' "$RAGKB_ENV_ROOT/production/.env"
sed -i 's/^WEB_PORT=.*/WEB_PORT=9090/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail 'production 省略 PROMETHEUS_PORT 仍与 staging WEB_PORT 9090 冲突' \
  bash "$root/deploy/env.sh" staging compose ps
# 所选环境自身删除可选端口时，默认值也要参与其端口唯一性比较。
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
sed -i 's/^WEB_PORT=.*/WEB_PORT=9090/' "$RAGKB_ENV_ROOT/staging/.env"
sed -i '/^RAGKB_PROMETHEUS_PORT=/d' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '所选环境省略 PROMETHEUS_PORT 仍检出自身端口重复' \
  bash "$root/deploy/env.sh" staging compose ps
# production 删除 WEB_PORT 时仍按 Compose 默认 80 参与跨环境冲突。
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
sed -i '/^WEB_PORT=/d' "$RAGKB_ENV_ROOT/production/.env"
sed -i 's/^WEB_PORT=.*/WEB_PORT=80/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail 'production 省略 WEB_PORT 仍与 staging WEB_PORT 80 冲突' \
  bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
sed -i '/^RAGKB_ALERTMANAGER_PORT=/d' "$RAGKB_ENV_ROOT/production/.env"
expect_fail '省略生产 Alertmanager 端口与 staging 的 19093 冲突' bash "$root/deploy/env.sh" staging compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
sed -i 's/^WEB_PORT=.*/WEB_PORT=00080/' "$RAGKB_ENV_ROOT/staging/.env"
expect_fail '端口前导零不能绕过 production 的 80' bash "$root/deploy/env.sh" staging compose ps
cp "$work/stg.env.bak" "$RAGKB_ENV_ROOT/staging/.env"
sed -i 's/^WEB_TLS_PORT=.*/WEB_TLS_PORT=080/' "$RAGKB_ENV_ROOT/production/.env"
expect_fail '同环境前导零端口重复' bash "$root/deploy/env.sh" production compose ps
cp "$work/prod.env.bak" "$RAGKB_ENV_ROOT/production/.env"
ok '1-65535 范围、数值端口唯一（含真实 Compose 默认值和前导零）'

echo '== 6. legacy 单环境兼容 =='
bash -c 'source "$1"; ENV_FILE=/dev/null COMPOSE_PROJECT_NAME= COMPOSE_FILE=docker-compose.yml ragkb_compose_command; printf "%s\n" "${RAGKB_COMPOSE_CMD[*]}"' \
  _ "$root/deploy/compose-command.sh" > "$work/legacy.out"
[[ "$(cat "$work/legacy.out")" == 'docker compose --env-file /dev/null -f docker-compose.yml' ]] || fail "legacy helper argv: $(cat "$work/legacy.out")"
# 基础 Compose 仍可用默认镜像/配置/端口；用真实 CLI（若可用）。不读仓库根实际
# .env 或 backend/.env：显式 --env-file /dev/null 与合成 BACKEND_ENV_FILE。
if [[ -n "$real_docker" ]] && "$real_docker" compose version >/dev/null 2>&1; then
  legacy_backend_env="$work/legacy-backend.env"
  printf 'ENVIRONMENT=production\nAPP_NAME=legacy-synthetic\n' > "$legacy_backend_env"
  POSTGRES_PASSWORD=test-only-legacy BACKEND_ENV_FILE="$legacy_backend_env" \
  "$real_docker" compose --env-file /dev/null \
    -f "$root/docker-compose.yml" config --format json > "$work/legacy-compose.json" 2>"$work/legacy-compose.err" || {
      cat "$work/legacy-compose.err" >&2; fail 'legacy Compose config 失败';
    }
  $python_bin -I - "$work/legacy-compose.json" "$legacy_backend_env" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
legacy_backend_env = sys.argv[2]
assert d['name'] == 'rag-kb', d['name']
s = d['services']
assert s['api']['image'] == s['worker']['image'] == 'rag-kb-backend:latest'
assert s['web']['image'] == 'rag-kb-web:latest'
assert s['web']['ports'][0]['published'] == '80'
# 合成 backend env 的内容应被 env_file 展开，证明没有读取仓库真实 backend/.env。
assert s['api']['environment']['APP_NAME'] == 'legacy-synthetic', s['api']['environment']
PY
  ok '真实 CLI: legacy 默认值保持'
else
  echo 'skip: legacy Compose render（真实 CLI 不可用）'
fi

echo '== 7. 实际环境入口真实 Compose render（CLI 可用时） =='
if [[ -n "$real_docker" ]] && "$real_docker" compose version >/dev/null 2>&1; then
  # 通过实际 env.sh 与受控模板渲染，不能手拼变量绕过 loader。只执行 config，不启动服务。
  make_env production "$work/data/production" ragkb-prod 80 backend:prod prod
  make_env staging "$work/data/staging" ragkb-staging 18080 backend:candidate stg
  for name in production staging; do
    printf 'APP_NAME=%s-synthetic\n' "$name" >> "$RAGKB_ENV_ROOT/$name/backend.env"
    cat >> "$RAGKB_ENV_ROOT/$name/.env" <<'EOF'
RAGKB_SERVER_NAME=render.example.invalid
RAGKB_TLS_CERT_DIR=${DATA_DIR}/tls
OUT_DIR=${DATA_DIR}/monitoring
RAGKB_PROMETHEUS_CONFIG_DIR=${DATA_DIR}/monitoring/prometheus
RAGKB_PROMETHEUS_SECRET_DIR=${DATA_DIR}/monitoring/secrets
RAGKB_ALERTMANAGER_CONFIG_DIR=${DATA_DIR}/monitoring/alertmanager
RAGKB_ALERTMANAGER_SECRET_DIR=${DATA_DIR}/monitoring/alertmanager/secrets
EOF
  done
  render_count=0
  for variant in core https monitoring alerting full; do
    files=docker-compose.yml:docker-compose.host.yml
    case "$variant" in
      https) files+=:docker-compose.https.yml ;;
      monitoring) files+=:docker-compose.monitoring.yml ;;
      alerting) files+=:docker-compose.monitoring.yml:docker-compose.alerting.yml ;;
      full) files+=:docker-compose.https.yml:docker-compose.monitoring.yml:docker-compose.alerting.yml ;;
    esac
    for name in production staging; do
      sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$files|" "$RAGKB_ENV_ROOT/$name/.env"
    done
    for name in production staging; do
      PATH="$original_path" bash "$root/deploy/env.sh" "$name" compose \
        --profile summary --profile project --profile regression config --format json \
        > "$work/$variant-$name.json" 2>"$work/$variant-$name.err" || {
          cat "$work/$variant-$name.err" >&2; fail "$variant/$name 实际入口 render 失败";
        }
      ! grep -q 'test-only-' "$work/$variant-$name.err" || fail "$variant/$name stderr 含 secret"
      render_count=$((render_count + 1))
    done
    $python_bin -I - "$work/$variant-production.json" "$work/$variant-staging.json" "$variant" <<'PY'
import json, sys
models = [json.load(open(p, encoding='utf-8')) for p in sys.argv[1:3]]
variant = sys.argv[3]
https = variant in ('https', 'full')
monitoring = variant in ('monitoring', 'alerting', 'full')
alerting = variant in ('alerting', 'full')
writers = ('api', 'worker', 'worker-summary', 'worker-project', 'worker-regression')
mount_sets, ports, networks, volume_names = [], [], [], []
for name, model, image, prefix in [('production', models[0], 'backend:prod', 'prod'),
                                    ('staging', models[1], 'backend:candidate', 'stg')]:
    project = 'ragkb-prod' if name == 'production' else 'ragkb-staging'
    assert model['name'] == project, model['name']
    assert model['networks']['internal']['name'] == project + '_internal'
    networks.append(model['networks']['internal']['name'])
    services = model['services']
    expected = {'db', 'web', *writers}
    if monitoring: expected.add('prometheus')
    if alerting: expected.add('alertmanager')
    assert set(services) == expected, (variant, name, set(services))
    bind = '0.0.0.0' if name == 'production' else '127.0.0.1'
    web_ports = services['web']['ports']
    assert len(web_ports) == (2 if https else 1)
    assert (web_ports[0]['host_ip'], str(web_ports[0]['published'])) == (bind, '80' if name == 'production' else '18080')
    if https:
        assert (web_ports[1]['host_ip'], str(web_ports[1]['published'])) == (bind, '443' if name == 'production' else '18443')
        acme = next(v for v in services['web']['volumes'] if v['target'] == '/var/www/certbot')
        assert acme['read_only'] and acme['source'].replace('\\', '/').endswith('/data/' + name + '/acme'), acme
    assert services['web']['image'] == 'rag-kb-web:' + prefix
    for svc in writers:
        assert services[svc]['image'] == image, (name, svc)
        assert services[svc]['environment']['APP_NAME'] == name + '-synthetic'
        assert services[svc]['environment']['DATABASE_URL'].endswith('@db:5432/' + ('ragdb' if name == 'production' else 'ragdb_staging'))
        for leaf in ('uploads', 'projects', 'logs'):
            assert any(v['target'] == '/data/' + leaf and v['source'].replace('\\', '/').endswith('/data/' + name + '/' + leaf) for v in services[svc]['volumes']), (name, svc, leaf)
    for svc, config in services.items():
        assert float(config['cpus']) > 0 and int(config['mem_limit']) > 0, (variant, name, svc)
        assert config['logging']['driver'] == 'json-file', (name, svc)
        assert config['logging']['options'] == {'max-size': '20m' if name == 'production' else '10m', 'max-file': '5' if name == 'production' else '3'}, (name, svc)
    if monitoring:
        assert int(services['prometheus']['mem_limit']) == (256 if name == 'production' else 192) * 1024**2
        assert services['prometheus']['ports'][0]['host_ip'] == '127.0.0.1'
        assert str(services['prometheus']['ports'][0]['published']) == ('9090' if name == 'production' else '19090')
    if alerting:
        assert int(services['alertmanager']['mem_limit']) == (128 if name == 'production' else 64) * 1024**2
        assert services['alertmanager']['ports'][0]['host_ip'] == '127.0.0.1'
        assert str(services['alertmanager']['ports'][0]['published']) == ('9093' if name == 'production' else '19093')
    # 唯一允许共享的是 checkout 中只读的 nginx 配置；所有可变状态必须独立。
    mount_sets.append({v['source'] for svc in services.values() for v in svc.get('volumes', []) if v['type'] == 'bind' and v['target'] not in ('/etc/nginx/conf.d/app.conf', '/etc/nginx/ragkb/snippets')})
    ports.append({str(p['published']) for svc in services.values() for p in svc.get('ports', [])})
    volume_names.append({v['name'] for v in model.get('volumes', {}).values()})
assert networks[0] != networks[1]
assert not (mount_sets[0] & mount_sets[1]), mount_sets
assert not (ports[0] & ports[1]), ports
assert not (volume_names[0] & volume_names[1]), volume_names
PY
    ok "$variant: 两环境实际入口/全部服务配额日志/镜像/env_file/路径端口隔离"
  done
  [[ $render_count -eq 10 ]] || fail '实际环境入口十份 Compose render 未全部完成'
else
  echo 'skip: 实际环境入口 Compose render（真实 CLI 不可用）'
fi

echo '环境隔离回归通过：严格选择、init 占位、参数保真、ambient、fail closed、legacy、Compose render。'
