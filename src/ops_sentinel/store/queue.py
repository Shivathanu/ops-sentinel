"""A durable job queue in the JOBS table.

At-least-once delivery with leases: a worker claims a job for `lease_seconds`; if it crashes, the lease expires and
another worker picks the job up again. Handlers must therefore be idempotent. Claiming is a SELECT followed by an
UPDATE guarded by the same condition, inside one serializable transaction, so two workers can never both own a job:
Exasol rolls one back with a collision and `run_in_transaction` retries it against the new state.

The volume this serves (incidents, agent steps, notifications) is low, so a database queue is enough and saves
running a broker. `JobQueue` is the only module that knows the queue is a table, so a broker can replace it later.
"""
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ops_sentinel.core.models import new_id, utcnow
from ops_sentinel.store.exasol import (
    Connection,
    ConnectionFactory,
    from_json,
    in_list,
    run_in_transaction,
    to_json,
    ts,
)

logger = logging.getLogger("ops_sentinel.queue")

MAX_ERROR_CHARS = 4000


@dataclass(frozen=True)
class Job:
    job_id: str
    queue: str
    kind: str
    workspace_id: str | None
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def backoff_seconds(attempts: int, *, base: float = 5.0, cap: float = 900.0, jitter: Callable[[], float] = random.random) -> float:
    """Exponential backoff with full jitter on the upper half: 5s, 10s, 20s ... capped at 15 minutes."""
    ceiling = min(cap, base * (2 ** max(0, attempts - 1)))
    return ceiling * (0.5 + jitter() / 2)


def enqueue_in(
    conn: Connection,
    kind: str,
    payload: dict[str, Any],
    *,
    queue: str = "default",
    workspace_id: str | None = None,
    idempotency_key: str | None = None,
    run_after: datetime | None = None,
    max_attempts: int = 5,
    now: datetime | None = None,
) -> str | None:
    """Enqueues inside the caller's transaction (a transactional outbox). Returns the job id, or None for a duplicate."""
    created = now or utcnow()
    params = {
        "job_id": new_id(),
        "queue": queue,
        "kind": kind,
        "workspace_id": workspace_id,
        "payload": to_json(payload),
        "key": idempotency_key,
        "max_attempts": max_attempts,
        "run_after": ts(run_after or created),
        "created_at": ts(created),
    }
    columns = (
        "JOB_ID, QUEUE_NAME, KIND, WORKSPACE_ID, PAYLOAD, IDEMPOTENCY_KEY, STATUS, ATTEMPTS, MAX_ATTEMPTS, "
        "RUN_AFTER, CREATED_AT"
    )
    values = (
        "{job_id}, {queue}, {kind}, {workspace_id}, {payload}, {key}, 'queued', 0, {max_attempts!d}, "
        "{run_after}, {created_at}"
    )
    if idempotency_key is None:
        conn.execute(f"INSERT INTO JOBS ({columns}) VALUES ({values})", params)
        return params["job_id"]
    inserted = conn.execute(
        f"INSERT INTO JOBS ({columns}) SELECT {values} FROM DUAL "
        "WHERE NOT EXISTS (SELECT 1 FROM JOBS WHERE IDEMPOTENCY_KEY = {key})",
        params,
    )
    return params["job_id"] if inserted.rowcount() else None


