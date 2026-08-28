# Multi-stage: build the UI, then serve it as static files from FastAPI in a
# single container. One image, one port, one URL.

# ---------------------------------------------------------------- UI build
FROM node:22-slim AS ui
WORKDIR /ui
COPY ui/package.json ui/package-lock.json* ./
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund
COPY ui/ ./
RUN npm run build

# ------------------------------------------------------- mcp-grafana binary
# The agent shells out to the official Grafana MCP server, so the binary has
# to be in the image. Pinned: a silent upgrade could rename a tool and break
# the curated allowlist.
FROM debian:bookworm-slim AS mcp
ARG MCP_GRAFANA_VERSION=1.2.0
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/mcp.tar.gz \
      "https://github.com/grafana/mcp-grafana/releases/download/v${MCP_GRAFANA_VERSION}/mcp-grafana_Linux_x86_64.tar.gz" \
    && tar -xzf /tmp/mcp.tar.gz -C /tmp \
    && install -m 0755 /tmp/mcp-grafana /usr/local/bin/mcp-grafana

# ------------------------------------------------------------------ runtime
FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=mcp /usr/local/bin/mcp-grafana /usr/local/bin/mcp-grafana
COPY agent/ ./agent/
COPY api/ ./api/
COPY simulator/ ./simulator/
COPY --from=ui /ui/dist ./ui/dist

# Resolved from PATH by agent/mcp_config.py.
ENV MCP_GRAFANA_BINARY=/usr/local/bin/mcp-grafana

# Cloud Run supplies PORT; default for local runs.
ENV PORT=8080
EXPOSE 8080

# Non-root. The simulator and MCP server both run fine unprivileged.
RUN useradd --create-home --uid 10001 stage && chown -R stage:stage /app
USER stage

CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
