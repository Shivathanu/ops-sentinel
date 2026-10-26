"""Deterministic validation of model-suggested remediation SQL.

An error stack is attacker-influenceable text, so a prompt-injected stack can steer the model into proposing
SQL that does far more than its business-impact narrative implies. Everything here is a hard gate that runs
before any model-suggested statement reaches a write connection: one statement, an allow-listed verb, no
schema, permission or external-I/O keywords, no Sentinel or system objects, a real WHERE scope on updates and
deletes, and a target table that belongs to the incident. It rejects first; widen the allow-list only with a
test that pins the new pattern.

Keyword, scope and table checks run on a masked copy of the statement in which string-literal contents are
blanked out, so a value such as 'DROP TABLE x' or 'a;b' is data, not syntax. Comments are rejected outright
because a machine-suggested fix never needs one and a comment can hide a keyword or fake a WHERE.
"""
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("ops_sentinel.guardrail")

MAX_STATEMENT_LENGTH = 10_000  # INCIDENTS.SUGGESTED_FIX_SQL is VARCHAR(10000)

ALLOWED_VERBS = frozenset({"INSERT", "UPDATE", "MERGE", "DELETE", "SELECT"})

# CALL is deliberately absent: it can invoke any stored procedure or script, which no guardrail can inspect.
FORBIDDEN_KEYWORDS = frozenset({
    "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE", "ATTACH", "DETACH",
    "EXECUTE", "IMPORT", "EXPORT", "RENAME", "COMMIT", "ROLLBACK",
})

# Sentinel's own metadata, including the audit trail, must never be a remediation target or source.
PROTECTED_TABLES = frozenset({
    "INCIDENTS", "REMEDIATION_ACTIONS", "PIPELINE_LOGS", "QUERY_METRICS", "DATA_LINEAGE",
})

