# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM ghcr.io/jordiboehme/crystalline:0.31.0@sha256:592898693eebdbac14f53b9f3580aec8181c7378b8f2fe63df4e82f286fd658d AS crystalline
FROM python:3.15.2-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0
COPY pyproject.toml uv.lock README.md ./
COPY marabrain ./marabrain
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.15.2-slim AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HOME=/data/home XDG_CONFIG_HOME=/data/config XDG_STATE_HOME=/data/state XDG_CACHE_HOME=/data/cache \
    MARABRAIN_TRANSPORT=http MARABRAIN_HOST=0.0.0.0 MARABRAIN_PORT=9110
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=crystalline /usr/local/bin/crystalline /usr/local/bin/crystalline
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p /data/home /data/config /data/state /data/cache && chown -R 65532:65532 /data
VOLUME /data
USER 65532:65532
EXPOSE 9110
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import json, os, urllib.request; from marabrain.config import Settings; secret = Settings().bearer_secret; assert secret is not None; req = urllib.request.Request('http://127.0.0.1:' + os.environ['MARABRAIN_PORT'] + '/mcp', data=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'health','arguments':{}}}).encode(), headers={'Authorization':'Bearer ' + secret.get_secret_value(),'Content-Type':'application/json','Accept':'application/json, text/event-stream'}); response = urllib.request.urlopen(req, timeout=3); body = response.read().decode(); assert response.status == 200 and '\"status\":\"ok\"' in body.replace(' ', '')"]
ENTRYPOINT ["env", "MARABRAIN_TRANSPORT=http", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["marabrain"]
