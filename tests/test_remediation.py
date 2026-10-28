"""Orchestration of an approval, run against scripted fake connections so the claim, scoping, executor split
and audit behaviour are pinned without a database."""
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from ops_sentinel import remediation
from ops_sentinel.db import DatabaseConfigurationError

SCOPED_FIX = "UPDATE sales_clean SET region_code = 'UNKNOWN' WHERE region_code IS NULL"


class FakeResult:
    def __init__(self, rows: list[tuple] | None = None, rowcount: int = 1) -> None:
        self._rows = rows or []
        self._rowcount = rowcount

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def rowcount(self) -> int:
        return self._rowcount


class FakeConnection:
    """Returns the first scripted result whose needle appears in the SQL, and records every statement."""

    def __init__(self, routes: list[tuple[str, FakeResult]]) -> None:
        self._routes = routes
        self.executed: list[tuple[str, dict | None]] = []

    def execute(self, sql: str, params: dict | None = None) -> FakeResult:
        self.executed.append((sql, params))
        for needle, result in self._routes:
            if needle in sql:
                return result
        return FakeResult()

    def audit_rows(self) -> list[dict]:
        return [params for sql, params in self.executed if sql.startswith("INSERT INTO REMEDIATION_ACTIONS")]

    def final_statuses(self) -> list[str]:
        return [
            params["status"] for sql, params in self.executed if sql.startswith("UPDATE INCIDENTS SET STATUS = {status}")
        ]


class FakeExecutor:
    def __init__(self, error: Exception | None = None) -> None:
        self.statements: list[str] = []
        self._error = error

    def execute(self, statement: str) -> None:
        if self._error:
            raise self._error
        self.statements.append(statement)


def _engine(
    fix_sql: str | None,
    *,
    claimed: bool = True,
    pipeline_row: tuple | None = ("sales_raw", "sales_clean"),
    incident_source: str = "PIPELINE_FAILURE",
) -> FakeConnection:
    return FakeConnection([
        ("SET STATUS = 'APPROVED'", FakeResult(rowcount=1 if claimed else 0)),
        ("SELECT SUGGESTED_FIX_SQL", FakeResult([(fix_sql, incident_source)])),
        ("SELECT SOURCE_TYPE", FakeResult([("PIPELINE_FAILURE", 7)])),
        ("FROM PIPELINE_LOGS", FakeResult([pipeline_row] if pipeline_row else [])),
        ("FROM DATA_LINEAGE", FakeResult([("sales_clean", "finance_executive_bi")])),
    ])


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    def _wire(engine: FakeConnection, executor: FakeExecutor | Exception) -> FakeExecutor | None:
        @contextmanager
        def engine_connection() -> Iterator[FakeConnection]:
            yield engine

        @contextmanager
        def executor_connection() -> Iterator[FakeExecutor]:
            if isinstance(executor, Exception):
                raise executor
            yield executor

        monkeypatch.setattr(remediation, "get_connection", engine_connection)
        monkeypatch.setattr(remediation, "get_executor_connection", executor_connection)
        return None if isinstance(executor, Exception) else executor

    return _wire


def test_approved_in_scope_fix_runs_on_the_executor_and_is_audited(wire) -> None:
    engine, executor = _engine(SCOPED_FIX), FakeExecutor()
    wire(engine, executor)

    success, detail = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert success, detail
    assert executor.statements == [SCOPED_FIX]
    assert engine.final_statuses() == ["REMEDIATED"]
    (audit,) = engine.audit_rows()
    assert (audit["outcome"], audit["executed_by"], audit["executed_sql"]) == ("SUCCESS", "alice (U1)", SCOPED_FIX)


def test_a_lineage_neighbour_of_the_failing_table_is_in_scope(wire) -> None:
    fix = "UPDATE finance_executive_bi SET total = 0 WHERE total IS NULL"
    engine, executor = _engine(fix), FakeExecutor()
    wire(engine, executor)

    success, _ = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert success
    assert executor.statements == [fix]


def test_a_duplicate_approval_is_ignored_without_touching_anything(wire) -> None:
    engine, executor = _engine(SCOPED_FIX, claimed=False), FakeExecutor()
    wire(engine, executor)

    success, detail = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert not success
    assert "not awaiting approval" in detail
    assert executor.statements == []
    assert engine.audit_rows() == []


