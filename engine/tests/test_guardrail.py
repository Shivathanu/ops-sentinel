"""The guardrail is the only barrier between an LLM-suggested statement and a write to Exasol,
so every rule it enforces is pinned here."""
import pytest

from sentinel.guardrail import MAX_STATEMENT_LENGTH, validate_fix_sql

SAFE_STATEMENTS = [
    "UPDATE sales_clean SET amount = 0 WHERE amount < 0",
    "update sales_clean set amount = 0 where amount < 0;",
    "INSERT INTO audit_log VALUES (1, 'x')",
    "DELETE FROM staging_orders WHERE load_id = 42",
    "SELECT * FROM sales_clean",
    "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.v = s.v",
    "UPDATE t SET note = 'a;b' WHERE id = 1",
    "UPDATE t SET note = 'DROP TABLE x -- not a comment' WHERE id = 1",
    "UPDATE t SET note = 'it''s fine' WHERE id = 1",
    "UPDATE t\nSET a = 1\nWHERE b = 2",
    "UPDATE t SET a = (SELECT MAX(b) FROM u) WHERE id = 1",
    "UPDATE t SET dropped_flag = 1 WHERE id = 1",
]

REJECTED_STATEMENTS = [
    (None, "No suggested fix"),
    ("", "No suggested fix"),
    ("   ", "No suggested fix"),
    ("UPDATE a SET x = 1 WHERE y = 2; DELETE FROM b WHERE z = 3", "multi-statement"),
    ("INSERT INTO t SELECT * FROM s; DROP TABLE t", "multi-statement"),
    ("DROP TABLE sales_clean", "'DROP'"),
    ("TRUNCATE TABLE sales_clean", "'TRUNCATE'"),
    ("CREATE TABLE x (a INT)", "'CREATE'"),
    ("CALL refresh_sales_clean()", "'CALL'"),
    ("DELETE FROM sales_clean", "unscoped DELETE"),
    ("UPDATE sales_clean SET amount = 0", "unscoped UPDATE"),
    ("UPDATE t SET a = (SELECT b FROM u WHERE u.id = 1)", "unscoped UPDATE"),
    ("INSERT INTO t VALUES (1) GRANT ALL ON t TO PUBLIC", "keyword"),
    ("INSERT INTO t SELECT * FROM (IMPORT INTO (a INT) FROM CSV AT 'http://x' FILE 'f')", "keyword"),
    ("UPDATE t SET a = 1 /* WHERE */", "comment"),
    ("DELETE FROM t -- WHERE x = 1", "comment"),
    ("DELETE FROM t WHERE 1 = 1", "always true"),
    ("DELETE FROM t WHERE TRUE", "always true"),
    ("DELETE FROM t WHERE (1=1)", "always true"),
    ("UPDATE t SET a = 1 WHERE id = 1 OR 1 = 1", "always true"),
    ("UPDATE REMEDIATION_ACTIONS SET OUTCOME = 'SUCCESS' WHERE ACTION_ID = 1", "own tables"),
    ("DELETE FROM sentinel.incidents WHERE incident_id = 1", "own tables"),
    ("SELECT * FROM SENTINEL.INCIDENTS", "own tables"),
    ("SELECT * FROM EXA_ALL_USERS", "system objects"),
    ("SELECT * FROM SYS.EXA_DBA_USERS", "system objects"),
    ("UPDATE t SET a = 'unterminated WHERE id = 1", "unterminated"),
    ("UPDATE t SET a = 1 WHERE id = 1\x00", "control characters"),
    ("UPDATE t SET a = 1 WHERE note = '" + "x" * MAX_STATEMENT_LENGTH + "'", "longer than"),
]


@pytest.mark.parametrize("sql", SAFE_STATEMENTS)
def test_safe_statements_are_allowed(sql: str) -> None:
    verdict = validate_fix_sql(sql)

    assert verdict.allowed, verdict.reason


def test_allowed_statement_is_returned_normalised_without_trailing_semicolon() -> None:
    verdict = validate_fix_sql("  UPDATE t SET a = 1 WHERE b = 2 ;  ")

    assert verdict.statement == "UPDATE t SET a = 1 WHERE b = 2"
    assert verdict.reason is None


@pytest.mark.parametrize(("sql", "reason"), REJECTED_STATEMENTS)
def test_unsafe_statements_are_rejected_with_a_reason(sql: str | None, reason: str) -> None:
    verdict = validate_fix_sql(sql)

    assert not verdict.allowed
    assert verdict.statement is None
    assert reason in (verdict.reason or "")


TABLES = frozenset({"SALES_CLEAN", "SALES_RAW"})


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE sales_clean SET a = 1 WHERE id = 1",
        'UPDATE "sales_clean" SET a = 1 WHERE id = 1',
        "UPDATE SENTINEL.sales_clean SET a = 1 WHERE id = 1",
        "DELETE FROM sales_raw WHERE id = 1",
        "INSERT INTO sales_clean SELECT * FROM sales_raw",
        "SELECT * FROM anything",
    ],
)
def test_statements_on_the_incident_tables_are_allowed(sql: str) -> None:
    verdict = validate_fix_sql(sql, allowed_tables=TABLES, home_schema="SENTINEL")

    assert verdict.allowed, verdict.reason


@pytest.mark.parametrize(
    ("sql", "reason"),
    [
        ("UPDATE finance_executive_bi SET a = 1 WHERE id = 1", "not a table this incident touches"),
        ("DELETE FROM other_table WHERE id = 1", "not a table this incident touches"),
        ("UPDATE OTHER_SCHEMA.sales_clean SET a = 1 WHERE id = 1", "only the SENTINEL schema"),
        ("DELETE t FROM sales_clean t WHERE t.id = 1", "cannot be determined"),
    ],
)
def test_statements_outside_the_incident_tables_are_rejected(sql: str, reason: str) -> None:
    verdict = validate_fix_sql(sql, allowed_tables=TABLES, home_schema="SENTINEL")

    assert not verdict.allowed
    assert reason in (verdict.reason or "")


def test_an_incident_with_no_known_tables_cannot_modify_any_table() -> None:
    verdict = validate_fix_sql("UPDATE sales_clean SET a = 1 WHERE id = 1", allowed_tables=frozenset())

    assert not verdict.allowed
