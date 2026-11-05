#!/usr/bin/env bash
# 恢复前完整验证；文件先暂存，SQL 单事务恢复，失败时恢复原文件和原服务状态。
#
# 用法：
#   ./deploy/restore.sh <ragdb-*.sql.gz> [uploads-*.tar.gz] [projects-*.tar.gz]
#
# 兼容旧备份：
#   - 旧备份只有 ragdb + uploads，命令仍可只传两个文件；
#   - 新备份的 projects 归档可以显式传第三个参数，也可以在第三个参数省略时按同目录
#     清单自动发现；找不到清单/项目归档就明确提示“旧备份不含项目存储”，不静默丢数据。
set -euo pipefail
[[ $# -ge 1 && $# -le 3 ]] || {
  echo "用法: $0 <ragdb-*.sql.gz> [uploads-*.tar.gz] [projects-*.tar.gz]" >&2; exit 1;
}
# 在切换工作目录前把调用者的相对路径转成绝对路径。
DB_BACKUP=$(realpath "$1")
FILES_BACKUP=""
PROJECTS_BACKUP=""
[[ -z "${2:-}" ]] || FILES_BACKUP=$(realpath "$2")
[[ -z "${3:-}" ]] || PROJECTS_BACKUP=$(realpath "$3")
source "$(dirname "$0")/common.sh"
[[ -f "$DB_BACKUP" ]] || { echo '数据库备份不存在' >&2; exit 1; }
[[ -z "$FILES_BACKUP" || -f "$FILES_BACKUP" ]] || { echo '文件备份不存在' >&2; exit 1; }
[[ -z "$PROJECTS_BACKUP" || -f "$PROJECTS_BACKUP" ]] || { echo '项目存储备份不存在' >&2; exit 1; }
validate_database_backup "$DB_BACKUP"
stamp=$(basename "$DB_BACKUP" .sql.gz)
stamp=${stamp#ragdb-}
manifest="$(dirname "$DB_BACKUP")/manifest-$stamp.sha256"
kind=""

if [[ -n "$FILES_BACKUP" ]]; then
  validate_upload_archive "$FILES_BACKUP"
  [[ $(basename "$FILES_BACKUP") == "uploads-$stamp.tar.gz" ]] || { echo '数据库与文件备份时间标识不匹配' >&2; exit 1; }
  if [[ -f "$manifest" ]]; then
    [[ $(dirname "$DB_BACKUP") == "$(dirname "$FILES_BACKUP")" ]] || { echo '请把成对备份放在同一目录' >&2; exit 1; }
    kind=$(manifest_kind "$manifest") || { echo '备份清单格式无效' >&2; exit 1; }
    # 项目归档：显式参数优先；省略时按清单自动发现；旧备份则明确没有。
    if [[ -z "$PROJECTS_BACKUP" && "$kind" == "projects" ]]; then
      PROJECTS_BACKUP="$(dirname "$DB_BACKUP")/projects-$stamp.tar.gz"
      [[ -f "$PROJECTS_BACKUP" ]] || { echo "清单声明了项目存储，但缺少 projects-$stamp.tar.gz" >&2; exit 1; }
    fi
    if [[ "$kind" == "legacy" && -n "$PROJECTS_BACKUP" ]]; then
      echo '备份清单未包含项目存储，但提供了 projects 归档；请检查是否混用了不同批次' >&2; exit 1
    fi
    validate_manifest "$manifest" "$(basename "$DB_BACKUP")" \
      "$(basename "$FILES_BACKUP")" "${PROJECTS_BACKUP:+$(basename "$PROJECTS_BACKUP")}"
  else
    echo '警告：这是无校验清单的旧备份，只能验证压缩包与路径，不能证明两份内容一致。' >&2
  fi
else
  echo '警告：只恢复数据库，现有源文件可能与数据库不匹配。建议提供同批 uploads 备份。' >&2
  [[ -z "$PROJECTS_BACKUP" ]] || { echo '提供 projects 归档时必须同时提供 uploads 归档' >&2; exit 1; }
fi

# 清单声明的 projects 归档必须在恢复前完整校验；旧备份没有该项。
if [[ -n "$PROJECTS_BACKUP" ]]; then
  [[ "$(basename "$PROJECTS_BACKUP")" == "projects-$stamp.tar.gz" ]] \
    || { echo '项目存储备份时间标识不匹配' >&2; exit 1; }
  [[ $(dirname "$PROJECTS_BACKUP") == "$(dirname "$DB_BACKUP")" ]] \
    || { echo '请把成对备份放在同一目录' >&2; exit 1; }
  validate_projects_archive "$PROJECTS_BACKUP"
fi

echo "即将覆盖数据库: $DB_BACKUP"
[[ -z "$FILES_BACKUP" ]] || echo "即将覆盖上传目录: $FILES_BACKUP"
[[ -z "$PROJECTS_BACKUP" ]] || echo "即将覆盖项目存储: $PROJECTS_BACKUP"
if [[ -z "$PROJECTS_BACKUP" ]]; then
  echo '提示：本次不恢复项目存储（旧备份或未提供），现有 projects 目录保持不变。' >&2
fi
read -r -p '输入 yes 继续: ' reply
[[ "$reply" == yes ]] || { echo '已取消'; exit 1; }

stage=$(mktemp -d "$DATA_DIR/.restore-stage.XXXXXX")
previous="$stage/previous"
committed=0
# 逐目录追踪本次实际动过的目录，只回滚这些：
#   0 = 未动；1 = 已把原目录移到 previous（或原本没有）；2 = 新目录已就位。
# 只恢复 uploads 的旧备份绝不能因为 SQL 失败而 rm 现有 projects——它从未被交换。
declare -A touched_state=([uploads]=0 [projects]=0)
restore_dir() { # 归档 顶层目录名
  tar -xzf "$1" -C "$stage"
  [[ -d "$stage/$2" ]] || { echo "备份缺少 $2 目录" >&2; return 1; }
  if [[ $(id -u) -eq 0 ]]; then chown -R 1000:1000 "$stage/$2"; fi
}
rollback_dir() { # 顶层目录名
  local name=$1 state=${touched_state[$name]:-0}
  [[ $state -ne 0 ]] || return 0
  # 只有“新目录已就位”才需要删掉它；若 mv 失败发生在就位前，现有目录仍是原目录。
  if [[ $state -eq 2 ]]; then rm -rf "${DATA_DIR:?}/$name"; fi
  if [[ -d "$previous/$name" ]]; then
    mv "$previous/$name" "$DATA_DIR/$name" || {
      echo "回滚 $name 失败：原目录仍在 $previous/$name，请人工恢复" >&2
      return 1
    }
  elif [[ $state -eq 1 ]]; then
    # 原本没有该目录：恢复成“没有”，而不是留下半成品。
    rm -rf "${DATA_DIR:?}/$name" 2>/dev/null || true
  fi
  touched_state[$name]=0
}
cleanup() {
  local status=$? name restore_failed=0 rollback_any=0
  trap - EXIT
  if [[ $committed -eq 0 ]]; then
    for name in uploads projects; do
      [[ ${touched_state[$name]:-0} -ne 0 ]] && rollback_any=1
      rollback_dir "$name" || restore_failed=1
    done
    if [[ $rollback_any -eq 1 ]]; then
      if [[ $restore_failed -eq 0 ]]; then
        echo '数据库恢复失败：SQL 已回滚，本次替换过的目录已还原。' >&2
      else
        echo '数据库恢复失败，且部分目录回滚未完成，请按上面的路径人工恢复。' >&2
      fi
    fi
  fi
  if [[ $restore_failed -ne 0 ]]; then
    # 回滚未完成：previous/ 里可能只剩唯一原件，绝不能删。保留 stage 并明确路径。
    # 注意：此分支必须在 rm -rf "$stage" 之前判断，否则“保留”提示之后原件已被删除。
    echo "回滚未完成：已保留暂存目录 $stage（原目录在 $stage/previous），请人工恢复后再删除。" >&2
    status=1
  else
    rm -rf "$stage"
  fi
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
  restore_dir "$FILES_BACKUP" uploads
fi
if [[ -n "$PROJECTS_BACKUP" ]]; then
  restore_dir "$PROJECTS_BACKUP" projects
fi
stop_writers
mkdir -p "$previous"
if [[ -n "$FILES_BACKUP" ]]; then
  if [[ -e "$DATA_DIR/uploads" ]]; then
    mv "$DATA_DIR/uploads" "$previous/uploads" || exit 1
  fi
  touched_state[uploads]=1
  mv "$stage/uploads" "$DATA_DIR/uploads" || exit 1
  touched_state[uploads]=2
fi
if [[ -n "$PROJECTS_BACKUP" ]]; then
  if [[ -e "$DATA_DIR/projects" ]]; then
    mv "$DATA_DIR/projects" "$previous/projects" || exit 1
  fi
  touched_state[projects]=1
  mv "$stage/projects" "$DATA_DIR/projects" || exit 1
  touched_state[projects]=2
fi
# pg_dump 不带 --create，支持在同一事务里恢复；任意 SQL 错误均回滚整个数据库变更。
gunzip -c "$DB_BACKUP" | docker compose -f "$COMPOSE_FILE" exec -T db \
  psql -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-ragdb}" \
  --single-transaction -v ON_ERROR_STOP=1
committed=1
start_writers
echo '恢复完成，请检查 /health，并抽查文档、项目来源与问答。'
