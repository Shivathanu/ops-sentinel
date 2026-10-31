# Ops Sentinel

> An AI SRE for data platforms: it turns alerts into triaged, investigated incidents, routes them to the right
> people, and never changes anything without a human's approval. Exasol is its system of record.

## The problem

**Who has it.** Platform, data-engineering and on-call SRE teams at companies whose decisions run on data
pipelines: nightly ETL into a warehouse, finance and executive dashboards, services that depend on fresh tables.

**What goes wrong.** A pipeline fails at 2 a.m. or a runaway query starts burning compute. The alerts are spread
across Prometheus, Grafana, CI and cron jobs, and they repeat. The on-call engineer has to work out by hand which
alert matters, read logs, query the warehouse, trace lineage to see which dashboard is now stale, check whether this
happened before, and decide on a fix, usually alone, at night, with admin credentials and no record of why.

**Why it matters.**

- **Decisions are made on stale data.** By the time someone notices, the morning finance dashboard is already wrong
  and people have acted on it. Trust in the data platform erodes.
- **Time to resolution is spent on investigation**, not on the fix. The same failures recur and are investigated
  from scratch each time, because what was learned last time is in someone's head.
- **Cost waste is silent.** Expensive queries run for hours before a person looks.
- **Automation is risky.** Letting a script or a model "just fix it" with production credentials is how outages
  get worse. Teams need speed *and* control.

**What Ops Sentinel does about it.** It ingests alerts from any tool, groups duplicates into one incident, and has a
team of AI agents triage it, investigate it with read-only tools (SQL on Exasol, incident history), and explain
the business impact and root cause with evidence. The incident is routed to the right channel per team and
severity. Fixes are limited to a catalog of pre-approved actions and run only after a named person approves.
Every step, including every model call and tool call, is stored in Exasol for audit.

## How it works

```
 Alertmanager / Grafana / CI / any webhook / the CLI runner
            │  POST /v1/ingest/{workspace}/{source}
            ▼
 ┌─────────────────────────── engine (api + worker) ───────────────────────────┐
 │ normalize → fingerprint → one incident per fingerprint ─► job queue         │
 │                                                              │              │
 │  route (team, severity, labels) ─► Slack / webhook          ▼              │
 │                                        AI SRE: triage ─► investigator       │
 │                                           │   delegates to data_analyst     │
 │                                           │   (read-only SQL) and           │
 │                                           │   history_analyst (past         │
 │                                           │   incidents)                    │
 │                                           ▼                                 │
 │                           impact, root cause, evidence, suggested action    │
 │                           ─► attached to the incident, messages updated     │
 └──────────────────────────────────────┬──────────────────────────────────────┘
                                        ▼
 Exasol: incidents · signals · timeline · jobs · agent runs & steps · evidence · audit · knowledge
         + Python UDFs (fingerprint, vector similarity, anomaly scores) where the database supports them
```

| Concept | In one line |
| --- | --- |
| **Workspace spec** | One `sentinel.yaml` per team: sources, channels, routes, models, toolsets, actions, policies |
| **Source** | Where alerts come from (`alertmanager`, `grafana`, `webhook`, `sentinel_runner`, plugins) |
| **Incident** | Signals with the same fingerprint, with a lifecycle and a full timeline |
| **Channel / route** | Who hears about it (Slack, signed webhook, plugins), by severity, labels and source |
| **Agent** | A declarative AI role with instructions, a model role, tools, subagents, budgets and a result schema |
| **Toolset** | Read-only tools an agent may call; write tools can never run from model output |
| **Action / policy** | Pre-approved remediations and the rules for who may approve them |

Design details: [docs/design/ai-sre-platform.md](docs/design/ai-sre-platform.md).
Pitch deck: [docs/pitch/ops-sentinel-pitch.pptx](docs/pitch/ops-sentinel-pitch.pptx) ([PDF](docs/pitch/ops-sentinel-pitch.pdf)).

## Status

| Part | State |
| --- | --- |
| Ingest, correlation, routing, Slack and webhook delivery, auto-resolve, timeline | Runs end to end against a live Exasol (verified on the starter kit) |
| Platform schema, migrations, job queue, least-privilege users | Runs against a live Exasol |
| Agent runtime (triage, investigator, subagents, read-only Exasol tools, budgets, audit) | Implemented and covered by tests; a full live analysis with a real model is pending |
| Python UDFs | Installed on Exasol editions with a Python 3 container; otherwise the engine runs the same code |
| Remediation through the action catalog, policies and approval | Next phase |
| CLI runner reporting through the ingest API | Next phase |

224 automated tests cover the engine (`pytest engine`).

## Quickstart

The full walkthrough, with expected output at each step, is in the **[run guide](docs/getting-started.md)**.
In short:

```bash
# 1. Install the engine tools (Python 3.10+)
python -m venv .venv && source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -e "engine[dev]"

# 2. Once per Exasol: schema and least-privilege users, credentials written into your project's .env
python engine/scripts/bootstrap_sentinel_roles.py --env-file my-sentinel/.env

# 3. Describe your team and check it offline
cp examples/sentinel.yaml my-sentinel/ && sentinel-validate my-sentinel/sentinel.yaml

# 4. Build the image, then start api + worker (migrations and UDFs are applied on start)
docker build -t ops-sentinel:latest engine
cp deploy/compose/compose.yaml my-sentinel/ && cd my-sentinel && docker compose up -d
```

