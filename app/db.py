from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiosqlite

from app.models import PageResult, RunRecord, User

DB_PATH = Path(os.environ.get("DB_PATH", "data/wordcount.db"))

_connection: aiosqlite.Connection | None = None


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if scheme == "http" and netloc.endswith(":80"):
        netloc = netloc[: -len(":80")]
    if scheme == "https" and netloc.endswith(":443"):
        netloc = netloc[: -len(":443")]
    path = parts.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


async def init_db() -> None:
    global _connection
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _connection = await aiosqlite.connect(DB_PATH)
    _connection.row_factory = aiosqlite.Row
    await _connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            name TEXT NOT NULL,
            picture TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL,
            total_words INTEGER NOT NULL,
            page_count INTEGER NOT NULL,
            limit_reached INTEGER NOT NULL,
            pages_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_runs_source_url ON runs(source_url);
        CREATE INDEX IF NOT EXISTS idx_runs_user_id ON runs(user_id);

        CREATE TABLE IF NOT EXISTS estimate_history (
            run_id TEXT PRIMARY KEY,
            source_url TEXT NOT NULL,
            created_at TEXT NOT NULL,
            pages_fetched INTEGER NOT NULL,
            discovered_total INTEGER NOT NULL,
            sitemap_count INTEGER,
            sitemap_found INTEGER NOT NULL,
            detected_cms TEXT,
            confidence TEXT NOT NULL,
            estimated_total_pages INTEGER NOT NULL,
            estimated_total_words INTEGER NOT NULL,
            actual_total_pages INTEGER,
            actual_total_words INTEGER,
            completed_at TEXT
        );
        """
    )
    await _connection.commit()
    await _ensure_columns()


async def _ensure_columns() -> None:
    """Lightweight migration so existing local databases pick up new columns
    without wiping previously-saved runs."""
    conn = _conn()
    cur = await conn.execute("PRAGMA table_info(runs)")
    existing = {row["name"] for row in await cur.fetchall()}
    if "login_blocked_count" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN login_blocked_count INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "domain_scope" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN domain_scope TEXT NOT NULL DEFAULT 'all'")
        await conn.commit()
    if "language" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN language TEXT")
        await conn.commit()
    if "language_auto_detected" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN language_auto_detected INTEGER NOT NULL DEFAULT 0")
        await conn.commit()
    if "resume_state_json" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN resume_state_json TEXT")
        await conn.commit()
    if "cancel_requested" not in existing:
        await conn.execute("ALTER TABLE runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
        await conn.commit()


async def close_db() -> None:
    global _connection
    if _connection is not None:
        await _connection.close()
        _connection = None


def _conn() -> aiosqlite.Connection:
    if _connection is None:
        raise RuntimeError("Database not initialized — call init_db() at startup")
    return _connection


async def get_or_create_user(google_sub: str, email: str, name: str, picture: str | None) -> User:
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,)) as cur:
        row = await cur.fetchone()
    if row is not None:
        return User(id=row["id"], google_sub=row["google_sub"], email=row["email"], name=row["name"], picture=row["picture"])

    now = datetime.now(timezone.utc).isoformat()
    cur = await conn.execute(
        "INSERT INTO users (google_sub, email, name, picture, created_at) VALUES (?, ?, ?, ?, ?)",
        (google_sub, email, name, picture, now),
    )
    await conn.commit()
    return User(id=cur.lastrowid, google_sub=google_sub, email=email, name=name, picture=picture)


async def get_user(user_id: int) -> User | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return User(id=row["id"], google_sub=row["google_sub"], email=row["email"], name=row["name"], picture=row["picture"])


def _row_to_run(row: aiosqlite.Row) -> RunRecord:
    pages = [PageResult(**p) for p in json.loads(row["pages_json"])]
    resume_state_json = row["resume_state_json"]
    return RunRecord(
        id=row["id"],
        source_url=row["source_url"],
        user_id=row["user_id"],
        created_at=row["created_at"],
        status=row["status"],
        total_words=row["total_words"],
        page_count=row["page_count"],
        limit_reached=bool(row["limit_reached"]),
        login_blocked_count=row["login_blocked_count"],
        domain_scope=row["domain_scope"],
        language=row["language"],
        language_auto_detected=bool(row["language_auto_detected"]),
        resume_state=json.loads(resume_state_json) if resume_state_json else None,
        pages=pages,
    )


async def get_latest_run(source_url: str) -> RunRecord | None:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM runs WHERE source_url = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
        (source_url,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def get_run(run_id: str) -> RunRecord | None:
    conn = _conn()
    async with conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_run(row) if row else None


async def save_run(
    run_id: str,
    source_url: str,
    user_id: int,
    status: str,
    total_words: int,
    pages: list[PageResult],
    limit_reached: bool,
    login_blocked_count: int = 0,
    domain_scope: str = "all",
    language: str | None = None,
    language_auto_detected: bool = False,
    resume_state: dict | None = None,
) -> None:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    pages_json = json.dumps([p.model_dump() for p in pages])
    resume_state_json = json.dumps(resume_state) if resume_state is not None else None
    await conn.execute(
        """
        INSERT INTO runs
            (id, source_url, user_id, created_at, status, total_words, page_count, limit_reached,
             login_blocked_count, domain_scope, language, language_auto_detected, resume_state_json, pages_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            status=excluded.status,
            total_words=excluded.total_words,
            page_count=excluded.page_count,
            limit_reached=excluded.limit_reached,
            login_blocked_count=excluded.login_blocked_count,
            domain_scope=excluded.domain_scope,
            language=excluded.language,
            language_auto_detected=excluded.language_auto_detected,
            resume_state_json=excluded.resume_state_json,
            pages_json=excluded.pages_json
        """,
        (
            # created_at is only ever set on first insert (see ON CONFLICT above) —
            # periodic checkpointing during a crawl must not keep bumping it forward.
            run_id, source_url, user_id, now, status, total_words, len(pages), int(limit_reached),
            login_blocked_count, domain_scope, language, int(language_auto_detected), resume_state_json, pages_json,
        ),
    )
    await conn.commit()


async def get_crawling_runs() -> list[RunRecord]:
    """Runs still marked "crawling" at startup are, by definition, orphaned —
    JOBS is always empty on a fresh process, so nothing else could still be
    running one. Used to auto-resume crawls interrupted by a crash/restart.

    First resets any rows stuck at "resuming" (claimed by claim_crawling_run,
    then crashed again before reaching a checkpoint) back to "crawling" so
    they're eligible again — safe to run redundantly if multiple processes
    boot at once, since the end state is identical either way."""
    conn = _conn()
    await conn.execute("UPDATE runs SET status = 'crawling' WHERE status = 'resuming'")
    await conn.commit()
    async with conn.execute("SELECT * FROM runs WHERE status = 'crawling'") as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]


async def claim_crawling_run(run_id: str) -> bool:
    """Atomically claims an orphaned run for auto-resume, so if more than one
    process/instance races to resume the same run on startup, only one wins.
    Must only ever match the exact "crawling" state (not e.g. "resuming" too)
    — SQLite serializes this UPDATE across processes sharing the same DB
    file, so whichever one flips the row first leaves nothing for the other
    to match."""
    conn = _conn()
    cur = await conn.execute(
        "UPDATE runs SET status = 'resuming' WHERE id = ? AND status = 'crawling'",
        (run_id,),
    )
    await conn.commit()
    return cur.rowcount == 1


async def request_cancel(run_id: str) -> None:
    """Persists a cancel request to the shared DB (not just in-memory), so it
    reaches whichever process is actually running the crawl even if the HTTP
    request that triggered it landed on a different one. A no-op if the run
    has no row yet (cancelled before its first checkpoint) — the in-memory
    flag set alongside this call covers that narrow window instead."""
    conn = _conn()
    await conn.execute("UPDATE runs SET cancel_requested = 1 WHERE id = ?", (run_id,))
    await conn.commit()


async def is_cancel_requested(run_id: str) -> bool:
    conn = _conn()
    async with conn.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)) as cur:
        row = await cur.fetchone()
    return bool(row["cancel_requested"]) if row else False


async def save_estimate_snapshot(run_id: str, source_url: str, estimate_result: dict) -> None:
    """Called exactly once per run, right when it pauses and computes an
    estimate — a run only ever pauses once (resuming never sets
    pause_at_words again), so there's nothing to upsert against here."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        """
        INSERT INTO estimate_history
            (run_id, source_url, created_at, pages_fetched, discovered_total, sitemap_count,
             sitemap_found, detected_cms, confidence, estimated_total_pages, estimated_total_words)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, source_url, now,
            estimate_result["pages_fetched"], estimate_result["discovered_total"], estimate_result["sitemap_count"],
            int(estimate_result["sitemap_found"]), estimate_result["detected_cms"], estimate_result["confidence"],
            estimate_result["total_pages_estimate"], estimate_result["estimated_total_words"],
        ),
    )
    await conn.commit()


async def record_estimate_actual(run_id: str, actual_total_pages: int, actual_total_words: int) -> None:
    """Safe to call unconditionally whenever a run completes — a no-op
    (affects zero rows) for any run that never paused and so never had an
    estimate snapshot saved in the first place."""
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    await conn.execute(
        "UPDATE estimate_history SET actual_total_pages = ?, actual_total_words = ?, completed_at = ? WHERE run_id = ?",
        (actual_total_pages, actual_total_words, now, run_id),
    )
    await conn.commit()


async def list_estimate_history() -> list[dict]:
    conn = _conn()
    async with conn.execute("SELECT * FROM estimate_history ORDER BY created_at DESC") as cur:
        rows = await cur.fetchall()
    return [dict(row) for row in rows]


async def list_recent_runs(user_id: int, limit: int = 10) -> list[RunRecord]:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]
