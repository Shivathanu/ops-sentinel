# Ops Sentinel v2: AI SRE platform design

Status: accepted, phases 0 and 1 implemented · Date: 2026-10-31 · Supersedes the single-engine design in
`docs/architecture.md` (the v1 `/api` endpoints keep working alongside).

**Exasol is the system of record.** Incidents, signals, the job queue, the audit log, agent runs, knowledge chunks,
embeddings and memory all live in Exasol. Python 3 UDFs bring the intelligence to the data (fingerprinting, vector
similarity, anomaly scores), so retrieval and analytics run in parallel inside the cluster instead of shipping rows
out. See §4a.

## 1. Goal

A team installs Ops Sentinel into its environment, connects its alert sources, data sources and chat channels with
a short config, and gets an AI SRE that:

1. **Triages**: groups noisy signals into incidents, ranks them, and explains business impact.
2. **Investigates**: an agent calls read-only tools (logs, metrics, databases, Kubernetes, CI, docs) and finds the root cause.
3. **Remediates**: proposes a fix from a catalog of pre-approved actions and runs it only within policy, after a
   human approves where policy requires it.
4. **Resolves**: verifies recovery, closes the incident, writes the post-incident summary and remembers the
   incident for next time.

Design constraints:

- **Multi-user and multi-team** from day one: organisations, workspaces, roles, per-team routing.
- **AI-native primitives** as first-class, declarative objects: agents, subagents, tools, routines, runbooks,
  knowledge (RAG), memory and evals.
- **Pluggable everywhere** without forking: sources, toolsets, channels, actions, model providers, stores.
- **Safe by construction**: read-only by default, writes go through policy and approval, every step is audited.
- **Deployment-ready**: `docker compose` for a laptop, a Helm chart for Kubernetes, the same images in both.

References studied: HolmesGPT (toolsets, YAML/HTTP/MCP connectors, read-only default, scheduled health checks,
OTel audit) and Robusta (alert grouping into incidents, any-webhook sources, many destinations, escalate only when
needed, SaaS/VPC/self-host). This design borrows those ideas and adds a remediation path with an approval gate,
plus edge runners, which neither emphasises.

## 2. What changes from v1, and why

| v1 | Problem | v2 |
| --- | --- | --- |
| One engine process doing watching, triage, Slack and remediation | Doesn't scale and can't be split | `api`, `worker`, `scheduler` services over a shared queue |
| Exasol tables mixed with demo data, `SELECT MAX(ID)` after insert, manual DDL | Races under concurrency; no migrations; setup friction | A separate **platform schema** with versioned migrations applied on start, ULID keys, a transactional outbox, UDFs installed from tested code. Exasol is also a **toolset** the agent investigates |
| One LLM call that returns JSON | No tool use, no follow-up questions | An agent loop with tool calling, subagents and budgets |
| `SLACK_CHANNEL_ID`, one channel | No routing | **Channels** + **routes** per workspace, severity and tags |
| Sources hard-coded in the TS CLI | Each new source needs code | **Source connectors** with payload presets, plus generic webhook, exec and MCP |
| Fix = one command per source | Can't express risk, scope or auto-approval | **Action catalog** + **policy** |
| `business-context.md` capped at 8000 characters | Doesn't scale; no retrieval | **Knowledge** pipeline (ingest → chunk → embed → retrieve) |
| Single API key | Not multi-user | Orgs, workspaces, users, roles, and per-runner tokens |

Kept from v1: the edge-runner security model (the engine never holds commands for client machines; it names an
action id and the runner executes only what its local config defines), the guardrail SQL validator, the atomic
approval claim, and the append-only audit trail.

## 3. Core concepts

The vocabulary, from signal to resolution:

```
Source ──► Signal ──► (correlate) ──► Incident ──► Routine ──► Agent ──► Tools / Subagents
                                          │                      │
                                          │                      └─► Finding, Proposed Action
                                          ▼
                                 Route ──► Channel (notify / approve)
                                          │
                                 Policy ──► Action ──► Runner (engine or edge) ──► Verification ──► Resolution
                                                                                          │
                                                                          Memory + Knowledge (RAG) ◄┘
```

