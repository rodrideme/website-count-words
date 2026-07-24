from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class User(BaseModel):
    id: int
    google_sub: str
    email: str
    name: str
    picture: str | None = None


class PageResult(BaseModel):
    url: str
    title: str = ""
    word_count: int = 0
    success: bool = True
    login_required: bool = False
    blocked_by_host: bool = False
    error: str | None = None


class RunRecord(BaseModel):
    id: str
    source_url: str
    user_id: int
    created_at: str
    status: str
    total_words: int
    page_count: int
    limit_reached: bool
    login_blocked_count: int = 0
    domain_scope: str = "all"
    language: str | None = None
    language_auto_detected: bool = False
    resume_state: dict | None = None
    pages: list[PageResult]


class CrawlRequest(BaseModel):
    url: str
    domain_scope: Literal["all", "subdomain_only", "top_domain_only"] = "all"
    language: str | None = None
    force_recrawl: bool = False
