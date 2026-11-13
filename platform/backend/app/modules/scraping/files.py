"""Turn a downloaded file's bytes into tables and text (spec §4.5).

Files are data, never code: they are only ever handed to the parsers below.
A file that fails to parse is described ("PDF, could not be read"), never
raised, so one bad file cannot sink a whole scan.
"""
import csv
import io
import json
import zipfile
from collections.abc import Callable, Iterator
from pathlib import PurePosixPath

import pandas as pd

from app.core.config import settings
from app.modules.ingestion.connectors.html_table import _flatten_columns
from app.modules.scraping.fetcher import Deadline
from app.modules.scraping.items import FileExtract, TableItem, TextItem

FILE_EXTS = {".csv", ".tsv", ".xls", ".xlsx", ".xlsm", ".json", ".pdf", ".docx", ".zip"}

_EXT_KIND = {".csv": "csv", ".tsv": "tsv", ".xls": "excel", ".xlsx": "excel", ".xlsm": "excel",
             ".json": "json", ".pdf": "pdf", ".docx": "docx", ".zip": "zip"}
_TYPE_KIND = (
    ("text/csv", "csv"), ("tab-separated", "tsv"), ("ms-excel", "excel"),
    ("spreadsheetml", "excel"), ("application/json", "json"), ("application/pdf", "pdf"),
    ("wordprocessingml", "docx"), ("application/zip", "zip"), ("x-zip", "zip"),
)
_LABEL = {"csv": "CSV", "tsv": "TSV", "excel": "Excel", "json": "JSON", "pdf": "PDF",
          "docx": "Word", "zip": "ZIP"}
UNSUPPORTED = "Not read (unsupported type)"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def file_kind(filename: str, content_type: str | None, head: bytes) -> str | None:
    kind = _EXT_KIND.get(PurePosixPath(filename.lower()).suffix)
    if kind:
        return kind
    ctype = (content_type or "").lower()
    for marker, kind in _TYPE_KIND:
        if marker in ctype:
            return kind
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    stripped = head.lstrip()[:1]
    if stripped in (b"{", b"[") and "html" not in ctype:
        return "json"
    return None


def record_arrays(obj, path: str = "$", max_depth: int = 6) -> Iterator[tuple[str, list[dict]]]:
    """Every list of at least 2 records with at least 2 fields, with its path."""
    if max_depth < 0:
        return
    if isinstance(obj, list):
        records = [x for x in obj if isinstance(x, dict)]
        fields = {k for r in records for k in r}
        if len(records) >= 2 and len(fields) >= 2:
            yield path, records
            return
        for i, item in enumerate(obj[:50]):
            yield from record_arrays(item, f"{path}[{i}]", max_depth - 1)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            yield from record_arrays(value, f"{path}.{key}", max_depth - 1)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = _flatten_columns(df)
    df = df.dropna(how="all").dropna(axis=1, how="all")
    return df.set_axis([str(c).strip() for c in df.columns], axis=1)


def _from_rows(rows: list[list]) -> pd.DataFrame | None:
    """First row as header (PDF / Word tables)."""
    rows = [[("" if c is None else str(c).strip()) for c in r] for r in rows if r]
    if len(rows) < 2:
        return None
    header = [h or f"column_{i + 1}" for i, h in enumerate(rows[0])]
    width = len(header)
    body = [(r + [""] * width)[:width] for r in rows[1:]]
    return pd.DataFrame(body, columns=header)


