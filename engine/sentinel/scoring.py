"""Computes the two headline dashboard metrics from Exasol directly (admin
read connection — fine here since it's just aggregation for the dashboard,
not incident investigation, which stays on the MCP path).

Uses the pooled connection from sentinel/db.py: these functions are hit on
every dashboard refresh, so this is exactly the kind of call site the
connection-per-call bug used to hurt most.

Note: pyexasol binds query parameters by name (`{param}` placeholders + a
dict), not positional `?` placeholders — and its *default* conversion quotes
every value as a string (`{x}` -> `'5'`), which Exasol rejects in numeric
contexts like `LIMIT` or a `DECIMAL` comparison. Numeric placeholders below
use the `!d` conversion (`{x!d}` -> `5`, unquoted) instead.
"""
from sentinel.config import settings
from sentinel.db import get_connection


def compute_trust_score() -> float:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN STATUS = 'SUCCESS' THEN 1 ELSE 0 END) AS successes, "
            "  COUNT(*) AS total "
            "FROM PIPELINE_LOGS "
            "WHERE EXECUTION_TS > CURRENT_TIMESTAMP - INTERVAL '{window_hours!d}' HOUR",
            {"window_hours": settings.trust_score_window_hours},
        ).fetchone()
    successes, total = row
    if not total:
        return 100.0
    return round((successes / total) * 100, 1)


def compute_efficiency_score() -> float:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT "
            "  SUM(CASE WHEN ESTIMATED_COST_USD > {threshold!d} THEN 1 ELSE 0 END) AS spikes, "
            "  COUNT(*) AS total "
            "FROM QUERY_METRICS "
            "WHERE EXECUTED_TS > CURRENT_TIMESTAMP - INTERVAL '{window_hours!d}' HOUR",
            {"threshold": settings.cost_spike_threshold_usd, "window_hours": settings.trust_score_window_hours},
        ).fetchone()
    spikes, total = row
    if not total:
        return 100.0
    return round(100 - (spikes / total) * 100, 1)


def recent_incidents(limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT INCIDENT_ID, SOURCE_TYPE, STATUS, BUSINESS_IMPACT, RISK_LEVEL, CREATED_TS "
            "FROM INCIDENTS ORDER BY CREATED_TS DESC LIMIT {limit!d}",
            {"limit": limit},
        ).fetchall()
    cols = ["incident_id", "source_type", "status", "business_impact", "risk_level", "created_ts"]
    return [dict(zip(cols, r, strict=True)) for r in rows]


def incident_timeline(incident_id: int) -> dict | None:
    """The full lifecycle of one incident: its row from INCIDENTS plus every
    remediation attempt against it, oldest first, for a per-incident timeline view."""
    with get_connection() as conn:
        incident_row = conn.execute(
            "SELECT INCIDENT_ID, SOURCE_TYPE, STATUS, BUSINESS_IMPACT, ROOT_CAUSE, SUGGESTED_FIX_SQL, "
            "RISK_LEVEL, CREATED_TS, RESOLVED_TS FROM INCIDENTS WHERE INCIDENT_ID = {incident_id!d}",
            {"incident_id": incident_id},
        ).fetchone()
        if incident_row is None:
            return None
        action_rows = conn.execute(
            "SELECT ACTION_ID, EXECUTED_SQL, EXECUTED_BY, EXECUTED_TS, OUTCOME, OUTCOME_DETAIL "
            "FROM REMEDIATION_ACTIONS WHERE INCIDENT_ID = {incident_id!d} ORDER BY EXECUTED_TS ASC",
            {"incident_id": incident_id},
        ).fetchall()

    incident_cols = [
        "incident_id", "source_type", "status", "business_impact", "root_cause",
        "suggested_fix_sql", "risk_level", "created_ts", "resolved_ts",
    ]
    action_cols = ["action_id", "executed_sql", "executed_by", "executed_ts", "outcome", "outcome_detail"]
    return {
        "incident": dict(zip(incident_cols, incident_row, strict=True)),
        "actions": [dict(zip(action_cols, r, strict=True)) for r in action_rows],
    }
