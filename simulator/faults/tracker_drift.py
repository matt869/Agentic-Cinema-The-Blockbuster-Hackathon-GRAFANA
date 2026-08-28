"""Fault 1 -- tracker drift.

A wall segment reposition logged at T+0 by ``stage_control``, after which
camera A tracking latency ramps 12 -> 45 ms and tracker_3 calibration
confidence decays 0.97 -> 0.61 over 90 seconds. Every other camera and tracker
stays healthy, so the cause is only reachable by slicing per camera, pivoting
to tracker confidence, then searching logs from *before* the onset.

The reposition log lands at T+0, ahead of any metric movement -- which is what
makes "search logs before the alert" the move that solves it.
"""

from __future__ import annotations

from simulator.faults.base import Fault, LogLine, Readings, ramp

DURATION_S = 90.0
CAMERA = "cam_a"
TRACKER = "tracker_3"

LATENCY_START, LATENCY_END = 12.0, 45.0
CONFIDENCE_START, CONFIDENCE_END = 0.97, 0.61


class TrackerDrift(Fault):
    name = "tracker_drift"
    summary = (
        "Wall segment repositioned without recalibration; tracker_3 solve "
        "drifts and cam_a tracking latency climbs."
    )

    def on_start(self) -> None:
        self.emit(
            LogLine(
                service="stage_control",
                level="info",
                body=(
                    "wall segment B4 repositioned 340mm camera-left for "
                    "seq_041 setup, operator=stage_ops"
                ),
                attributes={"segment": "B4", "sequence": "seq_041"},
            )
        )

    def tick(self, t: float, readings: Readings) -> None:
        latency = ramp(t, DURATION_S, LATENCY_START, LATENCY_END)
        for (camera, tracker) in readings.latency:
            if camera == CAMERA:
                # The camera's pose solve degrades as a whole; the drifting
                # tracker is only identifiable from its confidence signal.
                readings.latency[(camera, tracker)] = latency

        readings.confidence[TRACKER] = ramp(
            t, DURATION_S, CONFIDENCE_START, CONFIDENCE_END
        )

        if int(t) % 20 == 0 and t >= 20:
            readings.logs.append(
                LogLine(
                    service="tracker_daemon",
                    level="warn",
                    body=(
                        f"{TRACKER} solve residual rising, "
                        f"confidence={readings.confidence[TRACKER]:.2f}"
                    ),
                    attributes={"tracker": TRACKER},
                )
            )
