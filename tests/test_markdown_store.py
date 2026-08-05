"""Tests for the Markdown store.

The ZIP is the part worth testing hard: a framing bug here is invisible until
someone's several-hundred-MB download won't open.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import gzip
import io
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """A fresh store rooted in a temp dir. The module reads its config at import
    time, so point it somewhere disposable and reload."""
    monkeypatch.setenv("MARKDOWN_DIR", str(tmp_path / "markdown"))
    import importlib

    import app.markdown_store as markdown_store

    importlib.reload(markdown_store)
    yield markdown_store
    importlib.reload(markdown_store)


def read_zip(chunks) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(b"".join(chunks)))


# ---------------------------------------------------------------- entry names

def test_entry_name_is_stable_and_suffixed(store):
    name = store.entry_name("https://x.com/blog/post")
    assert name.startswith("x.com/blog/post-")
    assert name.endswith(".md")
    assert name == store.entry_name("https://x.com/blog/post")


def test_entry_name_distinguishes_query_strings(store):
    """Many sites serve different content per query — these must not collide."""
    a = store.entry_name("https://x.com/search?q=cats")
    b = store.entry_name("https://x.com/search?q=dogs")
    assert a != b


def test_entry_name_handles_root_and_trailing_slash(store):
    assert store.entry_name("https://x.com").startswith("x.com/index-")
    assert store.entry_name("https://x.com/").startswith("x.com/index-")


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/a:b/c*d?e",
        "https://x.com/../../etc/passwd",
        "https://x.com/./././a",
        "https://x.com/trailing.../",
        "https://x.com/" + "y" * 400,
        "https://x.com/日本語/ページ",
        "https://x.com/a b/c%20d",
    ],
)
def test_entry_names_are_safe(store, url):
    name = store.entry_name(url)
    assert not name.startswith("/")
    assert ".." not in name.split("/")
    assert not set(name) & set('\\:*?"<>|')
    assert len(name.encode()) <= 220
    for component in name.split("/"):
        assert len(component.encode()) <= 255
        assert component == component.rstrip(". ")


def test_entry_names_unique_across_many_similar_urls(store):
    urls = [f"https://x.com/{'a' * 300}/{i}" for i in range(500)]
    assert len({store.entry_name(u) for u in urls}) == 500


# -------------------------------------------------------------------- writing

def test_write_then_read_round_trip(store):
    n = store.write("run1", "https://x.com/a", "# Hello\n\nWorld")
    assert n > 0
    assert store.has_page("run1", "https://x.com/a")
    assert store.run_bytes("run1") == n


def test_write_is_idempotent(store):
    """A page re-crawled after a crash must overwrite, not duplicate."""
    store.write("run1", "https://x.com/a", "first")
    first = store.run_bytes("run1")
    store.write("run1", "https://x.com/a", "first")
    assert store.run_bytes("run1") == first

    store.write("run1", "https://x.com/a", "second version, longer text here")
    names = list(zipfile.ZipFile(io.BytesIO(b"".join(
        store.iter_zip("run1", ["https://x.com/a"])))).namelist())
    assert len(names) == 1


def test_gzip_header_is_exactly_ten_bytes(store):
    """The forward path to a no-recompression ZIP depends on this: no filename
    field, no mtime, so the deflate stream starts at a known offset."""
    store.write("run1", "https://x.com/a", "some content")
    raw = next(Path(store.MARKDOWN_DIR / "run1").rglob("*.md.gz")).read_bytes()
    assert raw[:2] == b"\x1f\x8b"
    assert raw[3] == 0, "FLG must be 0 — no filename, no extra fields"
    assert raw[4:8] == b"\x00\x00\x00\x00", "mtime must be zeroed"
    assert gzip.decompress(raw).startswith(b"<!-- source: https://x.com/a -->")


def test_source_url_is_recoverable_from_content(store):
    """A mangled filename must never lose the real URL."""
    url = "https://x.com/" + "z" * 400 + "?q=1"
    store.write("run1", url, "body")
    body = b"".join(store.iter_zip("run1", [url]))
    content = read_zip([body]).read(store.entry_name(url)).decode()
    assert f"<!-- source: {url} -->" in content


def test_oversized_page_is_truncated_not_rejected(store):
    store.write("run1", "https://x.com/big", "x " * 4_000_000)
    body = read_zip(store.iter_zip("run1", ["https://x.com/big"])).read(
        store.entry_name("https://x.com/big"))
    assert len(body) <= store.MAX_PAGE_BYTES + 100
    assert body.endswith(b"<!-- truncated -->\n")


# ------------------------------------------------------------------- the ZIP

def test_zip_round_trips_every_entry(store):
    pages = {f"https://x.com/p{i}": f"# Page {i}\n\ncontent {i}" for i in range(50)}
    for url, text in pages.items():
        store.write("run1", url, text)

    archive = read_zip(store.iter_zip("run1", list(pages)))
    assert archive.testzip() is None
    assert len(archive.namelist()) == len(pages)
    for url, text in pages.items():
        assert text in archive.read(store.entry_name(url)).decode()


def test_zip_is_valid_with_unicode_and_awkward_urls(store):
    urls = [
        "https://x.com/日本語/ページ",
        "https://x.com/a b/c d",
        "https://x.com/search?q=cats&p=2",
        "https://x.com/",
        "https://x.com/" + "y" * 400,
    ]
    for i, url in enumerate(urls):
        store.write("run1", url, f"content {i}")

    archive = read_zip(store.iter_zip("run1", urls))
    assert archive.testzip() is None
    assert len(archive.namelist()) == len(urls)
    for i, url in enumerate(urls):
        assert f"content {i}" in archive.read(store.entry_name(url)).decode()
    for info in archive.infolist():
        # Bit 3 = data descriptor, which is how ZipFile writes to a stream it
        # can't seek — the whole reason the archive can be produced lazily.
        assert info.flag_bits & 0x08
        # Bit 11 marks a UTF-8 name; ZipFile sets it only where it's needed.
        if not info.filename.isascii():
            assert info.flag_bits & 0x800


def test_zip_beyond_65535_entries_uses_zip64(store):
    """The classic end-of-central-directory record counts entries in 16 bits."""
    count = 70_000
    urls = [f"https://x.com/p{i}" for i in range(count)]
    for url in urls:
        store.write("run1", url, "x")

    archive = read_zip(store.iter_zip("run1", urls))
    assert len(archive.namelist()) == count
    assert len(set(archive.namelist())) == count


def test_zip_skips_pages_that_were_never_captured(store):
    """Capture may have stopped at a cap partway through the run."""
    store.write("run1", "https://x.com/a", "kept")
    urls = ["https://x.com/a", "https://x.com/never-captured"]

    archive = read_zip(store.iter_zip("run1", urls))
    assert archive.testzip() is None
    assert archive.namelist() == [store.entry_name("https://x.com/a")]


def test_zip_survives_a_corrupt_file(store):
    store.write("run1", "https://x.com/a", "fine")
    store.write("run1", "https://x.com/b", "also fine")
    bad = next(Path(store.MARKDOWN_DIR / "run1").rglob("*.md.gz"))
    bad.write_bytes(b"not gzip at all")

    archive = read_zip(store.iter_zip("run1", ["https://x.com/a", "https://x.com/b"]))
    assert archive.testzip() is None
    assert len(archive.namelist()) == 1


def test_zip_includes_readme_when_given(store):
    store.write("run1", "https://x.com/a", "body")
    archive = read_zip(store.iter_zip("run1", ["https://x.com/a"], readme="partial export"))
    assert "partial export" in archive.read("_README.txt").decode()


def test_zip_of_an_empty_run_is_still_a_valid_archive(store):
    archive = read_zip(store.iter_zip("run1", []))
    assert archive.testzip() is None
    assert archive.namelist() == []


def test_zip_opens_with_the_system_unzip(store, tmp_path):
    """zipfile reading what zipfile wrote proves less than a foreign reader."""
    if not shutil_which("unzip"):
        pytest.skip("unzip not available")
    urls = [f"https://x.com/p{i}" for i in range(100)] + ["https://x.com/日本語"]
    for url in urls:
        store.write("run1", url, f"# {url}\n\nbody")

    out = tmp_path / "out.zip"
    out.write_bytes(b"".join(store.iter_zip("run1", urls)))
    # errors="replace": unzip prints entry names in the filesystem encoding,
    # which isn't valid UTF-8 for the non-ASCII ones.
    result = subprocess.run(
        ["unzip", "-t", str(out)], capture_output=True, text=True, errors="replace"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No errors detected" in result.stdout


def shutil_which(name):
    import shutil

    return shutil.which(name)


# ---------------------------------------------------------------- housekeeping

def test_delete_removes_the_run_and_leaves_others(store):
    store.write("run1", "https://x.com/a", "a")
    store.write("run2", "https://x.com/b", "b")
    store.delete("run1")
    assert not store.has_page("run1", "https://x.com/a")
    assert store.has_page("run2", "https://x.com/b")
    assert store.existing_run_ids() == ["run2"]


def test_delete_of_an_unknown_run_is_not_an_error(store):
    store.delete("never-existed")


def test_total_bytes_sums_across_runs(store):
    a = store.write("run1", "https://x.com/a", "a" * 500)
    b = store.write("run2", "https://x.com/b", "b" * 500)
    assert store.total_bytes() == a + b


def test_disk_floor_reports_tight_when_the_floor_is_huge(store, monkeypatch):
    monkeypatch.setattr(store, "DISK_FLOOR_BYTES", 1 << 62)
    assert store.disk_is_tight() is True
    monkeypatch.setattr(store, "DISK_FLOOR_BYTES", 0)
    assert store.disk_is_tight() is False
