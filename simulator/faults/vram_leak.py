"""Fault 3 -- VRAM leak.

A GPU driver patch applied to node_12..node_18, logged well before the
incident window. From T+0 VRAM on those seven nodes climbs linearly 55% -> 97%
over RAMP_S (three minutes by default); past 90% frame failures increment on seq_042, render workers
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

import os
import time

from simulator.faults.base import Fault, LogLine, Readings, ramp

NODES = tuple(f"node_{i}" for i in range(12, 19))
SEQUENCE = "seq_042"

#: Backdating ceiling is 3h (Grafana Cloud Loki drops older records without
#: erroring). 2.5h keeps a 30-minute safety margin.
PATCH_AGE_S = 2.5 * 3600.0

#: How long VRAM takes to climb from VRAM_START to VRAM_END.
#:
#: Frame failures do not begin until the ramp crosses FAILURE_THRESHOLD, which
#: sits 83% of the way up -- so the ramp length, not the fault, decides how
#: long someone waits before there is anything to investigate. At the original
#: 20 minutes the first failure landed at 16.7, and nothing but a slow VRAM
#: rise existed before it: no OOM traces, no queue backup, no failures to
#: explain. Someone who injects the fault and investigates a minute later sees
#: a mild elevation and none of the evidence the scenario is built on, then
#: concludes the agent is weak when it is only early.
#:
#: Three minutes puts the first failure at 2.5 and the full 0.97 at 3, which a
#: visitor will actually sit through, and keeps the whole scenario inside the
#: idle window of a free host that sleeps after 15 minutes of no traffic. It
#: is still a slow build next to genlock_loss at 40s: the narrative is
#: unchanged, only the clock. Set VOLUME_OPS_VRAM_RAMP_S=1200 for the original.
RAMP_S = float(os.environ.get("VOLUME_OPS_VRAM_RAMP_S", "180"))
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