_IDENTIFIER = r'(?:"[^"]+"|[A-Za-z_][\w$#]*)'
_QUALIFIED_NAME = rf"{_IDENTIFIER}(?:\s*\.\s*{_IDENTIFIER})*"
_TARGET_PATTERNS: dict[str, re.Pattern[str]] = {
    "UPDATE": re.compile(rf"^\s*UPDATE\s+(?P<name>{_QUALIFIED_NAME})", re.IGNORECASE),
    "DELETE": re.compile(rf"^\s*DELETE\s+FROM\s+(?P<name>{_QUALIFIED_NAME})", re.IGNORECASE),
    "INSERT": re.compile(rf"^\s*INSERT\s+INTO\s+(?P<name>{_QUALIFIED_NAME})", re.IGNORECASE),
    "MERGE": re.compile(rf"^\s*MERGE\s+INTO\s+(?P<name>{_QUALIFIED_NAME})", re.IGNORECASE),
}
_SCOPED_VERBS = frozenset({"UPDATE", "DELETE"})
_LEADING_VERB = re.compile(r"^\s*([A-Za-z]+)")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*")
_SYSTEM_OBJECT = re.compile(r"\b(?:SYS|EXA_\w+)\b", re.IGNORECASE)
_ALWAYS_TRUE_PREDICATE = re.compile(r"^\(*\s*(?:TRUE|(\d+)\s*=\s*\1)\s*\)*$", re.IGNORECASE)
_OR_ALWAYS_TRUE = re.compile(r"\bOR\s+\(*\s*(?:TRUE|(\d+)\s*=\s*\1)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """Outcome of validating one suggested statement."""

    statement: str | None = None
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.statement is not None


class _Rejection(Exception):
    """Internal control flow: carries the human-readable reason a statement was refused."""


def validate_fix_sql(
    sql: str | None,
    *,
    allowed_tables: frozenset[str] | None = None,
    home_schema: str | None = None,
) -> Verdict:
    """Validates one suggested statement.

    `allowed_tables` (upper-case names) restricts INSERT, UPDATE, DELETE and MERGE to the tables an incident
    actually touches; pass None only where no incident exists, such as in unit tests. `home_schema` rejects a
    target qualified with any other schema.

    On success `Verdict.statement` is the single normalised statement; otherwise `Verdict.reason` says why,
    suitable for the Slack thread and the audit log.
    """
    try:
        statement = _validated_statement(sql, allowed_tables, home_schema)
    except _Rejection as rejection:
        logger.warning("remediation SQL rejected: %s", rejection)
        return Verdict(reason=str(rejection))
    return Verdict(statement=statement)


def _validated_statement(
    sql: str | None, allowed_tables: frozenset[str] | None, home_schema: str | None
) -> str:
    if not sql or not sql.strip():
        raise _Rejection("No suggested fix SQL recorded for this incident.")
    if len(sql) > MAX_STATEMENT_LENGTH:
        raise _Rejection(f"Refusing to run SQL longer than {MAX_STATEMENT_LENGTH} characters.")

    statement, masked_statement = _single_statement(sql, _mask_literals(sql))
    verb = _leading_verb(masked_statement)
    _reject_forbidden_content(masked_statement)
    if verb in _SCOPED_VERBS:
        _require_real_where(verb, masked_statement)
    if verb in _TARGET_PATTERNS:
        _require_incident_target(verb, masked_statement, allowed_tables, home_schema)
    return statement


def _mask_literals(sql: str) -> str:
    """Blanks the contents of string literals and rejects comments and control characters.

    The result has the same length and positions as `sql`, so indexes found in it apply to the original.
    """
    masked: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            end = _closing_quote(sql, index, "'")
            masked.append("'" + " " * (end - index - 1) + "'")
            index = end + 1
        elif char == '"':
            end = _closing_quote(sql, index, '"')
            masked.append(sql[index : end + 1])
            index = end + 1
        elif sql.startswith(("--", "/*"), index):
            raise _Rejection(
                "Refusing to run SQL containing a comment; comments can hide keywords or fake a WHERE."
            )
        elif ord(char) < 32 and char not in "\t\n\r":
            raise _Rejection("Refusing to run SQL containing control characters.")
        else:
            masked.append(char)
            index += 1
    return "".join(masked)


def _closing_quote(sql: str, start: int, quote: str) -> int:
    """Index of the quote closing the literal opened at `start`; a doubled quote is an escaped one."""
    index = start + 1
    while index < len(sql):
        if sql[index] == quote:
            if sql.startswith(quote, index + 1):
                index += 2
                continue
            return index
        index += 1
    raise _Rejection("Refusing to run SQL with an unterminated quote.")


def _single_statement(sql: str, masked: str) -> tuple[str, str]:
    """Splits on semicolons outside literals and returns the one non-empty statement, original and masked."""
    spans: list[tuple[int, int]] = []
    start = 0
    for index, char in enumerate(masked + ";"):
        if char == ";":
            if masked[start:index].strip():
                spans.append((start, index))
            start = index + 1
    if len(spans) != 1:
        raise _Rejection(
            "Refusing to run multi-statement SQL; exactly one statement is allowed per remediation."
        )
    begin, end = spans[0]
    return sql[begin:end].strip(), masked[begin:end].strip()


def _leading_verb(masked_statement: str) -> str:
    match = _LEADING_VERB.match(masked_statement)
    verb = match.group(1).upper() if match else ""
    if verb not in ALLOWED_VERBS:
        raise _Rejection(
            f"Refusing to run a '{verb or '?'}' statement; only {sorted(ALLOWED_VERBS)} are allowed "
            "in automated remediation."
        )
    return verb


def _reject_forbidden_content(masked_statement: str) -> None:
    words = {word.upper() for word in _WORD.findall(masked_statement)}
    if words & FORBIDDEN_KEYWORDS:
        raise _Rejection("Refusing to run SQL containing a schema, permission or external-I/O keyword.")
    if words & PROTECTED_TABLES:
        raise _Rejection("Refusing to run SQL that references Ops Sentinel's own tables.")
    if _SYSTEM_OBJECT.search(masked_statement):
        raise _Rejection("Refusing to run SQL that references system objects.")


def _require_real_where(verb: str, masked_statement: str) -> None:
    predicate = _top_level_where_predicate(masked_statement)
    if predicate is None:
        raise _Rejection(
            f"Refusing to run an unscoped {verb} with no WHERE clause; too high a blast radius "
            "for automated remediation."
        )
    if _ALWAYS_TRUE_PREDICATE.match(predicate.strip()) or _OR_ALWAYS_TRUE.search(predicate):
        raise _Rejection(f"Refusing to run a {verb} whose WHERE clause is always true.")


def _top_level_where_predicate(masked_statement: str) -> str | None:
    """Text after the first WHERE that is not inside parentheses, or None if there is none.

    A WHERE inside a subquery scopes the subquery, not the statement being updated or deleted.
    """
    depth = 0
    for match in re.finditer(r"[()]|\bWHERE\b", masked_statement, re.IGNORECASE):
        token = match.group()
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            return masked_statement[match.end() :]
    return None


def _require_incident_target(
    verb: str, masked_statement: str, allowed_tables: frozenset[str] | None, home_schema: str | None
) -> None:
    match = _TARGET_PATTERNS[verb].match(masked_statement)
    if match is None:
        raise _Rejection(f"Refusing to run a {verb} whose target table cannot be determined.")
    schema, table = _split_qualified_name(match.group("name"))
    if schema and home_schema and schema != home_schema.upper():
        raise _Rejection(
            f"Refusing to modify '{schema}.{table}'; only the {home_schema.upper()} schema is allowed."
        )
    if allowed_tables is not None and table not in allowed_tables:
        raise _Rejection(f"Refusing to modify '{table}'; it is not a table this incident touches.")


def _split_qualified_name(name: str) -> tuple[str | None, str]:
    parts = [part.strip().strip('"').upper() for part in name.split(".")]
    return (parts[-2] if len(parts) > 1 else None), parts[-1]
