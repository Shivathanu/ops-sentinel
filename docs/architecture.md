# Architecture

Ops Sentinel is one FastAPI service plus a Streamlit dashboard, with Exasol as the only datastore.

## The loop

1. **Detect.** `watcher.py` polls `PIPELINE_LOGS` for `FAILED` runs and `QUERY_METRICS` for queries above the
   cost threshold, and creates an `INCIDENTS` row for each one it has not seen.
2. **Investigate.** `mcp_client.py` starts the Exasol MCP server and reads the failing run and its downstream
   lineage through the read-only login.
3. **Triage.** `triage_agent.py` sends that context to the configured LLM and expects strict JSON: business
   impact, root cause, one proposed fix statement or `null`, and a risk level.
4. **Ask.** `slack_notifier.py` posts the result as a Block Kit card with Approve and Reject buttons.
5. **Remediate.** `main.py` verifies Slack's signature, acknowledges within Slack's 3-second window and hands the
   work to `remediation.py`, which runs the fix on the write connection.
6. **Reflect.** `scoring.py` computes the Trust Score and Efficiency Rating from the same tables; the dashboard
   reads them from the REST API.

## Read and write boundary

| Path | Module | Credential | Can write? |
| --- | --- | --- | --- |
| Investigation | `mcp_client.py` | `EXA_USER`, read-only login enforced by Exasol | No |
| Detection bookkeeping, scoring | `db.py` | admin connection from a small pool | Only Sentinel's own `INCIDENTS` rows |
| Remediation | `remediation.py` | admin connection | Yes, after approval and guardrail |

Only `remediation.py` executes model-suggested SQL.

## Remediation safeguards

1. **Atomic claim.** `UPDATE INCIDENTS SET STATUS = 'APPROVED' WHERE STATUS = 'AWAITING_APPROVAL'` succeeds for
   exactly one caller, so a double click or a retried Slack delivery cannot run a fix twice.
2. **Guardrail.** `_validate_fix_sql` allows one statement, with a leading verb from an allow-list
   (`INSERT`, `UPDATE`, `MERGE`, `DELETE`, `CALL`, `SELECT`), rejects schema and permission keywords, and rejects
   `UPDATE` and `DELETE` without a `WHERE`.
3. **Audit.** Every outcome, including blocked ones, is written to `REMEDIATION_ACTIONS` with who approved it.

## Incident states

```
DETECTED ─► AWAITING_APPROVAL ─► APPROVED ─► REMEDIATED
                  │                  └─────► FAILED   (guardrail blocked it, or execution raised)
                  └─► REJECTED
```

## Data model

| Table | Purpose |
| --- | --- |
| `PIPELINE_LOGS` | Pipeline runs with status and error stack |
| `QUERY_METRICS` | Query text, compute time and estimated cost |
| `DATA_LINEAGE` | Source table to target table to downstream dashboard, with owner and criticality |
| `INCIDENTS` | One row per detected incident and its lifecycle |
| `REMEDIATION_ACTIONS` | Audit trail of every write Sentinel attempted |

## Concurrency notes

The watcher runs inside the API process as a background task and wraps every blocking call in
`asyncio.to_thread`. Running the API with more than one worker starts several watchers that can race and create
duplicate incidents, so run one worker until the watcher is split into its own process.
