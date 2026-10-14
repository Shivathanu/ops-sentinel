"""
The WRITE path. Only remediation.py and the watcher's bookkeeping (creating
INCIDENTS rows, which is sentinel's own metadata, not a "fix") should import
this. Everything that investigates an incident (reading error logs, reading
lineage) should go through mcp_client.py instead, to keep the read/write
boundary honest end to end, not just in the DB grants.

CRITICAL FIX: the previous version called pyexasol.connect() / conn.close()
on every single query. That meant every dashboard refresh and every step of
every incident paid a full connect+auth round trip, and concurrent requests
had no shared capacity limit — nothing stood between normal traffic and
exhausting Exasol's session limit. This version keeps a small pool of
already-authenticated connections and hands them out via a context manager,
so `with get_connection() as conn:` is a drop-in replacement for the old
`conn = get_admin_connection(); try/finally: conn.close()` pattern.
"""
import queue
import ssl
import threading
from contextlib import contextmanager

import pyexasol

from engine.config import settings


class ExasolConnectionPool:
    """A small blocking connection pool for pyexasol.

    Thread-safe because callers reach this from multiple asyncio.to_thread
    workers at once (see watcher.py / main.py), not just from one request
    handler at a time.
    """

    def __init__(self, max_size: int):
        self._max_size = max_size
        self._idle: "queue.Queue[pyexasol.ExaConnection]" = queue.Queue()
        self._created = 0
        self._lock = threading.Lock()

    def _create_connection(self) -> "pyexasol.ExaConnection":
        return pyexasol.connect(
            dsn=f"{settings.exasol_admin_host}:{settings.exasol_admin_port}",
            user=settings.exasol_admin_user,
            password=settings.exasol_admin_password,
            schema=settings.exasol_schema,
            # Exasol Personal Local uses a self-signed certificate (see
            # `exakit info`). The MCP server is configured the same way via
            # EXA_SSL_CERT_VALIDATION=no — this is the admin-connection
            # equivalent of that, appropriate for a local-only instance only.
            websocket_sslopt={"cert_reqs": ssl.CERT_NONE},
        )

    def _acquire(self) -> "pyexasol.ExaConnection":
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            pass

        with self._lock:
            if self._created < self._max_size:
                self._created += 1
                return self._create_connection()

        # Pool is at capacity — block until another caller releases one
        # rather than opening an unbounded number of sessions against Exasol.
        return self._idle.get()

    def _release(self, conn: "pyexasol.ExaConnection", *, healthy: bool) -> None:
        if healthy:
            self._idle.put(conn)
            return
        # Drop connections that errored instead of returning them to the
        # pool — better to pay for a fresh connect() than to hand the next
        # caller a connection that might be left in a bad state.
        with self._lock:
            self._created -= 1
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup of a connection we're discarding anyway
            pass

    @contextmanager
    def connection(self):
        conn = self._acquire()
        healthy = True
        try:
            yield conn
        except Exception:
            healthy = False
            raise
        finally:
            self._release(conn, healthy=healthy)


_pool = ExasolConnectionPool(max_size=settings.exasol_pool_size)


def get_connection():
    """Context manager yielding a pooled admin connection.

    Usage:
        with get_connection() as conn:
            conn.execute(...)

    The connection is returned to the pool (not closed) when the block
    exits normally, and dropped/recreated if the block raised.
    """
    return _pool.connection()
