#!/usr/bin/env bash
# 演练安全边界专用 Docker 替身：只模拟 restore-drill.sh 用到的调用。
#
# 场景（由环境变量选择）：
#   FAKE_DRILL_UP_FAIL=1         compose up 失败，用于验证失败后的清理路径
#   FAKE_DRILL_FOREIGN_OWNER=x   项目里已有资源，但 owner 标签是别人的
#                                （项目冲突检查阶段就直接拒绝）
#   FAKE_DRILL_APPEAR_LATE=1     项目冲突检查期间为空，清理列举时出现别人的资源
#                                （用于验证 cleanup 的逐项 owner 核对，而不是只靠前置检查）
# 任何真实 Docker 副作用都不会发生。
set -euo pipefail
printf '%s\n' "$*" >> "${OPERATIONS_LOG:?}"

state_dir=${FAKE_DRILL_STATE:-${TMPDIR:-/tmp}/ragkb-fake-drill-state}
mkdir -p "$state_dir"

# 每次列举同一类资源就累加计数：第 1 次对应“启动前冲突检查”，之后对应清理阶段。
next_count() { # kind
  local f="$state_dir/$1" n=0
  [[ -f "$f" ]] && n=$(cat "$f")
  n=$((n + 1))
  printf '%s' "$n" > "$f"
  printf '%s' "$n"
}

# 该项目是否要“暴露”资源；返回 0 表示列出
project_has_resources() { # kind
  [[ -n "${FAKE_DRILL_FOREIGN_OWNER:-}" ]] || return 1
  if [[ "${FAKE_DRILL_APPEAR_LATE:-0}" == 1 ]]; then
    # 前置检查（第 1 次）为空，清理阶段（>=2）才出现
    (( $(next_count "$1") >= 2 ))
    return
  fi
  next_count "$1" >/dev/null
  return 0
}

case "$*" in
  *'compose'*'up -d --wait db'*)
    [[ ${FAKE_DRILL_UP_FAIL:-0} != 1 ]] || { echo 'fake compose: db 启动失败' >&2; exit 70; }
    echo ' Container db  Started'
    ;;
  *'compose'*'ps -aq'*) ;;
  *'compose'*'down'*)
    echo '不应使用 compose down（会按项目批量删除、不核对 owner）' >&2
    exit 72
    ;;
  *'ps -aq --filter label=com.docker.compose.project='*)
    project_has_resources containers && printf 'fakecontainercid\n'
    ;;
  *'ps -q --filter label=com.docker.compose.project='*)
    # 启动后核对容器 owner 时的短查询；本次不返回容器（清理阶段才出现）。
    ;;
  *'compose'*'exec'*)
    echo 'fake compose: 演练数据写入失败（替身不实现 psql）' >&2
    exit 94
    ;;
  *'volume ls -q --filter label=com.docker.compose.project='*)
    project_has_resources volumes && printf 'fakevol\n'
    ;;
  *'network ls -q --filter label=com.docker.compose.project='*)
    project_has_resources networks && printf 'fakenet\n'
    ;;
  *'ps -aq --filter label=ragkb.drill.owner='*)
    [[ -n "${FAKE_DRILL_FOREIGN_OWNER:-}" ]] && printf 'fakecontainercid\n'
    ;;
  *'volume ls -q --filter label=ragkb.drill.owner='*) ;;
  *'network ls -q --filter label=ragkb.drill.owner='*) ;;
  *'inspect --format'*)
    # 容器查询走 .Config.Labels，卷/网络走 .Labels；两者都返回同一个“外部”owner。
    [[ -n "${FAKE_DRILL_FOREIGN_OWNER:-}" ]] && printf '%s\n' "$FAKE_DRILL_FOREIGN_OWNER"
    ;;
  *'rm -f fakecontainercid'*) echo '不应删除不属于本次演练的资源' >&2; exit 71 ;;
  *'volume rm fakevol'*) echo '不应删除不属于本次演练的卷' >&2; exit 73 ;;
  *'network rm fakenet'*) echo '不应删除不属于本次演练的网络' >&2; exit 74 ;;
  *) echo "意外的 Docker 调用: $*" >&2; exit 93 ;;
esac
