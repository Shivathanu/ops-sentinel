"""Computes the two headline dashboard metrics from Exasol directly (admin
read connection — fine here since it's just aggregation for the dashboard,
not incident investigation, which stays on the MCP path).

Uses the pooled connection from engine/db.py: these functions are hit on
every dashboard refresh, so this is exactly the kind of call site the
connection-per-call bug used to hurt most.

Note: pyexasol binds query parameters by name (`{param}` placeholders + a
dict), not positional `?` placeholders — and its *default* conversion quotes
every value as a string (`{x}` -> `'5'`), which Exasol rejects in numeric
contexts like `LIMIT` or a `DECIMAL` comparison. Numeric placeholders below
use the `!d` conversion (`{x!d}` -> `5`, unquoted) instead.
"""
from engine.config import settings
from engine.db import get_connection


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
    return [dict(zip(cols, r)) for r in rows]
