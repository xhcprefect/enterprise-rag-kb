#!/usr/bin/env bash
# 备份数据库与上传文件，按天归档并自动清理过期备份。
#
# 用法：  ./deploy/backup.sh
# 定时：  crontab -e 加一行
#         20 3 * * * /path/to/enterprise-rag-kb/deploy/backup.sh >> /var/log/rag-backup.log 2>&1
#
# 竞价实例上建议把 BACKUP_DIR 指向对象存储的挂载点，或再加一步 rsync 到异地。
set -euo pipefail

cd "$(dirname "$0")/.."

set -a; source <(grep -E '^[A-Z_]+=' .env | sed 's/[[:space:]]*$//'); set +a
DATA_DIR=${DATA_DIR:-./data}
BACKUP_DIR=${BACKUP_DIR:-$DATA_DIR/backups}
KEEP_DAYS=${KEEP_DAYS:-7}
COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.yml}

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)
db_file="$BACKUP_DIR/ragdb-$stamp.sql.gz"
files_file="$BACKUP_DIR/uploads-$stamp.tar.gz"

# pg_dump 在容器里执行（容器内有匹配版本的客户端），输出直接压缩到宿主机。
# pipefail 已在开头开启：pg_dump 失败会原样传出来，不会被 gzip 掩盖。
docker compose -f "$COMPOSE_FILE" exec -T db \
  pg_dump -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-ragdb}" --clean --if-exists \
  | gzip -9 > "$db_file"

tar -czf "$files_file" -C "$DATA_DIR" uploads

# 校验产物可用，避免磁盘满/容器挂掉时留下"看起来成功"的坏备份。
# 注意光看文件大小不够：pg_dump 失败时 gzip 仍会写出一个 20 字节的空流，
# 必须实际解压 + 检查 SQL 内容才算数。
if ! gzip -t "$db_file" 2>/dev/null; then
  echo "备份失败：$db_file 不是有效的 gzip" >&2
  rm -f "$db_file" "$files_file"
  exit 1
fi
# 用 grep -c 而不是 -q：-q 命中后会立刻关闭管道，gunzip 收到 SIGPIPE 报错，
# 在 pipefail 下会让整个条件判为失败（明明备份是好的）。
if [[ $(gunzip -c "$db_file" | grep -c "PostgreSQL database dump") -eq 0 ]]; then
  echo "备份失败：$db_file 里没有 pg_dump 内容" >&2
  rm -f "$db_file" "$files_file"
  exit 1
fi
if ! tar -tzf "$files_file" >/dev/null 2>&1; then
  echo "备份失败：$files_file 不是有效的 tar.gz" >&2
  rm -f "$db_file" "$files_file"
  exit 1
fi

# 清理过期备份（只删自己生成的文件名，避免误删）
find "$BACKUP_DIR" -maxdepth 1 -type f \
  \( -name 'ragdb-*.sql.gz' -o -name 'uploads-*.tar.gz' \) \
  -mtime "+$KEEP_DAYS" -delete

echo "$(date '+%F %T') 备份完成: $(du -h "$db_file" | cut -f1) + $(du -h "$files_file" | cut -f1) -> $BACKUP_DIR"
