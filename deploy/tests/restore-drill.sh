#!/usr/bin/env bash
# 隔离恢复演练：在独立的临时 Compose 项目里做**真实**的 pg_dump + psql 恢复，
# 再核对上传文件 SHA-256 与数据库行内容。不接触生产 DATA_DIR、不停止生产服务、
# 不向任何真实 remote 写入。
#
# 安全边界：
#   - 默认 dry-run：只打印将要创建/删除的资源和命令，不做任何改动。
#   - 必须显式 --isolated 才执行；执行前校验 owner 标记与项目名前缀。
#   - 生成临时 env（只含测试凭据），通过 --env-file 显式传入，绝不 source 真实 .env。
#   - 不加载仓库根 docker-compose.yml（它 env_file backend/.env 指向真实配置），
#     演练使用自带的 deploy/tests/drill-compose.yml。
#   - 清理前核对容器/卷/网络/临时目录的 ownership 标记；不一致就拒绝删除并保留现场。
#
# 用法：
#   ./deploy/tests/restore-drill.sh                 # dry-run，打印计划
#   ./deploy/tests/restore-drill.sh --isolated      # 实际执行
#   ./deploy/tests/restore-drill.sh --isolated --keep   # 执行后保留现场（默认清理）
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
用法: restore-drill.sh [--isolated] [--keep] [--rows N]

  --isolated   执行演练（缺省为 dry-run，只打印计划）
  --keep       演练结束后保留容器/卷/临时目录，便于排查
  --rows N     写入测试库的文档行数，默认 25
EOF
  exit 2
}

mode=dry-run
keep=0
rows=25
while [[ $# -gt 0 ]]; do
  case "$1" in
    --isolated) mode=isolated; shift ;;
    --keep) keep=1; shift ;;
    --rows) [[ $# -ge 2 ]] || usage; rows=$2; shift 2 ;;
    -h|--help) usage ;;
    *) echo "未知参数: $1" >&2; usage ;;
  esac
done
[[ "$rows" =~ ^[1-9][0-9]{0,3}$ ]] || { echo '--rows 必须是 1-9999 的整数' >&2; exit 2; }

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
compose_file="$repo/deploy/tests/drill-compose.yml"

# ---------- owner / 项目标识 ----------
# owner 由演练自己生成并写入所有资源标签；清理时逐项核对，必须是本次这一个值。
owner="drill-$(date +%Y%m%d%H%M%S)-$$-${RANDOM}${RANDOM}"
project="ragkb-drill-${owner#drill-}"
[[ "$owner" =~ ^drill-[0-9]{14}-[0-9]+-[0-9]+$ ]] || { echo '内部错误：owner 标识格式异常' >&2; exit 1; }
[[ "$project" =~ ^ragkb-drill- ]] || { echo '内部错误：项目名前缀异常' >&2; exit 1; }

