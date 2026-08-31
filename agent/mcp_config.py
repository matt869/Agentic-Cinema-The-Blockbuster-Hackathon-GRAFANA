"""Curated mcp-grafana toolset.

Builds the ADK ``McpToolset`` connected to the official ``grafana/mcp-grafana``
server against the live Grafana Cloud instance, restricted to exactly eight
tools.

``grafana/mcp-grafana`` exposes 60+ tools. Handing all of them to a model makes
tool selection unreliable, so the allowlist is deliberate and enforced twice:
once at the server via ``--enabled-tools``, and again client-side via ADK's
``tool_filter``. The server-side flag is what actually shrinks the surface; the
client-side filter is what fails loudly if a future server version renames
something.

There is no offline or fallback path here by design. If Grafana is
unreachable the toolset raises and the agent fails visibly -- it never
substitutes canned data.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset

from simulator.otlp_client import DEPLOYMENT
from mcp import StdioServerParameters

#: The tools this project is allowed to call. Widening this list is a
#: deliberate decision, not an implementation detail -- see CLAUDE.md.
#:
#: CLAUDE.md names eight tools. mcp-grafana consolidated ``list_alert_rules``
#: and ``get_alert_rule_by_uid`` into the single ``alerting_manage_rules`` tool
#: in v0.11.3 (2026-03-12), so on any current server those two names do not
#: exist and the curated surface is seven tools covering the same eight
#: capabilities. ``--disable-write`` keeps the consolidated tool read-only.
ALLOWED_TOOLS: tuple[str, ...] = (
    "query_prometheus",
    "query_loki_logs",
    "query_loki_stats",
    "list_datasources",
    "search_dashboards",
    "alerting_manage_rules",  # was: list_alert_rules + get_alert_rule_by_uid
    "list_incidents",
)

#: mcp-grafana's ``--enabled-tools`` flag takes tool *categories*, not tool
#: names. These are the categories containing ALLOWED_TOOLS; everything else
#: -- including the ``proxied`` datasource tools -- stays off.
ENABLED_CATEGORIES: tuple[str, ...] = (
    "datasource",
    "prometheus",
    "loki",
    "alerting",
    "search",
    "incident",
)

#: Datasource UIDs on this stack, resolved once via list_datasources.
PROMETHEUS_UID = "grafanacloud-prom"
LOKI_UID = "grafanacloud-logs"

_REQUIRED_VARS = ("GRAFANA_URL", "GRAFANA_SERVICE_ACCOUNT_TOKEN")


def _resolve_binary() -> str:
    """Locate the mcp-grafana executable.

    Checked in order: ``MCP_GRAFANA_BINARY``, the repo-local ``.tools``
    directory used for development, then ``PATH`` -- which is where the
    container image puts it.
    """
    override = os.environ.get("MCP_GRAFANA_BINARY")
    if override:
        if not Path(override).exists():
            raise RuntimeError(f"MCP_GRAFANA_BINARY points at {override}, which does not exist")
        return override

    root = Path(__file__).resolve().parent.parent / ".tools"
    for candidate in (root / "mcp-grafana.exe", root / "mcp-grafana"):
        if candidate.exists():
            return str(candidate)

    found = shutil.which("mcp-grafana")
    if found:
        return found

    raise RuntimeError(
        "mcp-grafana executable not found. Install it from "
        "https://github.com/grafana/mcp-grafana/releases into .tools/, "
        "or set MCP_GRAFANA_BINARY."
    )


def grafana_env() -> dict[str, str]:
    """Credentials the MCP server needs, read from the environment."""
    load_dotenv()
    missing = [v for v in _REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". The agent cannot query Grafana without them."
        )
    return {
        "GRAFANA_URL": os.environ["GRAFANA_URL"],
        "GRAFANA_SERVICE_ACCOUNT_TOKEN": os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"],
        # Go binaries need a writable temp dir and a resolvable PATH.
        "PATH": os.environ.get("PATH", ""),
        "TEMP": os.environ.get("TEMP", "/tmp"),
        "TMP": os.environ.get("TMP", "/tmp"),
    }


def connection_params() -> StdioConnectionParams:
    """Stdio connection to a locally launched mcp-grafana server."""
    return StdioConnectionParams(
        server_params=StdioServerParameters(
            command=_resolve_binary(),
            args=[
                "--enabled-tools", ",".join(ENABLED_CATEGORIES),
                # This agent only ever reads. Without it, the consolidated
                # alerting tool would also create, update and delete rules.
                "--disable-write",
            ],
            env=grafana_env(),
        ),
        timeout=60.0,
    )


#: Every toolset built for an investigation. Each one owns a live
#: mcp-grafana subprocess that does NOT go away on garbage collection, so the
#: caller must close them when the investigation ends or the container
#: accumulates one stranded server process per alert.
_LIVE_TOOLSETS: list[McpToolset] = []


def build_grafana_toolset() -> McpToolset:
    """The curated Grafana toolset handed to the investigator agent."""
    toolset = McpToolset(
        connection_params=connection_params(),
        tool_filter=list(ALLOWED_TOOLS),
    )
    _LIVE_TOOLSETS.append(toolset)
    return toolset


async def close_toolsets() -> None:
    """Shut down every mcp-grafana subprocess started for an investigation."""
    while _LIVE_TOOLSETS:
        toolset = _LIVE_TOOLSETS.pop()
        try:
            await toolset.close()
        except Exception:  # a dead session must not block the rest
            logging.getLogger("agent.mcp_config").warning(
                "toolset close failed", exc_info=True
            )


#: What the agent needs to know to query this stack correctly. Two of these
#: are hard-won and non-obvious; getting either wrong returns empty results
#: with no error, which reads to a model as "signal is healthy".
GRAFANA_QUERY_GUIDE = f"""\
DATASOURCE UIDS (pass as datasourceUid):
  Prometheus metrics : {PROMETHEUS_UID}
  Loki logs          : {LOKI_UID}

