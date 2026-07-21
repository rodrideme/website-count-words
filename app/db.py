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
) -> None:
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    pages_json = json.dumps([p.model_dump() for p in pages])
    await conn.execute(
        """
        INSERT OR REPLACE INTO runs
            (id, source_url, user_id, created_at, status, total_words, page_count, limit_reached,
             login_blocked_count, domain_scope, language, language_auto_detected, pages_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id, source_url, user_id, now, status, total_words, len(pages), int(limit_reached),
            login_blocked_count, domain_scope, language, int(language_auto_detected), pages_json,
        ),
    )
    await conn.commit()


async def list_recent_runs(user_id: int, limit: int = 10) -> list[RunRecord]:
    conn = _conn()
    async with conn.execute(
        "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (user_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [_row_to_run(row) for row in rows]
