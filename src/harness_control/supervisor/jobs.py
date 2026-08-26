"""In-memory registry of activation jobs, surfaced by `GET /admin/jobs/{id}`.

In-memory is deliberate for the MVP: a job is only interesting while the client
that started it is still polling, and a control-plane restart abandons the
activation anyway. Persisting the last 20 to disk is an M2 item.
"""

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime

from harness_control.models import JobStatus

MAX_JOBS = 100
LOG_TAIL_LINES = 20


def utcnow_iso() -> str:
    """`2026-08-25T18:04:11Z` — the format the contract's examples use."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Job:
    job_id: str
    model_id: str
    status: JobStatus = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    log_tail: list[str] = field(default_factory=list)

    def start(self) -> None:
        self.status = "running"
        self.started_at = utcnow_iso()

    def succeed(self) -> None:
        self.status = "succeeded"
        self.finished_at = utcnow_iso()

    def fail(self, error: str, log_tail: list[str] | None = None) -> None:
        self.status = "failed"
        self.finished_at = utcnow_iso()
        self.error = error
        self.log_tail = (log_tail or [])[-LOG_TAIL_LINES:]

    def cancel(self) -> None:
        self.status = "cancelled"
        self.finished_at = utcnow_iso()

    @property
    def is_finished(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")


class JobRegistry:
    """Bounded, insertion-ordered. The oldest job falls off at `MAX_JOBS`."""

    def __init__(self, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: dict[str, Job] = {}
        self._max_jobs = max_jobs
        self._counter = itertools.count(1)

    def create(self, model_id: str) -> Job:
        # `act_` + a monotonic counter: readable in logs, and unlike a timestamp it
        # cannot collide when two activations are rejected in the same millisecond.
        job = Job(job_id=f"act_{next(self._counter):08d}", model_id=model_id)
        self._jobs[job.job_id] = job
        while len(self._jobs) > self._max_jobs:
            self._jobs.pop(next(iter(self._jobs)))
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def __len__(self) -> int:
        return len(self._jobs)
