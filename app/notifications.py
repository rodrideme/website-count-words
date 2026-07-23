from __future__ import annotations

import os

import httpx

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN")
MAILGUN_FROM = os.environ.get("MAILGUN_FROM") or (
    f"Word Counter <noreply@{MAILGUN_DOMAIN}>" if MAILGUN_DOMAIN else None
)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

_STATUS_SUBJECTS = {
    "completed": "Crawl finished",
    "failed": "Crawl failed",
    "cancelled": "Crawl stopped",
}


async def send_crawl_notification(
    to_email: str,
    source_url: str,
    status: str,
    total_words: int,
    page_count: int,
    run_id: str,
    error: str | None = None,
) -> None:
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        return

    subject = f"{_STATUS_SUBJECTS.get(status, 'Crawl update')}: {source_url}"
    lines = [f"Your crawl of {source_url} is {status}."]
    if status == "completed":
        lines.append(f"Total words: {total_words:,}")
        lines.append(f"Pages counted: {page_count:,}")
    elif error:
        lines.append(f"Reason: {error}")
    if PUBLIC_BASE_URL:
        lines.append(f"View the full report: {PUBLIC_BASE_URL}/crawl/{run_id}")
    text = "\n\n".join(lines)

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={"from": MAILGUN_FROM, "to": to_email, "subject": subject, "text": text},
                timeout=10,
            )
    except Exception:
        # Best-effort — a notification failure should never affect the crawl
        # itself, which has already finished and saved its results by now.
        pass
