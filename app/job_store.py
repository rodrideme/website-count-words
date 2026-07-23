from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.models import PageResult

JOBS: dict[str, "Job"] = {}


@dataclass
class Job:
    id: str
    source_url: str
    user_id: int
    max_pages: int
    status: str = "starting"  # starting | crawling | completed | failed | cancelled | paused
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    pages: dict[str, PageResult] = field(default_factory=dict)
    login_blocked: dict[str, PageResult] = field(default_factory=dict)
    total_words: int = 0
    error: str | None = None
    limit_reached: bool = False
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    cancel_requested: bool = False
    detected_language: str | None = None
    domain_scope: str = "all"
    language_setting: str | None = None
    # BFSDeepCrawlStrategy's on_state_change snapshot — lets a paused crawl
    # (see run_crawl's pause_at_words) resume later from the same frontier.
    resume_state: dict | None = None
    estimate_result: dict | None = None
    stopped_reason: str | None = None
    # Only set when reconstructed from a checkpoint (see restore_job) — the
    # crash happened before the specific login-blocked URLs were persisted,
    # just their count, so it's tracked separately from the live dict above.
    restored_login_blocked_count: int = 0
    # The asyncio.Task actually running run_crawl for this job, if it's in
    # this process (never persisted/serialized — purely a runtime handle).
    # crawl4ai's BFSDeepCrawlStrategy only checks its cooperative
    # should_cancel callback between BFS levels, which can take a long time
    # to come back around on a slow site — cancelling this task directly
    # interrupts it immediately, at whatever await point it's currently at.
    task: asyncio.Task | None = None

    def request_cancel(self) -> None:
        # Still set for should_cancel to see (covers the narrow window before
        # a page's first checkpoint, or if .task somehow isn't set) — but
        # .task.cancel() below is what actually makes this prompt.
        self.cancel_requested = True
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def publish(self, event: dict) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def status_payload(self) -> dict:
        return {
            "type": "status",
            "status": self.status,
            "total_words": self.total_words,
            "page_count": len(self.pages),
            "login_blocked_count": len(self.login_blocked) + self.restored_login_blocked_count,
            "limit_reached": self.limit_reached,
            "error": self.error,
            "detected_language": self.detected_language,
            "domain_scope": self.domain_scope,
            "language_setting": self.language_setting,
            "estimate_result": self.estimate_result,
            "stopped_reason": self.stopped_reason,
        }


def create_job(source_url: str, user_id: int, max_pages: int) -> Job:
    job = Job(id=uuid.uuid4().hex, source_url=source_url, user_id=user_id, max_pages=max_pages)
    JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)


def restore_job(run) -> Job:
    """Reconstructs an in-memory Job from a checkpointed RunRecord (see
    app.db.get_crawling_runs) so an interrupted crawl can resume from exactly
    where it left off after a server restart."""
    job = Job(
        id=run.id,
        source_url=run.source_url,
        user_id=run.user_id,
        max_pages=float("inf"),
        status="crawling",
        started_at=run.created_at,
        pages={p.url: p for p in run.pages},
        total_words=run.total_words,
        limit_reached=run.limit_reached,
        detected_language=run.language if run.language_auto_detected else None,
        domain_scope=run.domain_scope,
        language_setting=None if run.language_auto_detected else run.language,
        resume_state=run.resume_state,
        restored_login_blocked_count=run.login_blocked_count,
    )
    JOBS[job.id] = job
    return job
