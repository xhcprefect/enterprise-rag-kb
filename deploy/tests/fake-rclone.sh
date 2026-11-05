#!/usr/bin/env bash
# rclone 替身：用本地目录模拟远端，验证 remote-backup.sh 的顺序与失败行为。
# 不是加密实现，也不访问网络；只记录调用顺序供测试断言。
#
# 刻意复刻真实 crypt remote 的两个关键行为（v1.75.2 实测）：
#   1. crypt 没有可共同计算的哈希：普通 `check` 只打印
#      "No common hash found ... 1 hashes could not be checked" 并 **exit 0**，
#      即使内容不同、大小相同也会“通过”。
#   2. 加 `--download` 后会真解下载并逐字节比较，内容不同则 exit 1。
# 如果被测脚本误用普通 check，本替身就会像真 rclone 一样放行坏数据，
# 从而让回归测试真的能抓到这个缺陷。
set -euo pipefail
printf '%s\n' "$*" >> "${OPERATIONS_LOG:?}"

remote_root=${FAKE_RCLONE_ROOT:?需要 FAKE_RCLONE_ROOT}
# 跳过全局参数 --config <file>
[[ "$1" == "--config" ]] && shift 2
cmd=$1; shift

type_of() { printf '%s' "${FAKE_RCLONE_TYPE:-crypt}"; }

case "$cmd" in
  config)
    [[ "$1" == "show" ]] || { echo "unsupported config $*" >&2; exit 9; }
    printf '[%s]\ntype = %s\n' "$2" "$(type_of)"
    ;;
  mkdir)
    [[ ${FAIL_REMOTE_MKDIR:-0} != 1 ]] || exit 81
    mkdir -p "$remote_root"
    ;;
  lsf)
    # lsf <dir> --files-only  —— 探测存在性；模拟网络/权限失败
    [[ ${FAIL_REMOTE_LSF:-0} != 1 ]] || { echo 'lsf: connection reset by peer' >&2; exit 90; }
    [[ -d "$remote_root" ]] || exit 0
    ls -1 "$remote_root"
    ;;
  copyto)
    [[ ${FAIL_REMOTE_COPY:-0} != 1 ]] || exit 82
    # 跳过 --retries N / --low-level-retries N 之类的选项，最后一个前项是源、后项是目标。
    args=()
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --retries|--low-level-retries) shift 2 ;;
        --*) shift ;;
        *) args+=("$1"); shift ;;
      esac
    done
    src=${args[0]}; dst=${args[1]}
    name=$(basename "$dst")
    # 记录上传顺序，测试断言清单最后（独立前缀，避免与顶部的原始参数行混淆）
    printf 'UPLOAD %s\n' "$name" >> "${OPERATIONS_LOG:?}"
    if [[ ${FAIL_REMOTE_MANIFEST:-0} == 1 && "$name" == manifest-* ]]; then exit 83; fi
    mkdir -p "$remote_root"
    cp "$src" "$remote_root/$name"
    ;;
  check)
    # check <local dir> <remote dir> --one-way --download --include /name
    [[ ${FAIL_REMOTE_CHECK:-0} != 1 ]] || exit 84
    local_dir=$1
    name=''
    download=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --include) name=${2#/}; shift 2 ;;
        --download) download=1; shift ;;
        *) shift ;;
      esac
    done
    if [[ $download -eq 0 ]]; then
      # 真实 crypt 的行为：无共同哈希 → 只报告无法校验，仍然 exit 0。
      printf 'NOTICE: %s: No common hash found - not using a hash for checks\n' "$name"
      printf 'NOTICE: %s: 1 hashes could not be checked\n' "$name"
      exit 0
    fi
    cmp -s "$local_dir/$name" "$remote_root/$name"
    ;;
  cat)
    [[ ${FAIL_REMOTE_CAT:-0} != 1 ]] || exit 91
    path=${1##*:}
    name=${path##*/}
    [[ -f "$remote_root/$name" ]] || exit 85
    cat "$remote_root/$name"
    ;;
  *) echo "意外的 rclone 调用: $cmd $*" >&2; exit 86 ;;
esac
