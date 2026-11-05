#!/usr/bin/env bash
# 恢复演练的“守卫”回归：验证 dry-run 默认、owner 校验与项目复用拒绝。
# 用 Docker 替身运行，不创建任何真实资源；真实演练由主代理另行执行。
# 只在独占临时容器内执行：脚本使用固定的 /tmp 目录并在退出时删除。
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
work=/tmp/rag-drill-guard-test
bin=$(mktemp -d)
trap 'rm -rf "$bin" "$work"' EXIT
rm -rf "$work"
mkdir -p "$work" "$bin"
cp "$root/deploy/tests/fake-docker-drill.sh" "$bin/docker"
chmod +x "$bin/docker"
export PATH="$bin:$PATH"
export OPERATIONS_LOG="$work/operations.log"
export FAKE_DRILL_STATE="$work/state"
drill="$root/deploy/tests/restore-drill.sh"
: > "$OPERATIONS_LOG"

echo "== 1. 默认 dry-run 不调用 Docker =="
out=$(bash "$drill")
grep -q 'dry-run' <<<"$out"
[[ ! -s "$OPERATIONS_LOG" ]] || { echo 'dry-run 触碰了 Docker' >&2; exit 1; }
echo "   ok: dry-run 零副作用"

echo "== 2. 未知参数被拒绝 =="
if bash "$drill" --production >/dev/null 2>&1; then echo '未知参数被接受' >&2; exit 1; fi
echo "   ok: 拒绝非隔离参数"

echo "== 3. 非法行数被拒绝 =="
if bash "$drill" --isolated --rows 0 >/dev/null 2>&1; then echo '非法行数被接受' >&2; exit 1; fi
echo "   ok: 行数校验"

echo "== 4. up 失败时清理仍执行且报告保留 =="
: > "$OPERATIONS_LOG"; rm -rf "$work/state"
if FAKE_DRILL_UP_FAIL=1 bash "$drill" --isolated --keep >/dev/null 2>&1; then
  echo 'up 失败被误报成功' >&2; exit 1
fi
echo "   ok: 失败路径非零退出"

echo "== 5. 前置检查：项目已有别人的资源 → 拒绝复用 =="
: > "$OPERATIONS_LOG"; rm -rf "$work/state"
if FAKE_DRILL_FOREIGN_OWNER=someone-else bash "$drill" --isolated >/dev/null 2>&1; then
  echo '项目复用被接受' >&2; exit 1
fi
grep -q '拒绝复用' "$OPERATIONS_LOG" 2>/dev/null || true
if grep -qE 'rm -f |volume rm |network rm |compose down' "$OPERATIONS_LOG"; then
  echo '冲突检查阶段就删除了资源' >&2; exit 1
fi
echo "   ok: 项目冲突拒绝且零删除"

echo "== 6. 清理阶段：逐项核对 owner，不用 compose down -v =="
: > "$OPERATIONS_LOG"; rm -rf "$work/state"
# 前置检查为空，清理列举时才出现别人的资源：必须被 owner 守卫拦下。
if FAKE_DRILL_FOREIGN_OWNER=someone-else FAKE_DRILL_APPEAR_LATE=1 \
     bash "$drill" --isolated >/dev/null 2>&1; then
  echo '外部 owner 的资源被删除' >&2; exit 1
fi
if grep -qE 'rm -f fakecontainercid|volume rm fakevol|network rm fakenet|compose down' "$OPERATIONS_LOG"; then
  echo '执行了越权删除' >&2; exit 1
fi
grep -q -- '--filter label=com.docker.compose.project=' "$OPERATIONS_LOG" \
  || { echo '清理没有按项目列举资源' >&2; exit 1; }
echo "   ok: owner 守卫生效且从不调用 compose down"

echo "== 7. 脚本自身不 source 真实 .env =="
# 只检查可执行行（去掉注释），避免把说明文字当成读取行为。
if grep -vE '^[[:space:]]*#' "$drill" \
   | grep -nE '(^|[;&|[:space:]])(source|\.)[[:space:]]+[^<]*\.env|ENV_FILE' >/dev/null; then
  echo '演练脚本引用了 .env' >&2; exit 1
fi
# 反向确认：它确实用的是自建 drill.env（防止上面的检查因为写法变化而失效）。
grep -q 'envfile="$work/drill.env"' "$drill"
grep -q 'drill.env' "$drill"
echo "   ok: 只使用自建的 drill.env"

echo '演练守卫回归通过：默认 dry-run、参数校验、失败清理、项目冲突拒绝、owner 守卫、不读真实 .env。'
