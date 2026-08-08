# 关系洞察看板：独立于主微信容器的离线分析 + 只读 Web 服务。
#
# 构建上下文是仓库根目录，因为镜像里同时需要 wechat_history（只用它的 reader 链）
# 和 wechat_insights：
#   docker build -f integrations/wechat-insights/Dockerfile -t wechat-insights:latest .
FROM python:3.12-slim

# 与主容器 compose 里的 PUID/PGID 默认值保持一致。密钥文件是 0600 且属于主容器的
# abc 用户，这里的运行 uid 必须对得上，否则读不到密钥。
ARG PUID=1000
ARG PGID=100

# ECharts 在构建时下载并 vendor 进镜像，运行时不依赖任何 CDN。
ARG ECHARTS_VERSION=5.6.0
ARG ECHARTS_SHA256=e1b82487dd7f8d9f0438eec1f1433051148e0095ac7a15d4db890afda45cf610

ENV PYTHONPATH=/opt/wechat-insights \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/wechat-insights

COPY integrations/wechat-insights/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir --only-binary=:all: --require-hashes \
        -r requirements.lock && \
    rm -f requirements.lock

RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    curl -fsSL --retry 3 --retry-delay 5 -o /tmp/echarts.tgz \
        "https://registry.npmjs.org/echarts/-/echarts-${ECHARTS_VERSION}.tgz"; \
    echo "${ECHARTS_SHA256}  /tmp/echarts.tgz" | sha256sum -c -; \
    mkdir -p /opt/wechat-insights/wechat_insights/static/vendor; \
    tar -xzf /tmp/echarts.tgz -C /tmp package/dist/echarts.min.js package/LICENSE; \
    mv /tmp/package/dist/echarts.min.js \
        /opt/wechat-insights/wechat_insights/static/vendor/echarts.min.js; \
    mv /tmp/package/LICENSE \
        /opt/wechat-insights/wechat_insights/static/vendor/LICENSE.echarts; \
    rm -rf /tmp/echarts.tgz /tmp/package; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

# 解密、快照、账户白名单与私聊过滤全部复用主仓库的这个包，本镜像不重复实现。
COPY integrations/wechat-history/wechat_history ./wechat_history
COPY integrations/wechat-insights/wechat_insights ./wechat_insights
COPY integrations/wechat-insights/tests ./tests

# 命名卷首次创建时会继承镜像里同路径目录的属主，分析结果因此能被非 root 进程写入。
RUN mkdir -p /data && chown "${PUID}:${PGID}" /data

USER ${PUID}:${PGID}
EXPOSE 8300

CMD ["python3", "-m", "wechat_insights"]
