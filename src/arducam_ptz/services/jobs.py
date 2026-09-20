"""Background jobs: the 202 + poll pattern for anything slow.

Why this exists: an autofocus sweep takes 3-8 seconds. In the old app it ran inside
the request handler *while holding the camera*, so the browser waited, the preview
froze, a second request could not get a lock, and on a phone the request often just
timed out with no idea whether the camera was moving. Here the request returns
immediately with a job id and a poll URL, progress is reported, and the work can be
cancelled.

The registry is in memory on purpose: jobs describe work in progress, not history.
They are pruned when they become old, keeping at most a handful around so a client
that polls a second late still finds its result.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from arducam_ptz.models import JobState, JobStatus

logger = logging.getLogger(__name__)

#: How long a finished job stays retrievable. A phone that backgrounded itself
#: during a sweep should still find the answer when it comes back.
DEFAULT_TTL_S = 300.0

#: Never keep more than this many finished jobs, however recent they are.
DEFAULT_KEEP = 20


class Job:
    """One unit of background work, safe to touch from several threads."""

    def __init__(self, job_id: str, kind: str, poll_url: str) -> None:
        self.id = job_id
        self.kind = kind
        self.poll_url = poll_url
        self.status = JobStatus.PENDING
        self.progress = 0.0
        self.phase: str | None = None
        self.result: Any = None
        self.error: str | None = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ control
    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        """Ask the worker to stop. It decides when it is safe (between samples)."""
        self._cancel.set()
        with self._lock:
            if not self.status.terminal:
                self.phase = "cancelling"

    # ------------------------------------------------------------------ updates
    def update(
        self,
        *,
        status: JobStatus | None = None,
        progress: float | None = None,
        phase: str | None = None,
    ) -> None:
        """Advance the job. Ignored once the job is terminal (no resurrection)."""
        with self._lock:
            if self.status.terminal and status is not None and status != self.status:
                return
            if status is not None:
                self.status = status
            if progress is not None:
                self.progress = max(0.0, min(1.0, progress))
            if phase is not None:
                self.phase = phase
            self.updated_at = time.time()

    def succeed(self, result: Any) -> None:
        with self._lock:
            self.status = JobStatus.COMPLETED
            self.result = result
            self.progress = 1.0
            self.phase = None
            self.updated_at = time.time()

    def fail(self, error: str) -> None:
        with self._lock:
            self.status = JobStatus.FAILED
            self.error = error
            self.updated_at = time.time()

    def cancelled_finally(self) -> None:
        with self._lock:
            self.status = JobStatus.CANCELLED
            self.phase = None
            self.updated_at = time.time()

    # ------------------------------------------------------------------ views
    def to_model(self) -> JobState:
        """Snapshot for the API. Never hands out the live object."""
        with self._lock:
            # A worker may store a pydantic model as the payload; the wire wants
            # plain JSON, so serialise it here instead of at every call site.
            result = self.result.model_dump() if isinstance(self.result, BaseModel) else self.result
            return JobState(
                id=self.id,
                kind=self.kind,  # type: ignore[arg-type]
                status=self.status,
                progress=self.progress,
                phase=self.phase,
                created_at=self.created_at,
                updated_at=self.updated_at,
                poll_url=self.poll_url,
                result=result,
                error=self.error,
            )

    def is_fresh(self, ttl_s: float = DEFAULT_TTL_S) -> bool:
        return (time.time() - self.updated_at) < ttl_s


class JobRegistry:
    """Keeps track of running and recently finished jobs."""

    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        keep: int = DEFAULT_KEEP,
        log: logging.Logger | None = None,
    ) -> None:
        self.log = log or logger
        self.ttl_s = ttl_s
        self.keep = keep
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def create(self, kind: str, poll_url: Callable[[str], str]) -> Job:
        """Register a new job. ``poll_url`` turns the id into a URL to poll."""
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id, kind, poll_url(job_id))
        with self._lock:
            self.prune_locked()
            self._jobs[job_id] = job
        self.log.debug("job %s (%s) created", job_id, kind)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def active(self, kind: str | None = None) -> list[Job]:
        """Jobs that are still pending or running."""
        with self._lock:
            return [
                job
                for job in self._jobs.values()
                if not job.status.terminal and (kind is None or job.kind == kind)
            ]

    def latest(self, kind: str) -> Job | None:
        """The most recently updated job of a kind - running or finished.

        Used to answer "what is the autofocus doing?" in the aggregate state
        endpoint without the client having to remember a job id.
        """
        with self._lock:
            candidates = [job for job in self._jobs.values() if job.kind == kind]
        return max(candidates, key=lambda job: job.updated_at, default=None)

    # ------------------------------------------------------------------ pruning
    def prune(self) -> int:
        with self._lock:
            return self.prune_locked()

    def prune_locked(self) -> int:
        """Drop stale finished jobs; keep the newest ``keep`` of them anyway."""
        stale = [job for job in self._jobs.values() if job.status.terminal and not job.is_fresh(self.ttl_s)]
        for job in stale:
            self._jobs.pop(job.id, None)
        finished = sorted(
            (job for job in self._jobs.values() if job.status.terminal),
            key=lambda job: job.updated_at,
            reverse=True,
        )
        for job in finished[self.keep :]:
            self._jobs.pop(job.id, None)
        if stale:
            self.log.debug("pruned %d finished job(s)", len(stale))
        return len(stale)
