"""Connections to the platform schema, transactions and retry on Exasol transaction collisions.

Exasol runs every transaction serializably and resolves write conflicts optimistically: when two sessions'
transactions cannot both commit, one is rolled back with SQLSTATE 40001 ("transaction collision"). Correct code
therefore retries the whole unit of work, which is what `run_in_transaction` does. Every unit of work it runs must be
safe to repeat, and every one in this package is (statements are guarded by status or existence checks).
"""
import json
import logging
import random
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Any, Protocol, TypeVar

from sentinel.config import settings
from sentinel.db import ExasolConnectionPool, ExasolCredentials

logger = logging.getLogger("sentinel.store")

T = TypeVar("T")


class Statement(Protocol):
    def fetchone(self) -> tuple | None: ...
    def fetchall(self) -> list[tuple]: ...
    def rowcount(self) -> int: ...


class Connection(Protocol):
    """The part of pyexasol.ExaConnection this package uses, so tests can substitute a fake."""

    def execute(self, query: str, query_params: dict[str, Any] | None = None) -> Statement: ...
    def set_autocommit(self, value: bool) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


ConnectionFactory = Callable[[], AbstractContextManager[Connection]]


def is_transaction_collision(exc: BaseException) -> bool:
    code = str(getattr(exc, "code", "") or "")
    return code == "40001" or "transaction collision" in str(exc).lower()


@contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    """Groups statements into one atomic commit. Pool connections are autocommit, and are returned that way."""
    conn.set_autocommit(False)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.set_autocommit(True)


def run_in_transaction(
    connect: ConnectionFactory,
    work: Callable[[Connection], T],
    *,
    attempts: int = 5,
    base_delay: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Runs `work` in a transaction, retrying with jittered backoff when Exasol rolls it back for a collision."""
    for attempt in range(1, attempts + 1):
        try:
            with connect() as conn, transaction(conn):
                return work(conn)
        except Exception as exc:
            if not is_transaction_collision(exc) or attempt == attempts:
                raise
            delay = base_delay * (2 ** (attempt - 1)) * (0.5 + random.random())
            logger.info("transaction collision, retrying in %.0f ms (attempt %d/%d)", delay * 1000, attempt, attempts)
            sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def in_list(prefix: str, values: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    """Placeholders for an IN (...) list, since the driver's formatter takes scalars only."""
    if not values:
        raise ValueError("an IN list needs at least one value")
    params = {f"{prefix}_{i}": value for i, value in enumerate(values)}
    return ", ".join("{" + name + "}" for name in params), params


def ts(value: datetime | None) -> str | None:
    """Exasol's default TIMESTAMP literal format (YYYY-MM-DD HH24:MI:SS.FF6)."""
    return None if value is None else value.strftime("%Y-%m-%d %H:%M:%S.%f")


def to_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str, ensure_ascii=False)


def from_json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


_platform_pool: ExasolConnectionPool | None = None
_readonly_pool: ExasolConnectionPool | None = None


def readonly_connection() -> AbstractContextManager[Connection]:
    """The read-only investigation login (EXA_USER, `mcp_readonly`), used by the agents' exasol toolset. Exasol
    enforces that it cannot write, so a model-written query can never change data."""
    global _readonly_pool
    if _readonly_pool is None:
        _readonly_pool = ExasolConnectionPool(
            max_size=3,
            credentials=ExasolCredentials(settings.exa_user, settings.exa_password, "EXA_USER"),
            schema="",
        )
    return _readonly_pool.connection()


def platform_connection() -> AbstractContextManager[Connection]:
    """A pooled runtime connection with the platform schema open. The pool is created on first use."""
    global _platform_pool
    if _platform_pool is None:
        _platform_pool = ExasolConnectionPool(
            max_size=settings.exasol_pool_size,
            credentials=ExasolCredentials(
                settings.exasol_admin_user, settings.exasol_admin_password, "EXASOL_ADMIN_USER"
            ),
            schema=settings.platform_schema,
        )
    return _platform_pool.connection()
