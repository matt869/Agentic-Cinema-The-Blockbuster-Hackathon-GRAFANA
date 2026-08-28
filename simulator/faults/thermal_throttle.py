"""Fault 4 -- thermal throttle. BUILD BUT DO NOT TUNE AGAINST.

Every node in the north zone rises 68 -> 87 C over 15 minutes and frame
duration degrades from a p99 of 0.038 to 0.071, with throttle notices at
debug level only. No errors, no failures, no alert fires -- this is silent
quality decay, the kind that only shows up in the dailies.

This fault exists to demonstrate generalization on camera. Agent prompts and
the scorer are never tuned against it, it is excluded from development test
runs, and it is used once, live, in the demo. Nothing in this module should
be adjusted to make an agent perform better against it.
"""

from __future__ import annotations

import random

from simulator.faults.base import Fault, LogLine, Readings, ramp
from simulator.signals import NODE_ZONE

ZONE = "north"
RAMP_S = 15 * 60.0

TEMP_START, TEMP_END = 68.0, 87.0
THROTTLE_TEMP = 82.0

#: Healthy frame duration is centred so p99 sits at ~0.038. Under throttle the
#: whole distribution shifts out to a p99 near 0.071.
FRAME_START, FRAME_END = 0.0295, 0.0586


class ThermalThrottle(Fault):
    name = "thermal_throttle"
    summary = (
        "North zone cooling is underperforming; those cards are throttling "
        "and frames are landing slower with no error anywhere."
    )

    def __init__(self) -> None:
        super().__init__()
        self.nodes = tuple(n for n, z in NODE_ZONE.items() if z == ZONE)

    def tick(self, t: float, readings: Readings) -> None:
        temp = ramp(t, RAMP_S, TEMP_START, TEMP_END)
        centre = ramp(t, RAMP_S, FRAME_START, FRAME_END)

        for node in self.nodes:
            readings.temp[node] = temp + random.uniform(-0.3, 0.3)
            readings.frame_duration[node] = max(
                0.012, random.gauss(centre, centre * 0.09)
            )

        # Throttle notices exist, but only at debug. Nothing escalates.
        if temp >= THROTTLE_TEMP and int(t) % 10 == 0:
            node = random.choice(self.nodes)
            readings.logs.append(
                LogLine(
                    service="render_worker",
                    level="debug",
                    body=(
                        f"gpu clock throttled to 82% of nominal "
                        f"(temp={temp:.1f}C, target=82.0C)"
                    ),
                    attributes={"node": node, "zone": ZONE},
                )
            )
