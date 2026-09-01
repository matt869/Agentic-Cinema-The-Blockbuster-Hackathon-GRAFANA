"""Fault 2 -- genlock loss.

node_07 sync drift goes 0.2 -> 8.0 ms over 40 seconds then oscillates 6-10 ms,
with error logs about the genlock reference being lost and a fallback to the
internal clock.

A decoy runs alongside it: node_12 emits unrelated network errors from T+20s
for 30 seconds, with no metric impact at all. The decoy is *louder* than the
real fault for part of the window, and the only thing that rules it out is
timing -- node_12's errors begin twenty seconds after the drift already
started, so they cannot have caused it. An investigation that reasons from
error volume alone picks the wrong node.
"""

from __future__ import annotations

import math
import random

from simulator.faults.base import Fault, LogLine, Readings, ramp

NODE = "node_07"
DECOY_NODE = "node_12"

RAMP_S = 40.0
DRIFT_START, DRIFT_END = 0.2, 8.0
OSCILLATE_LOW, OSCILLATE_HIGH = 6.0, 10.0

DECOY_START_S = 20.0
DECOY_DURATION_S = 30.0

_DECOY_MESSAGES = (
    "network: tx queue overrun on eth1, 4 packets dropped",
    "network: link flap detected on eth1, renegotiating",
    "network: retransmit timeout talking to asset cache",
)


class GenlockLoss(Fault):
    name = "genlock_loss"
    #: Full drift, well past the 2.0ms alert threshold, and long enough for
    #: the decoy window (20-50s) to have opened and closed.
    maturity_s = RAMP_S + 15.0
    summary = (
        "node_07 lost its genlock reference and is free-running on its "
        "internal clock; its wall segment tears on camera."
    )

    def tick(self, t: float, readings: Readings) -> None:
        if t <= RAMP_S:
            drift = ramp(t, RAMP_S, DRIFT_START, DRIFT_END)
        else:
            # Free-running clock beats against the reference: a slow wander
            # between 6 and 10ms rather than a clean line.
            mid = (OSCILLATE_LOW + OSCILLATE_HIGH) / 2
            span = (OSCILLATE_HIGH - OSCILLATE_LOW) / 2
            drift = mid + span * math.sin((t - RAMP_S) / 7.0)
        readings.sync[NODE] = drift

        if int(t) % 5 == 0:
            readings.logs.append(
                LogLine(
                    service="render_worker",
                    level="error",
                    body=(
                        "genlock reference lost, falling back to internal "
                        f"clock (drift={drift:.1f}ms)"
                    ),
                    attributes={"node": NODE},
                )
            )

        # ---- decoy: loud, unrelated, and demonstrably too late ----
        if DECOY_START_S <= t < DECOY_START_S + DECOY_DURATION_S:
            if int(t) % 3 == 0:
                readings.logs.append(
                    LogLine(
                        service="render_worker",
                        level="error",
                        body=random.choice(_DECOY_MESSAGES),
                        attributes={"node": DECOY_NODE},
                    )
                )
