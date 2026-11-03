#!/usr/bin/env python
"""Pass a URL (or a file), get a CSV.

One entry point, three separate extractors — API, CSV/Excel, and web page —
each with its own code path. Nothing is shared between them beyond the shape
of the result, so a change to HTML parsing can never affect an API pull.

    python extract.py <url-or-file> [--type api|file|web|auto] [--out DIR]
                                    [--name NAME] [--table-index N]

Examples:
    # auto-detect (probes the URL, follows the priority ladder)
    python extract.py https://example.gov.in/data.json

    # force a specific extractor
    python extract.py https://example.gov.in/report      --type web
    python extract.py https://example.gov.in/sheet.xlsx  --type file
    python extract.py https://api.data.gov.in/resource/<uuid> --type api

    # a local file
    python extract.py ./downloads/districts.csv

    # Census of India needs area codes, not a URL:
    python extract.py census://districts                 # all India
    python extract.py census://districts?state=10        # one state

Output goes to data/exports/<method>/<method>__<name>__<timestamp>.csv, so an
API pull is never written into the same file as a scraped page.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd

from app.core.config import EXPORTS_DIR
from app.modules.ingestion.connectors import census_india, csv_excel, html_table, rest_api
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult
from app.modules.ingestion.connectors import data_gov_in
from app.modules.cleaning.service import clean_dataframe
from app.modules.ingestion.detector import probe_url
from app.modules.reports.naming import safe_slug

# --------------------------------------------------------------------------
# Extractor 1 — API (JSON over HTTP)
# --------------------------------------------------------------------------

def extract_from_api(url: str) -> IngestResult:
    """JSON REST endpoints, including data.gov.in resources."""
    if "data.gov.in" in urlparse(url).netloc:
        return data_gov_in.ingest(url)
    return rest_api.ingest_url(url)


# --------------------------------------------------------------------------
# Extractor 2 — CSV / Excel (a file, downloaded or local)
# --------------------------------------------------------------------------

def extract_from_file(target: str) -> IngestResult:
    """A spreadsheet: a local path, or a URL that resolves to one."""
    if target.startswith(("http://", "https://")):
        return csv_excel.ingest_url(target)

    path = Path(target).expanduser()
    if not path.exists():
        raise ConnectorError(f"File not found: {path}")
    return csv_excel.ingest_upload(path.read_bytes(), path.name)


# --------------------------------------------------------------------------
# Extractor 3 — Web page (HTML tables)
# --------------------------------------------------------------------------

def extract_from_web(url: str, table_index: int | None = None) -> IngestResult:
    """Parse <table> elements out of a rendered-server-side HTML page."""
    return html_table.ingest(url, table_index=table_index)


# --------------------------------------------------------------------------
# Extractor 4 — Census of India (needs area codes, not a URL)
# --------------------------------------------------------------------------

def extract_from_census(target: str) -> IngestResult:
    """census://districts[?state=10&urbrur=0] -> Census PCA indicators."""
    query = parse_qs(urlparse(target).query)
    kwargs: dict = {"level": "district", "urbrur": 0}
    if "state" in query:
        kwargs["state"] = int(query["state"][0])
    if "urbrur" in query:
        kwargs["urbrur"] = int(query["urbrur"][0])
    if "level" in query:
        kwargs["level"] = query["level"][0]
    return census_india.ingest(**kwargs, progress_callback=lambda m: print(f"    {m}"))


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

EXTRACTORS = {
    "api": "API (JSON over HTTP)",
    "file": "CSV / Excel file",
    "web": "Web page (HTML tables)",
    "census": "Census of India API",
}

# Which connector the probe recommends -> which extractor handles it.
CONNECTOR_TO_TYPE = {
    "rest_api": "api",
    "data_gov_in": "api",
    "data_gov_catalog": "api",
    "census_india": "census",
    "csv_excel": "file",
    "html_table": "web",
    "dynamic_site": "web",
}


