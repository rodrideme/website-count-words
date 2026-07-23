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
from app.auth import require_user, require_user_api
from app.crawler import PAUSE_AT_WORDS, run_crawl
from app.job_store import create_job, get_job, restore_job
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
        job = restore_job(run)
        language = job.language_setting or job.detected_language
        asyncio.create_task(
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

    # An estimate always runs fresh — the point is to preview this exact
    # settings combination, even if the site's been crawled before.
    if not payload.force_recrawl and not payload.estimate:
        cached = await db.get_latest_run(source_url)
        if cached is not None:
            return JSONResponse({"cached": True, "run_id": cached.id})

    pause_at_words = PAUSE_AT_WORDS if payload.estimate else None
    job = create_job(source_url=source_url, user_id=user.id, max_pages=max_pages)
    asyncio.create_task(
        run_crawl(job.id, source_url, max_pages, payload.domain_scope, payload.language, pause_at_words=pause_at_words)
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
    asyncio.create_task(
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


@app.post("/crawl/{job_id}/cancel")
async def cancel_crawl(job_id: str, user: User = Depends(require_user_api)):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in _TERMINAL_STATUSES:
        return JSONResponse({"status": job.status})

    job.request_cancel()
    return JSONResponse({"status": "cancelling"})


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
