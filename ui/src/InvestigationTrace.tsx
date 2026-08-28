/**
 * Investigation trace — the SSE stream rendered live.
 *
 * Hypotheses appear as they are formed, queries run beneath them, and a
 * rejected hypothesis is struck through in place with the reason it was ruled
 * out. That strikethrough is the single most important visual in the project:
 * it is the difference between an agent that guessed correctly and one that
 * reasoned. It never disappears — a rejected candidate stays on screen.
 */
import { useEffect, useRef, useState } from "react";

type Args = Record<string, unknown>;
type Ev = {
  type: string;
  author?: string;
  tool?: string;
  args?: Args;
  text?: string;
  error?: string;
  alert?: string;
  summary?: string;
  scored?: any;
  brief?: any;
  failed?: boolean;
  ts?: number;
};

type Hypo = {
  id: string;
  statement: string;
  entity: string;
  confidence: number;
  evidence: number;
  rejected: boolean;
  reason: string;
  widened: boolean;
};

const str = (v: unknown) => (typeof v === "string" ? v : v == null ? "" : String(v));

/** Fold the event stream into the hypothesis board. */
function reduceEvents(events: Ev[]) {
  const order: string[] = [];
  const map = new Map<string, Hypo>();
  const queries: { tool: string; detail: string; outside: boolean }[] = [];
  let brief: any = null;
  let scored: any = null;
  let failure = "";

  // Hypothesis ids are assigned server-side in call order (h1, h2, ...).
  let proposed = 0;
  const idOf = (a: Args | undefined) =>
    str(a?.hypothesis_id) || `h${proposed}`;

  for (const e of events) {
    const a = e.args ?? {};
    switch (e.type) {
      case "hypothesis": {
        proposed += 1;
        const id = `h${proposed}`;
        order.push(id);
        map.set(id, {
          id,
          statement: str(a.statement),
          entity: str(a.entity),
          confidence: 0,
          evidence: 0,
          rejected: false,
          reason: "",
          widened: false,
        });
        break;
      }
      case "evidence": {
        const h = map.get(idOf(a));
        if (h) {
          h.evidence += 1;
          if (a.from_outside_alert_window === true) h.widened = true;
        }
        queries.push({
          tool: str(a.source) || "query",
          detail: str(a.query),
          outside: a.from_outside_alert_window === true,
        });
        break;
      }
      case "confidence": {
        const h = map.get(idOf(a));
        if (h) h.confidence = Number(a.confidence ?? 0);
        break;
      }
      case "rejected": {
        const h = map.get(idOf(a));
        if (h) {
          h.rejected = true;
          h.reason = str(a.reason);
        }
        break;
      }
      case "query":
        queries.push({
          tool: str(e.tool),
          detail: str(a.expr) || str(a.logql) || str(a.query) || "",
          outside: false,
        });
        break;
      case "complete":
        brief = e.brief ?? null;
        scored = e.scored ?? null;
        if (e.failed) failure = failure || "investigation did not complete";
        break;
      case "error":
        failure = str(e.error);
        break;
    }
  }
  return {
    hypotheses: order.map((id) => map.get(id)!).filter(Boolean),
    queries,
    brief,
    scored,
    failure,
  };
}

