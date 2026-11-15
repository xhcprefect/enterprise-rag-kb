#!/usr/bin/env bash
# Docker CLI 替身：只记录调用并按脚本返回固定输出，绝不接触运行中的真实服务。
#
# 状态模拟：
#   维护脚本通过容器标签发现“正在运行的写服务”，再按容器 ID 逐个 stop/start。
#   state/running 每行是 `<project> <id> <service>`。
#     FAKE_DOCKER_RUNNING="id:service id:service ..."（或成对 `id service ...`）
#       所有容器归入 FAKE_DOCKER_DEFAULT_PROJECT / 默认 rag-kb，兼容旧测试。
#     FAKE_DOCKER_PROJECT_RUNNING="project:id:service project:id:service ..."
#       同时模拟多个 Compose project，按真实 label 过滤。
#   未设置时默认 rag-kb 项目的 api + worker（兼容旧版本的维护测试）。
#
# 故意**不**实现 `compose ps --services` / `compose config --services`：维护脚本必须
# 通过标签发现写服务，否则 profile 角色/遗留容器会被漏停，测试要能抓到这种回退。
set -euo pipefail
log=${OPERATIONS_LOG:?}
printf '%s\n' "$*" >> "$log"

state=${FAKE_DOCKER_STATE:-${TMPDIR:-/tmp}/ragkb-fake-docker-state}
[[ -d "$state" ]] || mkdir -p "$state"
if [[ ! -f "$state/running" ]]; then
  : > "$state/running"
  if [[ -n "${FAKE_DOCKER_PROJECT_RUNNING:-}" ]]; then
    read -r -a items <<< "$FAKE_DOCKER_PROJECT_RUNNING"
    for item in "${items[@]}"; do
      IFS=: read -r project id svc <<< "$item"
      [[ -n "$project" && -n "$id" && -n "$svc" ]] || {
        echo "FAKE_DOCKER_PROJECT_RUNNING 必须是 project:id:service 项（当前: $item）" >&2
        exit 2
      }
      printf '%s %s %s\n' "$project" "$id" "$svc" >> "$state/running"
    done
  elif [[ -n "${FAKE_DOCKER_RUNNING:-}" ]]; then
    default_project=${FAKE_DOCKER_DEFAULT_PROJECT:-${FAKE_COMPOSE_PROJECT:-rag-kb}}
    # 支持两种写法：`id:service id:service` 或成对的 `id service id service`。
    # 不能直接 `printf '%s\n' $FAKE_DOCKER_RUNNING`——未加引号会按空格拆成单列，
    # 把 id 与 service 变成两行，后续解析全部错位。
    read -r -a tokens <<< "${FAKE_DOCKER_RUNNING//:/ }"
    [[ $(( ${#tokens[@]} % 2 )) -eq 0 ]] || {
      echo "FAKE_DOCKER_RUNNING 必须是 id service 对（当前: $FAKE_DOCKER_RUNNING）" >&2
      exit 2
    }
    for ((i=0; i<${#tokens[@]}; i+=2)); do
      printf '%s %s %s\n' "$default_project" "${tokens[i]}" "${tokens[i+1]}" >> "$state/running"
    done
  else
    printf '%s\n' 'rag-kb c-api api' 'rag-kb c-worker worker' > "$state/running"
  fi
fi
# 记录“曾经存在”的容器：start 只允许恢复其中仍存在的，已删除的必须失败。
# 只增不减——stop 会从 running 移除，但不能因此忘记它曾经存在。
if [[ -f "$state/known" ]]; then
  cat "$state/known" "$state/running" | sort -u > "$state/known.tmp"
else
  cp "$state/running" "$state/known.tmp"
fi
mv "$state/known.tmp" "$state/known"

service_of() { # id -> service（找不到返回空）
  awk -v id="$1" '$2==id {print $3}' "$state/running" | head -1
}

# 解析 helper 生成的 compose 参数，并拒绝把冒号分隔 COMPOSE_FILE 当成单个 -f。
parse_compose_args() {
  compose_project=''
  compose_env_file=''
  compose_files=()
  local file
  while (( $# )); do
    case "$1" in
      compose) shift ;;
      -p|--project-name)
        [[ $# -ge 2 ]] || { echo 'compose -p 缺少参数' >&2; exit 94; }
        compose_project=$2; shift 2 ;;
      --project-name=*) compose_project=${1#*=}; shift ;;
      -f|--file)
        [[ $# -ge 2 ]] || { echo 'compose -f 缺少参数' >&2; exit 94; }
        compose_files+=("$2"); shift 2 ;;
      --file=*) compose_files+=("${1#*=}"); shift ;;
      --env-file)
        [[ $# -ge 2 ]] || { echo 'compose --env-file 缺少参数' >&2; exit 94; }
        compose_env_file=$2; shift 2 ;;
      --env-file=*) compose_env_file=${1#*=}; shift ;;
      *) shift ;;
    esac
  done
  for file in "${compose_files[@]}"; do
    [[ "$file" != *:* ]] || {
      echo "COMPOSE_FILE 必须拆成多个 -f，不可把冒号路径直接传给 compose: $file" >&2
      exit 94
    }
  done
  if [[ -z "$compose_project" ]]; then
    compose_project=${FAKE_COMPOSE_PROJECT:-}
    if [[ -z "$compose_project" ]]; then
      for file in "${compose_files[@]}"; do
        [[ -f "$file" ]] || continue
        compose_project=$(sed -n 's/^name:[[:space:]]*\([^[:space:]]*\).*/\1/p' "$file" | head -1)
        [[ -n "$compose_project" ]] && break
      done
    fi
    compose_project=${compose_project:-rag-kb}
  fi
}

record_compose_call() { # subcommand
  [[ -n "${FAKE_COMPOSE_LOG:-}" ]] || return 0
  local joined=${compose_files[*]:-}
  printf '%s|%s|%s|%s|%s|%s\n' "$1" "$compose_project" \
    "$compose_env_file" "$joined" "${BACKEND_ENV_FILE:-}" "$*" >> "$FAKE_COMPOSE_LOG"
}

case "$*" in
  *'compose'*' exec -T db pg_dump'*|*'compose'*'exec -T db pg_dump'*|*pg_dump*)
    parse_compose_args "$@"
    record_compose_call pg_dump
    [[ ${FAIL_DUMP:-0} != 1 ]] || exit 91
    printf -- '-- PostgreSQL database dump\nSELECT 1;\n'
    ;;
  *'compose'*'exec -T db'*|*psql*)
    parse_compose_args "$@"
    record_compose_call psql
    cat >/dev/null
    [[ ${FAIL_RESTORE:-0} != 1 ]] || exit 92
    ;;
  *'compose'*' config'*)
    parse_compose_args "$@"
    record_compose_call config
    # 校验失败模拟：docker/compose 不可用时维护脚本必须 fail closed。
    [[ ${FAIL_COMPOSE_CONFIG:-0} != 1 ]] || { echo 'compose config failed' >&2; exit 90; }
    # 维护脚本只读取顶层项目名；模拟显式 -p 或 compose 文件里的顶层 name。
    printf 'name: %s\n' "$compose_project"
    ;;
  *' ps --filter label=com.docker.compose.project='*|'ps --filter label=com.docker.compose.project='*)
    # docker ps 失败模拟：不能让维护脚本把失败当成“没有写服务”继续备份。
    [[ ${FAIL_DOCKER_PS:-0} != 1 ]] || { echo 'docker ps failed' >&2; exit 89; }
    project=''
    for arg in "$@"; do
      [[ "$arg" == label=com.docker.compose.project=* ]] || continue
      project=${arg##*=}
      break
    done
    [[ -n "$project" ]] || { echo 'docker ps 必须按 com.docker.compose.project 标签过滤' >&2; exit 88; }
    awk -v project="$project" '$1==project {print $2, $3}' "$state/running"
    ;;
  *' stop --timeout '*|'stop --timeout '*)
    # docker stop --timeout N <id>
    id=${*: -1}
    svc=$(service_of "$id")
    [[ -n "$svc" ]] || { echo "no such container: $id" >&2; exit 1; }
    awk -v id="$id" '$2!=id' "$state/running" > "$state/running.tmp"
    mv "$state/running.tmp" "$state/running"
    ;;
  *' start '*|'start '*)
    # docker start <id>；已删除的容器必须失败，验证“只恢复原本运行的服务”。
    id=${*: -1}
    if ! awk -v id="$id" '$2==id {found=1} END {exit !found}' "$state/known"; then
      echo "no such container: $id" >&2; exit 1
    fi
    project=$(awk -v id="$id" '$2==id {print $1; exit}' "$state/known")
    svc=$(awk -v id="$id" '$2==id {print $3; exit}' "$state/known")
    if ! awk -v id="$id" '$2==id {found=1} END {exit !found}' "$state/running"; then
      printf '%s %s %s\n' "$project" "$id" "$svc" >> "$state/running"
    fi
    ;;
  *) echo "意外的 Docker 调用: $*" >&2; exit 93 ;;
esac
