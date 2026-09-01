"""Per-investigation cache for Grafana reads.

A run of the loop observed the investigator issue this LogQL twice, 27
seconds apart, byte for byte:

    {service_name="render_worker"} | deployment="local" | level="error"   now-1h

Each repeat costs a model call, and the free tier allows 20 per model per
DAY -- so one duplicated query is 5% of a day's budget spent re-reading
something already in the transcript. The model does this because a tool result
scrolls back in context and re-asking feels cheaper than re-reading; it is not.

Scope is one investigation. The cache lives in session state, so it is created
and discarded with the session and can never leak a reading from one incident
into another.

ONLY read-only Grafana queries are cached. The trace tools -- propose,
reject, record -- must run every time: they are the investigation's writes,
and serving a cached "already recorded" would silently drop reasoning.

A note on time: "now-1h" is cached as the literal string, not as the instant
it resolves to. Two such queries seconds apart are treated as identical, which
is the intent -- the model is re-asking the same question, not sampling a
moving window. Over a bounded investigation of a few minutes the drift is
smaller than the 1 Hz simulation's own noise. An investigation that genuinely
needs a fresh read writes a different range.
"""

from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("agent.query_cache")

#: Session-state key holding this investigation's cached reads.
CACHE_STATE_KEY = "query_cache"

#: Read-only Grafana tools. Anything not listed here always executes.
CACHEABLE_TOOLS: frozenset[str] = frozenset({
    "query_prometheus",
    "query_loki_logs",
    "query_loki_stats",
    "list_datasources",
    "get_datasource_by_uid",
    "list_prometheus_metric_names",
    "list_loki_label_names",
    "list_loki_label_values",
})

#: Upper bound on retained entries. An investigation is bounded to a handful
#: of iterations, so this is a guard against a pathological loop rather than a
#: working limit -- reaching it means something else has gone wrong.
MAX_ENTRIES = 64

#: Marker added to a served response. api.routes_webhook reads it to show the
#: hit in the live trace, so a saved call is visible rather than merely absent.
CACHE_MARKER = "_volume_ops_cache"


def cache_key(tool_name: str, args: dict[str, Any]) -> str:
    """Stable key for one query: the tool plus its arguments, canonicalised.

    Sorted keys, so the same query written with its arguments in a different
    order is still recognised as the same query -- which the model does.
    """
    try:
        canonical = json.dumps(args or {}, sort_keys=True, default=str)
    except Exception:  # pragma: no cover - defensive
        canonical = repr(sorted((args or {}).items()))
    return f"{tool_name}::{canonical}"


def before_tool(tool: Any, args: dict[str, Any], tool_context: Any) -> Any:
    """Serve a repeated Grafana read from this investigation's cache.

    Returning a value tells ADK to skip the real tool call, which is the whole
    point: the saving is the round trip, not the parsing.
    """
    name = getattr(tool, "name", "") or ""
    if name not in CACHEABLE_TOOLS:
        return None
    cache = (tool_context.state.get(CACHE_STATE_KEY) or {})
    key = cache_key(name, args)
    if key not in cache:
        return None
    hit = cache[key]
    log.info(
        "query cache hit: %s (saved one Grafana round trip and the model call "
        "that would have read its result)", name,
        extra={"extra_fields": {"tool": name, "args": args, "cache": "hit"}},
    )
    served = dict(hit) if isinstance(hit, dict) else {"result": hit}
    served[CACHE_MARKER] = "hit"
    return served


def after_tool(
    tool: Any, args: dict[str, Any], tool_context: Any, tool_response: Any
) -> Any:
    """Record a fresh Grafana read so its repeat costs nothing."""
    name = getattr(tool, "name", "") or ""
    if name not in CACHEABLE_TOOLS:
        return None
    if isinstance(tool_response, dict) and tool_response.get(CACHE_MARKER):
        return None  # already served from cache; nothing new to store
    cache = dict(tool_context.state.get(CACHE_STATE_KEY) or {})
    key = cache_key(name, args)
    if key in cache:
        return None
    if len(cache) >= MAX_ENTRIES:
        log.warning(
            "query cache full at %d entries; not caching further reads this "
            "investigation", MAX_ENTRIES,
        )
        return None
    cache[key] = tool_response
    tool_context.state[CACHE_STATE_KEY] = cache
    return None  # None keeps ADK's original response
