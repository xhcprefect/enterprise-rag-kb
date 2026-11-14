#!/usr/bin/env bash
# 演练备份三件套只读校验 helper。
#
# 只验证“演练自己生成的合成备份”的一致性，输入只有一个显式 backup 目录：
#   ragdb-drill.sql.gz / uploads-drill.tar.gz / projects-drill.tar.gz /
#   manifest-drill.sha256（清单一恰好 3 行：db + uploads + projects）。
# 不是通用恢复器，不信任外来归档；不能用来校验/导入生产备份。
#
# 边界：
#   - 纯只读文件检查：不调用 Docker、不读取任何 .env、不做数据库恢复、不写文件。
#   - 校验规则与 deploy/common.sh 的 manifest_kind / validate_manifest 对齐
#     （新格式三项），并额外要求固定 drill 文件名，避免不同批次混用；
#     归档路径检查比 common.sh 多拒绝 //（split("/") 抓不到的穿越形态）。
#   - sha256sum 支持 GNU 两种标准标记：hash SPACE SPACE filename（text）与
#     hash SPACE *filename（binary，本机 Windows 挂载盘输出）；两者都由
#     sha256sum --check 原样读取，文件名仍严格限定本演练的三个固定名。
#
# 用法：
#   drill-backup-check.sh <backup目录>   固定三件套 + 清单的全套校验
#                                       （演练在 DROP 原始数据前必须用这一种）
#   drill-backup-check.sh --help         打印帮助（不触碰文件）
#
# 退出码：0 校验通过；1 校验失败；2 用法/参数错误；--help 返回 0。
# 全部模式只读输入文件，不创建/改写任何文件；帮助与失败信息不回显环境凭据。
set -euo pipefail

db_name=ragdb-drill.sql.gz
files_name=uploads-drill.tar.gz
projects_name=projects-drill.tar.gz
manifest_name=manifest-drill.sha256
fail() { echo "drill-backup-check: FAIL: $*" >&2; exit 1; }
usage() { echo "drill-backup-check: 用法错误: $*" >&2; exit 2; }

# 清单结构：每行必须是“64位小写 hex + 两个空格/一个空格加星标 + 固定文件名”。
# 逐行用 index($0,$2) 取 FILENAME START，避免 awk 把 * 前缀当成一个外来文件名：
# 只认字面量 marker，不接受 hash<filename 或任意前缀；缺项/重复/外来/坏哈希都拒绝。
check_manifest_structure() { # manifest文件
  local manifest=$1
  [[ -f "$manifest" ]] || fail "清单不存在: $manifest"
  awk -v db="$db_name" -v files="$files_name" -v projects="$projects_name" '
    BEGIN { text = "  "; binary = " *" }
    {
      hash = substr($0, 1, 64)
      marker = substr($0, 65, 2)
      name = substr($0, 67)
      if (length($0) < 67 || length(hash) != 64 || hash !~ /^[a-f0-9]+$/) { bad = 1; next }
      if (marker != text && marker != binary) { bad = 1; next }
      if (name != db && name != files && name != projects) { bad = 1; next }
      if (seen[name]++) { bad = 1; next }
      if (name == db) d++
      else if (name == files) u++
      else p++
    }
    END {
      if (bad || NR != 3 || d != 1 || u != 1 || p != 1) {
        printf "清单结构无效（应恰好 3 行：db + uploads + projects；接受 text/binary 标记，拒绝缺项/重复/外来/坏哈希）: %s\n", ARGV[1] > "/dev/stderr"
        exit 1
      }
    }' "$manifest" || fail "清单结构校验未通过"
}

# 单个归档的结构校验（gzip 完整性、顶层目录、路径范围、链接类型、dump 标识）。
# 校验规则与 common.sh 的 validate_tree_archive / validate_database_backup 一致；
# 路径检查额外拒绝 //（split("/") 抓不到的穿越形态）。
check_archive_structure() { # 归档路径 顶层目录名(uploads|projects|db)
  local archive=$1 root=$2
  [[ -f "$archive" ]] || fail "归档不存在: $archive"
  if [[ "$root" == "db" ]]; then
    # gzip -t 先完整读取整个压缩流，是上游读取错误的真正把关；标识行检查用
    # grep -c 读完整个流（grep -q 会在首个匹配后提前退出，gunzip 随即收到
    # SIGPIPE，pipefail 会把完好的大 dump 误判成损坏）。这里不用 || true：
    # grep 无匹配（退出 1）或 gunzip 真失败（pipefail）都落到 markers=0，一并拒绝。
    gzip -t "$archive" || fail "数据库备份压缩包损坏: $archive"
    local markers
    markers=$(gunzip -c "$archive" | grep -c 'PostgreSQL database dump') || markers=0
    [[ "$markers" =~ ^[0-9]+$ && $markers -gt 0 ]] \
      || fail "数据库备份缺少 pg_dump 标识: $archive"
    return 0
  fi
  gzip -t "$archive" || fail "归档压缩包损坏（gzip 校验失败）: $archive"
  # 两次 tar 列表读同一份归档；结构完整要求顶层目录条目存在（与 common.sh 的
  # archive_has_root 一致），空归档不能只凭 SHA 清单通过。
  local listing
  listing=$(tar -tzf - < "$archive") || fail "归档列表读取失败: $archive"
  grep -Fxq "$root/" <<<"$listing" || grep -Fxq "$root" <<<"$listing" \
    || fail "归档缺少 $root/ 顶层目录条目: $archive"
  grep -v '^$' <<<"$listing" | awk -v root="$root" '
    $0 ~ /^\/|\/\// { bad = 1 }
    { n = split($0, a, "/"); if (a[1] != root) bad = 1; for (i = 1; i <= n; i++) if (a[i] == "..") bad = 1 }
    END { exit bad }' \
    || fail "归档只能包含 $root/ 下的相对路径（拒绝绝对路径、//、.. 与外来顶层目录）: $archive"
  tar -tvzf - < "$archive" | awk 'substr($0,1,1) != "-" && substr($0,1,1) != "d" { bad = 1 } END { exit bad }' \
    || fail "归档含链接或特殊文件: $archive"
  return 0
}

[[ $# -eq 1 ]] || usage '需要且只需要一个 backup 目录参数'
case "$1" in
  -h|--help)
    sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  -*) usage "未知参数: $1" ;;
esac
dir=$1
[[ -d "$dir" ]] || fail "备份目录不存在: $dir"
mf="$dir/$manifest_name"
check_manifest_structure "$mf"
check_archive_structure "$dir/$db_name" db
check_archive_structure "$dir/$files_name" uploads
check_archive_structure "$dir/$projects_name" projects
for f in "$db_name" "$files_name" "$projects_name" "$manifest_name"; do
  [[ -f "$dir/$f" ]] || fail "缺少备份产物: $dir/$f"
done
# sha256sum --check 自己解析 text/binary 两种 GNU 标记；这里不重写文件名。
(cd "$dir" && sha256sum --check "$manifest_name" --quiet) >/dev/null \
  || fail "清单 sha256 校验未通过（有文件被篡改或清单过期）"
echo "CHECK-OK: 三件套与清单完整（$dir/$manifest_name）"
