/**
 * Stage status — the signals a crew would actually watch, updating live.
 *
 * Not a metrics dashboard. A first AD does not care about a gauge; they care
 * whether the wall is tearing and whether the background is lining up. Each
 * tile leads with the plain-language consequence and keeps the number as
 * supporting detail.
 */
import { useEffect, useState } from "react";

type Worst = { value: number; healthy: boolean; node?: string; tracker?: string };

export type StageStatus = {
  ready: boolean;
  ticks?: number;
  cameras?: { name: string; latency_ms: number; healthy: boolean }[];
  trackers?: { name: string; confidence: number; healthy: boolean }[];
  worst?: { sync: Worst; vram: Worst; temp: Worst; confidence: Worst };
  queues?: { sequence: string; depth: number; healthy: boolean }[];
  zones?: { zone: string; temp: number }[];
};

function Tile({
  label,
  reading,
  detail,
  healthy,
}: {
  label: string;
  reading: string;
  detail: string;
  healthy: boolean;
}) {
  return (
    <div
      className={
        "rounded-xl border-2 px-5 py-4 transition-colors duration-500 " +
        (healthy
          ? "border-slate-700/70 bg-slate-900/50"
          : "border-red-500 bg-red-950/40")
      }
    >
      <div className="text-[13px] uppercase tracking-[0.14em] text-slate-400">
        {label}
      </div>
      <div
        className={
          "mono mt-1 text-3xl font-bold tabular-nums " +
          (healthy ? "text-slate-100" : "text-red-300")
        }
      >
        {reading}
      </div>
      <div
        className={
          "mt-1 text-[15px] leading-snug " +
          (healthy ? "text-slate-400" : "text-red-300/90")
        }
      >
        {detail}
      </div>
    </div>
  );
}

export default function StageConsole() {
  const [status, setStatus] = useState<StageStatus>({ ready: false });

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const res = await fetch("/stage/status");
        const data = (await res.json()) as StageStatus;
        if (alive) setStatus(data);
      } catch {
        /* transient; the next tick will catch up */
      }
    };
    poll();
    const id = setInterval(poll, 1000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  const w = status.worst;
  const badCamera = status.cameras?.find((c) => !c.healthy);
  const badQueue = status.queues?.find((q) => !q.healthy);

  return (
    <section className="flex h-full flex-col gap-4">
      <header className="flex items-center gap-3">
        <h2 className="text-xl font-bold tracking-tight text-slate-200">
          Stage Status
        </h2>
        <span className="flex items-center gap-2 text-[13px] text-emerald-400">
          <span className="live-dot inline-block h-2.5 w-2.5 rounded-full bg-emerald-400" />
          live
        </span>
        {status.ticks !== undefined && (
          <span className="mono ml-auto text-[13px] text-slate-500">
            {status.ticks} ticks
          </span>
        )}
      </header>

      {!status.ready ? (
        <div className="rounded-xl border-2 border-slate-800 bg-slate-900/40 px-5 py-8 text-center text-slate-500">
          waiting for first telemetry tick…
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <Tile
            label="LED wall sync"
            reading={`${w!.sync.value.toFixed(2)} ms`}
            detail={
              w!.sync.healthy
                ? "wall is in sync, no tearing"
                : `${w!.sync.node} is tearing on camera`
            }
            healthy={w!.sync.healthy}
          />
          <Tile
            label="Camera tracking"
            reading={`${(badCamera ?? status.cameras![0]).latency_ms.toFixed(1)} ms`}
            detail={
              badCamera
                ? `${badCamera.name} background not lining up`
                : "background locked to camera move"
            }
            healthy={!badCamera}
          />
          <Tile
            label="Panel memory"
            reading={`${(w!.vram.value * 100).toFixed(0)}%`}
            detail={
              w!.vram.healthy
                ? "panels have headroom"
                : `${w!.vram.node} may go black mid-take`
            }
            healthy={w!.vram.healthy}
          />
          <Tile
            label="Panel heat"
            reading={`${w!.temp.value.toFixed(0)}°C`}
            detail={
              w!.temp.healthy
                ? "cooling is keeping up"
                : `${w!.temp.node} throttling, frames landing late`
            }
            healthy={w!.temp.healthy}
          />
          <Tile
            label="Tracker calibration"
            reading={w!.confidence.value.toFixed(2)}
            detail={
              w!.confidence.healthy
                ? "all trackers solving cleanly"
                : `${w!.confidence.tracker} has drifted`
            }
            healthy={w!.confidence.healthy}
          />
          <Tile
            label="Render queue"
            reading={`${badQueue?.depth ?? status.queues![0].depth}`}
            detail={
              badQueue
                ? `${badQueue.sequence} backing up`
                : "frames keeping pace"
            }
            healthy={!badQueue}
          />
        </div>
      )}
    </section>
  );
}
