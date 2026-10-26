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
| Detection bookkeeping, scoring | `db.py` | `sentinel_engine` (`EXASOL_ADMIN_*`), pooled | Only Sentinel's own `INCIDENTS` rows and the append-only audit table |
| Remediation | `remediation.py` | `sentinel_executor` (`EXASOL_EXECUTOR_*`) | Business tables only, after approval and guardrail |

Only `remediation.py` executes model-suggested SQL.

## Remediation safeguards

1. **Atomic claim.** `UPDATE INCIDENTS SET STATUS = 'APPROVED' WHERE STATUS = 'AWAITING_APPROVAL'` succeeds for
   exactly one caller, so a double click or a retried Slack delivery cannot run a fix twice.
2. **Guardrail.** `guardrail.validate_fix_sql` masks string literals, rejects comments, allows exactly one statement
   with a verb from an allow-list (`INSERT`, `UPDATE`, `MERGE`, `DELETE`, `SELECT`), rejects schema, permission and
   external-I/O keywords, Sentinel's own tables and system objects, requires a real (non-tautological) top-level
   `WHERE` on `UPDATE` and `DELETE`, and restricts the target to the incident's tables plus one lineage hop.
3. **Least privilege.** The executor user has no grants on Sentinel's tables (`sql/roles.sql`), so a guardrail
   bug still cannot rewrite incidents or the audit trail.
4. **Audit.** Every outcome, including blocked ones, is written to `REMEDIATION_ACTIONS` with who approved it.

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
