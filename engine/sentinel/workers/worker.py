"""The job worker: `sentinel-worker`.

Runs N threads, each claiming one job at a time from the Exasol queue. A heartbeat extends the lease of a running job,
so a long agent run is not mistaken for a crashed worker; a worker that really crashes stops heart-beating and its
jobs return to the queue when the lease expires. SIGTERM and SIGINT stop claiming, let running jobs finish, then exit,
which is what Kubernetes and Docker expect from a graceful shutdown.
"""
import argparse
import logging
import os
import signal
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from sentinel.config import settings
from sentinel.store.queue import Job, JobQueue
from sentinel.workers.handlers import Handler, Handlers, PermanentJobError

logger = logging.getLogger("sentinel.worker")

REAP_EVERY_SECONDS = 30.0
HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "sentinel-worker.alive"


class Worker:
    def __init__(
        self,
        queue: JobQueue,
        handlers: Mapping[str, Handler],
        *,
        queues: Sequence[str] = ("default",),
        concurrency: int = 4,
        poll_seconds: float = 2.0,
        lease_seconds: int = 300,
        worker_id: str | None = None,
        heartbeat_file: Path | None = None,
    ):
        self._queue = queue
        self._handlers = dict(handlers)
        self._queues = list(queues)
        self._concurrency = concurrency
        self._poll = poll_seconds
        self._lease = lease_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.stopping = threading.Event()
        self.heartbeat_file = heartbeat_file

    def run_once(self, slot: int = 0) -> bool:
        """Claims and processes at most one job. Returns True if a job was processed."""
        job = self._queue.claim(f"{self.worker_id}/{slot}", self._queues, self._lease)
        if job is None:
            return False
        self._process(job, f"{self.worker_id}/{slot}")
        return True

    def _process(self, job: Job, owner: str) -> None:
        handler = self._handlers.get(job.kind)
        if handler is None:
            self._queue.fail(replace(job, attempts=job.max_attempts), owner, f"no handler for '{job.kind}'")
            logger.error("buried job %s: no handler for kind '%s'", job.job_id, job.kind)
            return

        beat_stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(job, owner, beat_stop), daemon=True)
        heartbeat.start()
        started = time.monotonic()
        try:
            handler(job)
        except PermanentJobError as exc:
            self._queue.fail(replace(job, attempts=job.max_attempts), owner, str(exc))
            logger.error("job %s (%s) failed permanently: %s", job.job_id, job.kind, exc)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job and retried
            status = self._queue.fail(job, owner, f"{type(exc).__name__}: {exc}")
            logger.warning("job %s (%s) attempt %d failed (%s): %s", job.job_id, job.kind, job.attempts, status, exc)
        else:
            if not self._queue.complete(job, owner):
                logger.warning("job %s finished after its lease was lost; another worker owns it now", job.job_id)
            logger.info("job %s (%s) done in %.0f ms", job.job_id, job.kind, (time.monotonic() - started) * 1000)
        finally:
            beat_stop.set()
            heartbeat.join(timeout=5)

    def _heartbeat(self, job: Job, owner: str, stop: threading.Event) -> None:
        while not stop.wait(self._lease / 3):
            try:
                if not self._queue.extend_lease(job, owner, self._lease):
                    logger.warning("lost the lease on job %s", job.job_id)
                    return
            except Exception:  # noqa: BLE001
                logger.warning("could not extend the lease on job %s", job.job_id, exc_info=True)

    def _loop(self, slot: int) -> None:
        while not self.stopping.is_set():
            try:
                worked = self.run_once(slot)
                self._mark_alive()
            except Exception:  # noqa: BLE001 - the database may be briefly unavailable; keep the thread alive
                logger.exception("worker slot %d could not claim a job", slot)
                worked = False
            if not worked:
                self.stopping.wait(self._poll)

    def _mark_alive(self) -> None:
        """Touches the heartbeat file after every successful trip to the queue. A container healthcheck compares
        its age (`sentinel-worker --check`), so a worker that cannot reach Exasol turns unhealthy."""
        if self.heartbeat_file is None:
            return
        try:
            self.heartbeat_file.touch()
        except OSError:
            logger.warning("cannot write the heartbeat file %s", self.heartbeat_file, exc_info=True)

    def _reaper(self) -> None:
        while not self.stopping.wait(REAP_EVERY_SECONDS):
            try:
                self._queue.reap_expired()
            except Exception:  # noqa: BLE001
                logger.warning("lease reaper failed", exc_info=True)

    def run(self) -> None:
        threads = [threading.Thread(target=self._loop, args=(slot,), name=f"worker-{slot}") for slot in range(self._concurrency)]
        threads.append(threading.Thread(target=self._reaper, name="reaper", daemon=True))
        for thread in threads:
            thread.start()
        logger.info("worker %s started: %d slots on queues %s, kinds %s",
                    self.worker_id, self._concurrency, self._queues, sorted(self._handlers))
        for thread in threads[:-1]:
            thread.join()
        logger.info("worker %s stopped", self.worker_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel-worker", description="Process Ops Sentinel jobs.")
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("OPS_SENTINEL_WORKER_CONCURRENCY", "4")))
    parser.add_argument("--queue", action="append", dest="queues", help="queue to serve; repeat for several")
    parser.add_argument("--check", action="store_true",
                        help="healthcheck: exit 0 if a running worker reached the queue recently, else 1")
    args = parser.parse_args(argv)
    if args.check:
        try:
            age = time.time() - HEARTBEAT_FILE.stat().st_mtime
        except OSError:
            return 1
        return 0 if age < max(60.0, settings.worker_poll_seconds * 10) else 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from sentinel.platform import get_platform

    platform = get_platform()
    if platform is None:
        logger.error("OPS_SENTINEL_SPEC_FILE is not set; the worker needs a workspace spec")
        return 2
    problems = platform.validate()
    if problems:
        for problem in problems:
            logger.error("config: %s", problem)
        return 2

    worker = Worker(
        platform.queue,
        Handlers(platform).mapping(),
        queues=args.queues or ["default"],
        concurrency=max(1, args.concurrency),
        poll_seconds=settings.worker_poll_seconds,
        lease_seconds=settings.worker_lease_seconds,
        heartbeat_file=HEARTBEAT_FILE,
    )

    def stop(signum: int, _frame: object) -> None:
        logger.info("received signal %d; finishing running jobs", signum)
        worker.stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
