"""Findings must inherit the evidence that supports them.

A good investigation hypothesises at fleet level and reports findings at
entity level: "render nodes are exhausting VRAM" resolving to "node_12 is at
0.97". Those two strings share no substring, so matching on entity and
statement alone dropped the evidence for exactly the investigations that
reasoned best. The finding then failed the scorer's evidence rule and vanished
-- and a real run reported GREEN at $0 on seven panels about to go black.

Observed in a live run against Grafana, not hypothesised: the finding entity
was node_12, the surviving hypothesis entity was render_nodes, and the driver
log naming node_12 verbatim sat in that hypothesis's evidence.
"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime, timedelta, timezone

from agent.models import Alert, Finding, Investigation
from agent.root import investigation_from_state
from agent.scorer import score
from agent.trace_tools import STATE_KEY

FLEET_HYPOTHESIS = {
    "statement": "Render nodes are experiencing VRAM exhaustion due to "
                 "excessive allocation or memory leak.",
    "entity": "render_nodes",
    "status": "confirmed",
    "confidence": 0.95,
    "evidence": [
        {"source": "query_prometheus",
         "query": 'stage_gpu_vram_used_bytes{deployment="local"}',
         "result": "Nodes 12 through 18 have VRAM usage fraction at 0.97.",
         "outside_alert_window": False},
        {"source": "query_loki_logs",
         "query": '{service_name="stage_control"}',
         "result": 'maintenance window complete: GPU driver 552.41 applied to '
                   'node_12,node_13,node_14,node_15,node_16,node_17,node_18',
         "outside_alert_window": True},
    ],
}

NODE_FINDING = {
    "title": "Render node VRAM exhaustion on node_12",
    "detail": "97% after GPU driver 552.41.",
    "entity": "node_12",
    "signal": "vram_used_fraction",
    "value": 0.97,
}


def _alert(minutes_ago: float = 6.0) -> Alert:
    return Alert(rule_name="Render node VRAM exhaustion",
                 fired_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


class TestFleetHypothesisToNodeFinding(unittest.TestCase):
    def _investigation(self, state: dict) -> Investigation:
        inv = investigation_from_state(state, _alert())
        inv.completed_at = datetime.now(timezone.utc)
        return inv

    def test_evidence_bridges_via_evidence_text(self) -> None:
        inv = self._investigation({STATE_KEY: {
            "hypotheses": [FLEET_HYPOTHESIS], "findings": [NODE_FINDING]}})
        self.assertEqual(len(inv.findings[0].evidence), 2)

    def test_the_finding_survives_scoring_and_carries_a_figure(self) -> None:
        scored = score(self._investigation({STATE_KEY: {
            "hypotheses": [FLEET_HYPOTHESIS], "findings": [NODE_FINDING]}}))
        self.assertEqual(len(scored.findings), 1)
        self.assertEqual(scored.severity.value, "RED")
        self.assertGreater(scored.total_cost_usd, 0.0)

    def test_rejected_hypotheses_still_cannot_lend_evidence(self) -> None:
        """The decoy must not be able to fund a finding through its evidence."""
        decoy = dict(FLEET_HYPOTHESIS, entity="seq_042", status="rejected",
                     rejection_reason="nodes 09-11 on seq_042 are healthy")
        inv = self._investigation({STATE_KEY: {
            "hypotheses": [decoy], "findings": [NODE_FINDING]}})
        self.assertEqual(inv.findings[0].evidence, [])

    def test_unrelated_entity_still_gets_nothing(self) -> None:
        # Widening the haystack must not make everything match everything.
        other = dict(NODE_FINDING, entity="tracker_5")
        inv = self._investigation({STATE_KEY: {
            "hypotheses": [FLEET_HYPOTHESIS], "findings": [other]}})
        self.assertEqual(inv.findings[0].evidence, [])

    def test_direct_entity_match_still_works(self) -> None:
        direct = dict(FLEET_HYPOTHESIS, entity="node_12")
        inv = self._investigation({STATE_KEY: {
            "hypotheses": [direct], "findings": [NODE_FINDING]}})
        self.assertEqual(len(inv.findings[0].evidence), 2)


class TestDroppedScoringFindingIsLoud(unittest.TestCase):
    """Silence is the dangerous failure: nothing downstream distinguishes
    "investigated, nothing wrong" from "found it, then lost the evidence"."""

    def _score_with_logs(self, finding: Finding):
        inv = Investigation(alert=_alert())
        inv.findings.append(finding)
        inv.completed_at = datetime.now(timezone.utc)
        with self.assertLogs("agent.scorer", level=logging.INFO) as cm:
            score(inv)
        return cm.output

    def test_warns_when_a_scoring_finding_is_dropped(self) -> None:
        out = self._score_with_logs(Finding(
            title="VRAM exhaustion on node_12", detail="", entity="node_12",
            signal="vram_used_fraction", value=0.97, evidence=[]))
        warnings = [l for l in out if l.startswith("WARNING")]
        self.assertTrue(warnings, f"expected a WARNING, got: {out}")
        joined = " ".join(warnings)
        self.assertIn("node_12", joined)
        self.assertIn("vram_used_fraction", joined)
        self.assertIn("RED", joined)

    def test_non_scoring_finding_stays_at_info(self) -> None:
        out = self._score_with_logs(Finding(
            title="Queue slightly up", detail="", entity="seq_041",
            signal="queue_depth", value=9.0, evidence=[]))
        self.assertFalse([l for l in out if l.startswith("WARNING")])

    def test_the_drop_itself_is_unchanged(self) -> None:
        """The evidence rule is correct and must keep dropping."""
        inv = Investigation(alert=_alert())
        inv.findings.append(Finding(
            title="VRAM exhaustion on node_12", detail="", entity="node_12",
            signal="vram_used_fraction", value=0.97, evidence=[]))
        inv.completed_at = datetime.now(timezone.utc)
        scored = score(inv)
        self.assertEqual(len(scored.findings), 0)
        self.assertEqual(scored.severity.value, "GREEN")


if __name__ == "__main__":
    unittest.main()