@pytest.mark.parametrize(
    ("fix_sql", "reason"),
    [
        ("DROP TABLE sales_clean", "'DROP'"),
        ("DELETE FROM sales_clean", "unscoped DELETE"),
        ("UPDATE inventory_clean SET qty = 0 WHERE qty < 0", "not a table this incident touches"),
        ("UPDATE incidents SET status = 'REMEDIATED' WHERE incident_id = 7", "own tables"),
        (None, "No suggested fix"),
    ],
)
def test_blocked_fix_never_reaches_the_executor_and_is_audited(wire, fix_sql: str | None, reason: str) -> None:
    engine, executor = _engine(fix_sql), FakeExecutor()
    wire(engine, executor)

    success, detail = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert not success
    assert reason in detail
    assert executor.statements == []
    assert engine.final_statuses() == ["FAILED"]
    assert engine.audit_rows()[0]["outcome"] == "BLOCKED"


def test_an_incident_with_no_recorded_tables_cannot_modify_data(wire) -> None:
    engine, executor = _engine(SCOPED_FIX, pipeline_row=None), FakeExecutor()
    wire(engine, executor)

    success, detail = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert not success
    assert "not a table this incident touches" in detail
    assert executor.statements == []


def test_an_executor_error_is_audited_as_failed(wire) -> None:
    engine = _engine(SCOPED_FIX)
    wire(engine, FakeExecutor(error=RuntimeError("constraint violated")))

    success, detail = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert not success
    assert "constraint violated" in detail
    assert engine.final_statuses() == ["FAILED"]
    audit = engine.audit_rows()[0]
    assert (audit["outcome"], audit["detail"]) == ("FAILED", "constraint violated")


def test_missing_executor_credentials_are_audited_as_failed_not_run_as_the_engine(wire) -> None:
    engine = _engine(SCOPED_FIX)
    wire(engine, DatabaseConfigurationError("EXASOL_EXECUTOR_USER is not set"))

    success, detail = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert not success
    assert "EXASOL_EXECUTOR_USER" in detail
    assert engine.audit_rows()[0]["outcome"] == "FAILED"


def test_rejection_only_applies_to_incidents_still_awaiting_approval(wire) -> None:
    engine = _engine(SCOPED_FIX)
    wire(engine, FakeExecutor())

    remediation.reject_incident(7)

    sql, _ = engine.executed[0]
    assert "STATUS = 'REJECTED'" in sql
    assert "AND STATUS = 'AWAITING_APPROVAL'" in sql


AGENT_FIX = "ACTION: rerun-export (agent source=nightly-export, job=export.sh)"


def test_an_agent_fix_is_approved_and_audited_without_the_engine_running_anything(wire) -> None:
    engine, executor = _engine(AGENT_FIX, incident_source="CLI_EVENT"), FakeExecutor()
    wire(engine, executor)

    success, detail = remediation.execute_remediation(9, approved_by="alice (U1)")

    assert success
    assert "rerun-export" in detail
    assert executor.statements == []
    assert engine.final_statuses() == []  # stays APPROVED until the agent reports back
    (audit,) = engine.audit_rows()
    assert (audit["outcome"], audit["executed_by"], audit["executed_sql"]) == ("APPROVED", "alice (U1)", AGENT_FIX)


def test_an_action_marker_on_a_database_incident_is_treated_as_untrusted_sql_and_blocked(wire) -> None:
    engine, executor = _engine(AGENT_FIX, incident_source="PIPELINE_FAILURE"), FakeExecutor()
    wire(engine, executor)

    success, detail = remediation.execute_remediation(7, approved_by="alice (U1)")

    assert not success
    assert "'ACTION'" in detail
    assert executor.statements == []
    assert engine.audit_rows()[0]["outcome"] == "BLOCKED"


def test_an_agent_incident_with_no_fix_is_blocked_like_any_other_missing_fix(wire) -> None:
    engine, executor = _engine(None, incident_source="CLI_EVENT"), FakeExecutor()
    wire(engine, executor)

    success, detail = remediation.execute_remediation(9, approved_by="alice (U1)")

    assert not success
    assert "No suggested fix" in detail
    assert engine.audit_rows()[0]["outcome"] == "BLOCKED"
