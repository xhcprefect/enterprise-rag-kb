#!/usr/bin/env bash
# 隔离演练维护脚本；Docker CLI 被替身替代，绝不接触运行中的真实服务。
#
# 覆盖：
#   1. 新格式备份（db + uploads + projects）与校验清单；
#   2. 停写覆盖所有正在运行的写角色（含 profile 角色与旧版遗留容器），
#      且只恢复“原本在运行”的容器；
#   3. 恢复新格式、失败回滚（uploads 与 projects 都还原）；
#   4. 恢复旧格式（清单只有两项、无 projects 行）；
#   5. 损坏/缺项/混合批次/含链接的归档必须在停写前拒绝；
#   6. 可配置停写预算、非法预算在停服务前拒绝；
#   7. staging/production 双 project 隔离：只停启选定项目的全部活跃 writer，
#      compose config/db exec 都带显式 --env-file、-p、多个 -f，坏配置先拒绝；
#   8. init 选定环境路径与旧单环境动态变量引用展开。
#
# 只在独占、无网络的临时 Linux 容器内执行：docker 是命令替身，仓库只读挂载；
# 脚本使用容器私有的 /tmp 目录并在退出时删除。
set -euo pipefail
unset RAGKB_DEPLOY_ENVIRONMENT COMPOSE_PROJECT_NAME COMPOSE_FILE BACKEND_ENV_FILE \
  FAKE_DOCKER_PROJECT_RUNNING RAGKB_INIT_SKIP_SOURCE_CHECK
root=$(cd "$(dirname "$0")/../.." && pwd)
bin=$(mktemp -d)
work=/tmp/rag-maintenance-test
trap 'rm -rf "$bin" "$work"' EXIT
rm -rf "$work"
mkdir -p "$work/uploads" "$work/projects" "$work/backups" "$bin"
cp "$root/deploy/tests/fake-docker.sh" "$bin/docker"
chmod +x "$bin/docker"
export PATH="$bin:$PATH"
export FAKE_DOCKER_STATE="$work/docker-state"
export OPERATIONS_LOG="$work/operations.log"
export FAKE_COMPOSE_LOG="$work/compose-calls.log"
rm -rf "$FAKE_DOCKER_STATE"
: > "$OPERATIONS_LOG"
: > "$FAKE_COMPOSE_LOG"

ENV="$work/fixture.env"
cat > "$ENV" <<EOF
DATA_DIR=$work
BACKUP_DIR=$work/backups
POSTGRES_USER=postgres
POSTGRES_DB=regression_test
KEEP_DAYS=7
EOF
export ENV_FILE="$ENV"

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_contains() { grep -q -- "$2" "$1" || fail "缺少输出: $2"; }
compose_line() { grep -F "$1|" "$FAKE_COMPOSE_LOG" | tail -1; }
assert_compose_call() { # 子命令 project env_file 文件列表 backend_env_file
  local line
  line=$(compose_line "$1") || fail "缺少 compose 调用: $1"
  IFS='|' read -r -a fields <<< "$line"
  [[ ${fields[1]} == "$2" ]] || fail "compose $1 project 错误: ${fields[*]}"
  [[ ${fields[2]} == "$3" ]] || fail "compose $1 --env-file 错误: ${fields[*]}"
  [[ ${fields[3]} == "$4" ]] || fail "compose $1 -f 列表错误: ${fields[*]}"
  [[ ${fields[4]} == "$5" ]] || fail "compose $1 应用配置错误: ${fields[*]}"
  for file in $4; do
    grep -q -- "-f $file" "$OPERATIONS_LOG" || fail "compose $1 缺少显式 -f $file"
  done
  [[ $(grep -c -- '--env-file ' "$OPERATIONS_LOG") -ge 1 ]] \
    || fail "compose $1 未显式传递 --env-file"
  [[ $(grep -c -- "-p $2" "$OPERATIONS_LOG") -ge 1 ]] \
    || fail "compose $1 未显式传递 -p"
  grep -q -- "--env-file $3" "$OPERATIONS_LOG" || fail "compose $1 未显式传递 --env-file"
  grep -q -- "-f ${4%% *} -f ${4##* }" "$OPERATIONS_LOG" \
    || fail "compose $1 未把冒号 COMPOSE_FILE 拆成多个 -f"
  if grep -qE '^compose .* -f [^ ]*:[^ ]* ' "$OPERATIONS_LOG"; then
    fail "compose $1 把冒号路径作为单个 -f 参数"
  fi
}
reset_calls() { : > "$OPERATIONS_LOG"; : > "$FAKE_COMPOSE_LOG"; }

