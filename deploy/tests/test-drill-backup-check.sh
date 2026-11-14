#!/usr/bin/env bash
# drill-backup-check.sh 的纯本地回归：用真实归档构造成功/缺失/篡改/清单缺项/重复/
# 外来等用例，全部输入是本测试自建目录里的合成产物；不调用 Docker、不读 .env。
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
check="$root/deploy/tests/drill-backup-check.sh"

# 只允许本测试自建的 mktemp 根；固定 prefix + 归属 guard 防止 cleanup 误删任意路径。
work=$(mktemp -d "${TMPDIR:-/tmp}/ragkb-drill-backup-check.XXXXXX")
work=$(cd "$work" && pwd -P)
owner_tag=$(basename "$work")
[[ "$owner_tag" == ragkb-drill-backup-check.* ]] || { echo '测试根目录前缀异常' >&2; exit 1; }
cleanup() {
  [[ "$(basename "$work")" == "$owner_tag" && -f "$work/.owned-by-this-test" ]] \
    || { echo "拒绝清理非本测试根目录: $work" >&2; exit 1; }
  rm -rf "$work"
}
trap cleanup EXIT
: > "$work/.owned-by-this-test"
mkdir -p "$work/src/uploads" "$work/src/projects"
printf 'demo' > "$work/src/uploads/a.txt"
printf 'blob' > "$work/src/projects/abc"
echo "   测试根: $work"

fail() { echo "FAIL: $*" >&2; exit 1; }
expect_ok() {
  local out
  if ! out=$(bash "$check" "$@" 2>&1); then
    fail "应通过却失败: $* —— 输出: $out"
  fi
  [[ "$out" == CHECK-OK* ]] || fail "输出应以 CHECK-OK 开头: $out"
}
expect_fail() {
  if out=$(bash "$check" "$@" 2>&1); then
    fail "应失败却通过: $* —— 输出: $out"
  fi
}
# 生成恰好 3 行的清单：marker 可选 text（默认 sha256sum 输出）或 binary/mixed。
# 每次都按当前归档真实字节重算，保证负例不是被过期哈希误拒。
make_manifest() { # 目录 模式(text|binary|mixed)
  local d=$1 mode=$2
  (cd "$d" && sha256sum ragdb-drill.sql.gz uploads-drill.tar.gz projects-drill.tar.gz \
     > manifest-drill.sha256)
  if [[ "$mode" == "binary" || "$mode" == "mixed" ]]; then
    (cd "$d" && sha256sum -b ragdb-drill.sql.gz uploads-drill.tar.gz projects-drill.tar.gz \
       > manifest-drill.binary)
    if [[ "$mode" == "binary" ]]; then
      mv "$d/manifest-drill.binary" "$d/manifest-drill.sha256"
    else
      # 混合：db 用 binary 标记，uploads/projects 保留 text 标记；分别按固定
      # 文件名取行，不能用 grep -v 过滤（会同时留下 text 版 db 行变成 4 行）。
      local bin_db text_up text_prj
      bin_db=$(grep -F '*ragdb-drill.sql.gz' "$d/manifest-drill.binary")
      text_up=$(grep -F '  uploads-drill.tar.gz' "$d/manifest-drill.sha256")
      text_prj=$(grep -F '  projects-drill.tar.gz' "$d/manifest-drill.sha256")
      printf '%s\n%s\n%s\n' "$bin_db" "$text_up" "$text_prj" > "$d/manifest-drill.sha256"
      rm -f "$d/manifest-drill.binary"
    fi
  fi
}
# 合成三件套：db 是带 pg_dump 标识的真实 gzip，uploads/projects 是真实文件归档。
make_batch() { # 目录 [模式]
  local d=$1 mode=${2:-text}
  mkdir -p "$d"
  (cd "$work/src" && tar -czf "$d/uploads-drill.tar.gz" uploads)
  (cd "$work/src" && tar -czf "$d/projects-drill.tar.gz" projects)
  { printf 'PostgreSQL database dump placeholder\n'; } > "$d/tmp.sql"
  gzip -6 -c "$d/tmp.sql" > "$d/ragdb-drill.sql.gz" && rm -f "$d/tmp.sql"
  make_manifest "$d" "$mode"
}
# 目录内所有文件的路径/大小/mtime/内容指纹，用于 helper 只读性验证。
fingerprint() { (cd "$1" && find . -type f -printf '%P %s %T@\n' | LC_ALL=C sort); }
content_hash() { (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum); }

