#!/usr/bin/env bash
# 隔离恢复演练：在独立的临时 Compose 项目里做**真实的 pg_dump + psql 恢复**，
# 再核对数据库行、上传文件与项目内容寻址 blob 的 SHA-256。不接触生产 DATA_DIR、
# 不停止生产服务、不向任何真实 remote 写入。
#
# 结果只算 **local-synthetic**：全部输入是本脚本生成的合成数据（25 个普通行、
# 4 个项目 blob 引用、4 个上传文件、4 个 project blob）。通过本演练**不**自动
# 证明生产备份可恢复、production RTO/RPO 或应用层项目来源预览；真实业务恢复
# 仍走 deploy/restore.sh 并需另行验收。
#
# 安全边界：
#   - 默认 dry-run：只打印将要创建/删除的资源和命令，不做任何改动。
#   - 必须显式 --isolated 才执行；执行前校验 owner 标记与项目名前缀。
#   - 生成临时 env（只含测试凭据），通过 --env-file 显式传入，绝不 source 真实 .env。
#   - 不加载仓库根 docker-compose.yml（它 env_file backend/.env 指向真实配置），
#     演练使用自带的 deploy/tests/drill-compose.yml。
#   - 备份三件套（db/uploads/projects）与清单在 DROP 原始数据前先经
#     drill-backup-check.sh 校验；缺失、损坏、清单缺项/重复/外来条目都拒绝。
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

backup_check="$repo/deploy/tests/drill-backup-check.sh"
[[ -f "$backup_check" ]] || { echo '缺少 drill-backup-check.sh，无法校验备份三件套' >&2; exit 1; }

pg_port=${DRILL_PG_PORT:-55999}
pg_user=drilluser
pg_db=drilldb
pg_password="drill-$(printf '%s' "$owner" | sha256sum | cut -c1-24)"

envfile="$work/drill.env"
mkdir -p "$work/uploads/kb-a" "$work/backups" "$work/restored" "$work/projects" "$work/blobs"
# 演练自己的测试文件（不是生产文件）：两个 KB 目录 + 一个空文件 + 一个子目录。
printf 'alpha-document' > "$work/uploads/kb-a/doc-alpha.txt"
printf 'beta-document'  > "$work/uploads/kb-a/doc-beta.pdf"
mkdir -p "$work/uploads/kb-b/nested"
printf 'gamma' > "$work/uploads/kb-b/nested/doc-gamma.md"
: > "$work/uploads/kb-b/empty.bin"

# ---------- 合成 project blobs（内容寻址：平铺 64 位 sha256 文件名） ----------
# 4 个 blob 覆盖：UTF8 文本、二进制、空文件、另一段不同内容。
# 文件名 = 内容 sha256；引用由 drill_project_blobs 表保存（sha/size/固定标签）。
blob_hex() { LC_ALL=C printf "$1" | sha256sum | cut -d' ' -f1; }
declare -a blob_payloads=(
  '\xe9\xa1\xb9\xe7\x9b\xae\xe5\x86\x85\xe5\xae\xb9blob-\xe6\xb5\x8b\xe8\xaf\x95\n'
  '\x00\x01\x02\xfe\xffbinary\npayload'
  ''
  'another-project-blob-content'
)
declare -a blob_labels=(utf8-text binary-payload empty-blob plain-text)
declare -a blob_shas=()
blob_size() { LC_ALL=C printf "$1" | wc -c; }
for i in 0 1 2 3; do
  payload=${blob_payloads[$i]}
  sha=$(blob_hex "$payload")
  printf '%b' "$payload" > "$work/blobs/$sha"
  blob_shas+=("$sha")
done
# 内容寻址不变式：文件名就是内容 sha256（篡改一个 blob 等于出现“孤儿文件名”）。
for sha in "${blob_shas[@]}"; do
  [[ "$(sha256sum "$work/blobs/$sha" | cut -d' ' -f1)" == "$sha" ]] \
    || { echo "内部错误：blob $sha 内容与文件名不符" >&2; exit 1; }