work=$(mktemp -d "${TMPDIR:-/tmp}/ragkb-drill.${owner}.XXXXXX")
work=$(cd "$work" && pwd -P)
# 临时根目录也必须带 owner，且不在仓库里 —— 防止误把生产 DATA_DIR 当演练目录。
case "$work" in
  "$repo"|"$repo"/*) echo "临时目录落在仓库内，拒绝继续: $work" >&2; exit 1 ;;
esac
[[ "$(basename "$work")" == ragkb-drill.${owner}.* ]] || { echo '临时目录名不含 owner 标记' >&2; exit 1; }

pg_port=${DRILL_PG_PORT:-55999}
pg_user=drilluser
pg_db=drilldb
pg_password="drill-$(printf '%s' "$owner" | sha256sum | cut -c1-24)"

envfile="$work/drill.env"
mkdir -p "$work/uploads/kb-a" "$work/backups" "$work/restored"
# 演练自己的测试文件（不是生产文件）：两个 KB 目录 + 一个空文件 + 一个子目录。
printf 'alpha-document' > "$work/uploads/kb-a/doc-alpha.txt"
printf 'beta-document'  > "$work/uploads/kb-a/doc-beta.pdf"
mkdir -p "$work/uploads/kb-b/nested"
printf 'gamma' > "$work/uploads/kb-b/nested/doc-gamma.md"
: > "$work/uploads/kb-b/empty.bin"

cat > "$envfile" <<EOF
# 演练临时配置：只含本次随机测试凭据，不得复用到生产。
POSTGRES_USER=$pg_user
POSTGRES_PASSWORD=$pg_password
POSTGRES_DB=$pg_db
POSTGRES_PORT=$pg_port
DRILL_OWNER=$owner
DRILL_UPLOADS=$work/uploads
DRILL_BACKUPS=$work/backups
DRILL_RESTORED=$work/restored
EOF
chmod 600 "$envfile"

compose() { docker compose --env-file "$envfile" -p "$project" -f "$compose_file" "$@"; }

echo "=== 隔离恢复演练 ==="
echo "owner    : $owner"
echo "项目     : $project"
echo "临时目录 : $work"
echo "模式     : $mode$([[ $keep -eq 1 ]] && echo ' (保留现场)')"
echo

if [[ "$mode" != "isolated" ]]; then
  cat <<EOF
[dry-run] 将执行以下步骤（未做任何改动）：
  1. docker compose --env-file $envfile -p $project -f $compose_file up -d --wait db
  2. 写入 $rows 行测试数据与真实文件到 $work/uploads
  3. pg_dump 出 $work/backups/ragdb-drill.sql.gz，另存 uploads 的 sha256 清单
  4. 删除测试数据，psql 单事务恢复 dump
  5. 校验：行数与内容一致、每个文件 SHA-256 一致
  6. 清理：核对 owner 标签后 down -v、删除 $work
确认无误后加 --isolated 执行。
EOF
  # dry-run 不创建任何 Docker 资源，临时目录也不保留。
  rm -rf "$work"
  exit 0
fi

report="$work/report.txt"
: > "$report"
log() { echo "$*" | tee -a "$report"; }
fail() {
  log "FAIL: $*"
  echo "失败报告保留在: $report" >&2
  if [[ $keep -eq 1 ]]; then echo "现场保留: $work（项目 $project）" >&2; fi
  exit 1
}

# ---------- owner 守卫 ----------
# 只清理带本次 owner 标签、且项目名匹配的资源；任何不一致都保留现场。
verify_owned() {
  local what=$1 name=$2 label
  # 容器的标签在 .Config.Labels，卷/网络在 .Labels；按类型取，取不到一律视为无标签。
  case "$what" in
    容器) label=$(docker inspect --format '{{ index .Config.Labels "ragkb.drill.owner" }}' "$name" 2>/dev/null || true) ;;
    *)    label=$(docker inspect --format '{{ index .Labels "ragkb.drill.owner" }}' "$name" 2>/dev/null || true) ;;
  esac
  [[ "$label" == "$owner" ]] || fail "$what $name 的 owner 标签不是 $owner（实际: ${label:-<无>}），拒绝清理"
}

# 列出属于本项目的某类资源（用 compose 项目标签，能覆盖 compose 自动创建、
# 但可能没带 ragkb.drill.owner 的资源）。
project_ids() { # containers|volumes|networks
  case "$1" in
    containers) docker ps -aq --filter "label=com.docker.compose.project=$project" ;;
    volumes)    docker volume ls -q --filter "label=com.docker.compose.project=$project" ;;
    networks)   docker network ls -q --filter "label=com.docker.compose.project=$project" ;;
  esac
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ $keep -eq 1 ]]; then
    log "按 --keep 保留现场（项目 $project，目录 $work）"
    exit "$status"
  fi
  log "清理：逐个核对 project=$project 的 owner 后删除"
  # 不使用 `compose down -v`：它是按项目名批量删除，不会核对 owner，
  # 一旦项目名撞上别人的资源就会连别人的卷/网络一起删。这里先列举、逐项核对、
  # 逐项删除；任何一项 owner 不符就停手并保留现场。
  local id v n
  for id in $(project_ids containers); do
    verify_owned 容器 "$id"
    docker rm -f "$id" >/dev/null || fail "无法删除容器 $id"
  done
  for v in $(project_ids volumes); do
    verify_owned 卷 "$v"
    docker volume rm "$v" >/dev/null || fail "无法删除卷 $v"
  done
  for n in $(project_ids networks); do
    verify_owned 网络 "$n"
    docker network rm "$n" >/dev/null || fail "无法删除网络 $n"
  done
  # 删完再确认没有残留；有残留说明有 owner 不符或删除失败，保留现场供排查。
  local left=''
  for kind in containers volumes networks; do
    left+=$(project_ids "$kind")
  done
  [[ -z "$left" ]] || fail "仍有未清理的项目资源，保留现场: $work"
  # 先记日志再删目录，否则 tee 会写已删除的报告文件。
  echo "清理完成"
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT

# 项目名冲突检查：不允许复用已有项目（避免连到别人的库/卷/网络）。
for kind in containers volumes networks; do
  if project_ids "$kind" | grep -q .; then
    fail "项目 $project 已存在同类资源（$kind），拒绝复用"
  fi
done

# ---------- 1. 拉起隔离数据库 ----------
log "启动隔离数据库（端口 127.0.0.1:$pg_port）"
compose up -d --wait db >>"$report" 2>&1 || fail "隔离数据库启动失败"

for id in $(docker ps -q --filter "label=com.docker.compose.project=$project"); do
  got=$(docker inspect --format '{{ index .Config.Labels "ragkb.drill.owner" }}' "$id")
  [[ "$got" == "$owner" ]] || fail "容器 $id 未带本次 owner 标签"
done

# ---------- 2. 写入测试数据 ----------
log "写入 $rows 行测试数据"
sql() { compose exec -T db psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 "$@"; }
sql -q <<SQL >>"$report" 2>&1
CREATE TABLE drill_rows (
  id          int PRIMARY KEY,
  filename    text NOT NULL,
  content_sha text NOT NULL,
  note        text
);
INSERT INTO drill_rows (id, filename, content_sha, note)
SELECT g,
       'doc-' || g || '.txt',
       md5('payload-' || g),
       CASE WHEN g % 5 = 0 THEN 'edge-中文-' || g ELSE NULL END
FROM generate_series(1, $rows) g;
CREATE TABLE drill_marker (k text PRIMARY KEY, v text NOT NULL);
INSERT INTO drill_marker VALUES ('owner', '$owner'), ('rows', '$rows');
SQL
[[ "$(sql -tAc 'SELECT count(*) FROM drill_rows')" == "$rows" ]] || fail "测试数据行数不符"

# 上传文件清单（演练自己的文件，不入生产目录）
(cd "$work/uploads" && find . -type f -print0 | sort -z | xargs -0 sha256sum > "$work/uploads.sha256")
file_count=$(wc -l < "$work/uploads.sha256")
log "上传文件 $file_count 个"

# ---------- 3. 真实备份 ----------
log "pg_dump"
compose exec -T db pg_dump -U "$pg_user" -d "$pg_db" --clean --if-exists \
  | gzip -6 > "$work/backups/ragdb-drill.sql.gz" || fail "pg_dump 失败"
gzip -t "$work/backups/ragdb-drill.sql.gz" || fail "dump 压缩包损坏"
gunzip -c "$work/backups/ragdb-drill.sql.gz" | grep -q 'PostgreSQL database dump' || fail "dump 缺少 pg_dump 标识"
tar -czf "$work/backups/uploads-drill.tar.gz" -C "$work" uploads || fail "打包上传文件失败"
cp "$work/uploads.sha256" "$work/backups/uploads-drill.sha256"

# 记录原始内容的独立指纹，恢复后与 dump 无关地对比。
sql -tAc 'SELECT id, filename, content_sha, coalesce(note, $$<null>$$) FROM drill_rows ORDER BY id' \
  > "$work/original_rows.txt" || fail "无法导出原始行"
[[ -s "$work/original_rows.txt" ]] || fail "原始行为空"

# ---------- 4. 破坏后真实恢复 ----------
log "删除数据并单事务恢复"
sql -q -c 'DROP TABLE drill_rows; DROP TABLE drill_marker;' >>"$report" 2>&1 || fail "删除失败"
[[ "$(sql -tAc "SELECT to_regclass('public.drill_rows') IS NULL")" == "t" ]] || fail "数据未被删除，恢复验证无意义"

gunzip -c "$work/backups/ragdb-drill.sql.gz" \
  | compose exec -T db psql -U "$pg_user" -d "$pg_db" --single-transaction -v ON_ERROR_STOP=1 \
  >>"$report" 2>&1 || fail "psql 恢复失败"

sql -tAc 'SELECT id, filename, content_sha, coalesce(note, $$<null>$$) FROM drill_rows ORDER BY id' \
  > "$work/restored_rows.txt" || fail "无法导出恢复后行"
if ! diff -u "$work/original_rows.txt" "$work/restored_rows.txt" >>"$report" 2>&1; then
  fail "恢复后数据行与备份前不一致"
fi
[[ "$(sql -tAc "SELECT v FROM drill_marker WHERE k='owner'")" == "$owner" ]] || fail "marker 表内容不一致"
log "数据库行一致性通过（$rows 行逐列比对）"

# ---------- 5. 文件恢复与 SHA 校验 ----------
tar -xzf "$work/backups/uploads-drill.tar.gz" -C "$work/restored" || fail "解包失败"
# 清单里的路径相对 uploads 根（备份时就是 cd 到 uploads 生成的），校验也在同一根下。
(cd "$work/restored/uploads" && sha256sum --check "$work/backups/uploads-drill.sha256" --quiet) >>"$report" 2>&1 \
  || fail "恢复后上传文件 SHA-256 与备份前不一致"
log "上传文件 SHA-256 一致性通过（$file_count 个）"

# 负向校验：改一个字节必须被发现，证明校验不是空跑。
printf 'x' >> "$work/restored/uploads/kb-a/doc-alpha.txt"
if (cd "$work/restored/uploads" && sha256sum --check "$work/backups/uploads-drill.sha256" --quiet) >/dev/null 2>&1; then
  fail "篡改后校验仍然通过，SHA 校验无效"
fi
log "篡改检测有效"

log "演练通过：真实 pg_dump/psql 恢复 + 行内容 + 文件 SHA-256 全部一致"