class JobQueue:
    def __init__(self, connect: ConnectionFactory, *, now: Callable[[], datetime] = utcnow):
        self._connect = connect
        self._now = now

    def enqueue(self, kind: str, payload: dict[str, Any], **options: Any) -> str | None:
        return run_in_transaction(self._connect, lambda conn: enqueue_in(conn, kind, payload, now=self._now(), **options))

    def claim(self, worker_id: str, queues: Sequence[str], lease_seconds: int) -> Job | None:
        if not queues:
            return None

        def work(conn: Connection) -> Job | None:
            now = self._now()
            queue_sql, queue_params = in_list("queue", queues)
            params = {"now": ts(now), **queue_params}
            ready = f"STATUS = 'queued' AND RUN_AFTER <= {{now}} AND QUEUE_NAME IN ({queue_sql})"
            row = conn.execute(
                f"SELECT JOB_ID FROM JOBS WHERE {ready} ORDER BY RUN_AFTER, JOB_ID LIMIT 1", params
            ).fetchone()
            if row is None:
                return None
            claimed = conn.execute(
                "UPDATE JOBS SET STATUS = 'running', ATTEMPTS = ATTEMPTS + 1, LEASE_OWNER = {owner}, "
                f"LEASE_UNTIL = {{lease_until}} WHERE JOB_ID = {{job_id}} AND {ready}",
                {**params, "owner": worker_id, "job_id": row[0], "lease_until": ts(now + timedelta(seconds=lease_seconds))},
            )
            if not claimed.rowcount():
                return None
            job_id, queue, kind, workspace_id, payload, attempts, max_attempts = conn.execute(
                "SELECT JOB_ID, QUEUE_NAME, KIND, WORKSPACE_ID, PAYLOAD, ATTEMPTS, MAX_ATTEMPTS FROM JOBS "
                "WHERE JOB_ID = {job_id}",
                {"job_id": row[0]},
            ).fetchone()
            return Job(job_id, queue, kind, workspace_id, from_json(payload, {}), int(attempts), int(max_attempts))

        return run_in_transaction(self._connect, work)

    def complete(self, job: Job, worker_id: str) -> bool:
        """False when the lease was lost (expired and taken by another worker); the result is then discarded."""
        def work(conn: Connection) -> bool:
            done = conn.execute(
                "UPDATE JOBS SET STATUS = 'done', FINISHED_AT = {now}, LEASE_OWNER = NULL, LEASE_UNTIL = NULL "
                "WHERE JOB_ID = {job_id} AND STATUS = 'running' AND LEASE_OWNER = {owner}",
                {"now": ts(self._now()), "job_id": job.job_id, "owner": worker_id},
            )
            return bool(done.rowcount())

        return run_in_transaction(self._connect, work)

    def fail(self, job: Job, worker_id: str, error: str) -> str:
        """Schedules a retry with backoff, or marks the job dead after its last attempt. Returns the new status."""
        now = self._now()
        dead = job.attempts >= job.max_attempts
        status = "dead" if dead else "queued"

        def work(conn: Connection) -> str:
            conn.execute(
                "UPDATE JOBS SET STATUS = {status}, LAST_ERROR = {error}, RUN_AFTER = {run_after}, "
                "FINISHED_AT = {finished_at}, LEASE_OWNER = NULL, LEASE_UNTIL = NULL "
                "WHERE JOB_ID = {job_id} AND STATUS = 'running' AND LEASE_OWNER = {owner}",
                {
                    "status": status,
                    "error": error[-MAX_ERROR_CHARS:],
                    "run_after": ts(now if dead else now + timedelta(seconds=backoff_seconds(job.attempts))),
                    "finished_at": ts(now) if dead else None,
                    "job_id": job.job_id,
                    "owner": worker_id,
                },
            )
            return status

        return run_in_transaction(self._connect, work)

    def extend_lease(self, job: Job, worker_id: str, lease_seconds: int) -> bool:
        def work(conn: Connection) -> bool:
            extended = conn.execute(
                "UPDATE JOBS SET LEASE_UNTIL = {lease_until} "
                "WHERE JOB_ID = {job_id} AND STATUS = 'running' AND LEASE_OWNER = {owner}",
                {"lease_until": ts(self._now() + timedelta(seconds=lease_seconds)), "job_id": job.job_id, "owner": worker_id},
            )
            return bool(extended.rowcount())

        return run_in_transaction(self._connect, work)

    def reap_expired(self) -> int:
        """Returns jobs whose worker vanished to the queue, or buries them if that was their last attempt."""
        def work(conn: Connection) -> int:
            params = {"now": ts(self._now())}
            buried = conn.execute(
                "UPDATE JOBS SET STATUS = 'dead', FINISHED_AT = {now}, LAST_ERROR = 'lease expired on the final attempt', "
                "LEASE_OWNER = NULL, LEASE_UNTIL = NULL "
                "WHERE STATUS = 'running' AND LEASE_UNTIL < {now} AND ATTEMPTS >= MAX_ATTEMPTS",
                params,
            ).rowcount()
            requeued = conn.execute(
                "UPDATE JOBS SET STATUS = 'queued', RUN_AFTER = {now}, LEASE_OWNER = NULL, LEASE_UNTIL = NULL "
                "WHERE STATUS = 'running' AND LEASE_UNTIL < {now}",
                params,
            ).rowcount()
            if buried or requeued:
                logger.warning("reaped expired leases: %d requeued, %d dead", requeued, buried)
            return buried + requeued

        return run_in_transaction(self._connect, work)
