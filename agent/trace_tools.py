"""Tools the investigator uses to record its reasoning as it goes.

These are the hook points that make the investigation watchable. Every call
mutates the investigation held in session state, which is what the SSE stream
renders live: a hypothesis appearing, a query running, a candidate being
struck through with its reason.

Two of them carry the properties the project is judged on. ``reject_hypothesis``
requires a reason and never deletes anything -- rejected candidates are
retained and shown. ``record_evidence`` is the only way an observation
survives, because the scorer drops any finding that has none.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext

STATE_KEY = "investigation"
CONFIDENCE_EXIT = 0.85
MIN_EVIDENCE_FOR_EXIT = 2


def new_investigation() -> dict[str, Any]:
    return {
        "hypotheses": [],
        "findings": [],
        "widened_window": False,
        "queries_run": 0,
    }


def _load(ctx: ToolContext) -> dict[str, Any]:
    return ctx.state.get(STATE_KEY) or new_investigation()


def _save(ctx: ToolContext, inv: dict[str, Any]) -> None:
    ctx.state[STATE_KEY] = inv


def _find(inv: dict[str, Any], hid: str) -> dict[str, Any] | None:
    return next((h for h in inv["hypotheses"] if h["id"] == hid), None)


def propose_hypothesis(
    statement: str, entity: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Record a candidate explanation before testing it.

    Args:
        statement: Falsifiable claim naming a mechanism, e.g. "node_07 lost
            its genlock reference and is free-running".
        entity: The specific entity involved, e.g. "node_07".
    """
    inv = _load(tool_context)
    hid = f"h{len(inv['hypotheses']) + 1}"
    inv["hypotheses"].append(
        {
            "id": hid,
            "statement": statement,
            "entity": entity,
            "status": "proposed",
            "confidence": 0.0,
            "evidence": [],
            "rejection_reason": "",
        }
    )
    _save(tool_context, inv)
    return {"hypothesis_id": hid, "status": "recorded"}


def record_evidence(
    hypothesis_id: str,
    source: str,
    query: str,
    result: str,
    from_outside_alert_window: bool,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Attach a concrete observation to a hypothesis.

    A finding with no evidence is dropped before it reaches the crew, so
    anything you want to survive must be recorded here.

    Args:
        hypothesis_id: The id returned by propose_hypothesis.
        source: The tool that produced it, e.g. "query_prometheus".
        query: The exact query you ran.
        result: What came back. Include actual numbers or log text.
        from_outside_alert_window: True if this came from a time range wider
            than the alert window.
    """
    inv = _load(tool_context)
    h = _find(inv, hypothesis_id)
    if h is None:
        return {"error": f"no hypothesis {hypothesis_id}"}
    h["evidence"].append(
        {
            "source": source,
            "query": query,
            "result": result[:2000],
            "outside_alert_window": bool(from_outside_alert_window),
        }
    )
    inv["queries_run"] += 1
    if from_outside_alert_window:
        inv["widened_window"] = True
    _save(tool_context, inv)
    return {"evidence_count": len(h["evidence"])}


def update_confidence(
    hypothesis_id: str, confidence: float, tool_context: ToolContext
) -> dict[str, Any]:
    """Set how strongly the evidence supports a hypothesis, 0.0 to 1.0."""
    inv = _load(tool_context)
    h = _find(inv, hypothesis_id)
    if h is None:
        return {"error": f"no hypothesis {hypothesis_id}"}
    h["confidence"] = max(0.0, min(1.0, float(confidence)))
    h["status"] = "confirmed" if h["confidence"] > CONFIDENCE_EXIT else "investigating"
    _save(tool_context, inv)
    ready = (
        h["confidence"] > CONFIDENCE_EXIT
        and len(h["evidence"]) >= MIN_EVIDENCE_FOR_EXIT
    )
    return {"confidence": h["confidence"], "meets_exit_criteria": ready}


def reject_hypothesis(
    hypothesis_id: str, reason: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Rule a hypothesis out, recording why.

    Never abandon a candidate silently. The reason is shown to the crew and is
    how they see the reasoning was sound. A timing argument -- "its errors
    began 21s after the drift had already started, so it cannot be the cause"
    -- is far more useful than "not supported".

    Args:
        hypothesis_id: The id returned by propose_hypothesis.
        reason: Why it is ruled out. Be specific and cite what you observed.
    """
    inv = _load(tool_context)
    h = _find(inv, hypothesis_id)
    if h is None:
        return {"error": f"no hypothesis {hypothesis_id}"}
    h["status"] = "rejected"
    h["rejection_reason"] = reason
    _save(tool_context, inv)
    return {"status": "rejected", "retained": True}


def record_finding(
    title: str,
    entity: str,
    signal: str,
    value: float,
    detail: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Record a conclusion with the measured value behind it.

    Args:
        title: Short description, e.g. "LED wall sync drift on node_07".
        entity: e.g. "node_07".
        signal: One of tracking_latency_ms, sync_drift_ms, vram_used_fraction,
            gpu_temp_celsius, calibration_confidence, queue_depth. The scorer
            matches on this exact string, so use it verbatim.
        value: The measured value. Use a fraction 0-1 for vram_used_fraction.
        detail: One sentence of context.
    """
    inv = _load(tool_context)
    inv["findings"].append(
        {
            "title": title,
            "entity": entity,
            "signal": signal,
            "value": float(value),
            "detail": detail,
        }
    )
    _save(tool_context, inv)
    return {"findings": len(inv["findings"])}


def conclude(reason: str, tool_context: ToolContext) -> dict[str, Any]:
    """End the investigation. Call this once you have a confirmed root cause.

    Args:
        reason: One sentence on why the investigation is complete.
    """
    inv = _load(tool_context)
    inv["conclusion"] = reason
    _save(tool_context, inv)
    tool_context.actions.escalate = True
    return {"status": "concluded", "reason": reason}


TRACE_TOOLS = (
    propose_hypothesis,
    record_evidence,
    update_confidence,
    reject_hypothesis,
    record_finding,
    conclude,
)
