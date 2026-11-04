from datetime import timedelta

import pytest

from app.core.config import settings
from app.models import ScrapeFinding, ScrapeScan
from app.models.common import utcnow
from app.modules.scraping import discover_browser, discover_http, service, store
from app.modules.scraping.discover_browser import Feed, Rendered
from app.modules.scraping.fetcher import Deadline, Fetched, Fetcher
from app.modules.scraping.safety import ScrapeBlocked

from tests.scraping import helpers


def findings(db, scan):
    return db.query(ScrapeFinding).filter_by(scan_id=scan.id).order_by(ScrapeFinding.position).all()


def codes(scan):
    return [b["code"] for b in scan.blocked]


def test_page_with_table_csv_and_layout(db, httpserver, loopback_ok):
    scan = helpers.run(db, helpers.standard_site(httpserver), loopback_ok)
    rows = findings(db, scan)
    tables = [f for f in rows if f.kind in service.TABLE_KINDS]
    by_kind = {f.kind: f for f in rows if f.kind not in service.TABLE_KINDS}
    html = [t for t in tables if t.kind == "html_table" and t.title == "District beds"][0]
    nav = [t for t in tables if t.kind == "html_table" and t.title != "District beds"][0]
    csv = [t for t in tables if t.kind == "file_table"][0]

    assert len(tables) == 3
    assert nav.suggested == "skip" and nav.signals["looks_like_layout"] is True
    assert html.signals["duplicate_of"] == csv.id and html.suggested == "skip"
    assert csv.suggested == "keep" and csv.parent_id == by_kind["file"].id
    file = by_kind["file"]
    assert set(file.details) >= {"name", "url", "content_type", "bytes", "extracted"}
    assert file.details["name"] == "data.csv" and file.details["extracted"] == "CSV → 1 table"
    assert "text" in by_kind
    for t in tables:
        assert len(t.preview) <= 20 and all(len(r) <= 30 for r in t.preview)
        assert t.column_names and t.rows and t.columns and t.content_hash
        assert store.resolve(scan.id, t.storage_path).exists()
    assert csv.rows == 38 and csv.preview[0] == ["District 0", "0"]  # CSV values as published
    assert scan.status == "done"
    assert scan.summary == {"tables": 3, "files": 1, "text": 1, "blocked": 0, "suggested_keep": 1,
                            "rendered_in_browser": False}
    assert scan.final_url.endswith("/stats")
    assert abs((scan.expires_at - scan.finished_at) - timedelta(hours=24)) < timedelta(seconds=5)


