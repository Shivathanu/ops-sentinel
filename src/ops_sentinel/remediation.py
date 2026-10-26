"""
The ONLY module that executes model-suggested SQL, and only after a Slack approval. It never uses MCP: the
MCP login is read-only by design and that boundary should stay real, not just conventional.

Three independent protections stand between an approval click and a write:

1. An atomic claim. `UPDATE ... WHERE STATUS = 'AWAITING_APPROVAL'` succeeds for exactly one caller, so a
   double click or a retried Slack delivery (Slack retries slow interactions) cannot run a fix twice.
2. The guardrail (guardrail.py), scoped to the tables this incident touches.
3. A separate executor database user (see db.py and sql/roles.sql) that cannot write INCIDENTS or the audit
   trail, so even a guardrail bug cannot rewrite history.

Every outcome, including blocked ones, is written to REMEDIATION_ACTIONS.

Note on pyexasol parameter binding: it uses named `{name}` placeholders bound from a dict, not positional `?`.
Its default conversion quotes every value as a string, so numeric columns use `{x!d}`.
"""
import logging
from datetime import datetime, timezone

import pyexasol

from ops_sentinel.config import settings
from ops_sentinel.db import get_connection, get_executor_connection
from ops_sentinel.guardrail import validate_fix_sql

logger = logging.getLogger("ops_sentinel.remediation")


def execute_remediation(incident_id: int, approved_by: str) -> tuple[bool, str]:
    with get_connection() as conn:
        if not _claim_incident(conn, incident_id):
            logger.warning(
                "ignoring approval by %s: incident #%s is not awaiting approval", approved_by, incident_id
            )
            return False, "Incident is not awaiting approval (already resolved or a duplicate action) — ignoring."

        fix_sql = _load_fix_sql(conn, incident_id)
        verdict = validate_fix_sql(
            fix_sql,
            allowed_tables=_incident_tables(conn, incident_id),
            home_schema=settings.exasol_schema,
        )
        if not verdict.allowed:
            logger.warning("blocked remediation for incident #%s approved by %s", incident_id, approved_by)
            _record_outcome(conn, incident_id, fix_sql or "", approved_by, "BLOCKED", verdict.reason or "")
            return False, verdict.reason or "Blocked by the guardrail."

        try:
            _run_as_executor(verdict.statement or "")
        except Exception as exc:
            # The outcome is recorded below instead of re-raising: the failure is about the statement or its
            # data, and re-raising would also mark this healthy engine connection bad in the pool.
            logger.exception("remediation for incident #%s failed", incident_id)
            _record_outcome(conn, incident_id, verdict.statement or "", approved_by, "FAILED", str(exc))
            return False, f"Remediation failed: {exc}"

        _record_outcome(
            conn, incident_id, verdict.statement or "", approved_by, "SUCCESS", "Remediation applied"
        )
        logger.info("remediation for incident #%s applied, approved by %s", incident_id, approved_by)
        return True, "Remediation applied successfully."


def reject_incident(incident_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE INCIDENTS SET STATUS = 'REJECTED', RESOLVED_TS = {resolved_ts} "
            "WHERE INCIDENT_ID = {incident_id!d} AND STATUS = 'AWAITING_APPROVAL'",
            {"resolved_ts": _utcnow(), "incident_id": incident_id},
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # Exasol TIMESTAMP columns are timezone-naive


def _claim_incident(conn: pyexasol.ExaConnection, incident_id: int) -> bool:
    claim = conn.execute(
        "UPDATE INCIDENTS SET STATUS = 'APPROVED' "
        "WHERE INCIDENT_ID = {incident_id!d} AND STATUS = 'AWAITING_APPROVAL'",
        {"incident_id": incident_id},
    )
    return claim.rowcount() > 0


def _load_fix_sql(conn: pyexasol.ExaConnection, incident_id: int) -> str | None:
    row = conn.execute(
        "SELECT SUGGESTED_FIX_SQL FROM INCIDENTS WHERE INCIDENT_ID = {incident_id!d}",
        {"incident_id": incident_id},
    ).fetchone()
    return row[0] if row else None


def _incident_tables(conn: pyexasol.ExaConnection, incident_id: int) -> frozenset[str]:
    """Upper-case names of the tables a fix for this incident may modify.

    That is the tables the failing pipeline or expensive query touched plus their one-hop lineage neighbours,
    because the root cause is often just upstream of the table that failed.
    """
    seeds = _incident_seed_tables(conn, incident_id)
    tables = set(seeds)
    for table in seeds:
        rows = conn.execute(
            "SELECT SOURCE_TABLE, TARGET_TABLE FROM DATA_LINEAGE "
            "WHERE UPPER(SOURCE_TABLE) = {table} OR UPPER(TARGET_TABLE) = {table}",
            {"table": table},
        ).fetchall()
        tables.update(name.upper() for row in rows for name in row if name)
    return frozenset(tables)


def _incident_seed_tables(conn: pyexasol.ExaConnection, incident_id: int) -> set[str]:
    source = conn.execute(
        "SELECT SOURCE_TYPE, SOURCE_REF_ID FROM INCIDENTS WHERE INCIDENT_ID = {incident_id!d}",
        {"incident_id": incident_id},
    ).fetchone()
    if source is None:
        logger.warning("incident #%s has no INCIDENTS row; no tables are in scope", incident_id)
        return set()
    source_type, ref_id = source
    if source_type == "PIPELINE_FAILURE":
        row = conn.execute(
            "SELECT SOURCE_TABLE, TARGET_TABLE FROM PIPELINE_LOGS WHERE LOG_ID = {ref_id!d}",
            {"ref_id": int(ref_id)},
        ).fetchone()
    elif source_type == "COST_SPIKE":
        row = conn.execute(
            "SELECT TABLE_NAME FROM QUERY_METRICS WHERE QUERY_ID = {ref_id!d}", {"ref_id": int(ref_id)}
        ).fetchone()
    else:
        logger.warning("incident #%s has unknown source type %r; no tables are in scope", incident_id, source_type)
        return set()
    names = {name.upper() for name in (row or ()) if name}
    if not names:
        logger.warning("incident #%s has no recorded tables; no tables are in scope", incident_id)
    return names


def _run_as_executor(statement: str) -> None:
    with get_executor_connection() as executor:
        executor.execute(statement)


def _record_outcome(
    conn: pyexasol.ExaConnection,
    incident_id: int,
    executed_sql: str,
    approved_by: str,
    outcome: str,
    detail: str,
) -> None:
    """Moves the incident to its final status and appends the audit row."""
    succeeded = outcome == "SUCCESS"
    conn.execute(
        "UPDATE INCIDENTS SET STATUS = {status}, RESOLVED_TS = {resolved_ts} WHERE INCIDENT_ID = {incident_id!d}",
        {
            "status": "REMEDIATED" if succeeded else "FAILED",
            "resolved_ts": _utcnow() if succeeded else None,
            "incident_id": incident_id,
        },
    )
    conn.execute(
        "INSERT INTO REMEDIATION_ACTIONS "
        "(INCIDENT_ID, EXECUTED_SQL, EXECUTED_BY, EXECUTED_TS, OUTCOME, OUTCOME_DETAIL) "
        "VALUES ({incident_id!d}, {executed_sql}, {executed_by}, {executed_ts}, {outcome}, {detail})",
        {
            "incident_id": incident_id,
            "executed_sql": executed_sql,
            "executed_by": approved_by,
            "executed_ts": _utcnow(),
            "outcome": outcome,
            "detail": detail,
        },
    )
