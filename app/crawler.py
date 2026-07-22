from __future__ import annotations

import re
from urllib.parse import urlparse

from crawl4ai import (
    AsyncUrlSeeder,
    AsyncWebCrawler,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    PruningContentFilter,
    SeedingConfig,
)
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import DomainFilter, FilterChain, URLFilter
from crawl4ai.utils import get_base_domain

from app import db
from app.job_store import get_job
from app.models import PageResult
from app.word_count import count_words


def _markdown_text(result) -> str:
    markdown = getattr(result, "markdown", None)
    if markdown is None:
        return ""
    if isinstance(markdown, str):
        return markdown
    # fit_markdown (main-content only, via the PruningContentFilter below) is
    # far closer to visible article text than raw_markdown's full-page dump.
    fit = getattr(markdown, "fit_markdown", None)
    if fit and not fit.startswith("Error generating fit markdown"):
        return fit
    return getattr(markdown, "raw_markdown", None) or ""


_IMAGE_MARKDOWN_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_MARKDOWN_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_LEADING_MARKUP_RE = re.compile(r"^\s*(#{1,6}|[-*+]|\d+\.|>)\s+", re.MULTILINE)


def clean_markdown_for_counting(text: str) -> str:
    """Strips markdown syntax a browser text-selection would never include: image markup, link URLs, heading/bullet markers."""
    text = _IMAGE_MARKDOWN_RE.sub("", text)
    text = _LINK_MARKDOWN_RE.sub(r"\1", text)
    text = _LEADING_MARKUP_RE.sub("", text)
    return text


_LOGIN_KEYWORDS = (
    "log in",
    "login",
    "log-in",
    "sign in",
    "signin",
    "sign-in",
    "please log in",
    "authentication required",
    "you must be logged in",
)


class TopDomainOnlyFilter(URLFilter):
    """Restricts a crawl to the exact registrable domain (optionally with a
    'www.' prefix), rejecting any other subdomain. Crawl4AI's own DomainFilter
    can't express this — its allowed_domains matching treats any subdomain as
    a match by design, which is the opposite of what "top domain only" needs.
    """

    def __init__(self, base_domain: str):
        super().__init__(name="TopDomainOnlyFilter")
        self._base_domain = base_domain

    def apply(self, url: str) -> bool:
        host = urlparse(url).netloc.split(":")[0].lower()
        if host.startswith("www."):
            host = host[4:]
        passed = host == self._base_domain
        self._update_stats(passed)
        return passed


# ISO 639-1 two-letter language codes — a stable, unchanging standard, used
# to recognize language-prefixed path segments (e.g. /en/, /fr/) without
# needing an external lookup.
_ISO_639_1_CODES = {
    "aa", "ab", "ae", "af", "ak", "am", "an", "ar", "as", "av", "ay", "az",
    "ba", "be", "bg", "bh", "bi", "bm", "bn", "bo", "br", "bs",
    "ca", "ce", "ch", "co", "cr", "cs", "cu", "cv", "cy",
    "da", "de", "dv", "dz",
    "ee", "el", "en", "eo", "es", "et", "eu",
    "fa", "ff", "fi", "fj", "fo", "fr", "fy",
    "ga", "gd", "gl", "gn", "gu", "gv",
    "ha", "he", "hi", "ho", "hr", "ht", "hu", "hy", "hz",
    "ia", "id", "ie", "ig", "ii", "ik", "io", "is", "it", "iu",
    "ja", "jv",
    "ka", "kg", "ki", "kj", "kk", "kl", "km", "kn", "ko", "kr", "ks", "ku", "kv", "kw", "ky",
    "la", "lb", "lg", "li", "ln", "lo", "lt", "lu", "lv",
    "mg", "mh", "mi", "mk", "ml", "mn", "mr", "ms", "mt", "my",
    "na", "nb", "nd", "ne", "ng", "nl", "nn", "no", "nr", "nv", "ny",
    "oc", "oj", "om", "or", "os",
    "pa", "pi", "pl", "ps", "pt",
    "qu",
    "rm", "rn", "ro", "ru", "rw",
    "sa", "sc", "sd", "se", "sg", "si", "sk", "sl", "sm", "sn", "so", "sq", "sr", "ss", "st", "su", "sv", "sw",
    "ta", "te", "tg", "th", "ti", "tk", "tl", "tn", "to", "tr", "ts", "tt", "tw", "ty",
    "ug", "uk", "ur", "uz",
    "ve", "vi", "vo",
    "wa", "wo",
    "xh",
    "yi", "yo",
    "za", "zh", "zu",
}


