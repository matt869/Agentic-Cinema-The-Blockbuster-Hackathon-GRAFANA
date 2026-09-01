"""Severity and cost scoring. PURE PYTHON -- NO LLM, EVER.

Three steps, in order:

1. Drop every finding lacking at least one evidence item. An unevidenced
   claim never reaches the brief.
2. Classify severity from a fixed rule table.
3. Compute dollar exposure from the stage cost constants.

This file contains no model call and never will. The determinism is a
deliberate design property of the project: the numbers a producer acts on are
arithmetic over measured values, not something a language model chose. If this
module ever appears to need an LLM, the design has been misread -- ask.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from agent.models import Finding, Investigation, Severity

log = logging.getLogger("agent.scorer")

# --------------------------------------------------------------------------
# Cost model
# --------------------------------------------------------------------------

STAGE_DAY_RATE_USD = 95_000  # 10-hour shooting day
STAGE_COST_PER_MINUTE = 158
PAINT_OUT_COST_PER_SHOT = 2_400  # VFX cleanup, per affected shot
RESHOOT_COST_PER_SETUP = 12_000
AVG_SHOTS_PER_HOUR = 6

#: Healthy queue depth midpoint, used for the "3x baseline" rule.
QUEUE_BASELINE = 8.0


def shots_since(minutes: float) -> int:
    """How many shots were exposed while a fault was live."""
    return int(minutes / 60.0 * AVG_SHOTS_PER_HOUR)


# --------------------------------------------------------------------------
# Rule table
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One row of the severity table.

    ``exposure`` returns extra dollars beyond burnt stage time, plus the
    plain-language basis for that number.
    """

    signal: str
    triggers: Callable[[float], bool]
    severity: Severity
    exposure: Callable[[float, float], tuple[float, str]]


def _paint_out(value: float, minutes: float) -> tuple[float, str]:
    """Tracking drift means every shot taken since onset needs VFX cleanup."""
    shots = shots_since(minutes)
    if shots <= 0:
        return 0.0, "no complete shots exposed yet"
    return (
        shots * PAINT_OUT_COST_PER_SHOT,
        f"{shots} shot(s) exposed since onset need paint-out at "
        f"${PAINT_OUT_COST_PER_SHOT:,} each",
    )


def _reshoot(value: float, minutes: float) -> tuple[float, str]:
    """Visible tearing makes takes unusable -- those setups get reshot."""
    setups = max(1, shots_since(minutes))
    return (
        setups * RESHOOT_COST_PER_SETUP,
        f"{setups} setup(s) unusable, reshoot at ${RESHOOT_COST_PER_SETUP:,} each",
    )


def _wall_failure(value: float, minutes: float) -> tuple[float, str]:
    """Cards past 90% will black out a wall segment mid-take."""
    return (
        RESHOOT_COST_PER_SETUP,
        f"wall segment failure imminent at {value * 100:.0f}% VRAM; "
        f"one lost setup at ${RESHOOT_COST_PER_SETUP:,}",
    )


def _none(value: float, minutes: float) -> tuple[float, str]:
    return 0.0, "stage time only"


#: Order matters only for readability; every rule is evaluated.
RULES: tuple[Rule, ...] = (
    Rule("tracking_latency_ms", lambda v: v > 12.0, Severity.RED, _paint_out),
    Rule("sync_drift_ms", lambda v: v > 2.0, Severity.RED, _reshoot),
    Rule("vram_used_fraction", lambda v: v > 0.90, Severity.RED, _wall_failure),
    Rule("gpu_temp_celsius", lambda v: v > 82.0, Severity.AMBER, _none),
    Rule("calibration_confidence", lambda v: v < 0.75, Severity.AMBER, _none),
    Rule("queue_depth", lambda v: v > 3 * QUEUE_BASELINE, Severity.AMBER, _none),
)

_BY_SIGNAL: dict[str, Rule] = {r.signal: r for r in RULES}

_RANK = {Severity.GREEN: 0, Severity.AMBER: 1, Severity.RED: 2}


def score_finding(finding: Finding, minutes_affected: float) -> Finding:
    """Apply the rule table and cost model to one finding, in place."""
    rule = _BY_SIGNAL.get(finding.signal)
    if rule is None or finding.value is None:
        finding.severity = Severity.GREEN
        finding.cost_basis = "no rule matched this signal"
        return finding

    if not rule.triggers(finding.value):
        finding.severity = Severity.GREEN
        finding.cost_usd = 0.0
        finding.cost_basis = "within tolerance"
        return finding

    finding.severity = rule.severity
    stage_time = minutes_affected * STAGE_COST_PER_MINUTE
    extra, basis = rule.exposure(finding.value, minutes_affected)
    finding.cost_usd = round(stage_time + extra, 2)
    finding.cost_basis = (
        f"{minutes_affected:.0f} min of stage time at "
        f"${STAGE_COST_PER_MINUTE}/min (${stage_time:,.0f}); {basis}"
    )
    return finding


def score(investigation: Investigation, minutes_affected: float = 0.0) -> Investigation:
    """Drop unevidenced findings, classify severity, and total the cost.

    ``minutes_affected`` is how long the fault has been live. When it is not
    supplied, it is derived from the investigation's own elapsed time.
    """
    if minutes_affected <= 0:
        end = investigation.completed_at or investigation.started_at
        minutes_affected = max(
            0.0, (end - investigation.alert.fired_at).total_seconds() / 60.0
        )

    kept: list[Finding] = []
    for finding in investigation.findings:
        if not finding.has_evidence:
            # The rule still runs -- only to say what was lost. A finding that
            # would have scored and is dropped anyway is the dangerous case:
            # nothing downstream distinguishes "investigated, nothing wrong"
            # from "found it, then lost the evidence on the way here", so the
            # brief reports GREEN at $0 either way. That silence hid seven
            # panels about to go black. Warn loudly, name the rule that would
            # have fired, and leave the drop itself alone -- unevidenced
            # findings must not reach a producer.
            rule = _BY_SIGNAL.get(finding.signal)
            would_have_scored = (
                rule is not None
                and finding.value is not None
                and rule.triggers(float(finding.value))
            )
            if would_have_scored:
                log.warning(
                    "DROPPED A SCORING FINDING: %s (%s=%s) matched the %s rule "
                    "and would have scored %s, but carried no evidence. The "
                    "brief will understate this. Evidence is attached upstream "
                    "in agent.root.investigation_from_state -- suspect the "
                    "hypothesis/finding entity linkage.",
                    finding.entity, finding.signal, finding.value,
                    rule.signal, rule.severity.value,
                    extra={"extra_fields": {
                        "finding": finding.title,
                        "entity": finding.entity,
                        "signal": finding.signal,
                        "value": finding.value,
                        "would_have_scored": rule.severity.value,
                        "dropped_reason": "no_evidence",
                    }},
                )
            else:
                log.info(
                    "dropping unevidenced finding: %s", finding.title,
                    extra={"extra_fields": {"finding": finding.title}},
                )
            continue
        kept.append(score_finding(finding, minutes_affected))

    investigation.findings = kept
    investigation.total_cost_usd = round(sum(f.cost_usd for f in kept), 2)
    investigation.cost_display = f"${investigation.total_cost_usd:,.0f}"
    investigation.severity = (
        max((f.severity for f in kept), key=lambda s: _RANK[s])
        if kept
        else Severity.GREEN
    )
    return investigation
