"""Root agent assembly.

A ``SequentialAgent`` wiring triage -> investigator -> scorer -> brief.

The scorer sits in the middle of an otherwise LLM-driven pipeline as plain
deterministic Python. That is the point: by the time a dollar figure reaches
the crew it has been produced by a rule table and arithmetic, not chosen by a
model. ``ScorerAgent`` is the thin ADK wrapper that lets a pure function take
its turn in the sequence -- it contains no model call and delegates every
decision to :mod:`agent.scorer`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncGenerator

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from agent.brief import build_brief_agent
from agent.investigator import build_investigator
from agent.models import (
    Alert,
    Evidence,
    Finding,
    Hypothesis,
    HypothesisStatus,
    Investigation,
)
from agent.scorer import score
from agent.trace_tools import STATE_KEY
from agent.triage import build_triage_agent

log = logging.getLogger("agent.root")

SCORED_KEY = "scored"


def investigation_from_state(state: dict, alert: Alert) -> Investigation:
    """Rebuild a typed Investigation from what the loop recorded."""
    raw = state.get(STATE_KEY) or {}
    inv = Investigation(alert=alert)
    inv.widened_window = bool(raw.get("widened_window"))
    inv.iterations = int(raw.get("queries_run", 0))

    for h in raw.get("hypotheses", []):
        inv.hypotheses.append(
            Hypothesis(
                statement=h.get("statement", ""),
                entity=h.get("entity", ""),
                status=HypothesisStatus(h.get("status", "proposed")),
                confidence=float(h.get("confidence", 0.0)),
                rejection_reason=h.get("rejection_reason", ""),
                evidence=[
                    Evidence(
                        source=e.get("source", ""),
                        query=e.get("query", ""),
                        result=e.get("result", ""),
                        outside_alert_window=bool(e.get("outside_alert_window")),
                    )
                    for e in h.get("evidence", [])
                ],
            )
        )

    # A finding inherits the evidence of the SURVIVING hypotheses about the
    # same entity, so the scorer's "evidence or it doesn't exist" rule has
    # something to test. Findings with no backing are dropped there.
    #
    # Rejected hypotheses are deliberately excluded. Their evidence is what
    # ruled the entity out, and letting it validate a finding would let the
    # decoy node be reported as the cause -- with a dollar figure attached --
    # immediately after the agent argued it was not.
    # The haystack includes each hypothesis's EVIDENCE, not just its entity
    # and statement. A good investigation hypothesises at fleet level and
    # reports findings at entity level -- "render nodes are exhausting VRAM"
    # resolving to "node_12 is at 0.97" -- and those two strings share no
    # substring. Matching on entity and statement alone therefore dropped the
    # evidence for exactly the investigations that reasoned best, and the
    # finding then failed the scorer's evidence rule and vanished: seven
    # panels about to go black were reported GREEN at $0.
    #
    # The bridge is that the specific entity appears verbatim in the evidence
    # the agent gathered -- the driver log names node_12 outright -- so the
    # text it actually collected is what links the two levels.
    for f in raw.get("findings", []):
        entity = str(f.get("entity", "")).strip()
        key = entity.casefold()
        backing: list[Evidence] = []
        for h in inv.hypotheses:
            if h.is_rejected or not key:
                continue
            hay = " ".join(
                [h.entity, h.statement]
                + [f"{e.query} {e.result}" for e in h.evidence]
            ).casefold()
            if h.entity.strip().casefold() == key or key in hay:
                backing.extend(h.evidence)
        inv.findings.append(
            Finding(
                title=f.get("title", ""),
                detail=f.get("detail", ""),
                entity=entity,
                signal=f.get("signal", ""),
                value=f.get("value"),
                evidence=backing,
            )
        )
    return inv


class ScorerAgent(BaseAgent):
    """Deterministic scoring step. NO LLM -- see :mod:`agent.scorer`."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state if hasattr(ctx, "session") else {}
        alert_raw = state.get("alert") or {}
        alert = Alert.model_validate(alert_raw) if alert_raw else Alert(
            rule_name="unknown"
        )

        investigation = investigation_from_state(state, alert)
        # Close the window before scoring. Without this the scorer measures
        # from the alert to `started_at` -- both stamped at construction --
        # so every elapsed-time cost collapses to zero.
        investigation.completed_at = datetime.now(timezone.utc)
        before = len(investigation.findings)
        scored = score(investigation)
        log.info(
            "scored: %d findings in, %d out, severity=%s, cost=$%.0f",
            before, len(scored.findings), scored.severity.value,
            scored.total_cost_usd,
        )

        yield Event(
            author=self.name,
            actions=EventActions(
                state_delta={SCORED_KEY: scored.model_dump(mode="json")}
            ),
        )


def build_root_agent(include_triage: bool = True) -> SequentialAgent:
    """triage -> investigator -> scorer -> brief.

    ``include_triage=False`` omits the first step, for the development path
    where a cached plan has already been written into session state. The
    investigator reads ``state["triage"]`` either way, so the pipeline behaves
    identically -- it just does not pay for the call again.
    """
    steps: list[BaseAgent] = []
    if include_triage:
        steps.append(build_triage_agent())
    steps += [
        build_investigator(),
        ScorerAgent(name="scorer", description="Deterministic severity and cost."),
        build_brief_agent(),
    ]
    return SequentialAgent(
        name="volume_ops",
        description=(
            "Investigates a volume stage alert and reports it in crew language "
            "with a cost attached."
        ),
        sub_agents=steps,
    )


root_agent = None
"""Populated lazily by :func:`build_root_agent`; ADK discovers it by name."""