function HypothesisCard({ h }: { h: Hypo }) {
  return (
    <div
      className={
        "slide-in rounded-xl border-2 p-4 transition-all duration-500 " +
        (h.rejected
          ? "border-red-800/70 bg-red-950/20 opacity-80"
          : h.confidence > 0.85
            ? "border-emerald-500 bg-emerald-950/25"
            : "border-slate-700 bg-slate-900/50")
      }
    >
      <div className="flex items-start gap-3">
        <span
          className={
            "mono shrink-0 rounded px-2 py-0.5 text-[13px] font-bold " +
            (h.rejected
              ? "bg-red-900/60 text-red-300"
              : "bg-slate-700 text-slate-200")
          }
        >
          {h.id}
        </span>
        <p
          className={
            "text-[17px] font-semibold leading-snug " +
            (h.rejected
              ? "text-slate-500 line-through decoration-red-500 decoration-[3px]"
              : "text-slate-100")
          }
        >
          {h.statement}
        </p>
      </div>

      {h.rejected ? (
        <div className="mt-3 rounded-lg border-l-4 border-red-500 bg-red-950/40 px-3 py-2">
          <div className="text-[12px] font-bold uppercase tracking-[0.16em] text-red-400">
            Ruled out
          </div>
          <div className="mt-1 text-[15px] leading-snug text-red-200">
            {h.reason || "no reason recorded"}
          </div>
        </div>
      ) : (
        <div className="mt-3 flex items-center gap-3">
          <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-800">
            <div
              className={
                "h-full rounded-full transition-all duration-700 " +
                (h.confidence > 0.85 ? "bg-emerald-400" : "bg-sky-400")
              }
              style={{ width: `${Math.round(h.confidence * 100)}%` }}
            />
          </div>
          <span className="mono w-12 text-right text-[14px] tabular-nums text-slate-300">
            {h.confidence.toFixed(2)}
          </span>
          <span className="text-[13px] text-slate-400">
            {h.evidence} evidence
          </span>
          {h.widened && (
            <span className="rounded bg-violet-900/60 px-2 py-0.5 text-[12px] font-semibold text-violet-300">
              widened window
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function Brief({ brief, scored }: { brief: any; scored: any }) {
  if (!brief) return null;
  const sev = str(brief.severity) || str(scored?.severity) || "GREEN";
  const cost = Number(brief.cost_usd ?? scored?.total_cost_usd ?? 0);
  return (
    <div
      className={
        "slide-in rounded-xl border-2 p-5 " +
        (sev === "RED"
          ? "border-red-500 bg-red-950/40"
          : sev === "AMBER"
            ? "border-amber-500 bg-amber-950/30"
            : "border-emerald-600 bg-emerald-950/30")
      }
    >
      <div className="flex items-center gap-3">
        <span
          className={
            "rounded px-2.5 py-1 text-[13px] font-black tracking-wider " +
            (sev === "RED"
              ? "bg-red-500 text-slate-950"
              : sev === "AMBER"
                ? "bg-amber-400 text-slate-950"
                : "bg-emerald-400 text-slate-950")
          }
        >
          {sev}
        </span>
        <span className="mono ml-auto text-2xl font-bold tabular-nums text-slate-100">
          ${cost.toLocaleString()}
        </span>
      </div>
      <h3 className="mt-3 text-2xl font-bold leading-tight text-slate-50">
        {str(brief.headline)}
      </h3>
      <dl className="mt-4 space-y-3 text-[16px] leading-snug">
        {[
          ["What's broken", brief.what_is_broken],
          ["What it costs", brief.what_it_costs],
          ["What to do", brief.what_to_do],
          ["How long", brief.how_long],
        ].map(([label, value]) => (
          <div key={label as string}>
            <dt className="text-[12px] font-bold uppercase tracking-[0.16em] text-slate-400">
              {label as string}
            </dt>
            <dd className="text-slate-100">{str(value)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

export default function InvestigationTrace({
  investigationId,
  onDone,
}: {
  investigationId: string | null;
  onDone: () => void;
}) {
  const [events, setEvents] = useState<Ev[]>([]);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!investigationId) return;
    setEvents([]);
    const es = new EventSource(`/stream/investigation/${investigationId}`);
    const add = (e: MessageEvent) => {
      try {
        setEvents((prev) => [...prev, JSON.parse(e.data) as Ev]);
      } catch {
        /* ignore malformed frame */
      }
    };
    for (const t of [
      "started", "hypothesis", "evidence", "confidence", "rejected",
      "finding", "concluded", "query", "thought", "error", "complete",
    ]) {
      es.addEventListener(t, add as EventListener);
    }
    es.addEventListener("complete", () => {
      es.close();
      onDone();
    });
    es.onerror = () => es.close();
    return () => es.close();
  }, [investigationId, onDone]);

  useEffect(() => {
    scroller.current?.scrollTo({
      top: scroller.current.scrollHeight,
      behavior: "smooth",
    });
  }, [events.length]);

  const { hypotheses, queries, brief, scored, failure } = reduceEvents(events);
  const rejected = hypotheses.filter((h) => h.rejected).length;

  return (
    <section className="flex h-full min-h-0 flex-col gap-4">
      <header className="flex items-baseline gap-3">
        <h2 className="text-xl font-bold tracking-tight text-slate-200">
          Investigation Trace
        </h2>
        {investigationId && !brief && !failure && (
          <span className="flex items-center gap-2 text-[13px] text-sky-400">
            <span className="live-dot inline-block h-2.5 w-2.5 rounded-full bg-sky-400" />
            reasoning
          </span>
        )}
        <span className="mono ml-auto text-[13px] text-slate-500">
          {hypotheses.length} hypotheses · {rejected} ruled out · {queries.length} queries
        </span>
      </header>

      <div
        ref={scroller}
        className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto pr-1"
      >
        {!investigationId && (
          <div className="rounded-xl border-2 border-dashed border-slate-800 px-5 py-10 text-center text-slate-500">
            Inject a fault, then run an investigation.
          </div>
        )}

        {hypotheses.map((h) => (
          <HypothesisCard key={h.id} h={h} />
        ))}

        {queries.length > 0 && (
          <div className="rounded-xl border border-slate-800 bg-slate-950/60 p-3">
            <div className="text-[12px] font-bold uppercase tracking-[0.16em] text-slate-500">
              Queries run against Grafana
            </div>
            <ul className="mono mt-2 space-y-1 text-[13px] text-slate-400">
              {queries.slice(-8).map((q, i) => (
                <li key={i} className="truncate">
                  <span className="text-sky-400">{q.tool}</span>{" "}
                  {q.detail}
                  {q.outside && (
                    <span className="ml-2 text-violet-400">[wider window]</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        <Brief brief={brief} scored={scored} />

        {failure && (
          <div className="slide-in rounded-xl border-2 border-red-600 bg-red-950/40 p-4">
            <div className="text-[12px] font-bold uppercase tracking-[0.16em] text-red-400">
              Investigation failed
            </div>
            <p className="mono mt-1 break-words text-[14px] text-red-200">
              {failure}
            </p>
            <p className="mt-2 text-[14px] text-red-300/80">
              No cached or fabricated result is shown. If Grafana or the model
              is unreachable, this system says so.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
