"""FastAPI application entrypoint.

Mounts the webhook, fault-injection and SSE stream routers, exposes
``GET /health``, and in production serves the built UI as static files from
the same container.

The stage simulator starts with the app so that a fault injected from the UI
changes the telemetry the agent is about to read. One process, one container,
one URL -- a stranger opens the page, presses a button, and watches.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api.routes_faults import router as faults_router
from api.routes_stream import router as stream_router
from api.routes_webhook import router as webhook_router
from api.runtime import stage_runner, stage_status
from simulator.otlp_client import configure_stdout_logging

log = logging.getLogger("api.main")

UI_DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_dotenv()
    configure_stdout_logging()
    log.info("starting volume-ops")
    stage_runner.start()
    try:
        yield
    finally:
        stage_runner.stop()
        log.info("volume-ops stopped")


app = FastAPI(
    title="Volume Ops",
    description=(
        "Agentic on-call for a virtual production LED volume stage. "
        "Telemetry is simulated and disclosed; the investigation is real."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# The UI is served from this same origin in the container. In development it
# runs on the Vite port, so allow it explicitly rather than using "*".
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(webhook_router)
app.include_router(faults_router)
app.include_router(stream_router)


@app.get("/health", tags=["ops"])
def health() -> dict[str, object]:
    """Liveness plus enough state to tell whether the demo is actually up.

    Deliberately free of identifiers. This endpoint is public on a deployed
    Space, so it reports whether each backend is *configured*, never which
    account or project it points at -- a GCP project id on an unauthenticated
    endpoint is an invitation to enumerate.
    """
    backend = (
        "vertex"
        if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() == "true"
        else "ai_studio"
    )
    return {
        "status": "ok",
        "simulator_running": stage_runner.running,
        "simulator_error": stage_runner.start_error,
        "ticks": stage_runner.ticks,
        "grafana_configured": bool(os.environ.get("GRAFANA_URL")),
        "llm_backend": backend,
        "llm_configured": bool(
            os.environ.get("GOOGLE_CLOUD_PROJECT")
            if backend == "vertex"
            else os.environ.get("GOOGLE_API_KEY")
        ),
    }


@app.get("/stage/status", tags=["stage"])
def stage() -> dict[str, object]:
    """Live stage signals for the UI panel."""
    return stage_status()


if UI_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=UI_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(UI_DIST / "index.html")
