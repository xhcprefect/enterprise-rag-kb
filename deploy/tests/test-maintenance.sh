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
#   6. 可配置停写预算、非法预算在停服务前拒绝。
#
# 只在独占临时容器内执行：脚本使用固定的 /tmp 目录并在退出时删除。
set -euo pipefail
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
rm -rf "$FAKE_DOCKER_STATE"
: > "$OPERATIONS_LOG"

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

echo '维护脚本回归通过：新格式三件产物、全角色停写/只恢复原运行、双目录恢复与回滚、旧格式兼容、缺项/链接/混合批次/篡改拒绝、可配置预算。'
