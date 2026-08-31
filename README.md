---
title: Volume Ops
emoji: 🎬
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
license: mit
---

# Volume Ops

An agentic on-call system for a virtual production LED volume stage — the kind
of film set used to shoot *The Mandalorian*. A simulator emits live stage
telemetry to Grafana Cloud over OTLP. When a signal degrades, a Gemini agent
built on Google ADK investigates through the official `grafana/mcp-grafana`
MCP server against the live instance, forms and rejects hypotheses with
recorded reasons, scores the surviving evidence-backed findings with a
deterministic pure-Python rule table, and reports the root cause in language a
film crew can act on — with a dollar cost attached.

Built for the Agentic Cinema hackathon, Grafana track.

---

## Architecture

```
                      ┌──────────────────────────────────────┐
                      │          Grafana Cloud               │
   OTLP/HTTP  ──────► │  Prometheus (metrics) · Loki (logs)  │
   1 Hz, Basic auth   └──────────────┬───────────────────────┘
        │                            │ queried via MCP
        │                            │
┌───────┴────────┐         ┌─────────▼──────────┐
│   simulator/   │         │  mcp-grafana v1.2  │  ← real server, stdio
│  stage + 4     │         │  7 curated tools   │     subprocess
│  fault modules │         └─────────┬──────────┘
└───────┬────────┘                   │
        │ same process               │
┌───────▼────────────────────────────▼─────────────────────────┐
│                        agent/  (Google ADK)                   │
│                                                               │
│   triage ──────► investigator ──────► scorer ──────► brief    │
│   LlmAgent       LoopAgent            PURE PYTHON   LlmAgent  │
│   flash-lite     flash, max 4         no LLM        flash     │
│                  iterations                                   │
└───────┬───────────────────────────────────────────────────────┘
        │ every step published as it happens
┌───────▼────────┐        ┌──────────────────────────────┐
│   api/  SSE    │───────►│  ui/  React + Vite + TS      │
│   FastAPI      │        │  stage · faults · trace      │
└────────────────┘        └──────────────────────────────┘
```

One container. FastAPI serves the built UI and runs the simulator on a
background thread, so a fault injected from the browser changes the telemetry
the agent is about to read.

---

## Where `mcp-grafana` is called

The agent talks to a real `grafana/mcp-grafana` process over stdio against a
live Grafana Cloud stack. There is no mock, stub, or canned-response path
anywhere in `agent/` or `api/`.

