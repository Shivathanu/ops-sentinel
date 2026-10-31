"""One-time setup: creates (or resets) sentinel_engine and sentinel_executor Exasol users
per sql/roles.sql. mcp_readonly already exists on this instance, so it's skipped, and the
SALES_CLEAN grant is skipped since no business table exists yet in this local demo -- add
table-specific grants once you have real data to remediate.

Idempotent: ALTERs the password if the user already exists, and ignores "already granted"
errors, so it's safe to run more than once.

Run once:
    python scripts/bootstrap_sentinel_roles.py
"""
import os

import pyexasol
from dotenv import load_dotenv

load_dotenv()

ENGINE_PASSWORD = "EVBQMJoPcLZt2MtjJtIIdwEv"
EXECUTOR_PASSWORD = "Q15w2pk4EHmdTUe8tgOxmblM"

STATEMENTS = [
    f'CREATE USER sentinel_engine IDENTIFIED BY "{ENGINE_PASSWORD}"',
    "GRANT CREATE SESSION TO sentinel_engine",
    "GRANT SELECT ON TABLE SENTINEL.PIPELINE_LOGS TO sentinel_engine",
    "GRANT SELECT ON TABLE SENTINEL.QUERY_METRICS TO sentinel_engine",
    "GRANT SELECT ON TABLE SENTINEL.DATA_LINEAGE TO sentinel_engine",
    "GRANT SELECT, INSERT, UPDATE ON TABLE SENTINEL.INCIDENTS TO sentinel_engine",
    "GRANT SELECT, INSERT ON TABLE SENTINEL.REMEDIATION_ACTIONS TO sentinel_engine",
    f'CREATE USER sentinel_executor IDENTIFIED BY "{EXECUTOR_PASSWORD}"',
    "GRANT CREATE SESSION TO sentinel_executor",
]

RESET_STATEMENTS = [
    f'ALTER USER sentinel_engine IDENTIFIED BY "{ENGINE_PASSWORD}"',
    f'ALTER USER sentinel_executor IDENTIFIED BY "{EXECUTOR_PASSWORD}"',
]


def main() -> None:
    conn = pyexasol.connect(
        dsn=f"{os.environ['EXASOL_ADMIN_HOST']}:{os.environ['EXASOL_ADMIN_PORT']}",
        user=os.environ["EXASOL_ADMIN_USER"],
        password=os.environ["EXASOL_ADMIN_PASSWORD"],
        schema=os.environ["EXASOL_SCHEMA"],
        websocket_sslopt={"cert_reqs": 0},
    )
    try:
        for statement in STATEMENTS:
            label = statement.split("IDENTIFIED")[0].strip()
            try:
                conn.execute(statement)
                print("OK:", label)
            except pyexasol.exceptions.ExaQueryError as exc:
                if "conflicts with another user" in str(exc) or "already" in str(exc).lower():
                    print("SKIP (already exists):", label)
                else:
                    raise
        for statement in RESET_STATEMENTS:
            label = statement.split("IDENTIFIED")[0].strip()
            conn.execute(statement)
            print("OK:", label, "(password reset to the value this script uses)")
    finally:
        conn.close()
    print("\nAdd/confirm in .env:")
    print("EXASOL_ADMIN_USER=sentinel_engine")
    print(f"EXASOL_ADMIN_PASSWORD={ENGINE_PASSWORD}")
    print("EXASOL_EXECUTOR_USER=sentinel_executor")
    print(f"EXASOL_EXECUTOR_PASSWORD={EXECUTOR_PASSWORD}")


if __name__ == "__main__":
    main()
