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
  /** Seconds from start until the evidence the agent needs actually exists. */
  maturity_s: number;
  matured: boolean;
  matures_in_s: number;
};

/** m:ss — seconds alone stop being readable somewhere around a minute. */
function clock(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

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

  // The active fault furthest from being worth investigating, if any.
  const waiting = faults
    .filter((f) => f.active && !f.matured)
    .map((f) => ({ ...f, label: LABELS[f.name] ?? f.name }))
    .sort((a, b) => b.matures_in_s - a.matures_in_s)[0];

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
              {f.active ? (
                <span
                  className={
                    "mono ml-auto rounded px-2 py-0.5 text-[13px] font-semibold tabular-nums " +
                    (f.matured
                      ? "bg-emerald-500 text-slate-950"
                      : "bg-amber-950/60 text-amber-300")
                  }
                >
                  {f.matured
                    ? "READY TO INVESTIGATE"
                    : `BUILDING · ${clock(f.matures_in_s)}`}
                </span>
              ) : (
                <span className="mono ml-auto text-[13px] tabular-nums text-slate-500">
                  needs {clock(f.maturity_s)}
                </span>
              )}
            </div>

            {/* Every fault ramps, and some emit nothing at all until they
                cross a threshold: the VRAM leak has no failed frames, no OOM
                traces and no queue backup until 90%. Investigating before
                that spends a metered model call to look at a mild elevation.
                The bar is the honest answer to "is it worth running yet". */}
            {f.active && !f.matured && (
              <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-slate-800">
                <div
                  className="h-full rounded-full bg-amber-400 transition-all duration-1000"
                  style={{
                    width: `${Math.min(
                      100,
                      Math.round(
                        ((f.maturity_s - f.matures_in_s) / (f.maturity_s || 1)) * 100,
                      ),
                    )}%`,
                  }}
                />
              </div>
            )}

            <p className="mt-2 text-[14px] leading-snug text-slate-400">
              {f.summary}
            </p>
          </div>
        ))}
      </div>

      {/* Deliberately a warning, not a lock. Investigating early is a valid
          thing to do -- the agent will correctly report that little is wrong
          -- but it costs a real model call against a metered daily budget,
          so the cost is stated before it is spent rather than after. */}
      {waiting && (
        <p className="mt-auto mb-2 text-center text-[13px] text-amber-400">
          {waiting.label} still building — {clock(waiting.matures_in_s)} until
          there is evidence to find
        </p>
      )}

      <button
        onClick={() => onInvestigate("Stage signal degraded")}
        disabled={investigating}
        className={
          "rounded-xl px-5 py-4 text-lg font-bold text-slate-950 transition-colors disabled:cursor-not-allowed disabled:opacity-50 " +
          (waiting ? "" : "mt-auto ") +
          (waiting
            ? "bg-sky-500/70 hover:bg-sky-400"
            : "bg-sky-500 hover:bg-sky-400")
        }
      >
        {investigating ? "INVESTIGATING…" : "RUN INVESTIGATION"}
      </button>
    </section>
  );
}
