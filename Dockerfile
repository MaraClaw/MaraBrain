# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
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
    HOME=/tmp XDG_CACHE_HOME=/tmp/.cache XDG_CONFIG_HOME=/tmp/.config \
    MARABRAIN_TRANSPORT=http MARABRAIN_HOST=0.0.0.0 MARABRAIN_PORT=9110
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER 65532:65532
EXPOSE 9110
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["python", "-c", "import json, os, urllib.request; req = urllib.request.Request('http://127.0.0.1:' + os.environ['MARABRAIN_PORT'] + '/mcp', data=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'health','arguments':{}}}).encode(), headers={'Authorization':'Bearer ' + os.environ['MARABRAIN_BEARER_SECRET'],'Content-Type':'application/json','Accept':'application/json, text/event-stream'}); response = urllib.request.urlopen(req, timeout=3); body = response.read().decode(); assert response.status == 200 and '\"status\":\"ok\"' in body.replace(' ', '')"]
ENTRYPOINT ["env", "MARABRAIN_TRANSPORT=http"]
CMD ["marabrain"]
