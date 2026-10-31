"""
Drops one fresh FAILED pipeline row and one cost-spike query row into Exasol
so you can demo the full detect -> triage -> Slack -> approve -> remediate
loop without waiting for the random seed data to happen to contain one.

Run: python scripts/inject_demo_incident.py

Note: pyexasol binds query parameters by name (`{param}` placeholders + a
dict), not positional `?` placeholders.
"""
import os
import ssl
from datetime import datetime, timezone

import pyexasol
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("EXASOL_ADMIN_HOST", "127.0.0.1")
PORT = os.getenv("EXASOL_ADMIN_PORT", "8563")
USER = os.getenv("EXASOL_ADMIN_USER", "sys")
PASSWORD = os.getenv("EXASOL_ADMIN_PASSWORD", "")
SCHEMA = os.getenv("EXASOL_SCHEMA", "SENTINEL")


def utc_now() -> datetime:
    # Exasol TIMESTAMP has no time zone, so the offset must be stripped after reading UTC.
    return datetime.now(timezone.utc).replace(tzinfo=None)

conn = pyexasol.connect(
    dsn=f"{HOST}:{PORT}", user=USER, password=PASSWORD, schema=SCHEMA,
    websocket_sslopt={"cert_reqs": ssl.CERT_NONE},  # local self-signed cert
)

conn.execute(
    "INSERT INTO PIPELINE_LOGS "
    "(PIPELINE_ID, PIPELINE_NAME, SOURCE_TABLE, TARGET_TABLE, EXECUTION_TS, "
    " STATUS, DURATION_SECONDS, ROWS_PROCESSED, ERROR_STACK) "
    "VALUES ({pipeline_id}, {pipeline_name}, {source}, {target}, {ts}, "
    " {status}, {duration}, {rows_processed}, {error})",
    {
        "pipeline_id": "pl_sales_clean_to_finance_bi",
        "pipeline_name": "Sales Clean To Finance Bi",
        "source": "sales_clean",
        "target": "finance_executive_bi",
        "ts": utc_now(),
        "status": "FAILED",
        "duration": 12.4,
        "rows_processed": 0,
        "error": (
            "SchemaMismatchError: column 'region_code' not found in source, "
            "expected by transform_step(sales_clean.py:118)"
        ),
    },
)

conn.execute(
    "INSERT INTO QUERY_METRICS "
    "(USER_ID, TABLE_NAME, QUERY_TEXT, EXECUTED_TS, COMPUTE_TIME_MS, "
    " CPU_SECONDS, ROWS_SCANNED, ESTIMATED_COST_USD, IS_DUPLICATE) "
    "VALUES ({user_id}, {table_name}, {query_text}, {ts}, {compute_ms}, "
    " {cpu_seconds}, {rows_scanned}, {cost}, {is_duplicate})",
    {
        "user_id": "bob.analyst",
        "table_name": "finance_executive_bi",
        "query_text": (
            "SELECT * FROM finance_executive_bi f JOIN sales_clean s "
            "ON f.order_id = s.order_id"
        ),
        "ts": utc_now(),
        "compute_ms": 95000.0,
        "cpu_seconds": 95.0,
        "rows_scanned": 48000000,
        "cost": 9.75,
        "is_duplicate": True,
    },
)

print("Injected 1 pipeline failure + 1 cost-spike query. Watch the engine logs.")