| Concept | What it is |
| --- | --- |
| **Source** | Where signals come from: Alertmanager, Grafana, Datadog, PagerDuty, CloudWatch, GitHub Actions, generic webhook, log file, HTTP probe, command, DB query, MCP server. |
| **Signal** | One normalized event (CloudEvents-shaped): `source`, `fingerprint`, `severity`, `labels`, `summary`, `payload`, `observed_at`. |
| **Incident** | A correlated group of signals with a lifecycle. Duplicates attach to an open incident instead of creating a new one. |
| **Tool** | A function the model may call: name, JSON-schema args, description, `capability` (`read` / `write`), `risk`, timeout, output limits. |
| **Toolset** | A named bundle of tools plus their connection config (for example `prometheus`, `loki`, `kubernetes`, `exasol`, `postgres`, `github`, `http:<name>`, `mcp:<name>`). |
| **Agent** | A declarative spec: instructions, model role, allowed toolsets, subagents, output schema, step/token/time budget. |
| **Subagent** | An agent another agent can call as a tool (`delegate_to_logs_analyst`). It runs in its own context window and returns a compact result, which keeps the parent's context small. |
| **Routine** | A declarative workflow that a trigger starts: on a signal, on an incident change, on a cron schedule, or manually. Its steps run agents, tools, actions and notifications. Covers Robusta-style playbooks and Holmes-style scheduled health checks. |
| **Runbook** | Markdown guidance matched to incidents by labels or semantic similarity and injected into the agent's context. Stored as knowledge. |
| **Knowledge** | Documents (runbooks, architecture docs, business context, past post-mortems, code, tickets) ingested into a retrievable index. Agents reach it through the `knowledge.search` tool. |
| **Memory** | What the platform learned: resolved incidents with root cause and fix, operator feedback, per-service facts. Agents reach it through `memory.similar_incidents`. |
| **Action** | A pre-registered remediation: id, parameters schema, target (engine-executed SQL/HTTP, or `runner:<name>`), risk, verification check. |
| **Policy** | Rules for when an action may run: auto, needs approval (and by whom), or forbidden, per workspace/environment/risk/time window. |
| **Runner** | An edge process (today's TS CLI) inside the client network. It holds local action commands, executes approved actions and can host sources and toolsets that must not leave the network. |
| **Channel** | Where humans are reached: Slack, Teams, email, PagerDuty, Opsgenie, generic webhook, web UI. Interactive channels can carry approvals. |
| **Route** | Matches incidents (workspace, severity, labels, service) to channels for notify, approve and escalate. |

## 4. Architecture

```
                         ┌───────────────────────────── control plane ─────────────────────────────┐
 Alert tools ─webhook──► │ api (FastAPI)                                                           │
 (AM, Grafana, PD, ...)  │  /v1/ingest/{source}  /v1/incidents  /v1/approvals  /v1/runners  /mcp   │
                         │  authn (OIDC / API tokens) · RBAC · tenancy                             │
 Slack/Teams ◄─────────► │  channel callbacks (interactive approvals)                              │
                         └───────────────┬───────────────────────────────▲─────────────────────────┘
                                         │ enqueue                       │ results / events
                                ┌────────▼────────┐             ┌────────┴────────┐
                                │ queue (Exasol   │◄────────────│  scheduler      │ cron routines,
                                │ JOBS, leases)   │             │                 │ timeouts, escalations
                                └────────┬────────┘             └─────────────────┘
                                         │
                         ┌───────────────▼──────────────────────────────────────────┐
                         │ worker (agent runtime), scales horizontally              │
                         │  correlator → routine engine → agent loop                │
                         │  tool registry · context manager · LLM gateway           │
                         │  policy engine · action dispatcher · channel dispatcher  │
                         └──────┬─────────────┬──────────────┬─────────────┬────────┘
                                │             │              │             │
                        ┌───────▼──┐  ┌───────▼─────┐ ┌──────▼─────┐ ┌─────▼──────────┐
                        │ Exasol   │  │ LLM provider│ │ Toolsets   │ │ Knowledge      │
                        │ platform │  │ (via gateway│ │ (Prom, Loki│ │ ingest worker  │
                        │ schema + │  │ OpenAI/Anth/│ │ K8s, Exasol│ │ chunk · embed  │
                        │ Python   │  │ Ollama/...) │ │ HTTP, MCP) │ │ → Exasol chunks│
                        │ UDFs     │  └─────────────┘ └────────────┘ └────────────────┘
                        └──────────┘
          ─────────────────────────────── client network ───────────────────────────────
                         ┌──────────────────────────────────────────┐
                         │ runner (sentinel CLI, `sentinel runner`) │  outbound-only HTTPS/long-poll
                         │  local sources · local toolsets          │  to the api; no inbound ports
                         │  local action commands (never uploaded)  │
                         └──────────────────────────────────────────┘
```

### Services

| Service | Responsibility | Scaling |
| --- | --- | --- |
| `api` | Ingest, REST, auth, channel callbacks, runner gateway, MCP endpoint (Sentinel as an MCP server for IDEs and other agents) | Stateless, N replicas |
| `worker` | Runs jobs: correlate, run routines and agents, dispatch actions and notifications | Stateless, N replicas, one job per lease |
| `scheduler` | Cron routines, approval timeouts, escalations, knowledge re-sync | One active (leader lease in PG) |
| `ingest` | Knowledge ingestion (same image as `worker`, separate queue) | N replicas |
| `runner` | Edge execution and local sources (TS CLI) | One or more per environment |
| `web` | Incident timeline, approvals, config, evals. Replaces Streamlit in phase 3 | Stateless |

All Python services ship in **one image** with different entrypoints (`sentinel-api`, `sentinel-worker`, ...), so
compose and Helm stay simple.

### Why the queue lives in Exasol

Exasol is already required, so a broker would be one more thing to run. Event volume is low (incidents, agent steps,
notifications), well within what a table handles. `store/queue.py` implements at-least-once delivery with leases:
a claim is a SELECT plus an UPDATE guarded by the same readiness condition, in one serializable transaction. Two
workers racing for one job cause an Exasol transaction collision; one is rolled back and retries against the new
state. Workers send heartbeats to extend leases, expired leases are reaped, and jobs that fail retry with backoff
until they are moved to a dead-letter state. Every handler is idempotent. `JobQueue` is the only code that knows the
queue is a table, so NATS or SQS can replace it later.

## 4a. Exasol as the system of record

### Schemas and users

| Schema | Holds | Written by |
| --- | --- | --- |
| `OPS_SENTINEL` (platform) | incidents, signals, timeline, jobs, audit, agent runs, knowledge, memory, proposals, runners | the engine, through `store/` only |
| `SENTINEL` (v1 and demo data) | pipeline logs, query metrics, lineage, business tables | the teams' pipelines; read by the agent |

| User | Role | Privileges |
| --- | --- | --- |
| `sentinel_owner` | Owns the platform schema; runs migrations and installs UDFs | `CREATE TABLE/VIEW/SCRIPT`, schema owner |
| `sentinel_engine` | Runtime api and worker | DML on platform tables, **SELECT and INSERT only on `AUDIT_EVENTS`**, `EXECUTE` on UDFs. The migrator reconciles these grants on every run |
| `sentinel_executor` | Runs approved, guardrail-checked remediation SQL | DML on named business tables only |
| `mcp_readonly` | Read-only investigation over MCP | `SELECT ANY TABLE` |

`scripts/bootstrap_sentinel_roles.py` creates all four users with generated passwords in one DBA run. On a laptop,
leaving `EXASOL_MIGRATOR_USER` empty makes the runtime user migrate too.

### Migrations

Numbered SQL files in `store/migrations/` are applied in order on api start (`OPS_SENTINEL_AUTO_MIGRATE`) or with
`sentinel-migrate`. Each is recorded in `SCHEMA_MIGRATIONS` with a checksum, and editing an applied migration is an
error. A migration and its bookkeeping row commit together, so replicas starting at the same time are safe.

### Concurrency model

- Exasol transactions are serializable and resolve write conflicts optimistically (SQLSTATE 40001).
  `store/exasol.run_in_transaction` retries the whole unit of work with jittered backoff, and every unit is
  repeat-safe.
- Exasol has no `UNIQUE` constraint, so uniqueness beyond the primary key (idempotency keys, one open incident per
  fingerprint) comes from single-transaction `INSERT ... WHERE NOT EXISTS` and check-then-write under
  serializability.
- Keys are application-generated ULIDs: sortable, collision-free across replicas, and with no `MAX(ID)` race.
- **Transactional outbox**: opening an incident and enqueuing its `incident.opened` job happen in one commit.

### UDFs (in-database intelligence)

UDF bodies are the source of `ops_sentinel/udf_lib/*`, installed verbatim with a small `run(ctx)` adapter, so the
code in the database is the code the unit tests cover.

| UDF | Kind | Use |
| --- | --- | --- |
| `FINGERPRINT(text)` | SCALAR | Normalizes volatile tokens (timestamps, ids, IPs, numbers) and hashes. Correlates historic `PIPELINE_LOGS` errors in bulk with the same function ingest uses |
| `VECTOR_COSINE(query, candidate)` | SCALAR | Cosine similarity of base64 float32 embeddings. Knowledge and memory retrieval are plain SQL: `ORDER BY VECTOR_COSINE(:q, EMBEDDING) DESC LIMIT k`, parallel across nodes, with the query vector decoded once per VM |
| `ROBUST_ZSCORE(series, ts, value)` | SET | Median/MAD anomaly scores per series (`GROUP BY` the series). Flags duration, row-count and cost outliers that become signals or agent evidence |

Planned UDFs: `CHUNK_TEXT` (heading-aware chunking in the database for bulk re-indexing), `EMBED` (calls the
embedding endpoint from inside Exasol where egress is allowed), and `LINEAGE_BLAST_RADIUS` (recursive downstream
impact over `DATA_LINEAGE`, for triage).

### Limits and how they are handled

- **Many small writes** are not Exasol's sweet spot. Writes are batched per unit of work, the queue is low-volume
  by design, and agent steps are written once per step, not per token.
- **No row-level security**: tenancy is enforced in `store/`, where every query carries `WORKSPACE_ID` and
  routes reach tables only through repositories. Per-workspace views can be added for BI access.
- **Vector search** is a full scan scored in parallel. That's fine up to millions of chunks per workspace. Beyond
  that, the `VectorStore` interface admits an external ANN index, with Exasol keeping the documents.

## 5. The agent runtime (AI-native core)

### 5.1 Agent loop

```
build context ─► LLM call ─► tool calls? ──yes──► policy check ─► execute (parallel where safe)
      ▲                         │                                       │
      │                         no                                transform output
      │                         ▼                                 (truncate / jq / summarize)
      │                 structured final answer                         │
      └──────────── append tool results, check budget ◄─────────────────┘
```

- **Budgets** per agent: `max_steps`, `max_tokens`, `max_wall_seconds`, `max_cost_usd`. When a budget is exhausted,
  the agent produces its best partial answer instead of failing.
- **Structured output**: each agent declares a JSON schema for its final answer (for example `TriageResult`,
  `Investigation`, `RemediationPlan`), validated with Pydantic. On a mismatch the agent gets one repair round.
- **Tool policy check before execution**: `write` tools are never executed by the loop. They become a
  `ProposedAction` that goes to the policy engine, so a prompt-injected log line cannot trigger a write.
- **Parallel tool calls** for read tools, with a concurrency limit per toolset.
- **Streaming events** (step started, tool called, tool result, answer) go to the incident timeline, the web UI
  and OTel.

### 5.2 Subagents

Subagents are agents exposed to a parent agent as tools named `delegate_<name>(task: str, hints: dict)`. Each runs
with its own context and toolsets and returns a compact `SubagentReport` (findings, evidence refs, confidence).
Default set:

| Agent | Role | Toolsets |
| --- | --- | --- |
| `triage` | Classify, deduplicate against memory, rate severity and business impact; decide whether to investigate | `memory`, `knowledge`, `incident` |
| `investigator` (orchestrator) | Form hypotheses, delegate, converge on a root cause | `knowledge`, `memory`, plus delegates |
| `logs_analyst` | Query and summarize logs | `loki`, `elasticsearch`, `cloudwatch_logs`, `file` |
| `metrics_analyst` | Query metrics, find anomalies and correlations | `prometheus`, `datadog` |
| `data_analyst` | Investigate pipelines, lineage and query cost in warehouses | `exasol`, `postgres`, `snowflake` |
| `change_analyst` | What changed: deploys, commits, config, feature flags | `github`, `argocd`, `kubernetes` |
| `remediator` | Choose an action from the catalog, fill its parameters, state risk and verification | `actions` (catalog read), `knowledge` |
| `scribe` | Write channel updates and the post-incident summary | `incident` |

Teams can override any of these or add their own in YAML (see §8).

### 5.3 Tools and toolsets

Ways to add a tool, from least to most code:

1. **HTTP toolset (YAML only)**: declare endpoints, methods, parameters and auth. Host, path and method allow-lists
   are enforced.
2. **MCP toolset**: point at an MCP server (stdio or remote, OAuth supported). Its tools are imported, read-only
   unless explicitly marked otherwise.
3. **Command toolset**: a script on a runner, with JSON in and JSON out. It runs at the edge, so data that cannot
   leave the network stays there.
4. **Python toolset**: a class implementing `Toolset` and registered through an entry point
   (`ops_sentinel.toolsets`).

Built-in toolsets ship first for what this project already touches: `exasol` (wrapping the existing MCP read
path), `postgres`, `http`, `prometheus`, `loki`, `kubernetes`, `github`, `slack_history`, plus internal toolsets
`knowledge`, `memory`, `incident` and `actions`.

**Output transformers** keep context small: `truncate(max_chars)`, `jq(expr)`, `tail(lines)`, `summarize(model=fast)`
and `table(max_rows)`, configurable per tool. Raw outputs are stored as **evidence** blobs and referenced by id, so
the model sees a summary and humans can open the full output.

### 5.4 Context engineering and RAG

The **context manager** assembles each LLM call from ranked slots, each with a token budget:

| Slot | Content | Source |
| --- | --- | --- |
| system | Agent instructions, guardrails, output schema | agent spec |
| org context | Business context, service catalog entry for the affected service | knowledge (pinned docs) |
| runbooks | Top-k runbooks matched by labels, then semantic similarity | knowledge |
| memory | Top-k similar resolved incidents with root cause and fix | memory |
| incident | Signals, timeline, previous findings | incident store |
| working | Recent tool calls and results; older ones are compacted into summaries | agent loop |

**Knowledge pipeline:**

```
connector (git repo, folder, Confluence, Notion, URL, Slack channel, past incidents)
   → loader → normalizer (markdown) → chunker (heading-aware, ~800 tokens, overlap)
   → metadata (workspace, service, doc type, labels, updated_at, ACL)
   → embedder (model role `embedding`) → KNOWLEDGE_CHUNKS in Exasol (base64 float32 embeddings) + keyword match
retrieval = hybrid (keyword + VECTOR_COSINE UDF, reciprocal-rank fusion) → optional re-rank → ACL filter by workspace
```

- Exposed to agents as tools (`knowledge.search(query, filters)`, `knowledge.get(doc_id)`), so agents retrieve on
  demand. Pinned docs are always injected.
- Incremental re-sync by content hash, scheduled by the `scheduler`.
- `VectorStore`, `Embedder` and `Retriever` are interfaces. Exasol with the `VECTOR_COSINE` UDF is the default, and an ANN index (Qdrant, OpenSearch) can be
  plugged in later.

### 5.5 Memory

On resolution, the `scribe` writes an `IncidentMemory` record: symptom fingerprint, services, root cause, action
taken, outcome, operator feedback (helpful or not, plus corrections) and an embedding. `triage` and `investigator`
query it first. A repeat incident can then become "same as #412, fixed by `rerun-export`, approve?" within seconds.

### 5.6 LLM gateway

A thin `ModelProvider` interface with **model roles** rather than model names in agent specs:

```yaml
models:
  smart:     { provider: anthropic, model: claude-sonnet-5-5 }   # investigator, remediator
  fast:      { provider: anthropic, model: claude-haiku-5-5 }    # triage, summarizer, transformers
  embedding: { provider: openai,    model: text-embedding-3-small }
  # local / air-gapped alternative
  # smart: { provider: ollama, model: llama3.1:8b, base_url: http://ollama:11434/v1 }
```

The gateway handles retries, timeouts, a fallback provider, token and cost accounting per incident and workspace,
prompt caching where the provider supports it, and redaction of secrets and PII before a prompt leaves the network.
The adapters (OpenAI-compatible, Anthropic, Ollama) are small. LiteLLM is an optional backend behind the same
interface, not a hard dependency.

## 6. Remediation and safety

```
ProposedAction ─► Policy engine ─► deny ────────────────────────────► recorded, explained
                        │
                        ├─► auto (low risk, allowed env, in window) ─► dispatch
                        └─► require approval ─► route to approve channel ─► approver in role? ─► dispatch
                                                                                    │
                                    dispatch ─► engine executor (guarded SQL/HTTP) or runner:<name>
                                                                                    │
                                                         verification check ─► resolved | failed → escalate
```

- **Action catalog** entries are declared by the team, never generated by the model. The model only picks an action
  id and fills parameters, which are validated against the action's JSON schema.
- **SQL actions** keep the v1 guardrail (`guardrail.validate_fix_sql`) and the least-privilege executor user.
- **Runner actions** keep the v1 property: the command exists only in the runner's local config, and the engine
  sends `{action_id, params}`. The runner checks that the id exists locally and the params match its schema.
- **Approval** is atomic (claim by `UPDATE ... WHERE status = 'awaiting_approval'`), bound to a role, and
  optionally requires two people for `risk: high`.
- **Verification**: every action declares how to confirm success (re-run the source probe, a PromQL query, an HTTP
  check). An incident resolves only on verification.
- **Audit**: every LLM call, tool call, decision and action is an append-only `audit_events` row and an OTel span.
- **Prompt-injection posture**: tool outputs are wrapped as data, `write` tools cannot run from the loop, actions
  are allow-listed, and parameters are schema-validated.

## 7. Multi-tenancy, identity and secrets

- **Hierarchy**: `organization → workspace (team) → environment (prod / staging)`. Every row carries
  `workspace_id`, enforced in the `store/` repositories (Exasol has no row-level security), with per-workspace views for BI access.
- **Identity**: OIDC SSO for humans (Google, Okta, Azure AD) and API tokens for automation. Chat identities (Slack
  user id) are linked to platform users, so "who approved" is a real user with a role.
- **Roles**: `owner`, `admin`, `responder` (approve and act), `viewer`. Policies can name roles or groups.
- **Runners** authenticate with per-runner tokens scoped to one workspace and environment. They connect outbound
  only (HTTPS long-poll, with WebSocket later), so no inbound firewall rules are needed.
- **Secrets** are never stored in config. Config holds references (`${env:SLACK_BOT_TOKEN}`,
  `${k8s:secret/name#key}`, `${vault:path#key}`), resolved by a `SecretProvider`.

## 8. Configuration: minimal to start, everything overridable

**Layering:** built-in defaults → org config → workspace config (`sentinel.yaml` or the API/UI) → environment
overrides. Every applied spec is versioned in Exasol (`CONFIG_VERSIONS`), editable through the API, and can be applied GitOps-style with
`sentinel apply -f`.

A team's whole starting config:

```yaml
# sentinel.yaml (workspace: data-platform)
apiVersion: sentinel/v1
workspace: data-platform

models:
  smart: { provider: anthropic, model: claude-sonnet-5-5, api_key: ${env:ANTHROPIC_API_KEY} }
  fast:  { provider: anthropic, model: claude-haiku-5-5,  api_key: ${env:ANTHROPIC_API_KEY} }

sources:
  - { name: prometheus, type: alertmanager }          # gives you a URL + token to paste into Alertmanager
  - { name: nightly-etl, type: logfile, runner: etl-box, path: /var/log/etl.log, match: "ERROR|FATAL" }

toolsets:
  exasol:     { dsn: ${env:EXA_DSN}, user: mcp_readonly, password: ${env:EXA_PASSWORD} }
  prometheus: { url: http://prometheus:9090 }

knowledge:
  - { name: runbooks, type: git, repo: https://github.com/acme/runbooks, path: /data-platform }
  - { name: business, type: file, path: ./business-context.md, pinned: true }

channels:
  oncall:  { type: slack, channel: "#data-oncall", interactive: true }
  finance: { type: teams, webhook: ${env:TEAMS_FINANCE_URL} }

routes:
  - match: { labels: { domain: finance } }
    notify: [finance]
    approve: oncall
  - default: { notify: [oncall], approve: oncall }

actions:
  - id: rerun-export
    runner: etl-box
    risk: low
    verify: { source: nightly-etl, healthy_for: 5m }

policies:
  - { match: { risk: low, environment: staging }, decision: auto }
  - { match: { risk: [low, medium] }, decision: approve, approvers: [role:responder] }
  - { match: { risk: high }, decision: approve, approvers: [role:admin], quorum: 2 }
```

Everything not written down comes from defaults: the default agents, the default routine
`on_incident → triage → investigate → propose → notify`, built-in transformers and budgets.

**Custom agent and routine (optional):**

```yaml
agents:
  - name: dbt_analyst
    extends: data_analyst
    instructions: |
      Check dbt run results and source freshness before blaming the warehouse.
    toolsets: [exasol, http:dbt_cloud]

routines:
  - name: morning-pipeline-check
    trigger: { cron: "0 6 * * 1-5", timezone: Europe/Berlin }
    steps:
      - agent: data_analyst
        task: "Did every nightly pipeline finish and is today's data fresh?"
      - notify: { channel: oncall, when: findings }
```

The CLI makes this discoverable: `sentinel connect slack`, `sentinel add source alertmanager`,
`sentinel test channel oncall`, `sentinel simulate --source nightly-etl`, `sentinel routes explain <incident>`,
`sentinel validate`. A published JSON Schema gives editor autocomplete.

## 9. Data model (Exasol, platform schema)

| Table | Key columns |
| --- | --- |
| `organizations`, `workspaces`, `environments` | tenancy |
| `users`, `memberships`, `identities`, `api_tokens`, `runners` | identity and access |
| `config_versions` | workspace, version, yaml, applied_by, applied_at |
| `signals` | workspace, source, fingerprint, severity, labels (jsonb), payload (jsonb), observed_at |
| `incidents` | workspace, environment, status, severity, title, service, fingerprint, opened_at, resolved_at |
| `incident_signals` | incident ↔ signal |
| `agent_runs`, `agent_steps` | agent, parent_run (subagent tree), model, tokens, cost, status / step type, tool, args, result_ref |
| `evidence` | run, tool, content (or object-store ref), summary |
| `proposed_actions`, `approvals`, `action_executions` | action id, params, risk, policy decision / approver, decision / runner, outcome, output |
| `notifications` | incident, channel, external message ref |
| `KNOWLEDGE_DOCUMENTS`, `KNOWLEDGE_CHUNKS` | `EMBEDDING` as base64 float32 VARCHAR, scored by the `VECTOR_COSINE` UDF |
| `incident_memories` | incident, summary, root_cause, action, outcome, feedback, embedding |
| `jobs` | queue, payload, run_after, lease_until, attempts, idempotency_key |
| `audit_events` | append-only; the actor, the subject and what happened |

Incident states: `open → triaging → investigating → awaiting_approval → remediating → verifying → resolved`, plus
side exits `suppressed`, `rejected`, `failed → escalated`.

## 10. Extensibility contracts (Python)

```python
class Source(Protocol):            # push (webhook) or pull (poll) → normalized Signals
    kind: str
    def parse(self, request: IngestRequest) -> list[Signal]: ...           # push
    async def poll(self, ctx: SourceContext) -> list[Signal]: ...          # pull (optional)

class Toolset(Protocol):
    name: str
    def tools(self) -> list[ToolSpec]: ...
    async def call(self, tool: str, args: dict, ctx: ToolContext) -> ToolResult: ...
    async def health(self) -> HealthStatus: ...

class Channel(Protocol):
    kind: str
    interactive: bool
    async def send(self, msg: IncidentMessage) -> MessageRef: ...
    async def update(self, ref: MessageRef, msg: IncidentMessage) -> None: ...
    def parse_callback(self, request: CallbackRequest) -> ApprovalDecision | None: ...

class ActionExecutor(Protocol):    # engine-side executors; runners implement the same contract in TS
    kind: str                      # "sql" | "http" | "runner"
    async def execute(self, action: ActionSpec, params: dict, ctx: ActionContext) -> ExecutionResult: ...

class ModelProvider(Protocol):
    async def complete(self, req: CompletionRequest) -> CompletionResponse: ...   # tools + structured output
    async def embed(self, texts: list[str]) -> list[list[float]]: ...

class KnowledgeConnector(Protocol):
    async def list_changed(self, since: datetime | None) -> AsyncIterator[RawDocument]: ...
```

Plugins register through Python entry points (`ops_sentinel.sources`, `.toolsets`, `.channels`, `.actions`,
`.models`, `.knowledge`), so a team ships its own plugin as a pip package without forking.

## 11. Proposed code layout

```
src/ops_sentinel/
  api/            app.py, routes/ (ingest, incidents, approvals, runners, config, mcp), auth.py, deps.py
  core/           models.py (Pydantic domain), states.py, config/ (schema, loader, secrets), tenancy.py
  store/          exasol.py (transactions, collision retry), migrate.py + migrations/*.sql, udfs.py, queue.py, incidents.py
  ingest/         sources/ (alertmanager, grafana, pagerduty, datadog, generic_webhook, ...), correlate.py
  agents/         runtime.py (loop), context.py, budget.py, specs/ (default YAML agents), subagents.py
  tools/          registry.py, transformers.py, toolsets/ (exasol, postgres, http, mcp, prometheus, k8s, ...)
  knowledge/      connectors/, chunking.py, embed.py, retrieve.py, memory.py
  llm/            gateway.py, providers/ (openai_compat, anthropic, ollama), redaction.py
  routines/       engine.py, triggers.py, default.yaml
  remediation/    catalog.py, policy.py, executors/ (sql, http, runner), guardrail.py, verify.py
  channels/       slack.py, teams.py, email.py, pagerduty.py, webhook.py, router.py
  observability/  otel.py, audit.py, metrics.py
  workers/        worker.py, scheduler.py
cli/              TypeScript: `sentinel` CLI + `sentinel runner` (edge agent)
deploy/           compose/ (quickstart, prod), helm/ops-sentinel/
evals/            incident replay fixtures, scorers, `sentinel eval` harness
```

### Where v1 modules go

| v1 | v2 |
| --- | --- |
| `watcher.py` (Exasol polling) | `ingest/sources/exasol_pipeline.py` (a pull source) |
| `mcp_client.py` | `tools/toolsets/exasol.py` (wraps the same MCP server) |
| `triage_agent.py` | `agents/specs/triage.yaml` + `llm/providers/*` |
| `slack_notifier.py` | `channels/slack.py` |
| `remediation.py`, `guardrail.py` | `remediation/executors/sql.py`, `remediation/guardrail.py` |
| `agent_events.py`, `agent_actions.py` | `api/routes/runners.py`, `remediation/executors/runner.py` |
| `scoring.py` | `observability/metrics.py` (trust score from `action_executions` and feedback) |
| `db.py` (Exasol pools) | reused by `store/exasol.py` for the platform schema; Exasol is also a toolset |
| `dashboard/app.py` | kept until the `web` service lands |

## 12. Deployment

**Quickstart (laptop or single VM):**

```
npx ops-sentinel init        # asks: model provider, Slack? → writes sentinel.yaml, .env, compose.yaml
sentinel up                  # api (migrates Exasol, installs UDFs) + worker [+ local Exasol] [+ ollama]
sentinel connect slack       # opens the Slack app manifest, stores the token
sentinel simulate            # fires a demo incident end to end
```

**Kubernetes:** `helm install sentinel oci://.../ops-sentinel -f values.yaml`

- Deployments for `api`, `worker`, `scheduler`, `ingest` (optional HPA on queue depth)
- External Exasol (SaaS or on-prem); secrets via ExternalSecrets or existing Secrets
- `runner` as a separate chart or binary for remote networks
- Ingress with TLS; NetworkPolicies; non-root, read-only filesystem, distroless image
- `/healthz`, `/readyz`, Prometheus `/metrics`, OTel exporter; `sentinel-migrate` as a pre-upgrade Job (with `OPS_SENTINEL_AUTO_MIGRATE=false`)

**Operational qualities:** idempotent jobs, retries with backoff, dead-letter queue, graceful shutdown that drains
leases, per-workspace rate limits and LLM cost caps, data retention policies, and backups (Exasol backups of the platform schema).

## 13. Quality: evals as a first-class feature

- `evals/` holds recorded incidents (signals plus frozen tool outputs) and expected outcomes (root cause keywords,
  chosen action, severity).
- `sentinel eval` replays them against an agent or model config and scores accuracy, steps, tokens and cost. It
  runs in CI on every change to prompts, agents or model roles.
- Production feedback (helpful or not, corrected root cause) flows into memory and can be promoted to new eval
  cases.

## 14. Delivery plan

| Phase | Scope | Exit criteria |
| --- | --- | --- |
| **0. Foundations** ✅ | Exasol platform schema + migrations, UDFs, tenancy tables, job queue, config schema/loader with secret refs, domain models, audit, compose quickstart | `docker compose up` brings up api+worker on Exasol; `sentinel-validate` passes |
| **1. Signal → incident → notify** ✅ (runner protocol v2 pending) | Ingest API, generic webhook + Alertmanager + Grafana + runner sources, correlation/dedup, channels (Slack, webhook), routes, auto-resolve on recovery; still to do: port the TS CLI to `/v1/ingest` and the scaffold to the v2 compose | An Alertmanager alert creates one incident and reaches the routed Slack channel |
| **2. Agent runtime** | LLM gateway (OpenAI-compatible, Anthropic, Ollama), agent loop, tool registry, transformers, built-in toolsets (exasol, http, mcp, prometheus), triage + investigator + subagents, timeline events | The demo incident gets a root cause backed by tool evidence |
| **3. Remediation** | Action catalog, policy engine, approvals with roles, SQL and runner executors, verification, memory write-back | An approved action runs, is verified and resolves the incident |
| **4. Knowledge / RAG** | Knowledge connectors (file, git, URL), chunk/embed/hybrid retrieval, runbook matching, similar-incident memory, context manager budgets | Runbooks and past incidents measurably improve eval scores |
| **5. Production hardening** | OIDC + RBAC, per-workspace views, Helm chart, rate and cost limits, web UI v1, eval harness in CI, MCP server endpoint | Helm install on a fresh cluster passes the smoke suite |
| **6. Ecosystem** | Teams, email, PagerDuty and Opsgenie channels; Datadog, CloudWatch and GitHub sources; k8s, loki and github toolsets; plugin SDK docs | Third-party plugin installable via pip |

Each phase ships working software. v1 endpoints (`/api/events`) stay behind a compatibility shim until phase 3 ends.

## 15. Decisions

1. **Exasol is the system of record**, including the queue, the audit log and vectors, with Python UDFs for
   in-database fingerprinting, similarity and anomaly scoring (§4a). Exasol is also a toolset the agent investigates.
2. **The Python engine is the brain; the TS CLI becomes `init` / `up` / `apply` plus the edge `runner`**, keeping
   the edge security model.
3. **Own thin LLM adapters, with LiteLLM optional**, rather than adopting an agent framework (LangGraph, CrewAI)
   wholesale, so budgets, policy checks and audit stay exact.
4. **Exasol-backed job queue**, with NATS or SQS behind the `JobQueue` interface if volume ever demands it.

## 16. Running it today

```
python scripts/bootstrap_sentinel_roles.py          # once, as a DBA: schema + 4 users, prints .env lines
cp examples/sentinel.yaml deploy/compose/            # edit workspace, sources, channels, routes
sentinel-validate deploy/compose/sentinel.yaml       # offline check; prints each source's ingest URL
cd deploy/compose && docker compose up -d            # api migrates + installs UDFs, worker processes jobs

curl -X POST localhost:8000/v1/ingest/data-platform/prometheus \
  -H "Authorization: Bearer $AM_INGEST_TOKEN" -d @alertmanager-sample.json
curl -H "X-API-Key: $OPS_SENTINEL_API_KEY" localhost:8000/v1/workspaces/data-platform/incidents
```
