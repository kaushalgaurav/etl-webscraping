import io
import json
import zipfile

import pytest
from openpyxl import Workbook

from app.core.config import settings
from app.modules.scraping import files
from app.modules.scraping.fetcher import Deadline

URL = "https://x.gov.in/files/"


def read(content: bytes, name: str, content_type=None, deadline=None):
    return files.read_file(content, name, URL + name, content_type, deadline)


def xlsx(sheets: dict) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pdf_with_table() -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    buf = io.BytesIO()
    table = Table([["District", "Beds"], ["Patna", "3100"], ["Gaya", "1250"]])
    table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    SimpleDocTemplate(buf, pagesize=A4).build(
        [Paragraph("District hospital beds report", getSampleStyleSheet()["Normal"]), table])
    return buf.getvalue()


def pdf_scanned() -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 200, 200, fill=1)
    c.showPage()
    c.save()
    return buf.getvalue()


def docx_bytes() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph("Annual report of the district health society.")
    t = doc.add_table(rows=3, cols=2)
    for r, row in enumerate([["District", "PHCs"], ["Patna", "42"], ["Gaya", "18"]]):
        for c, val in enumerate(row):
            t.cell(r, c).text = val
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def zipped(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_csv_one_table():
    out = read(b"District,Beds\nPatna,3100\nGaya,1250\n", "beds.csv")
    assert len(out.tables) == 1
    t = out.tables[0]
    assert t.kind == "file_table" and t.title == "beds.csv"
    assert t.locator == {"source": "file", "file_url": URL + "beds.csv"}
    assert list(t.df.columns) == ["District", "Beds"] and len(t.df) == 2
    assert out.description == "CSV → 1 table"


def test_csv_cp1252_and_bom_and_hindi():
    bom = read(b"\xef\xbb\xbfDistrict,Value\nPatna,1\nGaya,2\n", "a.csv").tables[0].df
    assert list(bom.columns)[0] == "District"
    latin = read("District,Value\nPun\xe9,1\nGaya,2\n".encode("cp1252"), "b.csv").tables[0].df
    assert latin.iloc[0, 0] == "Pun\xe9"
    hindi = read("District,Value\nपटना,1\nगया,2\n".encode("utf-8"), "c.csv").tables[0].df
    assert hindi.iloc[0, 0] == "पटना"


def test_tsv_by_extension():
    out = read(b"A\tB\n1\t2\n3\t4\n", "x.tsv")
    assert list(out.tables[0].df.columns) == ["A", "B"] and out.description == "TSV → 1 table"


def test_excel_every_nonempty_sheet():
    content = xlsx({"Beds": [["District", "Beds"], ["Patna", 3100], ["Gaya", 1250]],
                    "Empty": [],
                    "PHCs": [["District", "PHCs"], ["Patna", 42], ["Gaya", 18]]})
    out = read(content, "infra.xlsx")
    assert [t.locator["sheet"] for t in out.tables] == ["Beds", "PHCs"]
    assert out.tables[0].title == "infra.xlsx - Beds"
    assert out.description == "Excel, 3 sheets → 2 tables"


def test_xls_that_is_really_html():
    html = b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>"
    out = read(html, "report.xls")
    assert len(out.tables) == 1 and list(out.tables[0].df.columns) == ["A", "B"]


def test_multiheader_flattened():
    html = (b"<table><thead><tr><th>District</th><th colspan=2>Beds</th></tr>"
            b"<tr><th></th><th>Male</th><th>Female</th></tr></thead>"
            b"<tbody><tr><td>Patna</td><td>1</td><td>2</td></tr><tr><td>Gaya</td><td>3</td><td>4</td></tr></tbody></table>")
    cols = [str(c) for c in read(html, "r.xls").tables[0].df.columns]
    assert not any("Unnamed" in c or "(" in c for c in cols)
    assert "Beds Male" in cols


def test_json_record_arrays_nested():
    payload = {"props": {"pageProps": {"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}}}
    out = read(json.dumps(payload).encode(), "data.json")
    assert len(out.tables) == 1
    assert out.tables[0].locator["path"] == "$.props.pageProps.rows"
    assert out.description == "JSON → 1 table"


def test_json_ignores_single_field_or_single_record_lists():
    payload = {"one": [{"a": 1, "b": 2}], "narrow": [{"a": 1}, {"a": 2}], "nums": [1, 2, 3]}
    assert read(json.dumps(payload).encode(), "d.json").tables == []


def test_record_arrays_root_list():
    found = list(files.record_arrays([{"a": 1, "b": 2}, {"a": 3, "b": 4}]))
    assert [p for p, _ in found] == ["$"]


def test_pdf_tables_and_text():
    out = read(pdf_with_table(), "beds.pdf")
    assert len(out.tables) == 1
    t = out.tables[0]
    assert t.kind == "pdf_table" and t.locator["page"] == 1
    assert list(t.df.columns) == ["District", "Beds"] and len(t.df) == 2
    assert "District hospital beds report" in out.texts[0].text
    assert out.texts[0].source == URL + "beds.pdf"
    assert out.description == "PDF, 1 page, 1 table, text"


def test_pdf_without_text_is_flagged_scanned():
    out = read(pdf_scanned(), "scan.pdf")
    assert out.flags == ["scanned"] and out.tables == [] and out.texts == []
    assert out.description.endswith("scanned, no text")


def test_docx_tables_and_text():
    out = read(docx_bytes(), "report.docx")
    assert out.tables[0].kind == "docx_table" and out.tables[0].locator["table_index"] == 0
    assert list(out.tables[0].df.columns) == ["District", "PHCs"]
    assert "district health society" in out.texts[0].text
    assert out.description == "Word, 1 table, text"


def test_zip_one_level():
    inner = zipped({"deep.csv": b"a,b\n1,2\n"})
    out = read(zipped({"a.csv": b"x,y\n1,2\n3,4\n", "inner.zip": inner, "b.exe": b"MZ..."}), "bundle.zip")
    assert len(out.tables) == 1
    assert out.tables[0].locator["inner"] == "a.csv"
    assert out.tables[0].title == "bundle.zip / a.csv"
    assert out.description == "ZIP, 3 files → 1 table"


def test_zip_limits(monkeypatch):
    monkeypatch.setattr(settings, "scrape_zip_max_files", 50)
    content = zipped({f"f{i}.csv": b"a,b\n1,2\n3,4\n" for i in range(51)})
    out = read(content, "many.zip")
    assert len(out.tables) == 50 and "zip_truncated" in out.flags


def test_zip_bomb_by_declared_size(monkeypatch):
    monkeypatch.setattr(settings, "scrape_zip_max_mb", 1)
    content = zipped({"big.csv": b"0" * (2 * 1_048_576), "ok.csv": b"a,b\n1,2\n3,4\n"})
    out = read(content, "bomb.zip")
    assert [t.locator["inner"] for t in out.tables] == ["ok.csv"]
    assert "zip_truncated" in out.flags


def test_unknown_type():
    out = read(b"\x89PNG\r\n\x1a\n....", "x.png")
    assert out.tables == [] and out.description == "Not read (unsupported type)"


def test_corrupt_file_does_not_raise():
    out = read(b"%PDF-1.4 garbage", "broken.pdf")
    assert out.tables == [] and out.description == "PDF, could not be read"


def test_file_kind_detection():
    assert files.file_kind("a.CSV", None, b"") == "csv"
    assert files.file_kind("download", "application/pdf; charset=x", b"") == "pdf"
    assert files.file_kind("download", None, b"%PDF-1.7") == "pdf"
    assert files.file_kind("download", None, b"PK\x03\x04") == "zip"
    assert files.file_kind("download", "text/html", b"<html>") is None


def test_xlsx_without_extension_is_read_as_excel():
    content = xlsx({"S": [["A", "B"], [1, 2], [3, 4]]})
    out = files.read_file(content, "download", URL + "download", None)
    assert len(out.tables) == 1 and out.description.startswith("Excel")


def test_expired_deadline_stops_pdf():
    out = read(pdf_with_table(), "beds.pdf", deadline=Deadline(0))
    assert "timeout" in out.flags and out.tables == []


def test_tables_are_handed_over_one_at_a_time():
    content = xlsx({"Beds": [["District", "Beds"], ["Patna", 3100], ["Gaya", 1250]],
                    "PHCs": [["District", "PHCs"], ["Patna", 42], ["Gaya", 18]]})
    got = []
    out = files.read_file(content, "infra.xlsx", URL + "infra.xlsx", on_table=got.append)
    assert [t.locator["sheet"] for t in got] == ["Beds", "PHCs"]
    assert out.tables == [] and out.description == "Excel, 2 sheets → 2 tables"


def test_zip_unpacked_bytes_count_against_budget():
    part = b"a,b\n" + b"1,2\n" * 150_000  # 0.6 MB
    out = files.read_file(zipped({"one.csv": part, "two.csv": part}), "b.zip", URL + "b.zip",
                          byte_budget=1_048_576)
    assert [t.locator["inner"] for t in out.tables] == ["one.csv"]
    assert "zip_truncated" in out.flags and out.unpacked_bytes == len(part)


def test_single_column_csv_is_not_split():
    df = read(b"District\nPatna\nGaya\n", "one.csv").tables[0].df
    assert list(df.columns) == ["District"] and df["District"].tolist() == ["Patna", "Gaya"]


@pytest.mark.parametrize("raw", [b"Indicator|Value\nA|1\nB|2\n", b"Indicator;Value\nA;1\nB;2\n"])
def test_pipe_and_semicolon_delimiters(raw):
    assert list(read(raw, "x.csv").tables[0].df.columns) == ["Indicator", "Value"]


def test_csv_values_kept_as_published():
    df = read(b"Code,Value,Total\n01,NA,\"1,234\"\n02,N/A,5\n", "codes.csv").tables[0].df
    assert df["Code"].tolist() == ["01", "02"]
    assert df["Value"].tolist() == ["NA", "N/A"]
    assert df["Total"].tolist() == ["1,234", "5"]


def test_latin1_byte_undefined_in_cp1252():
    df = read(b"District,Value\nA\x81b,1\nGaya,2\n", "l.csv").tables[0].df
    assert df.iloc[0, 0] == "A\x81b"


def test_utf16_export():
    raw = "District\tValue\r\nPatna\t1\r\nगया\t2\r\n".encode("utf-16")
    df = read(raw, "unicode.csv").tables[0].df
    assert list(df.columns) == ["District", "Value"] and df.iloc[1, 0] == "गया"
