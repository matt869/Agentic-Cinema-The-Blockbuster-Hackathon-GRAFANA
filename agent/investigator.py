"""Investigation loop.

A ``LoopAgent`` holding the curated MCP toolset. Each
iteration picks the highest-value untested hypothesis, queries Grafana,
records evidence, updates confidence, and decides whether to continue. Hard
stop at MAX_ITERATIONS; early exit once a hypothesis passes 0.85 confidence with
at least two evidence items.

Two properties are load-bearing and neither is left to inference:

* Rejected hypotheses are retained with the reason for rejection. They are
  shown in the UI and are a scored part of the demo, so ``reject_hypothesis``
  requires a reason and the loop is told never to drop a candidate silently.
* Widening the time window past the alert is an explicit instruction, not
  something the model has to infer. The VRAM leak's root cause sits hours
  before the alert fires, and an agent that only ever looks inside the
  incident window cannot solve it -- it will find symptoms and stop.

The recording tools live in :mod:`agent.trace_tools`.
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent, LoopAgent

from agent.llm import FLASH_MODEL, build_model
from agent.mcp_config import DEPLOYMENT, GRAFANA_QUERY_GUIDE, build_grafana_toolset
from agent.trace_tools import (
    CONFIDENCE_EXIT,
    MIN_EVIDENCE_FOR_EXIT,
    TRACE_TOOLS,
)

#: BUILD_SPEC allows up to 8. Four is the working default: both scenarios that
#: have run end to end converged in well under four, and each unused iteration
#: is a request against a metered daily budget. 8 remains the hard ceiling if
#: raised via the environment.
MAX_ITERATIONS = int(os.environ.get("VOLUME_OPS_MAX_ITERATIONS", "4"))

INSTRUCTION = f"""\
You are the on-call investigator for a virtual production LED volume stage.
A film crew is on the stage right now. Every minute costs $158, so work
efficiently -- but be right, because a wrong diagnosis costs far more.

You have live access to Grafana Cloud through MCP tools. All data you see is
real. If a query fails, report it -- never invent a value.

{GRAFANA_QUERY_GUIDE}

HOW TO RUN ONE ITERATION
1. Pick the single highest-value UNTESTED hypothesis. If you have none yet,
   propose them from the triage plan with propose_hypothesis first.
2. Run the ONE query that best discriminates it -- a query whose result would
   look different if the hypothesis were false. Do not re-confirm what you
   already know.
3. Record what you got with record_evidence, including real numbers.
4. Update confidence with update_confidence.
5. If the evidence rules a hypothesis out, call reject_hypothesis WITH A
   SPECIFIC REASON. Never drop a candidate silently -- rejected hypotheses
   are shown to the crew and the reasoning is what earns their trust.
6. When you have a confirmed root cause, call record_finding for each
   measured problem, then call conclude.

STAY ON THE ALERT'S SIGNAL
Every query costs real budget. Query ONLY signals that can confirm or refute
the hypothesis you are testing right now. A tracking alert is not a reason to
sweep GPU temperature, VRAM, queue depth and sync drift "to be thorough" --
that is four wasted queries that tell you nothing about tracking. If a signal
cannot change your mind about the current hypothesis, do not query it.

Aim to finish in four or five queries. Reaching a confirmed root cause quickly
is better work than surveying the whole stage.

ONE EXCEPTION -- ALWAYS SEE WHO ELSE IS COMPLAINING
Before you settle on a culprit, run ONE query for error-level logs across the
whole fleet, not just your suspect:
  {{service_name="render_worker"}} | deployment="{DEPLOYMENT}" | level="error"
Note every entity that appears. If something other than your suspect is
erroring, propose it as a hypothesis and resolve it explicitly -- confirm it or
reject it on the evidence. Never leave a loud entity unexamined just because
you already have a favourite. This costs one query and is the difference
between a diagnosis and a guess.

STOP AS SOON AS YOU ARE DONE
The moment a hypothesis reaches the confidence bar with two evidence items,
call record_finding and then conclude in the SAME turn. Do not run another
query to feel more certain -- you are finished, and every extra call risks
losing the whole investigation.

SLICE BEFORE YOU CONCLUDE
An alert tells you a signal moved, not which entity moved it. Query with a
label breakdown and see which specific camera, tracker or node is responsible.
"Tracking is bad" is not a diagnosis; "cam_a only, and cam_a is fed by
tracker_3 whose confidence has fallen to 0.61" is.

LOUD IS NOT THE SAME AS CAUSAL -- ARGUE FROM TIMING
The noisiest entity is often not the culprit. Whenever you rule out a
candidate that IS genuinely misbehaving, the strongest argument is almost
always chronological, not statistical.

So for every entity you reject, first establish two timestamps:
  a) when the degradation in the ALERT's signal actually began, and
  b) when THAT entity's symptoms began.
Compare them. A cause cannot start after its effect. If the entity's trouble
began after the degradation was already underway, say so explicitly and give
the gap:

  "node_12's errors begin at 16:43:43, twenty-one seconds AFTER node_07's
   drift had already started at 16:43:22, so it cannot be the cause."

That reasoning is worth far more to the crew than "its other metrics look
fine" -- healthy metrics show it is not broken, but timing shows it is not
RESPONSIBLE. Prefer the timing argument. Use metrics only to support it.

YOU MAY WIDEN THE TIME WINDOW -- AND OFTEN MUST
The alert window contains the SYMPTOM. The CAUSE is frequently outside it.
A leak, a slow ramp, or a creeping failure was usually set in motion by a
change hours earlier: a driver patch, a recalibration, a reposition, a config
edit. Those show up as ordinary info-level logs in stage_control, not errors.

If you see a slow ramp or a resource climbing steadily, do NOT stop at the
alert window. Query stage_control logs over the last several HOURS and look
for a change event that predates the symptom. Set from_outside_alert_window
to true when you do. Finding the symptom is not finding the cause.

Stop when a hypothesis exceeds {CONFIDENCE_EXIT} confidence with at least
{MIN_EVIDENCE_FOR_EXIT} evidence items. You have at most {MAX_ITERATIONS}
iterations -- if you reach the limit, record your best-supported finding
anyway.
"""


def build_investigation_step() -> LlmAgent:
    """One iteration of the investigation."""
    return LlmAgent(
        name="investigation_step",
        model=build_model(FLASH_MODEL),
        description="Tests one hypothesis against live Grafana data.",
        instruction=INSTRUCTION,
        tools=[build_grafana_toolset(), *TRACE_TOOLS],
    )


def build_investigator() -> LoopAgent:
    """The bounded investigation loop."""
    return LoopAgent(
        name="investigator",
        description="Iteratively tests hypotheses against live Grafana data.",
        sub_agents=[build_investigation_step()],
        max_iterations=MAX_ITERATIONS,
    )
