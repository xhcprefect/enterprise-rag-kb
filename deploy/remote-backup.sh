#!/usr/bin/env bash
# 异地备份上传（默认关闭）。把本地已发布且校验通过的成对产物上传到 rclone crypt remote。
#
# 设计约束：
#   1. 只接受 rclone **crypt** remote；rclone 配置必须在仓库外，且权限为 0600/0400。
#   2. 上传前重新校验本地成对产物与清单；任何一项不通过都不上传。
#   3. 远端按时间标识建独立目录（不 sync、不 delete），绝不覆盖其他日期的备份。
#   4. 数据文件先传并做远端校验，**manifest 最后发布**；远端消费者以 manifest 判定完整。
#   5. 远端失败不删除本地任何文件，脚本以非零退出。
#
# 手动重跑（同一对产物、同一目标目录，幂等）：
#   ./deploy/remote-backup.sh data/backups/manifest-20261024-141159-1234.sha256
#
# 由 backup.sh 在**服务已恢复**之后调用；也可单独执行做补传。
set -euo pipefail

usage() { echo "用法: $0 <manifest-*.sha256>" >&2; exit 2; }
[[ $# -eq 1 ]] || usage
MANIFEST=$(realpath "$1")
[[ -f "$MANIFEST" ]] || { echo '备份清单不存在' >&2; exit 1; }

# 复用维护锁与路径约定；common.sh 会 cd 到仓库根，故先取绝对路径。
source "$(dirname "$0")/common.sh"

enabled=${RAGKB_REMOTE_BACKUP_ENABLED:-false}
if [[ "$enabled" != "true" ]]; then
  echo '异地备份未启用（RAGKB_REMOTE_BACKUP_ENABLED 不是 true），跳过上传' >&2
  exit 0
fi

# ---------- 1. 本地产物校验（上传前最后一道） ----------
base=$(basename "$MANIFEST")
[[ "$base" =~ ^manifest-(.+)\.sha256$ ]] || { echo "清单文件名不合法: $base" >&2; exit 1; }
stamp=${BASH_REMATCH[1]}
dir=$(dirname "$MANIFEST")
db="$dir/ragdb-$stamp.sql.gz"
files="$dir/uploads-$stamp.tar.gz"
projects="$dir/projects-$stamp.tar.gz"
[[ -f "$db" && -f "$files" ]] || { echo "缺少成对产物: ragdb-$stamp.sql.gz / uploads-$stamp.tar.gz" >&2; exit 1; }
validate_database_backup "$db"
validate_upload_archive "$files"
# 新备份清单含 projects 项；旧清单只有两项。以清单为准，绝不按“文件存在”猜测，
# 避免把别人遗留的 projects 归档混进本次上传。
if grep -Eq '  projects-.*\.tar\.gz$' "$MANIFEST"; then
  [[ -f "$projects" ]] || { echo "清单声明了项目存储，但缺少 projects-$stamp.tar.gz" >&2; exit 1; }
  validate_projects_archive "$projects"
  validate_manifest "$MANIFEST" "$(basename "$db")" "$(basename "$files")" "$(basename "$projects")"
else
  projects=""
  validate_manifest "$MANIFEST" "$(basename "$db")" "$(basename "$files")"
fi

# ---------- 2. rclone 配置安全校验 ----------
command -v rclone >/dev/null || { echo '需要 rclone，拒绝未加密的远端上传' >&2; exit 1; }

config=${RCLONE_CONFIG:-}
[[ -n "$config" ]] || { echo '必须设置 RCLONE_CONFIG 指向仓库外的 rclone 配置文件' >&2; exit 1; }
[[ "$config" == /* ]] || { echo 'RCLONE_CONFIG 必须是绝对路径' >&2; exit 1; }
[[ -f "$config" ]] || { echo "rclone 配置不存在: $config" >&2; exit 1; }
repo_root=$(pwd -P)
config_real=$(realpath "$config")
case "$config_real" in
  "$repo_root"|"$repo_root"/*) echo 'rclone 配置不能放在仓库内，请移到仓库外并限制权限' >&2; exit 1 ;;
esac
# 权限必须禁止组/其他用户读写（0600 或 0400）。Windows 挂载盘可能不支持，
# 此时明确报错而不是默默接受。
perm=$(stat -c '%a' "$config" 2>/dev/null || echo '')
[[ -n "$perm" ]] || { echo "无法读取 rclone 配置权限: $config" >&2; exit 1; }
(( (8#$perm & 077) == 0 )) || { echo "rclone 配置权限过宽（$perm），请 chmod 600 $config" >&2; exit 1; }

remote=${RCLONE_REMOTE:-}
[[ "$remote" == *:* ]] || { echo 'RCLONE_REMOTE 必须形如 crypt_remote:path/to/backups' >&2; exit 1; }
remote_name=${remote%%:*}
remote_path=${remote#*:}
[[ "$remote_name" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo 'RCLONE_REMOTE 的远端名不合法' >&2; exit 1; }
[[ "$remote_path" =~ ^[A-Za-z0-9_./-]*$ ]] || { echo 'RCLONE_REMOTE 的路径含不支持的字符' >&2; exit 1; }

# 类型校验：只接受 crypt。`rclone config show` 的输出可能含混淆后的密钥，
# 因此只在内部判断，绝不回显整段配置。
if ! rclone --config "$config" config show "$remote_name" 2>/dev/null \
     | grep -Eq '^[[:space:]]*type[[:space:]]*=[[:space:]]*crypt[[:space:]]*$'; then
  echo "远端 $remote_name 不是 rclone crypt 类型；拒绝上传明文备份" >&2
  exit 1
fi

# ---------- 3. 上传：数据先、清单最后；不 sync、不 delete ----------
dest="${remote%/}/$stamp"
log() { echo "[remote-backup] $*"; }

log "目标: $remote_name:$remote_path/$stamp"
rclone --config "$config" mkdir "$dest" || { echo '远端目录创建失败' >&2; exit 1; }

# ---------- 3.1 存在性探测（区分“不存在”和“查询失败”） ----------
# 不用 `rclone cat ... 2>/dev/null` 判断是否存在：权限错误、网络中断、限流都会
# 让 cat 失败，一律当成“不存在”就会继续上传并覆盖别人的产物。这里用 lsf 明确
# 列出目录；lsf 本身失败就是硬错误。
remote_exists() { # 文件名
  local listing
  listing=$(rclone --config "$config" lsf "$dest/" --files-only 2>&1) \
    || { echo "无法列出远端目录 $dest（网络/权限/配置问题）: $listing" >&2; exit 1; }
  grep -Fxq "$1" <<<"$listing"
}

# ---------- 3.2 逐字节内容校验（fail-closed） ----------
# crypt remote 没有可供 rclone 计算的通用哈希：普通 `check` 只会输出
# "No common hash found / N hashes could not be checked" 并仍然 exit 0，
# 比大小相同就会被误判成“校验通过”（本地实测可复现）。
# 因此这里必须用 `--download` 把远端内容解密下载后逐字节比较；任何
# “could not be checked / no common hash” 字样都视为失败而不是通过。
remote_matches() { # 本地目录 文件名
  local out
  if ! out=$(rclone --config "$config" check "$1" "$dest" \
        --one-way --download --include "/$2" 2>&1); then
    echo "远端内容与本地不一致: $2" >&2
    printf '%s\n' "$out" >&2
    return 1
  fi
  if grep -qiE 'could not be checked|no common hash' <<<"$out"; then
    echo "远端校验未能逐字节完成（crypt 无共同哈希），按失败处理: $2" >&2
    printf '%s\n' "$out" >&2
    return 1
  fi
  return 0
}

# 先检查已发布的清单：同名但内容不同说明这个时间标识已属于另一次备份，
# 此时**在上传任何数据文件之前**就拒绝，避免先覆盖对方数据再拒绝。
manifest_present=0
if remote_exists "$base"; then
  if [[ "$(rclone --config "$config" cat "$dest/$base" 2>/dev/null)" != "$(cat "$MANIFEST")" ]]; then
    echo "远端已存在内容不同的 $base，拒绝覆盖；请换 RCLONE_REMOTE 或人工确认" >&2
    exit 1
  fi
  manifest_present=1
  log "远端清单已存在且一致，本次只补齐数据文件"
fi

upload() { # 本地文件 远端文件名
  rclone --config "$config" copyto --retries 3 --low-level-retries 5 \
    "$1" "$dest/$2" >/dev/null || { echo "上传失败: $2（本地文件保留）" >&2; exit 1; }
}

files_to_upload=("$db" "$files")
[[ -z "$projects" ]] || files_to_upload+=("$projects")
for pair in "${files_to_upload[@]}"; do
  name=$(basename "$pair")
  if remote_exists "$name"; then
    # 已存在就不再写：先逐字节确认内容相同，不同则拒绝覆盖（可能是同 stamp
    # 的另一份备份，或上次上传被截断）。相同则跳过重复上传。
    remote_matches "$dir" "$name" \
      || { echo "远端已存在内容不同的 $name，拒绝覆盖" >&2; exit 1; }
    log "远端 $name 已存在且内容一致，跳过重复上传"
    continue
  fi
  upload "$pair" "$name"
  remote_matches "$dir" "$name" || {
    echo "远端校验失败: $name（清单未发布，本地文件保留）" >&2; exit 1
  }
done
log "数据文件已在远端逐字节校验通过"

# ---------- 3.3 清单最后发布 ----------
if [[ $manifest_present -eq 1 ]]; then
  log "清单此前已发布，跳过重复发布"
else
  upload "$MANIFEST" "$base"
  # 发布后回读比对，确认远端消费者读到的清单与本地一致。
  remote=$(rclone --config "$config" cat "$dest/$base" 2>/dev/null) \
    || { echo '远端清单回读失败（本地文件保留）' >&2; exit 1; }
  [[ "$remote" == "$(cat "$MANIFEST")" ]] || {
    echo '远端清单回读不一致（本地文件保留）' >&2; exit 1
  }
  log "清单已最后发布"
fi

log "完成: $remote_name:$remote_path/$stamp/$base"
