"""Fault 4 -- thermal throttle. BUILD BUT DO NOT TUNE AGAINST.

Every node in the north zone rises 68 -> 87 C over RAMP_S (15 minutes by
default) and frame
duration degrades from a p99 of 0.038 to 0.071, with throttle notices at
debug level only. No errors, no failures, no alert fires -- this is silent
quality decay, the kind that only shows up in the dailies.

This fault exists to demonstrate generalization on camera. Agent prompts and
the scorer are never tuned against it, it is excluded from development test
runs, and it is used once, live, in the demo. Nothing in this module should
be adjusted to make an agent perform better against it.
"""

from __future__ import annotations

import os
import random

from simulator.faults.base import Fault, LogLine, Readings, ramp
from simulator.signals import NODE_ZONE

ZONE = "north"

#: Seconds for the north zone to climb TEMP_START -> TEMP_END.
#:
#: The default is unchanged at 15 minutes: this fault is deliberately not
#: tuned, and its timing is part of what it demonstrates. The lever exists
#: only so a recording session can shorten the wait without editing the
#: module -- nobody should be hand-editing a fault that is meant to stay
#: untouched, and an env var keeps the committed default honest.
#:
#: Same pattern as VOLUME_OPS_VRAM_RAMP_S. Note this is the slowest of the
#: four scenarios by a wide margin; at the default nothing is worth
#: investigating for a quarter of an hour.
RAMP_S = float(os.environ.get("VOLUME_OPS_THERMAL_RAMP_S", str(15 * 60.0)))

TEMP_START, TEMP_END = 68.0, 87.0
THROTTLE_TEMP = 82.0

#: Healthy frame duration is centred so p99 sits at ~0.038. Under throttle the
#: whole distribution shifts out to a p99 near 0.071.
FRAME_START, FRAME_END = 0.0295, 0.0586


class ThermalThrottle(Fault):
    name = "thermal_throttle"
    #: Declares how long this scenario takes to become investigable. It is a
    #: label on existing behaviour, not a change to it: the ramp, thresholds
    #: and signals are untouched. At 15 minutes it is the slowest of the four,
    #: which is exactly what the cue needs to tell someone before they spend a
    #: model call on it.
    maturity_s = RAMP_S
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
