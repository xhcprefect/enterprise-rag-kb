#!/usr/bin/env bash
# 由 backup.sh / restore.sh 引入。配置文件必须由管理员维护，不能接受外部输入。
set -euo pipefail
umask 077

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE=${ENV_FILE:-.env}
[[ -f "$ENV_FILE" ]] || { echo '缺少部署环境配置' >&2; exit 1; }
set -a; source <(grep -E '^[A-Z_]+=' "$ENV_FILE" | sed 's/[[:space:]]*$//'); set +a
DATA_DIR=${DATA_DIR:-./data}
BACKUP_DIR=${BACKUP_DIR:-$DATA_DIR/backups}
COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.yml}
mkdir -p "$DATA_DIR" "$BACKUP_DIR"
command -v flock >/dev/null || { echo '需要 flock（util-linux），拒绝无锁维护' >&2; exit 1; }
exec 9>"$DATA_DIR/.maintenance.lock"
flock -n 9 || { echo '另一个备份/恢复任务正在执行' >&2; exit 1; }

writers=()
writers_stopped=0
stop_writers() {
  local running
  running=$(docker compose -f "$COMPOSE_FILE" ps --status running --services api worker)
  if [[ -n "$running" ]]; then
    mapfile -t writers <<<"$running"
    # 在 stop 之前设置，确保部分停止失败时 EXIT trap 也会恢复原本运行的服务。
    writers_stopped=1
    docker compose -f "$COMPOSE_FILE" stop --timeout 120 "${writers[@]}"
  fi
}

start_writers() {
  if [[ $writers_stopped -eq 1 && ${#writers[@]} -gt 0 ]]; then
    docker compose -f "$COMPOSE_FILE" start "${writers[@]}"
    writers_stopped=0
  fi
}

validate_database_backup() {
  gzip -t "$1"
  [[ $(gunzip -c "$1" | grep -c 'PostgreSQL database dump') -gt 0 ]] || {
    echo '数据库备份缺少 pg_dump 标识' >&2; return 1;
  }
}

validate_upload_archive() {
  # 只接受 uploads 下的普通文件/目录，拒绝路径穿越、符号链接和硬链接。
  tar -tzf "$1" | awk '
    /^\// {bad=1}
    {n=split($0,a,"/"); if(a[1]!="uploads") bad=1; for(i=1;i<=n;i++) if(a[i]=="..") bad=1}
    END {exit bad}'
  tar -tvzf "$1" | awk 'substr($0,1,1)!="-" && substr($0,1,1)!="d" {bad=1} END {exit bad}'
}
