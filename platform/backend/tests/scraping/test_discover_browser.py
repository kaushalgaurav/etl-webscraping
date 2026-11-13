import builtins
import time

import pytest

from app.modules.scraping import discover_browser, safety
from app.modules.scraping.fetcher import Deadline
from app.modules.scraping.safety import ScrapeBlocked

ROWS = [{"district": f"District {i}", "beds": i * 10} for i in range(5)]

TABLE_SCRIPT = """
<div id="root"></div>
<script>
fetch('/api/rows?page=1').then(r => r.json()).then(d => {
  const rows = d.data.map(r => `<tr><td>${r.district}</td><td>${r.beds}</td></tr>`).join('');
  document.getElementById('root').innerHTML =
    `<table><tr><th>District</th><th>Beds</th></tr>${rows}</table>`;
});
</script>
"""


def html(body: str) -> str:
    return f"<html><head><title>App</title></head><body>{body}</body></html>"


def serve_page(httpserver, body: str, path: str = "/app") -> str:
    httpserver.expect_request(path).respond_with_data(html(body), content_type="text/html")
    return httpserver.url_for(path)


def render(url, policy, seconds=30, **kw):
    return discover_browser.render(url, policy=policy, deadline=Deadline(seconds), **kw)


@pytest.mark.browser
def test_render_builds_table_from_script(httpserver, loopback_ok, browser_ok):
    httpserver.expect_request("/api/rows").respond_with_json({"data": ROWS})
    url = serve_page(httpserver, TABLE_SCRIPT)
    rendered = render(url, loopback_ok)
    assert "<table" in rendered.html and "District 4" in rendered.html
    assert rendered.final_url == url
    assert len(rendered.feeds) == 1
    feed = rendered.feeds[0]
    assert feed.url.endswith("/api/rows?page=1") and feed.method == "GET"
    assert feed.payload == {"data": ROWS}
    assert rendered.refused == [] and rendered.over_budget is False


@pytest.mark.browser
def test_render_page_that_never_goes_quiet(httpserver, loopback_ok, browser_ok):
    httpserver.expect_request("/ping").respond_with_json({"ok": True})
    url = serve_page(httpserver, "<p>Live</p><script>setInterval(() => fetch('/ping'), 300)</script>")
    started = time.monotonic()
    rendered = render(url, loopback_ok, seconds=20)
    assert time.monotonic() - started < 20
    assert "Live" in rendered.html


@pytest.mark.browser
def test_feed_capture_skips_noise_and_bad_json(httpserver, loopback_ok, browser_ok):
    httpserver.expect_request("/api/good").respond_with_json({"data": ROWS})
    httpserver.expect_request("/collect").respond_with_json({"data": ROWS})
    httpserver.expect_request("/api/bad").respond_with_data("not json", content_type="application/json")
    url = serve_page(httpserver, "<p>x</p><script>fetch('/api/good'); fetch('/collect?x=1'); "
                                 "fetch('/api/bad');</script>")
    rendered = render(url, loopback_ok)
    assert [f.url.rsplit("/", 1)[1] for f in rendered.feeds] == ["good"]


@pytest.mark.browser
def test_browser_cannot_reach_loopback_without_proxy_approval(httpserver, browser_ok):
    url = serve_page(httpserver, "<p>secret</p>")
    with pytest.raises(ScrapeBlocked) as err:
        render(url, safety.is_public_address)
    assert err.value.code == "private_address"
    assert len(httpserver.log) == 0


@pytest.mark.browser
def test_export_button_download(httpserver, loopback_ok, browser_ok):
    httpserver.expect_request("/export.csv").respond_with_data(
        "District,Beds\nA,1\nB,2\n", content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rows.csv"})
    url = serve_page(httpserver, "<button onclick=\"location='/export.csv'\">Export CSV</button>")
    rendered = render(url, loopback_ok)
    assert [d.filename for d in rendered.downloads] == ["rows.csv"]
    assert rendered.downloads[0].content == b"District,Beds\nA,1\nB,2\n"


@pytest.mark.browser
def test_export_click_that_navigates_stops_clicking(httpserver, loopback_ok, browser_ok):
    httpserver.expect_request("/other").respond_with_data(html("<p>other</p>"), content_type="text/html")
    httpserver.expect_request("/export.csv").respond_with_data(
        "a,b\n1,2\n", content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rows.csv"})
    url = serve_page(httpserver,
                     "<a href=\"/other\">Download centre</a>"
                     "<button onclick=\"location='/export.csv'\">Export CSV</button>")
    rendered = render(url, loopback_ok)
    assert rendered.downloads == []
    assert not any(e.url.endswith("/export.csv") for e, _ in httpserver.log)


def test_browser_unavailable(monkeypatch):
    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)
    with pytest.raises(ScrapeBlocked) as err:
        discover_browser.render("https://example.gov.in/", policy=safety.is_public_address,
                                deadline=Deadline(10))
    assert err.value.code == "browser_unavailable"


# --- final review fixes -----------------------------------------------------
import threading as _threading

from pytest_httpserver import HTTPServer
from werkzeug import Response

from app.core.config import settings


def finishes_within(seconds, fn):
    """(finished, result or exception): run fn in a thread, give up after seconds."""
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            box["value"] = exc

    thread = _threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), box.get("value")


