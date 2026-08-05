"""Tests for the resume-state repair.

crawl4ai's BFS marks a whole level visited before fetching any of it, but its
resume snapshot's "pending" only carries the NEXT level. Pausing mid-level —
what the estimate pause does — therefore loses every un-fetched URL in the
current level, and on a well interlinked site "pending" comes back empty, so
the resumed crawl finishes instantly having done nothing.

Run with:  .venv/bin/python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(Path(__file__).resolve().parents[1] / ".env"))

from app.crawler import _recovered_resume_state  # noqa: E402
from app.models import PageResult  # noqa: E402


class FakeJob:
    def __init__(self, resume_state, fetched=(), login_blocked=()):
        self.resume_state = resume_state
        self.pages = {u: PageResult(url=u, title="", word_count=1) for u in fetched}
        self.login_blocked = {u: PageResult(url=u, title="", word_count=0) for u in login_blocked}


def state(visited, pending):
    return {
        "strategy_type": "bfs",
        "visited": list(visited),
        "pending": [{"url": u, "parent_url": None} for u in pending],
        "depths": {},
        "pages_crawled": 0,
    }


def pending_urls(s):
    return sorted(item["url"] for item in s["pending"])


def test_the_reported_bug_a_paused_level_is_recovered():
    """The real shape: a fully interlinked site paused partway through level 1.
    Everything is marked visited, nothing is pending, so without this the
    resumed crawl has no frontier at all."""
    all_urls = [f"https://x.com/p{i}" for i in range(41)]
    fetched = all_urls[:6]
    job = FakeJob(state(visited=all_urls, pending=[]), fetched=fetched)

    recovered = _recovered_resume_state(job)

    assert len(recovered["pending"]) == 35
    assert pending_urls(recovered) == sorted(all_urls[6:])
    assert set(recovered["visited"]) == set(all_urls)


def test_already_fetched_pages_are_not_requeued():
    job = FakeJob(state(visited=["a", "b", "c"], pending=[]), fetched=["a", "b", "c"])
    assert _recovered_resume_state(job)["pending"] == []


def test_login_blocked_pages_count_as_fetched():
    """They produced a result and are deliberately excluded from job.pages, so
    without this they'd be re-crawled forever on every resume."""
    job = FakeJob(state(visited=["a", "b"], pending=[]), fetched=["a"], login_blocked=["b"])
    assert _recovered_resume_state(job)["pending"] == []


def test_existing_pending_is_preserved_and_not_duplicated():
    job = FakeJob(state(visited=["a", "b", "c"], pending=["b"]), fetched=["a"])
    recovered = _recovered_resume_state(job)
    assert pending_urls(recovered) == ["b", "c"]


def test_a_genuinely_finished_level_stays_empty():
    """Every visited URL was fetched — the crawl really is done, and the repair
    must not invent work and turn a completion into an endless loop."""
    job = FakeJob(state(visited=["a", "b"], pending=[]), fetched=["a", "b"])
    recovered = _recovered_resume_state(job)
    assert recovered["pending"] == []
    assert recovered is job.resume_state, "unchanged state should be returned as-is"


def test_other_state_fields_survive():
    s = state(visited=["a", "b"], pending=[])
    s["depths"] = {"a": 0, "b": 1}
    s["pages_crawled"] = 7
    job = FakeJob(s, fetched=["a"])
    recovered = _recovered_resume_state(job)
    assert recovered["depths"] == {"a": 0, "b": 1}
    assert recovered["pages_crawled"] == 7
    assert recovered["strategy_type"] == "bfs"


@pytest.mark.parametrize("empty", [None, {}])
def test_no_state_is_handled(empty):
    assert _recovered_resume_state(FakeJob(empty)) is None


def test_missing_keys_do_not_raise():
    job = FakeJob({"strategy_type": "bfs"}, fetched=["a"])
    assert _recovered_resume_state(job) == {"strategy_type": "bfs"}