# ---------- 初始数据 ----------
printf 'original-upload' > "$work/uploads/content.txt"
printf 'blob-content' > "$work/projects/abc123"
mkdir -p "$work/projects/.nested"
printf 'blob-2' > "$work/projects/.nested/def456"

echo "== 1. 新格式备份：uploads + projects + 清单 =="
bash "$root/deploy/backup.sh"
mapfile -t dbs < <(ls "$work"/backups/ragdb-*.sql.gz)
mapfile -t ups < <(ls "$work"/backups/uploads-*.tar.gz)
mapfile -t prj < <(ls "$work"/backups/projects-*.tar.gz)
mapfile -t mfs < <(ls "$work"/backups/manifest-*.sha256)
[[ ${#dbs[@]} -eq 1 && ${#ups[@]} -eq 1 && ${#prj[@]} -eq 1 && ${#mfs[@]} -eq 1 ]] \
  || fail "备份产物数量不正确"
[[ $(wc -l < "${mfs[0]}") -eq 3 ]] || fail '新清单一律三行'
tar -tzf "${prj[0]}" | grep -q '^projects/abc123$' || fail 'projects 归档缺少 blob'
tar -tzf "${ups[0]}" | grep -q '^uploads/content.txt$' || fail 'uploads 归档缺少文件'
assert_contains "${mfs[0]}" 'projects-'
echo "   ok: 三件产物、三行清单"

echo "== 2. 停写覆盖全部写角色（含第四角色 regression），只重启原本运行的 =="
# 先把替身状态重置为“新部署”：api + worker + 三个 profile 角色 + 旧版遗留 ingest-worker。
rm -rf "$FAKE_DOCKER_STATE"
export FAKE_DOCKER_RUNNING='c-api api c-ing worker c-sum worker-summary c-prj worker-project c-reg worker-regression c-old ingest-worker c-web web'
: > "$OPERATIONS_LOG"
rm -f "$work"/backups/*.sql.gz "$work"/backups/*.tar.gz "$work"/backups/manifest-*.sha256
bash "$root/deploy/backup.sh" >/dev/null
for id in c-api c-ing c-sum c-prj c-reg c-old; do
  grep -q "^stop --timeout [0-9]* $id$" "$OPERATIONS_LOG" \
    || fail "未停止写容器 $id"
done
if grep -q "stop --timeout .* c-web" "$OPERATIONS_LOG"; then fail '误停 web'; fi
# 比例：API 用短预算，worker 角色用 worker 预算。
assert_contains "$OPERATIONS_LOG" 'stop --timeout 180 c-ing'
assert_contains "$OPERATIONS_LOG" 'stop --timeout 180 c-reg'
assert_contains "$OPERATIONS_LOG" 'stop --timeout 30 c-api'
# 重启的必须恰好是之前那六个写容器，且不包含已停止/未运行的 web。
started=$(sed -n 's/^start //p' "$OPERATIONS_LOG" | sort | tr '\n' ' ')
[[ "$started" == "c-api c-ing c-old c-prj c-reg c-sum " ]] || fail "恢复列表不符: $started"
if grep -q '^start c-web$' "$OPERATIONS_LOG"; then fail '重启了原本未运行的 web'; fi
echo "   ok: 停/启列表与角色超时正确（含 regression）"

echo "== 2b. 未运行的 regression 角色不会被停写或顺手拉起 =="
# regression 是显式 profile：没启动过就不该出现在停写/恢复名单里。
rm -rf "$FAKE_DOCKER_STATE"
export FAKE_DOCKER_RUNNING='c-api api c-ing worker c-sum worker-summary'
: > "$OPERATIONS_LOG"
rm -f "$work"/backups/*.sql.gz "$work"/backups/*.tar.gz "$work"/backups/manifest-*.sha256
bash "$root/deploy/backup.sh" >/dev/null
if grep -q 'c-reg' "$OPERATIONS_LOG"; then fail '未运行的 regression 容器被停写/重启'; fi
started=$(sed -n 's/^start //p' "$OPERATIONS_LOG" | sort | tr '\n' ' ')
[[ "$started" == "c-api c-ing c-sum " ]] || fail "未运行 regression 时恢复列表不符: $started"
# 这一轮的产物留给后续恢复用例使用（仍是 db + uploads + projects 三件套）。
mapfile -t dbs < <(ls "$work"/backups/ragdb-*.sql.gz)
mapfile -t ups < <(ls "$work"/backups/uploads-*.tar.gz)
mapfile -t prj < <(ls "$work"/backups/projects-*.tar.gz)
mapfile -t mfs < <(ls "$work"/backups/manifest-*.sha256)
[[ ${#dbs[@]} -eq 1 && ${#ups[@]} -eq 1 && ${#prj[@]} -eq 1 && ${#mfs[@]} -eq 1 ]] \
  || fail "第二次备份产物数量不正确"
echo "   ok: 只处理实际运行的写角色"

echo "== 3. 恢复新格式：uploads 与 projects 文件都还原 =="
printf 'mutated' > "$work/uploads/content.txt"
printf 'mutated' > "$work/projects/abc123"
printf 'yes\n' | bash "$root/deploy/restore.sh" "${dbs[0]}" "${ups[0]}" >/dev/null
[[ $(cat "$work/uploads/content.txt") == original-upload ]] || fail 'uploads 未还原'
[[ $(cat "$work/projects/abc123") == blob-content ]] || fail 'projects 未还原'
[[ -f "$work/projects/.nested/def456" ]] || fail 'projects 子目录未还原'
echo "   ok: 两棵目录均恢复"

echo "== 4. 省略第三个参数时按清单自动发现 projects 归档 =="
printf 'mutated' > "$work/projects/abc123"
printf 'yes\n' | bash "$root/deploy/restore.sh" "${dbs[0]}" "${ups[0]}" >/dev/null
[[ $(cat "$work/projects/abc123") == blob-content ]] || fail '未自动发现 projects 归档'
echo "   ok: 自动发现"

echo "== 5. SQL 失败：uploads 与 projects 都回滚 =="
printf 'current-upload' > "$work/uploads/content.txt"
printf 'current-blob' > "$work/projects/abc123"
if printf 'yes\n' | FAIL_RESTORE=1 bash "$root/deploy/restore.sh" \
     "${dbs[0]}" "${ups[0]}" "${prj[0]}" >/dev/null 2>&1; then
  fail '失败的 SQL 恢复被误报成功'
fi
[[ $(cat "$work/uploads/content.txt") == current-upload ]] || fail 'SQL 失败后 uploads 未回滚'
[[ $(cat "$work/projects/abc123") == current-blob ]] || fail 'SQL 失败后 projects 未回滚'
echo "   ok: 双目录回滚"

echo "== 5b. 回滚 mv 失败：保留暂存目录（不得删除唯一原件） =="
# 用 mv 包装器只让“previous/x -> DATA/x”的回滚动作失败，验证 cleanup 在
# 回滚未完成时保留 stage/previous，而不是打印提示后再 rm -rf。
cat > "$bin/mv" <<'MV'
#!/usr/bin/env bash
if [[ ${FAIL_ROLLBACK_MV:-0} == 1 && "$1" == */previous/* ]]; then
  echo 'mv: simulated rollback failure' >&2; exit 1
fi
exec /bin/mv "$@"
MV
chmod +x "$bin/mv"
printf 'pre-rollback-upload' > "$work/uploads/content.txt"
printf 'pre-rollback-blob' > "$work/projects/abc123"
err="$work/rollback-mv.err"
if printf 'yes\n' | FAIL_RESTORE=1 FAIL_ROLLBACK_MV=1 bash "$root/deploy/restore.sh" \
     "${dbs[0]}" "${ups[0]}" "${prj[0]}" >/dev/null 2>"$err"; then
  fail '回滚 mv 失败被误报成功'
fi
grep -q '回滚未完成' "$err" || fail '回滚失败未提示保留暂存目录'
stage_kept=$(sed -n 's/.*已保留暂存目录 \([^（]*\)（.*/\1/p' "$err" | head -1)
[[ -n "$stage_kept" && -d "$stage_kept/previous" ]] || fail '回滚失败后暂存目录/原件被删除'
# projects 回滚成功（mv 只对 uploads previous 失败？这里两者同时失败），至少原件仍在 previous。
[[ -e "$stage_kept/previous/uploads" || -e "$stage_kept/previous/projects" ]] \
  || fail '暂存目录里没有任何待人工恢复的原件'
rm -rf "$stage_kept"
rm -f "$bin/mv"
# 回滚失败时 DATA_DIR 下的目录可能已被移除（原件保留在 previous），
# 这里重建目录并放回原始内容，供后续用例继续执行。
mkdir -p "$work/uploads" "$work/projects"
printf 'original-upload' > "$work/uploads/content.txt"
printf 'blob-content' > "$work/projects/abc123"

echo "== 6. 旧格式备份（无 projects 项）仍可恢复，projects 保持不变 =="
# 第 5 步已把 uploads 回滚成 current-upload；重建归档前先放回“备份内容”。
printf 'original-upload' > "$work/uploads/content.txt"
# 先保证 uploads 归档是可解压的真实目录内容（下面第 10 步会追加字节模拟篡改）。
tar -czf "${ups[0]}" -C "$work" uploads
tar -czf "${prj[0]}" -C "$work" projects
# 重建归档后同步刷新清单，保证后续“缺项/篡改”用例是因为目标原因被拒，
# 而不是卡在无关的校验值不匹配上。
(cd "$work/backups" && sha256sum "$(basename "${dbs[0]}")" "$(basename "${ups[0]}")" \
   "$(basename "${prj[0]}")" > "$(basename "${mfs[0]}")")
legacy="$work/legacy"
mkdir -p "$legacy"
cp "${dbs[0]}" "$legacy/ragdb-legacy-1.sql.gz"
cp "${ups[0]}" "$legacy/uploads-legacy-1.tar.gz"
(cd "$legacy" && sha256sum ragdb-legacy-1.sql.gz uploads-legacy-1.tar.gz \
   > manifest-legacy-1.sha256)
printf 'keep-me' > "$work/projects/abc123"
printf 'yes\n' | bash "$root/deploy/restore.sh" \
  "$legacy/ragdb-legacy-1.sql.gz" "$legacy/uploads-legacy-1.tar.gz" >/dev/null 2>&1
[[ $(cat "$work/uploads/content.txt") == original-upload ]] || fail '旧格式 uploads 未恢复'
[[ $(cat "$work/projects/abc123") == keep-me ]] || fail '旧格式恢复误改 projects'
echo "   ok: 旧备份恢复且不触碰 projects"

echo "== 6b. 旧格式恢复 SQL 失败：只回滚 uploads，projects 原样保留 =="
# 回归：cleanup 曾遍历 uploads/projects，且在“previous 不存在但 DATA 存在”时直接
# rm，导致只恢复 uploads 的旧备份在 SQL 失败后把现有 projects 删掉。
printf 'current-upload-2' > "$work/uploads/content.txt"
printf 'project-must-survive' > "$work/projects/abc123"
if printf 'yes\n' | FAIL_RESTORE=1 bash "$root/deploy/restore.sh" \
     "$legacy/ragdb-legacy-1.sql.gz" "$legacy/uploads-legacy-1.tar.gz" >/dev/null 2>&1; then
  fail '旧格式 SQL 失败被误报成功'
fi
[[ $(cat "$work/uploads/content.txt") == current-upload-2 ]] || fail '旧格式 SQL 失败后 uploads 未回滚'
[[ -f "$work/projects/abc123" ]] || fail '旧格式 SQL 失败后 projects 被删除'
[[ $(cat "$work/projects/abc123") == project-must-survive ]] || fail '旧格式 SQL 失败后 projects 被改写'
echo "   ok: 仅回滚 uploads，projects 未动"

echo "== 7. 缺 projects 归档（清单声明三项）必须在停写前拒绝 =="
rm -f "$work"/backups/projects-*.tar.gz
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" "${dbs[0]}" "${ups[0]}" >/dev/null 2>&1; then
  fail '缺 projects 归档仍被接受'
fi
[[ ! -s "$OPERATIONS_LOG" ]] || fail '校验失败前已停写'
# 恢复归档供后续测试使用。
tar -czf "${prj[0]}" -C "$work" projects

echo "== 8. 归档含链接必须在停写前拒绝 =="
ln -s /etc/passwd "$work/uploads/escape"
tar -czf "$work/unsafe.tar.gz" -C "$work" uploads
rm -f "$work/uploads/escape"
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" "${dbs[0]}" "$work/unsafe.tar.gz" "${prj[0]}" >/dev/null 2>&1; then
  fail '含链接归档被接受'
fi
[[ ! -s "$OPERATIONS_LOG" ]] || fail '含链接归档在停写前未被拒绝'

echo "== 9. 旧清单 + 显式 projects 归档（混合批次）必须拒绝 =="
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" \
     "$legacy/ragdb-legacy-1.sql.gz" "$legacy/uploads-legacy-1.tar.gz" "${prj[0]}" >/dev/null 2>&1; then
  fail '混合批次被接受'
fi
[[ ! -s "$OPERATIONS_LOG" ]] || fail '混合批次在停写前未被拒绝'

echo "== 10. 清单被篡改必须在停写前拒绝 =="
cp "${mfs[0]}" "$work/manifest.bak"
printf 'tamper' >> "${ups[0]}"
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" "${dbs[0]}" "${ups[0]}" "${prj[0]}" >/dev/null 2>&1; then
  fail '清单不匹配被接受'
fi
[[ ! -s "$OPERATIONS_LOG" ]] || fail '清单不匹配在停写前未被拒绝'
mv "$work/manifest.bak" "${mfs[0]}"
# 还原被追加的 uploads 归档，保持后续断言可重复。
tar -czf "${ups[0]}" -C "$work" uploads

echo "== 11. 可配置停写预算与非法预算 =="
budget_dir="$work/budget"
rm -rf "$budget_dir"; mkdir -p "$budget_dir/uploads" "$budget_dir/projects"
printf 'x' > "$budget_dir/uploads/x.txt"
printf 'y' > "$budget_dir/projects/y"
printf 'DATA_DIR=%s\nBACKUP_DIR=%s/backups\nPOSTGRES_USER=postgres\nPOSTGRES_DB=budget_test\n' \
  "$budget_dir" "$budget_dir" > "$work/budget.env"
: > "$OPERATIONS_LOG"
rm -rf "$FAKE_DOCKER_STATE"
ENV_FILE="$work/budget.env" WORKER_STOP_TIMEOUT_SECONDS=240 \
  bash "$root/deploy/backup.sh" >/dev/null
grep -q 'stop --timeout 240' "$OPERATIONS_LOG" || fail '预算未传入 stop'
: > "$OPERATIONS_LOG"
if ENV_FILE="$work/budget.env" WORKER_STOP_TIMEOUT_SECONDS=abc \
     bash "$root/deploy/backup.sh" >/dev/null 2>&1; then
  fail '非法预算被接受'
fi
[[ ! -s "$OPERATIONS_LOG" ]] || fail '非法预算在停服务前未被拒绝'
rm -rf "$budget_dir"

echo "== 12a. docker ps 失败：fail closed，不备份、不停写 =="
: > "$OPERATIONS_LOG"
rm -f "$work"/backups/manifest-*.sha256 "$work"/backups/ragdb-*.sql.gz \
      "$work"/backups/uploads-*.tar.gz "$work"/backups/projects-*.tar.gz
if FAIL_DOCKER_PS=1 bash "$root/deploy/backup.sh" >/dev/null 2>&1; then
  fail 'docker ps 失败被当成空名单继续备份'
fi
[[ -z "$(ls "$work"/backups/manifest-*.sha256 2>/dev/null)" ]] || fail 'docker ps 失败仍发布清单'
if grep -q 'pg_dump\|^stop ' "$OPERATIONS_LOG"; then fail 'docker ps 失败后仍停写/导出'; fi

echo "== 12b. compose config 失败：fail closed =="
: > "$OPERATIONS_LOG"
if FAIL_COMPOSE_CONFIG=1 COMPOSE_PROJECT_NAME= bash "$root/deploy/backup.sh" >/dev/null 2>&1; then
  fail 'compose config 失败被当成没有写服务'
fi
[[ -z "$(ls "$work"/backups/manifest-*.sha256 2>/dev/null)" ]] || fail 'compose 失败仍发布清单'
if grep -q 'pg_dump\|^stop ' "$OPERATIONS_LOG"; then fail 'compose 失败后仍停写/导出'; fi

echo "== 12. pg_dump 失败：产物不发布，服务恢复 =="
rm -f "$work"/backups/manifest-*.sha256 "$work"/backups/ragdb-*.sql.gz \
      "$work"/backups/uploads-*.tar.gz "$work"/backups/projects-*.tar.gz
: > "$OPERATIONS_LOG"
if FAIL_DUMP=1 bash "$root/deploy/backup.sh" >/dev/null 2>&1; then
  fail '失败的 pg_dump 被误报成功'
fi
[[ -z "$(ls "$work"/backups/manifest-*.sha256 2>/dev/null)" ]] || fail '失败仍发布清单'
grep -q '^start c-api$' "$OPERATIONS_LOG" || fail '失败后未恢复服务'

# ---------- 同机双环境：只维护显式选定的 project ----------
# A 提供的 helper 已落盘；测试不复刻它。env 文件模拟 env.sh 的规范导出：
# 绝对路径的 BACKEND_ENV_FILE 不得被部署 .env 中的相对/空值覆盖。
echo "== 13. 双环境维护隔离（staging 与 production 双向） =="
iso="$work/isolation"
mkdir -p "$iso/staging"/{uploads,projects,backups} "$iso/production"/{uploads,projects,backups} "$iso/app"
printf 'name: wrong-project-from-base\n' > "$iso/base.yml"
printf 'services: {}\n' > "$iso/host.yml"
printf 'staging' > "$iso/app/staging-backend.env"
printf 'production' > "$iso/app/production-backend.env"
for env_name in staging production; do
  cat > "$iso/$env_name.env" <<EOF
DATA_DIR=$iso/$env_name
BACKUP_DIR=$iso/$env_name/backups
POSTGRES_USER=postgres
POSTGRES_DB=${env_name}_db
KEEP_DAYS=7
BACKEND_ENV_FILE=./relative-should-not-win.env
COMPOSE_FILE=$iso/base.yml:$iso/host.yml
ENV_FILE=/dev/null-should-not-win.env
EOF
done
export FAKE_DOCKER_PROJECT_RUNNING="ragkb-staging:s-api:api ragkb-staging:s-ing:worker ragkb-staging:s-sum:worker-summary ragkb-staging:s-prj:worker-project ragkb-staging:s-reg:worker-regression ragkb-staging:s-web:web ragkb-prod:p-api:api ragkb-prod:p-ing:worker ragkb-prod:p-sum:worker-summary ragkb-prod:p-prj:worker-project ragkb-prod:p-reg:worker-regression ragkb-prod:p-web:web"
unset FAKE_DOCKER_RUNNING
rm -rf "$FAKE_DOCKER_STATE"

run_isolated_backup() { # environment project data_dir
  local environment=$1 project=$2 data_dir=$3 env_file=$4 backend=$5 log=$6
  reset_calls
  RAGKB_DEPLOY_ENVIRONMENT="$environment" \
  ENV_FILE="$env_file" BACKEND_ENV_FILE="$backend" \
  DATA_DIR="$data_dir" BACKUP_DIR="$data_dir/backups" \
  POSTGRES_USER=postgres POSTGRES_DB="${environment}_db" \
  COMPOSE_PROJECT_NAME="$project" COMPOSE_FILE="$iso/base.yml:$iso/host.yml" \
    bash "$root/deploy/backup.sh" > "$log" 2>&1 \
    || { cat "$log" >&2; fail "$environment 备份失败"; }
}

run_isolated_backup staging ragkb-staging "$iso/staging" "$iso/staging.env" \
  "$iso/app/staging-backend.env" "$iso/staging.log"
assert_contains "$iso/staging.log" '环境=staging'
assert_contains "$iso/staging.log" '数据库=staging_db'
assert_contains "$iso/staging.log" "$iso/staging/backups"
for id in s-api s-ing s-sum s-prj s-reg; do
  grep -q "^stop --timeout [0-9]* $id$" "$OPERATIONS_LOG" \
    || fail "staging 未停止所有活跃 writer: $id"
  grep -q "^start $id$" "$OPERATIONS_LOG" || fail "staging 未恢复 $id"
done
for id in p-api p-ing p-sum p-prj p-reg; do
  if grep -qE "^(stop --timeout [0-9]*|start) $id$" "$OPERATIONS_LOG"; then
    fail "staging 维护误操作 production 容器: $id"
  fi
done
grep -q '^ps --filter label=com.docker.compose.project=ragkb-staging' "$OPERATIONS_LOG" \
  || fail '停写未按 staging project 标签过滤'
assert_compose_call pg_dump ragkb-staging "$iso/staging.env" \
  "$iso/base.yml $iso/host.yml" "$iso/app/staging-backend.env"
[[ $(awk -F'|' '$1=="pg_dump" {n++} END {print n+0}' "$FAKE_COMPOSE_LOG") -eq 1 ]] \
  || fail 'staging pg_dump 的 compose 调用次数不正确'

# 合成一次同批恢复：明确验证 psql 与 pg_dump 指向同一 project/配置，仍然只停 staging。
db=$(ls -1t "$iso/staging"/backups/ragdb-*.sql.gz | head -1)
up=$(ls -1t "$iso/staging"/backups/uploads-*.tar.gz | head -1)
pr=$(ls -1t "$iso/staging"/backups/projects-*.tar.gz | head -1)
reset_calls
printf 'yes\n' | RAGKB_DEPLOY_ENVIRONMENT=staging ENV_FILE="$iso/staging.env" \
  BACKEND_ENV_FILE="$iso/app/staging-backend.env" \
  DATA_DIR="$iso/staging" BACKUP_DIR="$iso/staging/backups" \
  POSTGRES_USER=postgres POSTGRES_DB=staging_db \
  COMPOSE_PROJECT_NAME=ragkb-staging COMPOSE_FILE="$iso/base.yml:$iso/host.yml" \
  bash "$root/deploy/restore.sh" "$db" "$up" "$pr" >/dev/null \
  || fail 'staging 恢复失败'
assert_compose_call psql ragkb-staging "$iso/staging.env" \
  "$iso/base.yml $iso/host.yml" "$iso/app/staging-backend.env"
for id in p-api p-ing p-sum; do
  if grep -qE "^(stop --timeout [0-9]*|start) $id$" "$OPERATIONS_LOG"; then
    fail "staging 恢复误操作 production 容器: $id"
  fi
done

# 反向：production 也只停自己的 writer，state 中 staging 容器仍全部在运行。
rm -rf "$FAKE_DOCKER_STATE"
run_isolated_backup production ragkb-prod "$iso/production" "$iso/production.env" \
  "$iso/app/production-backend.env" "$iso/production.log"
assert_contains "$iso/production.log" '环境=production'
for id in p-api p-ing p-sum p-prj p-reg; do
  grep -q "^stop --timeout [0-9]* $id$" "$OPERATIONS_LOG" \
    || fail "production 未停止所有活跃 writer: $id"
done
for id in s-api s-ing s-sum s-prj s-reg; do
  if grep -qE "^(stop --timeout [0-9]*|start) $id$" "$OPERATIONS_LOG"; then
    fail "production 维护误操作 staging 容器: $id"
  fi
done
grep -q '^ps --filter label=com.docker.compose.project=ragkb-prod' "$OPERATIONS_LOG" \
  || fail '停写未按 production project 标签过滤'
assert_compose_call pg_dump ragkb-prod "$iso/production.env" \
  "$iso/base.yml $iso/host.yml" "$iso/app/production-backend.env"

# 坏配置必须在任何 stop/pg_dump 前拒绝；测试替身会在 compose config 阶段失败。
reset_calls
if FAIL_COMPOSE_CONFIG=1 COMPOSE_PROJECT_NAME= \
  RAGKB_DEPLOY_ENVIRONMENT=staging ENV_FILE="$iso/staging.env" \
  BACKEND_ENV_FILE="$iso/app/staging-backend.env" \
  DATA_DIR="$iso/staging" BACKUP_DIR="$iso/staging/backups" \
  COMPOSE_FILE="$iso/base.yml:$iso/host.yml" \
  bash "$root/deploy/backup.sh" >/dev/null 2>&1; then
  fail '坏配置仍被接受'
fi
if grep -qE '^(stop --timeout|start) |pg_dump' "$OPERATIONS_LOG"; then
  fail '坏配置在停写/导出之后才被拒绝'
fi

# init 的选定环境路径：不读/生成 legacy 文件，不覆盖已有文件，不回显密钥。
echo "== 14. init 选定环境路径与模板/占位/幂等 =="
init_home="$iso/init-home"
mkdir -p "$init_home/repo/backend" "$init_home/repo/deploy/environments/select" \
  "$init_home/deploy/environments/select"
cp "$root/deploy/init.sh" "$init_home/repo/deploy/init.sh"
printf 'legacy-root-secret\n' > "$init_home/repo/.env"
printf 'legacy-app-secret\n' > "$init_home/repo/backend/.env"
cat > "$init_home/deploy/environments/select/.env.example" <<EOF
DATA_DIR=$iso/init-data
POSTGRES_PASSWORD=synthetic-strong-password-20261114
WEB_PORT=18080
WEB_BIND_IP=127.0.0.1
WORKER_STOP_TIMEOUT_SECONDS=180
EOF
cat > "$init_home/deploy/environments/select/backend.env.example" <<EOF
JWT_SECRET=synthetic-strong-jwt-secret-20261114
ADMIN_PASSWORD=synthetic-strong-admin-password-20261114
EOF
init_env="$init_home/deploy/environments/select/.env"
init_backend="$init_home/deploy/environments/select/backend.env"
init_out="$iso/init.log"
init_stub="$bin/isolation-init"
mkdir -p "$init_stub"
cat > "$init_stub/curl" <<'CURL'
#!/usr/bin/env bash
# 离线测试替身：记录探测但不访问公网，明确返回可达。
printf '%s\n' "$*" >> "${FAKE_CURL_LOG:?}"
exit 0
CURL
chmod +x "$init_stub/curl"
export FAKE_CURL_LOG="$iso/curl.log"
: > "$FAKE_CURL_LOG"
init_run() { # 输出文件
  (cd "$init_home/repo" && RAGKB_DEPLOY_ENVIRONMENT=staging \
    ENV_FILE="$init_env" BACKEND_ENV_FILE="$init_backend" \
    PATH="$init_stub:$PATH" bash deploy/init.sh > "$1" 2>&1)
}
init_run "$init_out" \
  || { cat "$init_out" >&2; fail '选定环境的 init 成功路径执行失败'; }
[[ -f "$init_env" && -f "$init_backend" ]] || fail 'init 未从所选模板生成文件'
[[ $(grep -c 'https://' "$FAKE_CURL_LOG") -eq 3 ]] || fail 'init 未通过 curl 替身完成三项源探测'
grep -q '环境入口' "$init_out" || fail 'init 未提示环境入口命令'
grep -q -- 'compose up -d --no-build' "$init_out" || fail 'init 未提示不可变镜像启动'
grep -q 'synthetic-strong' "$init_out" && fail 'init 日志泄露配置值'
grep -q '环境=staging' "$init_out" && fail 'init 不应输出维护目标提示'
[[ $(cat "$init_home/repo/.env") == legacy-root-secret ]] || fail 'init 改写了 legacy 根 .env'
[[ $(cat "$init_home/repo/backend/.env") == legacy-app-secret ]] || fail 'init 改写了 legacy backend/.env'
# 填写配置后必须报告占位/弱值、不覆盖已填文件、也不触碰 legacy 或公网。
cat > "$init_env" <<EOF
DATA_DIR=$iso/init-data
POSTGRES_PASSWORD=change-me-strong-password
WEB_PORT=18080
WEB_BIND_IP=127.0.0.1
EOF
cat > "$init_backend" <<EOF
JWT_SECRET=change-me-to-a-random-string
ADMIN_PASSWORD=Admin@123456
EOF
if init_run "$init_out"; then
  fail 'init 未拒绝选定环境的占位值'
fi
if grep -q 'change-me-strong-password\|change-me-to-a-random-string\|Admin@123456' "$init_out"; then
  fail 'init 拒绝占位值时回显了密钥值'
fi
assert_contains "$init_out" '占位/弱值'
[[ $(grep -c '^' "$init_env") -eq 4 ]] || fail 'init 覆盖了已有的部署配置'
[[ $(grep -c '^' "$init_backend") -eq 2 ]] || fail 'init 覆盖了已有的应用配置'
rm -f "$init_env" "$init_backend" "$init_home/deploy/environments/select/.env.example"
if init_run "$init_out"; then
  fail '缺少所选环境模板时 init 未明确失败'
fi
grep -q '对应模板不存在' "$init_out" || fail '缺模板失败原因不明确'
[[ ! -e "$init_env" && ! -e "$init_backend" ]] || fail '缺模板时猜测并生成了 legacy 配置'

# 旧单环境 init：保留 source 的变量展开语义，BACKUP_DIR=${DATA_DIR}/...
# 不得变成字面目录，也不得把未展开值作为路径创建。
echo "== 15. init legacy 动态 DATA_DIR/BACKUP_DIR 展开 =="
legacy_home="$iso/legacy-init"
legacy_data="$legacy_home/data"
mkdir -p "$legacy_home/deploy" "$legacy_home/backend"
cp "$root/deploy/init.sh" "$legacy_home/deploy/init.sh"
cat > "$legacy_home/.env" <<EOF
DATA_DIR=$legacy_data
BACKUP_DIR=\${DATA_DIR}/custom-backups
POSTGRES_PASSWORD=synthetic-strong-password-20261114
JWT_SECRET=synthetic-strong-jwt-secret-20261114
WEB_PORT=18080
WEB_BIND_IP=127.0.0.1
EOF
cp "$legacy_home/.env" "$legacy_home/.env.expected"
cat > "$legacy_home/backend/.env" <<EOF
JWT_SECRET=synthetic-strong-jwt-secret-20261114
ADMIN_PASSWORD=synthetic-strong-admin-password-20261114
EOF
cp "$legacy_home/backend/.env" "$legacy_home/backend/.env.expected"
init_run_legacy() {
  (cd "$legacy_home" && env -u ENV_FILE -u BACKEND_ENV_FILE -u RAGKB_DEPLOY_ENVIRONMENT \
    -u COMPOSE_PROJECT_NAME -u COMPOSE_FILE \
    PATH="$init_stub:$PATH" bash deploy/init.sh > "$init_out" 2>&1)
}
init_run_legacy || { cat "$init_out" >&2; fail 'legacy init 成功路径执行失败'; }
cmp -s "$legacy_home/.env" "$legacy_home/.env.expected" || fail 'legacy init 改写了已有 .env'
cmp -s "$legacy_home/backend/.env" "$legacy_home/backend/.env.expected" \
  || fail 'legacy init 改写了已有 backend/.env'
[[ -d "$legacy_data/pgdata" && -d "$legacy_data/custom-backups" ]] \
  || fail 'legacy init 未创建展开后的数据/备份目录'
[[ ! -e "$legacy_home/\${DATA_DIR}" ]] || fail 'legacy init 创建了字面 ${DATA_DIR} 目录'
[[ ! -e "$legacy_home/legacy/\${DATA_DIR}" ]] || fail 'legacy init 在仓库内创建了字面目录'
grep -q "数据目录就绪: $legacy_data" "$init_out" || fail 'legacy init 输出未显示展开后的 DATA_DIR'

echo '维护脚本回归通过：新格式三件产物、全角色停写/只恢复原运行、双目录恢复与回滚、旧格式兼容、缺项/链接/混合批次/篡改拒绝、可配置预算、staging/production 维护隔离、init 选定环境路径与 legacy 动态展开。'
