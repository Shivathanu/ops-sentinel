"""
Module A helper — populate SENTINEL schema with synthetic data.

Run after db/schema.sql has been applied:
    python scripts/seed_data.py

Uses the ADMIN connection (pyexasol), same credentials as the engine's write
path, because seeding is itself a write operation and has nothing to do with
MCP.
"""
import os
import random
import ssl
from datetime import datetime, timedelta

import pyexasol
from dotenv import load_dotenv

load_dotenv()

HOST = os.getenv("EXASOL_ADMIN_HOST", "127.0.0.1")
PORT = os.getenv("EXASOL_ADMIN_PORT", "8563")
USER = os.getenv("EXASOL_ADMIN_USER", "sys")
PASSWORD = os.getenv("EXASOL_ADMIN_PASSWORD", "")
SCHEMA = os.getenv("EXASOL_SCHEMA", "SENTINEL")

PIPELINES = [
    ("pl_sales_raw_to_clean", "sales_raw", "sales_clean"),
    ("pl_sales_clean_to_finance_bi", "sales_clean", "finance_executive_bi"),
    ("pl_inventory_sync", "inventory_raw", "inventory_clean"),
    ("pl_marketing_attrib", "ad_events_raw", "marketing_attribution"),
]

USERS = ["alice.eng", "bob.analyst", "carol.bi", "dave.ds"]

ERROR_STACKS = [
    "ConnectionTimeoutError: source DB did not respond within 30s "
    "at extract_step(sales_raw.py:42)",
    "SchemaMismatchError: column 'region_code' not found in source, "
    "expected by transform_step(sales_clean.py:118)",
    "OutOfMemoryError: join operation exceeded 8GB working set "
    "at join_step(inventory_sync.py:77)",
    "DuplicateKeyError: primary key violation on ORDER_ID "
    "at load_step(sales_clean.py:200)",
]


def connect():
    return pyexasol.connect(
        dsn=f"{HOST}:{PORT}", user=USER, password=PASSWORD, schema=SCHEMA,
        websocket_sslopt={"cert_reqs": ssl.CERT_NONE},  # local self-signed cert
    )


def seed_lineage(conn):
    rows = [
        ("sales_raw", "sales_clean", "Finance Executive BI", "data-eng", "CRITICAL"),
        ("sales_clean", "finance_executive_bi", "Finance Executive BI", "data-eng", "CRITICAL"),
        ("inventory_raw", "inventory_clean", "Ops Inventory Dashboard", "ops-eng", "HIGH"),
        ("ad_events_raw", "marketing_attribution", "Marketing ROI Dashboard", "growth", "MEDIUM"),
    ]
    conn.execute("DELETE FROM DATA_LINEAGE")
    for source, target, dashboard, owner, criticality in rows:
        # pyexasol binds params by name ({source}, ...), not positional ? markers.
        conn.execute(
            "INSERT INTO DATA_LINEAGE "
            "(SOURCE_TABLE, TARGET_TABLE, DOWNSTREAM_DASHBOARD, OWNER_TEAM, CRITICALITY) "
            "VALUES ({source}, {target}, {dashboard}, {owner}, {criticality})",
            {"source": source, "target": target, "dashboard": dashboard, "owner": owner, "criticality": criticality},
        )


def seed_pipeline_logs(conn, n=40):
    conn.execute("DELETE FROM PIPELINE_LOGS")
    now = datetime.utcnow()
    for i in range(n):
        pipeline_id, source, target = random.choice(PIPELINES)
        ts = now - timedelta(minutes=random.randint(0, 60 * 24))
        failed = random.random() < 0.15
        status = "FAILED" if failed else "SUCCESS"
        error = random.choice(ERROR_STACKS) if failed else None
        conn.execute(
            "INSERT INTO PIPELINE_LOGS "
            "(PIPELINE_ID, PIPELINE_NAME, SOURCE_TABLE, TARGET_TABLE, EXECUTION_TS, "
            " STATUS, DURATION_SECONDS, ROWS_PROCESSED, ERROR_STACK) "
            "VALUES ({pipeline_id}, {pipeline_name}, {source}, {target}, {ts}, "
            " {status}, {duration}, {rows_processed}, {error})",
            {
                "pipeline_id": pipeline_id,
                "pipeline_name": pipeline_id.replace("pl_", "").replace("_", " ").title(),
                "source": source,
                "target": target,
                "ts": ts,
                "status": status,
                "duration": round(random.uniform(5, 300), 2),
                "rows_processed": random.randint(1000, 500000),
                "error": error,
            },
        )


def seed_query_metrics(conn, n=80):
    conn.execute("DELETE FROM QUERY_METRICS")
    now = datetime.utcnow()
    tables = ["sales_clean", "inventory_clean", "marketing_attribution", "finance_executive_bi"]
    for i in range(n):
        ts = now - timedelta(minutes=random.randint(0, 60 * 24))
        expensive = random.random() < 0.1
        compute_ms = random.uniform(5000, 60000) if expensive else random.uniform(50, 4000)
        cost = round(compute_ms / 1000 * 0.02, 4)  # toy cost model
        conn.execute(
            "INSERT INTO QUERY_METRICS "
            "(USER_ID, TABLE_NAME, QUERY_TEXT, EXECUTED_TS, COMPUTE_TIME_MS, "
            " CPU_SECONDS, ROWS_SCANNED, ESTIMATED_COST_USD, IS_DUPLICATE) "
            "VALUES ({user_id}, {table_name}, {query_text}, {ts}, {compute_ms}, "
            " {cpu_seconds}, {rows_scanned}, {cost}, {is_duplicate})",
            {
                "user_id": random.choice(USERS),
                "table_name": random.choice(tables),
                "query_text": f"SELECT * FROM {random.choice(tables)} WHERE updated_at > CURRENT_DATE - 30",
                "ts": ts,
                "compute_ms": round(compute_ms, 2),
                "cpu_seconds": round(compute_ms / 1000, 2),
                "rows_scanned": random.randint(10000, 5000000),
                "cost": cost,
                "is_duplicate": random.random() < 0.08,
            },
        )


if __name__ == "__main__":
    c = connect()
    seed_lineage(c)
    seed_pipeline_logs(c)
    seed_query_metrics(c)
    print("Seeded SENTINEL schema with synthetic lineage, pipeline logs, and query metrics.")
