from sentinel.store.queue import Job
from sentinel.workers.handlers import PermanentJobError
from sentinel.workers.worker import Worker


class _Queue:
    def __init__(self, jobs: list[Job]) -> None:
        self.jobs = list(jobs)
        self.completed: list[str] = []
        self.failed: list[tuple[str, int, str]] = []

    def claim(self, worker_id, queues, lease_seconds):
        return self.jobs.pop(0) if self.jobs else None

    def complete(self, job, worker_id):
        self.completed.append(job.job_id)
        return True

    def fail(self, job, worker_id, error):
        self.failed.append((job.job_id, job.attempts, error))
        return "dead" if job.attempts >= job.max_attempts else "queued"

    def extend_lease(self, job, worker_id, lease_seconds):
        return True


def _job(job_id: str, kind: str) -> Job:
    return Job(job_id, "default", kind, "w", {}, 1, 5)


def test_jobs_complete_retry_or_bury_by_outcome() -> None:
    def permanent(job):
        raise PermanentJobError("gone")

    def flaky(job):
        raise RuntimeError("slack 500")

    queue = _Queue([_job("ok", "fine"), _job("retry", "flaky"), _job("dead", "permanent"), _job("orphan", "unknown")])
    worker = Worker(queue, {"fine": lambda job: None, "flaky": flaky, "permanent": permanent}, lease_seconds=30)

    while worker.run_once():
        pass

    assert queue.completed == ["ok"]
    assert ("retry", 1, "RuntimeError: slack 500") in queue.failed
    assert ("dead", 5, "gone") in queue.failed  # buried: attempts forced to the maximum
    assert ("orphan", 5, "no handler for 'unknown'") in queue.failed
