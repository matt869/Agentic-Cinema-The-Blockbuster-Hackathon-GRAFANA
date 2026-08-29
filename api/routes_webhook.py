"""Grafana alert webhook.

``POST /webhook/alert`` receives the Grafana alert payload and triggers the
agent pipeline.

The investigation runs as a background task and returns immediately with an
id, because a Grafana webhook must not be held open for the length of an
eight-iteration investigation. The caller watches progress on
``GET /stream/investigation/{id}``.

Every step the agent takes is republished onto the event bus as it happens --
that translation from ADK events to SSE events is what the UI renders.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from agent.models import Alert
from agent.mcp_config import close_toolsets
from agent.root import SCORED_KEY, build_root_agent
from agent.triage import load_cached, save_cached
from api.runtime import event_bus, stage_runner

log = logging.getLogger("api.webhook")

router = APIRouter(tags=["webhook"])

APP_NAME = "volume_ops"
_session_service = InMemorySessionService()

#: Tool calls that carry investigation meaning, mapped to SSE event types.
_TOOL_EVENTS = {
    "propose_hypothesis": "hypothesis",
    "record_evidence": "evidence",
    "update_confidence": "confidence",
    "reject_hypothesis": "rejected",
    "record_finding": "finding",
    "conclude": "concluded",
}


class AlertAccepted(BaseModel):
    investigation_id: str
    stream_url: str
    alert: str
    #: When the degradation began -- from Grafana's startsAt, or the running
    #: fault's onset. This is what the scorer measures the affected window
    #: against, so it is worth being able to see it.
    fired_at: datetime


def _parse_starts_at(value: Any) -> datetime | None:
    """Grafana's ``startsAt``: RFC3339, sometimes with a trailing Z."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    # Grafana sends a zero time for alerts that have not started.
    return parsed if parsed.year > 1970 else None


def onset_time() -> datetime:
    """When the degradation actually began, for a UI-triggered investigation.

    The longest-running active fault is the real onset. Without this the alert
    is stamped "now", the affected window collapses to zero minutes, and every
    cost that depends on elapsed time comes out at $0.
    """
    now = datetime.now(timezone.utc)
    elapsed = [
        float(state.get("elapsed_s", 0.0))
        for state in stage_runner.fault_state().values()
        if state.get("active")
    ]
    return now - timedelta(seconds=max(elapsed)) if elapsed else now


def parse_grafana_alert(payload: dict[str, Any]) -> Alert:
    """Pull an Alert out of Grafana's webhook shape.

    Grafana posts ``{"alerts": [...]}`` for unified alerting but the shape
    varies by version, so fall back to the top level rather than failing.
    """
    alerts = payload.get("alerts") or []
    first = alerts[0] if alerts else payload
    labels = first.get("labels") or {}
    annotations = first.get("annotations") or {}
    name = (
        labels.get("alertname")
        or payload.get("title")
        or first.get("name")
        or "unnamed alert"
    )
    fired_at = _parse_starts_at(
        first.get("startsAt") or payload.get("startsAt")
    ) or onset_time()
    return Alert(
        rule_name=name,
        fired_at=fired_at,
        summary=annotations.get("summary") or annotations.get("description") or "",
        labels={str(k): str(v) for k, v in labels.items()},
        annotations={str(k): str(v) for k, v in annotations.items()},
        raw=payload,
    )


def _emit_from_event(investigation_id: str, event: Any) -> None:
    """Translate one ADK event into SSE events on the bus."""
    author = getattr(event, "author", "") or ""
    content = getattr(event, "content", None)
    for part in getattr(content, "parts", None) or []:
        call = getattr(part, "function_call", None)
        if call is not None:
            kind = _TOOL_EVENTS.get(call.name)
            if kind:
                event_bus.publish(
                    investigation_id,
                    {"type": kind, "author": author, "tool": call.name,
                     "args": dict(call.args or {})},
                )
            elif call.name:
                event_bus.publish(
                    investigation_id,
                    {"type": "query", "author": author, "tool": call.name,
                     "args": dict(call.args or {})},
                )
        text = getattr(part, "text", None)
        if text and text.strip():
            event_bus.publish(
                investigation_id,
                {"type": "thought", "author": author, "text": text.strip()[:1200]},
            )


async def run_investigation(investigation_id: str, alert: Alert) -> None:
    """Run the full pipeline, streaming every step onto the bus."""
    event_bus.publish(
        investigation_id,
        {"type": "started", "alert": alert.rule_name, "summary": alert.summary},
    )
    try:
        # Development only, and off unless VOLUME_OPS_CACHE_TRIAGE is set.
        cached_plan = load_cached(alert)
        runner = Runner(
            app_name=APP_NAME,
            agent=build_root_agent(include_triage=cached_plan is None),
            session_service=_session_service,
        )
        initial: dict[str, Any] = {"alert": alert.model_dump(mode="json")}
        if cached_plan is not None:
            initial["triage"] = cached_plan
            event_bus.publish(
                investigation_id,
                {"type": "thought", "author": "triage",
                 "text": "using cached triage plan (development mode)"},
            )
        session = await _session_service.create_session(
            app_name=APP_NAME,
            user_id="stage",
            session_id=investigation_id,
            state=initial,
        )
        message = types.Content(
            role="user",
            parts=[types.Part(text=(
                f"Grafana alert fired: {alert.rule_name}. "
                f"{alert.summary}\nLabels: {alert.labels}\n"
                "Investigate and report."
            ))],
        )
        async for event in runner.run_async(
            user_id="stage", session_id=session.id, new_message=message
        ):
            _emit_from_event(investigation_id, event)

        final = await _session_service.get_session(
            app_name=APP_NAME, user_id="stage", session_id=session.id
        )
        state = final.state if final else {}
        if cached_plan is None:
            save_cached(alert, state.get("triage"))
        event_bus.publish(
            investigation_id,
            {
                "type": "complete",
                "scored": state.get(SCORED_KEY),
                "brief": state.get("brief"),
            },
        )
    except Exception as exc:  # surfaced, never swallowed
        log.exception("investigation %s failed", investigation_id)
        event_bus.publish(
            investigation_id,
            {"type": "error", "error": f"{type(exc).__name__}: {exc}"},
        )
        event_bus.publish(investigation_id, {"type": "complete", "failed": True})
    finally:
        # Each investigation builds its own toolset, and each toolset owns an
        # mcp-grafana subprocess that outlives garbage collection.
        await close_toolsets()


@router.post("/webhook/alert", response_model=AlertAccepted)
async def receive_alert(
    payload: dict[str, Any], background: BackgroundTasks
) -> AlertAccepted:
    """Grafana alert intake. Starts an investigation and returns its id."""
    alert = parse_grafana_alert(payload)
    investigation_id = uuid.uuid4().hex[:12]
    log.info("alert received: %s -> investigation %s", alert.rule_name, investigation_id)
    # Pass the coroutine function itself. Handing Starlette an already-created
    # coroutine (or asyncio.create_task) runs it in a threadpool with no event
    # loop, where it is silently never awaited.
    background.add_task(run_investigation, investigation_id, alert)
    return AlertAccepted(
        investigation_id=investigation_id,
        stream_url=f"/stream/investigation/{investigation_id}",
        alert=alert.rule_name,
        fired_at=alert.fired_at,
    )
