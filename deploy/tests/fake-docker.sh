#!/usr/bin/env bash
# Docker CLI 替身：只记录调用并按脚本返回固定输出，绝不接触运行中的真实服务。
#
# 状态模拟：
#   维护脚本通过容器标签发现“正在运行的写服务”，再按容器 ID 逐个 stop/start。
#   这里用一个状态文件记录“当前在跑的容器”，让断言可以覆盖：
#     FAKE_DOCKER_RUNNING="id:service id:service ..."
#   未设置时默认 api + worker（兼容旧版本的维护测试）。
#
# 故意**不**实现 `compose ps --services` / `compose config --services`：维护脚本必须
# 通过标签发现写服务，否则 profile 角色/遗留容器会被漏停，测试要能抓到这种回退。
set -euo pipefail
log=${OPERATIONS_LOG:?}
printf '%s\n' "$*" >> "$log"

state=${FAKE_DOCKER_STATE:-${TMPDIR:-/tmp}/ragkb-fake-docker-state}
[[ -d "$state" ]] || mkdir -p "$state"
if [[ ! -f "$state/running" ]]; then
  if [[ -n "${FAKE_DOCKER_RUNNING:-}" ]]; then
    # 支持两种写法：`id:service id:service` 或成对的 `id service id service`。
    # 不能直接 `printf '%s\n' $FAKE_DOCKER_RUNNING`——未加引号会按空格拆成单列，
    # 把 id 与 service 变成两行，后续解析全部错位。
    read -r -a tokens <<< "${FAKE_DOCKER_RUNNING//:/ }"
    [[ $(( ${#tokens[@]} % 2 )) -eq 0 ]] || {
      echo "FAKE_DOCKER_RUNNING 必须是 id service 对（当前: $FAKE_DOCKER_RUNNING）" >&2; exit 2;
    }
    : > "$state/running"
    for ((i=0; i<${#tokens[@]}; i+=2)); do
      printf '%s %s\n' "${tokens[i]}" "${tokens[i+1]}" >> "$state/running"
    done
  else
    printf '%s\n' 'c-api api' 'c-worker worker' > "$state/running"
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
  awk -v id="$1" '$1==id {print $2}' "$state/running" | head -1
}

case "$*" in
  *'compose'*' exec -T db pg_dump'*|*'compose'*'exec -T db pg_dump'*|*pg_dump*)
    [[ ${FAIL_DUMP:-0} != 1 ]] || exit 91
    printf -- '-- PostgreSQL database dump\nSELECT 1;\n'
    ;;
  *pg_dump*)
    [[ ${FAIL_DUMP:-0} != 1 ]] || exit 91
    printf -- '-- PostgreSQL database dump\nSELECT 1;\n'
    ;;
  *'compose'*'exec -T db'*|*psql*)
    cat >/dev/null
    [[ ${FAIL_RESTORE:-0} != 1 ]] || exit 92
    ;;
  *'compose'*' config'*)
    # 校验失败模拟：docker/compose 不可用时维护脚本必须 fail closed。
    [[ ${FAIL_COMPOSE_CONFIG:-0} != 1 ]] || { echo 'compose config failed' >&2; exit 90; }
    # 维护脚本只读取顶层项目名；用环境变量/默认名模拟，避免依赖真实 compose 文件。
    printf 'name: %s\n' "${FAKE_COMPOSE_PROJECT:-rag-kb}"
    ;;
  *' ps --filter label=com.docker.compose.project='*|'ps --filter label=com.docker.compose.project='*)
    # docker ps 失败模拟：不能让维护脚本把失败当成“没有写服务”继续备份。
    [[ ${FAIL_DOCKER_PS:-0} != 1 ]] || { echo 'docker ps failed' >&2; exit 89; }
    while read -r id svc; do
      [[ -n "$id" && -n "$svc" ]] || continue
      printf '%s %s\n' "$id" "$svc"
    done < "$state/running"
    ;;
  *' stop --timeout '*|'stop --timeout '*)
    # docker stop --timeout N <id>
    id=${*: -1}
    svc=$(service_of "$id")
    [[ -n "$svc" ]] || { echo "no such container: $id" >&2; exit 1; }
    grep -v "^$id " "$state/running" > "$state/running.tmp" || true
    mv "$state/running.tmp" "$state/running"
    ;;
  *' start '*|'start '*)
    # docker start <id>；已删除的容器必须失败，验证“只恢复原本运行的服务”。
    id=${*: -1}
    if ! grep -q "^$id " "$state/known"; then
      echo "no such container: $id" >&2; exit 1
    fi
    svc=$(awk -v id="$id" '$1==id {print $2}' "$state/known" | head -1)
    grep -q "^$id " "$state/running" 2>/dev/null || printf '%s %s\n' "$id" "$svc" >> "$state/running"
    ;;
  *) echo "意外的 Docker 调用: $*" >&2; exit 93 ;;
esac
