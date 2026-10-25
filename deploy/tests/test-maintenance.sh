#!/usr/bin/env bash
# 隔离演练维护脚本失败路径；Docker CLI 被替身替代，绝不接触运行中的真实服务。
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
export ENV_FILE="$root/deploy/tests/fixture.env"
export OPERATIONS_LOG=/tmp/rag-maintenance-operations.log
work=/tmp/rag-maintenance-test
bin=$(mktemp -d)
trap 'rm -rf "$bin" "$work" "$OPERATIONS_LOG"' EXIT
rm -rf "$work"
mkdir -p "$work/uploads" "$bin"
cp "$root/deploy/tests/fake-docker.sh" "$bin/docker"
chmod +x "$bin/docker"
export PATH="$bin:$PATH"
printf 'original' > "$work/uploads/content.txt"
: > "$OPERATIONS_LOG"
bash "$root/deploy/backup.sh"
backups=("$work"/backups/ragdb-*.sql.gz)
files=("$work"/backups/uploads-*.tar.gz)
[[ ${#backups[@]} -eq 1 && ${#files[@]} -eq 1 ]]
grep -q 'stop --timeout 120' "$OPERATIONS_LOG"
grep -q 'start api worker' "$OPERATIONS_LOG"

: > "$OPERATIONS_LOG"
if FAIL_DUMP=1 bash "$root/deploy/backup.sh"; then echo '失败的 pg_dump 被误报成功' >&2; exit 1; fi
grep -q 'start api worker' "$OPERATIONS_LOG"
manifests=("$work"/backups/manifest-*.sha256)
[[ ${#manifests[@]} -eq 1 ]]

printf 'current' > "$work/uploads/content.txt"
: > "$OPERATIONS_LOG"
if printf 'yes\n' | FAIL_RESTORE=1 bash "$root/deploy/restore.sh" "${backups[0]}" "${files[0]}"; then
  echo '失败的 SQL 恢复被误报成功' >&2; exit 1
fi
[[ $(cat "$work/uploads/content.txt") == current ]]
grep -q -- '--single-transaction -v ON_ERROR_STOP=1' "$OPERATIONS_LOG"
grep -q 'start api worker' "$OPERATIONS_LOG"

printf 'yes\n' | bash "$root/deploy/restore.sh" "${backups[0]}" "${files[0]}"
[[ $(cat "$work/uploads/content.txt") == original ]]

# 损坏备份必须在停止服务前被拒绝。
printf 'broken' > "$work/broken.sql.gz"
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" "$work/broken.sql.gz"; then exit 1; fi
[[ ! -s "$OPERATIONS_LOG" ]]

# 缺项清单与含链接的归档都必须在停止服务前拒绝。
cp "${manifests[0]}" "$bin/manifest-original"
head -n 1 "$bin/manifest-original" > "${manifests[0]}"
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" "${backups[0]}" "${files[0]}"; then exit 1; fi
[[ ! -s "$OPERATIONS_LOG" ]]
cp "$bin/manifest-original" "${manifests[0]}"
mkdir -p "$bin/unsafe/uploads"
ln -s /etc/passwd "$bin/unsafe/uploads/escape"
tar -czf "$work/unsafe.tar.gz" -C "$bin/unsafe" uploads
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" "${backups[0]}" "$work/unsafe.tar.gz"; then exit 1; fi
[[ ! -s "$OPERATIONS_LOG" ]]

# 清单不匹配也必须在停止服务前被拒绝。
printf 'tamper' >> "${files[0]}"
: > "$OPERATIONS_LOG"
if printf 'yes\n' | bash "$root/deploy/restore.sh" "${backups[0]}" "${files[0]}"; then exit 1; fi
[[ ! -s "$OPERATIONS_LOG" ]]
echo '维护脚本回归通过：成功备份、失败恢复服务、SQL 失败回滚文件、成功恢复、损坏产物拒绝。'
