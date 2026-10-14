"""
The ONLY module that writes to Exasol, and only after a Slack approval. Uses
the pooled admin connection from engine/db.py, never MCP — the MCP login is
read-only by design and that boundary should stay real, not just conventional.

CRITICAL FIX: the previous version ran `SUGGESTED_FIX_SQL` from the LLM
verbatim, with no validation at all. That's the single largest risk surface
in the whole system — an error stack is attacker-influenceable text (it's
whatever a failing pipeline wrote to a log), and a prompt-injected error
stack could steer the model into suggesting SQL that does far more than the
business-impact narrative implies. `_validate_fix_sql` below is a hard gate
in front of every execution: single statement only, verb allow-listed,
schema-altering keywords blocked, and unscoped UPDATE/DELETE blocked. It is
intentionally conservative — reject first, widen the allow-list later with
real remediation patterns your team has actually seen, not with whatever an
LLM happens to produce.

This also fixes a correctness bug, not just a safety one: the previous
version didn't check the incident was still awaiting approval before
executing, so a double-click or a retried Slack webhook delivery (Slack
retries failed/slow interactions automatically) could run the same fix SQL
twice. `execute_remediation` now atomically claims the incident with an
UPDATE ... WHERE STATUS = 'AWAITING_APPROVAL' before touching the fix SQL.

Note on pyexasol parameter binding: unlike most DB-API drivers, pyexasol
does not use positional `?` placeholders — it uses named `{name}` format
placeholders bound from a dict (see `conn.execute(query, query_params)` in
the pyexasol docs). Its *default* conversion quotes every value as a string
(`{x}` -> `'1'`), so numeric columns like INCIDENT_ID use the `!d`
conversion (`{x!d}` -> `1`, unquoted) to match their actual column type.
"""
from datetime import datetime

from engine.db import get_connection

# Conservative allow-list: only these leading verbs are eligible for
# automated remediation at all. SELECT is included so a "re-run a query to
# refresh a cached result" fix is possible without being able to mutate data.
_ALLOWED_LEADING_VERBS = {"INSERT", "UPDATE", "MERGE", "DELETE", "CALL", "SELECT"}

# Keywords that mean "this changes schema/permissions", never appropriate for
# an unattended-approved remediation regardless of who/what suggested it.
_DANGEROUS_KEYWORDS = {
    "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE", "ATTACH", "DETACH",
}


def _validate_fix_sql(sql: str | None) -> tuple[bool, str]:
    """Returns (is_safe, detail). On success, `detail` is the single
    normalized statement that is safe to execute. On failure, `detail` is a
    human-readable reason, suitable for the Slack thread and the audit log.
    """
    if not sql or not sql.strip():
        return False, "No suggested fix SQL recorded for this incident."

    # Reject multi-statement payloads outright — a model (or an injected
    # error stack) appending a second statement after a semicolon is exactly
    # the kind of thing this gate exists to catch.
    statements = [s.strip() for s in sql.strip().rstrip(";").split(";") if s.strip()]
    if len(statements) != 1:
        return False, "Refusing to run multi-statement SQL — exactly one statement is allowed per remediation."

    statement = statements[0]
    upper = statement.upper()
    first_word = statement.split(None, 1)[0].upper() if statement.split() else ""

    if first_word not in _ALLOWED_LEADING_VERBS:
        return False, (
            f"Refusing to run a '{first_word or '?'}' statement — only "
            f"{sorted(_ALLOWED_LEADING_VERBS)} are allowed in automated remediation."
        )

    if any(f" {kw} " in f" {upper} " or upper.startswith(f"{kw} ") for kw in _DANGEROUS_KEYWORDS):
        return False, "Refusing to run SQL containing a schema- or permission-altering keyword."

    if first_word in {"UPDATE", "DELETE"} and " WHERE " not in upper:
        return False, (
            f"Refusing to run an unscoped {first_word} with no WHERE clause — "
            "too high a blast radius for automated remediation."
        )

    return True, statement


