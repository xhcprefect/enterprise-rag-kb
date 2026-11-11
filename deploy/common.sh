#!/usr/bin/env bash
# 由 backup.sh / restore.sh / remote-backup.sh 引入。配置文件必须由管理员维护，不能接受外部输入。
set -euo pipefail
umask 077

cd "$(dirname "${BASH_SOURCE[0]}")/.."
ENV_FILE=${ENV_FILE:-.env}
[[ -f "$ENV_FILE" ]] || { echo '缺少部署环境配置' >&2; exit 1; }
set -a; source <(grep -E '^[A-Z_]+=' "$ENV_FILE" | sed 's/[[:space:]]*$//'); set +a
DATA_DIR=${DATA_DIR:-./data}
BACKUP_DIR=${BACKUP_DIR:-$DATA_DIR/backups}
COMPOSE_FILE=${COMPOSE_FILE:-docker-compose.yml}

# 维护停写预算：根 .env 的 WORKER_STOP_TIMEOUT_SECONDS 同时决定
#   1) worker 服务的 stop_grace_period（docker-compose.yml）
#   2) 备份/恢复时 docker stop --timeout 的等待时间
# 管理员可以配大（例如入库大文件需要更久），但必须是正整数。
# 注意：这只是“等待上限”。解析或模型流卡住时 Docker 仍会在到期后强杀 worker；
# worker 靠心跳/租约把未完成任务退回队列续传，不能承诺每个任务都在该秒数内优雅结束。
WORKER_STOP_TIMEOUT_SECONDS=${WORKER_STOP_TIMEOUT_SECONDS:-180}
[[ "$WORKER_STOP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,5}$ ]] || {
  echo 'WORKER_STOP_TIMEOUT_SECONDS 必须是 1-999999 的正整数（秒）' >&2; exit 1;
}
# API 不再接收新请求即可，短 SSE 流无需等满 worker 预算；到期后 Docker 强杀，
# 客户端重连即可。单独一个上限，避免为等长对话把整个维护窗口拖长。
API_STOP_TIMEOUT_SECONDS=${API_STOP_TIMEOUT_SECONDS:-30}
[[ "$API_STOP_TIMEOUT_SECONDS" =~ ^[1-9][0-9]{0,5}$ ]] || {
  echo 'API_STOP_TIMEOUT_SECONDS 必须是 1-999999 的正整数（秒）' >&2; exit 1;
}

mkdir -p "$DATA_DIR" "$BACKUP_DIR"
command -v flock >/dev/null || { echo '需要 flock（util-linux），拒绝无锁维护' >&2; exit 1; }
if [[ "${RAGKB_MAINTENANCE_LOCK_HELD:-0}" != "1" ]]; then
  exec 9>"$DATA_DIR/.maintenance.lock"
  flock -n 9 || { echo '另一个备份/恢复任务正在执行' >&2; exit 1; }
fi

# 备份产物：legacy 两件套 + 新 projects 归档。两种组合都必须能恢复：
#   - 旧备份：ragdb + uploads（无 projects 行）
#   - 新备份：ragdb + uploads + projects
DB_PREFIX='ragdb-'
UPLOADS_PREFIX='uploads-'
PROJECTS_PREFIX='projects-'
ARCHIVE_SUFFIX='.tar.gz'

writers=()
writers_stopped=0

# 会写数据库/上传目录/项目存储的服务。历史版本只有一个 `worker`，新版本按角色拆成
# `worker`（ingest）+ `worker-summary` + `worker-project` + `worker-regression`
# （见 docker-compose.yml）。
# 这里按**容器标签**识别，而不是 `docker compose config --services`：后者会隐藏
# profile 服务（worker-summary/worker-project/worker-regression），导致启用后维护时漏停。
WRITER_CANDIDATES=(api worker worker-summary worker-project worker-regression)

compose_project() {
  if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
    printf '%s' "$COMPOSE_PROJECT_NAME"
    return 0
  fi
  # 优先取 compose 配置的顶层 name；compose v2 也接受 `-p` 覆盖。
  # 必须显式捕获退出码：管道 + $(...) 会让调用方只看得到 sed/head 的成功，
  # docker 不可用/配置损坏时会被误当成“没有写服务”继续备份。
  local cfg name
  cfg=$(docker compose -f "$COMPOSE_FILE" config 2>/dev/null) \
    || { echo '无法读取 compose 项目配置（docker compose config 失败）' >&2; return 1; }
  name=$(printf '%s\n' "$cfg" \
    | sed -n 's/^name:[[:space:]]*\([^[:space:]]*\).*/\1/p' | head -1)
  printf '%s' "$name"
}

is_writer_service() { # service
  local svc=$1 candidate
  # API 必须精确匹配；worker/ingest 允许带前后缀（旧版本可能叫 ingest-worker）。
  [[ "$svc" == "api" ]] && return 0
  case "$svc" in
    *worker*|*ingest*) return 0 ;;
  esac
  for candidate in "${WRITER_CANDIDATES[@]}"; do
    [[ "$svc" == "$candidate" ]] && return 0
  done
  return 1
}

