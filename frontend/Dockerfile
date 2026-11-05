# 前端镜像：多阶段构建，产物连同 Nginx 配置一起打进最终镜像。
#
# nginx 配置分成三部分，全部 COPY 进镜像，HTTPS 覆盖只是切换使用哪一份：
#   nginx.conf            默认 HTTP 入口（/etc/nginx/conf.d/app.conf）
#   nginx/tls.conf        可选 HTTPS 入口（docker-compose.https.yml 挂载覆盖）
#   nginx/snippets/       两套配置共用的 location / 代理 / TLS 参数
#   nginx/entrypoint/     启动前渲染校验过的域名、HSTS 与可信代理片段

FROM node:22-alpine AS build
WORKDIR /app

# 先用 lockfile 装依赖，源码变动时这层缓存仍然有效。
# package-lock.json 里的 165 条 resolved 已全部指向 registry.npmmirror.com，
# 国内可直接下载，无需改源；这里仍保留 registry 设置，便于换环境时覆盖。
ARG NPM_REGISTRY=https://registry.npmmirror.com
COPY package.json package-lock.json ./
RUN npm config set registry "${NPM_REGISTRY}" && npm ci

COPY . .
# npm run build 先做无产物类型检查，再运行 vite build
RUN npm run build

FROM nginx:1.27-alpine
RUN rm -f /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/app.conf
COPY nginx/snippets/ /etc/nginx/ragkb/snippets/
# TLS 入口也放进镜像（不启用）：HTTPS 覆盖会把仓库里的同名文件只读挂到
# /etc/nginx/conf.d/app.conf。镜像内留一份便于构建期校验 include 目标，
# 也避免运维只能依赖“挂了才生效”的路径来发现问题。
COPY nginx/tls.conf /etc/nginx/ragkb/tls.conf
RUN chmod 0644 /etc/nginx/ragkb/snippets/*.conf
COPY nginx/entrypoint/40-render-ragkb.sh /docker-entrypoint.d/40-render-ragkb.sh
# Git 在 Windows 上不保留可执行位，显式 chmod，避免入口脚本被静默跳过。
RUN chmod 0755 /docker-entrypoint.d/40-render-ragkb.sh

# 运行期片段的默认空值：保证 include 目标存在，入口脚本被跳过时也不会加载失败。
# 容器启动时 40-render-ragkb.sh 会用校验过的内容覆盖它们。
#
# 这里**刻意不执行 `nginx -t`**：默认配置里的 upstream 是 Compose 服务名
# `api:8000`，构建阶段没有该服务、也没有 Compose DNS，nginx 会以
# "host not found in upstream" 失败，导致全新镜像根本构建不出来（本地有缓存
# 时反而看不出来）。真正的配置校验放在部署期：容器启动时入口脚本渲染完片段
# 后执行 `nginx -t`，此时 api 已按 depends_on 就绪。
RUN mkdir -p /etc/nginx/ragkb-runtime \
 && : > /etc/nginx/ragkb-runtime/hsts.conf \
 && : > /etc/nginx/ragkb-runtime/real-ip.conf \
 && : > /etc/nginx/ragkb-runtime/server-name.conf \
 && : > /etc/nginx/ragkb-runtime/redirect.conf \
 && for conf in /etc/nginx/conf.d/app.conf /etc/nginx/ragkb/tls.conf /etc/nginx/ragkb/snippets/*.conf; do \
      incs=$( { grep -v '^[[:space:]]*#' "$conf" || true; } \
              | grep -oE 'include[[:space:]]+/etc/nginx/[^;[:space:]]+' \
              | sed 's/^include[[:space:]]*//' || true); \
      for inc in $incs; do \
        case "$inc" in *'*'*) continue ;; esac; \
        [ -e "$inc" ] || { echo "缺少 include 目标: $inc（来自 $conf）" >&2; exit 1; }; \
      done; \
    done

EXPOSE 80
# 官方 nginx 镜像自带前台启动的 CMD
