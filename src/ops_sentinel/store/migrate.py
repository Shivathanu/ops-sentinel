"""Applies the platform schema migrations and installs the UDFs. Safe to run on every start.

    sentinel-migrate            apply pending migrations, then (re)install UDFs
    sentinel-migrate --status   list applied and pending migrations

Migrations are the numbered `.sql` files next to this module. Each is applied once, recorded with a checksum in
SCHEMA_MIGRATIONS, and never edited afterwards: an edited applied migration is reported as an error rather than
silently diverging between environments. Concurrent runs (several replicas starting together) are safe: the version
row is inserted in the same transaction as the migration, so a collision rolls one runner back and it re-checks.
"""
import argparse
import hashlib
import logging
import re
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from ops_sentinel.config import settings
from ops_sentinel.core.models import utcnow
from ops_sentinel.db import ExasolConnectionPool, ExasolCredentials
from ops_sentinel.store import udfs
from ops_sentinel.store.exasol import Connection, run_in_transaction, ts

logger = logging.getLogger("ops_sentinel.migrate")

MIGRATIONS_DIR = Path(__file__).with_name("migrations")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()

    @property
    def statements(self) -> list[str]:
        return split_statements(self.sql)


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        version, _, name = path.stem.partition("_")
        if not version.isdigit():
            raise MigrationError(f"migration file {path.name} must start with a number, e.g. 0004_add_x.sql")
        migrations.append(Migration(version, name, path.read_text(encoding="utf-8")))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration versions in {directory}")
    return migrations


def split_statements(sql: str) -> list[str]:
    """Splits on semicolons outside string literals, quoted identifiers and comments; drops comment-only parts."""
    statements: list[str] = []
    current: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            end = sql.find("\n", i)
            i = n if end == -1 else end + 1
            current.append("\n")
            continue
        if ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            current.append(" ")
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # doubled quote is an escaped quote
                        j += 2
                        continue
                    break
                j += 1
            current.append(sql[i : j + 1])
            i = j + 1
            continue
        if ch == ";":
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    statements.append("".join(current))
    return [s.strip() for s in statements if s.strip()]


BOOKKEEPING = """
CREATE TABLE IF NOT EXISTS SCHEMA_MIGRATIONS (
    VERSION_NO  VARCHAR(20)  NOT NULL PRIMARY KEY,
    NAME        VARCHAR(200) NOT NULL,
    CHECKSUM    VARCHAR(64)  NOT NULL,
    APPLIED_AT  TIMESTAMP    NOT NULL
)
"""


def applied_versions(conn: Connection) -> dict[str, str]:
    rows = conn.execute("SELECT VERSION_NO, CHECKSUM FROM SCHEMA_MIGRATIONS").fetchall()
    return {str(version): str(checksum) for version, checksum in rows}


def migrate(
    connect: Callable[[], AbstractContextManager[Connection]],
    schema: str,
    migrations: list[Migration] | None = None,
    *,
    install_udfs: bool = True,
    runtime_user: str | None = None,
) -> list[str]:
    """Applies pending migrations in order. Returns the versions this call applied.

    With `runtime_user` (a separate schema owner runs migrations), the runtime user's grants are reconciled too."""
    migrations = discover() if migrations is None else migrations
    with connect() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS {schema!q}", {"schema": schema})
        conn.execute("OPEN SCHEMA {schema!q}", {"schema": schema})
        conn.execute(BOOKKEEPING)
        applied = applied_versions(conn)

    for migration in migrations:
        known = applied.get(migration.version)
        if known is not None and known != migration.checksum:
            raise MigrationError(
                f"migration {migration.version}_{migration.name} was edited after it was applied; "
                "add a new migration instead of changing an applied one"
            )

    done: list[str] = []
    for migration in migrations:
        if migration.version in applied:
            continue

        def apply(conn: Connection, migration: Migration = migration) -> bool:
            conn.execute("OPEN SCHEMA {schema!q}", {"schema": schema})
            # Re-checked inside the transaction: another runner may have applied it since we looked.
            already = conn.execute(
                "SELECT COUNT(*) FROM SCHEMA_MIGRATIONS WHERE VERSION_NO = {version}", {"version": migration.version}
            ).fetchone()
            if already and already[0]:
                return False
            for statement in migration.statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO SCHEMA_MIGRATIONS (VERSION_NO, NAME, CHECKSUM, APPLIED_AT) "
                "VALUES ({version}, {name}, {checksum}, {applied_at})",
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                    "applied_at": ts(utcnow()),
                },
            )
            return True

        if run_in_transaction(connect, apply):
            logger.info("applied migration %s_%s", migration.version, migration.name)
            done.append(migration.version)

    if install_udfs:
        with connect() as conn:
            for statement in udfs.install_statements(schema):
                conn.execute(statement)
        logger.info("installed %d UDF scripts in %s", len(udfs.UDFS), schema)
    if runtime_user:
        with connect() as conn:
            for statement in runtime_grants(schema, runtime_user, migrations):
                conn.execute(statement)
        logger.info("granted runtime privileges in %s to %s", schema, runtime_user)
    return done


