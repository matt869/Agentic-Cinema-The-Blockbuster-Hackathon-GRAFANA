"""Shared Gemini model construction.

One place to set the model name and the retry policy every agent uses.

The retry policy is not incidental. The Gemini API free tier allows five
requests per minute per model, and a single investigation makes far more than
that: triage, up to eight loop iterations, a further turn per tool call, and
the brief. Without backoff the run dies partway through with a 429 and the
investigation is lost after the expensive Grafana work has already been done.

The trade is latency, not correctness -- an investigation paced against a
5 RPM ceiling takes minutes rather than seconds. On a paid tier the same code
runs at full speed with no change.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from google.adk.models.google_llm import Gemini
from google.genai import types

load_dotenv()

#: BUILD_SPEC/CLAUDE.md specify gemini-2.5-flash and gemini-2.5-pro. Both were
#: retired: the Gemini API answers 404 "no longer available to new users" and
#: names the 3.x line as the replacement. gemini-3.6-flash is Google's own
#: named successor to 2.5-flash.
FLASH_MODEL = os.environ.get("VOLUME_OPS_MODEL", "gemini-3.6-flash")

#: The brief is specified to run on a pro-tier model. None is reachable on the
#: free tier -- pro models report
#: ``generate_content_free_tier_requests limit: 0``, a hard zero rather than a
#: rate limit -- so it runs on flash. Change this when a paid key is available.
BRIEF_MODEL = os.environ.get("VOLUME_OPS_BRIEF_MODEL", FLASH_MODEL)

#: Free tier is 5 requests/minute/model, so a retry must wait out a whole
#: minute-window rather than back off from milliseconds.
#:
#: Kept deliberately shallow. Every retry is itself a billable request against
#: the 20-per-day free-tier ceiling, so deep backoff spends the day's budget on
#: waiting rather than investigating. Three attempts clears a per-minute limit
#: without burning the daily allowance.
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
