import json

from app.modules.scraping.discover_http import parse_page

BASE = "https://health.example.gov.in/stats/index.html"


def rows(n: int) -> str:
    return "".join(f"<tr><td>District {i}</td><td>{i * 10}</td></tr>" for i in range(n))


def table(body_rows: str = "", caption: str = "") -> str:
    cap = f"<caption>{caption}</caption>" if caption else ""
    return f"<table>{cap}<tr><th>District</th><th>Beds</th></tr>{body_rows or rows(38)}</table>"


def test_tables_with_titles():
    html = (f"<html><body><h3>District beds</h3>{table()}"
            f"<h2>Ignored heading</h2>{table(caption='PHC count')}{table()}</body></html>")
    # The third table's nearest heading is "Ignored heading", so it takes that.
    scan = parse_page(html, BASE)
    assert [t.title for t in scan.tables] == ["District beds", "PHC count", "Ignored heading"]
    first = scan.tables[0]
    assert first.kind == "html_table" and first.locator == {"source": "page", "table_index": 0}
    assert len(first.df) == 38 and list(first.df.columns) == ["District", "Beds"]


def test_untitled_table_gets_numbered_title():
    scan = parse_page(f"<html><body>{table()}{table()}</body></html>", BASE)
    assert [t.title for t in scan.tables] == ["Table 1", "Table 2"]


def test_outer_layout_table_skipped_inner_kept():
    html = f"<html><body><table><tr><td>{table()}</td><td>menu</td></tr></table></body></html>"
    scan = parse_page(html, BASE)
    assert len(scan.tables) == 1 and len(scan.tables[0].df) == 38


def test_empty_or_unparseable_table_skipped():
    html = "<html><body><table></table><table><tr></tr></table></body></html>"
    assert parse_page(html, BASE).tables == []


def test_embedded_next_data_and_json_ld():
    data = {"props": {"pageProps": {"districts": [{"name": "Patna", "beds": 3100},
                                                  {"name": "Gaya", "beds": 1250}]}}}
    ld = {"@context": "https://schema.org", "@type": "Organization", "name": "Health Dept"}
    html = (f'<html><body><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
            f'<script type="application/ld+json">{json.dumps(ld)}</script></body></html>')
    scan = parse_page(html, BASE)
    assert len(scan.tables) == 1
    t = scan.tables[0]
    assert t.kind == "embedded" and t.locator["path"] == "$.props.pageProps.districts"
    assert t.locator["source"] == "page" and len(t.df) == 2


def test_broken_embedded_json_ignored():
    html = '<html><body><script type="application/json">{not json</script></body></html>'
    assert parse_page(html, BASE).tables == []


def test_file_links():
    html = """<html><body>
      <a href="../reports/annual.pdf">Annual report</a>
      <a href="https://cdn.other.in/data/beds.xlsx">Beds</a>
      <a href="/files/phc.CSV#top">PHC</a>
      <a href="/files/phc.CSV">PHC again</a>
      <a href="download.php?f=x.csv">query only</a>
      <a href="mailto:a@b.in">mail</a><a href="#top">top</a><a href="photo.jpg">photo</a>
    </body></html>"""
    links = parse_page(html, BASE).file_links
    assert links == [
        "https://health.example.gov.in/reports/annual.pdf",
        "https://health.example.gov.in/files/phc.CSV",
        "https://cdn.other.in/data/beds.xlsx",
    ]


def test_main_text():
    article = " ".join(["The district health society reviewed institutional deliveries this year."] * 8)
    html = (f"<html><head><title>Report</title></head><body><nav><a href='/'>Home</a> "
            f"<a href='/sitemap'>Sitemap navigation</a></nav><article><h1>Annual review</h1><p>{article}</p>"
            f"</article><footer>Copyright footer menu links</footer></body></html>")
    scan = parse_page(html, BASE)
    assert scan.title == "Report"
    assert scan.text.source == "page" and scan.text.title == "Page text"
    assert "institutional deliveries" in scan.text.text
    assert "Sitemap navigation" not in scan.text.text


def test_js_shell():
    shell = '<html><head><script src="/app.js"></script></head><body><div id="root"></div></body></html>'
    assert parse_page(shell, BASE).js_shell is True
    normal = f"<html><body><h1>Stats</h1>{table()}</body></html>"
    assert parse_page(normal, BASE).js_shell is False


def test_form_gated_flag():
    form = ("<html><body><form><select name=s><option>Bihar</option></select>"
            "<input type=submit></form></body></html>")
    assert parse_page(form, BASE).form_gated is True
    assert parse_page(form.replace("</form>", f"</form>{table()}"), BASE).form_gated is False


def test_downloads_only_page_is_not_a_shell():
    html = '<html><body><a href="/a.csv">a</a> <a href="/b.xlsx">b</a></body></html>'
    assert parse_page(html, BASE).js_shell is False


def test_embedded_data_page_is_not_a_shell():
    rows = json.dumps([{"a": 1, "b": 2}, {"a": 3, "b": 4}])
    html = f'<html><body><script type="application/json">{rows}</script></body></html>'
    assert parse_page(html, BASE).js_shell is False
