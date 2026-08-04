"""Server-side aggregation for the report page.

The page used to be handed every crawled page and did all of this in the
browser. On a large crawl that meant tens of MB of embedded JSON and a row in
the DOM for every page — 164k pages came to 46MB of HTML and a million DOM
nodes, which is what made the tab hang. The grouping is cheap; it's the raw
rows that are expensive to ship, so it happens here and only the totals go out.

The folder/language rules mirror folderForUrl() and languageForUrl() in
app.js, which still run in live mode while a crawl is streaming.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.models import PageResult

# How much of each list the report page gets up front. The full page list is
# paged in on demand; these two are shown in full behind a "Show all".
TOP_PAGES = 100
MAX_ISSUES = 500
PAGE_ROWS = 100

ISO_639_1_CODES = {
    "aa","ab","ae","af","ak","am","an","ar","as","av","ay","az","ba","be","bg","bh","bi","bm","bn",
    "bo","br","bs","ca","ce","ch","co","cr","cs","cu","cv","cy","da","de","dv","dz","ee","el","en",
    "eo","es","et","eu","fa","ff","fi","fj","fo","fr","fy","ga","gd","gl","gn","gu","gv","ha","he",
    "hi","ho","hr","ht","hu","hy","hz","ia","id","ie","ig","ii","ik","io","is","it","iu","ja","jv",
    "ka","kg","ki","kj","kk","kl","km","kn","ko","kr","ks","ku","kv","kw","ky","la","lb","lg","li",
    "ln","lo","lt","lu","lv","mg","mh","mi","mk","ml","mn","mr","ms","mt","my","na","nb","nd","ne",
    "ng","nl","nn","no","nr","nv","ny","oc","oj","om","or","os","pa","pi","pl","ps","pt","qu","rm",
    "rn","ro","ru","rw","sa","sc","sd","se","sg","si","sk","sl","sm","sn","so","sq","sr","ss","st",
    "su","sv","sw","ta","te","tg","th","ti","tk","tl","tn","to","tr","ts","tt","tw","ty","ug","uk",
    "ur","uz","ve","vi","vo","wa","wo","xh","yi","yo","za","zh","zu",
}


def _first_segment(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    # Matches `new URL(...)` throwing in the browser, which the JS treats as
    # "(root)" — anything without a scheme and host isn't a URL to split.
    if not parts.scheme or not parts.netloc:
        return None
    for segment in parts.path.split("/"):
        if segment:
            return segment
    return None


def folder_for_url(url: str) -> str:
    segment = _first_segment(url)
    return f"/{segment}" if segment else "(root)"


def language_for_url(url: str) -> str:
    segment = _first_segment(url)
    if not segment:
        return "Default"
    code = segment.lower().split("-")[0].split("_")[0]
    return code if len(code) == 2 and code in ISO_639_1_CODES else "Default"


def _grouped(pages: list[PageResult], key) -> list[dict]:
    groups: dict[str, dict] = {}
    for page in pages:
        name = key(page.url)
        group = groups.get(name)
        if group is None:
            group = groups[name] = {"name": name, "pages": 0, "words": 0}
        group["pages"] += 1
        group["words"] += page.word_count or 0
    return sorted(groups.values(), key=lambda g: g["words"], reverse=True)


def page_row(page: PageResult) -> dict:
    """Only the fields the table actually renders — PageResult carries more."""
    return {
        "url": page.url,
        "title": page.title or "",
        "word_count": page.word_count,
        "success": page.success,
        "blocked_by_host": page.blocked_by_host,
        "error": page.error,
    }


def summarize(pages: list[PageResult]) -> dict:
    blocked = [p for p in pages if p.blocked_by_host]
    failed = [p for p in pages if not p.success and not p.blocked_by_host]
    top = sorted((p for p in pages if p.success), key=lambda p: p.word_count, reverse=True)[:TOP_PAGES]

    return {
        "folders": _grouped(pages, folder_for_url),
        "languages": _grouped(pages, language_for_url),
        "top_pages": [{"url": p.url, "word_count": p.word_count} for p in top],
        "top_pages_capped": len([p for p in pages if p.success]) > TOP_PAGES,
        "blocked_count": len(blocked),
        "failed_count": len(failed),
        "issues": [
            {"url": p.url, "error": p.error, "blocked_by_host": p.blocked_by_host}
            for p in (blocked + failed)[:MAX_ISSUES]
        ],
        "issues_capped": len(blocked) + len(failed) > MAX_ISSUES,
        "total_pages": len(pages),
    }
