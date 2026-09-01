"""Healthy-state generation loop and fault engine.

Runs at 1 Hz. Each tick collects one second of healthy readings from the
per-entity random walks, hands them to every active fault in turn, then
exports the result. Runs indefinitely; export failures are logged and the loop
continues, because a dropped batch must never take the stage monitor down.

Because faults mutate readings rather than the walks themselves, ``stop()``
reverts to a live baseline on the very next tick, and concurrent faults
compose over the same readings.
"""

from __future__ import annotations

import argparse
import logging
import random
import time

from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

from simulator import signals as sg
from simulator.faults.base import FAULT_NAMES, Fault, LogLine, Readings, build_fault
from simulator.otlp_client import (
    DEPLOYMENT,
    build_logger_provider,
    build_meter_provider,
    configure_stdout_logging,
    stage_resource,
)

log = logging.getLogger("simulator.stage")

TICK_SECONDS = 1.0
LOGS_PER_TICK = (2, 5)


def tag(**attrs: object) -> dict[str, object]:
    """Attributes for one exported point, always carrying the deployment.

    A data-point attribute rather than a resource attribute, deliberately.
    Grafana Cloud promotes only a fixed set of resource attributes to series
    labels; anything else lands in ``target_info`` instead of on the series,
    where no PromQL selector the agent writes would ever see it.
    """
    return {"deployment": DEPLOYMENT, **attrs}

_SEVERITY = {
    "debug": (SeverityNumber.DEBUG, "DEBUG"),
    "info": (SeverityNumber.INFO, "INFO"),
    "warn": (SeverityNumber.WARN, "WARN"),
    "error": (SeverityNumber.ERROR, "ERROR"),
}


