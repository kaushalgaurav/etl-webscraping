"""Read one HTML page fetched over plain HTTP (spec §4.2).

Finds the page's tables, data embedded in its scripts, links to data files,
and its main readable text, and notes two things the scan reports rather
than acts on: the page is an empty JavaScript shell (needs a browser, which
is phase 2), or its data sits behind a form.
"""
import io
import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import urldefrag, urljoin, urlsplit

import pandas as pd
from bs4 import BeautifulSoup

from app.modules.ingestion.connectors.html_table import _flatten_columns
from app.modules.scraping.blocked import is_form_gated
from app.modules.scraping.files import FILE_EXTS, record_arrays
from app.modules.scraping.items import TableItem, TextItem

# Below this much visible text, with no tables, a page is treated as a shell
# its JavaScript fills in (plan's call; the spec leaves the rule open).
SHELL_MAX_TEXT = 200
_APP_ROOTS = ("root", "app", "__next")
_HEADINGS = ["h1", "h2", "h3", "h4"]


@dataclass
class PageScan:
    title: str | None
    tables: list[TableItem] = field(default_factory=list)
    file_links: list[str] = field(default_factory=list)
    text: TextItem | None = None
    js_shell: bool = False
    form_gated: bool = False


def _title_for(tag, number: int) -> str:
    caption = tag.find("caption")
    if caption and caption.get_text(strip=True):
        return caption.get_text(" ", strip=True)
    heading = tag.find_previous(_HEADINGS)
    if heading and heading.get_text(strip=True):
        return heading.get_text(" ", strip=True)
    return f"Table {number}"


def _tables(soup: BeautifulSoup) -> list[TableItem]:
    found: list[TableItem] = []
    for index, tag in enumerate(soup.find_all("table")):
        # A table holding other tables is page layout; its inner tables are
        # read on their own.
        if tag.find("table"):
            continue
        try:
            frames = pd.read_html(io.StringIO(str(tag)))
        except (ValueError, IndexError):
            continue
        if not frames:
            continue
        df = _flatten_columns(frames[0]).dropna(how="all")
        if df.empty:
            continue
        df = df.set_axis([str(c).strip() for c in df.columns], axis=1)
        found.append(TableItem("html_table", _title_for(tag, len(found) + 1),
                               {"source": "page", "table_index": index}, df))
    return found


def _embedded(soup: BeautifulSoup) -> list[TableItem]:
    found: list[TableItem] = []
    scripts = soup.find_all("script", attrs={"type": ["application/ld+json", "application/json"]})
    scripts += [s for s in soup.find_all("script", id="__NEXT_DATA__") if s not in scripts]
    for script in scripts:
        try:
            payload = json.loads(script.string or "")
        except ValueError:
            continue
        for path, records in record_arrays(payload):
            df = pd.json_normalize(records)
            found.append(TableItem("embedded", f"Page data {path}",
                                   {"source": "page", "path": path}, df))
    return found


def _file_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    base_host = (urlsplit(base_url).hostname or "").lower()
    same, other, seen = [], [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        url, _fragment = urldefrag(urljoin(base_url, href))
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            continue
        if PurePosixPath(parts.path.lower()).suffix not in FILE_EXTS or url in seen:
            continue
        seen.add(url)
        (same if (parts.hostname or "").lower() == base_host else other).append(url)
    return same + other


def _main_text(html: str) -> TextItem | None:
    import trafilatura

    text = trafilatura.extract(html, include_tables=False, include_comments=False)
    return TextItem("Page text", "page", text) if text and text.strip() else None


def _is_shell(soup: BeautifulSoup, has_data: bool) -> bool:
    for root_id in _APP_ROOTS:
        root = soup.find(id=root_id)
        if root is not None and not root.get_text(strip=True) and not root.find(True):
            return True
    # Tables, embedded records or file links mean the page already gave us
    # something to read, however little text it has (a bare downloads page).
    if has_data:
        return False
    body = soup.body or soup
    for tag in body(["script", "style", "noscript", "template"]):
        tag.decompose()
    return len(" ".join(body.get_text(" ").split())) < SHELL_MAX_TEXT


def parse_page(html: str, base_url: str) -> PageScan:
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else None
    tables = _tables(soup) + _embedded(soup)
    scan = PageScan(
        title=title or None,
        tables=tables,
        file_links=_file_links(soup, base_url),
        text=_main_text(html),
        form_gated=is_form_gated(html, len(tables)),
    )
    # Last: it strips scripts from the soup.
    scan.js_shell = _is_shell(soup, bool(scan.tables or scan.file_links))
    return scan
