# Ops Sentinel

> The autonomous, cost-aware platform command center.

Ops Sentinel watches an [Exasol](https://www.exasol.com/) database for failed data pipelines and
runaway query costs, investigates each incident over the Model Context Protocol (MCP), asks an LLM for
a business-impact summary and a proposed fix, and sends it to Slack as an Approve / Reject card.
Nothing is executed until a human approves, and every approved action passes a deterministic guardrail
and lands in an audit table.

## The problem

A platform engineering lead cannot scale business operations efficiently because overnight data pipeline
failures disrupt company-wide decision trust, while unoptimized query compute costs silently drain the
infrastructure budget before engineers can manually triage them. If this solution works, the organization
will maintain trusted data freshness and eliminate infrastructural cost waste through automated,
human-verified remediation.

## How it works

```
event / cost spike ─► Exasol (PIPELINE_LOGS, QUERY_METRICS) ─► watcher detects ─► INCIDENTS row
                                                                      │
        read-only MCP login: error log + downstream lineage ◄─────────┘
                                   │
                          LLM triage (impact, root cause, fix SQL)
                                   │
                       Slack card: Approve / Reject ──► guardrail ──► write connection ──► audit row
                                                                                   │
                              dashboard: Trust Score and Efficiency Rating ◄───────┘
```

Investigation runs on a database-enforced read-only login through MCP. The one privileged action, the
approved remediation, uses a separate write connection. See [docs/architecture.md](docs/architecture.md).

## Status

| Part | State |
| --- | --- |
| Detection, MCP investigation, dashboard API, remediation guardrail and audit | Exercised against a live Exasol Personal instance during development |
| LLM triage | Implemented, not yet exercised with a live key |
| Slack approval card | Implemented, not yet exercised with a live bot token |
| Offline demo (`scripts/dry_run.py`) | Runs the whole loop against an in-memory SQLite stand-in, no credentials needed |

## Quickstart

Requires Python 3.10 or newer and, for the live loop, an Exasol Personal instance.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dashboard]"
cp .env.example .env               # Windows: copy .env.example .env, then fill in the values
```

See the offline demo first, no database or keys needed:

```bash
python scripts/dry_run.py
```

Then run it for real:

```bash
exapump sql -p starter-kit -f sql/schema.sql      # create the SENTINEL schema
python scripts/seed_data.py                       # load synthetic data only
python -m uvicorn ops_sentinel.main:app --port 8000
streamlit run dashboard/app.py                    # in a second terminal
python scripts/inject_demo_incident.py            # in a third: simulate a failure and a cost spike
```

For the Slack approval step, set `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` and `SLACK_CHANNEL_ID` in `.env`
and point the Slack app's Interactivity Request URL at `https://<your-host>/slack/interactions`.
For triage, set `LLM_PROVIDER` and the matching API key. Without these credentials the engine still detects and
investigates incidents, but skips triage and the Slack card and logs that it did.

## Configuration

All settings come from environment variables or `.env`; [.env.example](.env.example) lists every one with
its default. The database is only ever loaded with synthetic or public data.

## Watch any automation with the CLI

The engine is not limited to database jobs. The [`cli/`](cli/README.md) package (`ops-sentinel-cli`, a Node tool
meant for npm) runs next to your own automation, watches a command, a log file or an HTTP endpoint, and reports a
failure to the engine. The incident then goes through the same triage, Slack approval and audit trail.

```bash
sentinel watch                                   # watch every source in sentinel.config.json
sentinel run --name nightly-export -- ./export.sh   # or wrap a single command in cron or CI
```

The fix for such an incident is a command **in your own config file**. The engine only ever sees its id, and on
approval tells the agent to run that id, so the engine can never make a client machine run an arbitrary command.

## Repository layout

```
src/ops_sentinel/   engine: watcher, MCP client, LLM triage, Slack notifier, remediation, scoring, API
dashboard/          Streamlit command dashboard (reads the engine's REST API)
cli/                ops-sentinel-cli: TypeScript agent that watches client automation (npm package)
sql/schema.sql      Exasol schema
scripts/            seeding, demo incident injection, offline demo, live checks
tests/              unit tests (guardrail, triage routing, MCP parsing, API, agent events)
docs/               architecture
```

## Development

```bash
pip install -e ".[dev,dashboard]"
ruff check .
pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## Security model

Model-suggested SQL is untrusted, so four independent controls sit between it and the database:

1. **Guardrail** (`guardrail.py`): one statement, an allow-listed verb, no comments, no DDL, permission or
   external-I/O keywords, no Sentinel or system tables, a real `WHERE` on `UPDATE` and `DELETE`, and a target
   table that belongs to the incident (or is one lineage hop away).
2. **Human approval**: only Slack user IDs in `SLACK_APPROVER_IDS` can approve, the request signature is
   verified, and an atomic claim means a double click runs the fix once.
3. **Least-privilege executor**: approved SQL runs as `sentinel_executor`, which has no grants on Sentinel's own
   tables, so even a guardrail bug cannot edit incidents or the audit trail (`sql/roles.sql`).
4. **Audit trail**: every outcome, including blocked statements, is written to `REMEDIATION_ACTIONS`.

The HTTP API requires `X-API-Key` (`OPS_SENTINEL_API_KEY`), and Exasol TLS certificates are validated by default.
Every one of these fails closed: a missing key, approver list or executor credential refuses the request instead
of falling back to something weaker.

## Known limitations

- `sql/roles.sql` is provided but not applied automatically; until you run it and set the engine and executor
  users, the app refuses to start database work.
- Agent incidents are not de-duplicated by the engine; the CLI de-duplicates per source, so two agents watching the
  same thing would each report it.
- The watcher must run as a single process; two workers could both pick up the same failure.
- `scripts/` helpers still connect with certificate validation off and are for local demos only.
- The guardrail is a deny-by-default filter, not a SQL parser. Widen it only with a test that pins the pattern.

## License

[Apache License 2.0](LICENSE).
