"""The scorer: rule table, cost model, and the evidence gate.

Every case here is deterministic. That is the point of the module -- the
figure a producer acts on is arithmetic over measured values, so it can be
asserted exactly rather than eyeballed.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from agent.models import (
    Alert,
    Evidence,
    Finding,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    Severity,
)
from agent.root import investigation_from_state
from agent.scorer import (
    PAINT_OUT_COST_PER_SHOT,
    RESHOOT_COST_PER_SETUP,
    STAGE_COST_PER_MINUTE,
    score,
    shots_since,
)

EV = Evidence(source="query_prometheus", query="q", result="9.46 ms")


def investigation(findings, minutes: float = 45.0) -> Investigation:
    inv = Investigation(
        alert=Alert(
            rule_name="t",
            fired_at=datetime.now(timezone.utc) - timedelta(minutes=minutes),
        ),
        findings=findings,
    )
    inv.completed_at = datetime.now(timezone.utc)
    return inv


class TestRuleTable(unittest.TestCase):
    def test_severity_by_signal(self) -> None:
        cases = [
            ("tracking_latency_ms", 45.0, Severity.RED),
            ("sync_drift_ms", 9.46, Severity.RED),
            ("vram_used_fraction", 0.97, Severity.RED),
            ("gpu_temp_celsius", 87.0, Severity.AMBER),
            ("calibration_confidence", 0.61, Severity.AMBER),
            ("queue_depth", 36.0, Severity.AMBER),
        ]
        for signal, value, expected in cases:
            with self.subTest(signal=signal):
                f = Finding(title=signal, signal=signal, value=value, evidence=[EV])
                self.assertIs(score(investigation([f])).findings[0].severity, expected)

    def test_values_within_tolerance_are_green_and_free(self) -> None:
        for signal, value in [
            ("sync_drift_ms", 0.2),
            ("vram_used_fraction", 0.55),
            ("gpu_temp_celsius", 66.0),
            ("calibration_confidence", 0.96),
        ]:
            with self.subTest(signal=signal):
                f = Finding(title=signal, signal=signal, value=value, evidence=[EV])
                scored = score(investigation([f])).findings[0]
                self.assertIs(scored.severity, Severity.GREEN)
                self.assertEqual(scored.cost_usd, 0.0)

    def test_unknown_signal_does_not_crash_or_charge(self) -> None:
        f = Finding(title="x", signal="not_a_signal", value=1.0, evidence=[EV])
        scored = score(investigation([f])).findings[0]
        self.assertIs(scored.severity, Severity.GREEN)
        self.assertEqual(scored.cost_usd, 0.0)


class TestCostModel(unittest.TestCase):
    def test_stage_time_plus_reshoot(self) -> None:
        f = Finding(title="t", signal="sync_drift_ms", value=9.46, evidence=[EV])
        scored = score(investigation([f], minutes=45)).findings[0]
        expected = 45 * STAGE_COST_PER_MINUTE + shots_since(45) * RESHOOT_COST_PER_SETUP
        self.assertEqual(scored.cost_usd, expected)

    def test_stage_time_plus_paint_out(self) -> None:
        f = Finding(title="t", signal="tracking_latency_ms", value=45.0, evidence=[EV])
        scored = score(investigation([f], minutes=45)).findings[0]
        expected = 45 * STAGE_COST_PER_MINUTE + shots_since(45) * PAINT_OUT_COST_PER_SHOT
        self.assertEqual(scored.cost_usd, expected)

    def test_elapsed_window_actually_reaches_the_cost(self) -> None:
        """Regression: fired_at defaulted to now, collapsing every cost to $0."""
        f = Finding(title="t", signal="gpu_temp_celsius", value=87.0, evidence=[EV])
        scored = score(investigation([f], minutes=45))
        self.assertGreater(scored.total_cost_usd, 0.0)
        self.assertIn("45 min of stage time", scored.findings[0].cost_basis)

    def test_cost_display_is_preformatted(self) -> None:
        """Regression: the brief rendered '$55110.0' when left to format it."""
        f = Finding(title="t", signal="sync_drift_ms", value=9.46, evidence=[EV])
        scored = score(investigation([f], minutes=45))
        self.assertEqual(scored.cost_display, f"${scored.total_cost_usd:,.0f}")
        self.assertIn(",", scored.cost_display)


class TestEvidenceGate(unittest.TestCase):
    def test_unevidenced_findings_are_dropped(self) -> None:
        good = Finding(title="kept", signal="sync_drift_ms", value=9.4, evidence=[EV])
        bad = Finding(title="dropped", signal="sync_drift_ms", value=9.4)
        scored = score(investigation([good, bad]))
        self.assertEqual([f.title for f in scored.findings], ["kept"])

    def test_severity_is_the_worst_surviving_finding(self) -> None:
        amber = Finding(title="a", signal="gpu_temp_celsius", value=87.0, evidence=[EV])
        red = Finding(title="r", signal="sync_drift_ms", value=9.4, evidence=[EV])
        self.assertIs(score(investigation([amber, red])).severity, Severity.RED)
        self.assertIs(score(investigation([amber])).severity, Severity.AMBER)


class TestRejectedHypothesesCannotValidateFindings(unittest.TestCase):
    """Regression: a finding on a ruled-out entity scored RED $55,110."""

    def _state(self, finding_entity: str) -> dict:
        return {
            "investigation": {
                "hypotheses": [
                    {
                        "id": "h1",
                        "statement": "node_12 network errors caused the drift",
                        "entity": "node_12",
                        "status": "rejected",
                        "confidence": 0.1,
                        "rejection_reason": "errors began 21s after onset",
                        "evidence": [{"source": "query_loki_logs", "query": "q",
                                      "result": "r", "outside_alert_window": False}],
                    },
                    {
                        "id": "h2",
                        "statement": "node_07 lost genlock",
                        "entity": "node_07",
                        "status": "confirmed",
                        "confidence": 0.93,
                        "rejection_reason": "",
                        "evidence": [{"source": "query_prometheus", "query": "q",
                                      "result": "9.46", "outside_alert_window": False}],
                    },
                ],
                "findings": [{"title": "f", "entity": finding_entity,
                              "signal": "sync_drift_ms", "value": 9.46, "detail": ""}],
            }
        }

    def test_finding_on_rejected_entity_is_dropped(self) -> None:
        inv = investigation_from_state(self._state("node_12"), Alert(rule_name="t"))
        inv.completed_at = datetime.now(timezone.utc)
        self.assertEqual(score(inv).findings, [])

    def test_finding_on_confirmed_entity_survives(self) -> None:
        inv = investigation_from_state(self._state("node_07"), Alert(rule_name="t"))
        inv.completed_at = datetime.now(timezone.utc)
        self.assertEqual(len(score(inv).findings), 1)

    def test_rejected_hypotheses_are_retained_with_their_reason(self) -> None:
        inv = investigation_from_state(self._state("node_07"), Alert(rule_name="t"))
        inv.completed_at = datetime.now(timezone.utc)
        rejected = score(inv).rejected
        self.assertEqual(len(rejected), 1)
        self.assertIn("21s after", rejected[0].rejection_reason)


class TestModels(unittest.TestCase):
    def test_early_exit_bar(self) -> None:
        h = Hypothesis(statement="s", confidence=0.9, evidence=[EV, EV.model_copy()])
        self.assertTrue(h.is_supported)
        self.assertFalse(Hypothesis(statement="s", confidence=0.9, evidence=[EV]).is_supported)
        self.assertFalse(
            Hypothesis(statement="s", confidence=0.5,
                       evidence=[EV, EV.model_copy()]).is_supported
        )

    def test_rejection_status(self) -> None:
        h = Hypothesis(statement="s", status=HypothesisStatus.REJECTED)
        self.assertTrue(h.is_rejected)


if __name__ == "__main__":
    unittest.main()
