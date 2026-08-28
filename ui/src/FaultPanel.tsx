/**
 * Fault panel — buttons to inject each fault.
 *
 * Visible in the demo on purpose: the simulator is disclosed, not hidden. The
 * telemetry is synthetic and the panel says so. What is real is everything
 * downstream — the Grafana queries, the reasoning, the cost.
 */
import { useCallback, useEffect, useState } from "react";

export type Fault = {
  name: string;
  active: boolean;
  elapsed_s: number;
  summary: string;
};

const LABELS: Record<string, string> = {
  tracker_drift: "Tracker Drift",
  genlock_loss: "Genlock Loss",
  vram_leak: "VRAM Leak",
  thermal_throttle: "Thermal Throttle",
};

export default function FaultPanel({
  onInvestigate,
  investigating,
}: {
  onInvestigate: (alertName: string) => void;
  investigating: boolean;
}) {
  const [faults, setFaults] = useState<Fault[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/faults");
      setFaults((await res.json()) as Fault[]);
    } catch {
      /* transient */
    }
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 2000);
    return () => clearInterval(id);
  }, [refresh]);

  const toggle = async (f: Fault) => {
    setBusy(f.name);
    try {
      await fetch(`/faults/${f.name}/${f.active ? "stop" : "start"}`, {
        method: "POST",
      });
      await refresh();
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="flex h-full flex-col gap-4">
      <header className="flex items-baseline gap-3">
        <h2 className="text-xl font-bold tracking-tight text-slate-200">
          Fault Injection
        </h2>
        <span className="text-[13px] text-amber-400/80">
          simulated telemetry — disclosed, not hidden
        </span>
      </header>

      <div className="flex flex-col gap-3">
        {faults.map((f) => (
          <div
            key={f.name}
            className={
              "rounded-xl border-2 p-4 transition-colors duration-300 " +
              (f.active
                ? "border-amber-500 bg-amber-950/30"
                : "border-slate-700/70 bg-slate-900/40")
            }
          >
            <div className="flex items-center gap-3">
              <button
                onClick={() => toggle(f)}
                disabled={busy === f.name}
                className={
                  "min-w-[124px] rounded-lg px-4 py-2 text-[15px] font-bold transition-colors disabled:opacity-50 " +
                  (f.active
                    ? "bg-amber-500 text-slate-950 hover:bg-amber-400"
                    : "bg-slate-700 text-slate-100 hover:bg-slate-600")
                }
              >
                {busy === f.name ? "…" : f.active ? "STOP" : "INJECT"}
              </button>
              <div className="text-[17px] font-semibold text-slate-100">
                {LABELS[f.name] ?? f.name}
              </div>
              {f.active && (
                <span className="mono ml-auto text-[13px] tabular-nums text-amber-400">
                  {f.elapsed_s.toFixed(0)}s
                </span>
              )}
            </div>
            <p className="mt-2 text-[14px] leading-snug text-slate-400">
              {f.summary}
            </p>
          </div>
        ))}
      </div>

      <button
        onClick={() => onInvestigate("Stage signal degraded")}
        disabled={investigating}
        className="mt-auto rounded-xl bg-sky-500 px-5 py-4 text-lg font-bold text-slate-950 transition-colors hover:bg-sky-400 disabled:cursor-not-allowed disabled:opacity-50"
      >
        {investigating ? "INVESTIGATING…" : "RUN INVESTIGATION"}
      </button>
    </section>
  );
}
