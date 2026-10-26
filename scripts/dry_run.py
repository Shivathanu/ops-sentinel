"""
Executes the Ops Sentinel 6-step closed loop end to end, right now, without
requiring a live Exasol instance, Slack app, or LLM key — so you can see the
real control flow working before installing any infrastructure.

What's REAL vs. SIMULATED in this run, and why:

  Database (Exasol)        SIMULATED -> an in-memory SQLite DB stands in,
                            with the same tables and the same detection
                            query shape as ops_sentinel/watcher.py, because Exasol
                            Personal isn't installed in this environment yet.

  MCP read path             SIMULATED -> prints what ops_sentinel/mcp_client.py
                            would ask for and reads it straight from SQLite,
                            because the real `exasol` MCP server needs
                            `exakit` installed and running.

  LLM triage                 REAL if OPENAI_API_KEY or ANTHROPIC_API_KEY is
                            set in .env (calls ops_sentinel.triage_agent.triage()
                            for real); otherwise a clearly labeled canned
                            response so the flow still completes.

  Slack notification         REAL if SLACK_BOT_TOKEN + SLACK_CHANNEL_ID are
                            set in .env (calls ops_sentinel.slack_notifier for
                            real, posts an actual message); otherwise the
                            Block Kit card is rendered as text here instead.

  Remediation guardrail       REAL, always -> imports validate_fix_sql
                            straight from ops_sentinel/guardrail.py, so this run
                            proves the safety fix actually blocks unsafe SQL,
                            not a reimplementation of it.

Run: .venv\\Scripts\\python.exe scripts\\dry_run.py
"""
import sys

sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a codepage that mangles em dashes etc.

import sqlite3
import textwrap
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from ops_sentinel.config import settings  # noqa: E402
from ops_sentinel.guardrail import validate_fix_sql  # noqa: E402 — the real guardrail, reused as-is

SEP = "-" * 78


def banner(step: str, title: str) -> None:
    print(f"\n{SEP}\n{step}  {title}\n{SEP}")


def build_demo_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE PIPELINE_LOGS (
            LOG_ID INTEGER PRIMARY KEY, PIPELINE_ID TEXT, PIPELINE_NAME TEXT,
            SOURCE_TABLE TEXT, TARGET_TABLE TEXT, EXECUTION_TS TEXT,
            STATUS TEXT, ERROR_STACK TEXT
        );
        CREATE TABLE DATA_LINEAGE (
            LINEAGE_ID INTEGER PRIMARY KEY, SOURCE_TABLE TEXT, TARGET_TABLE TEXT,
            DOWNSTREAM_DASHBOARD TEXT, OWNER_TEAM TEXT, CRITICALITY TEXT
        );
        CREATE TABLE INCIDENTS (
            INCIDENT_ID INTEGER PRIMARY KEY, SOURCE_TYPE TEXT, SOURCE_REF_ID INTEGER,
            CREATED_TS TEXT, STATUS TEXT, BUSINESS_IMPACT TEXT, ROOT_CAUSE TEXT,
            SUGGESTED_FIX_SQL TEXT, RISK_LEVEL TEXT, RESOLVED_TS TEXT
        );
        CREATE TABLE REMEDIATION_ACTIONS (
            ACTION_ID INTEGER PRIMARY KEY, INCIDENT_ID INTEGER, EXECUTED_SQL TEXT,
            EXECUTED_BY TEXT, EXECUTED_TS TEXT, OUTCOME TEXT, OUTCOME_DETAIL TEXT
        );
        -- The actual downstream table the suggested fix SQL targets, so
        -- approving the incident has something real to act on.
        CREATE TABLE sales_clean (
            order_id INTEGER PRIMARY KEY, region_code TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO sales_clean (order_id, region_code) VALUES (?, ?)",
        [(1, None), (2, "US"), (3, None)],
    )
    conn.execute(
        "INSERT INTO DATA_LINEAGE (SOURCE_TABLE, TARGET_TABLE, DOWNSTREAM_DASHBOARD, OWNER_TEAM, CRITICALITY) "
        "VALUES ('sales_raw', 'sales_clean', 'Finance Executive BI', 'data-eng', 'CRITICAL')"
    )
    conn.execute(
        "INSERT INTO DATA_LINEAGE (SOURCE_TABLE, TARGET_TABLE, DOWNSTREAM_DASHBOARD, OWNER_TEAM, CRITICALITY) "
        "VALUES ('sales_clean', 'finance_executive_bi', 'Finance Executive BI', 'data-eng', 'CRITICAL')"
    )
    conn.execute(
        "INSERT INTO PIPELINE_LOGS (PIPELINE_ID, PIPELINE_NAME, SOURCE_TABLE, TARGET_TABLE, EXECUTION_TS, STATUS, ERROR_STACK) "
        "VALUES ('pl_sales_clean_to_finance_bi', 'Sales Clean To Finance Bi', 'sales_clean', 'finance_executive_bi', ?, 'FAILED', ?)",
        (
            datetime.now(timezone.utc).isoformat(),
            "SchemaMismatchError: column 'region_code' not found in source, "
            "expected by transform_step(sales_clean.py:118)",
        ),
    )
    conn.commit()
    return conn