query_prometheus REQUIRES `endTime` (RFC3339 or 'now'). For queryType
'range' you must also pass `startTime` and `stepSeconds`.

DEPLOYMENT FILTER -- MANDATORY ON EVERY QUERY:
More than one stage writes into this Grafana. They share metric names AND
entity labels, so node_07 from another deployment is indistinguishable from
yours. An unfiltered query silently interleaves two stages and every number
you read is a blend of both. There is no error and no empty result -- just
wrong values that look plausible.

This deployment is `{DEPLOYMENT}`. Filter EVERY query on it.

The two query languages place the filter differently. This is not optional
formatting -- putting it in the wrong place either errors or silently matches
nothing:

  PromQL -- `deployment` IS a series label, so it goes INSIDE the selector:
    CORRECT   stage_gpu_temp_celsius{{deployment="{DEPLOYMENT}", zone="north"}}
    CORRECT   stage_render_queue_depth{{deployment="{DEPLOYMENT}"}}
    WRONG     stage_gpu_temp_celsius{{zone="north"}}   <- blends deployments

  LogQL -- `deployment` is structured metadata, NOT an index label, so it
  goes AFTER the pipe like node and level:
    CORRECT   {{service_name="render_worker"}} | deployment="{DEPLOYMENT}"
    CORRECT   {{service_name="stage_control"}} | deployment="{DEPLOYMENT}" |= "patch"
    WRONG     {{service_name="render_worker", deployment="{DEPLOYMENT}"}}
              <- returns 0 lines, no error

LOG QUERY FORM -- READ THIS CAREFULLY:
Grafana Cloud promotes only `service.name` to a Loki index label. It is
called `service_name`, NOT `service`. Every other field -- node, level,
sequence, tracker -- is structured metadata and CANNOT go in the {{}} selector.

  CORRECT   {{service_name="render_worker"}} | node="node_07"
  CORRECT   {{service_name="stage_control"}} |= "repositioned"
  CORRECT   {{service_name="render_worker"}} | level="error"
  WRONG     {{service="render_worker"}}          <- returns 0 lines, no error
  WRONG     {{node="node_07"}}                   <- returns 0 lines, no error

service_name is one of: stage_control, render_worker, tracker_daemon.

METRICS AND THEIR LABELS:
Every metric below ALSO carries `deployment`, which you must always match on
as shown above. The labels listed are the ones that identify the entity.
  stage_camera_tracking_latency_ms      labels: camera, tracker   healthy 8-12
  stage_tracker_calibration_confidence  labels: tracker           healthy 0.95-0.98
  stage_led_wall_sync_drift_ms          labels: node              healthy 0.1-0.3
  stage_gpu_vram_used_bytes             labels: node              healthy 45-60% of total
  stage_gpu_vram_total_bytes            labels: node
  stage_gpu_temp_celsius                labels: node, zone        healthy 62-71
  stage_render_frame_duration_seconds   labels: node, sequence    histogram, p99 ~0.038
  stage_frame_failures_total            labels: node, sequence    healthy: flat, no increments
  stage_render_queue_depth              labels: sequence          healthy 4-12

STAGE TOPOLOGY:
  cameras cam_a cam_b cam_c; trackers tracker_1..tracker_6
  render nodes node_01..node_24; zones north/south/east/west (6 nodes each,
  north=node_01..06, south=07..12, east=13..18, west=19..24)
  sequences seq_041 (node_01..08), seq_042 (node_09..18), seq_043 (node_19..24)
  cam_a is fed by tracker_1 and tracker_3; cam_b by tracker_2 and tracker_4;
  cam_c by tracker_5 and tracker_6.
"""