done
blob_count=${#blob_shas[@]}

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
  2. 写入 $rows 行测试数据、4 条项目 blob 引用与真实文件到 $work/uploads、$work/blobs
  3. pg_dump 出 $work/backups/ragdb-drill.sql.gz；uploads/projects 各打包一个 tar.gz
  4. drill-backup-check 校验三件套 + manifest-drill.sha256（恰好 3 项）
  5. 删除测试数据，psql 单事务恢复 dump
  6. 恢复到全新空 $work/restored：行内容逐列比对、每文件 SHA-256、blob 键/字节一致
  7. 分别篡改恢复后的 uploads 文件与 project blob，两种检测都必须失败
  8. 清理：核对 owner 标签后逐项删除容器/卷/网络、删除 $work
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
log "写入 $rows 行普通测试数据（另有 $blob_count 条项目 blob 引用与 $blob_count 个内容寻址 blob）"
sql() { compose exec -T db psql -U "$pg_user" -d "$pg_db" -v ON_ERROR_STOP=1 "$@"; }
# drill_project_blobs 的 VALUES 由 blob_shas/labels 生成；sha 是 64 位十六进制、
# label 是脚本内固定常量，均不含引号，可以直接内插。
blob_values=''
for i in "${!blob_shas[@]}"; do
  sha=${blob_shas[$i]}
  size=$(blob_size "${blob_payloads[$i]}")
  [[ -n "$blob_values" ]] && blob_values+=$'\n'
  blob_values+="  ('$sha', $size, '${blob_labels[$i]}'),"
done
blob_values=${blob_values%,}
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
CREATE TABLE drill_project_blobs (
  blob_sha  char(64) PRIMARY KEY,
  byte_size int NOT NULL,
  label     text NOT NULL
);
INSERT INTO drill_project_blobs (blob_sha, byte_size, label) VALUES
$blob_values;
SQL
[[ "$(sql -tAc 'SELECT count(*) FROM drill_rows')" == "$rows" ]] || fail "测试数据行数不符"
[[ "$(sql -tAc 'SELECT count(*) FROM drill_project_blobs')" == "$blob_count" ]] \
  || fail "项目 blob 引用行数不符"

# 上传文件清单（演练自己的文件，不入生产目录）
(cd "$work/uploads" && find . -type f -print0 | sort -z | xargs -0 sha256sum > "$work/uploads.sha256")
file_count=$(wc -l < "$work/uploads.sha256")
log "合成数据：$rows 个普通行、$blob_count 条项目 blob 引用、$file_count 个上传文件、$blob_count 个 project blob"

# 数据库 blob 引用指纹（与 dump 无关，恢复后逐列比对）。
sql -tAc 'SELECT blob_sha, byte_size, label FROM drill_project_blobs ORDER BY blob_sha' \
  > "$work/original_blobs.txt" || fail "无法导出 blob 引用"
[[ -s "$work/original_blobs.txt" ]] || fail "blob 引用行为空"

# ---------- 3. 真实备份（三件套 + 清单） ----------
log "pg_dump"
compose exec -T db pg_dump -U "$pg_user" -d "$pg_db" --clean --if-exists \
  | gzip -6 > "$work/backups/ragdb-drill.sql.gz" || fail "pg_dump 失败"
gzip -t "$work/backups/ragdb-drill.sql.gz" || fail "dump 压缩包损坏"
# 读完整个流数标识行（grep -q 会在首个匹配后提前退出，gunzip 收到 SIGPIPE，
# pipefail 会把完好的大 dump 误判成损坏）；grep 无匹配或真失败都按 0 拒绝。
dump_markers=$(gunzip -c "$work/backups/ragdb-drill.sql.gz" | grep -c 'PostgreSQL database dump') || dump_markers=0
[[ "$dump_markers" =~ ^[0-9]+$ && $dump_markers -gt 0 ]] || fail "dump 缺少 pg_dump 标识"
tar -czf "$work/backups/uploads-drill.tar.gz" -C "$work" uploads || fail "打包上传文件失败"
# 项目存储与生产 backup.sh 同构：${DATA_DIR}/projects 整目录打包，顶层 projects/。
mkdir -p "$work/projects"
for sha in "${blob_shas[@]}"; do
  cp "$work/blobs/$sha" "$work/projects/$sha"
done
tar -czf "$work/backups/projects-drill.tar.gz" -C "$work" projects || fail "打包项目存储失败"
# 三件产物采用同一固定 drill 文件名；清单恰好 3 行且最后生成，表示这一组完整。
(cd "$work/backups" && sha256sum ragdb-drill.sql.gz uploads-drill.tar.gz projects-drill.tar.gz \
  > manifest-drill.sha256) || fail "生成 manifest-drill.sha256 失败"
[[ "$(wc -l < "$work/backups/manifest-drill.sha256")" -eq 3 ]] \
  || fail "manifest-drill.sha256 必须恰好 3 项"

# 记录原始内容的独立指纹，恢复后与 dump 无关地对比。
sql -tAc 'SELECT id, filename, content_sha, coalesce(note, $$<null>$$) FROM drill_rows ORDER BY id' \
  > "$work/original_rows.txt" || fail "无法导出原始行"
[[ -s "$work/original_rows.txt" ]] || fail "原始行为空"

# ---------- 4. 备份三件套校验（在 DROP 原始数据之前） ----------
log "drill-backup-check 校验三件套"
bash "$backup_check" "$work/backups" >>"$report" 2>&1 \
  || fail "备份三件套校验未通过（缺失/损坏/清单缺项/重复/外来条目）"
log "三件套校验通过：ragdb-drill.sql.gz + uploads-drill.tar.gz + projects-drill.tar.gz（清单恰好 3 项）"

# ---------- 5. 破坏后真实恢复 ----------
log "删除数据并单事务恢复"
sql -q -c 'DROP TABLE drill_rows; DROP TABLE drill_marker; DROP TABLE drill_project_blobs;' \
  >>"$report" 2>&1 || fail "删除失败"
[[ "$(sql -tAc "SELECT to_regclass('public.drill_rows') IS NULL")" == "t" ]] || fail "数据未被删除，恢复验证无意义"
[[ "$(sql -tAc "SELECT to_regclass('public.drill_project_blobs') IS NULL")" == "t" ]] \
  || fail "drill_project_blobs 未被删除，恢复验证无意义"

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

# ---------- 6. 文件恢复（全新空 restored 目录，必须在核 SQL 引用之前解包） ----------
# restored 在脚本开头创建后一直是空目录；解包前再确认一次，保证恢复结果只来自备份。
[[ -z "$(ls -A "$work/restored")" ]] || fail "restored 目录不为空，恢复验证无意义"
tar -xzf "$work/backups/uploads-drill.tar.gz" -C "$work/restored" || fail "uploads 解包失败"
tar -xzf "$work/backups/projects-drill.tar.gz" -C "$work/restored" || fail "projects 解包失败"

# ---------- 7. 项目 blob 引用恢复校验（此时恢复目录里已有真实 blob 文件） ----------
sql -tAc 'SELECT blob_sha, byte_size, label FROM drill_project_blobs ORDER BY blob_sha' \
  > "$work/restored_blobs.txt" || fail "无法导出恢复后 blob 引用"
if ! diff -u "$work/original_blobs.txt" "$work/restored_blobs.txt" >>"$report" 2>&1; then
  fail "恢复后 drill_project_blobs 与备份前不一致"
fi
# SQL 记录的 sha/size 必须与真实恢复出的 blob 文件一致（独立于引用行比对）。
sql -tAc 'SELECT blob_sha, byte_size FROM drill_project_blobs ORDER BY blob_sha' \
  > "$work/restored_blob_refs.txt" || fail "无法导出恢复后 blob sha/size"
[[ "$(wc -l < "$work/restored_blob_refs.txt")" -eq "$blob_count" ]] \
  || fail "恢复后 blob sha/size 行数不符"
while IFS='|' read -r sha sz; do
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || fail "恢复后 blob sha 格式异常: $sha"
  [[ "$sz" =~ ^[0-9]+$ ]] || fail "恢复后 blob size 格式异常: $sz"
  f="$work/restored/projects/$sha"
  [[ -f "$f" ]] || fail "SQL 引用的 blob $sha 在恢复目录中不存在"
  [[ "$(sha256sum "$f" | cut -d' ' -f1)" == "$sha" ]] \
    || fail "恢复 blob $sha 的内容 sha256 与 SQL 记录不符"
  [[ "$(wc -c < "$f")" == "$sz" ]] \
    || fail "恢复 blob $sha 的字节数与 SQL 记录不符"
done < "$work/restored_blob_refs.txt"
log "项目 blob 引用一致性通过（$blob_count 条 SQL 引用逐列比对）"

# ---------- 8. 文件 SHA 校验（每文件 SHA/数量与源一致） ----------
# 源 uploads/projects 仍保留，用于独立复核。
src_up=$(cd "$work/uploads" && find . -type f -print0 | sort -z | xargs -0 sha256sum)
rst_up=$(cd "$work/restored/uploads" && find . -type f -print0 | sort -z | xargs -0 sha256sum)
[[ "$src_up" == "$rst_up" ]] || fail "恢复后 uploads 文件与源不一致"
[[ $(wc -l <<<"$rst_up") -eq "$file_count" ]] || fail "恢复后 uploads 文件数量不符"

src_prj=$(cd "$work/projects" && find . -type f -printf '%P\0' | LC_ALL=C sort -z | xargs -0 -I{} sha256sum {} | LC_ALL=C sort)
rst_prj=$(cd "$work/restored/projects" && find . -type f -printf '%P\0' | LC_ALL=C sort -z | xargs -0 -I{} sha256sum {} | LC_ALL=C sort)
[[ "$src_prj" == "$rst_prj" ]] || fail "恢复后 projects blob 与源不一致"
[[ $(wc -l <<<"$rst_prj") -eq "$blob_count" ]] || fail "恢复后 projects blob 数量不符"
# blob 键=内容 sha256、字节数与源一致（内容寻址不变式逐个复核）。
for sha in "${blob_shas[@]}"; do
  [[ -f "$work/restored/projects/$sha" ]] || fail "恢复目录缺少 blob $sha"
  [[ "$(sha256sum "$work/restored/projects/$sha" | cut -d' ' -f1)" == "$sha" ]] \
    || fail "恢复 blob $sha 键与内容不符"
  cmp -s "$work/blobs/$sha" "$work/restored/projects/$sha" \
    || fail "恢复 blob $sha 字节内容与源不符"
done
# 清单里的路径相对 uploads 根（备份时就是 cd 到 uploads 生成的），校验也在同一根下。
(cd "$work/restored/uploads" && sha256sum --check "$work/uploads.sha256" --quiet) >>"$report" 2>&1 \
  || fail "恢复后上传文件 SHA-256 与备份前不一致"
log "uploads/projects 恢复一致性通过（$file_count 个上传文件 + $blob_count 个 blob）"

# ---------- 9. 两种篡改负向校验（只动恢复副本，源与备份保持原样） ----------
# 8a. uploads：追加一个字节后清单校验必须失败。
printf 'x' >> "$work/restored/uploads/kb-a/doc-alpha.txt"
if (cd "$work/restored/uploads" && sha256sum --check "$work/uploads.sha256" --quiet) >/dev/null 2>&1; then
  fail "uploads 篡改后校验仍然通过，SHA 校验无效"
fi
log "uploads 篡改检测有效（恢复副本追加字节被拒绝，源与备份未动）"

# 8b. project blob：追加一个字节后内容寻址校验必须失败。
tampered_blob=${blob_shas[0]}
printf 'x' >> "$work/restored/projects/$tampered_blob"
if [[ "$(sha256sum "$work/restored/projects/$tampered_blob" | cut -d' ' -f1)" == "$tampered_blob" ]]; then
  fail "project blob 篡改后内容寻址校验仍然通过"
fi
if cmp -s "$work/blobs/$tampered_blob" "$work/restored/projects/$tampered_blob"; then
  fail "project blob 篡改未被检测到"
fi
if bash "$backup_check" "$work/backups" >>"$report" 2>&1; then
  : # 篡改只发生在恢复副本；源备份目录经同一个 full CLI 复核仍须独立通过。
else
  fail "备份三件套校验不应受恢复副本篡改影响"
fi
log "project blob 篡改检测有效（$tampered_blob 追加字节后键/内容不符，备份归档校验仍独立通过）"

log "演练通过（local-synthetic）：真实 pg_dump/psql 恢复 + 行内容 + uploads/projects SHA + blob 引用 + 两种篡改负向全部一致"
log "注意：本演练全部输入为合成数据，结论只覆盖 local-synthetic；不证明 production 恢复、RTO/RPO 或应用层项目来源预览。"
