#!/usr/bin/env bash
# 同机环境统一入口：部署、维护及监控渲染均必须先显式选择环境。
#
# 用法：
#   bash deploy/env.sh <production|staging> <command> [args...]
#   command: init | compose | backup | restore | remote-backup | render-prometheus | render-alertmanager
#
# 日常输出不打印配置内容；只有管理员显式执行 `compose config` 才会输出 Compose
# 渲染结果（其中可能包含密钥），请自行保护输出。
set -euo pipefail
repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repo"

usage() {
  cat >&2 <<'EOF'
用法: bash deploy/env.sh <production|staging> <init|compose|backup|restore|remote-backup|render-prometheus|render-alertmanager> [参数...]
环境必须显式选择，没有默认值。compose config 可能输出密钥；仅管理员显式调用。
EOF
  exit 2
}
die() { echo "环境入口: $*" >&2; exit 1; }

[[ $# -ge 2 ]] || usage
environment=$1
command=$2
shift 2
case "$environment" in
  production|staging) ;;
  *) usage ;;
esac
case "$command" in
  init|compose|backup|restore|remote-backup|render-prometheus|render-alertmanager) ;;
  *) usage ;;
esac

# shellcheck disable=SC1091
source "$repo/deploy/environment.sh"
# shellcheck disable=SC1091
source "$repo/deploy/compose-command.sh"

if [[ $command == init ]]; then
  root=${RAGKB_ENV_ROOT:-$repo/deploy/environments}
  [[ "$root" == /* ]] || root="$(cd "$root" 2>/dev/null && pwd)" || die 'RAGKB_ENV_ROOT 不存在'
  root=$(realpath -m -- "$root")
  env_dir="$root/$environment"
  env_file="$env_dir/.env"
  backend_file="$env_dir/backend.env"
  # 任何写入前拒绝 symlink/解析后位置不同的目录：目录可能指向另一环境，
  # 不能先把模板写进目标再由 loader 报错。
  [[ ! -L "$env_dir" ]] || die "配置目录不能是符号链接: $env_dir"
  [[ "$(realpath -m -- "$env_dir")" == "$env_dir" ]] || die "配置目录解析后指向不同位置: $env_dir"
  mkdir -p "$env_dir"
  # dangling symlink 也会是 -e=false；不能沿它写到另一个环境的配置文件。
  for config in "$env_file" "$backend_file" "$env_file.example" "$backend_file.example"; do
    [[ ! -L "$config" ]] || die "配置不能是符号链接: $config"
  done
  # RAGKB_ENV_ROOT 可指向独立测试根；模板始终用仓库内受控文件，不信任临时根的模板。
  env_template="$repo/deploy/environments/$environment/.env.example"
  backend_template="$repo/deploy/environments/$environment/backend.env.example"
  created=0
  if [[ ! -e "$env_file" ]]; then
    [[ -f "$env_template" ]] || die "缺少模板 $env_template"
    ( umask 077; cp "$env_template" "$env_file" )
    chmod 600 "$env_file"
    created=1
  fi
  if [[ ! -e "$backend_file" ]]; then
    [[ -f "$backend_template" ]] || die "缺少模板 $backend_template"
    ( umask 077; cp "$backend_template" "$backend_file" )
    chmod 600 "$backend_file"
    created=1
  fi
  if [[ $created -eq 1 ]]; then
    echo "已从模板生成 $environment 的占位配置（权限 0600，未覆盖已有文件）。" >&2
    echo "请先填写独立凭据、镜像标签及可选端口，再重新执行 init。" >&2
    exit 1
  fi
fi

ragkb_load_environment "$environment" || exit 1

ragkb_check_compose_args() {
  local arg expect_value=0
  RAGKB_COMPOSE_SUBCOMMAND=
  # 只检查子命令之前的 compose 全局参数；exec 等子命令内部可有自己的 -f/--file。
  for arg in "$@"; do
    if [[ $expect_value -eq 1 ]]; then expect_value=0; continue; fi
    case "$arg" in
      --) break ;;
      -p|--project-name|--env-file|-f|--file|--project-directory|-p*|-f*|--project-name=*|--env-file=*|--file=*|--project-directory=*)
        die '不能用 compose 全局参数覆盖已选环境/项目/配置' ;;
      --profile|--progress|--parallel|--ansi)
        expect_value=1; continue ;;
      --profile=*|--progress=*|--parallel=*|--ansi=*|--verbose|--dry-run)
        continue ;;
      -*) continue ;;
      *) RAGKB_COMPOSE_SUBCOMMAND=$arg; break ;;
    esac
  done
  case "$RAGKB_COMPOSE_SUBCOMMAND" in
    system|prune) die '不提供全局 prune / system 操作' ;;
  esac
}

case "$command" in
  init)
    # 由维护模块参数化的 init.sh 读取已选 ENV_FILE / BACKEND_ENV_FILE。
    bash "$repo/deploy/init.sh" "$@"
    ;;
  compose)
    [[ $# -ge 1 ]] || die 'compose 缺少子命令，例如 ps / up -d --no-build / config'
    ragkb_check_compose_args "$@"
    [[ $RAGKB_COMPOSE_SUBCOMMAND != config ]] || echo '提示: compose config 可能输出配置中的密钥，请确认输出通道安全。' >&2
    ragkb_compose "$@"
    ;;
  backup)
    bash "$repo/deploy/backup.sh" "$@"
    ;;
  restore)
    bash "$repo/deploy/restore.sh" "$@"
    ;;
  remote-backup)
    bash "$repo/deploy/remote-backup.sh" "$@"
    ;;
  render-prometheus)
    bash "$repo/deploy/monitoring/render-prometheus.sh" "$@"
    ;;
  render-alertmanager)
    bash "$repo/deploy/monitoring/render-alertmanager.sh" "$@"
    ;;
esac
