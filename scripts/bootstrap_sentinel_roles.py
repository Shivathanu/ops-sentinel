"""One-time database setup, run once by a DBA. Creates the platform schema and the four Ops Sentinel users with
generated passwords, grants each the least it needs, and prints the .env lines to paste.

    EXASOL_DBA_USER=sys EXASOL_DBA_PASSWORD=... python scripts/bootstrap_sentinel_roles.py
    python scripts/bootstrap_sentinel_roles.py --reset-passwords     rotate the passwords of existing users

Users (see docs/design/ai-sre-platform.md, "Exasol as the system of record"):
    sentinel_owner     owns the platform schema; runs migrations and installs UDFs     EXASOL_MIGRATOR_*
    sentinel_engine    runtime: DML on platform tables, append-only audit             EXASOL_ADMIN_*
    sentinel_executor  runs approved remediation SQL on business tables only          EXASOL_EXECUTOR_*
    mcp_readonly       read-only investigation through the Exasol MCP server          EXA_USER / EXA_PASSWORD

Idempotent: existing users are kept (their passwords too, unless --reset-passwords), and existing grants are fine.
Passwords are generated per run and printed once; nothing is stored in this repository.
"""
import argparse
import os
import secrets
import sys

import pyexasol
from dotenv import load_dotenv

USERS = {
    "sentinel_owner": "EXASOL_MIGRATOR",
    "sentinel_engine": "EXASOL_ADMIN",
    "sentinel_executor": "EXASOL_EXECUTOR",
    "mcp_readonly": "EXA",
}


def password() -> str:
    # Letters and digits only: Exasol passwords are quoted identifiers, and this avoids every escaping question.
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(28))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset-passwords", action="store_true", help="also rotate passwords of users that exist")
    parser.add_argument("--platform-schema", default=os.environ.get("OPS_SENTINEL_PLATFORM_SCHEMA", "OPS_SENTINEL"))
    parser.add_argument("--data-schema", default=os.environ.get("EXASOL_SCHEMA", "SENTINEL"),
                        help="v1 schema with the pipeline tables the engine reads")
    args = parser.parse_args()
    load_dotenv()

    dba_user = os.environ.get("EXASOL_DBA_USER")
    if not dba_user:
        print("Set EXASOL_DBA_USER and EXASOL_DBA_PASSWORD (a DBA such as sys). They are used for this run only.")
        return 2
    conn = pyexasol.connect(
        dsn=f"{os.environ.get('EXASOL_ADMIN_HOST', '127.0.0.1')}:{os.environ.get('EXASOL_ADMIN_PORT', '8563')}",
        user=dba_user,
        password=os.environ.get("EXASOL_DBA_PASSWORD", ""),
        websocket_sslopt={"cert_reqs": 0} if os.environ.get("EXASOL_VALIDATE_CERTIFICATE", "true").lower() == "false" else None,
    )
    schema = args.platform_schema.upper()
    issued: dict[str, str] = {}
    try:
        existing = {row[0].lower() for row in conn.execute("SELECT USER_NAME FROM EXA_DBA_USERS").fetchall()}
        for user in USERS:
            if user not in existing:
                issued[user] = password()
                conn.execute(f'CREATE USER {user} IDENTIFIED BY "{issued[user]}"')
                print(f"created user {user}")
            elif args.reset_passwords:
                issued[user] = password()
                conn.execute(f'ALTER USER {user} IDENTIFIED BY "{issued[user]}"')
                print(f"rotated password of {user}")
            else:
                print(f"kept existing user {user}")
            conn.execute(f"GRANT CREATE SESSION TO {user}")

        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        conn.execute(f'ALTER SCHEMA "{schema}" CHANGE OWNER sentinel_owner')
        # Creating objects needs the system privilege even inside one's own schema.
        for privilege in ("CREATE TABLE", "CREATE VIEW", "CREATE SCRIPT"):
            conn.execute(f"GRANT {privilege} TO sentinel_owner")
        conn.execute("GRANT SELECT ANY TABLE TO mcp_readonly")

        data = args.data_schema.upper()
        tables = {row[0] for row in conn.execute(
            "SELECT TABLE_NAME FROM EXA_ALL_TABLES WHERE TABLE_SCHEMA = {s}", {"s": data}).fetchall()}
        for table, privileges in (("PIPELINE_LOGS", "SELECT"), ("QUERY_METRICS", "SELECT"), ("DATA_LINEAGE", "SELECT"),
                                  ("INCIDENTS", "SELECT, INSERT, UPDATE"), ("REMEDIATION_ACTIONS", "SELECT, INSERT")):
            if table in tables:
                conn.execute(f'GRANT {privileges} ON "{data}"."{table}" TO sentinel_engine')
    finally:
        conn.close()

    print(f"\nPlatform schema {schema} is owned by sentinel_owner. The engine creates its tables on first start.")
    print("Grant sentinel_executor DML on each business table it may repair, and only those (see sql/roles.sql).")
    if issued:
        print("\nAdd to .env (shown once):")
        for user, secret in issued.items():
            prefix = USERS[user]
            print(f"{prefix}_USER={user}")
            print(f"{prefix}_PASSWORD={secret}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
