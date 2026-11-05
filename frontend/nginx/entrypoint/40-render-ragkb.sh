#!/bin/sh
# 由 nginx 官方入口脚本在启动前执行（/docker-entrypoint.d/40-render-ragkb.sh）。
#
# 职责：把需要“先校验再落进 nginx 语法”的值渲染成 include 片段：
#   - HTTP 模式：只写一个空 HSTS 片段，其余配置原样；
#   - HTTPS 模式：校验域名与证书文件，再写 server_name / 301 目标 / HSTS。
#
# 任何缺失或非法值都以非零退出，让容器启动失败而不是带着半截配置对外服务。
# 本脚本只读取环境变量与证书文件，不打印证书或私钥内容。
set -eu

ME=40-render-ragkb
log() { [ -n "${NGINX_ENTRYPOINT_QUIET_LOGS:-}" ] || echo "$ME: $*"; }
fail() { echo "$ME: 配置错误: $*" >&2; exit 1; }

mode=${RAGKB_TLS_MODE:-http}
case "$mode" in
  http|https) ;;
  *) fail "RAGKB_TLS_MODE 只能是 http 或 https（当前: $mode）" ;;
esac

runtime=/etc/nginx/ragkb-runtime
mkdir -p "$runtime" || fail "无法创建运行期配置目录 $runtime"

# 先写空 HSTS：HTTP 模式不发送 Strict-Transport-Security，但 include 必须存在，
# 否则每个自带 add_header 的 location 都会让 nginx 启动失败。
: > "$runtime/hsts.conf" || fail "无法写入 $runtime/hsts.conf"

# 可信代理：默认不信任任何上游，保持现有“不反射客户端 XFF”的安全默认。
# 仅在显式配置时渲染 set_real_ip_from；逐项校验，拒绝任何可能改变语法的字符。
: > "$runtime/real-ip.conf" || fail "无法写入 $runtime/real-ip.conf"
trusted=${RAGKB_TRUSTED_PROXIES:-}
if [ -n "$trusted" ]; then
  old_ifs=$IFS
  IFS=','
  set -f
  for entry in $trusted; do
    IFS=$old_ifs
    # 去空白后用白名单字符集校验（IPv4/IPv6/CIDR 只含这些字符）
    entry=$(printf '%s' "$entry" | tr -d ' \t')
    [ -n "$entry" ] || continue
    printf '%s' "$entry" | grep -Eq '^[0-9a-fA-F:.]+(/[0-9]{1,3})?$' \
      || fail "RAGKB_TRUSTED_PROXIES 含非法条目: $entry"
    printf 'set_real_ip_from %s;\n' "$entry" >> "$runtime/real-ip.conf" || fail "无法写入 $runtime/real-ip.conf"
    IFS=','
  done
  IFS=$old_ifs
  set +f
  printf 'real_ip_header X-Forwarded-For;\nreal_ip_recursive on;\n' >> "$runtime/real-ip.conf"
  log "启用可信代理: $trusted（只会信任这些地址转发的 X-Forwarded-For）"
fi

# 部署期配置校验：构建阶段刻意不跑 nginx -t（构建网络里没有 api 服务，
# upstream 解析必然失败）。到这里片段已渲染、Compose 网络里 api 已就绪，
# 校验才有意义；失败就让容器启动失败，而不是带着坏配置对外服务。
check_nginx() {
  nginx -t || fail "nginx 配置校验失败（片段已渲染，但语法或 upstream 解析不通过）"
}

if [ "$mode" = "http" ]; then
  log "HTTP 模式：未启用 TLS"
  check_nginx
  log "nginx -t 通过"
  exit 0
fi

name=${RAGKB_SERVER_NAME:-}
[ -n "$name" ] || fail "HTTPS 模式必须设置 RAGKB_SERVER_NAME"
[ "${#name}" -le 253 ] || fail "RAGKB_SERVER_NAME 长度超过 253"
# 只允许小写 DNS 标签：字母数字与连字符，至少两段，TLD 纯字母。
# 显式拒绝通配符、下划线、空白、引号、分号、$、大括号等可影响 nginx 语法的字符。
printf '%s' "$name" \
  | grep -Eq '^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$' \
  || fail "RAGKB_SERVER_NAME 不是合法的小写 DNS 域名: $name"

cert=${RAGKB_TLS_CERT:-/etc/nginx/certs/fullchain.pem}
key=${RAGKB_TLS_KEY:-/etc/nginx/certs/privkey.pem}
[ -f "$cert" ] && [ -r "$cert" ] || fail "证书文件不存在或不可读: $cert（应只读挂载，不要放进仓库）"
[ -f "$key" ] && [ -r "$key" ] || fail "私钥文件不存在或不可读: $key（应只读挂载，不要放进仓库）"
grep -q 'BEGIN CERTIFICATE' "$cert" || fail "证书文件不含 PEM 证书: $cert"
grep -q 'PRIVATE KEY' "$key" || fail "私钥文件不含 PEM 私钥: $key"

hsts_age=${RAGKB_HSTS_MAX_AGE:-31536000}
printf '%s' "$hsts_age" | grep -Eq '^[0-9]{1,9}$' || fail "RAGKB_HSTS_MAX_AGE 必须是 0-999999999 的整数秒"

acme_root=${RAGKB_ACME_WEBROOT:-/var/www/certbot}
if [ ! -d "$acme_root" ]; then
  log "提示: ACME webroot 目录 $acme_root 不存在，证书续期将不可用（不影响已有证书启动）"
fi

printf 'server_name %s;\n' "$name" > "$runtime/server-name.conf"
printf 'return 301 https://%s$request_uri;\n' "$name" > "$runtime/redirect.conf"
printf 'add_header Strict-Transport-Security "max-age=%s" always;\n' "$hsts_age" > "$runtime/hsts.conf"

log "HTTPS 模式：域名 $name，证书 $cert"

# 部署期配置校验：此时片段已按校验过的值渲染，且 api 已由 depends_on 就绪，
# upstream 可解析。构建阶段刻意不跑 nginx -t（构建网络里没有 api 服务）。
nginx -t || fail "nginx 配置校验失败（片段已渲染，但语法或 upstream 解析不通过）"
log "nginx -t 通过"