@pytest.fixture
def slow_server():
    """A threaded test server, so one never-ending answer doesn't block the rest."""
    server = HTTPServer(threaded=True)
    server.start()
    yield server
    server.clear()
    if server.is_running():
        server.stop()


def drip(seconds=120):
    def handler(_request):
        def body():
            for _ in range(seconds):
                yield b"a"
                time.sleep(1)
        return Response(body(), headers={"Content-Disposition": "attachment; filename=slow.csv"},
                        content_type="text/csv")
    return handler


@pytest.mark.browser
def test_page_whose_load_never_finishes_returns_html(slow_server, loopback_ok, browser_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_seconds", 4)
    slow_server.expect_request("/hang").respond_with_handler(lambda r: time.sleep(60) or Response("x"))
    slow_server.expect_request("/ping").respond_with_json({"ok": True})
    url = serve_page(slow_server, '<p>Live</p><img src="/hang">'
                                  "<script>setInterval(() => fetch('/ping'), 300)</script>")
    done, value = finishes_within(25, lambda: render(url, loopback_ok, seconds=60))
    assert done, "render hung"
    assert not isinstance(value, BaseException), value
    assert "Live" in value.html


@pytest.mark.browser
def test_busy_script_is_stopped_by_the_watchdog(httpserver, loopback_ok, browser_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_seconds", 4)
    url = serve_page(httpserver, "<p>x</p><script>setTimeout(() => { while (true) {} }, 300)</script>")
    done, value = finishes_within(25, lambda: render(url, loopback_ok, seconds=60))
    assert done, "render hung"
    assert isinstance(value, ScrapeBlocked) and value.code == "slow_response"


@pytest.mark.browser
def test_dripping_download_does_not_hang(slow_server, loopback_ok, browser_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_seconds", 4)
    slow_server.expect_request("/slow.csv").respond_with_handler(drip())
    url = serve_page(slow_server, "<p>Data</p><button onclick=\"location='/slow.csv'\">Export CSV</button>")
    done, value = finishes_within(25, lambda: render(url, loopback_ok, seconds=60))
    assert done, "render hung"
    assert not isinstance(value, BaseException), value
    assert value.downloads == [] and "Data" in value.html


@pytest.mark.browser
def test_streaming_feed_does_not_hang(slow_server, loopback_ok, browser_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_seconds", 4)

    def stream(_request):
        def body():
            yield b'{"data": ['
            for _ in range(120):
                yield b'{"a": 1, "b": 2},'
                time.sleep(1)
        return Response(body(), content_type="application/json")

    slow_server.expect_request("/feed").respond_with_handler(stream)
    url = serve_page(slow_server, "<p>Data</p><script>fetch('/feed')</script>")
    done, value = finishes_within(25, lambda: render(url, loopback_ok, seconds=60))
    assert done, "render hung"
    assert not isinstance(value, ScrapeBlocked) or value.code == "slow_response"


@pytest.mark.browser
def test_export_search_on_a_page_with_many_links(httpserver, loopback_ok, browser_ok):
    httpserver.expect_request("/export.csv").respond_with_data(
        "a,b\n1,2\n", content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rows.csv"})
    links = "".join(f'<a href="/p{i}">Page {i}</a>' for i in range(5000))
    url = serve_page(httpserver, links + "<button onclick=\"location='/export.csv'\">Export CSV</button>")
    started = time.monotonic()
    rendered = render(url, loopback_ok)
    assert [d.filename for d in rendered.downloads] == ["rows.csv"]
    assert time.monotonic() - started < 15


def test_noise_filter_keeps_real_data_apis():
    noise = discover_browser._is_noise
    assert noise("https://www.google-analytics.com/g/collect?v=2")
    assert noise("https://example.gov.in/g/collect")
    assert noise("https://stats.hotjar.io/x")
    assert not noise("https://example.gov.in/api/analytics/district-data")
    assert not noise("https://example.gov.in/collectorate/list.json")
    assert not noise("https://example.gov.in/tracker/api/rows")
    assert not noise("https://segmentdata.gov.in/api/rows")
