#!/usr/bin/env bash
# 恢复前完整验证；文件先暂存，SQL 单事务恢复，失败时恢复原文件和原服务状态。
set -euo pipefail
[[ $# -ge 1 ]] || { echo "用法: $0 <ragdb-*.sql.gz> [uploads-*.tar.gz]" >&2; exit 1; }
# 在切换工作目录前把调用者的相对路径转成绝对路径。
DB_BACKUP=$(realpath "$1")
FILES_BACKUP=""
[[ -z "${2:-}" ]] || FILES_BACKUP=$(realpath "$2")
source "$(dirname "$0")/common.sh"
[[ -f "$DB_BACKUP" ]] || { echo '数据库备份不存在' >&2; exit 1; }
[[ -z "$FILES_BACKUP" || -f "$FILES_BACKUP" ]] || { echo '文件备份不存在' >&2; exit 1; }
validate_database_backup "$DB_BACKUP"
if [[ -n "$FILES_BACKUP" ]]; then
  validate_upload_archive "$FILES_BACKUP"
  stamp=$(basename "$DB_BACKUP" .sql.gz)
  stamp=${stamp#ragdb-}
  [[ $(basename "$FILES_BACKUP") == "uploads-$stamp.tar.gz" ]] || { echo '数据库与文件备份时间标识不匹配' >&2; exit 1; }
  manifest="$(dirname "$DB_BACKUP")/manifest-$stamp.sha256"
  if [[ -f "$manifest" ]]; then
    [[ $(dirname "$DB_BACKUP") == "$(dirname "$FILES_BACKUP")" ]] || { echo '请把成对备份放在同一目录' >&2; exit 1; }
    # 清单必须且只能包含本次成对产物，防止缺项或任意路径被当成校验成功。
    awk -v db="$(basename "$DB_BACKUP")" -v files="$(basename "$FILES_BACKUP")" '
      NF!=2 || length($1)!=64 || $1!~/^[a-f0-9]+$/ {bad=1}
      $2==db {d++; next}
      $2==files {f++; next}
      {bad=1}
      END {exit (bad || NR!=2 || d!=1 || f!=1)}' "$manifest"
    (cd "$(dirname "$manifest")" && sha256sum --check "$(basename "$manifest")")
  else
    echo '警告：这是无校验清单的旧备份，只能验证压缩包与路径，不能证明两份内容一致。' >&2
  fi
else
  echo '警告：只恢复数据库，现有源文件可能与数据库不匹配。建议提供同批 uploads 备份。' >&2
fi

echo "即将覆盖数据库: $DB_BACKUP"
[[ -z "$FILES_BACKUP" ]] || echo "即将覆盖上传目录: $FILES_BACKUP"
read -r -p '输入 yes 继续: ' reply
[[ "$reply" == yes ]] || { echo '已取消'; exit 1; }

stage=$(mktemp -d "$DATA_DIR/.restore-stage.XXXXXX")
previous="$stage/previous-uploads"
swapped=0
committed=0
cleanup() {
  local status=$?
  trap - EXIT
  if [[ $swapped -eq 1 && $committed -eq 0 ]]; then
    rm -rf "${DATA_DIR:?}/uploads"
    if [[ -d "$previous" ]]; then mv "$previous" "$DATA_DIR/uploads"; fi
    echo '数据库恢复失败：SQL 已回滚，原上传文件已恢复。' >&2
  fi
  rm -rf "$stage"
  if ! start_writers; then
    echo '服务启动失败，请人工检查 docker compose ps' >&2
    status=1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n "$FILES_BACKUP" ]]; then
  # 解压失败或空间不足发生在停止服务和覆盖数据之前。
  tar -xzf "$FILES_BACKUP" -C "$stage"
  [[ -d "$stage/uploads" ]] || { echo '备份缺少 uploads 目录' >&2; exit 1; }
  if [[ $(id -u) -eq 0 ]]; then chown -R 1000:1000 "$stage/uploads"; fi
fi
stop_writers
if [[ -n "$FILES_BACKUP" ]]; then
  if [[ -e "$DATA_DIR/uploads" ]]; then mv "$DATA_DIR/uploads" "$previous"; fi
  swapped=1
  mv "$stage/uploads" "$DATA_DIR/uploads"
fi
# pg_dump 不带 --create，支持在同一事务里恢复；任意 SQL 错误均回滚整个数据库变更。
gunzip -c "$DB_BACKUP" | docker compose -f "$COMPOSE_FILE" exec -T db \
  psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-ragdb}" \
  --single-transaction -v ON_ERROR_STOP=1
committed=1
start_writers
echo '恢复完成，请检查 /health 并抽查文档与问答。'
