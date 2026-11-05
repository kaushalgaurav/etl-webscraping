"""Search end to end with a real headless Chromium (skipped where it can't launch)."""
import pytest

from app.models import Dataset, ScrapeFinding
from app.modules.scraping import importer

from tests.scraping import helpers

pytestmark = pytest.mark.browser

ROWS = [{"district": d, "beds": v} for d, v in helpers.DISTRICT_ROWS]

REACT_PAGE = """<html><head><title>District dashboard</title></head><body>
<div id="root"></div>
<script>
fetch('/api/districts?page=1').then(r => r.json()).then(d => {
  const rows = d.data.map(r => `<tr><td>${r.district}</td><td>${r.beds}</td></tr>`).join('');
  document.getElementById('root').innerHTML =
    `<h3>Beds</h3><table><tr><th>District</th><th>Beds</th></tr>${rows}</table>`;
});
</script></body></html>"""


def react_site(httpserver) -> str:
    helpers.serve(httpserver)
    httpserver.expect_request("/app").respond_with_data(REACT_PAGE, content_type="text/html")
    for page, part in ((1, ROWS[:19]), (2, ROWS[19:]), (3, [])):
        httpserver.expect_request("/api/districts", query_string=f"page={page}").respond_with_json(
            {"data": part})
    return httpserver.url_for("/app")


def by_kind(db, scan, kind):
    return [f for f in db.query(ScrapeFinding).filter_by(scan_id=scan.id) if f.kind == kind]


def test_js_page_end_to_end(db, httpserver, loopback_ok, browser_ok):
    scan = helpers.run(db, react_site(httpserver), loopback_ok)
    assert scan.summary["rendered_in_browser"] is True
    assert "needs_browser" not in [b["code"] for b in scan.blocked]
    [table] = by_kind(db, scan, "rendered_table")
    [feed] = by_kind(db, scan, "feed")
    assert table.rows == 19 and feed.rows == 38
    assert feed.locator["pages"] == 2
    assert feed.signals["joinable"] is not None


def test_export_button_file_read(db, httpserver, loopback_ok, browser_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/app").respond_with_data(
        helpers.page("<button onclick=\"location='/export'\">Export CSV</button>"),
        content_type="text/html")
    httpserver.expect_request("/export").respond_with_data(
        helpers.district_csv(), content_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=districts.csv"})
    scan = helpers.run(db, httpserver.url_for("/app"), loopback_ok)
    [file] = by_kind(db, scan, "file")
    [table] = by_kind(db, scan, "file_table")
    assert file.details["name"] == "districts.csv" and table.rows == 38


def test_import_feed_creates_web_feed_dataset(db, httpserver, loopback_ok, browser_ok):
    scan = helpers.run(db, react_site(httpserver), loopback_ok)
    [feed] = by_kind(db, scan, "feed")
    items = [{"finding_id": feed.key, "year": "2022-2023", "state_name": "Bihar"}]
    importer.claim_for_import(db, scan.key, items)
    saved = importer.run_import(db, scan.key, 7, items)["items"][0]
    raw = db.get(Dataset, saved["dataset_id"])
    assert raw.connector == "web_feed" and raw.row_count == 38
    assert "/api/districts?page=1" in raw.source_ref


FORM_PAGE = helpers.page(
    "<h2>District report</h2><form action='/report' method='get'>"
    "<select name='state'><option value='all'>All states</option><option value='10'>Bihar</option></select>"
    "<select name='year'><option>2019-2020</option><option>2020-2021</option></select>"
    "<input type='submit' value='Show report'></form>")


def test_form_is_submitted_with_its_default_choices(db, httpserver, loopback_ok, browser_ok):
    helpers.serve(httpserver)
    httpserver.expect_request("/form").respond_with_data(FORM_PAGE, content_type="text/html")
    httpserver.expect_request("/report", query_string="state=all&year=2019-2020").respond_with_data(
        helpers.page(helpers.district_table_html()), content_type="text/html")
    scan = helpers.run(db, httpserver.url_for("/form"), loopback_ok)
    [table] = by_kind(db, scan, "rendered_table")
    assert table.rows == 38
    assert "form_gated" not in [b["code"] for b in scan.blocked]


def test_sends_a_normal_browser_identity(db, httpserver, loopback_ok, browser_ok):
    from app.core.config import settings

    helpers.serve(httpserver)
    httpserver.expect_request("/app", headers={"User-Agent": settings.scrape_user_agent}).respond_with_data(
        REACT_PAGE, content_type="text/html")
    for page, part in ((1, ROWS[:19]), (2, ROWS[19:]), (3, [])):
        httpserver.expect_request("/api/districts", query_string=f"page={page}").respond_with_json(
            {"data": part})
    scan = helpers.run(db, httpserver.url_for("/app"), loopback_ok, render="always")
    assert by_kind(db, scan, "rendered_table")
