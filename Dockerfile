# Multi-stage: build the UI, then serve it as static files from FastAPI in a
# single container. One image, one port, one URL.
#
# Target is Hugging Face Spaces (Docker SDK), which expects the app on 7860
# and injects Space secrets as environment variables. Nothing here is
# Spaces-specific beyond the port default -- PORT is still honoured, so the
# same image runs anywhere.
#
# The stage simulator runs INSIDE this container on a background thread
# started by the FastAPI lifespan, so telemetry flows whenever the Space is
# awake. It does not depend on anyone's laptop being on.

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

# Hugging Face Spaces expects 7860. Any host supplying PORT overrides it.
ENV PORT=7860
EXPOSE 7860

# Non-root, UID 1000 to match the Spaces runtime. The simulator and the MCP
# server both run fine unprivileged.
RUN useradd --create-home --uid 1000 stage && chown -R stage:stage /app
USER stage
ENV HOME=/home/stage

# Exactly one worker, deliberately. Each worker process would construct its
# own StageEmitter and write the same Prometheus series concurrently, which
# Mimir sees as out-of-order samples rather than as an obvious failure.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
