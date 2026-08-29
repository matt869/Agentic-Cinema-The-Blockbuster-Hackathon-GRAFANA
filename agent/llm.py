"""Shared Gemini model construction and quota strategy.

One place to set model names and the retry policy every agent uses.

MODEL BUCKETS
The Gemini free tier meters requests per model per day, so putting every
agent on one model gives you a single shared pool that one investigation can
drain. Splitting the pipeline across two models gives two independent pools:
triage runs on a lite model, the investigator and brief share a second. The
same run then costs two smaller budgets rather than one large one.

MODEL CHOICE
Flash-class models throughout. No pro-tier model is used: on the free tier
they report `generate_content_free_tier_requests limit: 0` -- a hard zero
rather than a rate limit -- so a pro model cannot serve a request at all.

Every name is overridable by environment variable, so moving models or
backends is configuration rather than a code change.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.models.google_llm import Gemini
from google.genai import types

load_dotenv()

#: Triage is the cheap planning step and gets its own quota pool.
TRIAGE_MODEL = os.environ.get("VOLUME_OPS_TRIAGE_MODEL", "gemini-3.1-flash-lite")

#: The investigator does the real work. The brief shares its pool -- one call
#: at the very end, after the loop has already finished spending.
FLASH_MODEL = os.environ.get("VOLUME_OPS_MODEL", "gemini-3.6-flash")
BRIEF_MODEL = os.environ.get("VOLUME_OPS_BRIEF_MODEL", FLASH_MODEL)

#: Free tier is 5 requests/minute/model, so a retry must wait out a whole
#: minute-window rather than back off from milliseconds.
#:
#: Kept deliberately shallow. Every retry is itself a billable request against
#: the per-day ceiling, so deep backoff spends the day's budget on waiting
#: rather than investigating. Three attempts clears a per-minute limit without
#: burning the daily allowance.
RETRY = types.HttpRetryOptions(
    attempts=3,
    initial_delay=20.0,
    max_delay=65.0,
    exp_base=1.6,
    jitter=0.3,
    http_status_codes=[429, 502, 503, 504],
)


def build_model(model_name: str = FLASH_MODEL) -> Gemini:
    """A Gemini model that survives free-tier rate limiting."""
    return Gemini(model=model_name, retry_options=RETRY)