def step_1_2_detect(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    banner("STEP 1-2", "Detect: scan PIPELINE_LOGS for new FAILED runs")
    rows = conn.execute(
        "SELECT LOG_ID, PIPELINE_ID, PIPELINE_NAME, SOURCE_TABLE, TARGET_TABLE, EXECUTION_TS, STATUS, ERROR_STACK "
        "FROM PIPELINE_LOGS WHERE STATUS = 'FAILED'"
    ).fetchall()
    for r in rows:
        print(f"  found failure: LOG_ID={r['LOG_ID']} pipeline={r['PIPELINE_ID']} target={r['TARGET_TABLE']}")
    return rows


def step_3_investigate(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict, list[dict]]:
    banner("STEP 3", "Investigate via the MCP read path (simulated — exakit not installed here)")
    error_context = dict(row)
    print(f"  [MCP] get_error_context(log_id={row['LOG_ID']}) -> {error_context['ERROR_STACK'][:70]}...")
    lineage_rows = conn.execute(
        "SELECT SOURCE_TABLE, TARGET_TABLE, DOWNSTREAM_DASHBOARD, OWNER_TEAM, CRITICALITY "
        "FROM DATA_LINEAGE WHERE SOURCE_TABLE = ? OR TARGET_TABLE = ?",
        (row["TARGET_TABLE"], row["TARGET_TABLE"]),
    ).fetchall()
    lineage = [dict(r) for r in lineage_rows]
    print(f"  [MCP] get_lineage_for_table('{row['TARGET_TABLE']}') -> {len(lineage)} downstream row(s)")
    for link in lineage:
        print(f"      {link['SOURCE_TABLE']} -> {link['TARGET_TABLE']} -> {link['DOWNSTREAM_DASHBOARD']} ({link['CRITICALITY']})")
    return error_context, lineage


def step_4_triage(error_context: dict, lineage: list[dict]) -> dict:
    banner("STEP 4", "Triage: LLM generates business impact + a suggested fix")
    has_llm_key = bool(settings.openai_api_key or settings.anthropic_api_key)
    if has_llm_key:
        print("  LLM key found in .env -> calling the REAL ops_sentinel.triage_agent.triage()")
        from ops_sentinel.triage_agent import triage
        result = triage(error_context, lineage)
        print("  (the above was a real LLM API call)")
    else:
        print("  No LLM key in .env -> using a canned response so the flow still completes")
        result = {
            "business_impact": (
                "The Finance Executive BI dashboard is showing stale sales figures "
                "because the upstream sales_clean transform is failing."
            ),
            "root_cause": "The transform expects a 'region_code' column missing from the latest sales_raw extract.",
            "suggested_fix_sql": "UPDATE sales_clean SET region_code = 'UNKNOWN' WHERE region_code IS NULL",
            "risk_level": "MEDIUM",
        }
    for key, value in result.items():
        print(f"    {key}: {value}")
    return result


def step_5a_notify(incident_id: int, triage_result: dict) -> None:
    banner("STEP 5 (part 1)", "Post the interactive Approve / Reject card")
    has_slack = bool(settings.slack_bot_token and settings.slack_channel_id)
    if has_slack:
        print("  Slack credentials found in .env -> posting a REAL card")
        from ops_sentinel.slack_notifier import post_incident_card
        post_incident_card(
            incident_id=incident_id,
            source_type="PIPELINE_FAILURE",
            business_impact=triage_result["business_impact"],
            root_cause=triage_result["root_cause"],
            suggested_fix_sql=triage_result["suggested_fix_sql"],
            risk_level=triage_result["risk_level"],
        )
    else:
        print("  No Slack credentials in .env -> rendering the card as text instead of posting it")
        print(textwrap.dedent(f"""
            +---------------------------------------------------------------+
            | Incident #{incident_id} -- PIPELINE_FAILURE
            | Business impact: {triage_result['business_impact']}
            | Root cause:      {triage_result['root_cause']}
            | Suggested fix:   {triage_result['suggested_fix_sql']}
            | Risk level:      {triage_result['risk_level']}
            | [ Approve & Remediate ]   [ Reject ]
            +---------------------------------------------------------------+
        """))


def step_5b_safety_check(bad_examples: list[str]) -> None:
    banner("STEP 5 (safety check)", "What if the LLM had suggested something dangerous? (real guardrail, real calls)")
    for bad_sql in bad_examples:
        verdict = validate_fix_sql(bad_sql)
        print(f"  '{bad_sql}'")
        print(f"      -> safe={verdict.allowed}  reason={verdict.reason}")


def step_5c_approve(conn: sqlite3.Connection, incident_id: int, fix_sql: str, approved_by: str = "demo.user") -> None:
    banner("STEP 5 (part 2)", "Engineer clicks Approve -> real validation, then remediate")
    verdict = validate_fix_sql(fix_sql)
    print(f"  ops_sentinel.guardrail.validate_fix_sql(...) -> safe={verdict.allowed}, detail={verdict.statement or verdict.reason!r}")

    if not verdict.allowed:
        conn.execute("UPDATE INCIDENTS SET STATUS = 'FAILED' WHERE INCIDENT_ID = ?", (incident_id,))
        conn.execute(
            "INSERT INTO REMEDIATION_ACTIONS (INCIDENT_ID, EXECUTED_SQL, EXECUTED_BY, EXECUTED_TS, OUTCOME, OUTCOME_DETAIL) "
            "VALUES (?, ?, ?, ?, 'BLOCKED', ?)",
            (incident_id, fix_sql, approved_by, datetime.now(timezone.utc).isoformat(), verdict.reason),
        )
        conn.commit()
        return

    safe_sql = verdict.statement
    before = conn.execute("SELECT COUNT(*) AS n FROM sales_clean WHERE region_code IS NULL").fetchone()["n"]
    conn.execute(safe_sql)  # the SQLite stand-in for ops_sentinel/remediation.py's Exasol admin write path
    after = conn.execute("SELECT COUNT(*) AS n FROM sales_clean WHERE region_code IS NULL").fetchone()["n"]
    print(f"  sales_clean rows with NULL region_code: {before} -> {after}")
    conn.execute(
        "UPDATE INCIDENTS SET STATUS = 'REMEDIATED', RESOLVED_TS = ? WHERE INCIDENT_ID = ?",
        (datetime.now(timezone.utc).isoformat(), incident_id),
    )
    conn.execute(
        "INSERT INTO REMEDIATION_ACTIONS (INCIDENT_ID, EXECUTED_SQL, EXECUTED_BY, EXECUTED_TS, OUTCOME, OUTCOME_DETAIL) "
        "VALUES (?, ?, ?, ?, 'SUCCESS', 'Remediation applied')",
        (incident_id, safe_sql, approved_by, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    print(f"  Remediation executed: {safe_sql}")


def step_6_refresh(conn: sqlite3.Connection, incident_id: int) -> None:
    banner("STEP 6", "Destination status refreshed")
    incident = conn.execute(
        "SELECT STATUS, RESOLVED_TS FROM INCIDENTS WHERE INCIDENT_ID = ?", (incident_id,)
    ).fetchone()
    print(f"  Incident #{incident_id} is now: {incident['STATUS']} (resolved at {incident['RESOLVED_TS']})")

    total = conn.execute("SELECT COUNT(*) AS n FROM PIPELINE_LOGS").fetchone()["n"]
    failed = conn.execute("SELECT COUNT(*) AS n FROM PIPELINE_LOGS WHERE STATUS = 'FAILED'").fetchone()["n"]
    trust_score = round((total - failed) / total * 100, 1) if total else 100.0
    print(f"  Platform Trust Score (same formula as ops_sentinel.scoring.compute_trust_score): {trust_score}%")

    print("  Remediation audit trail (REMEDIATION_ACTIONS):")
    for action in conn.execute("SELECT OUTCOME, COUNT(*) AS n FROM REMEDIATION_ACTIONS GROUP BY OUTCOME"):
        print(f"    {action['OUTCOME']}: {action['n']}")


def main() -> None:
    print("Ops Sentinel -- dry run of the 6-step closed loop")
    print("(no live Exasol / Slack / LLM required; swaps in real credentials automatically if .env has them)")

    conn = build_demo_db()

    failures = step_1_2_detect(conn)
    if not failures:
        print("No failures found — nothing to do.")
        return
    failure = failures[0]

    conn.execute(
        "INSERT INTO INCIDENTS (SOURCE_TYPE, SOURCE_REF_ID, CREATED_TS, STATUS) VALUES ('PIPELINE_FAILURE', ?, ?, 'DETECTED')",
        (failure["LOG_ID"], datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    incident_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    print(f"  created INCIDENTS row #{incident_id}")

    error_context, lineage = step_3_investigate(conn, failure)
    triage_result = step_4_triage(error_context, lineage)

    conn.execute(
        "UPDATE INCIDENTS SET STATUS='AWAITING_APPROVAL', BUSINESS_IMPACT=?, ROOT_CAUSE=?, SUGGESTED_FIX_SQL=?, RISK_LEVEL=? WHERE INCIDENT_ID=?",
        (
            triage_result["business_impact"],
            triage_result["root_cause"],
            triage_result["suggested_fix_sql"],
            triage_result["risk_level"],
            incident_id,
        ),
    )
    conn.commit()

    step_5a_notify(incident_id, triage_result)
    step_5b_safety_check([
        "DELETE FROM sales_clean",
        "DROP TABLE sales_clean",
        "UPDATE sales_clean SET region_code = 'X'; DELETE FROM sales_clean",
    ])
    step_5c_approve(conn, incident_id, triage_result["suggested_fix_sql"])
    step_6_refresh(conn, incident_id)

    print(f"\n{SEP}\nDone. The safety guardrail above is the real ops_sentinel/guardrail.py code, not a reimplementation.\n{SEP}")


if __name__ == "__main__":
    main()
