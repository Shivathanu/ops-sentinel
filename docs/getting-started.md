# Getting started

This guide takes a team from nothing to "an alert reaches the right channel, and its whole history is in Exasol"
in about 15 minutes. It was verified end to end on Windows with Docker Desktop and the Exasol starter kit.

```
alerting tool ──POST──► /v1/ingest/{workspace}/{source} ──► Exasol (signal, incident, job)
                                                               │
                                     worker claims the job ◄───┘──► routes ──► Slack / webhook / ...
```

## 0. Prerequisites

| Need | Check |
| --- | --- |
| Docker (Desktop or Engine) | `docker info` |
| An Exasol database | Local: the [Exasol starter kit](https://github.com/exasol) (`exakit start`, `exakit status` says `running`). Or any Exasol you can reach |
| Python 3.10+ (for the one-time bootstrap and `sentinel-validate`) | `python --version` |
| Optional: a Slack bot token with `chat:write` and the channel id | |

Install the engine tools once: `pip install ops-sentinel` (from this repository: `pip install -e engine`).

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

## 4. Start the engine

```
cd my-sentinel
docker compose up -d --build
docker compose ps            # api and worker both "healthy"
curl localhost:8000/v1/readyz
```

On start, the api applies pending migrations, installs the UDFs, records the spec version in Exasol and validates
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

Send a test alert (the demo project ships `send_alert.ps1`, which posts an Alertmanager payload), then:

```
curl -H "X-API-Key: $OPS_SENTINEL_API_KEY" localhost:8000/v1/workspaces/demo/incidents
curl -H "X-API-Key: $OPS_SENTINEL_API_KEY" localhost:8000/v1/workspaces/demo/incidents/<id>
```

What you should observe:

1. **New alert** → one incident (`new_incident: true`) → worker routes it → Slack message (and/or webhook).
2. **Same alert again** → joins the open incident (`new_incident: false`, `signal_count` goes up), messages update.
3. **Alert resolved** → incident `resolved`, messages updated to "Resolved: ...".
4. The timeline shows every step with its actor: `opened`, `routed`, `notified`, `signal`, `source_resolved`,
   `status.resolved`. All of it is in `OPS_SENTINEL.INCIDENT_EVENTS`, queryable with SQL.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `Bind for 127.0.0.1:8000 failed: port is already allocated` | Another engine (for example a v1 project) uses 8000. Change the host port in `compose.yaml`, e.g. `127.0.0.1:8100:8000` |
| `insufficient privileges for creating schema` | Run step 2; the platform schema must exist and belong to `sentinel_owner` |
| api exits with `invalid workspace configuration` | Run `sentinel-validate`; it names the source, channel or secret at fault |
| `401 invalid or missing token` on ingest | A source with `token:` accepts only that token, not the API key |
| Worker `unhealthy` | It cannot reach Exasol; `docker compose logs worker` |
