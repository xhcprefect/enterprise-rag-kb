#!/usr/bin/env bash
# 一致性备份：维护锁 -> 停止写进程 -> 数据库与文件 -> 校验 -> 发布清单 -> 恢复服务。
set -euo pipefail
source "$(dirname "$0")/common.sh"
KEEP_DAYS=${KEEP_DAYS:-7}
[[ "$KEEP_DAYS" =~ ^[0-9]+$ ]] || { echo 'KEEP_DAYS 必须是非负整数' >&2; exit 1; }
stamp=$(date +%Y%m%d-%H%M%S)-$$
work=$(mktemp -d "$BACKUP_DIR/.pending.XXXXXX")
db_name="ragdb-$stamp.sql.gz"
files_name="uploads-$stamp.tar.gz"
manifest="manifest-$stamp.sha256"
published=0
cleanup() {
  local status=$?
  trap - EXIT
  if [[ $published -eq 0 ]]; then
    rm -f "$BACKUP_DIR/$db_name" "$BACKUP_DIR/$files_name" "$BACKUP_DIR/$manifest"
  fi
  rm -rf "$work"
  if ! start_writers; then
    echo '备份结束但服务恢复失败，请检查 docker compose ps' >&2
    status=1
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
tar -czf "$work/$files_name" -C "$DATA_DIR" uploads
validate_database_backup "$work/$db_name"
validate_upload_archive "$work/$files_name"
(cd "$work" && sha256sum "$db_name" "$files_name" > "$manifest")
mv "$work/$db_name" "$BACKUP_DIR/$db_name"
mv "$work/$files_name" "$BACKUP_DIR/$files_name"
# 清单最后发布，标识这一对产物完整且经过校验。
mv "$work/$manifest" "$BACKUP_DIR/$manifest"
published=1
start_writers
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'ragdb-*.sql.gz' -o -name 'uploads-*.tar.gz' -o -name 'manifest-*.sha256' \) \
  -mtime "+$KEEP_DAYS" -delete
echo "备份完成: $BACKUP_DIR/$manifest（包含数据库与上传文件校验值）"
