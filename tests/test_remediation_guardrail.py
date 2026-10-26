"""The guardrail is the only barrier between an LLM-suggested statement and a write to Exasol,
so every rule it enforces is pinned here, and every known gap is an explicit strict xfail."""
import pytest

from ops_sentinel.remediation import _validate_fix_sql

SAFE_STATEMENTS = [
    "UPDATE sales_clean SET amount = 0 WHERE amount < 0",
    "update sales_clean set amount = 0 where amount < 0;",
    "INSERT INTO audit_log VALUES (1, 'x')",
    "DELETE FROM staging_orders WHERE load_id = 42",
    "SELECT * FROM sales_clean",
    "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN UPDATE SET t.v = s.v",
    "CALL refresh_sales_clean()",
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
    ("DELETE FROM sales_clean", "unscoped DELETE"),
    ("UPDATE sales_clean SET amount = 0", "unscoped UPDATE"),
    ("INSERT INTO t VALUES (1) GRANT ALL ON t TO PUBLIC", "permission-altering"),
]


@pytest.mark.parametrize("sql", SAFE_STATEMENTS)
def test_safe_statements_are_allowed(sql: str) -> None:
    is_safe, detail = _validate_fix_sql(sql)

    assert is_safe, detail


def test_allowed_statement_is_returned_normalised_without_trailing_semicolon() -> None:
    is_safe, detail = _validate_fix_sql("  UPDATE t SET a = 1 WHERE b = 2 ;  ")

    assert (is_safe, detail) == (True, "UPDATE t SET a = 1 WHERE b = 2")


@pytest.mark.parametrize(("sql", "reason"), REJECTED_STATEMENTS)
def test_unsafe_statements_are_rejected_with_a_reason(sql: str | None, reason: str) -> None:
    is_safe, detail = _validate_fix_sql(sql)

    assert not is_safe
    assert reason in detail


@pytest.mark.xfail(reason="known gap: a WHERE inside a comment satisfies the unscoped-UPDATE check")
def test_where_inside_block_comment_does_not_count_as_a_scope() -> None:
    is_safe, _ = _validate_fix_sql("UPDATE t SET a = 1 /* WHERE */")

    assert not is_safe


@pytest.mark.xfail(reason="known gap: a WHERE inside a line comment satisfies the unscoped-DELETE check")
def test_where_inside_line_comment_does_not_count_as_a_scope() -> None:
    is_safe, _ = _validate_fix_sql("DELETE FROM t -- WHERE x = 1")

    assert not is_safe
