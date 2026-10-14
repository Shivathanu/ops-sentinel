"""
Proves the real remediation write path end to end against the live Exasol
database: the atomic AWAITING_APPROVAL claim, the safety guardrail, and the
actual SQL execution + audit trail — all via engine/remediation.py's real
functions, no simulation.

Run: .venv\\Scripts\\python.exe scripts\\test_remediation_live.py
"""
from dotenv import load_dotenv

load_dotenv()

from engine.db import get_connection  # noqa: E402
from engine.remediation import execute_remediation, reject_incident  # noqa: E402

GOOD_INCIDENT_ID = 7   # corresponds to the real sales_clean / finance_executive_bi failure
BAD_INCIDENT_ID = 6    # a different real incident, used only to prove the guardrail rejects unsafe SQL
SEP = "-" * 78


def set_awaiting_approval(incident_id: int, fix_sql: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE INCIDENTS SET STATUS = 'AWAITING_APPROVAL', SUGGESTED_FIX_SQL = {fix_sql} "
            "WHERE INCIDENT_ID = {incident_id!d}",
            {"fix_sql": fix_sql, "incident_id": incident_id},
        )


def show_sales_clean() -> None:
    with get_connection() as conn:
        rows = conn.execute("SELECT order_id, region_code FROM sales_clean ORDER BY order_id").fetchall()
    for r in rows:
        print(f"    order_id={r[0]} region_code={r[1]!r}")


print(f"\n{SEP}\nPart 1: approve a REAL, safe fix against live Exasol\n{SEP}")
set_awaiting_approval(GOOD_INCIDENT_ID, "UPDATE sales_clean SET region_code = 'UNKNOWN' WHERE region_code IS NULL")
print("sales_clean BEFORE:")
show_sales_clean()

success, detail = execute_remediation(GOOD_INCIDENT_ID, approved_by="live.demo.user")
print(f"\nexecute_remediation(#{GOOD_INCIDENT_ID}) -> success={success}, detail={detail!r}")

print("\nsales_clean AFTER:")
show_sales_clean()

print(f"\n{SEP}\nPart 2: try to approve the SAME incident again (double-click / Slack retry)\n{SEP}")
success2, detail2 = execute_remediation(GOOD_INCIDENT_ID, approved_by="live.demo.user")
print(f"execute_remediation(#{GOOD_INCIDENT_ID}) again -> success={success2}, detail={detail2!r}")
print("(expected: rejected, because the incident is no longer AWAITING_APPROVAL)")

print(f"\n{SEP}\nPart 3: a dangerous suggested fix gets blocked before touching real data\n{SEP}")
set_awaiting_approval(BAD_INCIDENT_ID, "DELETE FROM sales_clean")
success3, detail3 = execute_remediation(BAD_INCIDENT_ID, approved_by="live.demo.user")
print(f"execute_remediation(#{BAD_INCIDENT_ID}, fix='DELETE FROM sales_clean') -> success={success3}, detail={detail3!r}")

print(f"\n{SEP}\nFinal state, read straight from Exasol\n{SEP}")
with get_connection() as conn:
    incidents = conn.execute(
        "SELECT INCIDENT_ID, STATUS, SUGGESTED_FIX_SQL FROM INCIDENTS "
        "WHERE INCIDENT_ID IN ({a!d}, {b!d}) ORDER BY INCIDENT_ID",
        {"a": GOOD_INCIDENT_ID, "b": BAD_INCIDENT_ID},
    ).fetchall()
    actions = conn.execute(
        "SELECT INCIDENT_ID, OUTCOME, OUTCOME_DETAIL FROM REMEDIATION_ACTIONS ORDER BY ACTION_ID"
    ).fetchall()

print("INCIDENTS:")
for row in incidents:
    print(f"    #{row[0]}: {row[1]} (fix: {row[2]})")
print("REMEDIATION_ACTIONS (the audit trail):")
for row in actions:
    print(f"    incident #{row[0]}: {row[1]} -- {row[2]}")
