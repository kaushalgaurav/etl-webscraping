"""Getting past blockers that are our own rules or easy to work around.

What is still reported, not defeated: a CAPTCHA or login with no data behind
it (once the browser has also failed), and private addresses.
"""
from werkzeug.wrappers import Response

from app.core.config import settings
from app.models import ScrapeFinding
from app.modules.scraping import discover_browser, service
from app.modules.scraping.discover_browser import Rendered
from app.modules.scraping.fetcher import Fetcher

from tests.scraping import helpers

CHALLENGE = "<html><title>Just a moment...</title><body>Checking your browser</body></html>"


def codes(scan):
    return [b["code"] for b in scan.blocked]


def kinds(db, scan):
    return [f.kind for f in db.query(ScrapeFinding).filter_by(scan_id=scan.id)]


def rendered_with(html):
    return lambda u, **k: Rendered(final_url=u, html=html, feeds=[], downloads=[])


# identity ----------------------------------------------------------------------
def test_reads_as_a_normal_browser(httpserver, loopback_ok):
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return Response("ok")

    httpserver.expect_request("/").respond_with_handler(handler)
    Fetcher(policy=loopback_ok).get(httpserver.url_for("/"), max_bytes=1000)
    assert seen["User-Agent"].startswith("Mozilla/5.0") and "Chrome/" in seen["User-Agent"]
    assert "Headless" not in seen["User-Agent"] and "Bot" not in seen["User-Agent"]
    assert "text/html" in seen["Accept"] and seen["Accept-Language"].startswith("en")


def test_file_downloads_send_the_page_as_referer(db, httpserver, loopback_ok):
    page_url = httpserver.url_for("/stats")
    httpserver.expect_request("/stats").respond_with_data(
        helpers.page(helpers.district_table_html(), '<a href="/files/data.csv">CSV</a>'),
        content_type="text/html")
    httpserver.expect_request("/files/data.csv", headers={"Referer": page_url}).respond_with_data(
        helpers.district_csv(), content_type="text/csv")
    scan = helpers.run(db, page_url, loopback_ok)
    assert "file_table" in kinds(db, scan) and codes(scan) == []


# rate limits -------------------------------------------------------------------
def test_too_many_requests_is_retried(httpserver, loopback_ok):
    httpserver.expect_oneshot_request("/p").respond_with_data(
        "slow down", status=429, headers={"Retry-After": "0"})
    httpserver.expect_request("/p").respond_with_data("ok")
    got = Fetcher(policy=loopback_ok).get(httpserver.url_for("/p"), max_bytes=1000)
    assert got.status == 200 and got.content == b"ok"