| What | Where |
|---|---|
| Toolset construction | [`agent/mcp_config.py:141`](agent/mcp_config.py#L141) — `build_grafana_toolset()` returns an ADK `McpToolset` |
| Server launch | [`agent/mcp_config.py:120`](agent/mcp_config.py#L120) — `StdioServerParameters(command=…)` spawns the binary |
| Tool allowlist | [`agent/mcp_config.py:39`](agent/mcp_config.py#L39) — `ALLOWED_TOOLS`, enforced server-side at [`:123`](agent/mcp_config.py#L123) and client-side at [`:145`](agent/mcp_config.py#L145) |
| Handed to the agent | [`agent/investigator.py:142`](agent/investigator.py#L142) — `tools=[build_grafana_toolset(), *TRACE_TOOLS]` |

If Grafana is unreachable the toolset raises and the investigation fails
loudly with the error surfaced to the UI. It never falls back to fake data.

### The curated tool surface

`mcp-grafana` exposes 60+ tools, and handing all of them to a model makes tool
selection unreliable. The agent is restricted to these, enforced twice — at the
server via `--enabled-tools` and again in the ADK toolset. `--disable-write`
keeps them read-only:

```
query_prometheus   query_loki_logs   query_loki_stats   list_datasources
search_dashboards  alerting_manage_rules   list_incidents
```

## Where Gemini is called

Model names and the shared retry policy live in [`agent/llm.py`](agent/llm.py).
Both Gemini backends work — set `GOOGLE_API_KEY` for the Gemini Developer API,
or `GOOGLE_GENAI_USE_VERTEXAI=true` with a GCP project for Vertex AI.

Triage runs on a different model from the investigation loop on purpose. Free-tier
request limits are metered per model, so splitting the pipeline gives it two
independent daily budgets instead of one shared pool.

| Agent | Model | Defined | Used |
|---|---|---|---|
| Triage | `gemini-3.1-flash-lite` | [`llm.py:32`](agent/llm.py#L32) | [`triage.py:67`](agent/triage.py#L67) |
| Investigator | `gemini-3.6-flash` | [`llm.py:36`](agent/llm.py#L36) | [`investigator.py:139`](agent/investigator.py#L139) |
| Brief | `gemini-3.6-flash` | [`llm.py:37`](agent/llm.py#L37) | [`brief.py:90`](agent/brief.py#L90) |

The loop is capped at 4 iterations ([`investigator.py:40`](agent/investigator.py#L40)),
raisable to the spec's 8 via `VOLUME_OPS_MAX_ITERATIONS`.

## Where no model is called

[`agent/scorer.py`](agent/scorer.py) is pure deterministic Python — a rule
table at [`:99`](agent/scorer.py#L99) and arithmetic at
[`:130`](agent/scorer.py#L130). No LLM call appears in that file, by design.
The dollar figure a producer acts on is computed, not generated.

It also enforces the project's central rule: a finding carrying no evidence is
**dropped before it reaches the brief** ([`:152`](agent/scorer.py#L152)).

---

## Setup

Python 3.11+, Node 20+.

```bash
cp .env.example .env        # then fill it in
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
```

`.env`:

| Variable | What it is |
|---|---|
| `GRAFANA_OTLP_ENDPOINT` | OTLP gateway for your region |
| `GRAFANA_OTLP_INSTANCE_ID` | Numeric stack id — the Basic auth username |
| `GRAFANA_OTLP_TOKEN` | `glc_…` write token — the Basic auth password |
| `GRAFANA_URL` | Your stack, `https://<name>.grafana.net` |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | `glsa_…` read token for `mcp-grafana` |
| `GOOGLE_API_KEY` | Gemini API key — used when `GOOGLE_GENAI_USE_VERTEXAI=false` |
| `GOOGLE_GENAI_USE_VERTEXAI` | `false` for the Gemini Developer API, `true` for Vertex AI |
| `GOOGLE_CLOUD_PROJECT` | GCP project — Vertex only |
| `GOOGLE_CLOUD_LOCATION` | e.g. `us-central1` — Vertex only |
| `DEPLOYMENT_ENV` | Label stamped on every metric and log. Defaults to `local`; a deployment sets its own (`space`) |

For Vertex, authenticate with ADC instead of a key:

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project $GOOGLE_CLOUD_PROJECT
```

`mcp-grafana` — download the release binary into `.tools/` (gitignored), or
set `MCP_GRAFANA_BINARY`:

```bash
# https://github.com/grafana/mcp-grafana/releases  →  .tools/mcp-grafana[.exe]
```

Run:

```bash
# simulator only
python -m simulator.stage                       # 1 Hz, runs indefinitely
python -m simulator.stage --seconds 60 --fault genlock_loss

# full app — API + UI + simulator, one process
uvicorn api.main:app --port 7860
cd ui && npm install && npm run build           # then open http://localhost:7860
cd ui && npm run dev                            # or hot-reload on :5173

# tests
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/ -q
```

Docker:

```bash
docker build -t volume-ops .
docker run -p 7860:7860 --env-file .env volume-ops   # http://localhost:7860
```

### Deploying

Hugging Face Spaces, Docker SDK. The frontmatter at the top of this file is
what selects it — `sdk: docker`, `app_port: 7860`.

```bash
git remote add space https://huggingface.co/spaces/<user>/volume-ops
git push space main
```

Set the six secrets under **Settings → Variables and secrets**; they arrive
as environment variables:

```
GRAFANA_OTLP_ENDPOINT   GRAFANA_OTLP_INSTANCE_ID   GRAFANA_OTLP_TOKEN
GRAFANA_URL             GRAFANA_SERVICE_ACCOUNT_TOKEN
GOOGLE_API_KEY
```

Set `DEPLOYMENT_ENV=space` as a **variable** (not a secret — it is not
sensitive, and you want to read it back). Every metric and log the Space emits
is stamped with it, and every Grafana query the agent writes filters on it.
Without it a deployed Space and a laptop write the same series names with the
same entity labels into one stack: `node_07` from one is indistinguishable
from `node_07` from the other, so the agent silently reads a blend of two
stages — no error, no empty result, just plausible wrong numbers.

A missing or misspelled secret degrades the demo rather than killing it: the
container stays up, the UI still loads, `GET /health` names the variable that
is missing in `simulator_error`, and the fault routes answer 503. That is
deliberate — a crash loop on a host that injects secrets for you hides the
reason in container logs where nobody looks.

The simulator runs inside the container on a background thread started by the
FastAPI lifespan, so telemetry flows whenever the Space is awake — it does not
depend on any laptop being on. A stranger can open the URL, inject a fault
from the panel, and watch a full investigation.

---

## The stage

| Entity | Instances |
|---|---|
| Cameras | `cam_a`, `cam_b`, `cam_c` |
| Trackers | `tracker_1` … `tracker_6` |
| Render nodes | `node_01` … `node_24` |
| Thermal zones | `north`, `south`, `east`, `west` |
| Sequences | `seq_041`, `seq_042`, `seq_043` |

Render nodes drive the LED wall. Trackers feed camera position so background
parallax matches camera movement. Tracking drift means VFX cleanup; lost
genlock means the wall tears on camera; exhausted VRAM means a wall segment
goes black mid-take.

## The four faults

Injectable from the UI at `POST /faults/{name}/start`.

1. **`tracker_drift`** — cam_a latency ramps 12 → 45 ms, tracker_3 confidence
   decays 0.97 → 0.61 over 90s. Every other camera and tracker stays healthy.
   Solvable only by slicing per camera, pivoting to tracker confidence, then
   searching logs *before* onset for the wall-segment reposition.

2. **`genlock_loss`** — node_07 sync drift 0.2 → 8.0 ms, then oscillating.
   A **decoy** runs alongside: node_12 emits louder, unrelated network errors
   starting 20s later with no metric impact. It must be rejected **on timing
   grounds** and the rejection recorded, not silently dropped.

3. **`vram_leak`** — seven nodes climb 55% → 97% over 20 minutes; past 90%,
   frames fail on seq_042 and the queue backs up. The root cause is a driver
   patch logged **hours before** the alert window. Everything inside the window
   is a symptom.

4. **`thermal_throttle`** — north zone 68 → 87 °C, frame times degrade, notices
   at `debug` only. **No errors, no failures, no alert.** Silent quality decay.
   Used once, live, to demonstrate generalization — never tuned against.

---

## API

| Route | Purpose |
|---|---|
| `POST /webhook/alert` | Grafana alert intake; starts an investigation, returns its id |
| `GET /stream/investigation/{id}` | SSE — one event per hypothesis, query and rejection, live |
| `POST /faults/{name}/start` · `/stop` | Injection control |
| `GET /faults` | Current fault state |
| `GET /stage/status` | Live signals for the UI |
| `GET /health` | Liveness plus simulator and config state |

---

## Notes from the build

Two platform behaviours shaped the implementation, both measured rather than
assumed:

**Grafana Cloud promotes only `service.name` to a Loki index label.** It
arrives as `service_name`; everything else (`node`, `level`, `sequence`)
becomes structured metadata. `{service="render_worker"}` returns **zero lines
with no error**, which a model reads as "healthy". The agents are taught the
correct form explicitly in
[`agent/mcp_config.py`](agent/mcp_config.py#L152).

**Grafana Cloud Loki silently drops OTLP log records older than 3 hours.**
Probes at 180 minutes land; 195 minutes do not. `BUILD_SPEC` places the VRAM
leak's driver patch at T-8h, which is un-ingestable on this platform — the
record simply vanishes. It is backdated to 2.5h instead, documented in
[`simulator/faults/vram_leak.py`](simulator/faults/vram_leak.py#L15). The
scenario is unaffected: a 15-minute alert window still misses it by over two
hours.

---

## License

MIT — see [LICENSE](LICENSE).
