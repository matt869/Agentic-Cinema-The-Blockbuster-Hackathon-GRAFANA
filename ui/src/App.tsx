/**
 * Three regions on one screen, sized for a projector: stage status, fault
 * panel, investigation trace. No scrolling at 1080p — everything a viewer
 * needs to follow the story is visible at once.
 */
import { useCallback, useState } from "react";
import FaultPanel from "./FaultPanel";
import InvestigationTrace from "./InvestigationTrace";
import StageConsole from "./StageConsole";

export default function App() {
  const [investigationId, setInvestigationId] = useState<string | null>(null);
  const [investigating, setInvestigating] = useState(false);

  // Must be identity-stable. InvestigationTrace lists it as an effect
  // dependency, so an inline arrow here would tear down and re-open the
  // EventSource on every render -- wiping the trace exactly as the brief
  // arrives, because completing sets state and re-renders this component.
  const finish = useCallback(() => setInvestigating(false), []);

  const startInvestigation = useCallback(async (alertName: string) => {
    setInvestigating(true);
    try {
      const res = await fetch("/webhook/alert", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          alerts: [
            {
              labels: { alertname: alertName },
              annotations: {
                summary:
                  "A stage signal has moved outside its healthy band. " +
                  "Find the root cause.",
              },
            },
          ],
        }),
      });
      const data = await res.json();
      setInvestigationId(data.investigation_id);
    } catch {
      setInvestigating(false);
    }
  }, []);

  return (
    <div className="flex h-screen flex-col overflow-hidden p-5">
      <header className="mb-4 flex items-baseline gap-4">
        <h1 className="text-3xl font-black tracking-tight text-slate-50">
          VOLUME OPS
        </h1>
        <p className="text-[16px] text-slate-400">
          Agentic on-call for a virtual production LED stage
        </p>
        <div className="mono ml-auto text-[13px] text-slate-500">
          Gemini · grafana/mcp-grafana · Grafana Cloud
        </div>
      </header>

      <main className="grid min-h-0 flex-1 grid-cols-12 gap-5">
        <div className="col-span-4 flex min-h-0 flex-col gap-6">
          <StageConsole />
          <div className="min-h-0 flex-1">
            <FaultPanel
              onInvestigate={startInvestigation}
              investigating={investigating}
            />
          </div>
        </div>

        <div className="col-span-8 min-h-0 rounded-2xl border border-slate-800 bg-slate-950/40 p-5">
          <InvestigationTrace
            investigationId={investigationId}
            onDone={finish}
          />
        </div>
      </main>
    </div>
  );
}