TABLE_NAME = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Z_][A-Z0-9_]*)", re.IGNORECASE)
APPEND_ONLY = frozenset({"AUDIT_EVENTS"})


def runtime_grants(schema: str, user: str, migrations: list[Migration]) -> list[str]:
    """Least privilege for the runtime user, reconciled on every run (GRANT is idempotent in Exasol): DML on platform
    tables, only SELECT and INSERT on the audit log so history cannot be rewritten, nothing on SCHEMA_MIGRATIONS
    beyond reading it, and EXECUTE on the UDFs. The runtime user can never create, alter or drop anything."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", user):
        raise MigrationError(f"runtime user name {user!r} is not a plain identifier")
    tables = sorted({name.upper() for m in migrations for name in TABLE_NAME.findall(m.sql)})
    statements = [f'GRANT SELECT ON "{schema}"."SCHEMA_MIGRATIONS" TO {user}']
    for table in tables:
        privileges = "SELECT, INSERT" if table in APPEND_ONLY else "SELECT, INSERT, UPDATE, DELETE"
        statements.append(f'GRANT {privileges} ON "{schema}"."{table}" TO {user}')
    statements += [f'GRANT EXECUTE ON "{schema}"."{udf.name}" TO {user}' for udf in udfs.UDFS]
    return statements


_migrator_pool: ExasolConnectionPool | None = None


def migrator_connection() -> AbstractContextManager[Connection]:
    """The schema owner if configured, otherwise the runtime engine user. Opens no schema: it may not exist yet."""
    global _migrator_pool
    if _migrator_pool is None:
        if settings.exasol_migrator_user:
            credentials = ExasolCredentials(
                settings.exasol_migrator_user, settings.exasol_migrator_password, "EXASOL_MIGRATOR_USER"
            )
        else:
            credentials = ExasolCredentials(
                settings.exasol_admin_user, settings.exasol_admin_password, "EXASOL_ADMIN_USER"
            )
        _migrator_pool = ExasolConnectionPool(max_size=1, credentials=credentials, schema="")
    return _migrator_pool.connection()


def configured_runtime_user() -> str | None:
    """The user to grant to: only when a separate owner migrates, since an owner already has every privilege."""
    if settings.exasol_migrator_user and settings.exasol_admin_user != settings.exasol_migrator_user:
        return settings.exasol_admin_user or None
    return None


def migrate_from_settings(schema: str | None = None) -> list[str]:
    return migrate(migrator_connection, schema or settings.platform_schema, runtime_user=configured_runtime_user())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-migrate", description=__doc__.splitlines()[0])
    parser.add_argument("--status", action="store_true", help="show applied and pending migrations and exit")
    parser.add_argument("--schema", default=settings.platform_schema, help="platform schema (default: %(default)s)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        if args.status:
            with migrator_connection() as conn:
                conn.execute("OPEN SCHEMA {schema!q}", {"schema": args.schema})
                applied = applied_versions(conn)
            for migration in discover():
                state = "applied" if migration.version in applied else "pending"
                print(f"{migration.version}  {migration.name:<30} {state}")
            return 0
        done = migrate_from_settings(args.schema)
    except Exception as exc:  # noqa: BLE001 - a CLI reports and exits non-zero
        logger.error("migration failed: %s", exc)
        return 1
    print(f"schema {args.schema}: {len(done)} migration(s) applied, UDFs installed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
