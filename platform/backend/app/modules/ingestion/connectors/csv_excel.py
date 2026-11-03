"""CSV / Excel connector.

Handles two entry points: an uploaded file (bytes + filename) or a direct
URL that resolves to a .csv/.xlsx/.xls payload. Validation is intentionally
light here (readable as a table, non-empty) — deeper checks happen in the
profiling stage.
"""
import io
from pathlib import Path

import pandas as pd
import requests

from app.core.config import settings
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DataIngestionEngine/1.0)"}

EXCEL_EXTS = (".xlsx", ".xls", ".xlsm")
CSV_EXTS = (".csv", ".tsv")


def _read_table(raw: bytes, filename: str) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    try:
        if suffix in EXCEL_EXTS:
            return pd.read_excel(io.BytesIO(raw))
        sep = "\t" if suffix == ".tsv" else None
        return pd.read_csv(io.BytesIO(raw), sep=sep, engine="python")
    except Exception as exc:  # pandas raises many different error types
        raise ConnectorError(f"Could not parse '{filename}' as CSV/Excel: {exc}") from exc


def ingest_upload(raw: bytes, filename: str) -> IngestResult:
    df = _read_table(raw, filename)
    if df.empty:
        raise ConnectorError(f"'{filename}' parsed to zero rows.")
    return IngestResult(
        dataframe=df,
        connector="csv_excel",
        source_ref=filename,
        raw_bytes=raw,
        raw_filename=filename,
    )


def ingest_url(url: str) -> IngestResult:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=settings.request_timeout_seconds)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectorError(f"Failed to download '{url}': {exc}") from exc

    filename = Path(url.split("?")[0]).name or "download.csv"
    df = _read_table(resp.content, filename)
    if df.empty:
        raise ConnectorError(f"'{url}' parsed to zero rows.")
    return IngestResult(
        dataframe=df,
        connector="csv_excel",
        source_ref=url,
        raw_bytes=resp.content,
        raw_filename=filename,
    )