def _lang_code(segment: str) -> str:
    """Normalizes a locale-shaped segment to its base language code,
    e.g. "pt-BR" / "pt_BR" -> "pt"."""
    return segment.lower().split("-")[0].split("_")[0]


def _looks_like_language_segment(segment: str) -> bool:
    code = _lang_code(segment)
    return len(code) == 2 and code in _ISO_639_1_CODES


def parse_languages(language: str | None) -> list[str]:
    """Parses the language field's comma-separated text into a list of
    normalized codes, e.g. "en, pt, es, fr" -> ["en", "pt", "es", "fr"]."""
    if not language:
        return []
    return [_lang_code(part) for part in language.split(",") if part.strip()]


class LanguageFilter(URLFilter):
    """Restricts a crawl to one or more languages, for sites that publish the
    same content under multiple /xx/ path prefixes. A URL is rejected only if
    its first path segment looks like a language code that ISN'T in the kept
    set — a segment that doesn't look like a language code at all is always
    allowed through, so this works whether or not the site's primary/default
    language has its own prefix.

    Heuristic, not exact: a two-letter path segment that happens to coincide
    with an ISO 639-1 code but isn't actually a language marker (e.g. a
    country section, an unrelated product code) could be misclassified —
    same trade-off as this app's other content heuristics (login-wall and
    anti-bot detection).
    """

    def __init__(self, keep_languages: list[str]):
        super().__init__(name="LanguageFilter")
        self._keep = {_lang_code(l) for l in keep_languages if l.strip()}

    def apply(self, url: str) -> bool:
        segments = urlparse(url).path.split("/")
        first = next((s for s in segments if s), "")
        if not _looks_like_language_segment(first):
            passed = True
        else:
            passed = _lang_code(first) in self._keep
        self._update_stats(passed)
        return passed


_HTML_LANG_RE = re.compile(r'<html[^>]+\blang=["\']([a-zA-Z0-9-]+)["\']', re.IGNORECASE)


def _detect_page_language(html: str | None) -> str | None:
    """Reads the page's own declared language (the same <html lang="..">
    signal search engines and browsers rely on) so a blank language field
    can mean "restrict to whatever language this page is in" instead of
    "no restriction at all"."""
    match = _HTML_LANG_RE.search(html or "")
    if not match:
        return None
    code = _lang_code(match.group(1))
    return code if len(code) == 2 and code in _ISO_639_1_CODES else None


PAUSE_AT_WORDS = 100_000


async def _discover_sitemap_page_count(url: str, filters: list[URLFilter]) -> int | None:
    """Best-effort: how many pages does this site's sitemap list, after
    applying the same domain/language filters the real crawl would use?
    Returns None if no sitemap could be found — not every site has one, and
    that's not an error, just a missing signal."""
    hostname = urlparse(url).netloc
    try:
        async with AsyncUrlSeeder() as seeder:
            config = SeedingConfig(source="sitemap", extract_head=False, live_check=False)
            discovered = await seeder.urls(hostname, config)
    except Exception:
        return None
    if not discovered:
        return None
    urls = [item["url"] for item in discovered if item.get("url")]
    if filters:
        urls = [u for u in urls if all(f.apply(u) for f in filters)]
    return len(urls) if urls else None


