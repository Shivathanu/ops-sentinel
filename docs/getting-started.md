# Run guide

This guide takes a team from nothing to "an alert reaches the right channel, the AI SRE has analysed it, and its
whole history is in Exasol" in about 15 minutes. Steps 0 to 6 were verified end to end on Windows with Docker
Desktop and the Exasol starter kit. For production, see [deployment.md](deployment.md).

```
alerting tool ──POST──► /v1/ingest/{workspace}/{source} ──► Exasol (signal, incident, job)
                                                               │
                                     worker claims the job ◄───┘──► routes ──► Slack / webhook / ...
                                                               └──► AI SRE: triage ─► investigation ─► analysis
```

## 0. Prerequisites

| Need | Check |
| --- | --- |
| Docker (Desktop or Engine) | `docker info` |
| An Exasol database | Local: the [Exasol starter kit](https://github.com/exasol) (`exakit start`, `exakit status` says `running`). Or any Exasol you can reach |
| Python 3.10+ (for the one-time bootstrap and `sentinel-validate`) | `python --version` |
| Optional: a Slack bot token with `chat:write` and the channel id | |
| Optional, for AI analysis: an Anthropic or OpenAI API key, or a local model in [Ollama](https://ollama.com) | `ollama list` |

Install the engine tools once, from the repository root: `pip install -e engine`.

## 1. Create a project folder

```
my-sentinel/
  sentinel.yaml        what to watch and who to tell (copy examples/sentinel.yaml)
  .env                 secrets (never commit)
  compose.yaml         api + worker (copy deploy/compose/compose.yaml)
  business-context.md  what the triage model should know about your business
```

In `.env`, set how containers reach Exasol and the engine API key:

```
EXASOL_ADMIN_HOST=host.docker.internal      # an Exasol on this machine; otherwise its host name
EXASOL_ADMIN_PORT=8563
EXASOL_VALIDATE_CERTIFICATE=false           # only for a local self-signed instance
OPS_SENTINEL_API_KEY=<python -c "import secrets; print(secrets.token_urlsafe(32))">
AM_INGEST_TOKEN=<another random token, one per source that has `token:`>
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNEL_ID=C0123456789
```

## 2. Bootstrap the database (once per Exasol)

```
python engine/scripts/bootstrap_sentinel_roles.py --env-file my-sentinel/.env
```

This creates the `OPS_SENTINEL` schema and the users `sentinel_owner` (migrations), `sentinel_engine` (runtime),
`sentinel_executor` (approved fixes) and `mcp_readonly` (investigation), with generated passwords written
**directly into `.env`**, never printed. With the starter kit it uses the stored `sys` login automatically; otherwise
set `EXASOL_DBA_USER` and `EXASOL_DBA_PASSWORD` for this one run. Running it again is safe, and existing users keep
their passwords unless you pass `--reset-passwords`.

## 3. Describe your workspace and validate it

A minimal `sentinel.yaml`:

```yaml
workspace: demo
sources:
  - { name: prometheus, type: alertmanager, token: "${env:AM_INGEST_TOKEN}" }
  - { name: jobs, type: webhook }
channels:
  oncall: { type: slack, token: "${env:SLACK_BOT_TOKEN}", channel: "${env:SLACK_CHANNEL_ID}", interactive: true }
routes:
  - match: { severity_at_least: high }
    notify: [oncall]
    approve: oncall
```

```
sentinel-validate my-sentinel/sentinel.yaml
```

It reads the `.env` next to the spec, needs no database, and prints each source's ingest URL and where a critical
and a medium incident would be routed. Fix anything it reports before starting.

### 3b. Turn on the AI SRE (optional)

Add model roles and the read-only Exasol toolset to `sentinel.yaml`:

```yaml
models:
  smart: { provider: anthropic, model: claude-opus-5-5, api_key: "${env:ANTHROPIC_API_KEY}" }
  fast:  { provider: anthropic, model: claude-haiku-5-5, api_key: "${env:ANTHROPIC_API_KEY}" }
  # Fully local instead:  smart: { provider: ollama, model: llama3.1:8b, base_url: http://host.docker.internal:11434/v1 }
toolsets:
  exasol: { schemas: [SENTINEL] }        # schemas the agents may read
actions:                                 # pre-approved fixes the investigator may suggest
  - { id: rerun-finance-export, description: "Re-run the finance export", runner: etl-box, risk: low }
```

and to `.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
EXA_USER=mcp_readonly                    # the read-only investigation login from step 2
EXA_PASSWORD=...
```

Without `models`, everything else works and analysis is skipped. A local model needs a GPU to be practical: on a
CPU, one 8B-model turn can take minutes.

## 4. Start the engine

```
docker build -t ops-sentinel:latest engine     # from the repository root
cd my-sentinel
docker compose up -d
docker compose ps            # api and worker both "healthy"
curl localhost:8000/v1/readyz
```

Build the image first: the `build:` path in `compose.yaml` is relative to `deploy/compose/`, so it does not resolve
from a copied project folder. On start, the api applies pending migrations, installs the UDFs, records the spec version in Exasol and validates
every channel. If anything is wrong it exits with the reason (`docker compose logs api`) instead of half-starting.

> **UDFs and the starter kit.** The local "nano" runtime has no Python 3 script language container, so the log
> says `UDFs disabled` and the engine runs the same fingerprint, similarity and anomaly code itself. Nothing else
> changes. On a full Exasol with the standard script languages, the UDFs install and run inside the database.

## 5. Connect your alert sources

| Tool | Configure |
| --- | --- |
| Prometheus Alertmanager | `webhook_configs: [{url: "http://<engine>:8000/v1/ingest/demo/prometheus", http_config: {authorization: {credentials: "<AM_INGEST_TOKEN>"}}}]` |
| Grafana alerting | Contact point "Webhook" with the same URL; source `type: grafana`; Authorization header `Bearer <token>` |
| Anything else (CI, cron, scripts) | `curl -X POST <engine>/v1/ingest/demo/jobs -H "X-API-Key: $OPS_SENTINEL_API_KEY" -d '{"title":"Backup failed","severity":"warning","id":"backup-1"}'` |

## 6. See the flow

Send a test alert as Alertmanager would, then read it back:

```
curl -X POST localhost:8000/v1/ingest/demo/prometheus -H "Authorization: Bearer $AM_INGEST_TOKEN" \
  -H "Content-Type: application/json" -d '{"version":"4","status":"firing","alerts":[{"status":"firing",
  "labels":{"alertname":"FinanceExportFailed","severity":"critical","job":"finance-export"},
  "annotations":{"summary":"Nightly finance export failed"}}]}'
curl -H "X-API-Key: $OPS_SENTINEL_API_KEY" localhost:8000/v1/workspaces/demo/incidents
curl -H "X-API-Key: $OPS_SENTINEL_API_KEY" localhost:8000/v1/workspaces/demo/incidents/<id>
```

What you should observe:

1. **New alert** → one incident (`new_incident: true`) → worker routes it → Slack message (and/or webhook).
2. **Same alert again** → joins the open incident (`new_incident: false`, `signal_count` goes up), messages update.
3. **Alert resolved** → incident `resolved`, messages updated to "Resolved: ...".
4. The timeline shows every step with its actor: `opened`, `routed`, `notified`, `signal`, `source_resolved`,
   `status.resolved`. All of it is in `OPS_SENTINEL.INCIDENT_EVENTS`, queryable with SQL.
5. **With models configured (3b):** the incident moves `open → triaging → investigating → open`, the timeline gains
   `agent.started`, `agent.tool` (each SQL query and lookup) and `analysis`, and the channel messages are updated
   with impact, root cause, evidence and a suggested pre-approved action. Cost and tokens per run are in
   `OPS_SENTINEL.AGENT_RUNS`.

Useful SQL while it runs:

```sql
SELECT AGENT_NAME, STATUS, MODEL, INPUT_TOKENS, OUTPUT_TOKENS, COST_USD FROM OPS_SENTINEL.AGENT_RUNS ORDER BY STARTED_AT DESC;
SELECT STEP_NO, KIND, TOOL_NAME, RESULT_SUMMARY FROM OPS_SENTINEL.AGENT_STEPS WHERE RUN_ID = '<run id>' ORDER BY STEP_NO;
```

## Stop and reset

```
docker compose down                       # stop api and worker; data stays in Exasol
python engine/scripts/bootstrap_sentinel_roles.py --reset-passwords --env-file my-sentinel/.env   # rotate users
```

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Bind for 127.0.0.1:8000 failed: port is already allocated` | Another service already uses port 8000. Change the host port in `compose.yaml`, e.g. `127.0.0.1:8100:8000` |
| `insufficient privileges for creating schema` | Run step 2; the platform schema must exist and belong to `sentinel_owner` |
| api exits with `invalid workspace configuration` | Run `sentinel-validate`; it names the source, channel or secret at fault |
| `401 invalid or missing token` on ingest | A source with `token:` accepts only that token, not the API key |
| Worker `unhealthy` | It cannot reach Exasol; `docker compose logs worker` |
| `unable to prepare context: path ".../engine" not found` | `compose.yaml` was copied out of `deploy/compose/`; build the image first (step 4) and run `docker compose up -d` without `--build` |
| Notifications go to channels that are not in your spec, or jobs retry with errors about unknown channels | Another deployment uses the same Exasol and platform schema, so its workers take your jobs. Give each deployment its own `OPS_SENTINEL_PLATFORM_SCHEMA` (and run step 2 with `--platform-schema`) |
| api exits with `environment variable ANTHROPIC_API_KEY is not set` | The spec names a model key that `.env` lacks: add it, or remove `models` to run without AI analysis |
| Timeline shows `agent.finished` with `budget_exhausted` | The model was too slow or the task too big for the agent's budget; raise `budget` for that agent in the spec, or use a faster model |
| Analysis says it could not query Exasol | Set `EXA_USER` / `EXA_PASSWORD` to the read-only login, and list the schemas under `toolsets.exasol.schemas` |
