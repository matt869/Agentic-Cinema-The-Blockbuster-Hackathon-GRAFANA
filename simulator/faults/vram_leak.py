"""Fault 3 -- VRAM leak.

A GPU driver patch applied to node_12..node_18, logged well before the
incident window. From T+0 VRAM on those seven nodes climbs linearly 55% -> 97%
over 20 minutes; past 90% frame failures increment on seq_042, render workers
emit CUDA out-of-memory stack traces, and the sequence queue depth rises as
failures accumulate.

This is the hardest reasoning step in the project. Everything inside the alert
window is a *symptom*: rising VRAM, failing frames, a growing queue. The cause
is a single info-level log hours earlier, so an investigation that only
searches the incident window finds nothing but consequences. It has to widen
the time range past the alert to solve this one.

BUILD_SPEC asks for the patch event at T-8h. Grafana Cloud Loki silently
rejects OTLP log records older than 3 hours -- measured, not assumed: probes
at 180 minutes land and 195 minutes do not. An 8-hour-old record is therefore
un-ingestable on this platform and simply vanishes. We backdate to the largest
safe margin under that ceiling instead. The scenario is unaffected: alert
windows are minutes wide, so a 2.5-hour-old cause is still far outside one and
the agent must still widen its range to find it.
"""

from __future__ import annotations

import time

from simulator.faults.base import Fault, LogLine, Readings, ramp

NODES = tuple(f"node_{i}" for i in range(12, 19))
SEQUENCE = "seq_042"

#: Backdating ceiling is 3h (Grafana Cloud Loki drops older records without
#: erroring). 2.5h keeps a 30-minute safety margin.
PATCH_AGE_S = 2.5 * 3600.0
RAMP_S = 20 * 60.0
VRAM_START, VRAM_END = 0.55, 0.97
FAILURE_THRESHOLD = 0.90

_OOM_TRACE = (
    "CUDA error: out of memory\n"
    "  File \"/opt/render/worker.py\", line 214, in allocate_frame_buffer\n"
    "    buf = torch_alloc(self.tile_bytes)\n"
    "  File \"/opt/render/gpu.py\", line 88, in torch_alloc\n"
    "    raise CudaOutOfMemoryError(requested=self.tile_bytes)\n"
    "CudaOutOfMemoryError: tried to allocate 412 MiB on device 0"
)


class VramLeak(Fault):
    name = "vram_leak"
    summary = (
        "GPU driver patch on node_12..node_18 leaks VRAM; seq_042 frames "
        "fail once cards fill and the render queue backs up."
    )

    def on_start(self) -> None:
        # Backdated 2.5 hours. This log is the root cause and it sits
        # outside any sane incident window on purpose.
        patched_at = time.time_ns() - int(PATCH_AGE_S * 1e9)
        self.emit(
            LogLine(
                service="stage_control",
                level="info",
                body=(
                    "maintenance window complete: GPU driver 552.41 applied to "
                    "node_12,node_13,node_14,node_15,node_16,node_17,node_18 "
                    "(render pool B), all nodes returned to service"
                ),
                attributes={"driver_version": "552.41", "pool": "render_pool_b"},
                timestamp_ns=patched_at,
            )
        )

    def tick(self, t: float, readings: Readings) -> None:
        fraction = ramp(t, RAMP_S, VRAM_START, VRAM_END)
        failing = 0

        for node in NODES:
            readings.vram_fraction[node] = fraction
            if fraction >= FAILURE_THRESHOLD:
                failing += 1
                readings.failures[(node, SEQUENCE)] = (
                    readings.failures.get((node, SEQUENCE), 0) + 1
                )

        if failing and int(t) % 4 == 0:
            readings.logs.append(
                LogLine(
                    service="render_worker",
                    level="error",
                    body=_OOM_TRACE,
                    attributes={"node": NODES[int(t) % len(NODES)],
                                "sequence": SEQUENCE},
                )
            )

        # Failed frames go back on the queue, so depth climbs with the
        # failure count rather than on its own schedule.
        if failing:
            readings.queue[SEQUENCE] = readings.queue.get(SEQUENCE, 8.0) + failing * 4
