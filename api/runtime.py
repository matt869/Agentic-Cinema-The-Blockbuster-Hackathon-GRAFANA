"""Process-wide runtime: the running stage simulator and the event bus.

Two singletons the routers share.

``StageRunner`` owns a :class:`~simulator.stage.StageEmitter` ticking at 1 Hz
on a background thread, so fault injection from an HTTP request affects the
same telemetry the agent is about to query.

``EventBus`` carries investigation events to SSE subscribers. It keeps a
replay buffer per investigation because the UI almost always connects a beat
after the investigation starts, and an event stream that drops the first three
hypotheses is worse than useless for a demo.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from typing import Any

from simulator.faults.base import FAULT_NAMES
from simulator.stage import TICK_SECONDS, StageEmitter

log = logging.getLogger("api.runtime")


class StageRunner:
    """The stage simulator, ticking on a background thread."""

    def __init__(self) -> None:
        self._emitter: StageEmitter | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        #: Guards start/stop themselves; self._lock guards a tick in progress.
        self._start_lock = threading.RLock()
        self.ticks = 0
        #: Why the last start() failed, or None. Surfaced on /health.
        self.start_error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the simulator. Safe to call more than once.

        The guard is inside the lock deliberately. A reload, a duplicated
        lifespan, or two workers racing would otherwise each pass an unlocked
        ``running`` check and start a second emitter -- two threads then write
        the same Prometheus series at the same timestamps, which shows up as
        out-of-order samples rather than as an obvious crash.
        """
        with self._start_lock:
            if self.running:
                log.info("stage simulator already running, not starting again")
                return
            try:
                self._emitter = StageEmitter()
            except Exception as exc:
                # Constructing the emitter reads the OTLP credentials, so a
                # missing or misspelled secret surfaces here. Raising would
                # abort the lifespan and take the whole app down with it --
                # on a host that injects secrets for us, one wrong variable
                # would become a crash loop whose reason is visible only in
                # container logs. Stay up instead: the UI still loads, the
                # fault routes already answer 503, and /health names the
                # failure to whoever opens the page.
                self._emitter = None
                self.start_error = f"{type(exc).__name__}: {exc}"
                log.exception("stage simulator failed to start")
                return
            self.start_error = None
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="stage-simulator", daemon=True
            )
            self._thread.start()
            log.info("stage simulator thread started")

    def _loop(self) -> None:
        assert self._emitter is not None
        while not self._stop.is_set():
            begin = time.monotonic()
            try:
                with self._lock:
                    self._emitter.tick()
                self.ticks += 1
            except Exception:
                log.exception("tick failed, continuing")
            self._stop.wait(max(0.0, TICK_SECONDS - (time.monotonic() - begin)))

    def stop(self) -> None:
        with self._start_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._emitter is not None:
            self._emitter.shutdown()
        self._thread = None
        log.info("stage simulator thread stopped")

    def start_fault(self, name: str) -> None:
        if self._emitter is None:
            raise RuntimeError("simulator is not running")
        with self._lock:
            self._emitter.start_fault(name)

    def stop_fault(self, name: str) -> None:
        if self._emitter is None:
            raise RuntimeError("simulator is not running")
        with self._lock:
            self._emitter.stop_fault(name)

    def fault_state(self) -> dict[str, dict[str, object]]:
        if self._emitter is None:
            return {n: {"active": False, "elapsed_s": 0.0} for n in FAULT_NAMES}
        with self._lock:
            return self._emitter.fault_state()


class EventBus:
    """Fan-out of investigation events to SSE subscribers, with replay."""

    MAX_HISTORY = 500
    #: Investigations whose replay buffer is kept. Without a bound, a
    #: long-running container retains every event of every alert forever.
    MAX_INVESTIGATIONS = 25

    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._history: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()

    def history(self, investigation_id: str) -> list[dict[str, Any]]:
        # Plain get: reading an unknown id must not allocate a buffer for it.
        return list(self._history.get(investigation_id, ()))

    def _evict(self) -> None:
        while len(self._history) > self.MAX_INVESTIGATIONS:
            stale, _ = self._history.popitem(last=False)
            self._subs.pop(stale, None)

    def subscribe(self, investigation_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs[investigation_id].append(q)
        return q

    def unsubscribe(self, investigation_id: str, q: asyncio.Queue) -> None:
        subs = self._subs.get(investigation_id, [])
        if q in subs:
            subs.remove(q)

    def publish(self, investigation_id: str, event: dict[str, Any]) -> None:
        """Record an event and hand it to every live subscriber.

        Safe to call from any thread or task -- delivery is non-blocking and a
        slow consumer can never stall the investigation.
        """
        event.setdefault("ts", time.time())
        hist = self._history.setdefault(investigation_id, [])
        self._history.move_to_end(investigation_id)
        hist.append(event)
        if len(hist) > self.MAX_HISTORY:
            del hist[: len(hist) - self.MAX_HISTORY]
        for q in list(self._subs.get(investigation_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - unbounded queue
                log.warning("dropping event for slow subscriber")
        self._evict()


stage_runner = StageRunner()
event_bus = EventBus()


def stage_status() -> dict[str, Any]:
    """Compact snapshot of the last exported tick, for the UI's stage panel.

    Deliberately the same readings that went to Grafana rather than a fresh
    sample, so what the crew sees on screen is what the agent will query.
    """
    from simulator import signals as sg

    emitter = stage_runner._emitter  # noqa: SLF001 - same-module runtime
    readings = getattr(emitter, "last", None)
    if readings is None:
        return {"ready": False}

    def worst(mapping: dict, high_is_bad: bool = True) -> tuple[str, float]:
        key = (max if high_is_bad else min)(mapping, key=mapping.get)
        return str(key), float(mapping[key])

    vram = readings.vram_fraction
    sync_node, sync_val = worst(readings.sync)
    vram_node, vram_val = worst(vram)
    temp_node, temp_val = worst(readings.temp)
    conf_trk, conf_val = worst(readings.confidence, high_is_bad=False)

    by_camera: dict[str, float] = {}
    for (cam, _trk), value in readings.latency.items():
        by_camera[cam] = max(by_camera.get(cam, 0.0), value)

    return {
        "ready": True,
        "ticks": stage_runner.ticks,
        "cameras": [
            {"name": c, "latency_ms": round(v, 2), "healthy": v <= 12.0}
            for c, v in sorted(by_camera.items())
        ],
        "trackers": [
            {"name": t, "confidence": round(v, 3), "healthy": v >= 0.75}
            for t, v in sorted(readings.confidence.items())
        ],
        "worst": {
            "sync": {"node": sync_node, "value": round(sync_val, 2), "healthy": sync_val <= 2.0},
            "vram": {"node": vram_node, "value": round(vram_val, 3), "healthy": vram_val <= 0.90},
            "temp": {"node": temp_node, "value": round(temp_val, 1), "healthy": temp_val <= 82.0},
            "confidence": {"tracker": conf_trk, "value": round(conf_val, 3), "healthy": conf_val >= 0.75},
        },
        "queues": [
            {"sequence": s, "depth": int(round(v)), "healthy": v <= 24.0}
            for s, v in sorted(readings.queue.items())
        ],
        "zones": [
            {
                "zone": z,
                "temp": round(
                    sum(readings.temp[n] for n in sg.NODES if sg.NODE_ZONE[n] == z) / 6, 1
                ),
            }
            for z in sg.ZONES
        ],
    }
