"""Static HTML table connector.

Fetches a web page and extracts <table> elements. When several tables are
present, picks the largest by cell count (rows * columns) as the most
likely "the data" — callers can override via `table_index`.
"""
import requests

import pandas as pd

from app.core.config import settings
from app.modules.ingestion.connectors.base import ConnectorError, IngestResult


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse the MultiIndex headers pandas builds from stacked <th> rows.

    Without this, a Wikipedia-style table exports columns literally named
    "('Rank', 'Rank')" — unusable downstream and impossible to match
    semantically.
    """
    if not isinstance(df.columns, pd.MultiIndex):
        return df

    flattened = []
    for column in df.columns:
        # Drop repeated and unnamed levels ("Rank"/"Rank" -> "Rank").
        parts, seen = [], set()
        for level in column:
            text = str(level).strip()
            if not text or text.startswith("Unnamed:") or text in seen:
                continue
            seen.add(text)
            parts.append(text)
        flattened.append(" ".join(parts) if parts else "column")

    df = df.copy()
    df.columns = flattened
    return df


def ingest(url: str, table_index: int | None = None) -> IngestResult:
    try:
        resp = requests.get(
            url,
            timeout=settings.request_timeout_seconds,
            headers={"User-Agent": "Mozilla/5.0 (compatible; DataIngestionEngine/1.0)"},
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise ConnectorError(f"Failed to fetch page '{url}': {exc}") from exc

    try:
        tables = pd.read_html(resp.text)
    except ValueError as exc:
        raise ConnectorError(f"No HTML tables found on '{url}': {exc}") from exc

    if not tables:
        raise ConnectorError(f"No HTML tables found on '{url}'.")

    warnings: list[str] = []
    if table_index is not None:
        if table_index < 0 or table_index >= len(tables):
            raise ConnectorError(
                f"table_index={table_index} out of range; page has {len(tables)} table(s)."
            )
        df = tables[table_index]
    else:
        # Pick the largest table by cell count as the best guess.
        sizes = [t.shape[0] * t.shape[1] for t in tables]
        best = max(range(len(tables)), key=lambda i: sizes[i])
        df = tables[best]
        if len(tables) > 1:
            warnings.append(
                f"Page had {len(tables)} tables; picked table #{best} "
                f"({df.shape[0]} rows x {df.shape[1]} cols) as the largest. "
                "Pass table_index to choose a different one."
            )

    df = _flatten_columns(df)

    if df.empty:
        raise ConnectorError(f"Selected table on '{url}' has zero rows.")

    return IngestResult(
        dataframe=df,
        connector="html_table",
        source_ref=url,
        raw_bytes=resp.content,
        raw_filename="page.html",
        warnings=warnings,
    )