class StageEmitter:
    """Owns the OTel providers, instruments, random walks and active faults."""

    def __init__(self) -> None:
        view = View(
            instrument_name="stage_render_frame_duration_seconds",
            aggregation=ExplicitBucketHistogramAggregation(
                boundaries=sg.FRAME_DURATION_BUCKETS
            ),
        )
        self.meter_provider = build_meter_provider(
            stage_resource("volume_stage"), views=(view,)
        )
        meter = self.meter_provider.get_meter("volume_ops.stage")

        self.g_latency = meter.create_gauge("stage_camera_tracking_latency_ms")
        self.g_confidence = meter.create_gauge("stage_tracker_calibration_confidence")
        self.g_sync = meter.create_gauge("stage_led_wall_sync_drift_ms")
        self.g_vram_used = meter.create_gauge("stage_gpu_vram_used_bytes")
        self.g_vram_total = meter.create_gauge("stage_gpu_vram_total_bytes")
        self.g_temp = meter.create_gauge("stage_gpu_temp_celsius")
        self.g_queue = meter.create_gauge("stage_render_queue_depth")
        self.h_frame = meter.create_histogram("stage_render_frame_duration_seconds")
        self.c_failures = meter.create_counter("stage_frame_failures")

        # One logger provider per service: service.name is the only attribute
        # Grafana Cloud promotes to a Loki index label, so it has to carry the
        # service identity. node and level ride along as structured metadata.
        self.log_providers = {
            svc: build_logger_provider(stage_resource(svc)) for svc in sg.SERVICES
        }
        self.loggers = {
            svc: p.get_logger("volume_ops") for svc, p in self.log_providers.items()
        }

        b = sg.BASELINES
        self.latency = {
            (cam, trk): sg.Walk(b["tracking_latency_ms"])
            for cam, trks in sg.CAMERA_TRACKERS.items()
            for trk in trks
        }
        self.confidence = {t: sg.Walk(b["calibration_confidence"]) for t in sg.TRACKERS}
        self.sync = {n: sg.Walk(b["sync_drift_ms"]) for n in sg.NODES}
        self.vram = {n: sg.Walk(b["vram_used_fraction"]) for n in sg.NODES}
        self.temp = {n: sg.Walk(b["gpu_temp_celsius"]) for n in sg.NODES}
        self.queue = {s: sg.Walk(b["queue_depth"]) for s in sg.SEQUENCES}
        self.frame_no = 0
        self.faults: dict[str, Fault] = {}
        #: Last tick's readings, after faults. The UI reads this so the stage
        #: panel shows exactly what was exported, not a re-simulation.
        self.last: Readings | None = None

        # A counter needs one observation to exist as a series at all.
        for node in sg.NODES:
            self.c_failures.add(0, tag(node=node, sequence=sg.NODE_SEQUENCE[node]))

    # -------------------------------------------------------------- faults
    def start_fault(self, name: str) -> Fault:
        fault = self.faults.get(name) or build_fault(name)
        self.faults[name] = fault
        fault.start()
        log.info("fault started: %s", name)
        return fault

    def stop_fault(self, name: str) -> None:
        fault = self.faults.get(name)
        if fault is not None:
            fault.stop()
            log.info("fault stopped: %s", name)

    def fault_state(self) -> dict[str, dict[str, object]]:
        state: dict[str, dict[str, object]] = {}
        for name in FAULT_NAMES:
            fault = self.faults.get(name)
            # maturity_s is a class attribute, so it is known for a fault that
            # has never been started -- the UI needs it to say how long this
            # one will take before anyone commits to running it.
            maturity = float(build_fault(name).maturity_s
                             if fault is None else fault.maturity_s)
            active = fault is not None and fault.active
            state[name] = {
                "active": active,
                "elapsed_s": round(fault.elapsed(), 1) if fault else 0.0,
                "maturity_s": round(maturity, 1),
                "matured": bool(fault and fault.matured),
                "matures_in_s": round(
                    fault.maturity_remaining() if fault else maturity, 1
                ),
            }
        return state

    # ---------------------------------------------------------------- logs
    def emit_log(self, line: LogLine) -> None:
        """Emit one OTLP log record for a stage service."""
        number, text = _SEVERITY[line.level]
        now = line.timestamp_ns or time.time_ns()
        self.loggers[line.service].emit(
            timestamp=now,
            observed_timestamp=time.time_ns(),
            severity_number=number,
            severity_text=text,
            body=line.body,
            attributes=tag(level=line.level, **line.attributes),
        )

    # --------------------------------------------------------------- tick
    def collect(self) -> Readings:
        """One second of healthy readings, before any fault is applied."""
        readings = Readings(
            latency={k: w.tick() for k, w in self.latency.items()},
            confidence={k: w.tick() for k, w in self.confidence.items()},
            sync={k: w.tick() for k, w in self.sync.items()},
            vram_fraction={k: w.tick() for k, w in self.vram.items()},
            temp={k: w.tick() for k, w in self.temp.items()},
            queue={k: w.tick() for k, w in self.queue.items()},
            frame_duration={n: sg.sample_frame_duration() for n in sg.NODES},
        )
        for _ in range(random.randint(*LOGS_PER_TICK)):
            self.frame_no += 1
            service, level, body, attrs = sg.healthy_log_line(self.frame_no)
            readings.logs.append(LogLine(service, level, body, attrs))
        return readings

    def export(self, r: Readings) -> None:
        """Push one tick of readings to Grafana Cloud."""
        for (cam, trk), value in r.latency.items():
            self.g_latency.set(value, tag(camera=cam, tracker=trk))
        for trk, value in r.confidence.items():
            self.g_confidence.set(value, tag(tracker=trk))
        for node in sg.NODES:
            attrs = tag(node=node)
            self.g_sync.set(r.sync[node], attrs)
            self.g_vram_used.set(r.vram_fraction[node] * sg.VRAM_TOTAL_BYTES, attrs)
            self.g_vram_total.set(float(sg.VRAM_TOTAL_BYTES), attrs)
            self.g_temp.set(r.temp[node], tag(node=node, zone=sg.NODE_ZONE[node]))
            self.h_frame.record(
                r.frame_duration[node],
                tag(node=node, sequence=sg.NODE_SEQUENCE[node]),
            )
        for seq, value in r.queue.items():
            self.g_queue.set(round(value), tag(sequence=seq))
        for (node, seq), count in r.failures.items():
            self.c_failures.add(count, tag(node=node, sequence=seq))
        for line in r.logs:
            self.emit_log(line)

    def tick(self) -> None:
        readings = self.collect()
        for fault in self.faults.values():
            fault.apply(readings)
        self.export(readings)
        self.last = readings

    def shutdown(self) -> None:
        self.meter_provider.shutdown()
        for provider in self.log_providers.values():
            provider.shutdown()


def run(seconds: float | None = None, faults: tuple[str, ...] = ()) -> None:
    """Run the stage loop. ``seconds=None`` runs indefinitely."""
    emitter = StageEmitter()
    for name in faults:
        emitter.start_fault(name)
    log.info("stage simulator started at 1 Hz")
    started = time.monotonic()
    ticks = 0
    try:
        while seconds is None or time.monotonic() - started < seconds:
            begin = time.monotonic()
            try:
                emitter.tick()
                ticks += 1
            except Exception:
                log.exception("tick failed, continuing")
            time.sleep(max(0.0, TICK_SECONDS - (time.monotonic() - begin)))
    except KeyboardInterrupt:
        log.info("interrupted, shutting down")
    finally:
        emitter.shutdown()
        log.info("stage simulator stopped after %d ticks", ticks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Volume stage telemetry simulator")
    parser.add_argument("--seconds", type=float, default=None,
                        help="stop after N seconds (default: run indefinitely)")
    parser.add_argument("--fault", action="append", default=[], choices=FAULT_NAMES,
                        help="start a fault at launch (repeatable)")
    args = parser.parse_args()
    configure_stdout_logging()
    run(args.seconds, tuple(args.fault))


if __name__ == "__main__":
    main()
