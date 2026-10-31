from datetime import datetime

import pytest

from sentinel.core.models import Severity, Signal
from sentinel.store import migrate, udfs
from sentinel.store.exasol import is_transaction_collision, run_in_transaction
from sentinel.store.incidents import INCIDENT_OPENED, INCIDENT_SIGNAL, IncidentStore
from sentinel.store.queue import Job, JobQueue, backoff_seconds
from tests.fakes import Collision, FakeConnection, FakeStatement, factory

NOW = datetime(2026, 10, 31, 12, 0, 0)


# Migrations ---------------------------------------------------------------------------------------------------------

def test_statements_split_on_semicolons_outside_quotes_and_comments() -> None:
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE A (X VARCHAR(10) DEFAULT 'a;b');
    /* block; comment */
    INSERT INTO A VALUES ('it''s; fine');
    CREATE TABLE "B;C" (Y INT)
    """

    statements = migrate.split_statements(sql)

    assert len(statements) == 3
    assert statements[1] == "INSERT INTO A VALUES ('it''s; fine')"
    assert statements[2].startswith('CREATE TABLE "B;C"')


def test_bundled_migrations_are_ordered_and_parse() -> None:
    migrations = migrate.discover()

    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert all(m.statements for m in migrations)
    tables = " ".join(s for m in migrations for s in m.statements)
    for table in ("INCIDENTS", "SIGNALS", "JOBS", "AUDIT_EVENTS", "KNOWLEDGE_CHUNKS", "PROPOSED_ACTIONS"):
        assert f"CREATE TABLE IF NOT EXISTS {table} " in tables


def test_migrate_applies_pending_versions_once_each_and_installs_udfs() -> None:
    conn = FakeConnection()
    applied_list = FakeStatement(rows=[("0001", migrate.discover()[0].checksum)])
    schema_exists = FakeStatement(rows=[(1,)])
    conn.script(schema_exists, FakeStatement(), FakeStatement(), applied_list)
    migrations = migrate.discover()[:2]

    done = migrate.migrate(factory(conn), "OPS_SENTINEL", migrations)

    assert done == ["0002"]
    assert not conn.statements("CREATE SCHEMA")  # needs a privilege the schema owner lacks; skipped when it exists
    assert conn.params_of("INSERT INTO SCHEMA_MIGRATIONS")["version"] == "0002"
    assert len(conn.statements("CREATE OR REPLACE PYTHON3")) == len(udfs.UDFS)
    assert conn.commits == 1


def test_without_a_python_container_udfs_are_skipped_and_not_granted() -> None:
    class NoContainer(Exception):
        pass

    conn = FakeConnection().script(
        FakeStatement(),  # CREATE OR REPLACE ... FINGERPRINT succeeds
        NoContainer("No usable script language container is installed for the requested language."),
    )

    assert migrate.install_udfs_if_supported(factory(conn), "OPS_SENTINEL") == []
    assert conn.statements('DROP SCRIPT IF EXISTS "OPS_SENTINEL"."FINGERPRINT"')
    grants = migrate.runtime_grants("OPS_SENTINEL", "sentinel_engine", migrate.discover(), [])
    assert not any("EXECUTE" in g for g in grants)


def test_a_broken_udf_still_fails_the_migration() -> None:
    conn = FakeConnection().script(FakeStatement(), RuntimeError("syntax error in script"))

    with pytest.raises(RuntimeError, match="syntax error"):
        migrate.install_udfs_if_supported(factory(conn), "OPS_SENTINEL")


def test_an_edited_applied_migration_is_refused() -> None:
    conn = FakeConnection()
    conn.script(FakeStatement(rows=[(1,)]), FakeStatement(), FakeStatement(), FakeStatement(rows=[("0001", "0" * 64)]))

    with pytest.raises(migrate.MigrationError, match="edited after it was applied"):
        migrate.migrate(factory(conn), "OPS_SENTINEL", migrate.discover()[:1])


def test_runtime_grants_keep_the_audit_log_append_only_and_allow_no_ddl() -> None:
    grants = migrate.runtime_grants("OPS_SENTINEL", "sentinel_engine", migrate.discover())

    assert 'GRANT SELECT, INSERT ON "OPS_SENTINEL"."AUDIT_EVENTS" TO sentinel_engine' in grants
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON "OPS_SENTINEL"."INCIDENTS" TO sentinel_engine' in grants
    assert 'GRANT EXECUTE ON "OPS_SENTINEL"."VECTOR_COSINE" TO sentinel_engine' in grants
    assert not any("CREATE" in g or "ALTER" in g or "DROP" in g for g in grants)
    with pytest.raises(migrate.MigrationError):
        migrate.runtime_grants("OPS_SENTINEL", "x; DROP USER y", [])


def test_udf_bodies_are_the_tested_library_source() -> None:
    statement = next(u for u in udfs.UDFS if u.name == "VECTOR_COSINE").statement("OPS_SENTINEL")

    assert statement.startswith('CREATE OR REPLACE PYTHON3 SCALAR SCRIPT "OPS_SENTINEL"."VECTOR_COSINE"(')
    assert "def cosine(query_text, candidate_text):" in statement
    assert "def run(ctx):" in statement
    assert "import sentinel" not in statement


# Transactions -------------------------------------------------------------------------------------------------------

def test_a_collision_is_retried_and_other_errors_are_not() -> None:
    conn = FakeConnection()
    calls = []

    def work(c: FakeConnection) -> str:
        calls.append(1)
        if len(calls) < 3:
            raise Collision("GlobalTransactionRollback: transaction collision")
        return "ok"

    assert run_in_transaction(factory(conn), work, sleep=lambda _: None) == "ok"
    assert len(calls) == 3 and conn.rollbacks == 2 and conn.commits == 1 and conn.autocommit

    with pytest.raises(ValueError):
        run_in_transaction(factory(conn), lambda c: (_ for _ in ()).throw(ValueError("boom")), sleep=lambda _: None)


def test_collision_detection_uses_the_sqlstate_or_the_message() -> None:
    assert is_transaction_collision(Collision())
    assert is_transaction_collision(RuntimeError("Transaction collision: automatic transaction rollback"))
    assert not is_transaction_collision(RuntimeError("syntax error"))


# Queue --------------------------------------------------------------------------------------------------------------

def _queue(conn: FakeConnection) -> JobQueue:
    return JobQueue(factory(conn), now=lambda: NOW)


def test_claim_takes_the_oldest_ready_job_with_a_guarded_update() -> None:
    conn = FakeConnection().script(
        FakeStatement(rows=[("J1",)]),
        FakeStatement(rowcount=1),
        FakeStatement(rows=[("J1", "default", "incident.opened", "w", '{"incident_id": "I"}', 1, 5)]),
    )

    job = _queue(conn).claim("worker-a", ["default"], 300)

    assert job == Job("J1", "default", "incident.opened", "w", {"incident_id": "I"}, 1, 5)
    update = conn.statements("UPDATE JOBS SET STATUS = 'running'")[0]
    assert "STATUS = 'queued' AND RUN_AFTER <=" in update  # the same readiness check guards the update
    assert conn.params_of("UPDATE JOBS")["lease_until"] == "2026-10-31 12:05:00.000000"


def test_claim_returns_nothing_when_another_worker_won() -> None:
    conn = FakeConnection().script(FakeStatement(rows=[("J1",)]), FakeStatement(rowcount=0))

    assert _queue(conn).claim("worker-a", ["default"], 300) is None


def test_failure_retries_with_backoff_until_the_last_attempt_then_buries() -> None:
    conn = FakeConnection()
    queue = _queue(conn)

    assert queue.fail(Job("J", "default", "k", None, {}, 1, 3), "w", "boom") == "queued"
    assert queue.fail(Job("J", "default", "k", None, {}, 3, 3), "w", "boom") == "dead"
    retry, bury = (params for _, params in conn.executed)
    assert retry["run_after"] > "2026-10-31 12:00:00" and retry["finished_at"] is None
    assert bury["finished_at"] is not None


def test_backoff_grows_and_is_capped() -> None:
    assert backoff_seconds(1, jitter=lambda: 1.0) == 5.0
    assert backoff_seconds(3, jitter=lambda: 1.0) == 20.0
    assert backoff_seconds(30, jitter=lambda: 1.0) == 900.0
    assert backoff_seconds(1, jitter=lambda: 0.0) == 2.5


def test_idempotent_enqueue_reports_a_duplicate() -> None:
    conn = FakeConnection().script(FakeStatement(rowcount=0))

    assert _queue(conn).enqueue("k", {}, idempotency_key="x") is None
    assert "WHERE NOT EXISTS (SELECT 1 FROM JOBS WHERE IDEMPOTENCY_KEY = {key})" in conn.executed[0][0]


# Incidents ----------------------------------------------------------------------------------------------------------

def _signal(**overrides) -> Signal:
    return Signal(**{"source": "prometheus", "fingerprint": "f" * 32, "summary": "disk full", "severity": Severity.HIGH,
                     "labels": {"service": "etl"}, **overrides})


def test_a_new_fingerprint_opens_an_incident_and_enqueues_its_job_in_the_same_transaction() -> None:
    conn = FakeConnection().script(FakeStatement(rows=[]))  # no open incident with this fingerprint

    recorded = IncidentStore(factory(conn)).record_signal("team-a", "production", _signal())

    assert recorded.created and recorded.incident_id
    assert conn.params_of("INSERT INTO INCIDENTS")["service"] == "etl"
    assert conn.params_of("INSERT INTO JOBS")["kind"] == INCIDENT_OPENED
    assert conn.params_of("INSERT INTO SIGNALS")["incident_id"] == recorded.incident_id
    assert conn.commits == 1
    assert all("team-a" in str(params) for query, params in conn.executed if "WORKSPACE" in query)


def test_a_repeat_joins_the_open_incident_and_keeps_the_worst_severity() -> None:
    conn = FakeConnection().script(FakeStatement(rows=[("INC1", "critical", 3)]))

    recorded = IncidentStore(factory(conn)).record_signal("team-a", "production", _signal(severity=Severity.LOW))

    assert recorded == type(recorded)(recorded.signal_id, "INC1", False)
    assert conn.params_of("UPDATE INCIDENTS")["severity"] == "critical"
    assert conn.params_of("INSERT INTO JOBS")["kind"] == INCIDENT_SIGNAL
    assert not conn.statements("INSERT INTO INCIDENTS")


def test_a_recovery_for_an_unknown_incident_opens_nothing() -> None:
    conn = FakeConnection().script(FakeStatement(rows=[]))

    recorded = IncidentStore(factory(conn)).record_signal("team-a", "production", _signal(resolved=True))

    assert recorded.incident_id is None and not recorded.created
    assert not conn.statements("INSERT INTO INCIDENTS") and not conn.statements("INSERT INTO JOBS")
    assert conn.statements("INSERT INTO SIGNALS")
