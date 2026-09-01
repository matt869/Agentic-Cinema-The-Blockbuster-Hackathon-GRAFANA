"""Smoke test per fault: the shape it produces, and that it reverts.

Each test asserts the three things that make its scenario solvable -- the
signal that moves, the signals that must NOT move, and the evidence the agent
is supposed to find. A fault that degrades everything at once teaches the
investigation nothing.
"""

from __future__ import annotations

import time
import unittest

from simulator.faults.base import FAULT_NAMES, build_fault
from simulator.faults.vram_leak import RAMP_S
from tests.support import T0, at, blank_readings, error_nodes, started


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
    """Seven nodes climb 55% -> 97% over RAMP_S; failures past 90%.

    Timings are expressed as fractions of RAMP_S rather than as the literal
    seconds they once were. The ramp is configurable now, and hard-coded
    1200s probes would still pass against a 180s ramp -- by landing on the
    clamped tail, testing nothing about the climb.
    """

    LEAKING = tuple(f"node_{i}" for i in range(12, 19))

    def test_climbs_on_seven_nodes_only(self) -> None:
        with started(build_fault("vram_leak")) as f:
            start, end = at(f, 0), at(f, RAMP_S)
            for node in self.LEAKING:
                self.assertAlmostEqual(start.vram_fraction[node], 0.55, places=2)
                self.assertAlmostEqual(end.vram_fraction[node], 0.97, places=2)
            for node, value in end.vram_fraction.items():
                if node not in self.LEAKING:
                    self.assertEqual(value, 0.52)

    def test_failures_start_only_past_the_threshold(self) -> None:
        with started(build_fault("vram_leak")) as f:
            # 75% of the ramp is ~0.865, below the 0.90 threshold.
            self.assertEqual(sum(at(f, RAMP_S * 0.75).failures.values()), 0)
            # 92% of the ramp is ~0.936, above it.
            failing = at(f, RAMP_S * 0.92)
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
            at(f, RAMP_S)
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


class TestMaturity(unittest.TestCase):
    """Every fault declares when it is worth investigating.

    Faults ramp, and some emit nothing at all until they cross a threshold:
    vram_leak has no failed frames, no OOM traces and no queue backup until
    VRAM passes 90%, which is 83% of the way up its ramp. Investigating before
    that spends a metered model call to look at a mild elevation. The UI reads
    these values to say so before the call is made.
    """

    def test_every_fault_declares_a_positive_maturity(self) -> None:
        for name in FAULT_NAMES:
            with self.subTest(name):
                self.assertGreater(build_fault(name).maturity_s, 0.0)

    def test_vram_maturity_is_past_the_first_failure(self) -> None:
        from simulator.faults.vram_leak import (
            FAILURE_THRESHOLD, VRAM_END, VRAM_START,
        )
        f = build_fault("vram_leak")
        first = RAMP_S * (FAILURE_THRESHOLD - VRAM_START) / (VRAM_END - VRAM_START)
        self.assertGreater(f.maturity_s, first)

    def test_readings_at_maturity_actually_contain_the_evidence(self) -> None:
        """The promise the cue makes must be true, not merely declared."""
        f = build_fault("vram_leak")
        with started(f):
            r = at(f, f.maturity_s)
        self.assertTrue(r.failures, "no frame failures at maturity")
        self.assertGreater(r.queue["seq_042"], 8.0, "queue has not risen")
        for node in (f"node_{i}" for i in range(12, 19)):
            self.assertGreaterEqual(r.vram_fraction[node], 0.90)

    def test_before_maturity_the_evidence_is_absent(self) -> None:
        f = build_fault("vram_leak")
        with started(f):
            r = at(f, f.maturity_s * 0.5)
        self.assertFalse(r.failures, "failures should not exist this early")

    def test_matured_flag_tracks_elapsed(self) -> None:
        from unittest.mock import patch
        f = build_fault("vram_leak")
        with patch("time.monotonic", return_value=T0):
            f.start()
            self.assertFalse(f.matured)
            self.assertAlmostEqual(f.maturity_remaining(), f.maturity_s, places=1)
        with patch("time.monotonic", return_value=T0 + f.maturity_s + 1):
            self.assertTrue(f.matured)
            self.assertEqual(f.maturity_remaining(), 0.0)
        f.stop()

    def test_inactive_fault_reports_full_maturity_remaining(self) -> None:
        # The card shows "needs 2:45" before anyone presses inject.
        f = build_fault("vram_leak")
        self.assertFalse(f.matured)
        self.assertEqual(f.maturity_remaining(), f.maturity_s)


class TestRampLevers(unittest.TestCase):
    """Both slow faults expose a ramp lever, and only vram_leak's default moved.

    thermal_throttle is held untuned for the live demo, so its committed
    default must stay at 15 minutes. The lever exists so a recording session
    can shorten the wait via the environment rather than by editing a module
    that is explicitly not meant to be edited.
    """

    def test_thermal_default_is_unchanged(self) -> None:
        import importlib, os
        from unittest.mock import patch
        import simulator.faults.thermal_throttle as t
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("VOLUME_OPS_THERMAL_RAMP_S", None)
            importlib.reload(t)
            self.assertEqual(t.RAMP_S, 15 * 60.0)
        importlib.reload(t)

    def test_thermal_ramp_honours_the_variable(self) -> None:
        import importlib, os
        from unittest.mock import patch
        import simulator.faults.thermal_throttle as t
        with patch.dict(os.environ, {"VOLUME_OPS_THERMAL_RAMP_S": "120"}):
            importlib.reload(t)
            self.assertEqual(t.RAMP_S, 120.0)
            # maturity must follow the lever, not stay pinned to the default.
            self.assertEqual(t.ThermalThrottle.maturity_s, 120.0)
        importlib.reload(t)

    def test_thermal_signals_are_untouched(self) -> None:
        # The lever changes the clock and nothing else.
        import simulator.faults.thermal_throttle as t
        self.assertEqual((t.TEMP_START, t.TEMP_END), (68.0, 87.0))
        self.assertEqual(t.THROTTLE_TEMP, 82.0)
        self.assertEqual((t.FRAME_START, t.FRAME_END), (0.0295, 0.0586))
        self.assertEqual(t.ZONE, "north")