Production deployment (images, secrets, scaling, TLS, upgrades, backups): **[docs/deployment.md](docs/deployment.md)**.

## Usage

**Send an alert.** Point a tool at the source's URL. Alertmanager example:

```yaml
receivers:
  - name: ops-sentinel
    webhook_configs:
      - url: https://sentinel.example.com/v1/ingest/data-platform/prometheus
        http_config: { authorization: { credentials: <AM_INGEST_TOKEN> } }
```

Anything that can POST works with a `webhook` source:

```bash
curl -X POST https://sentinel.example.com/v1/ingest/data-platform/jobs \
  -H "X-API-Key: $OPS_SENTINEL_API_KEY" \
  -d '{"title": "Backup job exited 1", "severity": "warning", "id": "backup-db-2"}'
```

**Read incidents.**

```bash
curl -H "X-API-Key: $OPS_SENTINEL_API_KEY" https://sentinel.example.com/v1/workspaces/data-platform/incidents
curl -H "X-API-Key: $OPS_SENTINEL_API_KEY" https://sentinel.example.com/v1/workspaces/data-platform/incidents/<id>
```

The detail view returns the incident, its analysis and its timeline (`opened`, `routed`, `notified`,
`agent.started`, `agent.tool`, `analysis`, `status.resolved`, ...). Everything is also queryable in Exasol, for example
`SELECT * FROM OPS_SENTINEL.AGENT_STEPS WHERE RUN_ID = ...`.

**Configure a team.** A complete `sentinel.yaml` is in [examples/sentinel.yaml](examples/sentinel.yaml). The
essentials:

```yaml
workspace: data-platform
sources:
  - { name: prometheus, type: alertmanager, token: "${env:AM_INGEST_TOKEN}" }
channels:
  oncall: { type: slack, token: "${env:SLACK_BOT_TOKEN}", channel: "#data-oncall", interactive: true }
routes:
  - match: { severity_at_least: high }
    notify: [oncall]
    approve: oncall
models:
  smart: { provider: anthropic, model: claude-opus-5-5, api_key: "${env:ANTHROPIC_API_KEY}" }
  fast:  { provider: anthropic, model: claude-haiku-5-5, api_key: "${env:ANTHROPIC_API_KEY}" }
toolsets:
  exasol: { schemas: [SENTINEL] }
```

Secrets are always references (`${env:NAME}`, `${file:/run/secrets/name}`), never values. Several teams share one
deployment by putting one spec per team in a directory. Models can be Claude, OpenAI or a local model through
Ollama (`provider: ollama`).

**Engine commands.**

| Command | Purpose |
| --- | --- |
| `uvicorn sentinel.main:app` | The api (ingest, incidents, health, Slack callbacks) |
| `sentinel-worker` | Processes jobs: routing, notifications, AI analysis |
| `sentinel-migrate [--status]` | Applies platform migrations and installs UDFs (the api does this on start by default) |
| `sentinel-validate [spec]` | Checks specs offline and prints ingest URLs and routing |

## Repository layout

```
engine/             Python engine (pyproject.toml, Dockerfile); import name `sentinel`
  sentinel/         api, ingest, agents, llm, store (Exasol), channels, workers, udf_lib
  tests/            unit tests
  scripts/          database bootstrap, demo data, live checks
  dashboard/        Streamlit dashboard (trust and efficiency scores)
cli/                ops-sentinel-cli: TypeScript runner that watches commands, logs and endpoints (npm)
deploy/compose/     docker compose for api + worker (optional local Exasol)
examples/           example workspace spec
sql/                schema and roles for the demo pipeline tables
docs/               run guide, deployment, design, pitch deck
```

## Security model

Speed must not cost control, so every path that could change something has independent guards:

1. **Agents are read-only.** Tools declare a capability, and the loop only runs `read` tools. A write tool called by
   a model is refused and recorded. The Exasol toolset connects as `mcp_readonly`, so the database enforces it too.
2. **Model input is data.** Alert text and tool output are treated as data, arguments are checked against each
   tool's schema, output is size-limited, and known secrets are redacted before anything reaches a model.
3. **Pre-approved actions only.** A fix is an action id from the team's catalog, never model-written code. Runner
   actions keep their commands on the client machine; the engine only names the id.
4. **Human approval.** Only listed approvers can approve, Slack requests are signature-checked, and an atomic claim
   means a double click runs a fix once.
5. **Least-privilege database users.** `sentinel_owner` migrates, `sentinel_engine` runs (and can only append to
   the audit log), `sentinel_executor` runs approved SQL on named business tables, `mcp_readonly` investigates.
6. **SQL guardrail** for SQL fixes: one allow-listed statement, a real `WHERE`, no DDL, no Sentinel or system
   tables, target limited to the incident's lineage.
7. **Everything is audited** in Exasol: incidents, signals, agent runs and steps, evidence, notifications, decisions.

Every control fails closed: a missing key, token, approver list or credential refuses the request.

## Development

```bash
pip install -e "engine[dev,dashboard]"
ruff check engine
pytest engine
cd cli && npm install && npm test
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## Licence

[Apache License 2.0](LICENSE).
