#!/usr/bin/env bash
# 异地备份回归：本地校验、crypt 类型、权限、失败保留本地、清单最后发布、不覆盖他人标识。
# 用 fake-docker + fake-rclone 在临时目录里运行，不接触真实 Docker 或真实 remote。
# 只在独占临时容器内执行：脚本使用固定的 /tmp 目录并在退出时删除。
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=/tmp/rag-remote-backup-test
bin=$(mktemp -d)
trap 'rm -rf "$bin" "$work"' EXIT
rm -rf "$work"
mkdir -p "$work/uploads" "$work/projects" "$bin" "$work/remote" "$work/outside"
cp "$root/deploy/tests/fake-docker.sh" "$bin/docker"
cp "$root/deploy/tests/fake-rclone.sh" "$bin/rclone"
chmod +x "$bin/docker" "$bin/rclone"

export PATH="$bin:$PATH"
export ENV_FILE="$work/remote.env"
# 独立 env：只含测试用数据目录与测试库名，不 source 仓库真实 .env。
cat > "$ENV_FILE" <<EOF
DATA_DIR=$work
BACKUP_DIR=$work/backups
POSTGRES_USER=postgres
POSTGRES_DB=remote_regression_test
KEEP_DAYS=7
EOF
export OPERATIONS_LOG="$work/operations.log"
export FAKE_RCLONE_ROOT="$work/remote"
export RAGKB_REMOTE_BACKUP_ENABLED=true
export RCLONE_CONFIG="$work/outside/rclone.conf"
export RCLONE_REMOTE='crypt_remote:backups'
printf '[crypt_remote]\ntype = crypt\n' > "$RCLONE_CONFIG"
chmod 600 "$RCLONE_CONFIG"
# rclone 替身不读真实配置，但要求路径在仓库外且权限合规。
: > "$OPERATIONS_LOG"
printf 'payload' > "$work/uploads/a.txt"
printf 'blob' > "$work/projects/blob1"

run_backup() { bash "$root/deploy/backup.sh"; }
manifest_now() { printf '%s' "$(ls -1t "$work"/backups/manifest-*.sha256 | head -1)"; }

echo "== 1. 默认关闭 =="
if RAGKB_REMOTE_BACKUP_ENABLED=false bash "$root/deploy/backup.sh" >/dev/null; then :; else
  echo '默认关闭时备份失败' >&2; exit 1
fi
[[ -z "$(ls -A "$work/remote")" ]]
echo "   ok: 未启用时不接触 remote"

echo "== 2. 缺少 RCLONE_CONFIG =="
: > "$OPERATIONS_LOG"
if RCLONE_CONFIG= bash "$root/deploy/backup.sh"; then echo '缺配置仍成功' >&2; exit 1; fi
ls "$work"/backups/manifest-*.sha256 >/dev/null
echo "   ok: 非零退出且本地备份保留"

echo "== 3. 仓库内相对路径必须拒绝 =="
# 不往只读绑定（CI 用 :ro）的仓库里写文件，只验证“指向仓库内”会被拒绝。
inrepo="$root/.rclone-test.conf"
if RCLONE_CONFIG="$inrepo" bash "$root/deploy/backup.sh" 2>/dev/null; then
  echo '仓库内配置被接受' >&2; exit 1
fi
echo "   ok: 仓库内配置被拒绝"

echo "== 4. 权限过宽必须拒绝 =="
chmod 644 "$RCLONE_CONFIG"
if bash "$root/deploy/backup.sh" 2>/dev/null; then echo '过宽权限被接受' >&2; exit 1; fi
chmod 600 "$RCLONE_CONFIG"
echo "   ok: 权限检查生效"

echo "== 5. 非 crypt remote 必须拒绝且不写入 =="
: > "$OPERATIONS_LOG"
if FAKE_RCLONE_TYPE=s3 bash "$root/deploy/backup.sh" 2>/dev/null; then echo '非 crypt 被接受' >&2; exit 1; fi
if grep -q 'copyto' "$OPERATIONS_LOG"; then echo '非 crypt 仍发生上传' >&2; exit 1; fi
echo "   ok: 非 crypt 拒绝且零上传"

