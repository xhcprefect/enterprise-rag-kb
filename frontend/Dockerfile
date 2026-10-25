# 前端镜像：多阶段构建，产物连同 Nginx 配置一起打进最终镜像。
#
# 第 2 阶段的 nginx 配置与本仓库 deploy/nginx.conf 保持一致；
# 若你改了 nginx 配置，记得两处同步（或改成只挂载 deploy/nginx.conf）。

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

EXPOSE 80
# 官方 nginx 镜像自带前台启动的 CMD
