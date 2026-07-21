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

    def request_cancel(self) -> None:
        # crawl4ai's BFS strategy is given a should_cancel callback that reads
        # this flag on every check, so setting it is enough regardless of
        # whether the crawl has actually started yet (see crawler.py).
        self.cancel_requested = True

    def publish(self, event: dict) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def status_payload(self) -> dict:
        return {
            "type": "status",
            "status": self.status,
            "total_words": self.total_words,
            "page_count": len(self.pages),
            "login_blocked_count": len(self.login_blocked),
            "limit_reached": self.limit_reached,
            "error": self.error,
            "detected_language": self.detected_language,
            "domain_scope": self.domain_scope,
            "language_setting": self.language_setting,
            "estimate_result": self.estimate_result,
        }


def create_job(source_url: str, user_id: int, max_pages: int) -> Job:
    job = Job(id=uuid.uuid4().hex, source_url=source_url, user_id=user_id, max_pages=max_pages)
    JOBS[job.id] = job
    return job


def get_job(job_id: str) -> Job | None:
    return JOBS.get(job_id)
