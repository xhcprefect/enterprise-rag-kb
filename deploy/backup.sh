#!/usr/bin/env bash
# 一致性备份：维护锁 -> 停止写进程 -> 数据库与文件 -> 校验 -> 发布清单 -> 恢复服务 -> 可选异地上传。
#
# 产物（同一时间标识 stamp）：
#   ragdb-<stamp>.sql.gz        PostgreSQL 逻辑备份
#   uploads-<stamp>.tar.gz      上传目录（顶层 uploads/）
#   projects-<stamp>.tar.gz     项目 blob 存储（顶层 projects/）
#   manifest-<stamp>.sha256     上述三项的 sha256 清单（最后发布，表示完整）
#
# 兼容性：旧备份只有 ragdb + uploads，restore.sh 仍能恢复；新备份多一项 projects。
# projects 目录不存在（老部署尚未启用项目索引）时，归档内仍有空的 projects/ 目录，
# 这样清单格式保持新格式、恢复行为一致（恢复会创建空目录而不是删掉现有数据）。
set -euo pipefail
source "$(dirname "$0")/common.sh"
KEEP_DAYS=${KEEP_DAYS:-7}
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || { echo 'KEEP_DAYS 必须是非负整数' >&2; exit 1; }
stamp=$(date +%Y%m%d-%H%M%S)-$$
work=$(mktemp -d "$BACKUP_DIR/.pending.XXXXXX")
db_name="${DB_PREFIX}${stamp}.sql.gz"
files_name="${UPLOADS_PREFIX}${stamp}.tar.gz"
projects_name="${PROJECTS_PREFIX}${stamp}.tar.gz"
manifest="manifest-$stamp.sha256"
published=0
remote_status=0
cleanup() {
  local status=$?
  trap - EXIT
  if [[ $published -eq 0 ]]; then
    rm -f "$BACKUP_DIR/$db_name" "$BACKUP_DIR/$files_name" \
          "$BACKUP_DIR/$projects_name" "$BACKUP_DIR/$manifest"
  fi
  rm -rf "$work"
  if ! start_writers; then
    echo '备份结束但服务恢复失败，请检查 docker compose ps' >&2
    status=1
  fi
  # 异地上传失败时本地备份已发布且保留；这里只把退出码置为非零，不删本地文件。
  if [[ $remote_status -ne 0 && $status -eq 0 ]]; then
    status=$remote_status
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

stop_writers
docker compose -f "$COMPOSE_FILE" exec -T db \
  pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-ragdb}" --clean --if-exists \
  | gzip -6 > "$work/$db_name"

# 上传文件与项目 blob 都在宿主机 ${DATA_DIR} 下，按顶层目录打包。
mkdir -p "$DATA_DIR/projects"
tar -czf "$work/$files_name" -C "$DATA_DIR" uploads
tar -czf "$work/$projects_name" -C "$DATA_DIR" projects

validate_database_backup "$work/$db_name"
validate_upload_archive "$work/$files_name"
validate_projects_archive "$work/$projects_name"
archive_has_root "$work/$projects_name" projects || { echo '项目存储归档为空' >&2; exit 1; }
(cd "$work" && sha256sum "$db_name" "$files_name" "$projects_name" > "$manifest")
mv "$work/$db_name" "$BACKUP_DIR/$db_name"
mv "$work/$files_name" "$BACKUP_DIR/$files_name"
mv "$work/$projects_name" "$BACKUP_DIR/$projects_name"
# 清单最后发布，标识这组产物完整且经过校验。
mv "$work/$manifest" "$BACKUP_DIR/$manifest"
published=1
start_writers

# 可选异地备份：服务恢复后再传，避免上传耗时占用停写窗口。
# 未启用时 remote-backup.sh 直接返回 0；启用但失败时本地备份保留、脚本非零退出。
if [[ "${RAGKB_REMOTE_BACKUP_ENABLED:-false}" == "true" ]]; then
  if ! RAGKB_MAINTENANCE_LOCK_HELD=1 "$(dirname "$0")/remote-backup.sh" "$BACKUP_DIR/$manifest"; then
    echo '异地备份失败：本地备份已保留，请检查 remote-backup 输出' >&2
    remote_status=1
  fi
fi

find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'ragdb-*.sql.gz' -o -name 'uploads-*.tar.gz' \
     -o -name 'projects-*.tar.gz' -o -name 'manifest-*.sha256' \) \
  -mtime "+$KEEP_DAYS" -delete
echo "备份完成: $BACKUP_DIR/$manifest（包含数据库、上传文件与项目存储校验值）"