async def _build_estimate_result(job, url: str, filters: list[URLFilter]) -> dict:
    pages_fetched = len(job.pages)
    avg_words_per_page = job.total_words / pages_fetched if pages_fetched else 0
    discovered_total = len(job.resume_state["visited"]) if job.resume_state else pages_fetched
    sitemap_count = await _discover_sitemap_page_count(url, filters)
    # The sitemap is usually the more complete number this early — the BFS
    # traversal may not have reached deep enough yet to discover everything
    # itself within the word budget.
    total_pages_estimate = max(sitemap_count or 0, discovered_total)
    return {
        "pages_fetched": pages_fetched,
        "discovered_total": discovered_total,
        "sitemap_count": sitemap_count,
        "total_pages_estimate": total_pages_estimate,
        "avg_words_per_page": round(avg_words_per_page),
        "estimated_total_words": round(avg_words_per_page * total_pages_estimate),
    }


def _is_login_wall(result) -> bool:
    """Heuristic: crawl4ai has no dedicated "requires login" signal, so this
    combines the strongest hints available — an outright auth status code,
    a redirect to a login-shaped URL, or login wording in the page title."""
    if getattr(result, "status_code", None) in (401, 403):
        return True

    for candidate_url in (result.url, getattr(result, "redirected_url", None)):
        if candidate_url and ("login" in candidate_url.lower() or "signin" in candidate_url.lower()):
            return True

    title = ""
    if result.metadata:
        title = (result.metadata.get("title") or "").lower()
    if any(kw in title for kw in _LOGIN_KEYWORDS):
        return True

    return False