def execute_remediation(incident_id: int, approved_by: str) -> tuple[bool, str]:
    with get_connection() as conn:
        # Atomic claim: only one caller can move AWAITING_APPROVAL -> APPROVED
        # for a given incident. A duplicate Slack delivery or a double-click
        # loses this race and is turned away before it can touch the fix SQL.
        claim = conn.execute(
            "UPDATE INCIDENTS SET STATUS = 'APPROVED' "
            "WHERE INCIDENT_ID = {incident_id!d} AND STATUS = 'AWAITING_APPROVAL'",
            {"incident_id": incident_id},
        )
        if claim.rowcount() == 0:
            return False, "Incident is not awaiting approval (already resolved or a duplicate action) — ignoring."

        row = conn.execute(
            "SELECT SUGGESTED_FIX_SQL FROM INCIDENTS WHERE INCIDENT_ID = {incident_id!d}",
            {"incident_id": incident_id},
        ).fetchone()
        fix_sql = row[0] if row else None

        is_safe, detail = _validate_fix_sql(fix_sql)
        if not is_safe:
            conn.execute(
                "UPDATE INCIDENTS SET STATUS = 'FAILED' WHERE INCIDENT_ID = {incident_id!d}",
                {"incident_id": incident_id},
            )
            conn.execute(
                "INSERT INTO REMEDIATION_ACTIONS "
                "(INCIDENT_ID, EXECUTED_SQL, EXECUTED_BY, EXECUTED_TS, OUTCOME, OUTCOME_DETAIL) "
                "VALUES ({incident_id!d}, {executed_sql}, {executed_by}, {executed_ts}, 'BLOCKED', {detail})",
                {
                    "incident_id": incident_id,
                    "executed_sql": fix_sql or "",
                    "executed_by": approved_by,
                    "executed_ts": datetime.utcnow(),
                    "detail": detail,
                },
            )
            return False, detail

        safe_sql = detail
        try:
            conn.execute(safe_sql)
            conn.execute(
                "UPDATE INCIDENTS SET STATUS = 'REMEDIATED', RESOLVED_TS = {resolved_ts} "
                "WHERE INCIDENT_ID = {incident_id!d}",
                {"resolved_ts": datetime.utcnow(), "incident_id": incident_id},
            )
            conn.execute(
                "INSERT INTO REMEDIATION_ACTIONS "
                "(INCIDENT_ID, EXECUTED_SQL, EXECUTED_BY, EXECUTED_TS, OUTCOME, OUTCOME_DETAIL) "
                "VALUES ({incident_id!d}, {executed_sql}, {executed_by}, {executed_ts}, 'SUCCESS', {detail})",
                {
                    "incident_id": incident_id,
                    "executed_sql": safe_sql,
                    "executed_by": approved_by,
                    "executed_ts": datetime.utcnow(),
                    "detail": "Remediation applied",
                },
            )
            return True, "Remediation applied successfully."

        except Exception as exc:  # noqa: BLE001 — surface any failure to Slack + the audit log
            conn.execute(
                "UPDATE INCIDENTS SET STATUS = 'FAILED' WHERE INCIDENT_ID = {incident_id!d}",
                {"incident_id": incident_id},
            )
            conn.execute(
                "INSERT INTO REMEDIATION_ACTIONS "
                "(INCIDENT_ID, EXECUTED_SQL, EXECUTED_BY, EXECUTED_TS, OUTCOME, OUTCOME_DETAIL) "
                "VALUES ({incident_id!d}, {executed_sql}, {executed_by}, {executed_ts}, 'FAILED', {detail})",
                {
                    "incident_id": incident_id,
                    "executed_sql": safe_sql,
                    "executed_by": approved_by,
                    "executed_ts": datetime.utcnow(),
                    "detail": str(exc),
                },
            )
            # Re-raising would mark this connection unhealthy in the pool for
            # a failure that's about the *data*, not the connection — we've
            # already recorded the outcome, so swallow it here and return.
            return False, f"Remediation failed: {exc}"


def reject_incident(incident_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE INCIDENTS SET STATUS = 'REJECTED', RESOLVED_TS = {resolved_ts} "
            "WHERE INCIDENT_ID = {incident_id!d} AND STATUS = 'AWAITING_APPROVAL'",
            {"resolved_ts": datetime.utcnow(), "incident_id": incident_id},
        )
