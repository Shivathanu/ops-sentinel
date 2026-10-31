# Deployment

How to run Ops Sentinel for real teams. For a first local run, follow the [run guide](getting-started.md) instead.

## What you deploy

| Component | Image / command | Instances | Needs |
| --- | --- | --- | --- |
| **api** | `ops-sentinel` image, default command (`uvicorn sentinel.main:app`) | 2+ behind a load balancer | Exasol, inbound HTTPS |
| **worker** | same image, command `sentinel-worker` | 1+ (scale on queue depth) | Exasol, outbound to Slack/webhooks and the model API |
| **Exasol** | your existing cluster or Exasol SaaS | | the platform schema `OPS_SENTINEL` |
| **runner** (optional) | `ops-sentinel-cli` on client machines | per environment | outbound HTTPS to the api only |

There is no other infrastructure: the job queue, the audit log and agent memory all live in Exasol.

```
           Internet / alert tools / Slack
                        │ HTTPS (TLS terminated at the proxy or ingress)
                ┌───────▼────────┐
                │  api  ×N       │──┐
                └────────────────┘  │   Exasol (OPS_SENTINEL schema)
                ┌────────────────┐  ├──► incidents · jobs · agent runs · audit
                │  worker ×M     │──┘
                └───────┬────────┘
                        └──► Slack / webhooks / Claude, OpenAI or a local model
```

## 1. Prepare Exasol (once)

Run as a DBA, from a machine that can reach the database:

```bash
pip install -e engine
EXASOL_DBA_USER=sys EXASOL_DBA_PASSWORD=... EXASOL_ADMIN_HOST=exa.internal \
  python engine/scripts/bootstrap_sentinel_roles.py --env-file deploy.env
```

It creates the `OPS_SENTINEL` schema owned by `sentinel_owner`, and the users `sentinel_engine` (runtime),
`sentinel_executor` (approved SQL fixes) and `mcp_readonly` (investigation), with generated passwords written to
`deploy.env`. Move those values into your secret store and delete the file.

Grant the executor DML only on the business tables it may repair (see [sql/roles.sql](../sql/roles.sql)).

**UDFs.** On an Exasol with the standard Python 3 script language container, migrations install the `FINGERPRINT`,
`VECTOR_COSINE` and `ROBUST_ZSCORE` UDFs. Without one, the engine logs `UDFs disabled` and runs the same code itself.

## 2. Build and publish the image

```bash
docker build -t registry.example.com/ops-sentinel:1.0.0 engine
docker push registry.example.com/ops-sentinel:1.0.0
```

The image runs as a non-root user (uid 10001), works with a read-only root filesystem (give it a writable `/tmp`),
and has a built-in health check. Pin versions; do not deploy `latest`.

## 3. Configuration and secrets

Everything is configured through environment variables plus one workspace spec per team.

| Variable | Value |
| --- | --- |
| `OPS_SENTINEL_SPEC_FILE` | path to `sentinel.yaml`, or a directory with one spec per team |
| `OPS_SENTINEL_API_KEY` | random 32+ byte key for the API and for sources without their own token |
| `EXASOL_ADMIN_HOST`, `EXASOL_ADMIN_PORT` | Exasol address |
| `EXASOL_ADMIN_USER`, `EXASOL_ADMIN_PASSWORD` | `sentinel_engine` |
| `EXASOL_MIGRATOR_USER`, `EXASOL_MIGRATOR_PASSWORD` | `sentinel_owner` (only where migrations run) |
| `EXASOL_EXECUTOR_USER`, `EXASOL_EXECUTOR_PASSWORD` | `sentinel_executor` |
| `EXA_USER`, `EXA_PASSWORD` | `mcp_readonly`, used by the agents' Exasol tools |
| `EXASOL_VALIDATE_CERTIFICATE` | `true` in production |
| `OPS_SENTINEL_AUTO_MIGRATE` | `true` for compose; `false` where a separate migration step runs |
| model, Slack and source secrets | whatever names your specs reference, e.g. `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `AM_INGEST_TOKEN` |

The full list with defaults is in [.env.example](../.env.example). Specs hold secret **references**
(`${env:NAME}` or `${file:/run/secrets/name}`), never values, so they can live in Git. Check every spec in CI:

```bash
sentinel-validate specs/          # offline; exit code 1 on any problem
```

## 4a. Docker Compose (single host)

[deploy/compose/compose.yaml](../deploy/compose/compose.yaml) runs api and worker with a read-only filesystem,
`no-new-privileges`, health checks and restart policies.

```bash
cd deploy/compose
cp ../../examples/sentinel.yaml sentinel.yaml      # one workspace; or mount a directory of specs
cp ../../.env.example .env                         # fill in from step 1 and your secret store
docker compose up -d
docker compose up -d --scale worker=3              # more workers when needed
```

Put a TLS-terminating reverse proxy (Caddy, nginx, Traefik) in front of the api. The compose file binds the api to
`127.0.0.1:8000` only, so it is never exposed without the proxy.

## 4b. Kubernetes

A Helm chart is planned. Until then, the same image runs with two Deployments. A minimal outline:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata: { name: sentinel-api }
spec:
  replicas: 2
  selector: { matchLabels: { app: sentinel-api } }
  template:
    metadata: { labels: { app: sentinel-api } }
    spec:
      securityContext: { runAsNonRoot: true, runAsUser: 10001 }
      containers:
        - name: api
          image: registry.example.com/ops-sentinel:1.0.0
          ports: [{ containerPort: 8000 }]
          envFrom: [{ secretRef: { name: sentinel-env } }]
          env: [{ name: OPS_SENTINEL_SPEC_FILE, value: /config/specs }, { name: OPS_SENTINEL_AUTO_MIGRATE, value: "false" }]
          volumeMounts: [{ name: specs, mountPath: /config/specs, readOnly: true }, { name: tmp, mountPath: /tmp }]
          readinessProbe: { httpGet: { path: /v1/readyz, port: 8000 }, periodSeconds: 10 }
          livenessProbe:  { httpGet: { path: /v1/healthz, port: 8000 }, periodSeconds: 15 }
          securityContext: { readOnlyRootFilesystem: true, allowPrivilegeEscalation: false }
      volumes:
        - { name: specs, configMap: { name: sentinel-specs } }
        - { name: tmp, emptyDir: {} }
```