# 列出当前 compose 项目里**正在运行**的写服务容器（id + 服务名）。
# 输出每行：<container_id> <service>。legacy 旧 worker、profile 角色都会出现。
running_writers() {
  local project listing id svc
  project=$(compose_project) || return 1
  if [[ -z "$project" ]]; then
    echo '无法确定 compose 项目名，拒绝在未知状态下停写' >&2
    return 1
  fi
  # 显式捕获 docker ps 的退出码：`done < <(docker ps ...)` 里的进程替换不会把
  # 子进程失败传回 while/调用方，docker 不可用会被当成“没有写服务”继续备份。
  listing=$(docker ps --filter "label=com.docker.compose.project=$project" \
              --format '{{.ID}} {{.Label "com.docker.compose.service"}}') \
    || { echo '无法列出运行中的写服务容器，拒绝在未知状态下停写' >&2; return 1; }
  while read -r id svc; do
    [[ -n "$id" && -n "$svc" ]] || continue
    is_writer_service "$svc" || continue
    printf '%s %s\n' "$id" "$svc"
  done <<< "$listing"
}

warn_legacy_writers() { # 兼容旧接口：现在只提示“当前编排不认识但仍在写”的容器
  local project services svc
  project=$(compose_project) || return 0
  [[ -n "$project" ]] || return 0
  # 这里只是辅助提示；docker ps 失败时不要覆盖前面已成功的停写流程。
  services=$(docker ps --filter "label=com.docker.compose.project=$project" \
               --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null \
               | sort -u) || return 0
  while read -r svc; do
    [[ -n "$svc" ]] || continue
    is_writer_service "$svc" && continue
    case "$svc" in
      api|web|db|prometheus|alertmanager|grafana|otel-collector) continue ;;
      *worker*|*ingest*|*summary*|*project*) ;;
      *) continue ;;
    esac
    echo "警告：compose 项目 $project 里仍有当前编排未定义的写服务容器 $svc（可能来自旧版本）。" >&2
    echo "      它不在本次停写/恢复名单内；若它仍会写库或写盘，请先人工停止后再备份。" >&2
  done <<< "$services"
}

# 停写：逐个容器 `docker stop --timeout`。API 用短超时，worker 用 worker 预算。
# 直接按容器 ID 停（而不是 `compose stop <service>`）的原因：
#   - profile 角色在未启用 profile 时 compose 找不到服务；
#   - 旧版本遗留的 worker 容器可能不在当前 compose 文件里。
# 只停“原本正在运行”的容器，start_writers 之后只会重启这些容器。
stop_writers() {
  local id svc timeout line listing
  local -a lines=()
  # `mapfile < <(...)` 会吞掉进程替换的退出码；先取文本再拆分，确保 docker
  # 失败（或项目名未知）时 fail closed，而不是以空名单继续备份。
  listing=$(running_writers) || { echo '无法确定运行中的写服务，拒绝停写' >&2; return 1; }
  if [[ -n "$listing" ]]; then
    mapfile -t lines <<< "$listing"
    # 在 stop 之前设置，确保部分停止失败时 EXIT trap 也会恢复原本运行的服务。
    writers_stopped=1
    writers=()
  fi
  # 空数组在 `${arr[@]}` 下配合 set -u 在 bash 4.3+ 是合法的；不要加 `:-0`。
  for line in "${lines[@]}"; do
    [[ -n "$line" ]] || continue
    id=${line%% *}
    svc=${line#* }
    case "$svc" in
      api) timeout=$API_STOP_TIMEOUT_SECONDS ;;
      *)   timeout=$WORKER_STOP_TIMEOUT_SECONDS ;;
    esac
    # 记录失败的容器：即使 stop 失败，它仍在运行，恢复时也必须尝试 start。
    writers+=("$id")
    docker stop --timeout "$timeout" "$id" >/dev/null \
      || { echo "停止 $svc ($id) 失败" >&2; return 1; }
  done
  warn_legacy_writers
}

