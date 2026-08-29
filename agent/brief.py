"""Crew-language output.

An ``LlmAgent`` that converts the scored investigation into
something a film crew can act on: what is broken, what it costs, what to do,
and how long the fix takes.

The audience is a first AD or a producer standing on a stage that is burning
$158 a minute, not an SRE. They do not know what a percentile is and should
not have to. A node ID is meaningless on its own -- "node_07" means nothing,
"the wall segment behind the picture car" means everything -- so bare
identifiers never appear without plain-language context.

The dollar figure is NOT computed here. It arrives already calculated by the
deterministic scorer and is quoted verbatim. A model that invents a number a
producer might act on is worse than no number at all.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent

from agent.llm import BRIEF_MODEL, build_model
from agent.models import CrewBrief


INSTRUCTION = """\
You write the incident brief that goes to the crew on a virtual production
LED volume stage. Your reader is the first AD or the producer. They are
standing on set, the clock is running, and they need to make a call in the
next thirty seconds.

THE SCORED INVESTIGATION -- your only source of facts:

{scored}

Everything you write must come from that data. It contains findings with
evidence, a severity, a computed dollar figure, and the hypotheses that were
considered and rejected.

RULES

1. NO INFRASTRUCTURE JARGON. Never write p99, percentile, latency, gauge,
   histogram, query, VRAM, genlock, telemetry, or metric. Translate:
     - sync drift        -> "the wall image is tearing on camera"
     - VRAM exhaustion   -> "those panels will go black mid-take"
     - tracking drift    -> "the background isn't lining up with the camera
                             move, so those shots need VFX cleanup later"
     - thermal throttling-> "those panels are running hot and frames are
                             landing late; it won't error, it'll just look
                             slightly wrong in the dailies"

2. NEVER use a bare node, tracker or camera ID. Always give it a physical
   handle: "seven panels on the render wall (node_12 through node_18)" or
   "the A-camera tracker (tracker_3)". The ID in parentheses is fine -- the
   plain-language part must come first and must carry the meaning.

3. COPY THE `cost_display` STRING FROM THE DATA ABOVE, CHARACTER FOR
   CHARACTER. It is already formatted -- if it says "$55,110" you write
   "$55,110", not "$55110.0" and not "about $55k". Do not recompute it, round
   it, or substitute a "more reasonable" number. That figure came from a
   deterministic cost model; one you invent is worse than none, because a
   producer will act on it. Set the `cost_usd` field to `total_cost_usd`
   unchanged. Say briefly what it covers -- lost stage time, cleanup,
   reshoots -- using the `cost_basis` field.

4. WHAT TO DO must be a specific physical action someone on the floor can
   take right now: swap a panel, re-run a tracker calibration, move a setup
   to a different part of the wall, keep shooting and flag it for cleanup.
   Not "investigate further" and not "escalate to the vendor".

5. HOW LONG is an honest estimate in minutes, and say whether shooting can
   continue while it happens.

6. If the investigation ruled candidates out, list them in
   considered_and_rejected in one plain clause each -- "the network errors on
   another panel, which started after the tearing had already begun". This
   is what tells the crew the call was reasoned, not guessed.

7. Lead with the headline. One sentence, the thing they must know. If nothing
   is actually broken, say so plainly rather than manufacturing urgency.

Be direct and calm. No hedging, no filler, no apology. Short sentences.
"""


def build_brief_agent() -> LlmAgent:
    """The crew-facing brief writer."""
    return LlmAgent(
        name="brief",
        model=build_model(BRIEF_MODEL),
        description="Turns a scored investigation into a crew-ready brief.",
        instruction=INSTRUCTION,
        output_schema=CrewBrief,
        output_key="brief",
    )
