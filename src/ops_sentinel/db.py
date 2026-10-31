"""
Exasol connection pools for the two roles the engine plays.

- `get_connection()`: the engine connection. Creates and updates INCIDENTS rows, writes the audit trail and
  runs the dashboard aggregations. Investigating an incident (reading error logs and lineage) goes through
  mcp_client.py instead, so the read/write boundary stays honest end to end, not just in the database grants.
- `get_executor_connection()`: runs approved, guardrail-validated remediation SQL. It is a different database
  user with privileges on the data tables only, so a guardrail bug still cannot rewrite incidents or the
  audit trail. See sql/roles.sql.

Both are pools of already-authenticated connections: every dashboard refresh and incident step would
otherwise pay a full connect and authenticate round trip, and nothing would bound the number of sessions
opened against Exasol.
"""
import logging
import queue
import ssl
import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

import pyexasol

from ops_sentinel.config import settings

logger = logging.getLogger("ops_sentinel.db")


class DatabaseConfigurationError(RuntimeError):
    """A required Exasol credential is missing, so no connection was attempted."""


@dataclass(frozen=True)
class ExasolCredentials:
    user: str
    password: str
    setting_name: str  # environment variable to point the operator at when `user` is empty


def _ssl_options() -> dict[str, int]:
    if settings.exasol_validate_certificate:
        return {"cert_reqs": ssl.CERT_REQUIRED}
    logger.warning(
        "Exasol certificate validation is disabled; use this only with a local self-signed instance"
    )
    return {"cert_reqs": ssl.CERT_NONE}


class ExasolConnectionPool:
    """A small blocking connection pool for pyexasol.

    Thread-safe because callers reach this from multiple asyncio.to_thread
    workers at once (see watcher.py / main.py), not just from one request
    handler at a time.
    """

    def __init__(self, max_size: int, credentials: ExasolCredentials, schema: str | None = None):
        self._max_size = max_size
        self._credentials = credentials
        # None means the v1 schema; "" opens no schema (the migrator connects before the platform schema exists).
        self._schema = settings.exasol_schema if schema is None else schema
        self._idle: queue.Queue[pyexasol.ExaConnection] = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()

    def _create_connection(self) -> pyexasol.ExaConnection:
        if not self._credentials.user:
            raise DatabaseConfigurationError(
                f"{self._credentials.setting_name} is not set; see sql/roles.sql for the database users to create."
            )
        return pyexasol.connect(
            dsn=f"{settings.exasol_admin_host}:{settings.exasol_admin_port}",
            user=self._credentials.user,
            password=self._credentials.password,
            schema=self._schema,
            websocket_sslopt=_ssl_options(),
        )

    def _acquire(self) -> pyexasol.ExaConnection:
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass

        with self._lock:
            if self._created < self._max_size:
                self._created += 1
                try:
                    return self._create_connection()
                except Exception:
                    self._created -= 1
                    raise

        # Pool is at capacity: block until another caller releases one
        # rather than opening an unbounded number of sessions against Exasol.
        return self._idle.get()

    def _release(self, conn: pyexasol.ExaConnection, *, healthy: bool) -> None:
        if healthy:
            self._idle.put(conn)
            return
        # Drop connections that errored instead of returning them to the
        # pool: better to pay for a fresh connect() than to hand the next
        # caller a connection that might be left in a bad state.
        with self._lock:
            self._created -= 1
        try:
            conn.close()
        except Exception:
            logger.warning("failed to close a discarded Exasol connection", exc_info=True)

    @contextmanager
    def connection(self) -> Iterator[pyexasol.ExaConnection]:
        conn = self._acquire()
        healthy = True
        try:
            yield conn
        except Exception:
            healthy = False
            raise
        finally:
            self._release(conn, healthy=healthy)


_engine_pool = ExasolConnectionPool(
    max_size=settings.exasol_pool_size,
    credentials=ExasolCredentials(
        settings.exasol_admin_user, settings.exasol_admin_password, "EXASOL_ADMIN_USER"
    ),
)
_executor_pool = ExasolConnectionPool(
    max_size=2,
    credentials=ExasolCredentials(
        settings.exasol_executor_user, settings.exasol_executor_password, "EXASOL_EXECUTOR_USER"
    ),
)


def get_connection() -> AbstractContextManager[pyexasol.ExaConnection]:
    """Context manager yielding a pooled engine connection.

    Usage:
        with get_connection() as conn:
            conn.execute(...)

    The connection is returned to the pool (not closed) when the block
    exits normally, and dropped/recreated if the block raised.
    """
    return _engine_pool.connection()


def get_executor_connection() -> AbstractContextManager[pyexasol.ExaConnection]:
    """Context manager yielding a pooled connection for approved remediation SQL only."""
    return _executor_pool.connection()