# the browser as a second try ---------------------------------------------------
def test_challenge_page_is_retried_in_the_browser(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    httpserver.expect_request("/p").respond_with_data(CHALLENGE, status=403, content_type="text/html")
    monkeypatch.setattr(discover_browser, "render",
                        rendered_with(helpers.page(helpers.district_table_html())))
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert "rendered_table" in kinds(db, scan) and codes(scan) == []


def test_challenge_the_browser_cannot_pass_is_still_reported(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    httpserver.expect_request("/p").respond_with_data(CHALLENGE, status=403, content_type="text/html")
    monkeypatch.setattr(discover_browser, "render", rendered_with(CHALLENGE))
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["bot_protection"] and kinds(db, scan) == []


def test_forbidden_page_is_retried_in_the_browser(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    httpserver.expect_request("/p").respond_with_data("Forbidden", status=403, content_type="text/html")
    monkeypatch.setattr(discover_browser, "render",
                        rendered_with(helpers.page(helpers.district_table_html())))
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert "rendered_table" in kinds(db, scan) and codes(scan) == []


def test_missing_page_is_not_retried(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    calls = []
    monkeypatch.setattr(discover_browser, "render", lambda *a, **k: calls.append(a))
    httpserver.expect_request("/gone").respond_with_data("Not here", status=404, content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/gone"), loopback_ok)
    assert calls == [] and codes(scan) == ["http_error"]


def test_captcha_page_without_data_is_tried_in_the_browser(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    httpserver.expect_request("/p").respond_with_data(
        helpers.page('<div class="g-recaptcha"></div><p>Enter the code.</p>'), content_type="text/html")
    monkeypatch.setattr(discover_browser, "render",
                        rendered_with(helpers.page(helpers.district_table_html())))
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert "rendered_table" in kinds(db, scan) and codes(scan) == []


def test_render_never_still_reports_the_block(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    calls = []
    monkeypatch.setattr(discover_browser, "render", lambda *a, **k: calls.append(a))
    httpserver.expect_request("/p").respond_with_data(CHALLENGE, status=403, content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok, render="never")
    assert calls == [] and codes(scan) == ["bot_protection"]


# our own refusals --------------------------------------------------------------
def test_sources_with_a_dedicated_connector_can_be_searched(db):
    scan = service.create_scan(db, "https://hmis.mohfw.gov.in/#!/", True)
    assert scan.status == "running"


def test_server_error_page_is_retried_in_the_browser(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    httpserver.expect_request("/p").respond_with_data("Server error", status=500, content_type="text/html")
    monkeypatch.setattr(discover_browser, "render",
                        rendered_with(helpers.page(helpers.district_table_html())))
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert "rendered_table" in kinds(db, scan) and codes(scan) == []


def test_refused_page_with_only_template_links_is_still_reported(db, httpserver, loopback_ok, monkeypatch):
    """A 403 page wrapped in the site's header and footer (links to PDFs) is not data."""
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    httpserver.expect_request("/p").respond_with_data("Forbidden", status=403, content_type="text/html")
    monkeypatch.setattr(discover_browser, "render", rendered_with(helpers.page(
        "<h1>403 Forbidden</h1><a href='/rti.pdf'>RTI</a><a href='/citizen-charter.pdf'>Charter</a>")))
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["http_error"] and kinds(db, scan) == []
    assert not any(req.path.endswith(".pdf") for req, _ in httpserver.log)


def test_rendered_challenge_page_is_not_through(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    httpserver.expect_request("/p").respond_with_data(CHALLENGE, status=403, content_type="text/html")
    monkeypatch.setattr(discover_browser, "render", rendered_with(
        "<html><title>Just a moment...</title><body><a href='/x.pdf'>x</a>"
        "<div class='cf-turnstile'></div></body></html>"))
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["bot_protection"]


def test_retry_after_is_capped(httpserver, loopback_ok, monkeypatch):
    from app.modules.scraping import fetcher as fetcher_module

    waits = []
    monkeypatch.setattr(fetcher_module.time, "sleep", waits.append)
    httpserver.expect_oneshot_request("/p").respond_with_data("busy", status=503, headers={"Retry-After": "3600"})
    httpserver.expect_request("/p").respond_with_data("ok")
    assert Fetcher(policy=loopback_ok).get(httpserver.url_for("/p"), max_bytes=1000).content == b"ok"
    assert waits == [30.0]


def test_no_time_to_retry_keeps_the_first_answer(httpserver, loopback_ok):
    from app.modules.scraping.fetcher import Deadline

    httpserver.expect_request("/p").respond_with_data(CHALLENGE, status=503, headers={"Retry-After": "20"})
    got = Fetcher(policy=loopback_ok, deadline=Deadline(5)).get(httpserver.url_for("/p"), max_bytes=10_000)
    assert got.status == 503 and b"Just a moment" in got.content


def test_a_host_that_keeps_refusing_is_not_waited_on_again(httpserver, loopback_ok, monkeypatch):
    from app.modules.scraping import fetcher as fetcher_module

    waits = []
    monkeypatch.setattr(fetcher_module.time, "sleep", waits.append)
    httpserver.expect_request("/p").respond_with_data("busy", status=429, headers={"Retry-After": "1"})
    f = Fetcher(policy=loopback_ok)
    for _ in range(4):
        f.get(httpserver.url_for("/p"), max_bytes=1000)
    assert len(waits) == 2  # once for each of the first two refusals, then no more


def test_referer_follows_the_browser_default_policy():
    from app.modules.scraping.fetcher import referer_for

    page = "https://data.gov.in/resource/x?api-key=SECRET"
    assert referer_for(page, "https://data.gov.in/files/x.csv") == page
    assert referer_for(page, "https://cdn.example.org/x.csv") == "https://data.gov.in/"
    assert referer_for(page, "http://data.gov.in/files/x.csv") is None
    assert referer_for(None, "https://data.gov.in/files/x.csv") is None
