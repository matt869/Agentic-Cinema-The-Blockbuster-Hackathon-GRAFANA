"""Fault interface.

Defines the ``Fault`` contract -- ``start()``, ``tick(t)``, ``stop()`` -- that
every injected fault implements, plus the ``Readings`` bundle a fault mutates.

Faults are modifiers layered over the healthy baseline. Each tick the stage
collects one second of healthy readings, hands them to every active fault in
turn, and emits the result. Nothing is stateful on the fault's side beyond its
own start time, so ``stop()`` reverts to a live baseline rather than a frozen
snapshot, and several faults can run concurrently over the same readings.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class LogLine:
    """One log record a fault wants emitted this tick.

    ``timestamp_ns`` backdates the record. The VRAM leak needs it: its root
    cause is a driver patch applied eight hours before the incident window,
    and the whole point of that scenario is that the agent has to widen its
    time range to find it.
    """

    service: str
    level: str
    body: str
    attributes: dict[str, str] = field(default_factory=dict)
    timestamp_ns: int | None = None


@dataclass
class Readings:
    """One second of stage telemetry, before export.

    Faults mutate these in place. ``failures`` holds counter *increments* for
    this tick only, keyed by ``(node, sequence)``.
    """

    latency: dict[tuple[str, str], float]
    confidence: dict[str, float]
    sync: dict[str, float]
    vram_fraction: dict[str, float]
    temp: dict[str, float]
    queue: dict[str, float]
    frame_duration: dict[str, float]
    failures: dict[tuple[str, str], int] = field(default_factory=dict)
    logs: list[LogLine] = field(default_factory=list)


def ramp(t: float, duration: float, start: float, end: float) -> float:
    """Linear interpolation from ``start`` to ``end`` over ``duration``,
    clamped at both ends."""
    if duration <= 0:
        return end
    return start + (end - start) * max(0.0, min(1.0, t / duration))


class Fault(ABC):
    """A degradation that can be started, ticked and cleanly stopped."""

    name: ClassVar[str] = "fault"
    summary: ClassVar[str] = ""

    def __init__(self) -> None:
        self._started: float | None = None
        self._pending: list[LogLine] = []

    @property
    def active(self) -> bool:
        return self._started is not None

    def elapsed(self) -> float:
        """Seconds since ``start()``, or 0 when inactive."""
        return 0.0 if self._started is None else time.monotonic() - self._started

    def start(self) -> None:
        if self.active:
            return
        self._started = time.monotonic()
        self.on_start()

    def stop(self) -> None:
        """Revert. The next tick emits pure baseline again."""
        self._started = None
        self._pending.clear()
        self.on_stop()

    def apply(self, readings: Readings) -> None:
        """Flush any onset logs, then run this fault's per-second modifier."""
        if not self.active:
            return
        if self._pending:
            readings.logs.extend(self._pending)
            self._pending.clear()
        self.tick(self.elapsed(), readings)

    def emit(self, line: LogLine) -> None:
        """Queue a log line for the next tick (used from ``on_start``)."""
        self._pending.append(line)

    # ------------------------------------------------------------ subclass
    def on_start(self) -> None:
        """Hook for the T+0 event, if the scenario has one."""

    def on_stop(self) -> None:
        """Hook for releasing any fault-local state."""

    @abstractmethod
    def tick(self, t: float, readings: Readings) -> None:
        """Modify one second of readings. ``t`` is seconds since start."""


#: Fault name -> module path. Imported lazily so fault modules can import
#: this one without a cycle.
_REGISTRY: dict[str, str] = {
    "tracker_drift": "simulator.faults.tracker_drift",
    "genlock_loss": "simulator.faults.genlock_loss",
    "vram_leak": "simulator.faults.vram_leak",
    "thermal_throttle": "simulator.faults.thermal_throttle",
}

FAULT_NAMES: tuple[str, ...] = tuple(_REGISTRY)


def build_fault(name: str) -> Fault:
    """Instantiate a fault by name."""
    if name not in _REGISTRY:
        raise KeyError(f"unknown fault {name!r}; known: {', '.join(_REGISTRY)}")
    import importlib

    module = importlib.import_module(_REGISTRY[name])
    for value in vars(module).values():
        if isinstance(value, type) and issubclass(value, Fault) and value is not Fault:
            return value()
    raise RuntimeError(f"no Fault subclass in {_REGISTRY[name]}")
