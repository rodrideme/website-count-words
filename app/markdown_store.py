"""On-disk store for the Markdown crawl4ai already produces per page.

One gzipped file per page under MARKDOWN_DIR:

    <root>/<run_id>/<ab>/<name>-<hash8>.md.gz

The alternatives were both worse here. A SQLite table would never give the disk
space back when a run is deleted — that needs VACUUM, which needs twice the
database size free, which is exactly what you don't have on a volume that just
filled up. An incrementally-written archive would need crash-recovery offsets and
truncate-on-resume, and a half-written one is unreadable.

Separate files are idempotent instead: the name is a pure function of the URL, so
a page re-crawled after a crash overwrites its own file. The crawl loop already
skips URLs it has seen (crawler.py), and anything crawled after the last
checkpoint is simply crawled again, so no offsets need persisting.

Nothing here holds a page in memory beyond the one being written or read.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

MARKDOWN_DIR = Path(os.environ.get("MARKDOWN_DIR") or (Path(os.environ.get("DB_PATH", "data/wordcount.db")).parent / "markdown"))

MAX_RUN_BYTES = int(os.environ.get("MARKDOWN_MAX_RUN_MB", "300")) * 1024 * 1024
MAX_TOTAL_BYTES = int(os.environ.get("MARKDOWN_MAX_TOTAL_MB", "6000")) * 1024 * 1024
DISK_FLOOR_BYTES = int(os.environ.get("MARKDOWN_DISK_FLOOR_MB", "512")) * 1024 * 1024

# One page can't be allowed to eat a whole run's budget.
MAX_PAGE_BYTES = 5 * 1024 * 1024

# Windows caps a full path at 260 chars by default and most filesystems cap a
# single component at 255 bytes; stay well inside both.
_MAX_COMPONENT = 100
_MAX_NAME = 180

_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_ZIP_CHUNK = 1024 * 1024


def _run_dir(run_id: str) -> Path:
    return MARKDOWN_DIR / run_id


def _digest(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", "replace")).hexdigest()


def entry_name(url: str) -> str:
    """A ZIP entry name for a URL: readable, safe on every OS, and unique.

    The 8-char digest is appended unconditionally rather than only on collision.
    Tracking which names were already used would mean holding every name for the
    run in memory — about 30MB on a 164k-page crawl, against a memory ceiling
    that cancels crawls when it's hit.
    """
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s and s not in (".", "..")]

    cleaned = []
    for segment in segments:
        segment = _UNSAFE_RE.sub("-", segment).rstrip(". ").strip()
        if segment:
            cleaned.append(segment[:_MAX_COMPONENT])

    host = _UNSAFE_RE.sub("-", parts.netloc or "site").rstrip(". ") or "site"
    stem = "/".join([host[:_MAX_COMPONENT], *cleaned]) if cleaned else f"{host[:_MAX_COMPONENT]}/index"
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    return f"{stem[:_MAX_NAME]}-{_digest(url)[:8]}.md"


def _path_for(run_id: str, url: str) -> Path:
    digest = _digest(url)
    return _run_dir(run_id) / digest[:2] / f"{Path(entry_name(url)).name}.gz"


def disk_is_tight() -> bool:
    """True when the volume is close enough to full that the database — which
    shares it — needs the remaining room more than this feature does."""
    try:
        MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(MARKDOWN_DIR).free < DISK_FLOOR_BYTES
    except OSError:
        return True


def write(run_id: str, url: str, text: str) -> int:
    """Stores one page, returning the compressed bytes written.

    mtime=0 keeps the gzip header a fixed 10 bytes with no filename field. That
    matters beyond determinism: gzip is a 10-byte header, a raw deflate stream,
    then a trailer holding CRC-32 and the uncompressed size — exactly what a ZIP
    local header needs. A later version can copy those deflate payloads straight
    into ZIP entries with no recompression, without restating anything on disk.
    """
    body = f"<!-- source: {url} -->\n\n{text}".encode("utf-8", "replace")
    if len(body) > MAX_PAGE_BYTES:
        body = body[:MAX_PAGE_BYTES] + b"\n\n<!-- truncated -->\n"

    blob = gzip.compress(body, mtime=0)
    path = _path_for(run_id, url)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written whole via a temp file in the same directory, so a crash mid-write
    # can't leave a half-gzip that fails to decompress at download time.
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(blob)
    tmp.replace(path)
    return len(blob)


def has_page(run_id: str, url: str) -> bool:
    return _path_for(run_id, url).exists()


def run_bytes(run_id: str) -> int:
    total = 0
    for path in _run_dir(run_id).rglob("*.md.gz"):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def total_bytes() -> int:
    total = 0
    if not MARKDOWN_DIR.exists():
        return 0
    for path in MARKDOWN_DIR.rglob("*.md.gz"):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def delete(run_id: str) -> None:
    shutil.rmtree(_run_dir(run_id), ignore_errors=True)


def existing_run_ids() -> list[str]:
    if not MARKDOWN_DIR.exists():
        return []
    return [p.name for p in MARKDOWN_DIR.iterdir() if p.is_dir()]


class _Sink(io.RawIOBase):
    """Collects what ZipFile writes so the generator below can hand it onward.

    ZipFile accepts a stream it can't seek — it detects the missing tell() and
    emits data descriptors instead of seeking back to patch local headers — so
    the archive can be produced without ever holding it whole.
    """

    def __init__(self):
        self.buffer = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.buffer.extend(data)
        return len(data)

    def take(self) -> bytes:
        data = bytes(self.buffer)
        del self.buffer[:]
        return data


def iter_zip(run_id: str, urls, readme: str = ""):
    """Yields a ZIP of this run's stored pages, a chunk at a time.

    Yields are batched rather than emitted per entry: each one is its own ASGI
    message, and at 164k entries that overhead alone dominates the response.
    """
    sink = _Sink()
    # allowZip64 is the default, and required — 164k entries overflows the
    # 16-bit entry count in a classic end-of-central-directory record.
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        if readme:
            archive.writestr("_README.txt", readme)

        for url in urls:
            path = _path_for(run_id, url)
            try:
                body = gzip.decompress(path.read_bytes())
            except (OSError, EOFError, gzip.BadGzipFile):
                # A page that was never captured, or a file lost underneath us,
                # shouldn't cost the reader the other 164,000.
                continue
            archive.writestr(entry_name(url), body)

            if len(sink.buffer) >= _ZIP_CHUNK:
                yield sink.take()

    yield sink.take()
