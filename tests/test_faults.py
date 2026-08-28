"""Smoke test per fault: the shape it produces, and that it reverts.

Each test asserts the three things that make its scenario solvable -- the
signal that moves, the signals that must NOT move, and the evidence the agent
is supposed to find. A fault that degrades everything at once teaches the
investigation nothing.
"""

from __future__ import annotations

import time
import unittest

from simulator.faults.base import build_fault
from tests.support import at, blank_readings, error_nodes, started


class TestTrackerDrift(unittest.TestCase):
    """cam_a latency 12 -> 45 ms, tracker_3 confidence 0.97 -> 0.61 over 90s."""

    def test_ramps_only_cam_a_and_tracker_3(self) -> None:
        with started(build_fault("tracker_drift")) as f:
            start, mid, end = at(f, 0), at(f, 45), at(f, 90)

            self.assertAlmostEqual(start.latency[("cam_a", "tracker_3")], 12.0, places=1)
            self.assertAlmostEqual(mid.latency[("cam_a", "tracker_3")], 28.5, places=1)
            self.assertAlmostEqual(end.latency[("cam_a", "tracker_3")], 45.0, places=1)

            self.assertAlmostEqual(end.confidence["tracker_3"], 0.61, places=2)

            # Everything else stays healthy, which is what forces the pivot.
            for cam, trk in end.latency:
                if cam != "cam_a":
                    self.assertEqual(end.latency[(cam, trk)], 10.0)
            for trk in end.confidence:
                if trk != "tracker_3":
                    self.assertEqual(end.confidence[trk], 0.965)

    def test_reposition_log_lands_at_onset(self) -> None:
        with started(build_fault("tracker_drift")) as f:
            bodies = [line.body for line in at(f, 0).logs]
            self.assertTrue(any("repositioned" in b for b in bodies), bodies)

    def test_stop_reverts(self) -> None:
        f = build_fault("tracker_drift")
        with started(f):
            at(f, 90)
        reverted = blank_readings()
        f.apply(reverted)
        self.assertEqual(reverted.latency[("cam_a", "tracker_3")], 10.0)
        self.assertEqual(reverted.confidence["tracker_3"], 0.965)


class TestGenlockLoss(unittest.TestCase):
    """node_07 drift 0.2 -> 8.0 ms over 40s, then oscillating 6-10 ms."""

    def test_only_node_07_drifts(self) -> None:
        with started(build_fault("genlock_loss")) as f:
            self.assertAlmostEqual(at(f, 0).sync["node_07"], 0.2, places=1)
            self.assertAlmostEqual(at(f, 40).sync["node_07"], 8.0, places=1)
            for t in (60, 90, 120):
                self.assertTrue(5.9 <= at(f, t).sync["node_07"] <= 10.1)
            late = at(f, 90)
            for node, value in late.sync.items():
                if node != "node_07":
                    self.assertEqual(value, 0.2)

    def test_decoy_is_loud_but_late(self) -> None:
        """node_12 must error only AFTER the drift is already underway.

        This is the whole point of the scenario: the rejection has to be made
        on timing, not on which node is noisiest.
        """
        with started(build_fault("genlock_loss")) as f:
            first_decoy = next(
                (t for t in range(0, 70) if "node_12" in error_nodes(at(f, t))), None
            )
            self.assertIsNotNone(first_decoy, "decoy never fired")
            self.assertGreaterEqual(first_decoy, 20)
            self.assertLess(first_decoy, 50)

            # By the time the decoy speaks, node_07 has visibly degraded.
            self.assertGreater(at(f, first_decoy).sync["node_07"], 2.0)

            # And the decoy never touches a metric.
            self.assertEqual(at(f, first_decoy).sync["node_12"], 0.2)

    def test_stop_reverts(self) -> None:
        f = build_fault("genlock_loss")
        with started(f):
            at(f, 90)
        reverted = blank_readings()
        f.apply(reverted)
        self.assertEqual(reverted.sync["node_07"], 0.2)
        self.assertEqual(error_nodes(reverted), [])


class TestVramLeak(unittest.TestCase):
    """Seven nodes climb 55% -> 97% over 20 min; failures past 90%."""

    LEAKING = tuple(f"node_{i}" for i in range(12, 19))

    def test_climbs_on_seven_nodes_only(self) -> None:
        with started(build_fault("vram_leak")) as f:
            start, end = at(f, 0), at(f, 1200)
            for node in self.LEAKING:
                self.assertAlmostEqual(start.vram_fraction[node], 0.55, places=2)
                self.assertAlmostEqual(end.vram_fraction[node], 0.97, places=2)
            for node, value in end.vram_fraction.items():
                if node not in self.LEAKING:
                    self.assertEqual(value, 0.52)

    def test_failures_start_only_past_the_threshold(self) -> None:
        with started(build_fault("vram_leak")) as f:
            self.assertEqual(sum(at(f, 900).failures.values()), 0)   # ~86%
            failing = at(f, 1100)                                    # ~93%
            self.assertEqual(len(failing.failures), len(self.LEAKING))
            for (node, sequence) in failing.failures:
                self.assertIn(node, self.LEAKING)
                self.assertEqual(sequence, "seq_042")
            self.assertGreater(failing.queue["seq_042"], 8.0 * 3)

    def test_root_cause_predates_the_incident_window(self) -> None:
        """The patch log is the cause and must sit hours before T+0."""
        with started(build_fault("vram_leak")) as f:
            patch_logs = [l for l in at(f, 0).logs if "driver" in l.body]
            self.assertEqual(len(patch_logs), 1)
            line = patch_logs[0]
            self.assertEqual(line.service, "stage_control")
            self.assertEqual(line.level, "info")
            age_h = (time.time_ns() - line.timestamp_ns) / 1e9 / 3600
            self.assertGreater(age_h, 2.0)
            # Must stay under Grafana Cloud Loki's 3h rejection ceiling.
            self.assertLess(age_h, 3.0)

    def test_stop_reverts(self) -> None:
        f = build_fault("vram_leak")
        with started(f):
            at(f, 1200)
        reverted = blank_readings()
        f.apply(reverted)
        self.assertEqual(reverted.vram_fraction["node_15"], 0.52)
        self.assertEqual(reverted.failures, {})


class TestThermalThrottle(unittest.TestCase):
    """North zone 68 -> 87 C over 15 min. Silent: no errors, no failures.

    Not tuned against -- these assertions only check the shape the spec
    describes, never anything about how an agent responds to it.
    """

    def test_north_zone_only_and_silent(self) -> None:
        with started(build_fault("thermal_throttle")) as f:
            end = at(f, 900)
            for node in f.nodes:
                self.assertGreater(end.temp[node], 85.0)
            for node, value in end.temp.items():
                if node not in f.nodes:
                    self.assertEqual(value, 66.0)

            for t in (0, 300, 600, 900):
                readings = at(f, t)
                self.assertEqual(readings.failures, {})
                self.assertEqual(
                    [l.level for l in readings.logs if l.level in ("error", "warn")], []
                )

    def test_frames_degrade_without_erroring(self) -> None:
        with started(build_fault("thermal_throttle")) as f:
            self.assertGreater(at(f, 900).frame_duration["node_01"], 0.045)

    def test_stop_reverts(self) -> None:
        f = build_fault("thermal_throttle")
        with started(f):
            at(f, 900)
        reverted = blank_readings()
        f.apply(reverted)
        self.assertEqual(reverted.temp["node_01"], 66.0)


if __name__ == "__main__":
    unittest.main()
