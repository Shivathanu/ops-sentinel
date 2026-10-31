"""A scripted stand-in for an Exasol connection: records every statement and answers from a queue of results.

It does not interpret SQL. Tests assert on the statements sent and script what the database would return, which is
enough to pin the control flow (claims, retries, idempotency) without a database. tests/integration runs the same code
against a real Exasol.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


class FakeStatement:
    def __init__(self, rows: list[tuple] | None = None, rowcount: int = 0):
        self._rows = rows or []
        self._rowcount = rowcount

    def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple]:
        return list(self._rows)

    def rowcount(self) -> int:
        return self._rowcount


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any] | None]] = []
        self.results: list[FakeStatement | Exception] = []
        self.commits = 0
        self.rollbacks = 0
        self.autocommit = True

    def script(self, *results: FakeStatement | Exception) -> "FakeConnection":
        self.results.extend(results)
        return self

    def execute(self, query: str, query_params: dict[str, Any] | None = None) -> FakeStatement:
        self.executed.append((query, query_params))
        if not self.results:
            return FakeStatement()
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def set_autocommit(self, value: bool) -> None:
        self.autocommit = value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def statements(self, containing: str = "") -> list[str]:
        return [query for query, _ in self.executed if containing in query]

    def params_of(self, containing: str) -> dict[str, Any]:
        return next(params or {} for query, params in self.executed if containing in query)


def factory(conn: FakeConnection):
    @contextmanager
    def connect() -> Iterator[FakeConnection]:
        yield conn

    return connect


class Collision(Exception):
    code = "40001"
