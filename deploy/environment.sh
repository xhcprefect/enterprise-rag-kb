#!/usr/bin/env bash
# 选择并校验一个同机部署环境，供 deploy/env.sh source。不允许 .env 内容重定向到另一个环境。
#
# 入参：ragkb_load_environment <production|staging> [CONFIG_ROOT]
# 成功时导出 ENV_FILE、BACKEND_ENV_FILE、RAGKB_DEPLOY_ENVIRONMENT、COMPOSE_PROJECT_NAME、
# COMPOSE_FILE、DATA_DIR 等已选配置值，供 compose 与维护脚本使用。
set -euo pipefail

ragkb_env_error() { echo "环境配置错误: $*" >&2; }

# 读选定 .env 的最后一个简单 KEY=VALUE；去掉成对引号、CRLF 和行尾空白。
# 只用于读取身份/路径/项目名做 fail-closed 校验，实际加载由一次 source 完成。
ragkb_env_value() {
  local file=$1 key=$2 value
  [[ -f "$file" ]] || return 1
  value=$(grep -E "^${key}=" "$file" | tail -1 | cut -d= -f2-) || return 1
  value=${value%$'\r'}
  value=${value%"${value##*[![:space:]]}"}
  if [[ ${#value} -ge 2 && "$value" == \"*\" ]]; then value=${value:1:${#value}-2}; fi
  if [[ ${#value} -ge 2 && "$value" == \'*\' ]]; then value=${value:1:${#value}-2}; fi
  printf '%s' "$value"
}

ragkb_canonical_path() { realpath -m -- "$1"; }
ragkb_path_inside() { [[ "$1" == "$2" || "$1" == "$2"/* ]]; }
ragkb_is_dir_key() { case "$1" in OUT_DIR|RAGKB_PROMETHEUS_CONFIG_DIR|RAGKB_PROMETHEUS_SECRET_DIR|RAGKB_ALERTMANAGER_CONFIG_DIR|RAGKB_ALERTMANAGER_SECRET_DIR|RAGKB_TLS_CERT_DIR|RAGKB_ACME_WEBROOT) return 0 ;; *) return 1 ;; esac; }

# 读取一个可选路径字段：沿用简单 KEY=VALUE、去引号/CRLF 与 ${DATA_DIR} 展开，
# 只接受绝对路径。结果写入 RAGKB_ENV_OPTIONAL_PATH；空值表示字段缺席。
# 错误只报变量名和所属环境，不回显路径或配置内容。
ragkb_read_optional_path() { # context file key data_dir
  local context=$1 file=$2 key=$3 data_dir=$4 value
  RAGKB_ENV_OPTIONAL_PATH=
  value=$(ragkb_env_value "$file" "$key" || true)
  # 同机 HTTPS 的有效默认值也参与两环境比较，不能漏掉 Compose 隐式挂载。
  if [[ -z "$value" && "$key" == RAGKB_ACME_WEBROOT ]]; then
    case ":$(ragkb_env_value "$file" COMPOSE_FILE || true):" in
      *:docker-compose.https.yml:*) value="$data_dir/acme" ;;
    esac
  fi
  [[ -n "$value" ]] || return 0
  value=${value//'${DATA_DIR}'/$data_dir}
  [[ "$value" == /* ]] || {
    ragkb_env_error "$context 的 $key 必须是绝对路径"; return 1
  }
  RAGKB_ENV_OPTIONAL_PATH=$(ragkb_canonical_path "$value")
}

# 清理父进程导出的所有已知部署前缀变量，避免 shell 里的生产额度、TLS/发布地址、
# 项目名或密钥覆盖所选 .env。只按变量名前缀处理，不解释配置内容。
ragkb_clear_ambient() {
  local key
  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    case "$key" in
      ENV_FILE|BACKEND_ENV_FILE|BACKUP_DIR|DATA_DIR|OUT_DIR|\
      COMPOSE_*|BACKEND_*|FRONTEND_*|POSTGRES_*|JWT_*|ADMIN_*|\
      EMBEDDING_*|LLM_*|RERANK_*|METRICS_TOKEN|RCLONE_*|\
      DB_*|API_*|WORKER_*|WEB_*|RAGKB_*|KEEP_DAYS|CHAT_MAX_*|\
      CHAT_LEASE_SECONDS|PROJECT_*|LOG_*|THREAD_POOL_SIZE)
        unset "$key" ;;
    esac
  done < <(compgen -e || true)
}

ragkb_load_environment() {
  local name=${1:-} root_override=${2:-} root dir env_file backend_file expected_project
  local data backup value other other_name other_env other_data other_project image_name
  local other_backend canonical_data canonical_other canonical_backend canonical_other_backend
  local canonical_backup canonical_other_backup key selected_path other_path
  local i_path j_path flag path
  local https_enabled=0 monitoring_enabled=0 alerting_enabled=0
  local -a compose_files=()
  local -a deny_keys=(ENV_FILE BACKEND_ENV_FILE RAGKB_ENV_ROOT RAGKB_ENV_SELECTED)
  local -a dir_keys=(
    OUT_DIR RAGKB_PROMETHEUS_CONFIG_DIR RAGKB_PROMETHEUS_SECRET_DIR
    RAGKB_ALERTMANAGER_CONFIG_DIR RAGKB_ALERTMANAGER_SECRET_DIR
    RAGKB_TLS_CERT_DIR RAGKB_ACME_WEBROOT
  )
  local -a file_keys=(
    RAGKB_METRICS_TOKEN_FILE RAGKB_ALERT_WEBHOOK_FILE RCLONE_CONFIG
  )
  local -a path_keys=("${dir_keys[@]}" "${file_keys[@]}")

  case "$name" in
    production|staging) ;;
    *) ragkb_env_error '必须显式选择 production 或 staging'; return 1 ;;
  esac
  [[ -z "${RAGKB_ENV_SELECTED:-}" ]] || {
    ragkb_env_error '环境入口不可嵌套加载'; return 1;
  }

  root=${root_override:-${RAGKB_ENV_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/environments}}
  [[ "$root" == /* ]] || root="$(cd "$root" 2>/dev/null && pwd)" || {
    ragkb_env_error 'RAGKB_ENV_ROOT 不存在'; return 1;
  }
  root=$(ragkb_canonical_path "$root")
  dir=$(ragkb_canonical_path "$root/$name")
  # 环境目录/配置不能是指向另一环境的 symlink，避免 init 或维护写入共享文件。
  [[ ! -L "$root/$name" ]] || { ragkb_env_error '环境配置目录不能是符号链接'; return 1; }
  [[ "$dir" == "$root/$name" ]] || { ragkb_env_error '环境配置目录解析后指向不同位置'; return 1; }
  env_file="$dir/.env"
  backend_file="$dir/backend.env"
  for value in "$env_file" "$backend_file"; do
    [[ ! -L "$value" ]] || { ragkb_env_error '环境配置文件不能是符号链接'; return 1; }
  done
  expected_project=ragkb-prod
  [[ "$name" == staging ]] && expected_project=ragkb-staging
  # 应用配置必须是所选环境的独立文件：不能是 dangling/跨环境 symlink，也不能
  # 把部署 .env 的控制字段复制进来。只校验结构，不限制 APP_NAME 等业务命名。
  [[ "$(ragkb_env_value "$backend_file" ENVIRONMENT || true)" == production ]] || {
    ragkb_env_error "$name/backend.env 的应用配置身份不正确"; return 1;
  }
  for key in ENV_FILE BACKEND_ENV_FILE RAGKB_ENV_ROOT RAGKB_ENV_SELECTED \
             RAGKB_DEPLOY_ENVIRONMENT COMPOSE_PROJECT_NAME COMPOSE_FILE DATA_DIR BACKUP_DIR; do
    value=$(ragkb_env_value "$backend_file" "$key" || true)
    [[ "$value" != "$name" && "$value" != "$expected_project" && -z "$value" ]] || {
      ragkb_env_error "$name/backend.env 混入了部署环境配置"; return 1;
    }
  done
  [[ -f "$env_file" && -r "$env_file" ]] || {
    ragkb_env_error "缺少 $name 环境配置 $env_file（首次部署请先执行 init）"; return 1;
  }
  [[ -f "$backend_file" && -r "$backend_file" ]] || {
    ragkb_env_error "缺少 $name 应用配置 $backend_file（首次部署请先执行 init）"; return 1;
  }
  # 父进程不能通过 BACKEND_ENV_FILE 指向另一环境/legacy 配置；入口总会强制覆盖它。
  value=${BACKEND_ENV_FILE:-}
  if [[ -n "$value" ]]; then
    [[ "$value" == "$backend_file" || "$value" == "$dir/backend.env" ]] || {
      ragkb_env_error '父进程的 BACKEND_ENV_FILE 与所选环境不一致'; return 1;
    }
  fi

  # 拒绝入口控制字段出现在 .env 内，避免加载后重定向到另一个环境。
  while IFS= read -r value || [[ -n "$value" ]]; do
    value=${value%$'\r'}
    [[ "$value" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]] || continue
    key=${BASH_REMATCH[1]}
    for other in "${deny_keys[@]}"; do
      [[ "$key" != "$other" ]] || {
        ragkb_env_error "$key 属于入口控制变量，不能在环境 .env 内设置"; return 1;
      }
    done
  done < "$env_file"

  # 一次 source 选中的 .env；同一份结果供 compose/维护脚本使用。只展开模板允许的
  # ${DATA_DIR} 引用并去掉 CRLF，其余保持 shell 的 KEY=VALUE 语义（含引号）。
  ragkb_clear_ambient
  data=$(ragkb_env_value "$env_file" DATA_DIR) || data=
  backup=$(ragkb_env_value "$env_file" BACKUP_DIR) || backup=
  [[ -n "$data" ]] || { ragkb_env_error '环境 .env 缺少 DATA_DIR'; return 1; }
  backup=${backup//'${DATA_DIR}'/$data}
  set -a
  # shellcheck disable=SC1090
  source <(
    sed -e 's/[[:space:]]*$//' -e 's/\r$//' "$env_file" \
      | grep -E '^[A-Za-z_][A-Za-z0-9_]*='
    printf 'DATA_DIR=%q\nBACKUP_DIR=%q\n' "$data" "$backup"
  )
  set +a

  # 先核验 .env 自己的身份，再由入口强制设定控制字段。
  [[ "${RAGKB_DEPLOY_ENVIRONMENT:-}" == "$name" ]] || {
    ragkb_env_error "$name/.env 的 RAGKB_DEPLOY_ENVIRONMENT 身份不一致"; return 1;
  }
  ENV_FILE=$env_file
  BACKEND_ENV_FILE=$backend_file
  RAGKB_ENV_ROOT=$root
  RAGKB_DEPLOY_ENVIRONMENT=$name
  RAGKB_ENV_SELECTED=1
  export ENV_FILE BACKEND_ENV_FILE RAGKB_ENV_ROOT RAGKB_DEPLOY_ENVIRONMENT RAGKB_ENV_SELECTED

  [[ "${COMPOSE_PROJECT_NAME:-}" == "$expected_project" ]] || {
    ragkb_env_error "$name/.env 的 COMPOSE_PROJECT_NAME 必须为 $expected_project"; return 1;
  }
  # staging 必须回环；两套环境发布端口与可选 TLS/监控端口不得相同。
  [[ "$name" != staging || "${WEB_BIND_IP:-}" == 127.0.0.1 ]] || {
    ragkb_env_error 'staging 必须绑定回环地址'; return 1;
  }
  [[ "$name" != staging || -z "${WEB_TLS_BIND_IP:-}" || "${WEB_TLS_BIND_IP}" == 127.0.0.1 ]] || {
    ragkb_env_error 'staging 的 WEB_TLS_BIND_IP 必须绑定回环地址'; return 1;
  }
  # 监控/Alertmanager 管理 API 的宿主发布地址也不能从 staging 配置改成公网。
  for key in RAGKB_PROMETHEUS_PUBLISH RAGKB_ALERTMANAGER_PUBLISH; do
    [[ "$name" != staging || -z "${!key:-}" || "${!key}" == 127.0.0.1 ]] || {
      ragkb_env_error "staging 的 $key 必须绑定回环地址"; return 1;
    }
  done
  # 所有实际可发布端口必须处于 1..65535；两套环境之间以及同一环境不同字段
  # 均不允许撞端口（即使绑定地址不同也按文档保持全局唯一）。可选项缺省时按
  # Compose 默认值（80/443/9090/19093）纳入比较，删掉变量也不会漏掉冲突。
  local -a port_keys=(WEB_PORT WEB_TLS_PORT RAGKB_PROMETHEUS_PORT RAGKB_ALERTMANAGER_PORT)
  local -a port_defaults=(80 443 9090 19093)
  local -a selected_ports=()
  local port_value port_key port_key_name other_env_for_ports other_port_value
  other_env_for_ports=$root/production/.env
  [[ "$name" != production ]] || other_env_for_ports=$root/staging/.env
  for ((i=0; i<${#port_keys[@]}; i++)); do
    key=${port_keys[i]}
    value=${!key:-}
    [[ -n "$value" ]] || value=${port_defaults[i]}
    [[ -n "$value" ]] || continue
    [[ "$value" =~ ^[0-9]{1,5}$ ]] && (( 10#$value >= 1 && 10#$value <= 65535 )) || {
      ragkb_env_error "$key 必须是 1-65535 的端口数字"; return 1;
    }
    selected_ports+=("$((10#$value))")
  done
  [[ ${#selected_ports[@]} -eq 0 || ${#selected_ports[@]} -eq $(printf '%s\n' "${selected_ports[@]}" | sort -u | wc -l) ]] || {
    ragkb_env_error "$name 环境的发布端口存在重复"; return 1;
  }
  if [[ -f "$other_env_for_ports" ]]; then
    for port_key in "${!port_keys[@]}"; do
      port_key_name=${port_keys[port_key]}
      other_port_value=$(ragkb_env_value "$other_env_for_ports" "$port_key_name" || true)
      [[ -n "$other_port_value" ]] || other_port_value=${port_defaults[port_key]}
      [[ -n "$other_port_value" ]] || continue
      [[ "$other_port_value" =~ ^[0-9]{1,5}$ ]] && (( 10#$other_port_value >= 1 && 10#$other_port_value <= 65535 )) || {
        ragkb_env_error "另一环境的 $port_key_name 必须是 1-65535 的端口数字"; return 1;
      }
      other_port_value=$((10#$other_port_value))
      for value in "${selected_ports[@]}"; do
        [[ "$other_port_value" != "$value" ]] || {
          ragkb_env_error "两个环境的发布端口冲突（$port_key_name）"; return 1;
        }
      done
    done
  fi

  # 同一环境的 API 和全部 worker 共享显式不可变镜像；两环境允许引用同一已验收产物。
  [[ -n "${BACKEND_IMAGE:-}" && -n "${FRONTEND_IMAGE:-}" ]] || {
    ragkb_env_error '必须为所选环境显式设置 BACKEND_IMAGE 与 FRONTEND_IMAGE'; return 1;
  }
  for key in BACKEND_IMAGE FRONTEND_IMAGE; do
    value=${!key}
    [[ "$value" != *REPLACE_WITH_* && "$value" != *:latest ]] || {
      ragkb_env_error "$key 必须填写不可变版本标签或 @sha256 摘要"; return 1;
    }
    # 只看最后一个 / 之后的镜像名；registry.example:5000/team/backend 里的
    # 5000 是 registry 端口，不能当成镜像标签。
    image_name=${value##*/}
    if [[ "$image_name" == *@sha256:* ]]; then
      [[ "$image_name" =~ ^[^@]+@sha256:[a-fA-F0-9]{64}$ ]] || {
        ragkb_env_error "$key 的 @sha256 摘要无效"; return 1;
      }
    else
      [[ "$image_name" == *:* && "$image_name" != *: ]] || {
        ragkb_env_error "$key 必须包含显式标签或 @sha256 摘要"; return 1;
      }
    fi
  done

  data=${DATA_DIR:-}
  [[ "$data" == /* ]] || { ragkb_env_error 'DATA_DIR 必须是绝对路径'; return 1; }
  canonical_data=$(ragkb_canonical_path "$data")
  [[ "$canonical_data" != / ]] || { ragkb_env_error 'DATA_DIR 不能是根目录'; return 1; }
  case "$canonical_data" in
    /etc|/etc/*|/usr|/usr/*|/bin|/bin/*|/sbin|/sbin/*|/var|/var/*|/proc|/proc/*|/sys|/sys/*|/dev|/dev/*)
      ragkb_env_error 'DATA_DIR 不能指向系统目录'; return 1 ;;
  esac
  backup=${BACKUP_DIR:-}
  [[ "$backup" == /* ]] || { ragkb_env_error 'BACKUP_DIR 必须是绝对路径'; return 1; }
  canonical_backup=$(ragkb_canonical_path "$backup")
  [[ "$canonical_backup" == "$canonical_data/backups" ]] || {
    ragkb_env_error 'BACKUP_DIR 必须解析为本环境 DATA_DIR/backups'; return 1;
  }

  # 只允许仓库已有的 base + host + 三个可选覆盖组合；profile/其它 compose 参数照常透传。
  [[ -n "${COMPOSE_FILE:-}" ]] || { ragkb_env_error 'COMPOSE_FILE 不能为空'; return 1; }
  IFS=: read -r -a compose_files <<< "$COMPOSE_FILE"
  [[ ${#compose_files[@]} -ge 2 ]] || {
    ragkb_env_error 'COMPOSE_FILE 至少包含基础文件与 host 资源覆盖'; return 1;
  }
  [[ "${compose_files[0]}" == docker-compose.yml && "${compose_files[1]}" == docker-compose.host.yml ]] || {
    ragkb_env_error 'COMPOSE_FILE 前两项必须是 docker-compose.yml:docker-compose.host.yml'; return 1;
  }
  for value in "${compose_files[@]:2}"; do
    case "$value" in
      docker-compose.https.yml) https_enabled=1 ;;
      docker-compose.monitoring.yml) monitoring_enabled=1 ;;
      docker-compose.alerting.yml) alerting_enabled=1 ;;
      *) ragkb_env_error "COMPOSE_FILE 含不允许的覆盖: $value"; return 1 ;;
    esac
  done
  [[ $alerting_enabled -eq 0 || $monitoring_enabled -eq 1 ]] || {
    ragkb_env_error '告警覆盖必须同时启用监控覆盖'; return 1;
  }
  # 仅为已启用的可选服务追加配额，不让核心部署凭空新增监控/告警容器。
  [[ $monitoring_enabled -eq 0 ]] || COMPOSE_FILE+=:docker-compose.host.monitoring.yml
  [[ $alerting_enabled -eq 0 ]] || COMPOSE_FILE+=:docker-compose.host.alerting.yml
  if [[ $https_enabled -eq 1 ]]; then
    RAGKB_ACME_WEBROOT=${RAGKB_ACME_WEBROOT:-$canonical_data/acme}
    export RAGKB_ACME_WEBROOT
  fi

  canonical_backend=$(ragkb_canonical_path "$backend_file")
  [[ "$canonical_backend" == "$dir/backend.env" ]] || {
    ragkb_env_error 'BACKEND_ENV_FILE 必须指向所选环境的 backend.env'; return 1;
  }

  # 与另一环境逐项比对 project、身份、数据路径、发布端口和应用配置。
  other=production
  [[ "$name" == production ]] && other=staging
  other_env="$root/$other/.env"
  other_backend="$root/$other/backend.env"
  canonical_other_data=
  if [[ -f "$other_env" ]]; then
    other_name=$(ragkb_env_value "$other_env" RAGKB_DEPLOY_ENVIRONMENT || true)
    other_project=$(ragkb_env_value "$other_env" COMPOSE_PROJECT_NAME || true)
    other_data=$(ragkb_env_value "$other_env" DATA_DIR || true)
    [[ "$other_name" != "$name" && "$other_project" != "$expected_project" ]] || {
      ragkb_env_error "与 $other 环境的项目/身份配置冲突（可能复制错模板）"; return 1;
    }
    if [[ -n "$other_data" && "$other_data" == /* ]]; then
      canonical_other_data=$(ragkb_canonical_path "$other_data")
      canonical_other=$(ragkb_canonical_path "$other_data")
      if ragkb_path_inside "$canonical_other" "$canonical_data" || \
         ragkb_path_inside "$canonical_data" "$canonical_other"; then
        ragkb_env_error '两个环境的 DATA_DIR 相同或嵌套'; return 1
      fi
      value=$(ragkb_env_value "$other_env" BACKUP_DIR || true)
      value=${value//'${DATA_DIR}'/$other_data}
      [[ -z "$value" ]] || {
        canonical_other_backup=$(ragkb_canonical_path "$value")
        [[ "$canonical_other_backup" != "$canonical_backup" ]] || {
          ragkb_env_error '两个环境的 BACKUP_DIR 相同'; return 1;
        }
      }
    fi
    for key in WEB_PORT WEB_TLS_PORT RAGKB_PROMETHEUS_PORT RAGKB_ALERTMANAGER_PORT; do
      value=$(ragkb_env_value "$other_env" "$key" || true)
      [[ -z "$value" || -z "${!key:-}" || "$value" != "${!key}" ]] || {
        ragkb_env_error "两个环境的 $key 相同"; return 1;
      }
    done
  fi
  if [[ -f "$other_backend" ]]; then
    canonical_other_backend=$(ragkb_canonical_path "$other_backend")
    [[ "$canonical_backend" != "$canonical_other_backend" ]] || {
      ragkb_env_error '两个环境共用 backend.env'; return 1;
    }
  fi

  # 明确的 legacy/跨环境路径拒绝；错误消息只含路径，不含配置内容或密钥。
  for key in BACKEND_ENV_FILE "${path_keys[@]}"; do
    value=${!key:-}
    [[ -n "$value" ]] || continue
    [[ "$value" == /* ]] || { ragkb_env_error "$key 必须是绝对路径"; return 1; }
    canonical_other=$(ragkb_canonical_path "$value")
    [[ "$canonical_other" != "$canonical_backend" || "$key" == BACKEND_ENV_FILE ]] || {
      ragkb_env_error "$key 不能指向应用配置文件"; return 1;
    }
    if [[ "$canonical_other" == "$root/legacy"* ]] || \
       [[ "$value" == backend/.env || "$value" == */backend/.env ]] || \
       { [[ "$key" != BACKEND_ENV_FILE ]] && [[ "$value" == "$dir/backend.env" ]]; }; then
      ragkb_env_error "$key 不能指向 legacy 或另一环境的配置"; return 1
    fi
    if ragkb_path_inside "$canonical_other" "$root/$other" || \
       ragkb_path_inside "$canonical_other" "$(ragkb_canonical_path "/srv/ragkb-data/$other")" || \
       { [[ "$key" != BACKEND_ENV_FILE && -n "$canonical_other_data" ]] && \
         ragkb_path_inside "$canonical_other" "$canonical_other_data"; }; then
      ragkb_env_error "$key 不能指向另一环境目录"; return 1
    fi
  done

  # 可选 TLS/监控/告警/异地备份路径：只做**跨环境**比较——所选环境任一字段
  # 与另一环境任一字段。目录-目录拒绝相同/包含；文件-文件拒绝相同；任一文件
  # 落入对方任一目录都拒绝（双向覆盖）。同一环境内部字段互不比较；字段缺席
  # 不参与；错误消息只带变量名，不输出路径或 secret。
  if [[ -f "$other_env" ]]; then
    local -a sel_keys=()
    local -a sel_paths=()
    local -a sel_is_dir=()
    local path flag
    for key in "${path_keys[@]}"; do
      ragkb_read_optional_path "$name" "$env_file" "$key" "$data" || return 1
      if [[ -n "$RAGKB_ENV_OPTIONAL_PATH" ]]; then
        sel_keys+=("$key")
        sel_paths+=("$RAGKB_ENV_OPTIONAL_PATH")
        sel_is_dir+=(selected)
      fi
      ragkb_read_optional_path "$other" "$other_env" "$key" "$other_data" || return 1
      if [[ -n "$RAGKB_ENV_OPTIONAL_PATH" ]]; then
        sel_keys+=("$key")
        sel_paths+=("$RAGKB_ENV_OPTIONAL_PATH")
        sel_is_dir+=(other)
      fi
    done
    local i_path j_path
    for ((i=0; i<${#sel_keys[@]}; i++)); do
      [[ ${sel_is_dir[i]} == selected ]] || continue
      key=${sel_keys[i]}
      i_path=${sel_paths[i]}
      if ragkb_is_dir_key "$key"; then flag=dir; else flag=file; fi
      for ((j=0; j<${#sel_keys[@]}; j++)); do
        [[ ${sel_is_dir[j]} == other ]] || continue
        other_key=${sel_keys[j]}
        j_path=${sel_paths[j]}
        if [[ $flag == dir ]]; then
          # 目录-目录：拒绝相同与任意一侧包含；目录-文件：拒绝文件落入目录。
          if ragkb_is_dir_key "$other_key"; then
            if ragkb_path_inside "$i_path" "$j_path" || ragkb_path_inside "$j_path" "$i_path"; then
              ragkb_env_error "两个环境的 $key/$other_key 目录重叠"; return 1
            fi
          elif ragkb_path_inside "$j_path" "$i_path"; then
            ragkb_env_error "另一环境的 $other_key 落入 $name 的 $key 目录"; return 1
          fi
        elif ragkb_is_dir_key "$other_key"; then
          ragkb_path_inside "$i_path" "$j_path" || continue
          ragkb_env_error "$name 的 $key 落入另一环境的 $other_key 目录"; return 1
        else
          [[ "$i_path" != "$j_path" ]] || {
            ragkb_env_error "两个环境的 $key/$other_key 文件相同"; return 1
          }
        fi
      done
    done
  fi

  # 已选配置的控制字段不能被父进程或 source 内容再次改写；非密钥数据库身份
  # 保留给维护脚本的 pg_dump/psql，应用密钥仍只从所选 backend.env 读取。
  COMPOSE_PROJECT_NAME=$expected_project
  DATA_DIR=$data
  BACKUP_DIR=$backup
  export ENV_FILE BACKEND_ENV_FILE COMPOSE_PROJECT_NAME COMPOSE_FILE \
         RAGKB_DEPLOY_ENVIRONMENT DATA_DIR BACKUP_DIR
  unset COMPOSE_PATH_SEPARATOR COMPOSE_ENV_FILES COMPOSE_DISABLE_ENV_FILE COMPOSE_PROFILES
  unset POSTGRES_PASSWORD JWT_SECRET ADMIN_PASSWORD \
        EMBEDDING_API_KEY LLM_API_KEY RERANK_API_KEY METRICS_TOKEN
  for key in POSTGRES_USER POSTGRES_DB; do
    [[ -z "${!key:-}" ]] || export "$key"
  done
}