start_writers() {
  if [[ $writers_stopped -eq 1 && ${#writers[@]} -gt 0 ]]; then

    # 只重启原本在运行的服务，且逐个重启：某个容器已被删除时 start 会失败，
    # 不能因此中断恢复；失败时累计后统一返回非零。
    local id status=0
    for id in "${writers[@]}"; do
      docker start "$id" >/dev/null \
        || { echo "恢复容器失败: $id" >&2; status=1; }
    done
    writers=()
    writers_stopped=0
    return $status
  fi
}

validate_database_backup() {
  gzip -t "$1"
  [[ $(gunzip -c "$1" | grep -c 'PostgreSQL database dump') -gt 0 ]] || {
    echo '数据库备份缺少 pg_dump 标识' >&2; return 1;
  }
}

# 只接受指定顶层目录（uploads / projects）下的普通文件/目录，拒绝路径穿越、
# 符号链接和硬链接。旧脚本把 uploads 写死；现在用同一实现校验两种归档。
validate_tree_archive() { # archive root_dir
  local archive=$1 root=$2
  tar -tzf "$archive" | awk -v root="$root" '
    /^\// {bad=1}
    {n=split($0,a,"/"); if(a[1]!=root) bad=1; for(i=1;i<=n;i++) if(a[i]=="..") bad=1}
    END {exit bad}' || {
      echo "归档 $archive 只能包含 $root/ 下的相对路径" >&2; return 1;
    }
  tar -tvzf "$archive" | awk 'substr($0,1,1)!="-" && substr($0,1,1)!="d" {bad=1} END {exit bad}' || {
    echo "归档 $archive 含链接或特殊文件" >&2; return 1;
  }
}

validate_upload_archive() { validate_tree_archive "$1" uploads; }

validate_projects_archive() { validate_tree_archive "$1" projects; }

# 归档内必须恰好有一个非空目录（top-level），且不能为空归档。
archive_has_root() { # archive root_dir
  tar -tzf "$1" | awk -v root="$2" '
    {n=split($0,a,"/"); if(a[1]==root) found=1}
    END {exit !found}'
}

# 备份清单规则：
#   - 旧格式：恰好 2 行（db + uploads），必须能恢复；
#   - 新格式：恰好 3 行（db + uploads + projects）。
# 每项校验值合法、文件名与本次成对产物一致；清单里出现未知文件名或重复项即拒绝。
# restore.sh 与远端上传前的本地校验共用这一份实现，避免两处规则漂移。
manifest_kind() { # manifest -> legacy|projects|(空=无效)
  local manifest=$1
  [[ -f "$manifest" ]] || return 1
  awk '
    NF!=2 || length($1)!=64 || $1!~/^[a-f0-9]+$/ {bad=1}
    $2 ~ /^ragdb-.*\.sql\.gz$/ {d++; next}
    $2 ~ /^uploads-.*\.tar\.gz$/ {u++; next}
    $2 ~ /^projects-.*\.tar\.gz$/ {p++; next}
    {bad=1}
    END {
      if (bad || d!=1 || u!=1) exit 1
      if (p==0) {print "legacy"; exit 0}
      if (p==1 && NR==3) {print "projects"; exit 0}
      exit 1
    }' "$manifest"
}

validate_manifest() { # manifest db_name files_name [projects_name]
  local manifest=$1 db=$2 files=$3 projects=${4:-} kind
  kind=$(manifest_kind "$manifest") || {
    echo '备份清单必须只包含本次数据库、上传文件和（可选）项目存储三项' >&2; return 1;
  }
  case "$kind" in
    legacy)
      [[ -z "$projects" ]] || { echo '备份清单缺少项目存储项' >&2; return 1; } ;;
    projects)
      [[ -n "$projects" ]] || { echo '备份清单包含项目存储项，但恢复调用未提供 projects 归档' >&2; return 1; }
      grep -Fq "  $projects" "$manifest" || { echo '备份清单的项目存储文件名不匹配' >&2; return 1; } ;;
  esac
  grep -Fq "  $db" "$manifest" || { echo '备份清单的数据库文件名不匹配' >&2; return 1; }
  grep -Fq "  $files" "$manifest" || { echo '备份清单的上传文件文件名不匹配' >&2; return 1; }
  (cd "$(dirname "$manifest")" && sha256sum --check "$(basename "$manifest")" >/dev/null) || {
    echo '备份清单校验值与文件不符' >&2; return 1;
  }
}