echo "== 1. 完整三件套通过（text / binary / 混合标记清单） =="
make_batch "$work/good" text
expect_ok "$work/good"
make_manifest "$work/good" binary
expect_ok "$work/good"
make_manifest "$work/good" mixed
expect_ok "$work/good"
grep -q '\*ragdb-drill.sql.gz' "$work/good/manifest-drill.sha256" || fail '混合清单未包含 binary 标记'
grep -q '  uploads-drill.tar.gz' "$work/good/manifest-drill.sha256" || fail '混合清单未包含 text 标记'
echo "   ok: GNU text/binary/混合标记均 CHECK-OK"

echo "== 2. 固定文件名缺失 =="
d="$work/missing-db"; make_batch "$d"; rm -f "$d/ragdb-drill.sql.gz"
expect_fail "$d"
d="$work/missing-files"; make_batch "$d"; rm -f "$d/uploads-drill.tar.gz"
expect_fail "$d"
d="$work/missing-projects"; make_batch "$d"; rm -f "$d/projects-drill.tar.gz"
expect_fail "$d"
d="$work/missing-manifest"; make_batch "$d"; rm -f "$d/manifest-drill.sha256"
expect_fail "$d"
echo "   ok: 四类缺失全部拒绝"

echo "== 3. 归档内容篡改（清单 sha256 不再匹配） =="
d="$work/tamper-db"; make_batch "$d"; gunzip -c "$d/ragdb-drill.sql.gz" | sed 's/placeholder/tampered/' | gzip -6 > "$d/x.gz" && mv "$d/x.gz" "$d/ragdb-drill.sql.gz"
expect_fail "$d"
d="$work/tamper-up"; make_batch "$d"; printf 'extra' >> "$work/src/uploads/a.txt"
(cd "$work/src" && tar -czf "$work/tamper-up-new.tar.gz" uploads)
mv "$work/tamper-up-new.tar.gz" "$d/uploads-drill.tar.gz"
expect_fail "$d"
d="$work/tamper-prj"; make_batch "$d"; printf 'x' > "$work/src/projects/zzz"
(cd "$work/src" && tar -czf "$work/tamper-prj-new.tar.gz" projects)
mv "$work/tamper-prj-new.tar.gz" "$d/projects-drill.tar.gz"
rm -f "$work/src/projects/zzz"
expect_fail "$d"
echo "   ok: 三类归档篡改全部拒绝"

echo "== 4. 压缩包损坏与缺 pg_dump 标识（清单均重算匹配） =="
d="$work/corrupt-db"; make_batch "$d"; printf 'x' >> "$d/ragdb-drill.sql.gz"; make_manifest "$d" text
expect_fail "$d"
d="$work/not-dump"; make_batch "$d"; printf 'not a dump' | gzip -6 > "$d/ragdb-drill.sql.gz"; make_manifest "$d" text
expect_fail "$d"
d="$work/corrupt-up"; make_batch "$d"; printf 'x' >> "$d/uploads-drill.tar.gz"; make_manifest "$d" text
expect_fail "$d"
d="$work/corrupt-prj"; make_batch "$d"; printf 'x' >> "$d/projects-drill.tar.gz"; make_manifest "$d" text
expect_fail "$d"
echo "   ok: 损坏/伪 dump 归档拒绝（非哈希误拒）"

