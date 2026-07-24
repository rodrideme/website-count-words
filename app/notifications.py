from __future__ import annotations

import os

import httpx

from app.templates import templates

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


async def _send_email(
    to_email: str,
    subject: str,
    heading: str,
    intro_text: str,
    stats: list[tuple[str, str]],
    cta_url: str | None,
    cta_label: str | None,
) -> None:
    if not MAILGUN_API_KEY or not MAILGUN_DOMAIN:
        return

    html = templates.env.get_template("email_notification.html").render(
        heading=heading, intro_text=intro_text, stats=stats, cta_url=cta_url, cta_label=cta_label,
    )
    text_lines = [intro_text]
    text_lines += [f"{label}: {value}" for label, value in stats]
    if cta_url:
        text_lines.append(f"{cta_label or 'View'}: {cta_url}")
    text = "\n\n".join(text_lines)

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages",
                auth=("api", MAILGUN_API_KEY),
                data={"from": MAILGUN_FROM, "to": to_email, "subject": subject, "text": text, "html": html},
                timeout=10,
            )
    except Exception:
        # Best-effort — a notification failure should never affect the crawl
        # itself, which has already finished and saved its results by now.
        pass


async def send_crawl_notification(
    to_email: str,
    source_url: str,
    status: str,
    total_words: int,
    page_count: int,
    run_id: str,
    error: str | None = None,
    detected_cms: str | None = None,
    confidence: str | None = None,
) -> None:
    subject = f"{_STATUS_SUBJECTS.get(status, 'Crawl update')}: {source_url}"
    heading = _STATUS_SUBJECTS.get(status, "Crawl update")
    intro_text = f"Your crawl of {source_url} is {status}."

    stats: list[tuple[str, str]] = []
    if status == "completed":
        stats.append(("Total words", f"{total_words:,}"))
        stats.append(("Pages counted", f"{page_count:,}"))
        if detected_cms:
            stats.append(("Detected platform", detected_cms))
        if confidence:
            stats.append(("Estimate confidence", confidence.capitalize()))
    elif error:
        stats.append(("Reason", error))

    cta_url = f"{PUBLIC_BASE_URL}/crawl/{run_id}" if PUBLIC_BASE_URL else None
    await _send_email(to_email, subject, heading, intro_text, stats, cta_url, "View full report")


async def send_share_notification(to_email: str, shared_by_email: str, source_url: str, share_url: str) -> None:
    subject = f"{shared_by_email} shared a word-count report with you"
    heading = "A report was shared with you"
    intro_text = f"{shared_by_email} shared their word-count report for {source_url} with you."
    await _send_email(to_email, subject, heading, intro_text, [], share_url, "View shared report")