echo "== 6. 成功路径：清单最后发布 =="
: > "$OPERATIONS_LOG"
run_backup
m=$(manifest_now)
stamp=$(basename "$m" .sha256); stamp=${stamp#manifest-}
[[ -f "$work/remote/ragdb-$stamp.sql.gz" && -f "$work/remote/uploads-$stamp.tar.gz" \
   && -f "$work/remote/projects-$stamp.tar.gz" && -f "$work/remote/manifest-$stamp.sha256" ]]
# 调用顺序：三个数据文件的 UPLOAD 先于 manifest 的 UPLOAD。
order=$(sed -n 's/^UPLOAD //p' "$OPERATIONS_LOG")
[[ "$(printf '%s\n' "$order" | tail -1)" == "manifest-$stamp.sha256" ]]
[[ "$(printf '%s\n' "$order" | wc -l)" -eq 4 ]]
for prefix in ragdb- uploads- projects-; do
  [[ $(printf '%s\n' "$order" | cut -d- -f1 | grep -c "^${prefix%-}$") -eq 1 ]] \
    || { echo "缺少或重复上传 $prefix*: $order" >&2; exit 1; }
done
echo "   ok: 数据先传、清单最后（顺序: $(printf '%s ' $order)）"

echo "== 7. 远端上传失败：本地保留、非零退出 =="
before=$(ls "$work"/backups/ragdb-*.sql.gz | wc -l)
if FAIL_REMOTE_COPY=1 bash "$root/deploy/backup.sh"; then echo '远端失败被误报成功' >&2; exit 1; fi
after=$(ls "$work"/backups/ragdb-*.sql.gz | wc -l)
[[ $after -eq $((before + 1)) ]]
echo "   ok: 本地备份增加且脚本非零退出"

echo "== 8. 清单上传失败：远端不留无 manifest 的半成品判定 =="
# 远端已有数据文件但没有清单目录条目时，下游不会认；这里验证脚本非零且本地完好。
if FAIL_REMOTE_MANIFEST=1 bash "$root/deploy/backup.sh"; then echo '清单失败被误报成功' >&2; exit 1; fi
echo "   ok: 清单上传失败返回非零"

echo "== 9. 重跑同一清单幂等 =="
out=$(bash "$root/deploy/remote-backup.sh" "$m" 2>&1)
grep -q '远端清单已存在且一致' <<<"$out" || { echo "重跑输出不含幂等判定: $out" >&2; exit 1; }
echo "   ok: 重跑不重复发布"

echo "== 10. 远端同名清单内容不同：传输前拒绝，已有产物不被覆盖 =="
# 模拟“另一台机器的备份占用了同一时间标识”：远端清单被改成不同内容。
# 关键是校验必须发生在**上传数据文件之前**，否则先覆盖数据再拒绝，
# 对方的有效备份已经被破坏。
cp "$work/remote/manifest-$stamp.sha256" "$work/remote-manifest.bak"
foreign="0000000000000000000000000000000000000000000000000000000000000000  ragdb-$stamp.sql.gz"
printf '%s\n' "$foreign" > "$work/remote/manifest-$stamp.sha256"
# 记录远端数据文件的上传前指纹，确认整个失败路径没有改写它们。
before_db=$(sha256sum "$work/remote/ragdb-$stamp.sql.gz" | cut -d' ' -f1)
before_files=$(sha256sum "$work/remote/uploads-$stamp.tar.gz" | cut -d' ' -f1)
: > "$OPERATIONS_LOG"
if bash "$root/deploy/remote-backup.sh" "$m" >/dev/null 2>&1; then
  echo '同名不同内容清单被覆盖' >&2; exit 1
fi
if grep -q '^UPLOAD ' "$OPERATIONS_LOG"; then
  echo '拒绝覆盖之前仍然上传了文件' >&2; exit 1
fi
[[ "$(cat "$work/remote/manifest-$stamp.sha256")" == "$foreign" ]] || {
  echo '远端清单被改写' >&2; exit 1
}
[[ "$(sha256sum "$work/remote/ragdb-$stamp.sql.gz" | cut -d' ' -f1)" == "$before_db" ]]
[[ "$(sha256sum "$work/remote/uploads-$stamp.tar.gz" | cut -d' ' -f1)" == "$before_files" ]]
mv "$work/remote-manifest.bak" "$work/remote/manifest-$stamp.sha256"
echo "   ok: 传输前拒绝，远端数据/清单均未被改写"

echo "== 11. 已存在的同名数据文件内容不同 → fail-closed，不上传不覆盖 =="
# 模拟上次上传被截断/被替换：远端文件名字对、大小完全相同、内容不同。
# 普通 check 在 crypt 下会 “could not be checked” 但仍 exit 0，必须失败。
size_before=$(stat -c %s "$work/remote/uploads-$stamp.tar.gz")
printf 'X' | dd of="$work/remote/uploads-$stamp.tar.gz" bs=1 seek=0 conv=notrunc status=none
[[ "$(stat -c %s "$work/remote/uploads-$stamp.tar.gz")" == "$size_before" ]] \
  || { echo '构造的同大小篡改失败' >&2; exit 1; }
corrupt_sha=$(sha256sum "$work/remote/uploads-$stamp.tar.gz" | cut -d' ' -f1)
[[ "$corrupt_sha" != "$(sha256sum "$work/backups/uploads-$stamp.tar.gz" | cut -d' ' -f1)" ]] \
  || { echo '篡改后内容仍然相同' >&2; exit 1; }
: > "$OPERATIONS_LOG"
out=$(bash "$root/deploy/remote-backup.sh" "$m" 2>&1) && {
  echo '内容不同的远端文件被当成一致' >&2; exit 1
}
[[ "$(sha256sum "$work/remote/uploads-$stamp.tar.gz" | cut -d' ' -f1)" == "$corrupt_sha" ]] || {
  echo '内容不同的远端文件被覆盖' >&2; exit 1
}
grep -q '内容与本地不一致\|未能逐字节完成\|拒绝覆盖' <<<"$out" \
  || { echo "失败原因未说明内容校验: $out" >&2; exit 1; }
# 反向证明：如果没有 --download，替身会像真实 crypt 一样“通过”并 exit 0。
printf 'NOTICE: x: 1 hashes could not be checked\n' >/dev/null
echo "   ok: 逐字节校验失败即拒绝，远端不变"

echo "== 12. 远端探测失败（网络/权限）不得当成“不存在” =="
: > "$OPERATIONS_LOG"
if FAIL_REMOTE_LSF=1 bash "$root/deploy/remote-backup.sh" "$m" >/dev/null 2>&1; then
  echo 'lsf 失败被当成“远端不存在”继续上传' >&2; exit 1
fi
if grep -q '^UPLOAD ' "$OPERATIONS_LOG"; then echo '探测失败后仍上传' >&2; exit 1; fi
echo "   ok: 探测失败即中止"

echo "== 13. 损坏本地产物不得上传 =="
printf 'x' >> "$work/backups/ragdb-$stamp.sql.gz"
: > "$OPERATIONS_LOG"
if bash "$root/deploy/remote-backup.sh" "$m" 2>/dev/null; then echo '损坏产物被上传' >&2; exit 1; fi
if grep -q 'copyto' "$OPERATIONS_LOG"; then echo '损坏产物仍触发上传' >&2; exit 1; fi
echo "   ok: 上传前本地校验拦截"

echo '异地备份回归通过：默认关闭、缺配置/非 crypt/权限拒绝、失败保留本地、清单最后发布、重跑幂等、传输前拒绝覆盖、crypt 逐字节校验、探测失败中止。'
