from app.modules.scraping import feeds
from app.modules.scraping.discover_browser import Feed
from app.modules.scraping.fetcher import Fetcher

from tests.scraping import helpers


def records(start: int, n: int) -> list[dict]:
    return [{"district": f"District {i}", "beds": i} for i in range(start, start + n)]


def collect(feed, policy, max_pages=50):
    return feeds.collect(feed, Fetcher(policy=policy), max_pages=max_pages, max_bytes=10_000_000)


def test_paging_param():
    assert feeds.paging_param("https://x/api?pageNo=1&size=10") == ("pageNo", "page")
    assert feeds.paging_param("https://x/api?start=0") is None  # alone, could be a year
    assert feeds.paging_param("https://x/api?start=0&limit=20") == ("start", "offset")
    assert feeds.paging_param("https://x/api?q=x") is None


def test_with_param_keeps_other_params():
    assert feeds.with_param("https://x/api?size=10&page=1", "page", 2) == "https://x/api?size=10&page=2"


def test_collect_without_paging():
    feed = Feed("https://x/api/rows", "GET", {"data": records(0, 3)})
    result = feeds.collect(feed, None, max_pages=50, max_bytes=1000)
    assert len(result.tables) == 1 and result.pages == 1
    table = result.tables[0]
    assert table.kind == "feed" and table.title == "Data feed /api/rows $.data"
    assert table.locator == {"source": "feed", "feed_url": "https://x/api/rows", "path": "$.data", "pages": 1}
    assert len(table.df) == 3


def test_collect_follows_pages(httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/api/rows", query_string="page=2").respond_with_json({"data": records(10, 10)})
    httpserver.expect_request("/api/rows", query_string="page=3").respond_with_json({"data": records(20, 10)})
    httpserver.expect_request("/api/rows", query_string="page=4").respond_with_json({"data": []})
    feed = Feed(httpserver.url_for("/api/rows") + "?page=1", "GET", {"data": records(0, 10)})
    result = collect(feed, loopback_ok)
    assert result.pages == 3 and result.blocked == []
    assert len(result.tables[0].df) == 30
    assert result.tables[0].locator["pages"] == 3


def test_collect_offset_step(httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/api/rows", query_string="offset=5").respond_with_json({"data": records(5, 5)})
    httpserver.expect_request("/api/rows", query_string="offset=10").respond_with_json({"data": []})
    feed = Feed(httpserver.url_for("/api/rows") + "?offset=0", "GET", {"data": records(0, 5)})
    assert len(collect(feed, loopback_ok).tables[0].df) == 10


def test_collect_stops_on_repeat(httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/api/rows").respond_with_json({"data": records(0, 10)})
    feed = Feed(httpserver.url_for("/api/rows") + "?page=1", "GET", {"data": records(0, 10)})
    result = collect(feed, loopback_ok)
    assert result.pages == 1 and len(result.tables[0].df) == 10


def test_collect_respects_robots(httpserver, loopback_ok):
    helpers.serve(httpserver, robots="User-agent: *\nDisallow: /api/\n")
    feed = Feed(httpserver.url_for("/api/rows") + "?page=1", "GET", {"data": records(0, 10)})
    result = collect(feed, loopback_ok)
    assert len(result.tables[0].df) == 10
    assert [b.code for b in result.blocked] == ["robots_disallowed"]


def test_collect_page_limit(httpserver, loopback_ok):
    helpers.serve(httpserver)
    for page in range(2, 10):
        httpserver.expect_request("/api/rows", query_string=f"page={page}").respond_with_json(
            {"data": records(page * 10, 10)})
    feed = Feed(httpserver.url_for("/api/rows") + "?page=1", "GET", {"data": records(10, 10)})
    result = collect(feed, loopback_ok, max_pages=4)
    assert result.pages == 4 and len(result.tables[0].df) == 40


def test_collect_post_feed_not_paged(httpserver, loopback_ok):
    feed = Feed(httpserver.url_for("/api/rows") + "?page=1", "POST", {"data": records(0, 10)})
    result = collect(feed, loopback_ok)
    assert result.pages == 1 and len(httpserver.log) == 0


def test_collect_nothing_tabular():
    assert feeds.collect(Feed("https://x/a", "GET", {"ok": True}), None,
                         max_pages=50, max_bytes=1000).tables == []


def test_date_like_start_is_not_paging():
    assert feeds.paging_param("https://x/api?start=2015&end=2020") is None
    assert feeds.paging_param("https://x/api?from=20240101") is None
    assert feeds.paging_param("https://x/api?start=0&rows=10") == ("start", "offset")
    assert feeds.paging_param("https://x/api?offset=0") == ("offset", "offset")


def test_collect_stops_at_byte_budget(httpserver, loopback_ok):
    helpers.serve(httpserver)
    for page in range(2, 10):
        httpserver.expect_request("/api/rows", query_string=f"page={page}").respond_with_json(
            {"data": records(page * 10, 10)})
    feed = Feed(httpserver.url_for("/api/rows") + "?page=1", "GET", {"data": records(0, 10)})
    one_page = 700  # a page of 10 records is about 600 bytes
    result = feeds.collect(feed, Fetcher(policy=loopback_ok), max_pages=50, max_bytes=10_000_000,
                           byte_budget=2 * one_page)
    assert result.pages < 9 and result.bytes <= 2 * one_page
    assert [b.code for b in result.blocked] == ["too_large"]
