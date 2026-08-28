"""Metric definitions, label schemas and stage topology.

Declares the physical entities of the volume -- cameras, trackers, render
nodes, thermal zones and sequences -- the nine ``stage_*`` instruments, and
the healthy baseline band each signal random-walks within.

The topology here is not arbitrary. Two relationships exist so that the fault
scenarios in Phase 2 have a real causal path to follow:

* ``tracker_3`` feeds ``cam_a``, so tracker drift shows up on exactly one
  camera and the investigation has to pivot camera -> tracker.
* ``node_12``..``node_18`` all render ``seq_042``, so the VRAM leak's frame
  failures land on a single sequence.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------

CAMERAS: tuple[str, ...] = ("cam_a", "cam_b", "cam_c")
TRACKERS: tuple[str, ...] = tuple(f"tracker_{i}" for i in range(1, 7))
NODES: tuple[str, ...] = tuple(f"node_{i:02d}" for i in range(1, 25))
ZONES: tuple[str, ...] = ("north", "south", "east", "west")
SEQUENCES: tuple[str, ...] = ("seq_041", "seq_042", "seq_043")

SERVICES: tuple[str, ...] = ("stage_control", "render_worker", "tracker_daemon")
LEVELS: tuple[str, ...] = ("debug", "info", "warn", "error")

#: Which trackers feed which camera's pose solve. tracker_3 -> cam_a is what
#: makes Fault 1's camera->tracker pivot resolve to a single tracker.
CAMERA_TRACKERS: dict[str, tuple[str, ...]] = {
    "cam_a": ("tracker_1", "tracker_3"),
    "cam_b": ("tracker_2", "tracker_4"),
    "cam_c": ("tracker_5", "tracker_6"),
}

#: Six nodes per thermal zone. north is node_01..node_06 -- Fault 4's zone.
NODE_ZONE: dict[str, str] = {
    node: ZONES[i // 6] for i, node in enumerate(NODES)
}

#: Which sequence each node is rendering. node_09..node_18 are on seq_042,
#: which covers the whole node_12..node_18 range the VRAM leak touches.
def _node_sequence(node: str) -> str:
    n = int(node.split("_")[1])
    if n <= 8:
        return "seq_041"
    if n <= 18:
        return "seq_042"
    return "seq_043"


NODE_SEQUENCE: dict[str, str] = {node: _node_sequence(node) for node in NODES}

#: Every render node carries the same card: 24 GiB.
VRAM_TOTAL_BYTES: int = 24 * 1024**3

# --------------------------------------------------------------------------
# Healthy baselines
# --------------------------------------------------------------------------


@dataclass
class Band:
    """A healthy value range plus the step size of its random walk."""

    low: float
    high: float
    step: float

    def start(self) -> float:
        return random.uniform(self.low, self.high)


BASELINES: dict[str, Band] = {
    "tracking_latency_ms": Band(8.0, 12.0, 0.35),
    "calibration_confidence": Band(0.95, 0.98, 0.004),
    "sync_drift_ms": Band(0.1, 0.3, 0.02),
    "vram_used_fraction": Band(0.45, 0.60, 0.006),
    "gpu_temp_celsius": Band(62.0, 71.0, 0.4),
    "queue_depth": Band(4.0, 12.0, 0.8),
}

#: frame_duration is drawn per tick rather than walked; these shape a
#: distribution whose p99 sits at about 0.038s.
FRAME_DURATION_MEDIAN: float = 0.0295
FRAME_DURATION_SIGMA: float = 0.0022
FRAME_DURATION_TAIL_P: float = 0.018
FRAME_DURATION_TAIL_MIN: float = 0.034
FRAME_DURATION_TAIL_MAX: float = 0.042

#: Explicit buckets. The SDK default boundaries are whole seconds, which would
#: put every frame in the first bucket and make p99 meaningless.
FRAME_DURATION_BUCKETS: tuple[float, ...] = (
    0.005, 0.010, 0.020, 0.025, 0.028, 0.030, 0.032, 0.035,
    0.038, 0.042, 0.050, 0.060, 0.075, 0.100, 0.150, 0.250, 0.500,
)


class Walk:
    """A value that drifts inside a band, reflected at the edges.

    Faults are applied as modifiers on top of ``value`` rather than by
    replacing the walk, so a fault's ``stop()`` reverts to a live baseline.
    """

    def __init__(self, band: Band) -> None:
        self.band = band
        self.value = band.start()

    def tick(self) -> float:
        self.value += random.uniform(-self.band.step, self.band.step)
        if self.value < self.band.low:
            self.value = self.band.low + (self.band.low - self.value)
        elif self.value > self.band.high:
            self.value = self.band.high - (self.value - self.band.high)
        return self.value


def sample_frame_duration() -> float:
    """One frame render time in seconds, healthy."""
    if random.random() < FRAME_DURATION_TAIL_P:
        return random.uniform(FRAME_DURATION_TAIL_MIN, FRAME_DURATION_TAIL_MAX)
    return max(0.012, random.gauss(FRAME_DURATION_MEDIAN, FRAME_DURATION_SIGMA))


# --------------------------------------------------------------------------
# Healthy log stream
# --------------------------------------------------------------------------

#: Roughly three quarters of fleet chatter is render workers finishing frames.
_RENDER_SHARE = 0.72
_TRACKER_SHARE = 0.88


def healthy_log_line(frame_no: int) -> tuple[str, str, str, dict[str, str]]:
    """One plausible healthy line: ``(service, level, body, attributes)``."""
    roll = random.random()
    if roll < _RENDER_SHARE:
        node = random.choice(NODES)
        seq = NODE_SEQUENCE[node]
        ms = sample_frame_duration() * 1000
        return (
            "render_worker",
            "info",
            f"frame {frame_no} complete seq={seq} render={ms:.1f}ms",
            {"node": node, "sequence": seq},
        )
    if roll < _TRACKER_SHARE:
        trk = random.choice(TRACKERS)
        return (
            "tracker_daemon",
            "debug",
            f"{trk} solve converged residual={random.uniform(0.2, 0.8):.2f}mm",
            {"tracker": trk},
        )
    seq = random.choice(SEQUENCES)
    return (
        "stage_control",
        "info",
        f"{seq} queue nominal, wall segments in sync",
        {"sequence": seq},
    )
