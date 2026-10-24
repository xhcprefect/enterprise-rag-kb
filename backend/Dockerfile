# 后端镜像：API 与入库 worker 共用同一个镜像，只是启动命令不同。
#
# 基础镜像用 slim 而非 alpine：pymupdf（自带 MuPDF）和 onnxruntime 都依赖 glibc
# 的 manylinux 轮子，alpine 的 musl 上没有可用轮子，会退化成源码编译。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 国内构建必读：Debian 官方源在境外，实测 9.7MB 的 Packages 索引要拉数分钟，
# pip 那 98 个包更慢。默认换成腾讯云镜像（本机在腾讯云内网，最快）。
# 需要回退官方源或换别家时用 build-arg 覆盖，例如：
#   docker compose build --build-arg APT_MIRROR=deb.debian.org api
ARG APT_MIRROR=mirrors.cloud.tencent.com

# onnxruntime（pymupdf 的传递依赖）需要 libgomp1；curl 供镜像自检与健康检查使用。
# sed 的两个细节：
#   1. 先替换 debian-security 再替换 debian，否则前者的 URL 会被后者先吃掉一半；
#   2. python:3.12-slim 当前基于 trixie，源文件是 deb822 格式的 debian.sources；
#      若基础镜像退回 bookworm（老格式 sources.list），一并处理，两边都不存在时忽略错误。
RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        [ -f "$f" ] && sed -i \
            -e "s|http://deb.debian.org/debian-security|http://${APT_MIRROR}/debian-security|g" \
            -e "s|http://security.debian.org/debian-security|http://${APT_MIRROR}/debian-security|g" \
            -e "s|http://deb.debian.org/debian|http://${APT_MIRROR}/debian|g" \
            "$f" || true; \
    done \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只拷贝依赖清单，让依赖层在源码变动时仍然命中缓存。
# requirements.lock 里是 98 个精确版本，换索引源只影响下载速度，不改变解析结果。
ARG PIP_INDEX=https://mirrors.cloud.tencent.com/pypi/simple
COPY requirements.lock ./
RUN pip install --index-url "${PIP_INDEX}" -r requirements.lock

COPY . .

# 容器内不使用 .env（配置由 compose 的 env_file / environment 注入），
# 也不带开发产物，这里兜底清掉，避免有人把 .env 打进镜像。
# /data/* 在这里建好并授权，是为了没挂卷时也能跑；挂 bind mount 后宿主机目录的
# 属主会覆盖它，所以 deploy/init.sh 还会再把宿主机目录 chown 到 1000。
RUN rm -f .env .tok \
    && mkdir -p /data/uploads /data/logs \
    && useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

# 默认命令是 API；worker 在 compose 里覆盖 command。
# 注意不能用 run.py——它带 reload=True，是开发入口。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
