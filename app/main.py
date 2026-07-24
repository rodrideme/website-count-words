from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import auth, db
from app.auth import require_admin, require_user, require_user_api
from app.crawler import PAUSE_AT_WORDS, run_crawl
from app.job_store import create_job, get_job, list_active_jobs, restore_job
from app.models import CrawlRequest, User
from app.templates import templates

_TERMINAL_STATUSES = ("completed", "failed", "cancelled", "paused")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    # Any run still marked "crawling" here was interrupted by a crash/restart
    # (JOBS is always empty on a fresh process) — pick each one back up from
    # its last checkpoint rather than leaving it stuck forever.
    for run in await db.get_crawling_runs():
        if not await db.claim_crawling_run(run.id):
            # Another process/instance already claimed this one — skip it.
            continue
        job = restore_job(run)
        language = job.language_setting or job.detected_language
        job.task = asyncio.create_task(
            run_crawl(job.id, job.source_url, job.max_pages, job.domain_scope, language, resume_state=job.resume_state)
        )
    yield
    await db.close_db()


app = FastAPI(lifespan=lifespan)
# Render sets RENDER=true on every service; mark the session cookie Secure
# only there so local dev over plain http:// still works.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ["SESSION_SECRET"],
    https_only=bool(os.environ.get("RENDER")),
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth.router)


def _valid_url(url: str) -> bool:
    parts = urlsplit(url.strip())
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _sse(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


@app.get("/")
async def index(request: Request, user: User = Depends(require_user)):
    recent_runs = await db.list_recent_runs(user.id)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "recent_runs": recent_runs,
        },
    )


@app.post("/crawl")
async def start_crawl(payload: CrawlRequest, user: User = Depends(require_user_api)):
    url = payload.url.strip()
    if not _valid_url(url):
        raise HTTPException(status_code=400, detail="Please enter a valid http(s) URL")

    max_pages = float("inf")

    source_url = db.normalize_url(url)

    if not payload.force_recrawl:
        cached = await db.get_latest_run(source_url)
        if cached is not None:
            return JSONResponse({"cached": True, "run_id": cached.id})

    job = create_job(source_url=source_url, user_id=user.id, max_pages=max_pages)
    job.task = asyncio.create_task(
        run_crawl(job.id, source_url, max_pages, payload.domain_scope, payload.language, pause_at_words=PAUSE_AT_WORDS)
    )
    return JSONResponse({"cached": False, "run_id": job.id})


@app.post("/crawl/{job_id}/resume")
async def resume_crawl(job_id: str, user: User = Depends(require_user_api)):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != "paused":
        return JSONResponse({"status": job.status})

    language = job.language_setting or job.detected_language
    job.estimate_result = None
    job.task = asyncio.create_task(
        run_crawl(job.id, job.source_url, float("inf"), job.domain_scope, language, resume_state=job.resume_state)
    )
    return JSONResponse({"status": "resuming"})


@app.get("/crawl/{run_id}")
async def crawl_page(run_id: str, request: Request, user: User = Depends(require_user)):
    job = get_job(run_id)
    if job is not None:
        return templates.TemplateResponse(
            request,
            "crawl.html",
            {
                "mode": "live",
                "run_id": run_id,
                "source_url": job.source_url,
                "started_at": job.started_at,
                "initial_status_payload": job.status_payload(),
            },
        )

    run = await db.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Crawl not found")

    return templates.TemplateResponse(
        request,
        "crawl.html",
        {
            "mode": "past",
            "run_id": run_id,
            "source_url": run.source_url,
            "run": run,
            "initial_pages": [p.model_dump() for p in run.pages],
        },
    )


async def _cancel_job(job_id: str) -> str:
    """Cancels a job regardless of which process is actually running it
    (JOBS isn't shared across processes) — sets the in-memory flag if we
    happen to have it locally, but always also persists to the DB so
    whichever process is really running it picks this up on its next poll
    (see crawler.py's _should_cancel). Raises 404 only if the job is
    entirely unknown, both here and in the DB; otherwise returns its
    resulting status (a no-op "as-is" status for an already-terminal job)."""
    job = get_job(job_id)
    if job is not None:
        if job.status in _TERMINAL_STATUSES:
            return job.status
        job.request_cancel()
    else:
        run = await db.get_run(job_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Job not found")
        if run.status in _TERMINAL_STATUSES:
            return run.status

    await db.request_cancel(job_id)
    return "cancelling"


@app.post("/crawl/{job_id}/cancel")
async def cancel_crawl(job_id: str, user: User = Depends(require_user_api)):
    status = await _cancel_job(job_id)
    return JSONResponse({"status": status})


@app.get("/events/{job_id}")
async def crawl_events(job_id: str, request: Request, user: User = Depends(require_user_api)):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")

    async def event_stream():
        for page in job.pages.values():
            yield _sse("page", {"type": "page", "page": page.model_dump(), "total_words": job.total_words})
        yield _sse("status", job.status_payload())

        if job.status in _TERMINAL_STATUSES:
            return

        queue: asyncio.Queue = asyncio.Queue()
        job.subscribers.append(queue)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield _sse(event["type"], event)
                if event["type"] == "status" and event.get("status") in _TERMINAL_STATUSES:
                    break
        finally:
            if queue in job.subscribers:
                job.subscribers.remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _error_pct(estimated: int, actual: int | None) -> float | None:
    if not actual:
        return None
    return round((estimated - actual) / actual * 100, 1)


def _aggregate_estimate_errors(rows: list[dict], group_key: str) -> dict[str, dict]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        if row["word_error_pct"] is None:
            continue
        key = row[group_key] or "(none)"
        groups.setdefault(key, []).append(row["word_error_pct"])
    return {
        key: {
            "count": len(errors),
            "avg_signed_pct": round(sum(errors) / len(errors), 1),
            "avg_abs_pct": round(sum(abs(e) for e in errors) / len(errors), 1),
        }
        for key, errors in groups.items()
    }


@app.get("/admin/estimates")
async def admin_estimates(request: Request, admin: User = Depends(require_admin)):
    rows = await db.list_estimate_history()
    for row in rows:
        row["word_error_pct"] = _error_pct(row["estimated_total_words"], row["actual_total_words"])
        row["page_error_pct"] = _error_pct(row["estimated_total_pages"], row["actual_total_pages"])

    return templates.TemplateResponse(
        request,
        "admin_estimates.html",
        {
            "rows": rows,
            "by_confidence": _aggregate_estimate_errors(rows, "confidence"),
            "by_cms": _aggregate_estimate_errors(rows, "detected_cms"),
        },
    )


@app.get("/admin/jobs")
async def admin_jobs(request: Request, admin: User = Depends(require_admin)):
    jobs = []
    for job in list_active_jobs():
        owner = await db.get_user(job.user_id)
        jobs.append(
            {
                "id": job.id,
                "source_url": job.source_url,
                "status": job.status,
                "owner_email": owner.email if owner else "(unknown)",
                "started_at": job.started_at,
                "page_count": len(job.pages),
                "total_words": job.total_words,
            }
        )
    return templates.TemplateResponse(request, "admin_jobs.html", {"jobs": jobs})


@app.post("/admin/jobs/{job_id}/cancel")
async def admin_cancel_job(job_id: str, admin: User = Depends(require_admin)):
    status = await _cancel_job(job_id)
    return JSONResponse({"status": status})


@app.post("/admin/jobs/cancel-all")
async def admin_cancel_all(admin: User = Depends(require_admin)):
    cancelled = [job.id for job in list_active_jobs()]
    for job_id in cancelled:
        await _cancel_job(job_id)
    return JSONResponse({"cancelled": cancelled})