echo "== 5. 主审负例：结构对但内容/类型错，清单重新算好也必须拒绝 =="
d="$work/plain-db"; make_batch "$d"
printf 'not gzip at all\n' > "$d/ragdb-drill.sql.gz"
make_manifest "$d" text
expect_fail "$d"
d="$work/swapped"; make_batch "$d"
cp "$d/uploads-drill.tar.gz" "$d/projects-drill.tar.gz"
make_manifest "$d" binary
expect_fail "$d"
d="$work/gzip-no-dump"; make_batch "$d"
printf 'gzip ok, no dump marker' | gzip -6 > "$d/ragdb-drill.sql.gz"
make_manifest "$d" text
expect_fail "$d"
echo "   ok: 非 gzip SQL、归档冒充、无 dump 标识全部拒绝"

echo "== 6. 大 dump 完整流读取（grep -q 的 SIGPIPE 误判回归） =="
d="$work/big-dump"; make_batch "$d" mixed
# 4MiB 合法 dump：标识行在开头，后面是大正文；grep -q 会提前退出让 gunzip
# 收到 SIGPIPE（pipefail 下 rc=141），helper 用 grep -c 读完整个流必须 CHECK-OK。
python3 - "$d/ragdb-drill.sql.gz" <<'PY'
import gzip, sys
with gzip.open(sys.argv[1], 'wb') as f:
    f.write(b'--\n-- PostgreSQL database dump\n--\n\n' + b'x' * (4 * 1024 * 1024) + b'\n')
PY
make_manifest "$d" mixed
expect_ok "$d"
if gunzip -c "$d/ragdb-drill.sql.gz" | grep -q 'PostgreSQL database dump'; then
  : # 小缓冲场景可能不触发；下面单独断言完整 grep -c 路径。
fi
markers=$(gunzip -c "$d/ragdb-drill.sql.gz" | grep -c 'PostgreSQL database dump')
[[ "$markers" =~ ^[0-9]+$ && $markers -gt 0 ]] || fail '大 dump 标识行未读到'
echo "   ok: 4MiB 合法 dump 完整流读取 CHECK-OK（markers=$markers）"

echo "== 7. 归档缺顶层目录条目（清单重算匹配） =="
d="$work/no-root"; make_batch "$d"
python3 - "$work/no-root-new.tar.gz" <<'PY'
import io, sys, tarfile
out = sys.argv[1]
with tarfile.open(out, 'w:gz') as t:
    data = b'stray'
    ti = tarfile.TarInfo('projects/stray')
    ti.size = len(data)
    t.addfile(ti, io.BytesIO(data))
PY
mv "$work/no-root-new.tar.gz" "$d/projects-drill.tar.gz"
make_manifest "$d" text
expect_fail "$d"
echo "   ok: 缺 projects/ 顶层目录条目拒绝"

echo "== 8. 路径越界与链接（都保留合法顶层目录并重算清单） =="
d="$work/escape"; make_batch "$d"
python3 - "$work/src" "$work/escape-new.tar.gz" <<'PY'
import io, sys, tarfile
src, out = sys.argv[1], sys.argv[2]
with tarfile.open(out, 'w:gz') as t:
    t.add(src + '/uploads', arcname='uploads', recursive=True)
    data = b'evil'
    ti = tarfile.TarInfo('uploads/../evil')
    ti.size = len(data)
    t.addfile(ti, io.BytesIO(data))
PY
mv "$work/escape-new.tar.gz" "$d/uploads-drill.tar.gz"
make_manifest "$d" text
expect_fail "$d"
d="$work/slashslash"; make_batch "$d"
python3 - "$work/ss-new.tar.gz" <<'PY'
import io, sys, tarfile
out = sys.argv[1]
with tarfile.open(out, 'w:gz') as t:
    t.addfile(tarfile.TarInfo('uploads/'))
    t.addfile(tarfile.TarInfo('uploads/a.txt'))
    data = b'evil'
    ti = tarfile.TarInfo('uploads//evil')
    ti.size = len(data)
    t.addfile(ti, io.BytesIO(data))
PY
mv "$work/ss-new.tar.gz" "$d/uploads-drill.tar.gz"
make_manifest "$d" text
expect_fail "$d"
d="$work/link"; make_batch "$d"
ln -s /etc/passwd "$work/src/uploads/esc"
tar -czf "$work/link-new.tar.gz" -C "$work/src" uploads
mv "$work/link-new.tar.gz" "$d/uploads-drill.tar.gz"
rm -f "$work/src/uploads/esc"
make_manifest "$d" binary
expect_fail "$d"
echo "   ok: ../、// 穿越与 symlink 归档拒绝（非哈希/顶层误拒）"