The worker Deployment is the same with `command: ["sentinel-worker"]`, no port, and
`livenessProbe: { exec: { command: ["sentinel-worker", "--check"] } }`. Run migrations as a Job before each
rollout (`command: ["sentinel-migrate"]`, with the `EXASOL_MIGRATOR_*` secret). Expose the api through an Ingress
with TLS.

## 5. Connect the outside world

- **Alert sources.** `sentinel-validate` prints each source's ingest URL. Give each source its own `token:` in the
  spec so a leaked sender credential can post only to that source.
- **Slack.** Create a Slack app with `chat:write`, invite it to each channel, and set its Interactivity Request URL
  to `https://<your-host>/slack/interactions` for approvals. List approvers in `SLACK_APPROVER_IDS`.
- **Models.** Allow outbound HTTPS from the workers to the model API, or point `base_url` at a model server inside
  your network (`provider: ollama` or `openai_compatible`).

## 6. Operate

| Task | How |
| --- | --- |
| Health | api: `GET /v1/healthz` (process), `GET /v1/readyz` (config valid and Exasol reachable). Worker: `sentinel-worker --check` |
| Logs | Plain-text logs to stdout; logger names start with `sentinel.` |
| Jobs | `SELECT STATUS, COUNT(*) FROM OPS_SENTINEL.JOBS GROUP BY STATUS`; `dead` jobs carry `LAST_ERROR` |
| AI spend | `SELECT AGENT_NAME, SUM(COST_USD) FROM OPS_SENTINEL.AGENT_RUNS GROUP BY 1`; per-agent budgets are set in the spec |
| Config history | every applied spec version is in `OPS_SENTINEL.CONFIG_VERSIONS` |
| Audit | `OPS_SENTINEL.AUDIT_EVENTS` is append-only for the runtime user |
| Upgrades | Deploy the new image; the api (or the migration Job) applies new migrations. Migrations are append-only and checksummed, and an edited migration stops the upgrade instead of drifting |
| Scaling | api: stateless, scale freely. Workers: add replicas; jobs are leased, so a crashed worker's jobs are retried by others |
| Backups | Back up the `OPS_SENTINEL` schema with your normal Exasol backups |
| Credential rotation | `bootstrap_sentinel_roles.py --reset-passwords --env-file ...`, then roll the new secrets out |

## Production checklist

- [ ] `EXASOL_VALIDATE_CERTIFICATE=true` and TLS in front of the api
- [ ] Runtime uses `sentinel_engine`, never `sys`; migrations use `sentinel_owner`
- [ ] Every source has its own `token:`; `OPS_SENTINEL_API_KEY` is long and random
- [ ] `SLACK_APPROVER_IDS` lists real approvers; approve channels are `interactive: true`
- [ ] Agent budgets reviewed per team (`agents: [{name: investigator, budget: {...}}]`)
- [ ] `sentinel-validate` runs in CI on the specs repository
- [ ] Image version pinned; migrations run once per rollout
