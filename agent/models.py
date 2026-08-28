"""Pydantic v2 models shared across the agent pipeline.

Every agent boundary is typed -- no bare dicts cross one. The central rule of
the project lives here: a ``Finding`` without at least one ``Evidence`` item is
not a finding, and the scorer drops it before it can reach the brief. Evidence
therefore carries the query that produced it, the result, and a timestamp, so
any claim in the final output can be traced back to something real.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Severity(str, Enum):
    RED = "RED"
    AMBER = "AMBER"
    GREEN = "GREEN"


class HypothesisStatus(str, Enum):
    PROPOSED = "proposed"
    INVESTIGATING = "investigating"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Alert(BaseModel):
    """A Grafana alert that opened an investigation."""

    rule_name: str
    fired_at: datetime = Field(default_factory=_now)
    summary: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    raw: dict = Field(default_factory=dict)


class Evidence(BaseModel):
    """One concrete observation. The query, the result, and when it was run.

    ``source`` is the tool that produced it (``query_prometheus``,
    ``query_loki_logs``, ...) so the trace shows how the agent learned this.
    """

    source: str
    query: str
    result: str
    observed_at: datetime = Field(default_factory=_now)
    #: Set when the evidence comes from outside the alert window -- the VRAM
    #: leak is only solvable by looking further back than the incident.
    outside_alert_window: bool = False

    def short(self, limit: int = 240) -> str:
        text = self.result.strip().replace("\n", " ")
        return text if len(text) <= limit else text[: limit - 1] + "…"


class Hypothesis(BaseModel):
    """A candidate explanation, tracked whether or not it survives.

    Rejected hypotheses are never discarded: they are shown in the UI and are
    a scored part of the demo. ``rejection_reason`` is what makes a rejection
    legible -- "its errors began 21s after the drift" rather than a silent
    disappearance.
    """

    statement: str
    entity: str = ""
    status: HypothesisStatus = HypothesisStatus.PROPOSED
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[Evidence] = Field(default_factory=list)
    rejection_reason: str = ""
    proposed_at: datetime = Field(default_factory=_now)

    @property
    def is_rejected(self) -> bool:
        return self.status is HypothesisStatus.REJECTED

    @property
    def is_supported(self) -> bool:
        """Meets the loop's early-exit bar: confident and twice-evidenced."""
        return self.confidence > 0.85 and len(self.evidence) >= 2


class Finding(BaseModel):
    """A conclusion the investigation reached, with its supporting evidence."""

    title: str
    detail: str = ""
    entity: str = ""
    signal: str = ""
    value: float | None = None
    severity: Severity = Severity.GREEN
    evidence: list[Evidence] = Field(default_factory=list)
    #: Populated by the scorer, never by a model.
    cost_usd: float = 0.0
    cost_basis: str = ""

    @property
    def has_evidence(self) -> bool:
        return len(self.evidence) > 0


class Investigation(BaseModel):
    """The full trace: what was alerted, what was considered, what was found."""

    alert: Alert
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    iterations: int = 0
    started_at: datetime = Field(default_factory=_now)
    completed_at: datetime | None = None
    widened_window: bool = False
    #: Set by the scorer.
    severity: Severity = Severity.GREEN
    total_cost_usd: float = 0.0
    #: The figure exactly as it should appear to a human, e.g. "$55,110".
    #: Formatting a currency value is not a job to hand a language model --
    #: it produced "$55110.0" when asked. The scorer renders it once, and the
    #: brief copies the string.
    cost_display: str = "$0"

    @property
    def rejected(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.is_rejected]

    @property
    def confirmed(self) -> list[Hypothesis]:
        return [h for h in self.hypotheses if h.status is HypothesisStatus.CONFIRMED]

    @property
    def all_evidence(self) -> list[Evidence]:
        seen: list[Evidence] = []
        for h in self.hypotheses:
            seen.extend(h.evidence)
        for f in self.findings:
            seen.extend(f.evidence)
        return seen


class CrewBrief(BaseModel):
    """The crew-facing output. No infrastructure jargon reaches this model.

    Four questions, answered plainly: what is broken, what it costs, what to
    do, and how long the fix takes.
    """

    headline: str
    what_is_broken: str
    what_it_costs: str
    what_to_do: str
    how_long: str
    severity: Severity
    cost_usd: float
    #: Carried through so the UI can show the reasoning behind the call.
    considered_and_rejected: list[str] = Field(default_factory=list)
    evidence_count: int = 0


class TriageResult(BaseModel):
    """What triage hands the investigator.

    Deliberately narrow: triage does not query anything, it reads the alert
    and decides where to look. The investigator does the work.
    """

    restatement: str = Field(
        description="What the alert means in one plain sentence."
    )
    suspected_entities: list[str] = Field(
        default_factory=list,
        description="Specific cameras, trackers, nodes, zones or sequences "
        "worth examining first.",
    )
    signals_to_examine: list[str] = Field(
        default_factory=list,
        description="Metric names to query, most informative first.",
    )
    hypotheses: list[str] = Field(
        default_factory=list,
        description="Candidate explanations, most likely first. Each must be "
        "a falsifiable statement about a specific entity.",
    )
    consider_wider_window: bool = Field(
        default=False,
        description="True when the cause plausibly predates the alert window "
        "(a change, a patch, a reconfiguration).",
    )
