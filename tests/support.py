"""Test helpers.

Fixtures live here and are never importable from ``agent/`` or ``api/`` --
the application has no canned-data path, by design.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from simulator import signals as sg
from simulator.faults.base import Fault, Readings

#: Fixed "now" for the fault clock, so trajectories are reproducible.
T0 = 1_000_000.0


def blank_readings() -> Readings:
    """One tick of dead-centre healthy readings, before any fault."""
    return Readings(
        latency={
            (cam, trk): 10.0
            for cam, trks in sg.CAMERA_TRACKERS.items()
            for trk in trks
        },
        confidence={t: 0.965 for t in sg.TRACKERS},
        sync={n: 0.2 for n in sg.NODES},
        vram_fraction={n: 0.52 for n in sg.NODES},
        temp={n: 66.0 for n in sg.NODES},
        queue={s: 8.0 for s in sg.SEQUENCES},
        frame_duration={n: 0.0295 for n in sg.NODES},
    )


@contextmanager
def started(fault: Fault):
    """Start a fault against a frozen clock."""
    with patch("time.monotonic", return_value=T0):
        fault.start()
    try:
        yield fault
    finally:
        fault.stop()


def at(fault: Fault, seconds: float) -> Readings:
    """Readings as they would be ``seconds`` after the fault started."""
    readings = blank_readings()
    with patch("time.monotonic", return_value=T0 + seconds):
        fault.apply(readings)
    return readings


def error_nodes(readings: Readings) -> list[str]:
    return [
        line.attributes.get("node", "")
        for line in readings.logs
        if line.level == "error"
    ]
