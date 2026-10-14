# Ops Sentinel

> The Autonomous, Cost-Aware Platform Command Center — ExaThon India 2026

Ops Sentinel closes the loop between **data reliability** (pipeline failures) and
**cloud cost waste** (runaway/duplicate queries) using Exasol Personal as the
metadata/analytics layer, MCP as the read-only investigation path, an LLM as the
triage agent, and Slack as the human-approval gate before anything is ever
re-executed against the database.

## Why this is not a "plugin / npm package / tool file"

It is a small **multi-service platform**: a database, a FastAPI orchestration
engine, a Streamlit dashboard, and a Slack app — four things that talk to each
other over the network, not one thing that gets installed into a host app.
Each module below maps directly onto the brief's Module A/B/C split so it can
be parallelized across a 1–3 person team.

## Architecture (6-step loop)

```
Event Log / Compute Spike
        │ (ingested into Exasol)
        ▼
Exasol Personal (PIPELINE_LOGS, QUERY_METRICS, DATA_LINEAGE)
        │ watcher.py polls for new FAILED rows / cost spikes
        ▼
engine/watcher.py  ──MCP (read-only)──▶  error log + lineage context
        │
        ▼
engine/triage_agent.py  ──LLM API──▶  business-impact narrative + patch SQL
        │
        ▼
engine/slack_notifier.py  ──Slack Block Kit──▶  Approve / Reject card
        │ (engineer clicks a button)
        ▼
engine/main.py: POST /slack/interactions  ──verifies signature──▶
        │
        ▼
engine/remediation.py  ──admin pyexasol (write path, NOT MCP)──▶  re-run SQL
        │
        ▼
Exasol tables refreshed → dashboard/app.py reflects new Trust / Efficiency scores
```

**Read vs. write split (important):** the Exasol MCP server login is
database-enforced read-only. Ops Sentinel uses it for investigation (steps
2–3) and uses a *separate* admin `pyexasol` connection, gated by an explicit
Slack approval, for the one privileged action in step 5. Every remediation is
written to `REMEDIATION_ACTIONS` for audit.

## Repo layout

```
db/            Module A — schema + synthetic seed data
engine/        Module B — FastAPI engine: MCP client, LLM triage, Slack, remediation
dashboard/     Module C (dashboard half) — Streamlit Trust Score / Efficiency Rating
scripts/       Helper scripts (seeding, demo incident injection)
```

## Status: Exasol + MCP are live on this machine

The Exasol Personal Local Starter Kit is installed and running (`exakit status`),
`db/schema.sql` is applied, and `scripts/seed_data.py` / `scripts/inject_demo_incident.py`
have populated real data. `.env` holds this machine's real connection details
(DSN, admin credentials, and the MCP read-only login — see `exakit info`).

Two things to know before you run anything yourself:

1. **pyexasol binds parameters by name, not position.** `conn.execute(sql, params)`
   uses `{name}` placeholders bound from a dict — not `?` markers. Its default
   conversion also quotes every value as a string, which Exasol rejects in
   numeric contexts (`LIMIT '20'` fails) — use the `{x!d}` conversion for any
   numeric placeholder. Every query in `engine/` already does this correctly;
   keep the pattern if you add more.
2. **The MCP server needs `uvx`** (installed by the starter kit at
   `~/.local/bin`) and downloads `exasol-mcp-server` on first use — the very
   first MCP call takes ~60-90s, after which `uv`'s cache makes it fast.

