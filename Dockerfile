FROM python:3.12-slim

WORKDIR /app

# System libraries Chromium needs to actually run (fonts, graphics libs, etc.)
# playwright install --with-deps installs these via apt on Debian/Ubuntu.
COPY requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && crawl4ai-setup

COPY . .

ENV PYTHONUNBUFFERED=1

# Render injects $PORT; --proxy-headers makes Starlette trust Render's
# X-Forwarded-Proto so OAuth redirect URLs come out as https:// instead of
# http:// (Render terminates TLS in front of the container).
# --workers 1 is required, not just a default: this app keeps crawl/job state
# in in-process memory (see app/job_store.py), so more than one worker means
# requests get routed to processes that don't share that state. Explicit here
# because uvicorn otherwise falls back to $WEB_CONCURRENCY if it's set, which
# Render sets automatically based on instance size (e.g. 2 on a 2-CPU plan).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
