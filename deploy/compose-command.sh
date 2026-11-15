#!/usr/bin/env bash
# 统一的 docker compose 调用组装器。被环境入口及维护脚本 source。
#
# 约定：
#   ENV_FILE              传给 compose 的配置文件（默认 .env）；可为空表示不传 --env-file。
#   COMPOSE_PROJECT_NAME  非空时显式传 -p；为空时不传，由 compose 自己解析顶层 name。
#   COMPOSE_FILE          冒号分隔的覆盖文件列表（默认 docker-compose.yml）。
#
# 不用 eval，不读取配置内容，不打印命令；直接执行组装后的 docker compose。
ragkb_compose_command() {
  local value
  local -a files=()
  local -a cmd=(docker compose)
  local IFS=:

  value=${ENV_FILE-.env}
  if [[ -n "$value" ]]; then
    cmd+=(--env-file "$value")
  fi

  value=${COMPOSE_PROJECT_NAME-}
  if [[ -n "$value" ]]; then
    cmd+=(-p "$value")
  fi

  value=${COMPOSE_FILE-docker-compose.yml}
  [[ -n "$value" ]] || { echo 'COMPOSE_FILE 不能为空' >&2; return 1; }
  read -r -a files <<< "$value"
  for value in "${files[@]}"; do
    [[ -n "$value" ]] || { echo 'COMPOSE_FILE 含空的覆盖文件项' >&2; return 1; }
    cmd+=(-f "$value")
  done

  RAGKB_COMPOSE_CMD=("${cmd[@]}")
}

ragkb_compose() {
  ragkb_compose_command || return 1
  "${RAGKB_COMPOSE_CMD[@]}" "$@"
}