Proven working end to end against the live database in this session:
- **Detection** — the real watcher polled `PIPELINE_LOGS`/`QUERY_METRICS` and created real `INCIDENTS` rows.
- **MCP investigation** — the real `uvx exasol-mcp-server` was spawned and returned real error context + lineage (25 tools discovered).
- **Dashboard API** — `/api/trust-score`, `/api/efficiency-score`, `/api/incidents` all returned `200 OK` computed from live data.
- **Remediation guardrail** — a real `DELETE FROM sales_clean` (no `WHERE`) was blocked before touching data; a real approved `UPDATE` was applied and changed real rows; a duplicate approval was rejected by the atomic claim. All three outcomes landed in the real `REMEDIATION_ACTIONS` audit table.
- **Not yet real**: LLM triage and Slack notifications — no `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`SLACK_BOT_TOKEN` are set in `.env`. The watcher detects this, logs it, and moves on without crashing (see `engine/watcher.py`'s `_handle_incident_safely`). Add those keys to `.env` and the exact same code path becomes fully real — no code changes needed.

To rerun this yourself:
```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r engine\requirements.txt
$env:PYTHONPATH = "$PWD"; $env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe -m uvicorn engine.main:app --port 8000
# in another terminal:
.\.venv\Scripts\python.exe scripts\inject_demo_incident.py
.\.venv\Scripts\python.exe scripts\test_mcp_read.py          # proves the MCP read path
.\.venv\Scripts\python.exe scripts\test_remediation_live.py  # proves the write path + guardrail
```

## See it run right now, with nothing installed

Before touching Exasol, Slack, or an LLM key, you can watch the full 6-step
loop execute for real — it uses an in-memory SQLite stand-in for Exasol, and
automatically upgrades any step to a real call the moment you add the
matching credential to `.env` (LLM triage and the Slack card both do this;
the MCP read step stays simulated until `exakit` is installed, since that's
the piece Exasol itself provides):

```powershell
python -m venv .venv
.\.venv\Scripts\pip install pydantic-settings python-dotenv pyexasol
$env:PYTHONPATH = "$PWD"; $env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe scripts\dry_run.py
```

This also doubles as a smoke test for `engine/remediation.py`'s safety
guardrail — the run prints three deliberately dangerous SQL strings (`DROP
TABLE`, an unscoped `DELETE`, a multi-statement payload) and shows the real
validation function rejecting each one before showing the approved fix
actually being applied.

## Setup (weekend build order)

### Day 1 morning — Module A (whoever is comfortable with SQL)
1. Install the Exasol Personal Local Starter Kit (one command — ask your AI
   assistant to "set up the Exasol starter kit" or run it yourself):
   - macOS/Linux/WSL: `curl https://www.exasol.com/install/starter-kit.sh | sh`
   - Windows PowerShell: `irm https://www.exasol.com/install/starter-kit.ps1 | iex`
2. Connect an MCP client: `exakit mcp-setup` (pick Cursor/Claude/etc.)
3. Create the schema and load synthetic data:
   ```powershell
   exapump sql -p starter-kit -f db/schema.sql
   python scripts/seed_data.py
   ```
4. Sanity check: ask your MCP-connected assistant to list tables in the
   `SENTINEL` schema and show you the SQL before running anything (same
   inspect-before-run discipline as the rest of the kit).

### Day 1 afternoon — Module B (whoever owns backend/Python)
1. `python -m venv .venv && .venv\Scripts\activate` (Windows) then
   `pip install -r engine/requirements.txt`
2. Copy `.env.example` to `.env` and fill in:
   - `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`
   - `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_CHANNEL_ID`
   - `EXASOL_ADMIN_*` (the admin connection used only for remediation writes —
     `exakit info` shows where the password is stored)
3. Run the engine: `uvicorn engine.main:app --reload --port 8000`
4. Expose it to Slack for interactivity callbacks during the demo:
   `ngrok http 8000`, then set the Slack app's **Interactivity Request URL**
   to `https://<ngrok-id>.ngrok.io/slack/interactions`.

### Day 2 morning — Module C (whoever owns frontend)
1. `pip install -r dashboard/requirements.txt`
2. `streamlit run dashboard/app.py` — reads from the engine's REST API
   (`/api/trust-score`, `/api/efficiency-score`, `/api/incidents`).

### Day 2 afternoon — integration + demo script
1. Run `python scripts/inject_demo_incident.py` to simulate a pipeline
   failure and a cost-spike query landing in Exasol.
2. Watch `watcher.py` logs: it should detect the row, call MCP for context,
   call the LLM, and post a Slack card within a few seconds.
3. Click **Approve** in Slack → confirm `remediation.py` re-runs the fix and
   the dashboard's Trust Score / Efficiency Rating move.
4. Rehearse the "no" path too — click **Reject** and show the incident stays
   open, nothing executes.

## Team split (1–3 people)

| People | Split |
|---|---|
| 1 | Do Module A first (fast), then B, then C — in that order, since B depends on A and C depends on B. |
| 2 | Person 1: Module A + Module B's `mcp_client.py`/`triage_agent.py`. Person 2: Module B's `main.py`/`slack_notifier.py`/`remediation.py` + Module C. |
| 3 | Person 1: Module A. Person 2: Module B. Person 3: Module C + the Slack app config/ngrok plumbing + demo script. |
