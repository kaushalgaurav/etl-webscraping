"""SOURCE DETECTION + capability probing.

Given a URL, decide *how* to acquire the data. The three acquisition methods
are deliberately separate code paths (API / file download / HTML parsing), and
this module only chooses between them — it never mixes them.

Priority ladder (never start with a browser):

    1. Official API              -> rest_api / data_gov_in / census_india
    2. Downloadable CSV/Excel    -> csv_excel
    3. Raw JSON/XML payload      -> rest_api
    4. HTML table on the page    -> html_table
    5. JS-rendered page          -> dynamic_site

A page that merely *links to* a CSV is a file source, not a scraping target,
so probe_url() looks for download links before falling back to table parsing.
"""
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from app.core.config import settings
from app.modules.ingestion.connectors import data_gov_in
from app.modules.ingestion.connectors.base import ConnectorError

TABULAR_EXTS = {".csv", ".tsv", ".xlsx", ".xls", ".xlsm"}
DOWNLOAD_EXT_RE = re.compile(r"\.(csv|tsv|xlsx?|xlsm|ods)(\?|$)", re.I)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DataIngestionEngine/1.0)"}

# Hosts with a dedicated API connector — always Priority 1.
KNOWN_API_HOSTS = {
    "censusindia.gov.in": "census_india",
    "api.data.gov.in": "data_gov_in",
}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def detect_connector(url: str) -> str:
    """Pick a connector from the URL alone where possible, else by probing."""
    host = _host(url)

    # Priority 1 — a known official API for this host.
    for known_host, connector in KNOWN_API_HOSTS.items():
        if host.endswith(known_host):
            if connector == "data_gov_in":
                try:
                    data_gov_in.extract_resource_id(url)
                except ConnectorError:
                    continue
            return connector

    if "data.gov.in" in host:
        try:
            data_gov_in.extract_resource_id(url)
            return "data_gov_in"
        except ConnectorError:
            pass

    # Priority 2 — the URL is itself a downloadable file.
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in TABULAR_EXTS:
        return "csv_excel"
    # Priority 3 — a raw JSON payload.
    if suffix == ".json":
        return "rest_api"

    # Nothing decidable from the URL — probe the response.
    return probe_url(url)["recommended_connector"]


def probe_url(url: str) -> dict:
    """Inspect a URL and report what acquisition methods are actually available.

    Returns a capability report rather than just a connector name, so the UI
    can show *why* a method was chosen (BRD "source capability detector").
    """
    report = {
        "url": url,
        "host": _host(url),
        "content_type": None,
        "is_json": False,
        "download_links": [],
        "html_table_count": 0,
        "likely_javascript_rendered": False,
        "tls_verification_failed": False,
        "recommended_connector": "html_table",
        "reason": "",
    }

    try:
        resp = requests.get(url, headers=HEADERS, timeout=settings.request_timeout_seconds)
        resp.raise_for_status()
    except requests.exceptions.SSLError as exc:
        # Several government hosts serve incomplete certificate chains. Report
        # it as its own finding rather than a generic failure — the fix is a
        # per-source trust setting, not a different connector.
        report.update(
            tls_verification_failed=True,
            recommended_connector=None,
            reason=(
                f"TLS certificate verification failed for {report['host']}. The host "
                f"likely serves an incomplete certificate chain; ingesting it needs an "
                f"explicit per-source verify_ssl=false. ({exc})"
            ),
        )
        return report
    except requests.RequestException as exc:
        report.update(
            recommended_connector=None,
            reason=f"Could not fetch URL: {exc}",
        )
        return report

    content_type = (resp.headers.get("Content-Type") or "").lower()
    report["content_type"] = content_type

    # Priority 3 — the endpoint returns JSON directly.
    if "json" in content_type:
        report.update(
            is_json=True, recommended_connector="rest_api",
            reason="Endpoint returns JSON — using the API connector.",
        )
        return report

    # Priority 2 — the endpoint returns a spreadsheet directly.
    if any(token in content_type for token in ("csv", "excel", "spreadsheet")):
        report.update(
            recommended_connector="csv_excel",
            reason="Endpoint returns a CSV/Excel payload — using the file connector.",
        )
        return report

    if "html" not in content_type:
        report["reason"] = f"Unrecognized content type '{content_type}'; defaulting to HTML parsing."
        return report

    soup = BeautifulSoup(resp.text, "html.parser")

    # Priority 2 — the page links to downloadable files.
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if DOWNLOAD_EXT_RE.search(href):
            report["download_links"].append(urljoin(url, href))
    report["download_links"] = list(dict.fromkeys(report["download_links"]))[:25]

    tables = soup.find_all("table")
    report["html_table_count"] = len(tables)

    # A table whose cells are template placeholders isn't real data — that's a
    # JS-rendered page (e.g. censusindia's Vue-driven Population Finder).
    placeholder_markup = bool(re.search(r"\{\{.*?\}\}", resp.text))
    if placeholder_markup:
        report["likely_javascript_rendered"] = True

    if report["download_links"]:
        report.update(
            recommended_connector="csv_excel",
            reason=f"Page links to {len(report['download_links'])} downloadable file(s) — "
                   "preferring the file over scraping the page.",
        )
    elif tables and not placeholder_markup:
        report.update(
            recommended_connector="html_table",
            reason=f"Page has {len(tables)} HTML table(s) and no download links.",
        )
    elif placeholder_markup:
        report.update(
            recommended_connector="dynamic_site",
            reason="Page markup contains unrendered template placeholders — the data "
                   "is loaded by JavaScript, so a headless browser (or the page's own "
                   "API) is required.",
        )
    else:
        report["reason"] = "No tables, downloads or JSON found at this URL."

    return report
