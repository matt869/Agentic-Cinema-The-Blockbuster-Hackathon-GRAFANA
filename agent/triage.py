"""Alert intake.

An ``LlmAgent`` on a lite Gemini model that takes a Grafana alert payload, forms
the initial hypotheses, and identifies which signals the investigator should
examine first.

Triage holds no tools on purpose. It reads the alert and decides where to
look; the investigator does the querying. Keeping the two apart means the
cheap step stays cheap and the expensive step starts with a plan -- and it
lets triage run on its own model, so it draws from a separate daily quota
pool rather than competing with the investigation loop.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from google.adk.agents import LlmAgent

from agent.mcp_config import GRAFANA_QUERY_GUIDE
from agent.llm import TRIAGE_MODEL, build_model
from agent.models import Alert, TriageResult

log = logging.getLogger("agent.triage")


INSTRUCTION = f"""\
You are the on-call triage step for a virtual production LED volume stage --
the kind of film set used to shoot The Mandalorian. A live shooting crew is
standing on this stage right now and every minute of downtime costs $158.

You are given one Grafana alert. You do NOT have tools and you do NOT query
anything. Your job is to read the alert and produce a plan of attack.

{GRAFANA_QUERY_GUIDE}

WHAT THE PHYSICAL SYSTEM DOES
Render nodes drive the LED wall. Trackers feed camera position to the render
nodes so the background parallax matches camera movement.
  - If tracking drifts, the background is subtly wrong and the shot needs
    expensive VFX cleanup. Nobody notices on the day.
  - If a node loses genlock sync, its wall segment tears visibly on camera.
  - If a node runs out of VRAM, its wall segment goes black mid-take.
  - If cards overheat they throttle, and frames land late with no error at
    all -- silent quality decay that only shows up in the dailies.

HOW TO TRIAGE
1. Restate the alert in one plain sentence.
2. Name the specific entities worth examining. Be concrete: "node_07", not
   "the render nodes". If the alert names an entity, start there.
3. List the metrics to query, most informative first. Prefer signals that
   DISCRIMINATE between your hypotheses over signals that merely confirm the
   alert already fired.
4. Propose falsifiable hypotheses, most likely first. A hypothesis names an
   entity and a mechanism: "node_07 lost its genlock reference", not
   "there is a sync problem".
5. Decide whether the cause could predate the alert window. Set
   consider_wider_window to true whenever the symptom looks like a slow ramp,
   a leak, or anything that a change could have introduced hours earlier --
   a driver patch, a recalibration, a reposition, a config edit. Symptoms
   appear inside the alert window; causes very often do not.

Be specific and be brief. Every hypothesis you propose costs a query to test.
"""


def build_triage_agent() -> LlmAgent:
    """The triage agent. No tools -- reasoning over the alert payload only."""
    return LlmAgent(
        name="triage",
        model=build_model(TRIAGE_MODEL),
        description="Reads a Grafana alert and plans the investigation.",
        instruction=INSTRUCTION,
        output_schema=TriageResult,
        output_key="triage",
    )


# --------------------------------------------------------------------------
# Development-only triage cache
# --------------------------------------------------------------------------
#
# Triage is deterministic for a given alert: same payload, same plan. While
# debugging anything downstream -- the loop, the scorer, the brief -- paying
# for that call again on every run wastes a metered daily budget on a step
# that is not under test.
#
# Off unless VOLUME_OPS_CACHE_TRIAGE is set, so a demo or a judged run always
# does the real thing. The cache lives outside the package and is gitignored.

CACHE_ENABLED = os.environ.get("VOLUME_OPS_CACHE_TRIAGE", "").lower() in {"1", "true", "yes"}
_CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "triage.json"


def cache_key(alert: Alert) -> str:
    """Alerts with the same name and summary produce the same plan."""
    raw = f"{alert.rule_name}||{alert.summary}||{sorted(alert.labels.items())}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _read_cache() -> dict[str, Any]:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_cached(alert: Alert) -> dict[str, Any] | None:
    """A previously stored triage plan, or None."""
    if not CACHE_ENABLED:
        return None
    hit = _read_cache().get(cache_key(alert))
    if hit is not None:
        log.info("triage cache hit for %r -- skipping the call", alert.rule_name)
    return hit


def save_cached(alert: Alert, result: Any) -> None:
    """Store a triage plan for reuse during development."""
    if not CACHE_ENABLED or result is None:
        return
    cache = _read_cache()
    cache[cache_key(alert)] = result
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, indent=2, default=str), encoding="utf-8")
    log.info("triage cached for %r", alert.rule_name)