def test_url_is_a_file(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/data.csv").respond_with_data(helpers.district_csv(), content_type="text/csv")
    scan = helpers.run(db, httpserver.url_for("/data.csv"), loopback_ok)
    kinds = sorted(f.kind for f in findings(db, scan))
    assert kinds == ["file", "file_table"] and scan.status == "done"


def test_robots_disallowed_page(db, httpserver, loopback_ok):
    url = helpers.standard_site(httpserver, robots="User-agent: *\nDisallow: /\n")
    scan = helpers.run(db, url, loopback_ok)
    assert scan.status == "done" and findings(db, scan) == []
    assert codes(scan) == ["robots_disallowed"]
    assert scan.summary["tables"] == 0 and scan.summary["blocked"] == 1


def test_disallowed_file_only(db, httpserver, loopback_ok):
    url = helpers.standard_site(httpserver, robots="User-agent: *\nDisallow: /files/\n")
    scan = helpers.run(db, url, loopback_ok)
    assert codes(scan) == ["robots_disallowed"] and scan.blocked[0]["what"].endswith("/files/data.csv")
    assert any(f.kind == "html_table" for f in findings(db, scan))


def test_private_address_page(db, httpserver):
    url = helpers.standard_site(httpserver)
    scan = helpers.run(db, url, policy=None, fetcher=Fetcher())
    assert scan.status == "done" and codes(scan) == ["private_address"]
    assert len(httpserver.log) == 0


def test_challenge_page(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(
        "<title>Just a moment...</title>", status=403, content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["bot_protection"] and findings(db, scan) == []


def test_http_error_page(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/gone").respond_with_data("<h1>Not here</h1>", status=404,
                                                         content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/gone"), loopback_ok)
    assert codes(scan) == ["http_error"]
    assert scan.blocked[0]["message"] == "The site answered with status 404."


def test_js_shell_reports_needs_browser(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/app").respond_with_data(
        '<html><body><div id="root"></div><script src="/a.js"></script></body></html>',
        content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/app"), loopback_ok)
    assert codes(scan) == ["needs_browser"] and scan.status == "done"


def test_too_many_files(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_max_files", 2)
    helpers.serve(httpserver)
    links = "".join(f'<a href="/f{i}.csv">f{i}</a>' for i in range(3))
    httpserver.expect_request("/p").respond_with_data(helpers.page(links), content_type="text/html")
    for i in range(3):
        httpserver.expect_request(f"/f{i}.csv").respond_with_data(f"a,b\n{i},1\n{i},2\n{i},3\n")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert sum(f.kind == "file" for f in findings(db, scan)) == 2
    assert codes(scan) == ["too_many_files"]
    assert scan.blocked[0]["message"] == "Only the first 2 linked files were read."


def test_total_budget(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_total_mb", 1)
    helpers.serve(httpserver)
    body = b"a,b\n" + b"1,2\n" * 160_000  # ~0.64 MB each
    httpserver.expect_request("/p").respond_with_data(
        helpers.page('<a href="/one.csv">1</a><a href="/two.csv">2</a>'), content_type="text/html")
    httpserver.expect_request("/one.csv").respond_with_data(body)
    httpserver.expect_request("/two.csv").respond_with_data(body)
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert sum(f.kind == "file" for f in findings(db, scan)) == 1
    assert codes(scan) == ["too_large"] and scan.blocked[0]["what"].endswith("/two.csv")


def test_deadline_stops_cleanly(db, httpserver, loopback_ok):
    url = helpers.standard_site(httpserver)
    scan = helpers.run(db, url, loopback_ok, fetcher=Fetcher(policy=loopback_ok, deadline=Deadline(0)))
    assert scan.status == "done" and codes(scan) == ["timeout"]
    assert scan.blocked[0]["message"] == "Search stopped after 180 seconds."


def test_row_cap_truncates(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_max_rows", 10)
    helpers.serve(httpserver)
    httpserver.expect_request("/big.csv").respond_with_data(
        "a,b\n" + "".join(f"{i},{i}\n" for i in range(50)))
    scan = helpers.run(db, httpserver.url_for("/big.csv"), loopback_ok)
    table = [f for f in findings(db, scan) if f.kind == "file_table"][0]
    assert table.rows == 10 and table.signals["truncated"] is True
    assert len(store.read_table(scan.id, table.storage_path)) == 10


def test_unexpected_error_marks_scan_failed(db, httpserver, loopback_ok, monkeypatch):
    url = helpers.standard_site(httpserver)

    def boom(html, base_url):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(discover_http, "parse_page", boom)
    scan = service.create_scan(db, url, True)
    from app.modules.scraping import scan as scan_module

    with pytest.raises(RuntimeError):
        scan_module.run_scan(db, scan.id, fetcher=Fetcher(policy=loopback_ok))
    db.refresh(scan)
    assert scan.status == "failed" and "parser exploded" in scan.error


def test_create_scan_rejects():
    with pytest.raises(service.ScanRequestError):
        service.create_scan(None, "ftp://x.gov.in/a", True)
    with pytest.raises(service.ScanRequestError) as e:
        service.create_scan(None, "https://hmis.mohfw.gov.in/#!/", True)
    assert str(e.value) == "This source has a dedicated connector; use the URL tab."


def test_reap_interrupted_scans(db):
    running = ScrapeScan(url="https://a.in/", status="running")
    importing = ScrapeScan(url="https://b.in/", status="importing")
    db.add_all([running, importing])
    db.commit()
    assert service.reap_interrupted_scans(db) >= 2
    db.refresh(running)
    db.refresh(importing)
    assert running.status == "failed" and running.error
    assert importing.status == "done"


def test_purge_expired(db):
    now = utcnow()
    old = ScrapeScan(url="https://a.in/", status="done", expires_at=now - timedelta(hours=1))
    fresh = ScrapeScan(url="https://b.in/", status="done", expires_at=now + timedelta(hours=1))
    saved = ScrapeScan(url="https://c.in/", status="imported", expires_at=None)
    db.add_all([old, fresh, saved])
    db.flush()
    db.add(ScrapeFinding(scan_id=old.id, kind="text", title="t", locator={}))
    db.commit()
    store.write_text(old.id, "t", "x")
    old_id = old.id
    service.purge_expired(db, now)
    db.expire_all()
    assert db.get(ScrapeScan, old_id) is None
    assert db.query(ScrapeFinding).filter_by(scan_id=old_id).count() == 0
    assert not store.scan_dir(old_id).exists()
    assert db.get(ScrapeScan, fresh.id) is not None and db.get(ScrapeScan, saved.id) is not None


def test_file_link_answered_with_block_page(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(
        helpers.page(helpers.district_table_html(), '<a href="/report.pdf">PDF</a>'), content_type="text/html")
    httpserver.expect_request("/report.pdf").respond_with_data(
        "<html><body>The requested URL was rejected. Your support ID is: 99</body></html>",
        content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["bot_protection"] and scan.blocked[0]["what"].endswith("/report.pdf")
    assert not any(f.kind == "file" for f in findings(db, scan))


def test_file_link_answered_with_ordinary_page(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(
        helpers.page(helpers.district_table_html(), '<a href="/x.pdf">PDF</a>'), content_type="text/html")
    httpserver.expect_request("/x.pdf").respond_with_data(helpers.page("<p>Moved, see the archive.</p>"),
                                                          content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["unsupported"] and scan.blocked[0]["what"].endswith("/x.pdf")
    assert not any(f.kind == "file" for f in findings(db, scan))


def test_xls_link_that_is_html_table_is_still_read(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(
        helpers.page('<a href="/r.xls">XLS</a>'), content_type="text/html")
    httpserver.expect_request("/r.xls").respond_with_data(
        "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>",
        content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == [] and any(f.kind == "file_table" for f in findings(db, scan))


def test_feedback_captcha_does_not_hide_data(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    footer = ('<form><input id="txtCaptcha"><img src="/Captcha.aspx"></form>'
              '<script src="https://www.google.com/recaptcha/api.js"></script>')
    httpserver.expect_request("/p").respond_with_data(
        helpers.page(helpers.district_table_html(), footer), content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert any(f.title == "District beds" for f in findings(db, scan))
    assert "captcha" not in codes(scan)


def test_search_box_password_field_does_not_hide_data(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(
        helpers.page(helpers.district_table_html(), '<form><input type="password"></form>'),
        content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert any(f.title == "District beds" for f in findings(db, scan)) and codes(scan) == []


def test_captcha_page_without_data_is_still_reported(db, httpserver, loopback_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(
        helpers.page('<div class="g-recaptcha"></div><p>Enter the code to see the report.</p>'),
        content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["captcha"]


def test_slow_file_does_not_stop_the_others(db, httpserver, loopback_ok, monkeypatch):
    import time as _time

    from werkzeug.wrappers import Response

    monkeypatch.setattr(settings, "request_timeout_seconds", 1)
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(
        helpers.page('<a href="/slow.csv">slow</a><a href="/good.csv">good</a>'), content_type="text/html")
    httpserver.expect_request("/slow.csv").respond_with_handler(
        # 1.2 s: just over the 1 s limit. The test server answers one request
        # at a time, so a longer sleep would also delay good.csv past 1 s.
        lambda request: (_time.sleep(1.2), Response("a,b\\n1,2\\n"))[1])
    httpserver.expect_request("/good.csv").respond_with_data(helpers.district_csv())
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    assert codes(scan) == ["slow_response"] and scan.blocked[0]["what"].endswith("/slow.csv")
    assert any(f.kind == "file_table" for f in findings(db, scan))


def test_zip_contents_count_against_total_budget(db, httpserver, loopback_ok, monkeypatch):
    import io
    import zipfile

    monkeypatch.setattr(settings, "scrape_total_mb", 1)
    part = b"a,b\n" + b"1,2\n" * 150_000  # 0.6 MB each, compresses to almost nothing
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("one.csv", part)
        z.writestr("two.csv", part)
    helpers.serve(httpserver)
    httpserver.expect_request("/p").respond_with_data(helpers.page('<a href="/b.zip">zip</a>'),
                                                      content_type="text/html")
    httpserver.expect_request("/b.zip").respond_with_data(buf.getvalue(), content_type="application/zip")
    scan = helpers.run(db, httpserver.url_for("/p"), loopback_ok)
    rows = findings(db, scan)
    assert sum(f.kind == "file_table" for f in rows) == 1
    assert "zip_truncated" in [f for f in rows if f.kind == "file"][0].details["flags"]


JS_SHELL = '<html><body><div id="root"></div><script src="/a.js"></script></body></html>'


def serve_shell(httpserver) -> str:
    helpers.serve(httpserver)
    httpserver.expect_request("/app").respond_with_data(JS_SHELL, content_type="text/html")
    return httpserver.url_for("/app")


def test_browser_unavailable_keeps_plain_results(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)

    def unavailable(url, **_kw):
        raise ScrapeBlocked("browser_unavailable", "Browser mode is not available on this server, "
                            "so only a plain read was done.", url)

    monkeypatch.setattr(discover_browser, "render", unavailable)
    scan = helpers.run(db, helpers.standard_site(httpserver), loopback_ok, render="always")
    assert scan.status == "done"
    assert len([f for f in findings(db, scan) if f.kind in service.TABLE_KINDS]) == 3
    assert codes(scan) == ["browser_unavailable"]
    assert scan.summary["rendered_in_browser"] is False


def test_render_never_skips_browser(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    calls = []
    monkeypatch.setattr(discover_browser, "render", lambda *a, **k: calls.append(a))
    scan = helpers.run(db, serve_shell(httpserver), loopback_ok, render="never")
    assert calls == [] and codes(scan) == ["needs_browser"]


def test_auto_skips_browser_when_tables_found(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    calls = []
    monkeypatch.setattr(discover_browser, "render", lambda *a, **k: calls.append(a))
    helpers.run(db, helpers.standard_site(httpserver), loopback_ok)
    assert calls == []


def test_always_with_browser_turned_off(db, httpserver, loopback_ok):
    scan = helpers.run(db, serve_shell(httpserver), loopback_ok, render="always")
    assert codes(scan) == ["needs_browser", "browser_unavailable"] or \
        codes(scan) == ["browser_unavailable", "needs_browser"]


def test_rendered_page_stages_tables_feeds_and_downloads(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    url = serve_shell(httpserver)
    rendered_html = helpers.page(helpers.district_table_html())
    feed = Feed("https://data.example.in/api/rows", "GET",
                {"data": [{"district": d, "beds": v} for d, v in helpers.DISTRICT_ROWS]})
    download = Fetched(url="https://data.example.in/export", final_url="https://data.example.in/export",
                       status=200, content_type=None, content=b"a,b\n1,2\n3,4\n", filename="rows.csv")
    monkeypatch.setattr(discover_browser, "render", lambda u, **k: Rendered(
        final_url=u, html=rendered_html, feeds=[feed], downloads=[download],
        refused=["intranet.example"], over_budget=True))
    scan = helpers.run(db, url, loopback_ok)
    kinds = [f.kind for f in findings(db, scan)]
    assert kinds.count("rendered_table") == 1 and kinds.count("feed") == 1
    assert kinds.count("file") == 1 and kinds.count("file_table") == 1
    assert "html_table" not in kinds
    assert sorted(codes(scan)) == ["private_address", "too_large"]
    assert scan.summary["rendered_in_browser"] is True
    feed_finding = [f for f in findings(db, scan) if f.kind == "feed"][0]
    assert feed_finding.rows == 38 and feed_finding.locator["feed_url"] == feed.url


CAPTCHA_DOWNLOADS = helpers.page('<div class="g-recaptcha"></div><a href="/files/data.csv">CSV</a>')


@pytest.mark.parametrize("mode", ["auto", "always"])
def test_captcha_page_is_not_opened_in_browser(db, httpserver, loopback_ok, monkeypatch, mode):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    calls = []
    monkeypatch.setattr(discover_browser, "render", lambda *a, **k: calls.append(a))
    helpers.serve(httpserver)
    httpserver.expect_request("/stats").respond_with_data(
        helpers.page('<div class="g-recaptcha"></div>', helpers.district_table_html(),
                     '<a href="/files/data.csv">CSV</a>'), content_type="text/html")
    httpserver.expect_request("/q").respond_with_data(CAPTCHA_DOWNLOADS, content_type="text/html")
    httpserver.expect_request("/files/data.csv").respond_with_data(helpers.district_csv(), content_type="text/csv")
    helpers.run(db, httpserver.url_for("/stats"), loopback_ok, render=mode)
    helpers.run(db, httpserver.url_for("/q"), loopback_ok, render=mode)
    assert calls == []


def test_feeds_count_against_the_scan_byte_budget(db, httpserver, loopback_ok, monkeypatch):
    monkeypatch.setattr(settings, "scrape_browser_enabled", True)
    monkeypatch.setattr(settings, "scrape_total_mb", 1)
    url = serve_shell(httpserver)
    rows = {"data": [{"district": d, "beds": v} for d, v in helpers.DISTRICT_ROWS]}
    small = Feed("https://data.example.in/api/small", "GET", rows, size=1000)
    huge = Feed("https://data.example.in/api/huge", "GET", rows, size=2_000_000)
    monkeypatch.setattr(discover_browser, "render", lambda u, **k: Rendered(
        final_url=u, html=helpers.page("<p>app</p>"), feeds=[small, huge]))
    scan = helpers.run(db, url, loopback_ok)
    feed_urls = [f.locator["feed_url"] for f in findings(db, scan) if f.kind == "feed"]
    assert feed_urls == [small.url]
    assert codes(scan) == ["too_large"]