def detect_type(target: str) -> tuple[str, str, str]:
    """Return (extractor type, why, resolved target).

    The resolved target can differ from the input: if a web page turns out to
    link to a spreadsheet, the file extractor must be pointed at that download
    link, not at the page it was found on.
    """
    if target.startswith("census://"):
        return "census", "census:// scheme", target

    if not target.startswith(("http://", "https://")):
        return "file", "local filesystem path", target

    suffix = Path(urlparse(target).path).suffix.lower()
    if suffix in {".csv", ".tsv", ".xlsx", ".xls", ".xlsm"}:
        return "file", f"URL ends in {suffix}", target
    if suffix == ".json":
        return "api", "URL ends in .json", target
    if "api." in urlparse(target).netloc or "/api/" in target:
        return "api", "URL looks like an API endpoint", target

    report = probe_url(target)
    connector = report.get("recommended_connector")
    if connector is None:
        raise ConnectorError(f"Could not probe '{target}': {report.get('reason')}")

    kind = CONNECTOR_TO_TYPE.get(connector, "web")
    reason = report.get("reason", "probe result")

    # The page itself isn't a spreadsheet — follow the first file it links to.
    if kind == "file" and report.get("download_links"):
        link = report["download_links"][0]
        return kind, f"{reason} Following: {link}", link

    return kind, reason, target


def run_extractor(kind: str, target: str, table_index: int | None = None) -> IngestResult:
    if kind == "api":
        return extract_from_api(target)
    if kind == "file":
        return extract_from_file(target)
    if kind == "web":
        return extract_from_web(target, table_index=table_index)
    if kind == "census":
        return extract_from_census(target)
    raise ConnectorError(f"Unknown extractor type '{kind}'. Use: {', '.join(EXTRACTORS)}")


def write_csv(df: pd.DataFrame, kind: str, name: str, out_dir: Path) -> Path:
    """One CSV per extraction, foldered by the method that produced it."""
    folder = out_dir / ("api" if kind == "census" else kind)
    folder.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = folder / f"{'api' if kind == 'census' else kind}__{safe_slug(name)}__{stamp}.csv"
    # BOM so Excel renders non-ASCII place names correctly.
    path.write_bytes(df.to_csv(index=False).encode("utf-8-sig"))
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract data from a URL or file into a CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("target", help="URL, local file path, or census://districts")
    parser.add_argument(
        "--type", default="auto", choices=["auto", *EXTRACTORS],
        help="Force an extractor instead of auto-detecting (default: auto).",
    )
    parser.add_argument("--out", default=str(EXPORTS_DIR), help="Output directory.")
    parser.add_argument("--name", default=None, help="Name used in the output filename.")
    parser.add_argument(
        "--table-index", type=int, default=None,
        help="Which HTML table to take (web only); default picks the largest.",
    )
    parser.add_argument(
        "--clean", action="store_true",
        help="Normalize before writing: snake_case headers, trim whitespace, "
             "coerce numerics, drop empty rows/columns and duplicates. "
             "Off by default so the CSV mirrors the source exactly.",
    )
    args = parser.parse_args(argv)

    try:
        if args.type == "auto":
            kind, reason, target = detect_type(args.target)
        else:
            kind, reason, target = args.type, "forced by --type", args.target
        print(f"[detect]  {EXTRACTORS[kind]}  ({reason})")

        print(f"[extract] {target}")
        result = run_extractor(kind, target, table_index=args.table_index)
        df = result.dataframe
        print(f"[data]    {df.shape[0]:,} rows x {df.shape[1]} columns")
        for warning in result.warnings:
            print(f"[warn]    {warning}")

        if args.clean:
            df, clean_report = clean_dataframe(df)
            for step in clean_report:
                print(f"[clean]   {step}")

        name = args.name or result.source_ref or args.target
        path = write_csv(df, kind, name, Path(args.out))
        print(f"[written] {path}")
        return 0

    except ConnectorError as exc:
        print(f"[error]   {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n[aborted]", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
