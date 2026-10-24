#!/usr/bin/env bash
# 从备份恢复。会覆盖当前数据库与上传文件，执行前有确认提示。
#
# 用法：./deploy/restore.sh <ragdb-YYYYmmdd-HHMMSS.sql.gz> [uploads-YYYYmmdd-HHMMSS.tar.gz]
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
  echo "用法: $0 <ragdb-*.sql.gz> [uploads-*.tar.gz]" >&2
  exit 1
fi

DB_BACKUP=$1
FILES_BACKUP=${2:-}

[[ -f "$DB_BACKUP" ]] || { echo "找不到文件: $DB_BACKUP" >&2; exit 1; }
[[ -z "$FILES_BACKUP" || -f "$FILES_BACKUP" ]] || { echo "找不到文件: $FILES_BACKUP" >&2; exit 1; }

set -a; source <(grep -E '^[A-Z_]+=' .env | sed 's/[[:space:]]*$//'); set +a
DATA_DIR=${DATA_DIR:-./data}

echo "即将恢复："
echo "  数据库      <- $DB_BACKUP"
[[ -n "$FILES_BACKUP" ]] && echo "  上传文件    <- $FILES_BACKUP"
echo "这会覆盖当前的数据库与上传目录。"
read -r -p "输入 yes 继续: " reply
[[ "$reply" == "yes" ]] || { echo "已取消"; exit 1; }

# 恢复期间停掉写库的进程，避免并发写入
docker compose stop api worker

gunzip -c "$DB_BACKUP" | docker compose exec -T db \
  psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-ragdb}" -v ON_ERROR_STOP=1

if [[ -n "$FILES_BACKUP" ]]; then
  rm -rf "${DATA_DIR:?}/uploads"
  tar -xzf "$FILES_BACKUP" -C "$DATA_DIR"
fi

docker compose start api worker
echo "恢复完成；用 curl http://localhost:"\
"$(grep -E '^WEB_PORT=' .env | tail -1 | cut -d= -f2-)" "/health 检查"