async def run_crawl(
    job_id: str,
    url: str,
    max_pages: int,
    domain_scope: str = "all",
    language: str | None = None,
    pause_at_words: int | None = None,
    resume_state: dict | None = None,
) -> None:
    job = get_job(job_id)
    if job is None:
        return

    job.status = "crawling"
    job.domain_scope = domain_scope
    job.language_setting = language

    # Crawls are always unlimited now, so the depth cap just needs to be high
    # enough not to cut off a legitimately deep site.
    max_depth = 1000

    # By default crawl4ai treats any subdomain of the same registrable domain
    # (e.g. docs.example.com and www.example.com) as "internal", so the whole
    # domain gets crawled — that's what most people want ("all"). "subdomain_only"
    # locks to the exact starting host via crawl4ai's DomainFilter (whose
    # allowed_domains matching treats subdomains as a match, which is exactly
    # what pins it to that host and everything beneath it). "top_domain_only"
    # is the opposite restriction — root domain, but excluding other
    # subdomains — which needs the custom TopDomainOnlyFilter above.
    domain_filters: list[URLFilter] = []
    if domain_scope == "subdomain_only":
        hostname = urlparse(url).netloc
        domain_filters.append(DomainFilter(allowed_domains=[hostname]))
    elif domain_scope == "top_domain_only":
        domain_filters.append(TopDomainOnlyFilter(get_base_domain(url)))

    languages = parse_languages(language)

    try:
        async with AsyncWebCrawler() as crawler:
            if not languages and resume_state is None:
                # No language typed in, and this isn't a resumed crawl (where
                # the language was already resolved the first time around) —
                # probe the start page's own <html lang> before configuring
                # the crawl, so "blank" means "restrict to whatever language
                # this page is in" rather than "no restriction at all".
                # Best-effort: any failure here just falls through to the old
                # no-restriction behavior.
                try:
                    probe = await crawler.arun(url)
                    detected = _detect_page_language(getattr(probe, "html", None))
                    if detected:
                        languages = [detected]
                        job.detected_language = detected
                except Exception:
                    pass

            job.publish(job.status_payload())

            filters = list(domain_filters)
            if languages:
                filters.append(LanguageFilter(languages))
            filter_chain = FilterChain(filters)

            async def _on_state_change(state: dict) -> None:
                # Lets a paused crawl resume later from exactly this frontier
                # (see BFSDeepCrawlStrategy's resume_state parameter) and, in
                # the meantime, gives an accurate "how many pages have we
                # found links to" count for the pre-crawl estimate.
                job.resume_state = state

            strategy = BFSDeepCrawlStrategy(
                max_depth=max_depth,
                max_pages=max_pages,
                include_external=False,
                filter_chain=filter_chain,
                resume_state=resume_state,
                on_state_change=_on_state_change,
                # A should_cancel callback (rather than calling strategy.cancel())
                # because _arun_stream() resets its internal cancel event right as it
                # starts — calling cancel() before that point would be silently wiped
                # out. The callback is re-read live on every check, so it works no
                # matter when request_cancel() sets the flag. Also doubles as the
                # "pause this estimate crawl once it's sampled enough" trigger.
                should_cancel=lambda: job.cancel_requested or (
                    pause_at_words is not None and job.total_words >= pause_at_words
                ),
            )
            config = CrawlerRunConfig(
                deep_crawl_strategy=strategy,
                stream=True,
                markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
                excluded_tags=["nav", "footer", "aside", "form"],
                word_count_threshold=10,
            )

            async for result in await crawler.arun(url, config=config):
                if result.url in job.pages or result.url in job.login_blocked:
                    continue

                title = ""
                if result.metadata:
                    title = result.metadata.get("title") or ""

                if _is_login_wall(result):
                    # Hidden from the visible list and excluded from the word
                    # total entirely — it's not real page content, just a wall.
                    page = PageResult(
                        url=result.url,
                        title=title,
                        word_count=0,
                        success=False,
                        login_required=True,
                        error="Requires login",
                    )
                    job.login_blocked[result.url] = page
                    job.publish(
                        {
                            "type": "login_blocked",
                            "login_blocked_count": len(job.login_blocked),
                        }
                    )
                    continue

                if result.success:
                    text = clean_markdown_for_counting(_markdown_text(result))
                    word_count = count_words(text)
                    page = PageResult(url=result.url, title=title, word_count=word_count, success=True)
                    job.total_words += word_count
                else:
                    error_message = getattr(result, "error_message", None) or "Failed to fetch page"
                    # Crawl4AI runs its own layered anti-bot detection (Cloudflare/
                    # Akamai/PerimeterX/DataDome challenge pages, 429 rate limits,
                    # structurally-broken "empty shell" responses — see
                    # crawl4ai/antibot_detector.py) and prefixes error_message with
                    # this exact string when it concludes the host blocked us,
                    # rather than the page genuinely not existing/erroring.
                    page = PageResult(
                        url=result.url,
                        title="",
                        word_count=0,
                        success=False,
                        blocked_by_host=error_message.startswith("Blocked by anti-bot protection:"),
                        error=error_message,
                    )

                job.pages[result.url] = page
                job.publish(
                    {
                        "type": "page",
                        "page": page.model_dump(),
                        "total_words": job.total_words,
                    }
                )

        job.limit_reached = len(job.pages) + len(job.login_blocked) >= max_pages
        if job.cancel_requested:
            job.status = "cancelled"
        elif pause_at_words is not None and job.total_words >= pause_at_words:
            # Hit the pause threshold with the frontier still non-empty —
            # this is a genuine pause, not a finish. If the site instead
            # exhausted its own links before ever reaching the threshold,
            # this branch is skipped entirely and it's just a normal
            # completion below, exact rather than estimated.
            job.status = "paused"
            job.estimate_result = await _build_estimate_result(job, url, filters)
        else:
            job.status = "completed"
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)

    job.publish(job.status_payload())

    if job.status != "paused":
        await db.save_run(
            run_id=job.id,
            source_url=job.source_url,
            user_id=job.user_id,
            status=job.status,
            total_words=job.total_words,
            pages=list(job.pages.values()),
            limit_reached=job.limit_reached,
            login_blocked_count=len(job.login_blocked),
            domain_scope=job.domain_scope,
            language=",".join(languages) if languages else None,
            language_auto_detected=job.detected_language is not None,
        )