def _decode(raw: bytes) -> str:
    """UTF-16 (Excel's "Unicode Text"), UTF-8, Windows-1252, then Latin-1,
    which accepts any byte, so a file is never refused for its encoding."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _delimiter(text: str) -> str:
    """Comma, semicolon, tab or pipe, judged on the first lines; comma when
    unsure (a one-column file has no delimiter to find)."""
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _read_csv(raw: bytes, sep: str | None) -> pd.DataFrame:
    text = _decode(raw)
    # Every value as text, exactly as published: "NA" stays "NA" and "01"
    # keeps its zero. Only empty cells are missing. The cleaning stage
    # converts numbers after Save, as it does for an upload.
    return pd.read_csv(io.StringIO(text), sep=sep or _delimiter(text), dtype=str,
                       keep_default_na=False, na_values=[""])


def _html_tables(raw: bytes) -> list[pd.DataFrame]:
    text = raw.decode("utf-8", "replace")
    return pd.read_html(io.StringIO(text))


class _Reader:
    def __init__(self, filename: str, url: str, deadline: Deadline | None,
                 base_locator: dict | None = None, title_prefix: str | None = None,
                 on_table: Callable[[TableItem], None] | None = None,
                 byte_budget: int | None = None):
        self.filename = filename
        self.url = url
        self.deadline = deadline
        # With on_table, each table is handed over as soon as it is read and
        # not kept here, so a workbook or ZIP never sits in memory whole.
        self.on_table = on_table
        self.byte_budget = byte_budget
        self.count = 0
        self.locator = base_locator or {"source": "file", "file_url": url}
        self.prefix = title_prefix or filename
        self.out = FileExtract(description=UNSUPPORTED)

    def expired(self) -> bool:
        if self.deadline is not None and self.deadline.expired():
            if "timeout" not in self.out.flags:
                self.out.flags.append("timeout")
            return True
        return False

    def table(self, kind: str, title: str, df: pd.DataFrame, **loc) -> None:
        df = _clean(df)
        if df.empty or df.shape[1] == 0:
            return
        item = TableItem(kind, title, {**self.locator, **loc}, df)
        self.count += 1
        if self.on_table is not None:
            self.on_table(item)
        else:
            self.out.tables.append(item)

    def text(self, text: str) -> None:
        text = text.strip()
        if text:
            self.out.texts.append(TextItem(f"Text of {self.prefix}", self.url, text))

    # one method per kind ----------------------------------------------------
    def csv(self, raw: bytes, sep: str | None = None) -> str:
        self.table("file_table", self.prefix, _read_csv(raw, sep))
        return f"{'TSV' if sep else 'CSV'} → {_plural(self.count, 'table')}"

    def tsv(self, raw: bytes) -> str:
        return self.csv(raw, "\t")

    def excel(self, raw: bytes) -> str:
        try:
            sheets = pd.read_excel(io.BytesIO(raw), sheet_name=None)
        except Exception:
            # Many government .xls files are HTML tables (HMIS's are).
            frames = _html_tables(raw)
            for i, df in enumerate(frames):
                self.table("file_table", self.prefix if len(frames) == 1 else f"{self.prefix} - table {i + 1}",
                           df, table_index=i)
            return f"Excel (web page format) → {_plural(self.count, 'table')}"
        for name, df in sheets.items():
            self.table("file_table", f"{self.prefix} - {name}", df, sheet=name)
        return f"Excel, {_plural(len(sheets), 'sheet')} → {_plural(self.count, 'table')}"

    def json(self, raw: bytes) -> str:
        payload = json.loads(raw.decode("utf-8-sig"))
        for path, records in record_arrays(payload):
            self.table("file_table", f"{self.prefix} {path}", pd.json_normalize(records), path=path)
        return f"JSON → {_plural(self.count, 'table')}"

    def pdf(self, raw: bytes) -> str:
        import pdfplumber

        has_text = False
        with pdfplumber.open(io.BytesIO(raw)) as doc:
            pages = len(doc.pages)
            texts = []
            for number, page in enumerate(doc.pages, 1):
                if self.expired():
                    break
                text = page.extract_text() or ""
                if text.strip():
                    has_text = True
                    texts.append(text)
                for i, rows in enumerate(page.extract_tables()):
                    df = _from_rows(rows)
                    if df is not None:
                        self.table("pdf_table", f"{self.prefix} - page {number} table {i + 1}",
                                   df, page=number, table_index=i)
        if "timeout" in self.out.flags:
            return f"PDF, {_plural(pages, 'page')}, stopped early"
        if not has_text and not self.count:
            self.out.flags.append("scanned")
            return f"PDF, {_plural(pages, 'page')}, scanned, no text"
        self.text("\n\n".join(texts))
        parts = [f"PDF, {_plural(pages, 'page')}"]
        if self.count:
            parts.append(_plural(self.count, "table"))
        if has_text:
            parts.append("text")
        return ", ".join(parts)

    def docx(self, raw: bytes) -> str:
        from docx import Document

        doc = Document(io.BytesIO(raw))
        self.text("\n".join(p.text for p in doc.paragraphs))
        for i, t in enumerate(doc.tables):
            df = _from_rows([[cell.text for cell in row.cells] for row in t.rows])
            if df is not None:
                self.table("docx_table", f"{self.prefix} - table {i + 1}", df, table_index=i)
        parts = ["Word"]
        if self.count:
            parts.append(_plural(self.count, "table"))
        if self.out.texts:
            parts.append("text")
        return ", ".join(parts)

    def zip(self, raw: bytes) -> str:
        archive = zipfile.ZipFile(io.BytesIO(raw))
        names = archive.namelist()
        # An .xlsx or .docx is itself a ZIP; read it as what it is.
        if "xl/workbook.xml" in names:
            return self.excel(raw)
        if "word/document.xml" in names:
            return self.docx(raw)
        entries = [i for i in archive.infolist() if not i.is_dir()]
        max_files = settings.scrape_zip_max_files
        budget = settings.scrape_zip_max_mb * 1_048_576
        if self.byte_budget is not None:
            budget = min(budget, self.byte_budget)
        used = read_count = 0
        for info in entries:
            if self.expired():
                break
            if read_count >= max_files:
                self.out.flags.append("zip_truncated")
                break
            name = info.filename
            kind = file_kind(name, None, b"")
            if kind in (None, "zip"):
                continue  # nested archives and unknown types are ignored
            if used + info.file_size > budget:
                self._flag_truncated()
                continue
            with archive.open(info) as handle:
                # Capped read: a header can lie about the unpacked size.
                data = handle.read(budget - used + 1)
            if used + len(data) > budget:
                self._flag_truncated()
                continue
            used += len(data)
            read_count += 1
            inner = _Reader(name, self.url, self.deadline,
                            base_locator={**self.locator, "inner": name},
                            title_prefix=f"{self.prefix} / {name}", on_table=self.on_table)
            inner.read(data, kind)
            del data
            self.count += inner.count
            self.out.tables.extend(inner.out.tables)
            self.out.texts.extend(inner.out.texts)
        self.out.unpacked_bytes = used
        return f"ZIP, {_plural(len(entries), 'file')} → {_plural(self.count, 'table')}"

    def _flag_truncated(self) -> None:
        if "zip_truncated" not in self.out.flags:
            self.out.flags.append("zip_truncated")

    def read(self, raw: bytes, kind: str | None) -> FileExtract:
        if kind is None:
            self.out.description = UNSUPPORTED
            return self.out
        try:
            self.out.description = getattr(self, kind)(raw)
        except Exception:
            self.out.tables.clear()
            self.out.texts.clear()
            self.out.description = f"{_LABEL[kind]}, could not be read"
        return self.out


def read_file(content: bytes, filename: str, url: str, content_type: str | None = None,
              deadline: Deadline | None = None,
              on_table: Callable[[TableItem], None] | None = None,
              byte_budget: int | None = None) -> FileExtract:
    """`on_table`: receive each table as it is read instead of in the result.
    `byte_budget`: most bytes a ZIP may unpack (the scan's remaining total)."""
    kind = file_kind(filename, content_type, content[:16])
    return _Reader(filename, url, deadline, on_table=on_table,
                   byte_budget=byte_budget).read(content, kind)
