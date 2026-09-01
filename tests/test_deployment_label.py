"""The deployment label.

A deployed Space and a laptop write identical metric names with identical
entity labels into one Grafana stack. Without a deployment label the two
stages merge into one fictional stage and every reading the agent gets is a
blend of two independent random walks -- silently, with no error and no empty
result. These tests hold that label in place on both sides: every point the
simulator emits carries it, and every query the agent is taught to write
filters on it.
"""

from __future__ import annotations

import importlib
import os
import unittest
from unittest.mock import MagicMock, patch

from simulator import stage as stage_mod
from simulator.otlp_client import DEPLOYMENT


class TestEveryEmittedPointIsTagged(unittest.TestCase):
    """No emission site may be missed -- one untagged series is a merged one."""

    def _emitter(self):
        with patch("simulator.stage.build_meter_provider"), \
             patch("simulator.stage.build_logger_provider"):
            e = stage_mod.StageEmitter()
        # Replace every instrument with a recorder.
        for name in ("g_latency", "g_confidence", "g_sync", "g_vram_used",
                     "g_vram_total", "g_temp", "g_queue", "h_frame",
                     "c_failures"):
            setattr(e, name, MagicMock())
        e.loggers = {svc: MagicMock() for svc in e.loggers}
        return e

    def _attr_dicts(self, e):
        """Every attribute dict handed to any instrument or logger."""
        out = []
        for name in ("g_latency", "g_confidence", "g_sync", "g_vram_used",
                     "g_vram_total", "g_temp", "g_queue", "h_frame",
                     "c_failures"):
            inst = getattr(e, name)
            for c in inst.set.call_args_list + inst.add.call_args_list + \
                     inst.record.call_args_list:
                if len(c.args) >= 2 and isinstance(c.args[1], dict):
                    out.append((name, c.args[1]))
        for svc, lg in e.loggers.items():
            for c in lg.emit.call_args_list:
                if "attributes" in c.kwargs:
                    out.append((f"log:{svc}", c.kwargs["attributes"]))
        return out

    def test_metrics_and_logs_all_carry_deployment(self) -> None:
        e = self._emitter()
        e.export(e.collect())
        dicts = self._attr_dicts(e)
        self.assertGreater(len(dicts), 20, "expected many emission points")
        missing = [n for n, d in dicts if "deployment" not in d]
        self.assertEqual(missing, [], f"untagged emission sites: {set(missing)}")
        wrong = [(n, d["deployment"]) for n, d in dicts
                 if d["deployment"] != DEPLOYMENT]
        self.assertEqual(wrong, [], f"wrong deployment value: {wrong}")

    def test_logs_are_tagged_too(self) -> None:
        e = self._emitter()
        e.export(e.collect())
        logs = [d for n, d in self._attr_dicts(e) if n.startswith("log:")]
        self.assertTrue(logs, "no log records emitted")
        for d in logs:
            self.assertEqual(d["deployment"], DEPLOYMENT)
            self.assertIn("level", d, "level must survive alongside deployment")


class TestDeploymentValue(unittest.TestCase):
    """Sourced from DEPLOYMENT_ENV, defaulting to local."""

    def _reload(self):
        import simulator.otlp_client as oc
        return importlib.reload(oc).DEPLOYMENT

    def test_defaults_to_local(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEPLOYMENT_ENV", None)
            self.assertEqual(self._reload(), "local")

    def test_blank_is_treated_as_unset(self) -> None:
        # A Space secret left empty must not produce a series labelled "".
        with patch.dict(os.environ, {"DEPLOYMENT_ENV": "   "}):
            self.assertEqual(self._reload(), "local")

    def test_honours_the_variable(self) -> None:
        with patch.dict(os.environ, {"DEPLOYMENT_ENV": "space"}):
            self.assertEqual(self._reload(), "space")

    def tearDown(self) -> None:
        import simulator.otlp_client as oc
        importlib.reload(oc)


class TestAgentIsTaughtToFilter(unittest.TestCase):
    """The guide both agents share must mandate the filter, in both dialects."""

    def test_guide_names_the_deployment_and_both_forms(self) -> None:
        from agent.mcp_config import GRAFANA_QUERY_GUIDE as g
        self.assertIn("MANDATORY", g)
        self.assertIn(f'deployment="{DEPLOYMENT}"', g)
        # PromQL: inside the selector. LogQL: after the pipe.
        self.assertIn(f'stage_render_queue_depth{{deployment="{DEPLOYMENT}"}}', g)
        self.assertIn(f'| deployment="{DEPLOYMENT}"', g)
        # and the trap is named explicitly
        self.assertIn(f'{{service_name="render_worker", deployment="{DEPLOYMENT}"}}', g)

    def test_no_prompt_ships_an_unfiltered_log_example(self) -> None:
        from agent.investigator import INSTRUCTION
        from agent.triage import INSTRUCTION as TRIAGE
        for name, text in (("investigator", INSTRUCTION), ("triage", TRIAGE)):
            for line in text.splitlines():
                if 'service_name=' in line and 'CORRECT' not in line \
                        and 'WRONG' not in line:
                    self.assertIn("deployment=", line,
                                  f"{name} shows an unfiltered query: {line}")


if __name__ == "__main__":
    unittest.main()


class TestAlertRulesFilter(unittest.TestCase):
    """The provisioned alert rules must filter too.

    They are Prometheus queries like any other. An unfiltered max() spans
    every deployment writing to the stack, so a fault on one stage fires an
    alert about another -- and the agent, which does filter, then investigates
    a healthy stage and correctly finds nothing. The two halves have to agree.
    """

    def _exprs(self):
        import re
        from pathlib import Path
        text = Path("grafana/alert_rules.yaml").read_text(encoding="utf-8")
        return re.findall(r"^\s*expr:\s*(.+)$", text, re.M)

    def test_every_rule_has_at_least_one_expression(self) -> None:
        self.assertGreaterEqual(len(self._exprs()), 5)

    def test_no_rule_queries_across_deployments(self) -> None:
        unfiltered = [e for e in self._exprs() if "deployment=" not in e]
        self.assertEqual(unfiltered, [], f"unfiltered alert exprs: {unfiltered}")

    def test_every_metric_selector_is_filtered(self) -> None:
        # A binary expression has two selectors; both need the filter, or the
        # vector match silently drops to nothing.
        import re
        for e in self._exprs():
            metrics = re.findall(r"(stage_[a-z_]+)(\{[^}]*\})?", e)
            for name, sel in metrics:
                self.assertTrue(
                    sel and "deployment=" in sel,
                    f"{name} unfiltered in: {e}",
                )
