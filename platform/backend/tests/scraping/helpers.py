"""Shared builders for scan tests: a small fake government site."""
from app.modules.scraping import scan as scan_module
from app.modules.scraping import service
from app.modules.scraping.fetcher import Fetcher

DISTRICT_ROWS = [(f"District {i}", i * 10) for i in range(38)]


def district_table_html() -> str:
    body = "".join(f"<tr><td>{d}</td><td>{v}</td></tr>" for d, v in DISTRICT_ROWS)
    return f"<h3>District beds</h3><table><tr><th>District</th><th>Beds</th></tr>{body}</table>"


def district_csv() -> bytes:
    return ("District,Beds\n" + "".join(f"{d},{v}\n" for d, v in DISTRICT_ROWS)).encode()


NAV_TABLE = ("<table><tr><th>Menu</th><th>Links</th></tr><tr><td>Home</td><td>About Us</td></tr>"
             "<tr><td>Sitemap</td><td>FAQ</td></tr><tr><td>Login</td><td>Help</td></tr></table>")

ARTICLE = "<article><p>" + "The district health society publishes bed counts every year. " * 8 + "</p></article>"


def page(*parts: str) -> str:
    return "<html><head><title>Health statistics</title></head><body>" + "".join(parts) + "</body></html>"


def serve(httpserver, robots: str | None = None):
    if robots is None:
        httpserver.expect_request("/robots.txt").respond_with_data("", status=404)
    else:
        httpserver.expect_request("/robots.txt").respond_with_data(robots, content_type="text/plain")


def standard_site(httpserver, robots: str | None = None):
    """A page with a district table, a nav table, a CSV link and some text."""
    serve(httpserver, robots)
    httpserver.expect_request("/stats").respond_with_data(
        page(NAV_TABLE, district_table_html(), '<a href="/files/data.csv">CSV</a>', ARTICLE),
        content_type="text/html")
    httpserver.expect_request("/files/data.csv").respond_with_data(district_csv(), content_type="text/csv")
    return httpserver.url_for("/stats")


def run(db, url: str, policy, include_files: bool = True, fetcher: Fetcher | None = None,
        render: str = "auto"):
    scan = service.create_scan(db, url, include_files, render)
    scan_module.run_scan(db, scan.id, fetcher=fetcher or Fetcher(policy=policy))
    db.refresh(scan)
    return scan