echo "== 9. 清单缺项（legacy 两行） =="
d="$work/legacy"; make_batch "$d"
(cd "$d" && sha256sum ragdb-drill.sql.gz uploads-drill.tar.gz > manifest-drill.sha256)
expect_fail "$d"
echo "   ok: 两行旧格式清单拒绝"

echo "== 10. 清单重复条目 =="
d="$work/dup"; make_batch "$d"
(cd "$d" && sha256sum ragdb-drill.sql.gz uploads-drill.tar.gz projects-drill.tar.gz \
   ragdb-drill.sql.gz > manifest-drill.sha256)
expect_fail "$d"
echo "   ok: 重复条目拒绝"

echo "== 11. 清单外来条目 / 坏哈希 / 错误标记 =="
d="$work/foreign"; make_batch "$d" binary
printf '%s  other-drill.tar.gz\n' "$(printf z | sha256sum | cut -d' ' -f1)" >> "$d/manifest-drill.sha256"
expect_fail "$d"
d="$work/badhash"; make_batch "$d"
sed -i 's/^[0-9a-f]\{64\}/zzzz/' "$d/manifest-drill.sha256"
expect_fail "$d"
d="$work/badmarker"; make_batch "$d"
# 把 text 标记的两个空格改成单个空格：sha256sum -c 会把它当错误格式/坏文件名。
sed -i 's/^\([a-f0-9]\{64\}\)  /\1 /' "$d/manifest-drill.sha256"
expect_fail "$d"
d="$work/star-strip"; make_batch "$d" binary
# 只删掉任意 * 会变宽：这里把星标改成多余空格，结构必须拒绝而不是被当作文件名。
sed -i 's/ \*ragdb/   ragdb/' "$d/manifest-drill.sha256"
expect_fail "$d"
echo "   ok: 外来/坏哈希/错误标记/变宽标记拒绝"

echo "== 12. 精简 full CLI、退出码与只读性（成功/失败/帮助前后全比） =="
make_batch "$work/readonly" mixed
good_before=$(fingerprint "$work/readonly"; content_hash "$work/readonly")
[[ $(bash "$check" "$work/readonly" >/dev/null 2>&1; echo $?) -eq 0 ]] || fail '通过退出码应为 0'
good_after=$(fingerprint "$work/readonly"; content_hash "$work/readonly")
[[ "$good_before" == "$good_after" ]] || fail '成功 helper 调用改动了备份目录文件'
[[ $(bash "$check" "$work/missing-db" >/dev/null 2>&1; echo $?) -eq 1 ]] || fail '失败退出码应为 1'
[[ $(bash "$check" >/dev/null 2>&1; echo $?) -eq 2 ]] || fail '无参数退出码应为 2'
[[ $(bash "$check" --help >/dev/null 2>&1; echo $?) -eq 0 ]] || fail '--help 退出码应为 0'
[[ $(bash "$check" --file "$work/readonly/uploads-drill.tar.gz" uploads >/dev/null 2>&1; echo $?) -eq 2 ]] \
  || fail '已删除的 --file 子命令应报用法错误'
bad_before=$(fingerprint "$work/missing-db"; content_hash "$work/missing-db")
[[ $(bash "$check" "$work/missing-db" >/dev/null 2>&1; echo $?) -eq 1 ]] || fail '失败复测退出码应为 1'
bad_after=$(fingerprint "$work/missing-db"; content_hash "$work/missing-db")
[[ "$bad_before" == "$bad_after" ]] || fail '失败 helper 调用改动了备份目录文件'
echo "   ok: 只有 full 目录 CLI；0/1/2 边界与成功/失败前后 hash/mtime 全比一致"

echo 'drill-backup-check 回归通过：完整（text/binary/混合清单）/缺失/篡改/损坏/结构错/大 dump 完整流/路径/链接/清单缺项重复外来/退出码只读性。'
