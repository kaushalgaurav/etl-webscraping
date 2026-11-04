"""Background data feeds -> tables, with their further pages (spec §4.3).

A feed is JSON a page fetched by itself while rendering. Each list of
records in it (at least 2 records with at least 2 fields) becomes a table.
When the feed's URL pages through its data (`page=1`, `offset=0`, ...), the
scraper asks for the next pages itself, through the Fetcher, so those
requests are robots-checked, paced and address-guarded like any other. It
stops when a page comes back empty or repeats the previous one, on any
error, or at the page limit.
"""
import json
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

from app.modules.scraping.discover_browser import Feed
from app.modules.scraping.fetcher import Fetcher
from app.modules.scraping.files import record_arrays
from app.modules.scraping.items import TableItem
from app.modules.scraping.safety import ScrapeBlocked, message

PAGE_KEYS = {"page", "pageno", "page_no", "pagenumber", "pagenum", "pageindex", "page_index"}
OFFSET_KEYS = {"offset", "skip"}
# "start" and "from" are often dates or years (?start=2015&end=2020), so they
# count as an offset only next to a page-size parameter.
SIZED_OFFSET_KEYS = {"start", "from"}
SIZE_KEYS = {"limit", "size", "pagesize", "page_size", "per_page", "perpage", "rows",
             "length", "count", "max", "maxresults"}


@dataclass
class FeedResult:
    tables: list[TableItem] = field(default_factory=list)
    pages: int = 0
    blocked: list[ScrapeBlocked] = field(default_factory=list)
    bytes: int = 0  # downloaded for further pages (the first page came with the feed)


def paging_param(url: str) -> tuple[str, str] | None:
    """(query key as written, "page" | "offset"), if the URL pages."""
    query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    sized = any(k.lower() in SIZE_KEYS for k, _ in query)
    for key, value in query:
        if not value.isdigit():
            continue
        if key.lower() in PAGE_KEYS:
            return key, "page"
        if key.lower() in OFFSET_KEYS or (sized and key.lower() in SIZED_OFFSET_KEYS):
            return key, "offset"
    return None


def with_param(url: str, key: str, value: int) -> str:
    parts = urlsplit(url)
    query = [(k, str(value) if k == key else v)
             for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _arrays(payload) -> dict[str, list[dict]]:
    return dict(record_arrays(payload))


def _next_pages(feed: Feed, first: dict[str, list[dict]], fetcher: Fetcher,
                max_pages: int, max_bytes: int, byte_budget: int | None,
                result: FeedResult) -> dict[str, list[dict]]:
    stacked = {path: list(rows) for path, rows in first.items()}
    found = paging_param(feed.url)
    if fetcher is None or feed.method.upper() != "GET" or found is None:
        return stacked
    key, kind = found
    current = int(dict(parse_qsl(urlsplit(feed.url).query))[key])
    step = 1 if kind == "page" else max(len(rows) for rows in first.values())
    previous = first
    while result.pages < max_pages:
        current += step
        url = with_param(feed.url, key, current)
        cap = max_bytes
        if byte_budget is not None:
            cap = min(cap, byte_budget - result.bytes)
            if cap <= 0:
                result.blocked.append(ScrapeBlocked(
                    "too_large", message("too_large", n=max(1, round(byte_budget / 1_048_576))), url))
                break
        try:
            got = fetcher.get(url, max_bytes=cap)
        except ScrapeBlocked as exc:
            result.blocked.append(exc)
            break
        result.bytes += len(got.content)
        if got.status != 200:
            break
        try:
            page = _arrays(json.loads(got.content.decode("utf-8-sig")))
        except (UnicodeDecodeError, ValueError):
            break
        page = {path: page.get(path, []) for path in first}
        if not any(page.values()) or page == previous:
            break
        for path, rows in page.items():
            stacked[path].extend(rows)
        result.pages += 1
        previous = page
    return stacked


def collect(feed: Feed, fetcher: Fetcher | None, *, max_pages: int, max_bytes: int,
            byte_budget: int | None = None) -> FeedResult:
    """`byte_budget`: most bytes further pages may download in all."""
    result = FeedResult()
    first = _arrays(feed.payload)
    if not first:
        return result
    result.pages = 1
    stacked = _next_pages(feed, first, fetcher, max_pages, max_bytes, byte_budget, result)
    path_name = urlsplit(feed.url).path or "/"
    for path, rows in stacked.items():
        title = f"Data feed {path_name}" + (f" {path}" if path != "$" else "")
        locator = {"source": "feed", "feed_url": feed.url, "path": path, "pages": result.pages}
        result.tables.append(TableItem("feed", title, locator, pd.json_normalize(rows)))
    return result
